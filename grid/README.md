# 本机 A2A 多 Agent 通信底座

四个本地 AI agent（pi / claude / codex / dsh）通过 **A2A 协议**（Google 开源，Agent2Agent）互联互通。

## 运行形态与部署（2026-09-05 收敛，必读）

- **运行环境是 WSL**：四个 server 由 systemd 单元 `a2a-{pi,claude,codex,dsh}` 在 WSL Ubuntu 里以 root 运行（`Restart=always` 崩溃自愈、开机自启），监听 127.0.0.1:10000-10003，Windows 经 mirrored 网络直接访问。**Windows 侧 netstat 看不到这些监听是正常形态，不是故障。**
- **源码真源是 a2a-workbench 仓库的 `grid/` 目录**（`D:\Dev\a2a\a2a-workbench\grid`，2026-09-05 起；此前的 `C:\Users\zhen.yuan\a2a-agents` 已归档）；WSL 的 `/root/a2a-agents` 是部署产物，**不要直接改**。
- **唯一部署通道**：改代码 → `git commit` → `wsl -u root bash /mnt/d/Dev/a2a/a2a-workbench/deploy-to-wsl.sh`。脚本会 rsync 代码、写 VERSION 版本戳、重启 systemd 单元，并验收四张 Agent Card 的 `version` 字段等于本次戳。
- **验证在跑哪个版本**：`curl -s http://127.0.0.1:10001/.well-known/agent-card.json | python -c "import json,sys;print(json.load(sys.stdin)['version'])"`——输出形如 `abc1234-20260905-183000`；`dev` 表示没走部署通道。
- 历史教训：2026-08 迁 WSL 后靠手工拷贝，两周内所有修复只落在 Windows 份、活环境静默停在旧版且健康检查全绿。Windows watchdog（watchdog.ps1/agent_loop.ps1/A2AAgents.vbs）已于 2026-09-05 退役，避免双 owner 竞态。

## Agent 目录

| agent | 端口 | 状态 |
|---|---|---|
| pi | 10000 | 常驻 |
| claude | 10001 | 常驻 |
| codex | 10002 | 常驻 |
| dsh | 10003 | 常驻 |

## 怎么调用其他 agent

```bash
python ~/a2a-agents/venv/Scripts/python.exe ~/a2a-agents/a2a_call.py <agent名> "消息" [--stream] [--model <id>] [--provider <name>] [--cwd <绝对目录>]
```

例：
```bash
python ~/a2a-agents/venv/Scripts/python.exe ~/a2a-agents/a2a_call.py pi "帮我分析需求"
python ~/a2a-agents/venv/Scripts/python.exe ~/a2a-agents/a2a_call.py claude "走查仓库"
python ~/a2a-agents/venv/Scripts/python.exe ~/a2a-agents/a2a_call.py codex "写测试"
# 指定模型调 pi
python ~/a2a-agents/venv/Scripts/python.exe ~/a2a-agents/a2a_call.py pi "分析需求" --model "deepseek-v4-flash (self hosted)" --provider habi
```

**任何 agent（或你自己）都能用这条命令调任何其他 agent**——这是互通的统一入口。

### 按次指定模型（pi 多模型）

pi.cmd 本身多模型（--model / --provider / --models），A2A 调用时可通过
`SendMessageRequest.metadata` 按次指定，执行器白名单校验后拼成
`pi.cmd --print --model <id> --provider <name> ...`，不回落到命令行注入。
只有 **pi** 支持模型透传；claude/codex/dsh 忽略该元数据，行为不变。

```bash
# 示例：@ hbi 自部署 DeepSeek
python ~/a2a-agents/venv/Scripts/python.exe ~/a2a-agents/a2a_call.py pi \
    "分析这段代码" --model "deepseek-v4-flash (self hosted)" --provider habi
```

模型值校验：仅允许字母/数字/下划线/点/斜杠/冒号/空格/括号/连字符+中文，
拒绝换行/控制符/以 `-` 开头/含 `--` 的值，防止 A2A 元数据注入任意 pi 命令行选项。

`--cwd` 把执行钉到指定工作目录（如某个 git worktree），非法或不存在的目录会被拒绝并落 FAILED。
客户端在任务 FAILED/CANCELED 时退出码非零，编排方可用 `$?` 分辨成败。

`--context <id>` 启用会话连续性（目前仅 claude）：同 context 的调用延续同一原生会话，
执行手记得此前的工作；同 context 并发到达会被串行。`--fresh-context` 换一个全新原生会话。
注意：新会话只保证原生对话历史归零，CLI 自身的跨会话记忆（如 Claude Code auto-memory）
不受影响——context 是会话路由键，不是隔离边界。

## 圆桌协作

```bash
python ~/a2a-agents/venv/Scripts/python.exe ~/a2a-agents/roundtable.py "审核并修复项目问题"
python ~/a2a-agents/venv/Scripts/python.exe ~/a2a-agents/roundtable.py "只读评审" --moderator claude --agents pi codex dsh
```

主持人先生成带依赖关系的分工，成员并行或按依赖执行，随后交叉复核并由主持人汇总。相同 `write_scope` 的任务必须建立依赖串行，防止多个 Agent 同时修改同一范围。每次圆桌的计划、结果和复核记录保存在 `data/roundtables/<id>.json`；不需要交叉复核时可加 `--no-peer-review`。

## 能力

- 双向互调（pi⇄claude⇄codex 全实测通过）
- 崩溃自愈 + 开机自启（WSL systemd 单元 `a2a-*`，Restart=always）
- 任务持久化（data/<agent>-tasks.db，SQLite，重启不丢；启动时对账把中断遗留的非终态任务标 FAILED）
- 输入净化（限长 + 去控制字符 + 正确转义；POSIX 上 killpg 整树终止超时进程）
- 流式声明（--stream）
- 圆桌协作（主持分工、依赖调度、写冲突约束、交叉复核、最终收口）

## 怎么加新 agent

1. 写一个 executor 子类（继承 `subprocess_executor.py` 的 `SubprocessAgentExecutor`，声明双平台 BIN，约 10 行）
2. 写一个 server.py（起 A2A 服务，分配新端口）
3. 在 `agents.json` 和 `deploy-to-wsl.sh` 的 AGENTS/SERVER_SCRIPT 各加一行
4. `git commit` 后跑 `deploy-to-wsl.sh`

## 稳定检查

```bash
curl http://127.0.0.1:10000/.well-known/agent-card.json  # 四个端口都应 200
wsl -u root systemctl status a2a-pi a2a-claude a2a-codex a2a-dsh
```

服务日志在 WSL `/var/log/a2a-*.log`，启动链日志在 `/var/log/wsl-service-boot.log`。
