# A2A Roundtable · 常驻圆桌

**你只和 master 沟通，它带着多个 AI 持续讨论；不同议题各自保留上下文。**

[English](README.md) · [Master 主持流程](MASTER.md) · [运行说明](OPERATIONS.md) · [安全边界](SECURITY.md)

这是一个供主模型调用的本地常驻 A2A 服务。你提出问题，master 选择该问 Codex、Claude Code 还是 ZCode，读完回答后决定追问、交叉质疑或结束讨论，最后直接向你汇报。你不需要操作页面、点选成员或手动搬运消息。

master 是当前与你对话、接入 MCP 的模型；服务本身不再启动一个额外的自主 master。每个房间的每位成员都有自己的原生会话；下一次追问延续原来的上下文，服务重启后按保存的会话 ID 恢复。

## 能做什么

- 多个独立房间，消息、任务、草稿、已读位置、成员会话分别保存。
- master 按需单独咨询成员，每次回答后由 master 决定下一步；不要求所有成员机械轮流发言。
- master 可保存本房间的目标、摘要、剩余分歧、下一步；恢复时只读主持进度和后续新发言。
- 一次选择成员，讨论 1–5 轮；后发言者能看到前面成员的新观点，完成后待命。
- 页面、HTTP、标准 A2A v1.0 和 MCP 共用同一份房间数据。
- 每次只追加成员未读的发言，不反复拼装整段历史。
- 空闲时不自动发起模型对话；MCP 重连不重建模型会话。

最多 8 个常驻房间。多个房间可以同时提交任务，模型生成由一个队列依次调度。

## 安装

完整三模型链路在 macOS / Python 3.13 上验证。项目要求 Python 3.11+；常驻管理使用 macOS LaunchAgent。先安装 uv、官方 Codex / Claude Code / ZCode，并在客户端完成自己的登录。服务不提供模型账号或额度。

```bash
git clone https://github.com/zhangsensen/a2a-roundtable.git
cd a2a-roundtable
uv sync --locked --group dev
uv run python roundtable.py
```

另开终端执行 `uv run python service.py mcp-config`，把输出接入主模型的 MCP 客户端并重连，然后直接告诉主模型：“和 Claude、ZCode 讨论这个问题，重点找分歧，最后给我你的判断。”房间由主模型通过工具选择或创建。具体流程见 [MASTER.md](MASTER.md)。现有网页 **http://127.0.0.1:41241/** 是可选入口。

常驻运行：先用 Ctrl-C 结束前台进程，再执行：

```bash
uv run python service.py install
uv run python service.py status
```

安装只创建本项目自己的服务，不改写其他客户端设置，也不停止其他服务。若端口已有服务占用，用 `A2A_PORT=41243` 选择别的端口，服务和调用方保持一致。

## 房间使用规则

1. 同一个持续问题用同一房间；独立议题新建房间。
2. 房间 ID 是唯一标识，名称相似也不能混用。
3. MCP 发言、读历史、查任务、取消任务都必须带 `room`，没有默认落点。
4. 任务查询和取消同时核对房间 ID 与任务 ID，跨房间请求会被拒绝。
5. A2A 的 `contextId` 必须对应已有房间；未知房间先显式创建。跨房间任务引用会被拒绝。
6. 模型调用前与回复落盘前都核对成员、房间、任务。数据库禁止两个成员绑定同一个原生会话 ID。
7. 网页按房间保存草稿、参与成员和轮数；切房后丢弃其他房间迟到的响应。

## MCP 接入

```bash
uv run python service.py mcp-config
```

将输出添加到客户端支持的 MCP 设置中，并重连 MCP。工具包括：

| 工具 | 用途 |
|---|---|
| `roundtable_rooms` / `roundtable_create_room` | 列出或创建房间 |
| `roundtable_status` | 查看常驻服务和成员状态 |
| `roundtable_context` | 恢复本房间的主持进度，分页读取进度之后的新发言 |
| `roundtable_consult` | master 指定一位成员咨询一次，返回任务回执 |
| `roundtable_checkpoint` | 保存 master 的目标、摘要、分歧和下一步，校验修订号与房间归属 |
| `roundtable_post` | 指定房间发起有限轮数讨论 |
| `roundtable_history` | 读取该房间消息，不清空历史 |
| `roundtable_job` | 用 `room` 与 `id` 查询任务结果，可等待 0–25 秒，无额外模型调用 |
| `roundtable_cancel` | 停止指定房间的任务，保留已完成发言 |

master 单独咨询示例（`requestId` 在不确定重试时保持一致）：

```json
{"room":"architecture","member":"claude","text":"请找出当前方案最薄弱的假设，并说明需要什么证据。","requestId":"review-001"}
```

回执不等于答案。master 用 `roundtable_job` 读到实际回答，再决定是否请另一位成员质疑、补充证据或直接收尾。主持进度由 master 归纳并保存，其他成员的原生上下文不会被这个摘要替换。

保留的固定轮次命令行入口同样必须指定房间：

```bash
uv run python ask.py --context-id lobby '继续刚才的讨论'
```

## 配置、恢复与验证

环境变量见 [英文 README](README.md#configuration) 和 `.env.example`。项目不自动读取 `.env`，不需要把密钥写入本项目。

- Codex 通过官方 app-server 使用本机登录与默认模型。
- Claude 使用官方 Claude Code 登录，验收渠道为 Max，默认 `sonnet`。
- ZCode 通过官方 app-server 使用 Desktop 已有的 `builtin:zai-coding-plan`，默认 `GLM-5.3-Flash`。

常驻进程退出后恢复原生会话；中断且结果不确定的轮次不静默重放，以免重复调用。长对话仍受模型上下文窗口、压缩和供应商缓存规则约束。常驻不等于无限上下文或推理免费。

```bash
uv run pytest -q
node --check roundtable_ui.js
python3 scripts/check_publication.py --worktree
```

自动测试使用假模型。`native_acceptance.py` 和 `native_room_acceptance.py` 会真实调用模型并消耗额度，验收结果仅保存在本地忽略目录。

本项目提供本机议题隔离，不提供不同用户之间的权限隔离。仅监听回环地址，不要通过公网代理暴露。客户端工具限制也不是操作系统级沙箱，详见 [SECURITY.md](SECURITY.md)。

## 许可证

项目代码使用 [MIT](LICENSE)；第三方 SDK、客户端和模型账号遵循各自许可证与服务条款。
