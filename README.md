# cc-query

> 分层查询另一个 Claude Code / Codex session 的日志,诊断 agent 干了什么、
> 遇到什么错误,并评估某个 skill 的使用效果。纯 Python 标准库,无需 jq。
>
> 本仓包含两部分:命令行工具 `ccq`,以及配套的 Claude Code skill `cc-query`
> (让 Claude 在你问「那个 session 怎么了」时自动调 `ccq`)。

## 1. 安装命令行工具 `ccq` (Install the CLI)

```bash
# 从 GitHub 直接安装(推荐)——获得全局命令 `ccq`
uv tool install git+https://github.com/Super3001/ccq.git

# 或:克隆后本地可编辑安装(改动即时生效,适合二次开发)
git clone https://github.com/Super3001/ccq.git
cd ccq
uv tool install --editable .
```

升级:`uv tool upgrade ccq`

## 2. 安装 Skill `cc-query` (Install the skill — optional but recommended)

本仓在 `.claude/skills/cc-query` 里带了一个 Claude Code skill。装上后,你在 Claude Code
里直接问「d74d 那个 session 为什么构建失败」「昨晚那个 agent 干了啥」之类,Claude 会自动
触发该 skill 并替你跑 `ccq`,无需手敲命令。**skill 依赖已安装的 `ccq` 命令(见上一步)。**

把 skill 目录拷进你的用户级 skills 目录即可:

```bash
# macOS / Linux
cp -r .claude/skills/cc-query ~/.claude/skills/

# Windows (PowerShell)
Copy-Item -Recurse .claude/skills/cc-query $HOME\.claude\skills\
```

用法:装好后无需额外配置,在 Claude Code 中用自然语言问某个 session 的情况即可触发;
skill 内部就是调用下面这些 `ccq` 子命令。

也可以手动触发，例如在 claude code/codex/... 当中输入：

```prompt
/cc-query locate <sessionId?
/cc-query 请分析 <sessionId? 的 ...

# 如果分析的是codex session日志，建议添加"--codex"
/cc-query --codex 请分析 <sessionId? 的 ...
```

## 卸载 (Uninstall)

```bash
uv tool uninstall ccq                 # 卸载命令行工具
rm -rf ~/.claude/skills/cc-query      # 移除 skill(Windows: Remove-Item -Recurse $HOME\.claude\skills\cc-query)
```

---

## ccq —— Claude Code session 日志查询

分层查询另一个 Claude Code session 的日志(人类说了什么、agent 做了什么、遇到什么错误),
并评估「agent 使用某 skill 的效果」。环境无需 jq。

--codex for Codex session

```bash
ccq locate <selector>          # 定位:返回该 session 全部 log 路径(主 + 子 agent)
ccq <selector|path> overview   # 一屏看懂:skill、意图、工具直方图、错误、子agent、token、结局
ccq <selector|path> timeline   # 紧凑事件流
ccq <selector|path> human|tools|errors|agents|skills
ccq <selector|path> show <seq> # 钻取单事件完整 I/O
ccq <selector|path> grep <re>
ccq <selector|path> skill-report [<skill>]   # skill 效果评估报告(触发/执行/摩擦/成本/结果)
ccq <selector|path> "<query DSL>"            # 查询语言
```

**skill 触发来源**:同时识别两种——人类手敲 `/slash` 命令,与 agent 自发调用
`Skill` 工具(`input.skill`)。两者统一为 `kind=skill` 事件,`skills` / `skill-report` 都能捕获。

**子 agent(Agent/Task 调用)**:子 agent 日志是 `<sid>/subagents/agent-*.jsonl` 独立文件。
`locate` 会列出全部子 agent 路径;`agents` 给每个子 agent 的摘要(spawn 点/工具直方图/错误/token/末态/日志路径);
`skill-report` 会把 skill 段内 spawn 的子 agent 摘要内联,并把子 agent 的 token 成本计入该 skill。
查询用 `sidechain=1` 或 `agent=<类型>` 限定到子 agent。

selector:partial-id(`d74d`)、`latest`、`latest:<proj关键字>`、或直接 `.jsonl` 路径。

查询 DSL:`"<谓词...> | <动词>"`,谓词空格分隔 AND。
- 谓词:`kind=`(human/agent/thinking/tool/result/skill/error)、`name=`、`is_error=`、
  `sidechain=`、`agent=`、`after=`/`before=`、`skill=`、`text~<regex>`
- 动词:`count` `timeline` `text` `show` `tools` `json`(缺省 `timeline`)

```bash
ccq d74d "kind=tool name=Bash is_error=1 | show"   # 失败的 Bash 调用 + 完整 I/O
ccq d74d "skill=hmos-convert-pipeline | timeline"   # 某 skill 区间内全部事件
ccq d74d "sidechain=1 kind=tool | tools"            # 子 agent 干了哪些工具调用
```

底层逃生口(直接写脚本):`from ccq.ccq_core import load_session, locate, Event, Kind`。
`--check` 预检环境;`--validate <sel>` 校验某 session 解析完整性。
