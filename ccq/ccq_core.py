"""ccq_core —— Claude Code session 日志解析底座 (Layer 0 + locate).

把 ~/.claude/projects 下的 .jsonl 会话日志解析成归一化 Event 流,
并提供 session 定位(主文件 + 子 agent 文件)。

可被其它 ccq 模块 import,也可作为 agent 直接写脚本的逃生口:
    from ccq.ccq_core import load_session, Event, locate
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
class Token:
    """token 消耗的五元建模。各字段互斥,相加即该事件的真实计费口径。
    - input:       新增 prompt token(不含缓存命中)
    - output:      模型输出 token(不含单列的 thinking)
    - cache_read:  命中缓存的 token(每轮重复上下文,按约 0.1x 计费)
    - cache_write: 新建缓存的 token
    - thinking:    推理/思考 token。Claude 不单独上报(已并入 output),恒 0;
                   Codex 取 reasoning_output_tokens。
    """
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    thinking: int = 0

    _FIELDS = ("input", "output", "cache_read", "cache_write", "thinking")

    def __add__(self, other: "Token") -> "Token":
        return Token(*(getattr(self, f) + getattr(other, f) for f in Token._FIELDS))

    def __radd__(self, other):
        # 支持 sum([...], Token()) 与 sum([...]) 两种写法
        return self if other == 0 else self.__add__(other)

    @property
    def total(self) -> int:
        return sum(getattr(self, f) for f in Token._FIELDS)

    def as_dict(self) -> dict:
        """非 0 字段的字典(为 0 则不含)。机器可读输出用。"""
        return {f: v for f in Token._FIELDS if (v := getattr(self, f))}

    def fmt(self) -> str:
        """五字段展示,为 0 的字段省略;全 0 时显示 '0'。"""
        shown = [f"{f}={v:,}" for f in Token._FIELDS if (v := getattr(self, f))]
        return " ".join(shown) if shown else "0"


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
    tokens: Token = field(default_factory=Token)
    sidechain: bool = False
    agent_type: Optional[str] = None  # 该事件所属子 agent 的类型(主 agent 为 None)
    agent_id: Optional[str] = None    # 该事件所属子 agent 的唯一标识(= spawn 的 toolUseId)
    uuid: Optional[str] = None
    parent_uuid: Optional[str] = None
    raw: dict = field(default_factory=dict, repr=False)

    # 向后兼容:旧代码/测试用的两个标量口径。
    # in = 新增 input + 新建缓存;out = 输出 + 思考。刻意不含 cache_read(每轮重复,累加即天文数字)。
    @property
    def tokens_in(self) -> int:
        return self.tokens.input + self.tokens.cache_write

    @property
    def tokens_out(self) -> int:
        return self.tokens.output + self.tokens.thinking

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
                # 五元建模:input/output/缓存读写分开留存。thinking Claude 不单独上报
                # (已并入 output_tokens),恒 0。整条消息的 usage 只挂在首个子事件上,
                # 避免同一回合的多个 block 重复计费。
                tok = Token(
                    input=usage.get("input_tokens", 0) or 0,
                    output=usage.get("output_tokens", 0) or 0,
                    cache_read=usage.get("cache_read_input_tokens", 0) or 0,
                    cache_write=usage.get("cache_creation_input_tokens", 0) or 0,
                )
                first = True
                for b in (msg.get("content") or []):
                    if not isinstance(b, dict):
                        continue
                    bt = b.get("type")
                    if bt == "text":
                        seq += 1
                        events.append(Event(seq, Kind.AGENT, text=b.get("text", ""),
                                            tokens=tok if first else Token(), **common))
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
                                            tokens=tok if first else Token(), **common))
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


def list_sessions(selector: str = "", codex: bool = False,
                  opencode: bool = False) -> list[SessionFiles]:
    """列出匹配项目目录下的全部主会话(各自带子 agent 列表)。
    只读目录与 meta(不解析事件),对大量会话也轻量。按 mtime 降序。
    codex=True 时改读 ~/.codex 下的 Codex 会话(见 list_codex_sessions)。
    opencode=True 时改读 opencode SQLite 库(见 list_opencode_sessions)。"""
    if opencode:
        return list_opencode_sessions(selector)
    if codex:
        return list_codex_sessions(selector)
    out: list[SessionFiles] = []
    for proj in iter_projects(selector):
        for jf in proj.glob("*.jsonl"):
            out.append(_session_from_main(jf))
    out.sort(key=lambda s: s.mtime, reverse=True)
    return out


def locate(selector: str, codex: bool = False,
           opencode: bool = False) -> SessionFiles:
    """把 selector 解析为一个 session。支持:
    - 直接文件路径(.jsonl)
    - 'latest' / 'latest:<proj关键字>'
    - partial session-id 前缀(如 'd74d')
    抛 LookupError 表示找不到或歧义。
    codex=True 时改走 Codex 定位(见 locate_codex)。
    opencode=True 时改走 opencode 定位(见 locate_opencode)。
    """
    if opencode:
        return locate_opencode(selector)
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
                 codex: bool = False, opencode: bool = False
                 ) -> tuple[SessionFiles, list[Event]]:
    """定位并加载一个 session 的全部事件(主 + 可选子 agent)。
    子 agent 事件接在主事件之后,seq 在各自文件内独立,但加全局偏移避免冲突。
    codex=True 时改读 Codex 会话:子 agent 是同档独立 rollout,经 spawn_agent
    链接重建调用树,子事件标 agent_type/agent_id(=spawn call_id)/sidechain。
    opencode=True 时改读 opencode SQLite 库:子 agent 是独立 session 行
    (parent_id 指向父),经父会话 task 工具的 state.metadata.sessionId 反查 callID,
    子事件标 agent_type/agent_id(=task callID)/sidechain。"""
    if opencode:
        sf = locate_opencode(selector)
        events = parse_opencode_events(sf.main)
        if include_subagents:
            offset = events[-1].seq if events else 0
            for sub in sf.subagents:
                sub_ev = parse_opencode_events(sub.path,
                                               agent_type=sub.agent_type,
                                               agent_id=sub.tool_use_id)
                for e in sub_ev:
                    e.seq += offset
                    e.sidechain = True
                events.extend(sub_ev)
                offset += (sub_ev[-1].seq - offset) if sub_ev else 0
        return sf, events
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
    prev = Token()           # token 累计基线(total_token_usage 是累计值,逐字段取增量)
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
                    # 五元建模,各字段互斥:input 扣掉缓存命中、output 扣掉推理,
                    # cache_read=cached_input、thinking=reasoning_output;Codex 无 cache_write。
                    reason = tt.get("reasoning_output_tokens", 0) or 0
                    cached = tt.get("cached_input_tokens", 0) or 0
                    cur = Token(
                        input=max(0, (tt.get("input_tokens", 0) or 0) - cached),
                        output=max(0, (tt.get("output_tokens", 0) or 0) - reason),
                        cache_read=cached,
                        thinking=reason,
                    )
                    # total_token_usage 是累计值 → 逐字段取增量,基线取历史最大。
                    delta = Token(*(max(0, getattr(cur, f) - getattr(prev, f))
                                    for f in Token._FIELDS))
                    prev = Token(*(max(getattr(prev, f), getattr(cur, f))
                                   for f in Token._FIELDS))
                    if delta.total:
                        seq += 1
                        events.append(Event(seq, Kind.META, text="token_count",
                                            tokens=delta, **common))
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


# --------------------------------------------------------------------------- #
# opencode (sst/opencode CLI) 日志适配 — SQLite 后端
# --------------------------------------------------------------------------- #
# opencode 与 Claude Code / Codex 的日志差异:
#   - 存储:单一 SQLite 库 ~/.local/share/opencode/opencode.db(非 JSONL)。
#     所有会话、子 agent、消息、消息分片(part)共库;按 session.id 区分。
#   - session 行:已聚合 tokens(input/output/reasoning/cache_read/cache_write)、
#     cost、model、directory(cwd)、title、parent_id(子 agent 指向父 session)、
#     agent(opencode 的 mode:build/plan/general;子 agent 取 subagent_type)。
#   - message 行:role ∈ user/assistant,带 tokens/cost/time;data JSON。
#   - part 行:消息分片,type ∈ text/tool/reasoning/step-start/step-finish/
#     patch/compaction/file。tool 分片把「调用 + 结果」合在同一 part:
#     state.{status,input,output,error,metadata};status ∈ completed/error/running。
#   - 子 agent:独立的 session 行(parent_id 指向父)。父会话用 task 工具调用
#     派生,task 分片的 state.metadata.sessionId 即子 session id(实测全命中)。
#   - skill 触发:opencode 按「用户请求匹配 skill 触发描述」自动把整段 SKILL.md
#     注入到用户 text part 开头(非用户手敲 /slash,也非用户粘贴)。注入内容以
#     "Base directory for this skill: <path>\nRelative paths in this skill ...
#     base directory." footer 收尾,其后才是用户真实输入;按此 footer 切分并抽
#     skill 名(路径末段)。另:agent 也可用 tool=skill 分片(input.name)主动加载。
#   - 工具名小写(read/write/edit/bash/task/skill/apply_patch/...),归一到
#     Claude 大写约定(Read/Write/Edit/Bash/Task/Skill)以复用 ccq 的 files/
#     agents/skills 分类逻辑;apply_patch 已是小写,与 Codex 同形,直接复用。
#   - apply_patch 输入字段是 patchText(字符串),归一为 tool_input=字符串本身,
#     使 ccq 的 _parse_apply_patch 走 isinstance(str) 分支(与 Codex 一致)。
#   - 文件路径字段 camelCase(filePath/oldString/newString),归一为 snake_case
#     (file_path/old_string/new_string),使 _file_path_of / _seq_churn 复用。
import sqlite3

_OPENCODE_TOOL_MAP = {
    "read": "Read", "write": "Write", "edit": "Edit",
    "bash": "Bash", "skill": "Skill", "task": "Task",
}
_OPENCODE_FIELD_MAP = {
    "filePath": "file_path", "oldString": "old_string", "newString": "new_string",
}
# opencode 按「用户请求匹配 skill 触发描述」自动把整段 SKILL.md 注入到用户 text part
# 开头(非用户手敲 /slash,也非用户粘贴)。注入内容以两行 footer 收尾:
#   Base directory for this skill: <abs path to skill dir>
#   Relative paths in this skill (e.g., scripts/, references/) are relative to this base directory.
# 其后才是用户真实输入。用此 footer 作主切分标记(抽 skill 名 = 路径末段);
# skill 无此 footer 时退回 /command 行启发。
_OPENCODE_SKILL_FOOTER = re.compile(
    r"Base directory for this skill:\s*([^\n]+?)\s*\n"
    r"Relative paths in this skill.*?base directory\.\s*\n+",
    re.I,
)


def opencode_db_path() -> Path:
    """~/.local/share/opencode/opencode.db 的绝对路径。"""
    return Path(os.path.expanduser("~")) / ".local" / "share" / "opencode" / "opencode.db"


def _opencode_conn(db: Path | None = None):
    """只读连接 opencode 库(WAL 模式下读不阻塞写)。库缺失抛 FileNotFoundError。"""
    p = db or opencode_db_path()
    if not p.exists():
        raise FileNotFoundError(f"opencode 库不存在: {p}")
    # mode=ro 只读;immutable 不可(WAL 活跃时会读到旧快照)。timeout 容让写锁。
    return sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True, timeout=30)


def _opencode_row(conn, session_id: str) -> dict | None:
    """单 session 行(列名→值)。"""
    cur = conn.execute(
        "SELECT id, project_id, parent_id, slug, directory, title, agent, model, "
        "cost, tokens_input, tokens_output, tokens_reasoning, "
        "tokens_cache_read, tokens_cache_write, time_created, time_updated "
        "FROM session WHERE id = ?", (session_id,))
    r = cur.fetchone()
    if not r:
        return None
    cols = [d[0] for d in cur.description]
    return dict(zip(cols, r))


def _opencode_session_files(session_id: str, row: dict | None = None,
                            conn=None) -> SessionFiles:
    """由 session_id 构造 SessionFiles(main=Path(session_id),project=directory)。
    子 agent 经 _opencode_subagents_for 填充。"""
    own_conn = conn is None
    if own_conn:
        conn = _opencode_conn()
    try:
        if row is None:
            row = _opencode_row(conn, session_id) or {}
        mtime = (row.get("time_updated") or row.get("time_created") or 0) / 1000
        sf = SessionFiles(
            session_id=row.get("id") or session_id,
            project=row.get("directory") or "?",
            main=Path(session_id),
            subagents=[],
            mtime=mtime,
        )
        sf.subagents = _opencode_subagents_for(session_id, conn)
        return sf
    finally:
        if own_conn:
            conn.close()


def _opencode_task_calls(conn, session_id: str) -> dict[str, tuple[str, str, str]]:
    """父会话的 task 工具调用 → {child_session_id: (callID, subagent_type, description)}。
    task 分片的 state.metadata.sessionId 即子 session id。"""
    out: dict[str, tuple[str, str, str]] = {}
    cur = conn.execute(
        "SELECT data FROM part WHERE session_id = ? AND "
        "json_extract(data, '$.type') = 'tool' AND "
        "json_extract(data, '$.tool') = 'task'", (session_id,))
    for (d,) in cur.fetchall():
        try:
            o = json.loads(d)
        except Exception:
            continue
        st = (o.get("state") or {})
        inp = st.get("input") or {}
        cid = o.get("callID") or ""
        sid = (st.get("metadata") or {}).get("sessionId") or ""
        if not sid:
            continue
        out[sid] = (cid, inp.get("subagent_type") or "", inp.get("description") or "")
    return out


def _opencode_subagents_for(session_id: str, conn=None) -> list[SubAgent]:
    """该会话派生的全部子 agent(session.parent_id 指向本会话)。
    tool_use_id 取父会话 task 调用的 callID(经 metadata.sessionId 反查);
    agent_type 取 task input.subagent_type,回退到子 session 的 agent 列;
    description 取子 session 的 title。"""
    own_conn = conn is None
    if own_conn:
        conn = _opencode_conn()
    try:
        task_map = _opencode_task_calls(conn, session_id)
        cur = conn.execute(
            "SELECT id, agent, title, time_created FROM session "
            "WHERE parent_id = ? ORDER BY time_created", (session_id,))
        out: list[SubAgent] = []
        for sid, agent, title, _tc in cur.fetchall():
            cid, styp, sdesc = task_map.get(sid, ("", "", ""))
            out.append(SubAgent(
                agent_type=styp or agent or "general",
                description=title or sdesc or "",
                tool_use_id=cid or sid,
                path=Path(sid),
                depth=0,
            ))
        return out
    finally:
        if own_conn:
            conn.close()


def iter_opencode_sessions() -> Iterator[tuple[str, dict]]:
    """全部顶层 opencode 会话(子 agent 不进列表,经父会话 agents 钻取)。
    yield (session_id, row{directory,title,agent,time_created,time_updated})。"""
    conn = _opencode_conn()
    try:
        cur = conn.execute(
            "SELECT id, directory, title, agent, time_created, time_updated "
            "FROM session WHERE parent_id IS NULL OR parent_id = '' "
            "ORDER BY time_updated DESC")
        for sid, directory, title, agent, tc, tu in cur.fetchall():
            yield sid, {"directory": directory, "title": title, "agent": agent,
                        "time_created": tc, "time_updated": tu}
    finally:
        conn.close()


def _opencode_list_top_rows(conn, selector: str) -> list[tuple[str, dict]]:
    """匹配 selector 的顶层会话行(已过滤子 agent)。selector 语义同
    list_opencode_sessions:路径→directory 精确/前缀;关键字→directory/title 子串。"""
    is_path = bool(selector) and bool(re.search(r"[\\/:]", selector))
    nsel = _norm_dir(selector) if is_path else ""
    kw = selector.lower() if selector else ""
    cur = conn.execute(
        "SELECT id, directory, title, agent, time_created, time_updated "
        "FROM session WHERE parent_id IS NULL OR parent_id = '' "
        "ORDER BY time_updated DESC")
    out: list[tuple[str, dict]] = []
    for sid, directory, title, agent, tc, tu in cur.fetchall():
        row = {"directory": directory or "", "title": title or "",
               "agent": agent, "time_created": tc, "time_updated": tu}
        if selector:
            if is_path:
                nd = _norm_dir(row["directory"])
                if not (nd == nsel or nd.startswith(nsel + os.sep)):
                    continue
            elif (kw not in row["directory"].lower()
                  and kw not in row["title"].lower()):
                continue
        out.append((sid, row))
    return out


def list_opencode_sessions(selector: str = "") -> list[SessionFiles]:
    """列出匹配 selector 的顶层 opencode 会话(按 mtime 降序)。
    - selector 含路径分隔符 → 视为工作目录:精确 directory 匹配 + 子目录前缀
      (把目录下子工程的会话一并纳入,与 Claude/Codex 侧行为对齐)。
    - 否则视为关键字,对 directory 或 title 做大小写无关子串匹配。
    - 空 → 全部。"""
    conn = _opencode_conn()
    try:
        out: list[SessionFiles] = []
        for sid, row in _opencode_list_top_rows(conn, selector):
            out.append(_opencode_session_files(sid, row, conn))
        out.sort(key=lambda s: s.mtime, reverse=True)
        return out
    finally:
        conn.close()


def locate_opencode(selector: str) -> SessionFiles:
    """把 selector 解析为一个 opencode 顶层会话(目录有多个时取最近)。支持:
    - 'latest' / 'latest:<目录或关键字>'
    - 工作目录(含路径分隔符)→ directory 精确/前缀匹配
    - session-id 完整或前缀(如 ses_0bb1e5)
    抛 LookupError 表示找不到或歧义。"""
    conn = _opencode_conn()
    try:
        # 1) latest [: 目录/关键字]
        if selector == "latest" or selector.startswith("latest:"):
            kw = selector.split(":", 1)[1] if ":" in selector else ""
            sub = list_opencode_sessions(kw)
            if not sub:
                raise LookupError(f"opencode latest: 无匹配 (kw={kw!r})")
            return sub[0]

        # 2) 工作目录匹配
        if re.search(r"[\\/:]", selector):
            hits = list_opencode_sessions(selector)
            if not hits:
                raise LookupError(f"无 opencode 会话 directory 匹配 {selector!r}")
            return hits[0]

        # 3) session-id 前缀(允许直接定位子 agent 会话——它们也是库里的行,
        #    agents 动词会把子 session id 打出来供钻取。sessions 列表仍只列顶层。)
        cur = conn.execute(
            "SELECT id FROM session WHERE id LIKE ? ORDER BY time_updated DESC",
            (selector + "%",))
        ids = [r[0] for r in cur.fetchall()]
        if not ids:
            cur = conn.execute(
                "SELECT id FROM session WHERE id LIKE ? ORDER BY time_updated DESC",
                ("%" + selector + "%",))
            ids = [r[0] for r in cur.fetchall()]
        if not ids:
            raise LookupError(f"无 opencode session 匹配 {selector!r}")
        if len(ids) > 1:
            raise LookupError(f"歧义:{len(ids)} 个 opencode session 匹配 {selector!r}: "
                              f"{', '.join(ids[:8])}")
        return _opencode_session_files(ids[0], conn=conn)
    finally:
        conn.close()


def _opencode_norm_input(tool: str, inp):
    """opencode camelCase 输入 → Claude snake_case;apply_patch 取 patchText 字符串。
    使 ccq 的 _file_path_of / _seq_churn / _parse_apply_patch 零改动复用。"""
    if tool == "apply_patch":
        if isinstance(inp, dict):
            return inp.get("patchText") or ""
        return inp
    if isinstance(inp, dict):
        return {_OPENCODE_FIELD_MAP.get(k, k): v for k, v in inp.items()}
    return inp


def _opencode_ts(ms) -> Optional[str]:
    """毫秒 epoch → ISO 时间戳(ccq 的 _hhmm / epoch 解析用)。"""
    if not ms:
        return None
    from datetime import datetime
    try:
        return datetime.fromtimestamp(ms / 1000).isoformat()
    except Exception:
        return None


def parse_opencode_events(session_id_or_path: str | Path,
                          agent_type: Optional[str] = None,
                          agent_id: Optional[str] = None) -> list[Event]:
    """把一个 opencode session(库里的 messages + parts)解析成与 parse_events 同构
    的 Event 列表,使 overview / timeline / tools / errors / files / agents /
    skills / 查询 DSL 等动词可直接复用。

    session_id_or_path:session id 字符串,或 Path(取 .name 作 id;子 agent 的
    SubAgent.path 即此形式)。agent_type/agent_id:子 agent 作用域标注(主会话为 None)。

    每个 tool part 拆成 TOOL(调用)+ RESULT(结果)两条事件,以 tool_use_id
    (= callID)关联——与 Claude 的 tool_use/tool_result 双事件口径对齐,使
    errors / name=... is_error=1 / files 分类等逻辑零改动复用。
    token 取 message 级(挂在每条 assistant 消息的首个事件上,与 Claude 一致)。

    用户 /slash 触发:opencode 把 skill 内容注入到用户消息 text part 开头,用户
    的 /command 行附在末尾。抽掉前导注入内容只保留 /command 起的段作为 HUMAN 文本
    (使 sessions 速览 / skill-report 意图干净可读;注入内容仍在 raw 里)。
    """
    sid = (Path(session_id_or_path).name
           if isinstance(session_id_or_path, Path) else session_id_or_path)
    conn = _opencode_conn()
    try:
        # 拉一条消息+分片联合流,按 (消息时间, 分片时间, 分片 id) 全序
        cur = conn.execute(
            "SELECT m.id, m.data, p.id, p.data, p.time_created "
            "FROM message m JOIN part p ON p.message_id = m.id "
            "WHERE m.session_id = ? "
            "ORDER BY m.time_created, p.time_created, p.id", (sid,))
        rows = cur.fetchall()
    finally:
        conn.close()

    events: list[Event] = []
    seq = 0
    prev_msg_id: str | None = None
    msg_tok = Token()
    first_of_msg = True
    for mid, mdata, pid, pdata, ptime in rows:
        try:
            mo = json.loads(mdata) if mdata else {}
        except Exception:
            mo = {}
        try:
            po = json.loads(pdata) if pdata else {}
        except Exception:
            po = {}
        role = mo.get("role")
        ptype = po.get("type")
        ts = _opencode_ts(ptime or (mo.get("time") or {}).get("created"))
        common = dict(ts=ts, sidechain=agent_id is not None,
                      agent_type=agent_type, agent_id=agent_id,
                      uuid=pid, parent_uuid=mid, raw=po)

        # 消息边界:新消息的首个 assistant 事件挂 message 级 token(与 Claude 一致)
        if mid != prev_msg_id:
            t = mo.get("tokens") or {}
            cache = t.get("cache") or {}
            msg_tok = Token(
                input=t.get("input", 0) or 0,
                output=t.get("output", 0) or 0,
                cache_read=cache.get("read", 0) or 0,
                cache_write=cache.get("write", 0) or 0,
                thinking=t.get("reasoning", 0) or 0,
            )
            first_of_msg = True
            prev_msg_id = mid
        tok_here = msg_tok if (first_of_msg and role == "assistant") else Token()

        if ptype == "text":
            txt = (po.get("text") or "").replace("\r\n", "\n").replace("\r", "\n")
            if not txt.strip():
                continue
            if role == "user":
                # opencode 按用户请求匹配 skill 触发描述后,自动把整段 SKILL.md
                # 注入到用户 text part 开头(非用户手敲 /slash,也非用户粘贴)。
                # 注入内容以 "Base directory for this skill: <path>\nRelative
                # paths in this skill ... base directory." footer 收尾,其后才是
                # 用户真实输入。优先按 footer 切分(skill 名取路径末段);footer
                # 之后若以 /command 开头,则用户是显式 /slash 触发(用该名而非
                # 注入 skill 名);skill 无此 footer 时退回 /command 行启发。
                fms = list(_OPENCODE_SKILL_FOOTER.finditer(txt))
                if fms:
                    fm = fms[-1]
                    injected_skill = Path(fm.group(1).strip()).name or ""
                    genuine = txt[fm.end():].strip()
                    # footer 之后若以 /command 开头 → 用户的显式 /slash 触发优先
                    cm = _SLASH_RE.match(genuine) if genuine else None
                    trigger = cm.group(1) if cm else injected_skill
                    if trigger:
                        seq += 1
                        events.append(Event(seq, Kind.SKILL, text=trigger,
                                            tool_use_id=pid, **common))
                    if genuine:
                        seq += 1
                        events.append(Event(seq, Kind.HUMAN, text=genuine,
                                            tokens=Token(), **common))
                else:
                    # 无 footer:退回 /command 行启发(skill 无 footer,或无注入)
                    matches = list(_SLASH_RE.finditer(txt))
                    split_at = matches[-1].start() if matches else -1
                    if split_at > 0:
                        genuine = txt[split_at:].strip()
                        seq += 1
                        events.append(Event(seq, Kind.SKILL, text=matches[-1].group(1),
                                            tool_use_id=pid, **common))
                        seq += 1
                        events.append(Event(seq, Kind.HUMAN, text=genuine,
                                            tokens=Token(), **common))
                    else:
                        # 整段都是真实用户输入;/command 在首行时额外发一条 SKILL
                        if matches:
                            seq += 1
                            events.append(Event(seq, Kind.SKILL, text=matches[0].group(1),
                                                tool_use_id=pid, **common))
                        seq += 1
                        events.append(Event(seq, Kind.HUMAN, text=txt,
                                            tokens=Token(), **common))
            else:  # assistant
                seq += 1
                events.append(Event(seq, Kind.AGENT, text=txt,
                                    tokens=tok_here, **common))
                first_of_msg = False
        elif ptype == "reasoning":
            txt = po.get("text") or ""
            if txt.strip():
                seq += 1
                events.append(Event(seq, Kind.THINKING, text=txt, **common))
        elif ptype == "tool":
            tool = po.get("tool") or "unknown"
            name = _OPENCODE_TOOL_MAP.get(tool, tool)
            st = po.get("state") or {}
            inp = _opencode_norm_input(tool, st.get("input"))
            cid = po.get("callID") or pid
            status = st.get("status") or ""
            is_err = status == "error"
            # Skill 工具:额外发一条 SKILL 事件(与 Claude 侧 Skill 工具一致)
            if name == "Skill":
                sname = (st.get("input") or {}).get("name") or ""
                if sname:
                    seq += 1
                    events.append(Event(seq, Kind.SKILL, text=sname,
                                        tool_use_id=cid, **common))
            # TOOL 事件(调用)
            seq += 1
            events.append(Event(seq, Kind.TOOL, text=name,
                                tool_name=name, tool_input=inp,
                                tool_use_id=cid, tokens=tok_here, **common))
            first_of_msg = False
            # RESULT 事件(结果;同 part 拆出,tool_use_id 关联)
            out_txt = st.get("error") if is_err else st.get("output")
            if out_txt is None:
                out_txt = ""
            if not isinstance(out_txt, str):
                out_txt = json.dumps(out_txt, ensure_ascii=False)
            seq += 1
            events.append(Event(seq, Kind.RESULT, text=(out_txt or "")[:4000],
                                tool_use_id=cid, is_error=is_err, **common))
        elif ptype == "compaction":
            seq += 1
            events.append(Event(seq, Kind.SUMMARY, text="compaction", **common))
        # step-start / step-finish / patch / file:不产事件(token 已在 message 级)
    return events
