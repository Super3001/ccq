"""ccq —— Claude Code session 日志查询工具 (CLI 前端).

读另一个 Claude Code session 的日志,诊断其运行情况:人类说了什么、
agent 做了什么、遇到什么错误,并评估「agent 使用某 skill 的效果」。

分层用法(zoom out → zoom in):
    ccq locate <selector>            # 定位分支:返回该 session 的全部 log 路径
    ccq sessions <项目目录|关键字>   # 枚举/计数某项目下全部主会话与子 agent
    ccq <selector|path> overview     # 一屏看懂
    ccq <selector|path> timeline     # 紧凑事件流
    ccq <selector|path> human|tools|errors|agents|skills
    ccq <selector|path> files        # 读写文件统计:R只读/A创建/M修改,子agent分列
                                     #   加 -L 统计变更行数(+增/−删,较耗时)
    ccq <selector|path> show <seq>   # 钻取单事件完整内容
    ccq <selector|path> grep <regex> [in|io]  # 正则统计:出现次数+命中事件+动作分布
                                              # scope 缺省 io(人机所写+环境返回),in 只搜人机所写
    ccq <selector|path> skill-report [<skill>]   # skill 效果评估报告
    ccq <selector|path> "<query DSL>"            # 查询语言

查询 DSL:  "<谓词...> | <动词>"   谓词空格分隔 AND。
  谓词: kind= name= is_error= sidechain= agent= after= before= skill= text~<re>
  动词: count timeline text show tools json   (缺省 timeline)

--codex: 读 Codex(~/.codex)会话而非 Claude Code 会话。Codex 按日期归档、
  不按目录,故 selector 主用「工作目录」(按各会话 cwd 反查),也支持 rollout
  uuid 前缀 / latest。所有动词通用。
    ccq --codex sessions C:/w/HomeTrans-CJ   # 该目录下全部 Codex 会话与计数
    ccq --codex C:/w/HomeTrans-CJ overview    # 该目录最近一个 Codex 会话概览
    ccq --codex <uuid前缀> errors

--opencode: 读 opencode(~/.local/share/opencode/opencode.db)SQLite 库而非
  Claude Code 会话。opencode 把全部会话/子agent/消息/分片存于单一 SQLite 库,
  按 session.id 区分;selector 用「工作目录」(按 session.directory 反查)、
  session-id 前缀(如 ses_0bb1e5)、或 latest。所有动词通用。
    ccq --opencode --check                      # 库与顶层会话计数
    ccq --opencode sessions C:/w/HomeTrans-CJ   # 该目录下全部 opencode 会话
    ccq --opencode ses_0bb1e5 overview          # 单会话概览
    ccq --opencode latest errors                # 最近一个会话的错误
"""
from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from collections import Counter, defaultdict
from enum import IntEnum
from pathlib import Path

# 模块级 verbosity:0=quiet,1=normal(默认),2=verbose
_QUIET = False
_VERBOSE = False
# files 动词:是否统计变更行数(+增/−删)。较耗时(每次 Edit 一趟 difflib),默认关。
_COUNT_LINES = False

from ccq.ccq_core import (
    Event, Kind, SessionFiles, Token, list_sessions, load_session, locate,
    parse_events, parse_codex_events, parse_opencode_events, projects_root,
    opencode_db_path, iter_opencode_sessions, _codex_skill_names, _SLASH_RE,
)


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def _hhmm(ts: str | None) -> str:
    if not ts:
        return "--:--"
    m = re.search(r"T(\d\d:\d\d:\d\d)", ts)
    return m.group(1)[:5] if m else "--:--"


def _rel(ev: list[Event], e: Event) -> str:
    """相对会话开头的 +mm:ss。"""
    base = next((x.epoch for x in ev if x.epoch), 0)
    d = int(e.epoch - base) if e.epoch and base else 0
    return f"+{d // 60:02d}:{d % 60:02d}"


# 由 harness 注入、非真实人类提问的消息标记
_META_TAGS = ("<command-name>", "<command-message>", "<command-args>",
              "<local-command-stdout>", "<local-command-caveat>")

# grep 的搜索范围:input=「人/机所写」,output=「环境返回」
_INPUT_KINDS = {Kind.HUMAN, Kind.AGENT, Kind.THINKING, Kind.TOOL, Kind.SKILL}
_OUTPUT_KINDS = {Kind.RESULT}


def _haystack(e: Event) -> str:
    """grep/统计取材:事件文本 + TOOL 的 tool_input json(与 _match 口径一致)。"""
    hay = e.text or ""
    if e.kind == Kind.TOOL and e.tool_input:
        hay += " " + json.dumps(e.tool_input, ensure_ascii=False)
    return hay


def _action_label(e: Event) -> str:
    """该事件是什么动作——供 grep 分布统计。TOOL 按工具名细分。"""
    return {
        Kind.HUMAN: "用户输入", Kind.AGENT: "agent回复", Kind.THINKING: "思考",
        Kind.SKILL: "skill触发", Kind.RESULT: "工具结果",
    }.get(e.kind, f"工具:{e.tool_name}" if e.kind == Kind.TOOL else e.kind.value)


def _is_cmd(e: Event) -> bool:
    """该人类消息是否为命令/skill 触发(非用于界定区间的「真实提问」)。
    Claude 侧:harness 注入的 <command-*> 标签;Codex 侧:$<name> 前缀触发;
    opencode 侧:用户消息末尾的 /slash 独立行触发。这类消息不应成为它自己
    skill 段的右边界(否则段会被立刻截断成空)。"""
    t = e.text or ""
    if any(tag in t for tag in _META_TAGS):
        return True
    if _codex_skill_names(t):
        return True
    # /slash 独立行:覆盖 opencode 的末尾 /command 触发,也兜住 Claude 侧用户
    # 手敲(非 harness 注入)的 /slash——后者原先漏网,会使 skill 段被自身截断。
    if _SLASH_RE.search(t):
        return True
    return False


def _is_interrupt(e: Event) -> bool:
    return (e.text or "").startswith("[Request interrupted")


def _real_humans(ev: list[Event]) -> list[Event]:
    """人类侧消息:排除子 agent 的任务 prompt(sidechain)。含命令调用。"""
    return [e for e in ev if e.kind == Kind.HUMAN and not e.sidechain]


def _genuine_humans(ev: list[Event]) -> list[Event]:
    """真实的人类提问:排除 sidechain、命令展开、中断提示。
    用于统计轮次、推断意图、界定 skill 区间。"""
    return [e for e in ev if e.kind == Kind.HUMAN and not e.sidechain
            and not _is_cmd(e) and not _is_interrupt(e)]


def _seg_end(ev: list[Event], start: int, trigs: list[Event],
             genuine: list[Event]) -> int:
    """skill 段终点:start 之后最近的「下一个 skill 触发」或「下一个真实提问」。
    排除与触发同处一个 part/message 的 HUMAN(同 uuid)——它是触发的意图本身
    (opencode 按 footer 切分后,genuine prompt 与 SKILL 同 uuid),不是新指令。"""
    trig = next((t for t in trigs if t.seq == start), None)
    trig_uid = trig.uuid if trig else None
    cands = ([t.seq for t in trigs if t.seq > start]
             + [h.seq for h in genuine if h.seq > start and h.uuid != trig_uid])
    return min(cands) if cands else ev[-1].seq + 1


def _oneline(s: str, n: int = 100) -> str:
    s = " ".join((s or "").split())
    if not s:
        return ""
    return s if len(s) <= n or _VERBOSE else s[: n - 1] + "…"


def _span(ev: list[Event]) -> tuple[str, str, int]:
    ts = [e.epoch for e in ev if e.epoch]
    if not ts:
        return "--:--", "--:--", 0
    first = min(ev, key=lambda e: e.epoch if e.epoch else 9e18)
    last = max(ev, key=lambda e: e.epoch)
    return _hhmm(first.ts), _hhmm(last.ts), int(max(ts) - min(ts))


def _branch(ev: list[Event]) -> str:
    for e in ev:
        b = e.raw.get("gitBranch")
        if b:
            return b
    return "?"


# --------------------------------------------------------------------------- #
# Layer L: locate
# --------------------------------------------------------------------------- #
def cmd_locate(selector: str, as_json: bool, codex: bool = False,
               opencode: bool = False) -> int:
    try:
        sf = locate(selector, codex=codex, opencode=opencode)
    except LookupError as e:
        print(f"ccq locate: {e}", file=sys.stderr)
        return 2
    if as_json:
        print(json.dumps({
            "session_id": sf.session_id,
            "project": sf.project,
            "main": str(sf.main),
            "subagents": [
                {"agent_type": s.agent_type, "description": s.description,
                 "tool_use_id": s.tool_use_id, "path": str(s.path)}
                for s in sf.subagents
            ],
        }, ensure_ascii=False, indent=2))
        return 0
    print(f"session {sf.session_id}   project {sf.project}")
    print(f"main   {sf.main}")
    for s in sf.subagents:
        print(f"  └─ {s.agent_type:<10} {s.path}")
        if s.description:
            print(f"        {_oneline(s.description, 80)}")
    print(f"\n# {len(sf.subagents)} 个子 agent。后续查询用上面的路径,或直接 ccq {sf.session_id[:8]} <verb>")
    return 0


def _mtime_str(epoch: float) -> str:
    from datetime import datetime
    if not epoch:
        return "----------------"
    return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M")


def _session_digest(sf: SessionFiles, codex: bool = False,
                    opencode: bool = False) -> dict[str, str]:
    """单个会话的速览摘要:首个真实人类输入 + 末个真实人类输入 + 末个 agent 回复。
    只解析主会话文件(不含子 agent),避免 sessions 列表逐条全量加载。
    人类侧沿用 overview 口径(_genuine_humans),排除命令展开/中断等 harness 噪声,
    这样摘要展示的是真实提问而非 <command-*> 或 $skill 触发文本。
    末个人类仅在与首个不同(轮次>1)时给出,与 overview 保持一致。"""
    try:
        if opencode:
            ev = parse_opencode_events(sf.main)
        elif codex:
            ev = parse_codex_events(sf.main)
        else:
            ev = parse_events(sf.main)
    except (OSError, ValueError):
        return {}
    humans = _genuine_humans(ev)
    agents_txt = [e for e in ev if e.kind == Kind.AGENT and not e.sidechain]
    d: dict[str, str] = {}
    if humans:
        d["first_human"] = humans[0].text or ""
        if len(humans) > 1:
            d["last_human"] = humans[-1].text or ""
    if agents_txt:
        d["last_agent"] = agents_txt[-1].text or ""
    return d


def cmd_sessions(selector: str, as_json: bool, codex: bool = False,
                 opencode: bool = False, page: int = 1, limit: int = 20) -> int:
    """枚举某项目目录(或关键字)下的全部主会话与子 agent,并给出计数。
    selector 含路径分隔符时按工作目录精确匹配(含 .claude 等子目录会话)。
    codex=True 时枚举 Codex 会话(按会话 cwd 匹配该目录)。
    opencode=True 时枚举 opencode 会话(按 session.directory 匹配)。
    默认分页:按 mtime 全局降序只展示最近 limit(=20)条,且只对展示的会话
    解析速览摘要——摘要要读整份日志,不分页会让大项目每次都全量解析而变慢。
    page 翻到更早的一页;limit<=0 关闭分页,展示全部。"""
    sessions = list_sessions(selector, codex=codex, opencode=opencode)
    if not sessions:
        print(f"ccq sessions: 无匹配项目 {selector!r}", file=sys.stderr)
        return 2

    # 按项目目录分组(保持目录名排序,组内按 mtime 降序)
    groups: dict[str, list[SessionFiles]] = {}
    for s in sessions:
        groups.setdefault(s.project, []).append(s)
    for g in groups.values():
        g.sort(key=lambda s: s.mtime, reverse=True)

    total_main = len(sessions)
    total_sub = sum(len(s.subagents) for s in sessions)

    # 分页:跨项目按 mtime 全局降序取窗口(默认最近一页),摘要只算窗口内会话
    page = max(1, page)
    paged = limit > 0
    flat = sorted(sessions, key=lambda s: s.mtime, reverse=True)
    start = (page - 1) * limit if paged else 0
    window = flat[start:start + limit] if paged else flat
    win_ids = {id(s) for s in window}
    lo, hi = (start + 1, start + len(window)) if window else (0, 0)

    if as_json:
        proj_objs = []
        for proj, g in groups.items():
            win = [s for s in g if id(s) in win_ids]
            if not win:
                continue
            proj_objs.append({
                "project": proj,
                "main_sessions": len(g),         # 该项目匹配到的全部主会话数
                "subagents": sum(len(s.subagents) for s in g),
                "shown": len(win),               # 本页在该项目下展示的条数
                "sessions": [
                    {"session_id": s.session_id,
                     "mtime": _mtime_str(s.mtime),
                     "subagents": len(s.subagents),
                     "main": str(s.main),
                      "summary": _session_digest(s, codex, opencode)}
                    for s in win
                ],
            })
        print(json.dumps({
            "selector": selector,
            "pagination": {"page": page, "limit": limit if paged else 0,
                           "shown": len(window), "start": lo, "end": hi,
                           "total_main_sessions": total_main},
            "projects": proj_objs,
            "total_projects": len(groups),
            "total_main_sessions": total_main,
            "total_subagents": total_sub,
        }, ensure_ascii=False, indent=2))
        return 0

    if not window:
        print(f"# 该页无记录:共 {total_main} 条主会话,每页 {limit},"
              f"最大页 {max(1, -(-total_main // limit)) if paged else 1}")
        return 0

    for proj, g in groups.items():
        win = [s for s in g if id(s) in win_ids]
        if not win:
            continue
        sub_n = sum(len(s.subagents) for s in g)
        print(f"project {proj}   ({len(g)} 主会话, {sub_n} 子agent)")
        for s in win:
            mark = f"subs {len(s.subagents)}" if s.subagents else "      "
            print(f"  {s.session_id[:8]}  {_mtime_str(s.mtime)}   {mark}")
            dg = _session_digest(s, codex, opencode)
            if dg.get("first_human"):
                print(f"      首 human: {_oneline(dg['first_human'], 100)}")
            if dg.get("last_human"):
                print(f"      末 human: {_oneline(dg['last_human'], 100)}")
            if dg.get("last_agent"):
                print(f"      末 agent: {_oneline(dg['last_agent'], 100)}")
        print()

    tail = ""
    if paged and total_main > limit:
        more = (f";--page {page + 1} 看更早" if hi < total_main else ";已到最早")
        tail = f"  (第 {lo}-{hi}/{total_main} 条,最新在前{more},--limit 0 看全部)"
    print(f"# 合计: {len(groups)} 个项目目录, {total_main} 主会话, {total_sub} 子agent{tail}")
    return 0


# --------------------------------------------------------------------------- #
# Layer 1: overview / timeline
# --------------------------------------------------------------------------- #
def cmd_overview(sf: SessionFiles, ev: list[Event]) -> int:
    first, last, dur = _span(ev)
    humans = _genuine_humans(ev)
    agents_txt = [e for e in ev if e.kind == Kind.AGENT and not e.sidechain]
    tools = Counter(e.tool_name for e in ev if e.kind == Kind.TOOL)
    errs = [e for e in ev if e.kind == Kind.RESULT and e.is_error]
    skills = [e for e in ev if e.kind == Kind.SKILL]
    tok = sum((e.tokens for e in ev), Token())
    sub_kinds = Counter(s.agent_type for s in sf.subagents)
    # 人类中途插话:首条之后、会话结束前的人类消息数(纠偏信号)
    interjections = max(0, len(humans) - 1)

    print(f"session {sf.session_id}")
    print(f"project {sf.project}   branch {_branch(ev)}")
    print(f"span    {first} → {last}  ({dur // 60}m{dur % 60:02d}s)")
    print(f"turns   {len(humans)} human / {len(agents_txt)} agent   "
          f"events {len(ev)}   interject {interjections}")
    th = "  ".join(f"{n}×{c}" for n, c in tools.most_common(8))
    print(f"tools   {sum(tools.values())} calls: {th}")
    print(f"errors  {len(errs)}")
    if sub_kinds:
        sk = "  ".join(f"{k}×{c}" for k, c in sub_kinds.items())
        print(f"agents  {len(sf.subagents)} sub: {sk}")
    if skills:
        print(f"skills  {', '.join(dict.fromkeys(s.text for s in skills))}")
    print(f"tokens  {tok.fmt()}")
    if humans:
        print(f"\nfirst human: {_oneline(humans[0].text, 110)}")
        if len(humans) > 1:
            print(f"last  human: {_oneline(humans[-1].text, 110)}")
    if agents_txt:
        print(f"last  agent: {_oneline(agents_txt[-1].text, 110)}")
    if errs:
        print("\ntop errors:")
        for e in errs[:3]:
            print(f"  ✗ {_oneline(e.text, 100)}")
    return 0


def _line_for(ev: list[Event], e: Event) -> str:
    # quiet 模式下只为关键种类产出单行摘要
    if _QUIET and e.kind not in (Kind.TOOL, Kind.RESULT, Kind.SKILL, Kind.HUMAN):
        return ""
    tag = {
        Kind.HUMAN: "HUMAN", Kind.AGENT: "agent", Kind.THINKING: "think",
        Kind.TOOL: "TOOL ", Kind.RESULT: "  ->", Kind.SKILL: "SKILL",
        Kind.SYSTEM: "sys  ", Kind.SUMMARY: "summ ", Kind.META: "meta ",
    }.get(e.kind, "?")
    pfx = f"[{_rel(ev, e)}] {e.seq:>4} "
    a = f"({e.agent_type})" if e.agent_type else ""
    if e.kind == Kind.TOOL:
        inp_n = 80 if not _VERBOSE else 9999
        inp = _oneline(json.dumps(e.tool_input, ensure_ascii=False) if e.tool_input else "", inp_n)
        return f"{pfx}{tag}{a} {e.tool_name}: {inp}"
    if e.kind == Kind.RESULT:
        mark = "✗ERROR " if e.is_error else ""
        txt_n = 80 if not _VERBOSE else 9999
        return f"{pfx}{tag}{a} {mark}{_oneline(e.text, txt_n)}"
    txt_n = 100 if not _VERBOSE else 9999
    return f"{pfx}{tag}{a} {_oneline(e.text, txt_n)}"


def cmd_timeline(ev: list[Event], kinds: set[Kind] | None = None) -> int:
    show = {Kind.HUMAN, Kind.AGENT, Kind.TOOL, Kind.RESULT, Kind.SKILL} if kinds is None else kinds
    for e in ev:
        if e.kind in show:
            print(_line_for(ev, e))
    return 0


# --------------------------------------------------------------------------- #
# Layer 2: human / tools / errors / agents / skills
# --------------------------------------------------------------------------- #
def cmd_human(ev: list[Event]) -> int:
    for e in _real_humans(ev):
        if _is_cmd(e):
            print(f"[{_rel(ev, e)}] #{e.seq}  (命令调用)\n{'-'*60}")
        else:
            print(f"[{_rel(ev, e)}] #{e.seq}\n{e.text.strip()}\n{'-'*60}")
    return 0


def cmd_tools(ev: list[Event], name: str | None) -> int:
    for e in ev:
        if e.kind == Kind.TOOL and (name is None or e.tool_name == name):
            print(_line_for(ev, e))
    return 0


def cmd_errors(ev: list[Event]) -> int:
    by_id = {e.tool_use_id: e for e in ev if e.kind == Kind.TOOL}
    n = 0
    for e in ev:
        if e.kind == Kind.RESULT and e.is_error:
            n += 1
            cause = by_id.get(e.tool_use_id)
            print(f"[{_rel(ev, e)}] #{e.seq}  ✗ERROR")
            if cause:
                inp = _oneline(json.dumps(cause.tool_input, ensure_ascii=False), 100)
                print(f"  cause: {cause.tool_name}({inp})")
            print(f"  {_oneline(e.text, 200)}\n{'-'*60}")
    print(f"# {n} 个错误")
    return 0


def _spawn_role(sp: Event, sub_by_id: dict) -> tuple[str, str]:
    """一个 spawn 事件 → (role, 描述)。优先用 sf.subagents(Codex 取 agents/<role>.md),
    回退到工具 input(Claude 侧 subagent_type/description)。"""
    s = sub_by_id.get(sp.tool_use_id)
    if s is not None:
        return s.agent_type, s.description
    inp = sp.tool_input if isinstance(sp.tool_input, dict) else {}
    return inp.get("subagent_type", "?"), inp.get("description", "")


def _subagent_digest(ev: list[Event], key: str) -> dict:
    sub = [e for e in ev if e.agent_id == key]
    tools = Counter(e.tool_name for e in sub if e.kind == Kind.TOOL)
    errs = [e for e in sub if e.kind == Kind.RESULT and e.is_error]
    _, _, dur = _span(sub)
    last = next((e for e in reversed(sub) if e.kind == Kind.AGENT), None)
    return {"n": len(sub), "tools": tools, "errors": errs, "dur": dur,
            "final": last.text if last else "", "tok": sum((e.tokens for e in sub), Token())}


# spawn 工具:Claude 侧 Agent/Task,Codex 侧 spawn_agent
_SPAWN_TOOLS = ("Agent", "Task", "spawn_agent")


def cmd_agents(sf: SessionFiles, ev: list[Event]) -> int:
    if not sf.subagents:
        print("# 无子 agent(该 session 未派生子 agent)")
        return 0
    # 主/父日志里的 spawn 点,按 tool_use_id 关联子 agent
    spawns = {e.tool_use_id: e for e in ev
              if e.kind == Kind.TOOL and e.tool_name in _SPAWN_TOOLS}
    for s in sf.subagents:
        key = s.tool_use_id or s.path.stem
        d = _subagent_digest(ev, key)
        sp = spawns.get(s.tool_use_id)
        at = f"@{_rel(ev, sp)}" if sp else "@?"
        pad = "  " * getattr(s, "depth", 0)   # 调用树深度缩进(Claude 侧恒 0)
        bullet = "└■" if getattr(s, "depth", 0) else "■"
        print(f"{pad}{bullet} {s.agent_type}  {at}  {_oneline(s.description, 64)}")
        th = "  ".join(f"{n}×{c}" for n, c in d["tools"].most_common(6))
        print(f"{pad}    {d['n']} 事件  {sum(d['tools'].values())} 工具调用  "
              f"{d['dur']//60}m{d['dur']%60:02d}s  错误×{len(d['errors'])}  tokens {d['tok'].fmt()}")
        if th:
            print(f"{pad}    工具: {th}")
        if d["final"]:
            print(f"{pad}    末态: {_oneline(d['final'], 80)}")
        print(f"{pad}    日志: {s.path}   (toolUseId {s.tool_use_id})")
    print(f"\n# {len(sf.subagents)} 个子 agent。钻取某个: "
          f"ccq <sel> \"agent={sf.subagents[0].agent_type} | timeline\"")
    return 0


def cmd_skills(ev: list[Event]) -> int:
    found = [e for e in ev if e.kind == Kind.SKILL]
    if not found:
        print("# 未检测到 skill / slash 命令触发")
        return 0
    for e in found:
        print(f"[{_rel(ev, e)}] #{e.seq}  /{e.text}")
    return 0


def cmd_show(ev: list[Event], seq: int) -> int:
    hit = next((e for e in ev if e.seq == seq), None)
    if not hit:
        print(f"# 无 seq={seq}", file=sys.stderr)
        return 2
    print(f"seq {hit.seq}  kind {hit.kind.value}  ts {hit.ts}  "
          f"sidechain {hit.sidechain}  agent {hit.agent_type}")
    if hit.kind == Kind.TOOL:
        print(f"tool: {hit.tool_name}  id: {hit.tool_use_id}")
        print("input:")
        print(json.dumps(hit.tool_input, ensure_ascii=False, indent=2))
    elif hit.kind == Kind.RESULT:
        print(f"is_error: {hit.is_error}  tool_use_id: {hit.tool_use_id}")
        print("content:")
        print(hit.text)
    else:
        print(hit.text)
    return 0


def cmd_grep(ev: list[Event], pattern: str, scope: str = "io") -> int:
    """正则统计:总出现次数、命中事件数、按动作分布。
    scope: 'in'/'input' 只搜「人/机所写」(HUMAN/AGENT/THINKING/TOOL/SKILL);
           其余(缺省 'io'/'all') 额外含「环境返回」(RESULT)。"""
    rx = re.compile(pattern, re.I)
    inp_only = scope in ("in", "input")
    kinds = _INPUT_KINDS if inp_only else (_INPUT_KINDS | _OUTPUT_KINDS)
    matched = total_occ = 0
    by_action: Counter = Counter()
    for e in ev:
        if e.kind not in kinds:
            continue
        hits = rx.findall(_haystack(e))
        if hits:
            matched += 1
            total_occ += len(hits)
            by_action[_action_label(e)] += 1
            print(f"[{_rel(ev, e)}] {e.seq:>4} {_action_label(e)}  "
                  f"{e.summary(9999 if _VERBOSE else 100)}  «×{len(hits)}»")
    scope_desc = "人机所写" if inp_only else "人机所写,+环境返回"
    print(f"# 命中 {matched} 个事件 / 共 {total_occ} 次出现   (scope={scope}: {scope_desc})")
    if by_action:
        dist = "  ".join(f"{a}×{c}" for a, c in by_action.most_common())
        print(f"# 动作分布: {dist}")
    print("# 注: 单位是「事件/动作」非原始 JSONL 行;一个 assistant 回合可拆成多条事件")
    return 0


# --------------------------------------------------------------------------- #
# Layer F: files —— 会话读写文件统计 (R 只读 / A 创建 / M 修改)
# --------------------------------------------------------------------------- #
class FCat(IntEnum):
    """一个文件在某 agent 作用域内的最终归类。"""
    R = 0   # 只读:只被 Read,从未改动
    A = 1   # 创建:会话内新建(首次改动是无前置读的 Write,或 apply_patch Add)
    M = 2   # 修改:会话内改动既有文件(Edit,或改前读过的 Write,或 apply_patch Update)
    D = 3   # 删除:仅 Codex apply_patch Delete File(存在才展示)


_FCAT_LABEL = {FCat.R: "R 只读", FCat.A: "A 创建", FCat.M: "M 修改", FCat.D: "D 删除"}

# 结构化文件工具(Claude 侧)。Bash/shell 里的 cat、> 重定向等不透明,不纳入。
_READ_TOOLS = {"Read"}
_WRITE_TOOLS = {"Write"}                       # 覆盖写:文件可能新建也可能既有
_EDIT_TOOLS = {"Edit", "MultiEdit", "NotebookEdit"}   # 就地改:文件必然既有


def _file_path_of(e: Event) -> str | None:
    inp = e.tool_input if isinstance(e.tool_input, dict) else None
    if inp is None:
        return None
    return inp.get("file_path") or inp.get("notebook_path") or inp.get("path")


def _seq_churn(old: str, new: str) -> tuple[int, int]:
    """两段文本的行级变更量 (+增, −删)。replace 两侧都计入(即 churn,非净值)。"""
    a = d = 0
    sm = difflib.SequenceMatcher(None, old.splitlines(), new.splitlines())
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "replace":
            d += i2 - i1
            a += j2 - j1
        elif tag == "delete":
            d += i2 - i1
        elif tag == "insert":
            a += j2 - j1
    return a, d


def _parse_apply_patch(patch: str, count_lines: bool) -> list[tuple[str, str, int, int]]:
    """解析 Codex apply_patch → [(verb, path, adds, dels)]。verb ∈ Add/Update/Delete。
    count_lines=False 时仅取 (verb, path),不数 +/- 行(adds=dels=0)。"""
    out: list[tuple[str, str, int, int]] = []
    cur: tuple[str, str] | None = None
    a = d = 0

    def flush() -> None:
        nonlocal cur, a, d
        if cur:
            out.append((cur[0], cur[1], a, d))
        cur, a, d = None, 0, 0

    for line in (patch or "").splitlines():
        m = re.match(r"\*\*\*\s+(Add|Update|Delete)\s+File:\s+(.+?)\s*$", line)
        if m:
            flush()
            cur = (m.group(1), m.group(2).strip())
            continue
        if line.startswith("*** "):     # *** Begin/End Patch 等分节标记
            flush()
            continue
        if cur is None or not count_lines:
            continue
        if line.startswith("+"):
            a += 1
        elif line.startswith("-"):
            d += 1
    flush()
    return out


# 一条文件操作:(seq, op, adds, dels)。op ∈ {R,W,E,ADD,UPD,DEL};adds/dels 仅在
# _COUNT_LINES 且该操作属「精确可计」的前三种场景时非零。
def _collect_file_ops(events: list[Event]) -> dict[str, list[tuple[int, str, int, int]]]:
    """作用域内每个文件的操作序列:path → [(seq, op, adds, dels)],按 seq 排序。"""
    ops: dict[str, list[tuple[int, str, int, int]]] = defaultdict(list)
    cl = _COUNT_LINES
    for e in events:
        if e.kind != Kind.TOOL:
            continue
        name = e.tool_name or ""
        inp = e.tool_input if isinstance(e.tool_input, dict) else {}
        if name in _READ_TOOLS:
            p = _file_path_of(e)
            if p:
                ops[p].append((e.seq, "R", 0, 0))
        elif name in _WRITE_TOOLS:
            p = _file_path_of(e)
            if p:
                # 新增行数;是否计入由 _classify 按最终归类(仅 A 新建)决定
                a = len((inp.get("content") or "").splitlines()) if cl else 0
                ops[p].append((e.seq, "W", a, 0))
        elif name in _EDIT_TOOLS:
            p = _file_path_of(e)
            if p:
                a = d = 0
                if cl:
                    if name == "Edit":
                        a, d = _seq_churn(inp.get("old_string") or "",
                                          inp.get("new_string") or "")
                    elif name == "MultiEdit":
                        for ed in (inp.get("edits") or []):
                            if isinstance(ed, dict):
                                aa, dd = _seq_churn(ed.get("old_string") or "",
                                                    ed.get("new_string") or "")
                                a += aa
                                d += dd
                    # NotebookEdit:无旧内容可比,归 M 但不计行
                ops[p].append((e.seq, "E", a, d))
        elif name == "apply_patch":   # Codex:补丁头给出 Add/Update/Delete + 逐行 +/-
            patch = e.tool_input if isinstance(e.tool_input, str) else inp.get("input", "")
            for verb, path, a, d in _parse_apply_patch(patch or "", cl):
                op = {"Add": "ADD", "Update": "UPD", "Delete": "DEL"}[verb]
                ops[path].append((e.seq, op, a, d))
    for lst in ops.values():
        lst.sort()
    return ops


def _classify(ops: dict[str, list[tuple[int, str, int, int]]]
              ) -> dict[str, tuple[FCat, int, int, int]]:
    """path → (归类, 操作次数, adds, dels)。按「首个改动操作」定 A/M,无改动则 R。
    行数只累计前三种精确场景:Edit/MultiEdit、Write 新建、apply_patch Add/Update;
    Write 覆盖(盲区)与 Delete 不计,adds/dels 保持 0。"""
    out: dict[str, tuple[FCat, int, int, int]] = {}
    for path, lst in ops.items():
        muts = [(s, op) for s, op, _, _ in lst if op != "R"]
        if not muts:
            out[path] = (FCat.R, len(lst), 0, 0)
            continue
        fseq, fop = muts[0]
        if fop == "DEL":
            cat = FCat.D
        elif fop == "ADD":
            cat = FCat.A
        elif fop in ("E", "UPD"):     # Edit/Update:文件必然既有 → 修改
            cat = FCat.M
        else:                          # 'W' 覆盖写:改前读过=既有(M),否则新建(A)
            read_before = any(s < fseq and op == "R" for s, op, _, _ in lst)
            cat = FCat.M if read_before else FCat.A
        adds = dels = 0
        for _, op, a, d in lst:
            if op in ("E", "ADD", "UPD"):          # 前三种精确场景
                adds += a
                dels += d
            elif op == "W" and cat == FCat.A:       # Write 新建才计;覆盖(M)不计
                adds += a
                dels += d
        out[path] = (cat, len(lst), adds, dels)
    return out


def _render_scope(title: str, cats: dict[str, tuple[FCat, int, int, int]]) -> Counter:
    """打印一个作用域的 R/A/M(/D)分组明细,返回各类计数 Counter。
    _COUNT_LINES 开启时,标题带作用域总 churn,每个可计文件带 +增/−删。"""
    buckets: dict[FCat, list[tuple[str, int, int, int]]] = {c: [] for c in FCat}
    for path, (c, n, a, d) in cats.items():
        buckets[c].append((path, n, a, d))
    counts = Counter({c: len(buckets[c]) for c in FCat})
    parts = [f"{c.name}{counts[c]}" for c in (FCat.R, FCat.A, FCat.M)]
    if counts[FCat.D]:
        parts.append(f"D{counts[FCat.D]}")
    head = f"{title}   ({' / '.join(parts)}"
    if _COUNT_LINES:
        ta = sum(a for _, (c, n, a, d) in cats.items())
        td = sum(d for _, (c, n, a, d) in cats.items())
        head += f"   +{ta}/−{td}"
    print(head + ")")
    for c in (FCat.R, FCat.A, FCat.M, FCat.D):
        items = sorted(buckets[c])
        if not items:
            continue
        print(f"  {_FCAT_LABEL[c]} ({len(items)})")
        for path, n, a, d in items:
            churn = f"  +{a}/−{d}" if (_COUNT_LINES and (a or d)) else ""
            opn = f"  ×{n}" if n > 1 else ""
            print(f"    {path}{churn}{opn}")
    return counts


def _files_json(sf: SessionFiles, main_cats, groups, sub_meta) -> dict:
    def scope(cats):
        d = {c.name: [] for c in FCat}
        for path, (c, n, a, dd) in sorted(cats.items()):
            item = {"path": path, "ops": n}
            if _COUNT_LINES:
                item["added"], item["removed"] = a, dd
            d[c.name].append(item)
        if not d["D"]:
            del d["D"]
        return d
    return {
        "session_id": sf.session_id,
        "project": sf.project,
        "main": scope(main_cats),
        "subagents": [
            {
                "agent_type": (sub_meta.get(aid).agent_type if sub_meta.get(aid) else "?"),
                "agent_id": aid,
                "description": (sub_meta.get(aid).description if sub_meta.get(aid) else ""),
                "files": scope(_classify(_collect_file_ops(evs))),
            }
            for aid, evs in groups.items()
        ],
    }


def cmd_files(sf: SessionFiles, ev: list[Event], as_json: bool = False) -> int:
    """会话读写文件统计。归类 R 只读 / A 创建 / M 修改;
    子 agent 含在范围内但**独立作用域分别统计、分别展示**(不并入主 agent 数字)。"""
    main_ev = [e for e in ev if not e.sidechain]
    main_cats = _classify(_collect_file_ops(main_ev))
    # 子 agent 事件按 agent_id 分组(每个子 agent 一个独立作用域)
    groups: dict[str, list[Event]] = defaultdict(list)
    for e in ev:
        if e.sidechain and e.agent_id:
            groups[e.agent_id].append(e)
    sub_meta = {s.tool_use_id: s for s in sf.subagents}

    if as_json:
        print(json.dumps(_files_json(sf, main_cats, groups, sub_meta),
                         ensure_ascii=False, indent=2))
        return 0

    print(f"session {sf.session_id}   project {sf.project}\n")
    _render_scope("主 agent  文件读写", main_cats)

    if groups:
        print(f"\n子 agent 文件读写  (分别统计, {len(groups)} 个)")
        agg: Counter = Counter()
        for aid, evs in groups.items():
            s = sub_meta.get(aid)
            head = f"■ {s.agent_type if s else '?'}"
            if s and s.description:
                head += f"  «{_oneline(s.description, 50)}»"
            print()
            agg.update(_render_scope(head, _classify(_collect_file_ops(evs))))
        parts = [f"{c.name}{agg[c]}" for c in (FCat.R, FCat.A, FCat.M)]
        if agg[FCat.D]:
            parts.append(f"D{agg[FCat.D]}")
        print(f"\n# 子 agent 合计: {' / '.join(parts)}")

    print("\n# 仅统计结构化文件工具(Read/Write/Edit/NotebookEdit,Codex apply_patch);"
          "Bash/shell 里的 cat、> 重定向等不透明,不在范围。")
    print("# R=只读  A=会话内新建  M=会话内修改(用 Edit,或改前读过)。×n=该文件被操作次数。")
    if _COUNT_LINES:
        print("# +增/−删=行级变更量(churn,replace 两侧都计)。仅 Edit/MultiEdit、"
              "Write 新建、apply_patch Add/Update 精确;Write 覆盖既有文件的行数未知,不计。")
    else:
        print("# 加 -L/--lines 统计每个文件的变更行数(+增/−删,较耗时)。")
    return 0


# --------------------------------------------------------------------------- #
# Layer S: skill-report (核心场景交付)
# --------------------------------------------------------------------------- #
def cmd_skill_report(sf: SessionFiles, ev: list[Event], skill: str | None) -> int:
    triggers = [e for e in ev if e.kind == Kind.SKILL]
    if skill:
        triggers = [e for e in triggers if skill.lower() in e.text.lower()]
    if not triggers:
        print("# 未检测到 skill 触发(本工具按 /slash 与 <command-name> 标签识别)。")
        print("# 若该 skill 由 Skill 工具调用,请用:  ccq <sel> \"name=Skill | show\"")
        # 退化:仍给 Skill 工具调用线索
        sk_tool = [e for e in ev if e.kind == Kind.TOOL and e.tool_name == "Skill"]
        for e in sk_tool:
            print(_line_for(ev, e))
        return 0

    all_trigs = [e for e in ev if e.kind == Kind.SKILL]
    genuine = _genuine_humans(ev)
    for trig in triggers:
        # 区间:触发点 → 下一个 skill 触发 或 下一个真实提问(命令展开不算边界)
        start = trig.seq
        end = _seg_end(ev, start, all_trigs, genuine)
        seg = [e for e in ev if start <= e.seq < end]

        # 触发前最近的真实人类意图(跳过命令展开);
        # Codex $-触发无前置人类时,回退到触发消息自身($... 文本即意图)。
        # 仅回退到 Codex $-消息(无 <command-*> 标签),Claude 的命令注入消息仍跳过。
        intent = next((e for e in reversed([x for x in genuine if x.seq < start])), None)
        if intent is None:
            intent = next((e for e in ev if e.seq >= start and e.kind == Kind.HUMAN
                           and not e.sidechain
                           and not any(t in (e.text or "") for t in _META_TAGS)), None)
        tools = Counter(e.tool_name for e in seg if e.kind == Kind.TOOL)
        errs = [e for e in seg if e.kind == Kind.RESULT and e.is_error]
        # 段内 spawn 调用 → 子 agent(独立日志文件);role 优先取 sf.subagents
        sub_by_id = {s.tool_use_id: s for s in sf.subagents}
        spawns = [e for e in seg if e.kind == Kind.TOOL and e.tool_name in _SPAWN_TOOLS]
        subs = Counter(_spawn_role(e, sub_by_id)[0] for e in spawns)
        # 段内人类插话(中断/纠偏):排除触发自身的命令展开,以及与触发同 part 的
        # HUMAN(opencode footer 切分后 genuine prompt 与 SKILL 同 uuid,是意图非纠偏)
        interj = [e for e in seg if e.kind == Kind.HUMAN and not e.sidechain
                  and e.seq != start and not _is_cmd(e) and e.uuid != trig.uuid]
        _, _, dur = _span(seg)
        tok = sum((e.tokens for e in seg), Token())
        # 重复读同一文件(摩擦信号)
        reads = Counter(
            json.dumps(e.tool_input.get("file_path") if isinstance(e.tool_input, dict) else None)
            for e in seg if e.kind == Kind.TOOL and e.tool_name == "Read"
        )
        rereads = {k: v for k, v in reads.items() if v > 1 and k != "null"}
        last_agent = next((e for e in reversed(seg) if e.kind == Kind.AGENT), None)

        print("=" * 64)
        print(f"SKILL REPORT  /{trig.text}")
        print("=" * 64)
        print(f"① 触发      seq#{trig.seq}  @{_rel(ev, trig)}")
        print(f"   意图      {_oneline(intent.text, 90) if intent else '(无前置人类消息)'}")
        print(f"② 执行      {len(seg)} 事件  {sum(tools.values())} 工具调用  "
              f"{dur // 60}m{dur % 60:02d}s")
        print(f"   工具      {'  '.join(f'{n}×{c}' for n, c in tools.most_common(8))}")
        sub_tok = Token()
        if spawns:
            print(f"   子agent   {'  '.join(f'{k}×{c}' for k, c in subs.items())}"
                  f"   (各子 agent 独立日志,详见 agents)")
            for sp in spawns:
                d = _subagent_digest(ev, sp.tool_use_id)
                sub_tok += d["tok"]
                styp, desc = _spawn_role(sp, sub_by_id)
                print(f"     └ {styp} «{_oneline(desc, 40)}»: "
                      f"{sum(d['tools'].values())} 工具  错误×{len(d['errors'])}  "
                      f"tokens {d['tok'].fmt()}")
                if d["final"]:
                    print(f"        ⇒ {_oneline(d['final'], 76)}")
        print(f"③ 摩擦      错误×{len(errs)}  人类纠偏×{len(interj)}  重复读×{len(rereads)}")
        for e in errs[:4]:
            print(f"     ✗ {_oneline(e.text, 90)}")
        for e in interj[:3]:
            print(f"     ⟳ 人类: {_oneline(e.text, 80)}")
        cost = tok.fmt()
        if sub_tok.total:
            cost += f"  (+子agent {sub_tok.fmt()})"
        print(f"④ 成本      {cost}")
        # 结束原因
        nxt = next((e for e in ev if e.seq == end), None)
        if nxt is None:
            why = "会话在 skill 段中结束"
        elif nxt.kind == Kind.SKILL:
            why = f"被下一个命令 /{nxt.text} 接续"
        else:
            why = "人类下达新指令"
        print(f"⑤ 结果      {why}")
        if last_agent:
            print(f"   末态      {_oneline(last_agent.text, 100)}")
        print()
    return 0


# --------------------------------------------------------------------------- #
# Layer Q: 查询语言
# --------------------------------------------------------------------------- #
def _parse_query(q: str):
    """'kind=tool name=Bash is_error=1 | show' → (preds, verb)."""
    verb = "timeline"
    if "|" in q:
        q, verb = q.rsplit("|", 1)
        verb = verb.strip() or "timeline"
    preds = {}
    for tok in q.split():
        if "~" in tok:
            preds.setdefault("text~", []).append(tok.split("~", 1)[1])
        elif "=" in tok:
            k, v = tok.split("=", 1)
            preds[k] = v
    return preds, verb


def _match(e: Event, preds: dict) -> bool:
    for k, v in preds.items():
        if k == "kind" and e.kind.value != v:
            return False
        if k == "name" and e.tool_name != v:
            return False
        if k == "is_error" and bool(e.is_error) != (v in ("1", "true", "yes")):
            return False
        if k == "sidechain" and e.sidechain != (v in ("1", "true", "yes")):
            return False
        if k == "agent" and (e.agent_type or "") != v:
            return False
        if k == "after" and _hhmm(e.ts) < v:
            return False
        if k == "before" and _hhmm(e.ts) > v:
            return False
        if k == "text~":
            hay = (e.text or "")
            if e.tool_input:
                hay += " " + json.dumps(e.tool_input, ensure_ascii=False)
            for rx in v:
                if not re.search(rx, hay, re.I):
                    return False
    return True


def _skill_span(ev: list[Event], skill: str) -> set[int]:
    """skill= 谓词:返回该 skill 触发到下一个人类发言之间的 seq 集合。"""
    seqs: set[int] = set()
    all_trigs = [e for e in ev if e.kind == Kind.SKILL]
    genuine = _genuine_humans(ev)
    trigs = [e for e in all_trigs if skill.lower() in e.text.lower()]
    for t in trigs:
        end = _seg_end(ev, t.seq, all_trigs, genuine)
        seqs.update(e.seq for e in ev if t.seq <= e.seq < end)
    return seqs


def cmd_query(sf: SessionFiles, ev: list[Event], q: str) -> int:
    preds, verb = _parse_query(q)
    span = None
    if "skill" in preds:
        span = _skill_span(ev, preds.pop("skill"))
    sel = [e for e in ev if _match(e, preds) and (span is None or e.seq in span)]

    if verb == "count":
        print(len(sel))
    elif verb == "text":
        for e in sel:
            print(e.text.strip()); print("-" * 40)
    elif verb == "tools":
        for e in sel:
            if e.kind == Kind.TOOL:
                print(_line_for(ev, e))
    elif verb == "show":
        for e in sel:
            cmd_show(ev, e.seq); print("=" * 50)
    elif verb == "json":
        print(json.dumps([{"seq": e.seq, "kind": e.kind.value, "ts": e.ts,
                           "tool": e.tool_name, "is_error": e.is_error,
                           "tokens": e.tokens.as_dict(), "text": e.text[:500]} for e in sel],
                         ensure_ascii=False, indent=2))
    else:  # timeline
        for e in sel:
            print(_line_for(ev, e))
    return 0


# --------------------------------------------------------------------------- #
# --check / --validate
# --------------------------------------------------------------------------- #
def do_check(codex: bool = False, opencode: bool = False) -> int:
    if opencode:
        db = opencode_db_path()
        ok = db.exists()
        print(f"[check] opencode db: {db}  {'OK' if ok else '缺失'}")
        if not ok:
            return 1
        try:
            n = sum(1 for _ in iter_opencode_sessions())
        except Exception as e:
            print(f"[check] 读取失败: {e}", file=sys.stderr)
            return 1
        print(f"[check] 发现 {n} 个 opencode 顶层会话")
        print(f"[check] python {sys.version.split()[0]}  (无需 jq)")
        return 0 if n else 1
    if codex:
        from ccq.ccq_core import codex_sessions_root, iter_codex_logs
        root = codex_sessions_root()
        ok = root.is_dir()
        print(f"[check] codex sessions root: {root}  {'OK' if ok else '缺失'}")
        if not ok:
            return 1
        n = sum(1 for _ in iter_codex_logs())
        print(f"[check] 发现 {n} 个 Codex 会话日志")
        print(f"[check] python {sys.version.split()[0]}  (无需 jq)")
        return 0 if n else 1
    root = projects_root()
    ok = root.is_dir()
    print(f"[check] projects root: {root}  {'OK' if ok else '缺失'}")
    if not ok:
        return 1
    n = sum(1 for _ in root.glob("*/*.jsonl"))
    print(f"[check] 发现 {n} 个主会话日志")
    print(f"[check] python {sys.version.split()[0]}  (无需 jq)")
    return 0 if n else 1


def do_validate(selector: str, codex: bool = False,
                opencode: bool = False) -> int:
    try:
        sf, ev = load_session(selector, codex=codex, opencode=opencode)
    except Exception as e:
        print(f"[validate] 加载失败: {e}", file=sys.stderr)
        return 1
    tu = {e.tool_use_id for e in ev if e.kind == Kind.TOOL}
    tr = [e for e in ev if e.kind == Kind.RESULT]
    orphan = [e for e in tr if e.tool_use_id not in tu]
    print(f"[validate] session {sf.session_id}  事件 {len(ev)}  "
          f"工具 {len(tu)}  结果 {len(tr)}  孤儿结果 {len(orphan)}")
    # opencode 子 agent 是 DB 行而非文件,s.path=Path(<session_id>)不指向真实文件;
    # 已从库查得故存在性隐含,只报数。
    if opencode:
        print(f"[validate] 子 agent {len(sf.subagents)} 个(库内 session 行)")
    else:
        print(f"[validate] 子 agent {len(sf.subagents)} 个均可解析"
              if all(s.path.exists() for s in sf.subagents)
              else "[validate] 子 agent 文件缺失")
    return 0 if len(orphan) == 0 else 1


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def main() -> None:
    ap = argparse.ArgumentParser(
        prog="ccq", description="Claude Code session 日志查询工具",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("--json", action="store_true", help="结构化输出(locate)")
    ap.add_argument("-v", "--verbose", action="store_true", help="详细输出(不截断)")
    ap.add_argument("-q", "--quiet", action="store_true", help="精简输出")
    ap.add_argument("-L", "--lines", action="store_true",
                    help="files:统计变更行数(+增/−删;仅 Edit/新建 Write/apply_patch,较耗时)")
    ap.add_argument("--codex", action="store_true",
                    help="读 Codex(~/.codex)会话而非 Claude Code 会话;"
                         "selector 主用工作目录(按会话 cwd 反查)")
    ap.add_argument("--opencode", action="store_true",
                    help="读 opencode(~/.local/share/opencode/opencode.db)SQLite 库"
                         "而非 Claude Code 会话;selector 用工作目录 / session-id 前缀 / latest")
    ap.add_argument("--page", type=int, default=1, metavar="N",
                    help="sessions:翻页,默认第 1 页(最近的)")
    ap.add_argument("--limit", "-n", type=int, default=20, metavar="N",
                    help="sessions:每页条数,默认 20;<=0 展示全部(关分页)")
    ap.add_argument("--check", action="store_true",
                    help="预检环境(加 --codex 检 Codex;加 --opencode 检 opencode 库)")
    ap.add_argument("--validate", metavar="SEL",
                    help="校验某 session 解析完整性(加 --codex/--opencode 校验对应会话)")
    ap.add_argument("args", nargs="*", help="locate <sel> | <sel> <verb> [...]")
    ns = ap.parse_args()
    global _QUIET, _VERBOSE, _COUNT_LINES
    _QUIET = ns.quiet
    _VERBOSE = ns.verbose
    _COUNT_LINES = ns.lines

    if ns.check:
        sys.exit(do_check(ns.codex, ns.opencode))
    if ns.validate:
        sys.exit(do_validate(ns.validate, ns.codex, ns.opencode))

    a = ns.args
    if not a:
        ap.print_help()
        sys.exit(0)

    # 定位分支
    if a[0] == "locate":
        if len(a) < 2:
            print("用法: ccq locate <selector>", file=sys.stderr)
            sys.exit(2)
        sys.exit(cmd_locate(a[1], ns.json, ns.codex, ns.opencode))

    # 项目级会话枚举/计数分支(selector 是项目目录/关键字,非单个 session)
    if a[0] == "sessions":
        sys.exit(cmd_sessions(a[1] if len(a) > 1 else "", ns.json, ns.codex,
                              opencode=ns.opencode, page=ns.page, limit=ns.limit))

    # 其余:第一个参数是 selector/path,其后是 verb 或 query
    selector = a[0]
    rest = a[1:]
    try:
        sf, ev = load_session(selector, codex=ns.codex, opencode=ns.opencode)
    except Exception as e:
        print(f"ccq: {e}", file=sys.stderr)
        sys.exit(2)

    if not rest:
        sys.exit(cmd_overview(sf, ev))
    verb = rest[0]

    if verb == "overview":
        sys.exit(cmd_overview(sf, ev))
    if verb == "timeline":
        sys.exit(cmd_timeline(ev))
    if verb == "human":
        sys.exit(cmd_human(ev))
    if verb == "tools":
        sys.exit(cmd_tools(ev, rest[1] if len(rest) > 1 else None))
    if verb == "errors":
        sys.exit(cmd_errors(ev))
    if verb == "agents":
        sys.exit(cmd_agents(sf, ev))
    if verb == "skills":
        sys.exit(cmd_skills(ev))
    if verb == "files":
        sys.exit(cmd_files(sf, ev, ns.json))
    if verb == "show":
        sys.exit(cmd_show(ev, int(rest[1])))
    if verb == "grep":
        scope = rest[2] if len(rest) > 2 else "io"
        sys.exit(cmd_grep(ev, rest[1], scope))
    if verb in ("skill-report", "skill_report"):
        sys.exit(cmd_skill_report(sf, ev, rest[1] if len(rest) > 1 else None))
    if verb == "query":
        sys.exit(cmd_query(sf, ev, " ".join(rest[1:])))

    # 隐式查询:含 = ~ | 的当作 DSL
    joined = " ".join(rest)
    if any(c in joined for c in "=~|"):
        sys.exit(cmd_query(sf, ev, joined))

    print(f"未知动词: {verb}", file=sys.stderr)
    ap.print_help()
    sys.exit(2)


if __name__ == "__main__":
    main()
