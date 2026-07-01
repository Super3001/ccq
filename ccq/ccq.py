"""ccq —— Claude Code session 日志查询工具 (CLI 前端).

读另一个 Claude Code session 的日志,诊断其运行情况:人类说了什么、
agent 做了什么、遇到什么错误,并评估「agent 使用某 skill 的效果」。

分层用法(zoom out → zoom in):
    ccq locate <selector>            # 定位分支:返回该 session 的全部 log 路径
    ccq sessions <项目目录|关键字>   # 枚举/计数某项目下全部主会话与子 agent
    ccq <selector|path> overview     # 一屏看懂
    ccq <selector|path> timeline     # 紧凑事件流
    ccq <selector|path> human|tools|errors|agents|skills
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
  uuid 前缀 / latest。所有动词通用(Codex 无子 agent)。
    ccq --codex sessions C:/w/HomeTrans-CJ   # 该目录下全部 Codex 会话与计数
    ccq --codex C:/w/HomeTrans-CJ overview    # 该目录最近一个 Codex 会话概览
    ccq --codex <uuid前缀> errors
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

# 模块级 verbosity:0=quiet,1=normal(默认),2=verbose
_QUIET = False
_VERBOSE = False

from .ccq_core import (
    Event, Kind, SessionFiles, list_sessions, load_session, locate,
    projects_root, _codex_skill_names,
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
    Claude 侧:harness 注入的 <command-*> 标签;Codex 侧:$<name> 前缀触发。
    这类消息不应成为它自己 skill 段的右边界。"""
    t = e.text or ""
    if any(tag in t for tag in _META_TAGS):
        return True
    return bool(_codex_skill_names(t))


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
    """skill 段终点:start 之后最近的「下一个 skill 触发」或「下一个真实提问」。"""
    cands = [t.seq for t in trigs if t.seq > start] + [h.seq for h in genuine if h.seq > start]
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
def cmd_locate(selector: str, as_json: bool, codex: bool = False) -> int:
    try:
        sf = locate(selector, codex=codex)
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


def cmd_sessions(selector: str, as_json: bool, codex: bool = False) -> int:
    """枚举某项目目录(或关键字)下的全部主会话与子 agent,并给出计数。
    selector 含路径分隔符时按工作目录精确匹配(含 .claude 等子目录会话)。
    codex=True 时枚举 Codex 会话(按会话 cwd 匹配该目录,无子 agent)。"""
    sessions = list_sessions(selector, codex=codex)
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

    if as_json:
        print(json.dumps({
            "selector": selector,
            "projects": [
                {
                    "project": proj,
                    "main_sessions": len(g),
                    "subagents": sum(len(s.subagents) for s in g),
                    "sessions": [
                        {"session_id": s.session_id,
                         "mtime": _mtime_str(s.mtime),
                         "subagents": len(s.subagents),
                         "main": str(s.main)}
                        for s in g
                    ],
                }
                for proj, g in groups.items()
            ],
            "total_projects": len(groups),
            "total_main_sessions": total_main,
            "total_subagents": total_sub,
        }, ensure_ascii=False, indent=2))
        return 0

    for proj, g in groups.items():
        sub_n = sum(len(s.subagents) for s in g)
        print(f"project {proj}   ({len(g)} 主会话, {sub_n} 子agent)")
        for s in g:
            mark = f"subs {len(s.subagents)}" if s.subagents else "      "
            print(f"  {s.session_id[:8]}  {_mtime_str(s.mtime)}   {mark}")
        print()
    print(f"# 合计: {len(groups)} 个项目目录, {total_main} 主会话, {total_sub} 子agent")
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
    tin = sum(e.tokens_in for e in ev)
    tout = sum(e.tokens_out for e in ev)
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
    print(f"tokens  ~{tin:,} in / {tout:,} out")
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
            "final": last.text if last else "", "tok_out": sum(e.tokens_out for e in sub)}


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
              f"{d['dur']//60}m{d['dur']%60:02d}s  错误×{len(d['errors'])}  {d['tok_out']:,} tok_out")
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
        # 段内人类插话(中断/纠偏):排除触发自身的命令展开
        interj = [e for e in seg if e.kind == Kind.HUMAN and not e.sidechain
                  and e.seq != start and not _is_cmd(e)]
        _, _, dur = _span(seg)
        tin = sum(e.tokens_in for e in seg)
        tout = sum(e.tokens_out for e in seg)
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
        sub_tok = 0
        if spawns:
            print(f"   子agent   {'  '.join(f'{k}×{c}' for k, c in subs.items())}"
                  f"   (各子 agent 独立日志,详见 agents)")
            for sp in spawns:
                d = _subagent_digest(ev, sp.tool_use_id)
                sub_tok += d["tok_out"]
                styp, desc = _spawn_role(sp, sub_by_id)
                print(f"     └ {styp} «{_oneline(desc, 40)}»: "
                      f"{sum(d['tools'].values())} 工具  错误×{len(d['errors'])}  "
                      f"{d['tok_out']:,} tok_out")
                if d["final"]:
                    print(f"        ⇒ {_oneline(d['final'], 76)}")
        print(f"③ 摩擦      错误×{len(errs)}  人类纠偏×{len(interj)}  重复读×{len(rereads)}")
        for e in errs[:4]:
            print(f"     ✗ {_oneline(e.text, 90)}")
        for e in interj[:3]:
            print(f"     ⟳ 人类: {_oneline(e.text, 80)}")
        cost = f"~{tin:,} tok_in / {tout:,} tok_out"
        if sub_tok:
            cost += f"  (+子agent {sub_tok:,} tok_out)"
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
                           "text": e.text[:500]} for e in sel],
                         ensure_ascii=False, indent=2))
    else:  # timeline
        for e in sel:
            print(_line_for(ev, e))
    return 0


# --------------------------------------------------------------------------- #
# --check / --validate
# --------------------------------------------------------------------------- #
def do_check(codex: bool = False) -> int:
    if codex:
        from .ccq_core import codex_sessions_root, iter_codex_logs
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


def do_validate(selector: str, codex: bool = False) -> int:
    try:
        sf, ev = load_session(selector, codex=codex)
    except Exception as e:
        print(f"[validate] 加载失败: {e}", file=sys.stderr)
        return 1
    tu = {e.tool_use_id for e in ev if e.kind == Kind.TOOL}
    tr = [e for e in ev if e.kind == Kind.RESULT]
    orphan = [e for e in tr if e.tool_use_id not in tu]
    print(f"[validate] session {sf.session_id}  事件 {len(ev)}  "
          f"工具 {len(tu)}  结果 {len(tr)}  孤儿结果 {len(orphan)}")
    print(f"[validate] 子 agent {len(sf.subagents)} 个均可解析"
          if all(s.path.exists() for s in sf.subagents) else "[validate] 子 agent 文件缺失")
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
    ap.add_argument("--codex", action="store_true",
                    help="读 Codex(~/.codex)会话而非 Claude Code 会话;"
                         "selector 主用工作目录(按会话 cwd 反查)")
    ap.add_argument("--check", action="store_true", help="预检环境")
    ap.add_argument("--validate", metavar="SEL", help="执行后校验某 session 解析完整性")
    ap.add_argument("args", nargs="*", help="locate <sel> | <sel> <verb> [...]")
    ns = ap.parse_args()
    global _QUIET, _VERBOSE
    _QUIET = ns.quiet
    _VERBOSE = ns.verbose

    if ns.check:
        sys.exit(do_check(ns.codex))
    if ns.validate:
        sys.exit(do_validate(ns.validate, ns.codex))

    a = ns.args
    if not a:
        ap.print_help()
        sys.exit(0)

    # 定位分支
    if a[0] == "locate":
        if len(a) < 2:
            print("用法: ccq locate <selector>", file=sys.stderr)
            sys.exit(2)
        sys.exit(cmd_locate(a[1], ns.json, ns.codex))

    # 项目级会话枚举/计数分支(selector 是项目目录/关键字,非单个 session)
    if a[0] == "sessions":
        sys.exit(cmd_sessions(a[1] if len(a) > 1 else "", ns.json, ns.codex))

    # 其余:第一个参数是 selector/path,其后是 verb 或 query
    selector = a[0]
    rest = a[1:]
    try:
        sf, ev = load_session(selector, codex=ns.codex)
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
