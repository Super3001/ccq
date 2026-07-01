"""ccq_core —— Claude Code session 日志解析底座 (Layer 0 + locate).

把 ~/.claude/projects 下的 .jsonl 会话日志解析成归一化 Event 流,
并提供 session 定位(主文件 + 子 agent 文件)。

可被其它 ccq 模块 import,也可作为 agent 直接写脚本的逃生口:
    from scripts.ccq_core import load_session, Event, locate
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Iterator, Optional


def projects_root() -> Path:
    """~/.claude/projects 的绝对路径(跨平台)。"""
    return Path(os.path.expanduser("~")) / ".claude" / "projects"


# --------------------------------------------------------------------------- #
# 归一化事件模型
# --------------------------------------------------------------------------- #
class Kind(str, Enum):
    """事件种类——查询语言里的 kind= 谓词直接用这些值。"""
    HUMAN = "human"          # 人类说的话(type:user 且非工具结果、非 meta)
    AGENT = "agent"          # agent 的文本回复(assistant text block)
    THINKING = "thinking"    # agent 思考(assistant thinking block)
    TOOL = "tool"            # 工具调用(assistant tool_use block)
    RESULT = "result"        # 工具结果(user 里的 tool_result block)
    SKILL = "skill"          # skill / slash 命令触发
    SYSTEM = "system"        # 系统事件(含 subtype)
    SUMMARY = "summary"      # 压缩摘要 / ai-title
    META = "meta"            # 模式切换、快照等元事件
    OTHER = "other"


@dataclass
class Event:
    """一条归一化事件。seq 为该文件内的稳定序号,用于 show <seq> 钻取。"""
    seq: int
    kind: Kind
    ts: Optional[str]                 # ISO 时间戳
    text: str = ""                    # 人类/agent 文本,或结果摘要
    tool_name: Optional[str] = None
    tool_input: Any = None
    tool_use_id: Optional[str] = None
    is_error: Optional[bool] = None
    tokens_in: int = 0
    tokens_out: int = 0
    sidechain: bool = False
    agent_type: Optional[str] = None  # 该事件所属子 agent 的类型(主 agent 为 None)
    agent_id: Optional[str] = None    # 该事件所属子 agent 的唯一标识(= spawn 的 toolUseId)
    uuid: Optional[str] = None
    parent_uuid: Optional[str] = None
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def epoch(self) -> float:
        """时间戳转 epoch 秒,无则 0。用于排序/区间。"""
        if not self.ts:
            return 0.0
        try:
            from datetime import datetime
            return datetime.fromisoformat(self.ts.replace("Z", "+00:00")).timestamp()
        except Exception:
            return 0.0

    def summary(self, n: int = 100) -> str:
        """该事件的一句话摘要——「这个动作干了什么」的概括。
        最小实现:取代表性内容(TOOL 取调用参数,其余取文本)前 n 字符、折叠空白。
        后续可按 kind 细化(工具取标识性参数、结果取成败+首行等)。"""
        s = self.text or ""
        if self.kind == Kind.TOOL and self.tool_input is not None:
            s = json.dumps(self.tool_input, ensure_ascii=False)
        s = " ".join(s.split())
        return s[:n] + ("…" if len(s) > n else "")


# slash 命令 / skill 触发的识别
_SLASH_RE = re.compile(r"(?m)^\s*/([a-z0-9][a-z0-9:_-]*)\b")
_SKILL_CMD_TAG = re.compile(r"<command-name>\s*/?([^<\s]+)", re.I)

# Codex 自定义 prompt/skill:消息开头的 $<name>(原样存进 user_message,可链式 $a $b)。
# 区别于 Claude 侧 /slash。_DOLLAR_TOK 抽单个 $token;_DOLLAR_LEAD 框定开头连续的
# $token 串,只在这段里抽,避免误命中正文里的 $VAR / $价格。
_DOLLAR_TOK = re.compile(r"\$([a-z0-9][a-z0-9:_-]*)")
_DOLLAR_LEAD = re.compile(r"^\s*((?:\$[a-z0-9][a-z0-9:_-]*[ \t]*)+)")
# rollout 文件名 / spawn 输出里的会话 uuid
_UUID_RE = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")


def _codex_skill_names(text: str) -> list[str]:
    """Codex skill 触发:消息开头连续的 $<name>(支持 $a $b 链式)。无则空。"""
    m = _DOLLAR_LEAD.match(text or "")
    return _DOLLAR_TOK.findall(m.group(1)) if m else []


def _block_text(content: Any) -> str:
    """把 message.content(string 或 block 列表)拍平成纯文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text":
                out.append(b.get("text", ""))
            elif b.get("type") == "thinking":
                out.append(b.get("thinking", ""))
        return "\n".join(out)
    return ""


def _result_text(block: dict) -> str:
    c = block.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return " ".join(
            x.get("text", "") for x in c if isinstance(x, dict) and x.get("type") == "text"
        )
    return ""


def parse_events(path: str | Path, agent_type: Optional[str] = None,
                 agent_id: Optional[str] = None) -> list[Event]:
    """解析单个 .jsonl 文件为 Event 列表。
    agent_type/agent_id 用于标注子 agent 文件(同类型多实例靠 agent_id 区分)。"""
    path = Path(path)
    events: list[Event] = []
    seq = 0
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = o.get("type")
            ts = o.get("ts") or o.get("timestamp")
            side = bool(o.get("isSidechain"))
            common = dict(
                ts=ts, sidechain=side, agent_type=agent_type, agent_id=agent_id,
                uuid=o.get("uuid"), parent_uuid=o.get("parentUuid"), raw=o,
            )

            if t == "assistant":
                msg = o.get("message", {}) or {}
                usage = msg.get("usage", {}) or {}
                # 只算真正新增的 input(prompt + 新建缓存),不累加 cache_read
                # ——cache_read 是每轮重复的上下文,累加会得到天文数字。
                tin = (usage.get("input_tokens", 0) or 0) + \
                      (usage.get("cache_creation_input_tokens", 0) or 0)
                tout = usage.get("output_tokens", 0) or 0
                first = True
                for b in (msg.get("content") or []):
                    if not isinstance(b, dict):
                        continue
                    bt = b.get("type")
                    if bt == "text":
                        seq += 1
                        events.append(Event(seq, Kind.AGENT, text=b.get("text", ""),
                                            tokens_in=tin if first else 0,
                                            tokens_out=tout if first else 0, **common))
                        first = False
                    elif bt == "thinking":
                        seq += 1
                        events.append(Event(seq, Kind.THINKING, text=b.get("thinking", ""),
                                            **common))
                    elif bt == "tool_use":
                        # agent 用 Skill 工具调起 skill → 额外发一条 SKILL 事件,
                        # 与人类手敲 /slash 统一成 Kind.SKILL,被 skills/skill-report 捕获。
                        if b.get("name") == "Skill":
                            sk = (b.get("input") or {}).get("skill", "")
                            if sk:
                                seq += 1
                                events.append(Event(seq, Kind.SKILL, text=sk,
                                                    tool_use_id=b.get("id"), **common))
                        seq += 1
                        events.append(Event(seq, Kind.TOOL, text=b.get("name", ""),
                                            tool_name=b.get("name"), tool_input=b.get("input"),
                                            tool_use_id=b.get("id"),
                                            tokens_out=tout if first else 0,
                                            tokens_in=tin if first else 0, **common))
                        first = False

            elif t == "user":
                msg = o.get("message", {}) or {}
                content = msg.get("content")
                # 工具结果?
                result_blocks = [b for b in (content or [])
                                 if isinstance(b, dict) and b.get("type") == "tool_result"] \
                                if isinstance(content, list) else []
                if result_blocks:
                    for b in result_blocks:
                        seq += 1
                        events.append(Event(seq, Kind.RESULT, text=_result_text(b)[:4000],
                                            tool_use_id=b.get("tool_use_id"),
                                            is_error=bool(b.get("is_error")), **common))
                    continue
                if o.get("isMeta"):
                    continue
                txt = _block_text(content)
                if not txt.strip():
                    continue
                # 人类消息里夹带 skill / slash 触发?——额外发一条 SKILL 事件
                kind = Kind.HUMAN
                m = _SKILL_CMD_TAG.search(txt) or _SLASH_RE.search(txt)
                if m:
                    seq += 1
                    events.append(Event(seq, Kind.SKILL, text=m.group(1), **common))
                seq += 1
                events.append(Event(seq, kind, text=txt, **common))

            elif t == "system":
                seq += 1
                events.append(Event(seq, Kind.SYSTEM, text=str(o.get("content", ""))[:2000],
                                    **common))
            elif t in ("summary", "ai-title"):
                seq += 1
                events.append(Event(seq, Kind.SUMMARY,
                                    text=o.get("summary") or o.get("aiTitle") or "", **common))
            elif t in ("mode", "permission-mode", "file-history-snapshot",
                       "last-prompt", "attachment"):
                seq += 1
                events.append(Event(seq, Kind.META, text=t, **common))
    return events


# --------------------------------------------------------------------------- #
# session 定位
# --------------------------------------------------------------------------- #
@dataclass
class SubAgent:
    agent_type: str
    description: str
    tool_use_id: str
    path: Path
    depth: int = 0                    # 调用树深度(0=主会话直接子 agent;Claude 侧恒 0)
    parent_id: Optional[str] = None   # 父 spawn 的 tool_use_id;None=根的直接子 agent


@dataclass
class SessionFiles:
    session_id: str
    project: str
    main: Path
    subagents: list[SubAgent] = field(default_factory=list)
    mtime: float = 0.0

    def all_paths(self) -> list[Path]:
        return [self.main] + [s.path for s in self.subagents]


def _subagents_for(main: Path) -> list[SubAgent]:
    """主文件旁的 <sid>/subagents/ 目录里的子 agent 文件 + meta。"""
    sub_dir = main.with_suffix("") / "subagents"
    out: list[SubAgent] = []
    if not sub_dir.is_dir():
        return out
    for jf in sorted(sub_dir.glob("agent-*.jsonl")):
        meta = jf.with_suffix(".meta.json")
        at, desc, tid = "?", "", ""
        if meta.exists():
            try:
                m = json.loads(meta.read_text(encoding="utf-8"))
                at = m.get("agentType", "?")
                desc = m.get("description", "")
                tid = m.get("toolUseId", "")
            except Exception:
                pass
        out.append(SubAgent(at, desc, tid, jf))
    return out


def _session_from_main(main: Path) -> SessionFiles:
    return SessionFiles(
        session_id=main.stem,
        project=main.parent.name,
        main=main,
        subagents=_subagents_for(main),
        mtime=main.stat().st_mtime,
    )


def iter_main_logs() -> Iterator[Path]:
    """所有项目下的主会话文件(不含 subagents 子目录里的)。"""
    root = projects_root()
    if not root.is_dir():
        return
    for proj in root.iterdir():
        if not proj.is_dir():
            continue
        for jf in proj.glob("*.jsonl"):
            yield jf


def encode_project_dir(path: str) -> str:
    """把一个工作目录路径编码成 ~/.claude/projects 下的目录名。
    Claude Code 用 '-' 替换路径里所有非字母数字字符
    (盘符冒号、正反斜杠、点号皆然),故编码是有损的——
    `C:\\w\\HomeTrans-CJ` 与 `C-w-HomeTrans-CJ` 会撞名。诊断场景可接受。"""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def iter_projects(selector: str = "") -> list[Path]:
    """匹配 selector 的项目目录。
    - selector 含路径分隔符(/ \\ :)→ 视为工作目录路径,编码后做
      精确匹配 + 子目录前缀匹配(把 .claude 等子目录会话一并纳入)。
    - 否则视为关键字,对目录名做大小写无关子串匹配。
    - 空 selector → 全部项目目录。
    返回按目录名排序的列表。"""
    root = projects_root()
    if not root.is_dir():
        return []
    if selector and re.search(r"[\\/:]", selector):
        enc = encode_project_dir(selector).rstrip("-")
        hits = [p for p in root.iterdir() if p.is_dir()
                and (p.name == enc or p.name.startswith(enc + "-"))]
    else:
        kw = selector.lower()
        hits = [p for p in root.iterdir() if p.is_dir()
                and (kw in p.name.lower() if kw else True)]
    return sorted(hits, key=lambda p: p.name)


def list_sessions(selector: str = "", codex: bool = False) -> list[SessionFiles]:
    """列出匹配项目目录下的全部主会话(各自带子 agent 列表)。
    只读目录与 meta(不解析事件),对大量会话也轻量。按 mtime 降序。
    codex=True 时改读 ~/.codex 下的 Codex 会话(见 list_codex_sessions)。"""
    if codex:
        return list_codex_sessions(selector)
    out: list[SessionFiles] = []
    for proj in iter_projects(selector):
        for jf in proj.glob("*.jsonl"):
            out.append(_session_from_main(jf))
    out.sort(key=lambda s: s.mtime, reverse=True)
    return out


def locate(selector: str, codex: bool = False) -> SessionFiles:
    """把 selector 解析为一个 session。支持:
    - 直接文件路径(.jsonl)
    - 'latest' / 'latest:<proj关键字>'
    - partial session-id 前缀(如 'd74d')
    抛 LookupError 表示找不到或歧义。
    codex=True 时改走 Codex 定位(见 locate_codex)。
    """
    if codex:
        return locate_codex(selector)
    # 1) 直接路径
    p = Path(os.path.expanduser(os.path.expandvars(selector)))
    if p.suffix == ".jsonl" and p.exists():
        # 若给的是子 agent 文件,回溯到主文件
        if p.parent.name == "subagents":
            main = p.parent.parent.with_suffix(".jsonl")
            if main.exists():
                return _session_from_main(main)
        return _session_from_main(p)

    # 2) latest [: 项目关键字]
    if selector == "latest" or selector.startswith("latest:"):
        kw = selector.split(":", 1)[1].lower() if ":" in selector else ""
        cands = [m for m in iter_main_logs() if (kw in m.parent.name.lower() if kw else True)]
        if not cands:
            raise LookupError(f"latest: 无匹配 (kw={kw!r})")
        return _session_from_main(max(cands, key=lambda m: m.stat().st_mtime))

    # 3) partial id 前缀
    matches = [m for m in iter_main_logs() if m.stem.startswith(selector)]
    if not matches:
        # 退一步:子串匹配
        matches = [m for m in iter_main_logs() if selector in m.stem]
    if not matches:
        raise LookupError(f"无 session 匹配 {selector!r}")
    if len(matches) > 1:
        ids = ", ".join(sorted(m.stem[:12] for m in matches)[:8])
        raise LookupError(f"歧义:{len(matches)} 个 session 匹配 {selector!r}: {ids} ...")
    return _session_from_main(matches[0])


def load_session(selector: str, include_subagents: bool = True,
                 codex: bool = False) -> tuple[SessionFiles, list[Event]]:
    """定位并加载一个 session 的全部事件(主 + 可选子 agent)。
    子 agent 事件接在主事件之后,seq 在各自文件内独立,但加全局偏移避免冲突。
    codex=True 时改读 Codex 会话:子 agent 是同档独立 rollout,经 spawn_agent
    链接重建调用树,子事件标 agent_type/agent_id(=spawn call_id)/sidechain。"""
    if codex:
        sf = locate_codex(selector)
        events = parse_codex_events(sf.main)
        if include_subagents:
            sf.subagents = _codex_subagents_for(sf.main)
            offset = events[-1].seq if events else 0
            for sub in sf.subagents:
                sub_ev = parse_codex_events(sub.path)
                for e in sub_ev:
                    e.seq += offset
                    e.agent_type = sub.agent_type
                    e.agent_id = sub.tool_use_id   # = spawn call_id,对齐 digest 键
                    e.sidechain = True
                events.extend(sub_ev)
                offset += (sub_ev[-1].seq - offset) if sub_ev else 0
        return sf, events
    sf = locate(selector)
    events = parse_events(sf.main)
    if include_subagents:
        offset = (events[-1].seq if events else 0)
        for sub in sf.subagents:
            # agent_id 用 spawn 的 toolUseId,唯一标识该子 agent(同类型多实例可区分)
            sub_ev = parse_events(sub.path, agent_type=sub.agent_type,
                                  agent_id=sub.tool_use_id or sub.path.stem)
            for e in sub_ev:
                e.seq += offset
            events.extend(sub_ev)
            offset += (sub_ev[-1].seq - offset if sub_ev else 0)
    return sf, events


# --------------------------------------------------------------------------- #
# Codex (OpenAI Codex CLI) 日志适配
# --------------------------------------------------------------------------- #
# Codex 与 Claude Code 的日志差异:
#   - 存储:~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl,按日期归档,不按工作目录。
#     每个 rollout 的首行 session_meta.payload 记录该会话的 cwd —— 「目录→会话」靠它反查。
#   - 行结构:{timestamp, type, payload}。type ∈ session_meta / response_item /
#     event_msg / turn_context / compacted。
#   - 取材去重:人类/agent 文本、思考、token 取自 event_msg(干净的回合级事件);
#     工具调用/结果取自 response_item;response_item 里的 message/reasoning 跳过
#     (与 event_msg 重复,且夹带注入的 AGENTS.md/权限上下文)。
#   - 子 agent:**是独立 rollout 文件**(不在子目录,按日期与主会话同档)。主会话
#     spawn_agent(function_call)→ 同 call_id 的 function_call_output.output 含
#     {"agent_id":<子uuid>} → 子 rollout 文件名末段即该 uuid;子文件首行
#     session_meta 反记 parent_thread_id / thread_source="subagent"。详见
#     _codex_subagents_for。skill 触发用 $<name> 前缀(见 _codex_skill_names)。
def codex_sessions_root() -> Path:
    """~/.codex/sessions 的绝对路径。"""
    return Path(os.path.expanduser("~")) / ".codex" / "sessions"


def iter_codex_logs() -> Iterator[Path]:
    """所有 Codex rollout 会话文件。"""
    root = codex_sessions_root()
    if not root.is_dir():
        return
    yield from root.glob("**/rollout-*.jsonl")


def codex_session_meta(path: str | Path) -> dict:
    """读 rollout 文件首行 session_meta.payload(含 id / cwd / timestamp /
    model_provider 等)。首个非空行不是 session_meta 或读取失败时返回 {}。"""
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                o = json.loads(line)
                return o.get("payload", {}) or {} if o.get("type") == "session_meta" else {}
    except Exception:
        pass
    return {}


def _norm_dir(p: str) -> str:
    """工作目录归一化:统一分隔符、大小写、去尾分隔符,供 cwd 匹配。
    兼容用户传入的正斜杠路径(C:/w/x)与日志里的反斜杠(C:\\w\\x)。"""
    return os.path.normcase(os.path.normpath(p.strip())) if p.strip() else ""


def _codex_session_from(path: Path, meta: dict | None = None) -> SessionFiles:
    meta = codex_session_meta(path) if meta is None else meta
    return SessionFiles(
        session_id=meta.get("id") or path.stem,
        project=meta.get("cwd", "?"),       # Codex 的「项目」= 该会话的工作目录
        main=Path(path),
        subagents=[],
        mtime=path.stat().st_mtime,
    )


def _codex_is_subagent(meta: dict) -> bool:
    """该 Codex 会话是否为某次 spawn_agent 派生的子 agent(非顶层会话)。"""
    return meta.get("thread_source") == "subagent" or bool(meta.get("parent_thread_id"))


def _codex_logs_index() -> dict[str, Path]:
    """uuid → rollout 文件路径(全量 Codex 会话一次性建索引,供子 agent 反查)。"""
    idx: dict[str, Path] = {}
    for f in iter_codex_logs():
        m = _UUID_RE.search(f.name)
        if m:
            idx[m.group(1)] = f
    return idx


def _codex_spawn_role(message: str) -> tuple[str, str]:
    """从 spawn_agent 的 message 提取 (role, 简短描述)。
    role 取所引用的 `agents/<role>.md`;描述附上 Stage 标记(若有)。"""
    msg = message or ""
    rm = re.search(r"agents[\\/]+([A-Za-z0-9_-]+)\.md", msg)
    role = rm.group(1) if rm else "worker"
    sm = re.search(r"\bStage\s+([0-9][0-9A-Za-z]*(?:\s+Round\s+\d+|[a-z])?)", msg)
    desc = f"Stage {sm.group(1)} · {role}" if sm else role
    return role, desc


def _codex_direct_spawns(main: Path) -> list[tuple[str, dict, str]]:
    """扫一个 rollout 文件里的 spawn_agent,返回 [(call_id, arguments, 子uuid)]。
    子 uuid 取自同 call_id 的 function_call_output.output 里的 agent_id。"""
    spawns: dict[str, dict] = {}     # call_id → arguments
    child: dict[str, str] = {}       # call_id → 子 uuid
    try:
        with open(main, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if o.get("type") != "response_item":
                    continue
                p = o.get("payload", {}) or {}
                pt = p.get("type")
                if pt == "function_call" and p.get("name") == "spawn_agent":
                    args = p.get("arguments")
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except Exception:
                            args = {}
                    spawns[p.get("call_id")] = args or {}
                elif pt == "function_call_output" and p.get("call_id") in spawns:
                    out = p.get("output")
                    txt = out if isinstance(out, str) else json.dumps(out)
                    m = _UUID_RE.search(txt or "")
                    if m:
                        child[p.get("call_id")] = m.group(1)
    except OSError:
        return []
    return [(cid, args, child.get(cid, "")) for cid, args in spawns.items()]


def _codex_subagents_for(main: Path, idx: dict[str, Path] | None = None,
                         depth: int = 0, parent_id: Optional[str] = None,
                         _seen: set[str] | None = None) -> list[SubAgent]:
    """递归重建一个 Codex 主 rollout 的子 agent 调用树(扁平列表,各带 depth)。
    spawn_agent → function_call_output(agent_id)→ 子 rollout 文件 → 再递归求孙。
    tool_use_id 用 spawn 的 call_id(与子事件 agent_id 对齐);封顶防环。"""
    if idx is None:
        idx = _codex_logs_index()
    if _seen is None:
        _seen = set()
    if depth > 6:
        return []
    out: list[SubAgent] = []
    for cid, args, uuid in _codex_direct_spawns(main):
        path = idx.get(uuid) if uuid else None
        if not path or uuid in _seen:
            continue
        _seen.add(uuid)
        role, desc = _codex_spawn_role(args.get("message", ""))
        out.append(SubAgent(agent_type=role, description=desc, tool_use_id=cid,
                            path=path, depth=depth, parent_id=parent_id))
        out.extend(_codex_subagents_for(path, idx, depth + 1, cid, _seen))
    return out


def list_codex_sessions(selector: str = "") -> list[SessionFiles]:
    """列出匹配 selector 的 Codex 会话(按 mtime 降序)。
    - selector 含路径分隔符 → 视为工作目录:精确 cwd 匹配 + 子目录(把目录下
      子工程的会话一并纳入,与 Claude 侧 .claude 子目录行为对齐)。
    - 否则视为关键字,对 cwd 做大小写无关子串匹配。
    - 空 → 全部。"""
    is_path = bool(selector) and bool(re.search(r"[\\/:]", selector))
    nsel = _norm_dir(selector) if is_path else ""
    out: list[SessionFiles] = []
    for f in iter_codex_logs():
        m = codex_session_meta(f)
        if _codex_is_subagent(m):   # 子 agent 会话不进顶层列表(经父会话 agents 钻取)
            continue
        cwd = m.get("cwd", "") or ""
        if selector:
            if is_path:
                ncwd = _norm_dir(cwd)
                if not (ncwd == nsel or ncwd.startswith(nsel + os.sep)):
                    continue
            elif selector.lower() not in cwd.lower():
                continue
        out.append(_codex_session_from(f, m))
    out.sort(key=lambda s: s.mtime, reverse=True)
    return out


def locate_codex(selector: str) -> SessionFiles:
    """把 selector 解析为一个 Codex 会话(目录有多个会话时取最近的一个)。支持:
    - 直接 rollout-*.jsonl 路径
    - 工作目录(含路径分隔符)→ cwd 精确匹配
    - 'latest' / 'latest:<目录或关键字>'
    - rollout uuid / 文件名前缀
    抛 LookupError 表示找不到。"""
    # 1) 直接路径
    p = Path(os.path.expanduser(os.path.expandvars(selector)))
    if p.suffix == ".jsonl" and p.exists():
        return _codex_session_from(p)

    sessions = list_codex_sessions()   # 已按 mtime 降序,带 meta

    # 2) latest [:目录/关键字]
    if selector == "latest" or selector.startswith("latest:"):
        kw = selector.split(":", 1)[1] if ":" in selector else ""
        if kw:
            sub = list_codex_sessions(kw)
        else:
            sub = sessions
        if not sub:
            raise LookupError(f"codex latest: 无匹配 (kw={kw!r})")
        return sub[0]

    # 3) 工作目录精确匹配
    if re.search(r"[\\/:]", selector):
        hits = list_codex_sessions(selector)
        if not hits:
            raise LookupError(f"无 codex 会话 cwd 匹配 {selector!r}")
        return hits[0]

    # 4) session-id / 文件名前缀
    matches = [s for s in sessions
               if s.session_id.startswith(selector) or s.main.stem.startswith(selector)]
    if not matches:
        matches = [s for s in sessions if selector in s.session_id or selector in s.main.stem]
    if not matches:
        raise LookupError(f"无 codex session 匹配 {selector!r}")
    return matches[0]   # 已按 mtime 降序,取最近


def _codex_is_error(out_txt: str, payload: dict) -> bool:
    """Codex 工具结果是否失败。无显式 is_error 字段,按惯例启发式判断:
    custom 工具看 status;shell 结果看 'Exit code: <非0>'。"""
    if payload.get("status") == "failed":
        return True
    m = re.search(r"Exit code:\s*(-?\d+)", out_txt or "")
    return bool(m) and m.group(1) != "0"


def parse_codex_events(path: str | Path) -> list[Event]:
    """把 Codex rollout-*.jsonl 解析成与 parse_events 同构的 Event 列表,
    使 overview / timeline / tools / errors / 查询 DSL 等动词可直接复用。"""
    path = Path(path)
    events: list[Event] = []
    seq = 0
    prev_in = prev_out = 0   # token 累计基线(total_token_usage 是累计值,取增量)
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            typ = o.get("type")
            common = dict(ts=o.get("timestamp"), sidechain=False,
                          agent_type=None, agent_id=None, raw=o)
            p = o.get("payload", {}) or {}

            if typ == "event_msg":
                et = p.get("type")
                if et == "user_message":
                    txt = p.get("message", "") or ""
                    if txt.strip():
                        for name in _codex_skill_names(txt):
                            seq += 1
                            events.append(Event(seq, Kind.SKILL, text=name, **common))
                        seq += 1
                        events.append(Event(seq, Kind.HUMAN, text=txt, **common))
                elif et == "agent_message":
                    txt = p.get("message", "") or ""
                    if txt.strip():
                        seq += 1
                        events.append(Event(seq, Kind.AGENT, text=txt, **common))
                elif et == "agent_reasoning":
                    txt = p.get("text", "") or ""
                    if txt.strip():
                        seq += 1
                        events.append(Event(seq, Kind.THINKING, text=txt, **common))
                elif et == "token_count":
                    tt = (p.get("info") or {}).get("total_token_usage") or {}
                    # 非缓存新增 input(对齐 Claude 侧不累加 cache_read 的口径)+ output
                    cur_in = (tt.get("input_tokens", 0) or 0) - (tt.get("cached_input_tokens", 0) or 0)
                    cur_out = tt.get("output_tokens", 0) or 0
                    d_in, d_out = max(0, cur_in - prev_in), max(0, cur_out - prev_out)
                    prev_in, prev_out = max(prev_in, cur_in), max(prev_out, cur_out)
                    if d_in or d_out:
                        seq += 1
                        events.append(Event(seq, Kind.META, text="token_count",
                                            tokens_in=d_in, tokens_out=d_out, **common))
                elif et == "context_compacted":
                    seq += 1
                    events.append(Event(seq, Kind.SUMMARY, text="context_compacted", **common))
                elif et in ("task_started", "task_complete", "turn_aborted",
                            "item_completed", "error", "stream_error"):
                    seq += 1
                    events.append(Event(seq, Kind.META, text=et, **common))
                # 其余 event_msg(*_delta 流增量等)忽略

            elif typ == "response_item":
                pt = p.get("type")
                if pt == "function_call":
                    args = p.get("arguments")
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except Exception:
                            pass
                    seq += 1
                    events.append(Event(seq, Kind.TOOL, text=p.get("name", ""),
                                        tool_name=p.get("name"), tool_input=args,
                                        tool_use_id=p.get("call_id"), **common))
                elif pt == "custom_tool_call":
                    seq += 1
                    events.append(Event(seq, Kind.TOOL, text=p.get("name", ""),
                                        tool_name=p.get("name"), tool_input=p.get("input"),
                                        tool_use_id=p.get("call_id"), **common))
                elif pt == "web_search_call":
                    seq += 1
                    events.append(Event(seq, Kind.TOOL, text="web_search",
                                        tool_name="web_search", tool_input=p.get("action"),
                                        tool_use_id=p.get("call_id"), **common))
                elif pt in ("function_call_output", "custom_tool_call_output"):
                    out = p.get("output")
                    out_txt = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)
                    seq += 1
                    events.append(Event(seq, Kind.RESULT, text=(out_txt or "")[:4000],
                                        tool_use_id=p.get("call_id"),
                                        is_error=_codex_is_error(out_txt, p), **common))
                # message / reasoning 跳过(event_msg 已覆盖)

            elif typ == "compacted":
                seq += 1
                events.append(Event(seq, Kind.SUMMARY,
                                    text=str(p.get("message") or p)[:2000], **common))
            # session_meta / turn_context 不产事件
    return events
