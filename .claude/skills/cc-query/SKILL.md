---
name: cc-query
description: Use ccq to diagnose other Claude Code, OpenAI Codex, or opencode sessions. Triggers when user asks "what happened in session X", "why did that build fail", "review that session's skill usage", "check on my other Claude tab", "what was the agent doing last night", or any request to inspect/pry/review/audit a past or parallel Claude Code / Codex / opencode session. Also triggers when the user refers to a previous session by partial ID, wants to understand why a build failed, how a skill performed, or what errors occurred. Use this skill even for indirect references like "how did that go earlier", "was the hmos skill effective yesterday", "看看那个 opencode 会话", "opencode 跑得怎么样", or any opencode log/session diagnosis need. Also triggers when the user uses / refers to `ccq` or `cc-query` literally.
---

# cc-query — Diagnose Other Claude Code Sessions

Use the globally-installed `ccq` CLI to peer into another session's logs with zero context-switch cost.

**Core workflow** — zoom out then zoom in:

```
ccq <sel> overview        → see the whole session at a glance
ccq <sel> errors          → what broke?
ccq <sel> files           → which files were read / created / modified?
ccq <sel> skill-report [name] → was that skill effective?
ccq <sel> "kind=tool name=Bash is_error=1 | show" → drill into one failure
```

## Before You Start

### Locate the session

```bash
# Which sessions are active in this project?
ccq locate latest              # most recent across all projects
ccq locate latest:HomeTrans    # most recent matching project keyword
ccq locate d74d               # partial session-id prefix
ccq locate /path/to/abc123.jsonl  # direct file path
```

`ccq locate` outputs the session-id, project name, main log path, and all sub-agent paths. Use `--json` for machine-readable output.

### Enumerate / count all sessions in a project

`locate` returns only the single latest session. To list (and count) **every** main session and sub-agent under a project directory:

```bash
ccq sessions C:/w/HomeTrans-CJ   # path selector: exact dir + its .claude/ subdir sessions
ccq sessions HomeTrans           # keyword selector: substring match on project folder name
ccq sessions HomeTrans --json    # machine-readable: per-project + grand totals
ccq sessions HomeTrans --page 2  # older page (sessions 21–40, newest-first)
ccq sessions HomeTrans --limit 0 # disable paging: every session (slower, parses all)
```

Output groups by project folder, lists each session (short id, mtime, sub-agent count), and under each session a short **会话摘要** (first genuine human input / last genuine human input / last agent reply — the same genuine-human filtering `overview` uses, so `<command-*>` and `$skill` triggers are skipped; the last-human line is omitted when there was only one human turn). It ends with a `# 合计:` line with total projects / main sessions / sub-agents. In `--json`, each session carries a `summary` object (`first_human` / `last_human` / `last_agent`).

**Pagination (default on):** because each summary requires reading the session's whole log, `sessions` defaults to the **20 most recent** sessions (`mtime` newest-first, across all matched project dirs) and only parses summaries for that page — on a 55-session project this is ~0.26s instead of parsing all 55. Use `--page N` to walk back to older sessions, `--limit N` to change the page size, or `--limit 0` to show everything (no pagination, parses all). The `# 合计:` count always reflects the full total, not just the shown page; `--json` adds a top-level `pagination` block (`page` / `limit` / `shown` / `start` / `end` / `total_main_sessions`) and a per-project `shown` count.

A path selector is encoded the same lossy way Claude Code names project dirs (every non-alnum char → `-`), so `C:/w/HomeTrans-CJ` also catches the `…\.claude` working-dir variant as a child.

### Check the tool works

```bash
ccq --check               # verify projects root and session count
ccq --codex --check       # verify Codex sessions root + count
ccq --opencode --check    # verify opencode db + top-level session count
ccq --validate d74d            # verify a specific Claude session parses cleanly
ccq --opencode --validate ses_0bb1e5   # verify an opencode session parses cleanly
```

### Diagnosing Codex sessions (`--codex`)

Pass `--codex` to read **OpenAI Codex CLI** sessions (`~/.codex/sessions/…/rollout-*.jsonl`) instead of Claude Code logs. Every verb works the same; only the source and selector semantics change:

- Codex archives by **date, not project dir** — each rollout's line-1 `session_meta.cwd` records its working directory, so the selector is primarily a **directory** (matched against each session's `cwd`). uuid-prefix and `latest` still work.
- Codex sub-agents **are** separate rollout files (same date-archive, not a subdir). ccq reconstructs the call tree: a parent's `spawn_agent` tool call links to its child via the `function_call_output` (`{"agent_id": <child-uuid>}`); the child rollout is found by that uuid and parsed recursively (nested spawns render indented in `agents`). Sub-agent sessions (`thread_source: "subagent"` / `parent_thread_id` set) are excluded from the top-level `sessions`/`locate` listing — reach them via the parent's `agents` verb.
- Codex **skills are `$`-prefixed**, not `/slash` — a user message beginning with `$<name>` (chainable, e.g. `$test-generator $systematic-debugging`) is a skill trigger. `skills` / `skill-report` detect these; `skill=<name>` confines a query to that `$`-skill's segment.

```bash
ccq --codex --check                         # codex root + session count
ccq --codex sessions C:/w/HomeTrans-CJ      # all Codex sessions run in that dir + count
ccq --codex C:/w/HomeTrans-CJ overview      # latest Codex session for that dir
ccq --codex <uuid-prefix> errors            # drill into one Codex session
ccq --codex <uuid> "name=shell_command is_error=1 | show"
```

Mapping into ccq's model: human/agent/thinking text + token counts come from the clean `event_msg` stream; tool calls/results from `response_item` (`shell_command`, `apply_patch`, `web_search`, …). Tokens use the shared five-field `Token` model (see below); on the Codex side `thinking` is populated from `reasoning_output_tokens` and there is no `cache_write`.

### Diagnosing opencode sessions (`--opencode`)

Pass `--opencode` to read **opencode** (sst/opencode CLI) sessions from `~/.local/share/opencode/opencode.db` — a single SQLite database, not JSONL. Every verb works the same; only the source and selector semantics change:

- opencode stores **all** sessions, sub-agents, messages, and message-parts in one SQLite DB, keyed by `session.id` (shaped `ses_<…>`). `ccq` opens it read-only (WAL-safe — reads don't block the live opencode writing).
- **Selector** is one of: a working directory (matched against each session's `directory`), a `session.id` prefix (e.g. `ses_0bb1e5`), or `latest[:<dir-or-keyword>]`. Sub-agent sessions (`session.parent_id` set) are excluded from the top-level `sessions`/`locate` listing — reach them via the parent's `agents` verb.
- **Sub-agents** are independent session rows linked to the parent via `session.parent_id`. The parent's `task` tool call carries the child `session.id` in `state.metadata.sessionId`; ccq reconstructs the call tree (each sub-agent labeled with its `subagent_type` + the parent `task` callID).
- **Skill triggers**: opencode **auto-injects** a skill's full `SKILL.md` into the user message text-part when the user's request matches the skill's trigger description — the user does **not** type a `/slash` or paste the content. (The agent can also invoke the `skill` tool directly, `input.name`.) The injected content ends with a two-line footer:
  ```
  Base directory for this skill: <abs path to skill dir>
  Relative paths in this skill (e.g., scripts/, references/) are relative to this base directory.
  ```
  ccq uses this footer as the primary split marker: everything after it is the user's genuine prompt; the skill name is taken from the footer path's last segment (`…\skills\req-optimize` → `req-optimize`). If the user's prompt after the footer starts with a `/command` (explicit `/slash` invocation), that command name wins as the trigger. Skills without the footer fall back to a `/command`-line heuristic. This keeps session digests and `skill-report ① 意图` showing the genuine request (e.g. `specgen 查询当前状态`, not `# req-optimize …`) while still emitting the `SKILL` event.
- **Tool names** are lowercase in opencode (`read`/`write`/`edit`/`bash`/`task`/`skill`/`apply_patch`/…); ccq normalizes them to Claude's conventions (`Read`/`Write`/`Edit`/`Bash`/`Task`/`Skill`) so the `files` (R/A/M), `agents` (sub-agent tree), and `skills` classification logic works unchanged. `apply_patch` input (`patchText`) and camelCase file fields (`filePath`/`oldString`/`newString`) are normalized too.
- **Tokens** use the shared five-field `Token` model; opencode's per-message `tokens.{input,output,reasoning,cache.read,cache.write}` maps directly (`reasoning` → `thinking`). `cache_write` is reported by opencode (unlike Codex).

```bash
ccq --opencode --check                          # db + top-level session count
ccq --opencode sessions C:/w/HomeTrans-CJ       # all opencode sessions run in that dir + count
ccq --opencode ses_0bb1e5 overview              # one session by id-prefix
ccq --opencode latest errors                    # most recent session's errors
ccq --opencode ses_0bb784 agents                # sub-agent tree (child session ids)
ccq --opencode -L ses_0bb864 files              # R/A/M + line churn per file
ccq --opencode ses_0bb1e5 skill-report cc-query # was the cc-query skill effective?
ccq --opencode ses_0bb1e5 "kind=tool name=Bash is_error=1 | show"
```

Mapping into ccq's model: human/agent text comes from `part` (`type=text`) under `message` (`role` user/assistant); thinking from `part` (`type=reasoning`); each `part` (`type=tool`) is split into a TOOL event (call, `state.input`) and a RESULT event (`state.output`/`state.error`, `is_error = state.status=="error"`), linked by `callID` — mirroring Claude's `tool_use`/`tool_result` dual-event shape so `errors`/`files`/DSL reuse the same logic. Tokens are attributed message-level (to the first event of each assistant message). Auto-injected skill content is split off from the user's genuine prompt via the footer marker (above); the co-located HUMAN (same part `uuid` as the SKILL trigger) is treated as the trigger's intent, not a segment boundary or interjection.

## The Zoom-out → Zoom-in Flow

Always start wide, then narrow. Don't skip to `grep` or `show` without first seeing the overview — you'll waste turns.

### Step 1 — Overview (one screen)

```bash
ccq d74d overview
```

Shows: time window, turn count, tool histogram, error count, sub-agent breakdown, token cost (five-field `Token` model, below), first/last human and agent messages, top errors.

**Token model:** every token figure ccq prints (overview, `agents`, `skill-report ④ 成本`, `json` verb) uses one `Token {input, output, cache_read, cache_write, thinking}` struct. Fields are mutually exclusive; a field that is `0` is omitted from the display (so a Claude session never shows `thinking`, which it doesn't report; a Codex session never shows `cache_write`; an opencode session can show all five, since it reports both `reasoning`→`thinking` and `cache.write`). The old scalar `tokens_in`/`tokens_out` (= `input+cache_write` / `output+thinking`) survive as backward-compat properties on `Event`.

### Step 2 — What's interesting?

Based on overview, pick one:

| Signal | Drill-in |
|---|---|
| Many errors | `ccq d74d errors` |
| Many tool calls by name | `ccq d74d "name=Bash \| tools"` |
| A specific skill was used | `ccq d74d skill-report <skill-name>` |
| Many sub-agents spawned | `ccq d74d agents` |
| What did the human say? | `ccq d74d human` |
| Which files were touched? | `ccq d74d files` |
| Suspicious tool input | `ccq d74d show <seq>` |
| Keyword hunt / count | `ccq d74d grep <regex> [in\|io]` |

### Step 3 — DSL for targeted queries

The DSL is: `"<predicates...> | <verb>"`. Predicates are AND'd. Verb defaults to `timeline`.

**Predicates:**
- `kind=` — human / agent / thinking / tool / result / skill
- `name=` — tool name (e.g., `Bash`, `Agent`, `Skill`)
- `is_error=1` — only failed tool results
- `sidechain=1` — limit to sub-agent events
- `agent=<type>` — specific sub-agent type (Explore, Plan, etc.)
- `after=` / `before=` — time range (like `after=10:00` or `before=12:30`)
- `skill=<name>` — confine to one skill's execution segment
- `text~<regex>` — content regex match

**Verbs:** `count`, `timeline`, `text`, `show` (full I/O), `tools`, `json`

**Examples:**
```bash
# Every failed Bash call with full input/output
ccq d74d "kind=tool name=Bash is_error=1 | show"

# Sub-agent tool calls from Explore agents
ccq d74d "sidechain=1 agent=Explore | tools"

# What was human doing during the hmos skill?
ccq d74d "skill=hmos-convert kind=human | timeline"

# Count of error results in the build-fixer skill
ccq d74d "skill=hmos-fix-build is_error=1 | count"

# All events in time window with full text (no truncation)
ccq -v d74d "after=10:30 before=10:45 | text"
```

### grep — regex count + per-action distribution

`ccq <sel> grep <regex> [in|io]` is the "common count" query: how many times a regex
occurs, in how many events, and what action each hit is. Each matching event prints as
`[time] #seq <action>  <summary>  «×n»` where `<action>` is the action label
(用户输入 / agent回复 / 工具:<name> / 工具结果 / …), `<summary>` is that event's
`Event.summary()` (minimal impl: first ~100 chars of the tool-call args or text;
`-v` shows full), and `«×n»` = hits in that event. The footer gives total occurrences,
matched-event count, and an action histogram.

- **scope** (3rd positional, default `io`):
  - `io` / `all` — search both **人机所写** (HUMAN, AGENT, THINKING, TOOL call+input, SKILL) and **环境返回** (RESULT, i.e. tool-result text).
  - `in` / `input` — search **人机所写** only (excludes tool-result text). Occurrence count is always ≤ `io`.
- Unit is the **event/action**, not the raw JSONL line — one assistant turn can be several events. So counts differ from `grep -oi` on the raw file (also: RESULT text is truncated to ~4000 chars). Use this for "what kind of action mentions X", not byte-exact line counts.

```bash
# Where does "blocker" show up, across everything?
ccq --codex 019f12d9 grep blocker          # io: includes tool-result output
# Only in what human/agent authored (prompts, agent text, tool calls)
ccq --codex 019f12d9 grep blocker in
```

## files — What files did the session read / create / modify?

`ccq <sel> files` classifies every file the session touched (via the structured file
tools) into three buckets, so you can answer "what did this agent actually change?" at a
glance:

- **R 只读** — file was only `Read`, never mutated
- **A 创建** — file was newly created this session (first mutating op is a `Write` with no
  prior `Read` in scope; on the Codex side, an `apply_patch` `*** Add File`)
- **M 修改** — an existing file was changed this session (an `Edit`/`NotebookEdit`, a
  `Write` that was preceded by a `Read`, or a Codex `*** Update File`)

Each path shows `×n` when it was operated on more than once (a re-read or repeated edit —
a useful friction signal). A `D 删除` bucket appears only when a Codex `*** Delete File`
is present.

### Line-change counts (`-L` / `--lines`)

Add `-L` to also report per-file churn as `+added/−removed` (and a scope total in the
header). This runs a `difflib` pass per edit, so it's **off by default** — turn it on when
you actually want the magnitude of changes, not just which files changed.

```bash
ccq d74d files          # which files, R/A/M (fast)
ccq -L d74d files       # …plus +added/−removed line churn per file
```

Churn is only counted for the cases where the log carries enough to compute it exactly:
`Edit`/`MultiEdit` (diff of `old_string`↔`new_string`), a `Write` that **creates** a new
file (all lines added), and Codex `apply_patch` `Add`/`Update` (the patch is already a
diff). A `Write` that **overwrites** an existing file has no pre-image in the log, so its
churn is deliberately left blank rather than guessed — that M file shows `×n` with no
`+/−`. "Churn" counts both sides of a replace (a 3-line reformat reads as `+3/−3`), so it
measures edit magnitude, not net file growth.

**Sub-agents are counted in a separate scope, not merged into the main agent's numbers.**
The main agent's R/A/M is printed first, then each sub-agent gets its own R/A/M block
(labeled by agent type + description), followed by a sub-agent grand total. This matters
because in a spawn-heavy session most reads happen inside Explore sub-agents while the
writes happen in the main agent — merging them would hide who did what.

```bash
ccq d74d files            # main-agent R/A/M, then per-sub-agent R/A/M, then sub totals
ccq d74d files --json     # structured: {main:{R,A,M}, subagents:[{agent_type, files:{…}}]}
ccq --codex 019ef3 files  # Codex: A/M/D derived from apply_patch headers (reads via shell are opaque)
```

**Scope note:** only the structured file tools are tracked (`Read`, `Write`, `Edit`,
`NotebookEdit`; Codex `apply_patch`). File I/O buried in `Bash`/shell commands — `cat`,
`>` redirects, `sed -i` — is opaque and deliberately excluded, so treat this as a
lower-bound on file activity, not an exhaustive filesystem audit.

## Skill-report: The Key Assessment Tool

`ccq <sel> skill-report [<skill-name>]` produces a structured report:

```
① 触发 → when, preceding human intent
② 执行 → event/tool counts, sub-agent spawns with per-agent digest
③ 摩擦 → error count, human interruptions/corrections, re-reads
④ 成本 → five-field Token breakdown (includes sub-agent token cost)
⑤ 结果 → why it ended (next skill / new human instruction / session end)
```

Skill triggers are detected from **both** human-typed `/slash` commands AND agent `Skill` tool invocations. If a skill name is given, only reports matching segments.

**Evaluating a skill:** look for friction signals — high error count, human interruptions mid-segment, agents re-reading the same files repeatedly — these indicate the skill is struggling. A clean skill segment has zero errors, zero interjections, and clear sub-agent delegation.

### skill-report is segment-scoped — cross-check `files`/`agents` for actual output

`skill-report` is bounded by the skill **trigger**: it starts at the `/slash` or auto-injected SKILL event and ends at the next genuine human instruction (⑤ 结果 = "人类下达新指令" / "next skill"). This isolates one skill invocation for assessment — but it has a sharp edge: **work done in the continuation, after the user's follow-up instruction, is NOT in the skill-report**, even when that work is logically part of the same campaign.

The trap bites hardest on "closed-loop" skills (req-optimize specgen, hmos-spec-generate, spec-audit-plus-repair) that run in two phases:
1. A short trigger phase *inside* the segment — often just a status query or a plan (skill-report shows ~1m, few tools, 0 sub-agents).
2. A production phase *after* the user says "开始" / "继续" / "go" — the agent then spawns spec-gen / audit / repair sub-agents that write the real output files. These sub-agents are NOT attributed to the skill segment, because they were spawned by the follow-up instruction, not the trigger.

Judging "did this skill produce output?" from skill-report alone yields a **false negative**: you see a 1-minute status query and conclude "nothing was produced", when the session actually went on to write dozens of SPEC files via sub-agents in the continuation. (This exact misread happened twice in one session before being caught by `files`.)

The fix: to answer "did this skill/session actually produce files?", run `files` — it scans the **whole session + every sub-agent** for `Write`/create evidence and is not segment-bounded, so it catches output written in the continuation that skill-report misses. `agents` likewise lists sub-agents spawned across the entire session. If `files` shows created output but skill-report shows a tiny segment, that's the tell: production happened post-trigger, driven by a follow-up human instruction.

```bash
ccq <sel> skill-report <skill>   # segment-scoped: trigger phase only
ccq <sel> files                  # session-scoped: ALL writes incl. continuation + sub-agents
ccq <sel> agents                 # sub-agents spawned across the WHOLE session, not just the segment
```

A corollary: when skill-report's ⑤ 结果 reads "人类下达新指令", do not assume the campaign ended — it means the *skill segment* ended. Read `agents` / `files` to see what the follow-up instruction actually drove.

## Sub-Agents and Their Logs

Sub-agents (spawned via `Agent` or `Task` tools) have **separate log files** under `<sid>/subagents/agent-<hash>.jsonl`, with `<hash>.meta.json` recording `{agentType, description, toolUseId}`.

- `ccq d74d agents` — per-sub-agent digest: spawn time, tools, errors, tokens, final output, log file path
- `ccq d74d "sidechain=1 | timeline"` — all sub-agent events across all sub-agents
- `ccq d74d "agent=Explore | tools"` — tool calls from Explore-type sub-agents only
- To drill into a single sub-agent's full log: read the specific `.jsonl` file path shown by `agents`

## Using ccq from Python (escape hatch)

When the CLI verbs don't fit, import the parsing engine directly:

```python
from scripts.ccq_core import load_session, locate, parse_events, Event, Kind, Token

sf, events = load_session("d74d")         # main + all sub-agents
# or
sf = locate("d74d")                        # just locate, no parse
ev = parse_events(sf.main)                 # main only

failed_bash = [e for e in events if e.kind==Kind.TOOL and e.tool_name=="Bash"
               and e.is_error]
```

The module is at `C:\Dev\usercmd\scripts\ccq_core.py` (installed via `uv tool install -e .` in the usercmd project directory).

## Verb Flags

- `-v`/`--verbose` — don't truncate output; see full tool inputs and text
- `-q`/`--quiet` — reduced output (only key event kinds)
- `-L`/`--lines` — `files`: also count `+added/−removed` line churn (costs a difflib pass per edit; off by default)
- `--json` — structured JSON output (works with `locate` and `files`)

## Common Diagnostic Patterns

### "Why did that build fail?"
```bash
ccq <sel> overview        # check error count and top errors
ccq <sel> errors          # each error with its cause tool + input
ccq <sel> "is_error=1 name=Bash | show"  # full error details
```

### "Was the skill effective?"
```bash
ccq <sel> skill-report <skill-name>
# Read ③ 摩擦: errors, human corrections, re-reads
# Read ⑤ 结果: did it complete or get interrupted?
# ⚠ If ⑤ = "人类下达新指令", the skill SEGMENT ended but the session may have
#   kept producing via sub-agents — skill-report won't show that work.
ccq <sel> files     # cross-check: actual files written (session-wide, not segment-scoped)
ccq <sel> agents    # sub-agents spawned across the whole session
```

### "What did that agent actually do?"
```bash
ccq <sel> overview        # tool histogram + summary
ccq <sel> agents          # sub-agent breakdown
ccq <sel> "kind=human | timeline"  # human side only
```

### "Find when something broke"
```bash
ccq <sel> timeline | head -200   # scan for ✗ERROR markers
ccq <sel> "is_error=1 | tools"   # which tools failed
```
