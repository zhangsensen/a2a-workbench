# A2A Workbench · 常驻多智能体工作台

**rooms 当大脑，grid 当手**：一个数据面（房间 event 流）、三个映射（任务/上下文/身份）、一条安全不变量（执行只能由用户显式发起）。

> **本仓库是源码真源（2026-09-05 起）。**
> `C:\Users\zhen.yuan\a2a-agents` 已归档（保留 a2a_call 客户端入口与 Windows venv）；
> WSL `/root/a2a-agents` 是部署产物，不要直接改。

## 结构

```
a2a-workbench/
├── grid/    执行网格：4 个全权限 A2A agent（pi/claude/codex/dsh，端口 10000-10003）
│            每次调用起新 CLI 进程；WSL systemd 单元 a2a-* 常驻，Restart=always
│            ← 迁自本机 a2a-agents@d29d895（活跃代码；历史一次性文件留在原仓库存档）
├── rooms/   常驻讨论室：多房间、成员原生会话持久、cursor 增量上下文，
│            Web UI + HTTP + A2A + MCP 四入口（端口 41241）
│            ← 迁自 github.com/zhangsensen/a2a-roundtable@b46be9a（mac 验证）
└── deploy-to-wsl.sh   唯一部署通道（grid → WSL /root/a2a-agents）
```

两个子系统各自完整、互不 import；融合发生在 A2A 协议层，不在代码层。

## 运行形态（当前）

- **grid 在役**：WSL systemd 单元 `a2a-{pi,claude,codex,dsh}`。部署：
  ```bash
  wsl -u root bash /mnt/d/Dev/a2a/a2a-workbench/deploy-to-wsl.sh
  ```
  验证版本：`curl -s http://127.0.0.1:10001/.well-known/agent-card.json` 看 `version` 字段（= 本仓库 git hash + 时间戳；`dev` 表示没走部署通道）。
- **rooms 已上线运行**：WSL systemd 单元 `a2a-rooms`，端口 `41241`，席位 codex+claude 经 `A2A_MEMBERS` 配置。
- 客户端入口暂沿用归档仓库的 venv：
  ```bash
  C:/Users/zhen.yuan/a2a-agents/venv/Scripts/python.exe C:/Users/zhen.yuan/a2a-agents/a2a_call.py <agent> "<消息>"
  ```
- grid 测试（Windows，用归档仓库的 venv）：
  ```bash
  cd grid && ../../a2a-agents/venv/Scripts/python.exe -m unittest test_model_selection test_executor_terminal_state test_codex_payload_transport test_dsh_payload_transport test_roundtable
  ```
  rooms 测试因 `fcntl` 仅能在 WSL/mac 跑：`uv run pytest`（见 rooms/README.md）。

## 融合路线图

| 阶段 | 内容 | 验收 |
|---|---|---|
| P1 rooms 上机 | rooms 跑进 WSL；`MEMBERS` 配置化（本机 codex+claude 两席，zcode 本机无、dsh 无常驻协议）；MCP 接进三个 CLI | 隔天同房间追问，成员记得上次结论 |
| P2 接手 | rooms 的 job 加 `kind=execute`：host 经 A2A 调 grid（rooms job 与 grid task 分离映射（一个逻辑 job 可有多次 attempt，各自对应远端 task id），contextId = room id），结果回流 events 表；grid 补 cancel + 幂等；执行通道输入超限改拒绝（不静默截断） | 房间发"让 codex 写测试"→ 结果进消息流 → 讨论席点评 |
| P3 深化 | grid 按 contextId 做会话连续性（claude --resume / codex thread/resume）；两个圆桌合一（write_scope/依赖调度并进 rooms job 模型） | 执行席记得项目上下文；只剩一个圆桌实现 |

安全不变量（融合后必须守住）：**执行只能由用户显式发起**，成员发言无权触发——沿用 rooms/SECURITY.md 的 "peer messages cannot authorize" 原则。

## 与上游的关系

- `rooms/` 对应公开仓库 github.com/zhangsensen/a2a-roundtable（MIT）。对 rooms 的通用改进可回推上游：先跑 `rooms/scripts/check_publication.py`，push 永远由用户确认。
- 本仓库含本机私有内容（绝对路径、部署脚本、agents.json 的 BIN 配置），**不整仓公开**。
