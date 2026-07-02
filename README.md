# ccq

> 分层查询另一个 Claude Code / Codex session 的日志,诊断 agent 干了什么、
> 遇到什么错误,并评估某个 skill 的使用效果。纯 Python 标准库,无需 jq。

## 安装 (Installation)

```bash
# 从 GitHub 直接安装(推荐)——获得全局命令 `ccq`
uv tool install git+https://github.com/Super3001/ccq.git

# 或:克隆后本地可编辑安装(改动即时生效,适合二次开发)
git clone https://github.com/Super3001/ccq.git
cd ccq
uv tool install --editable .
```

升级:`uv tool upgrade ccq`

## 卸载 (Uninstall)

```bash
uv tool uninstall ccq
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
