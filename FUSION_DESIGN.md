# A2A Workbench：开放式多 Agent 并行开发

> 状态：产品与融合基线（2026-09-06）

## 1. 核心定位

A2A Workbench 的核心不是“长期圆桌”，也不是“本机四个 Agent 管理器”。

它要解决的是：

> **让多个彼此独立的 Agent 通过开放协议协同开发，在并行提升效率的同时，不覆盖彼此的代码和上下文。**

现有 subagent 模式通常是封闭树形结构：

```text
主 Agent
├─ 自己创建的子 Agent A
├─ 自己创建的子 Agent B
└─ 自己创建的子 Agent C
```

子 Agent 依附于父 Agent，通常不能被其他 Agent 发现、复用或继续协作；上下文和成果最终还要经过父 Agent 转述。

A2A Workbench 提供的是开放协作网络：

```text
Claude ─┐
Codex ──┼── A2A Workbench ── room / task graph / workspace / evidence
Pi ─────┤
Custom ─┘
```

每个 Agent 都是独立参与者：

- 可以被其他 Agent 发现和调用；
- 可以进入同一个项目房间；
- 可以读取自己需要的增量上下文；
- 可以认领或接收独立任务；
- 在隔离工作区中并行开发；
- 通过结果、diff、测试和依赖关系完成交接；
- 不要求成为某个主 Agent 私有的 subagent。

**产品价值优先级：**

1. 并行开发提效；
2. 写入隔离，不互相覆盖；
3. 开放互操作，不绑定单一父 Agent、模型厂商或客户端；
4. 协作过程可恢复、可追踪、可验证；
5. 讨论、执行和集成边界清晰。

## 2. 与传统 subagent 的本质区别

| 维度 | 传统 subagent | A2A Workbench |
|---|---|---|
| 拓扑 | 父子树 | 开放的 Agent 网络 |
| 所有权 | 子 Agent 属于创建它的父 Agent | Agent 独立运行，可被任何协作方发现和调用 |
| 发现 | 父 Agent 内部私有 | 通过 A2A Agent Card / registry 发现 |
| 上下文 | 父 Agent分发，结果向上汇总 | 项目房间和事件流共享，按 cursor 增量读取 |
| 并行写入 | 常共享同一工作树，容易覆盖 | 每个执行任务使用隔离 workspace/worktree |
| 协作 | 一次性委派 | 可讨论、交接、复核、继续执行 |
| 结果 | 主要是自然语言回复 | commit/diff/test/artifact/evidence |
| 生命周期 | 随父会话结束 | 独立任务和会话可跨进程恢复 |
| 协议 | Harness 私有机制 | A2A + MCP/HTTP/CLI 接入 |

Workbench 可以有 coordinator/master，但它只是一个可替换角色，不是整个系统的所有者。Agent 之间的互操作不能依赖某个 master 的私有 subagent API。

## 3. 目标工作流

一个典型开发任务：

```text
1. 用户创建项目任务
2. coordinator 将任务拆为可并行 DAG
3. Workbench 分配互不冲突的 workspace/worktree
4. Claude 修改后端，Codex 修改测试，另一个 Agent 做只读审查
5. Agent 通过 room events 交换接口决定、状态和证据
6. 调度器阻止冲突写入，按依赖解锁后续任务
7. verifier 运行测试、检查 diff 和验收条件
8. integrator 按确定顺序合并成果；冲突时回到责任 Agent 修正
9. Workbench 输出可追踪的最终报告
```

“互相开发”不等于多个 Agent 同时写同一个目录。正确模型是：

> **共享任务与事实，隔离写入空间，通过显式契约和集成步骤合并成果。**

## 4. 产品架构

```text
CLI / MCP / HTTP / A2A clients
              │
              ▼
┌───────────────────────────────────────────┐
│ Workbench Control Plane                   │
│                                           │
│ Agent Registry      Room Event Log        │
│ Task DAG            Workspace Manager     │
│ Scheduler           Integration Queue     │
│ Verification        Recovery / Audit      │
└───────────────┬───────────────────────────┘
                │ A2A / adapter protocol
      ┌─────────┼──────────┬──────────┐
      ▼         ▼          ▼          ▼
   Claude     Codex       Pi       Remote A2A
   worker     worker     worker       worker
      │         │          │          │
      └──── isolated workspaces/worktrees ────┘
```

### Control Plane 负责

- Agent 注册、能力发现和健康状态；
- 房间、事件和项目上下文；
- 任务 DAG、依赖、优先级和状态；
- workspace/worktree 创建、绑定、回收；
- 写入范围冲突检测；
- 任务派发、幂等、取消和恢复；
- 验收条件与证据；
- 合并顺序和集成状态。

### Worker 负责

- 接收一个边界明确的任务；
- 只在被分配的 workspace 中工作；
- 汇报结构化状态；
- 交付 commit/diff/artifact/test evidence；
- 响应取消；
- 不自行修改其他 Agent 的 workspace。

### Room 负责

Room 是协作日志和上下文通道，不是任务执行器本身。它记录：

- 用户目标；
- Agent 间接口约定；
- 任务认领和交接；
- 进展、阻塞、决策；
- 执行结果和验证证据；
- 未解决分歧。

## 5. 防止互相覆盖：产品的第一技术不变量

仅靠 prompt 中写“不要覆盖别人”不可靠，必须由系统保证。

### 5.1 默认隔离工作区

每个写任务绑定唯一 workspace：

```text
project
├─ base checkout
├─ worktree/task-backend-123
├─ worktree/task-tests-124
└─ worktree/task-review-125 (read-only or no-write)
```

任务必须记录：

```text
repository_id
base_revision
workspace_id
branch
write_scopes
owner_agent
```

所有执行进程的 `cwd` 必须由 Workbench 设置为该 workspace，不能使用 Agent server 自己的源码目录。

### 5.2 写入范围是调度提示，不是唯一保护

`write_scope` 用于尽早发现冲突，但真正隔离依靠 worktree/workspace。

Scope 使用 workspace-relative path/glob，并检测：

- 完全相同路径；
- 父子目录；
- glob 覆盖；
- repo-wide write；
- 生成文件和共享 lockfile 等隐式冲突资源。

不同 worktree 可以并行写；进入集成阶段后按依赖顺序合并。共享数据库、端口或部署环境等非文件资源另建 resource locks。

### 5.3 单一集成者

并行实现完成后，不允许所有 Agent 同时向目标分支写入。

- 每个任务提交自己的 commit；
- verifier 先验证交付；
- integration queue 串行 cherry-pick/rebase/merge；
- 冲突返回产生该改动的 Agent 修复；
- 只有 integrator 能推进目标分支。

首版 integrator 可以是用户或指定 Agent；以后再支持策略化自动集成。

## 6. 开放 Agent 模型

Core 不硬编码 `pi/claude/codex/dsh`。Agent 通过 registry 注册：

```json
{
  "id": "backend-codex",
  "adapter": "codex",
  "endpoint": "local",
  "capabilities": ["code", "test", "resume", "cancel"],
  "concurrency": 1,
  "workspaceModes": ["git-worktree"],
  "trust": "local-executor"
}
```

一个 Agent 是独立地址和能力集合，不是某个调用者的私有子节点。任意已接入 Workbench 的 Agent 都可以：

- 发现其他 Agent；
- 请求协作；
- 向房间发布状态和结果；
- 引用已有 task/event；
- 请求创建后续任务。

首版是本机开发工具，不建设账号、登录、租户、OAuth、mTLS 或复杂授权系统。开放指协议和 Agent 拓扑开放，不指建设用户系统。安全边界沿用本机进程与现有 CLI 权限；唯一需要保留的规则是：Agent 不能通过一段聊天文本把自己升级成集成者或绕过工作区边界。

### 首发适配器层次

1. **Remote A2A adapter**：连接任何标准 A2A Agent，是开放性的核心。
2. **Generic command adapter**：包装任意本地 CLI。
3. **Native adapters**：Codex app-server、Claude stream-json 等，提供会话恢复和更强状态能力。

Pi、DSH、ZCode、Gemini CLI、OpenCode 等应是可插拔 adapter，而不是 Core 架构前提。

## 7. 统一领域模型

### Agent

独立参与者及能力声明：endpoint、adapter、capabilities、concurrency、trust、health。

### Project

代码协作边界：repository、base branch/revision、workspace policy、integration policy、verification policy。

### Room

项目或主题的事件流。可长期存在，不拥有 Agent 进程。

### Task

逻辑工作单元：

```text
id, project_id, room_id
kind: analyze | implement | test | review | integrate
owner_agent
instruction / acceptance
state
depends_on
write_scopes / resource_scopes
workspace_id
request_id / request_hash
```

### Attempt

一次真实 Agent 调用：

```text
id, task_id, number
message_id, remote_task_id
state, started_at, finished_at
exit_code, error_code, result
```

### Workspace

```text
id, project_id, task_id
path, branch, base_revision
state: preparing | ready | dirty | delivered | integrated | retained | removed
```

### Evidence

```text
kind: command | test | diff | commit | file | url | review
producer
payload / digest
verified_state
```

### Event

```text
event_type
actor_id / actor_type
task_id / attempt_id / workspace_id
payload
causation_id
sequence / timestamp
```

事件必须结构化。UI 可以显示为聊天，但调度器不能靠解析自然语言 `[TODO]` 或 `exec:codex` 前缀驱动状态。

## 8. 调度原则

1. DAG 中依赖满足的任务才进入 ready。
2. 同一 Agent 不超过其声明的 concurrency。
3. 同一个物理 workspace 只允许一个 writer。
4. 不同 worktree 的文件写可并行，但共享资源冲突仍需串行。
5. review 默认只读，并由不同 Agent 执行。
6. 前置任务失败时，下游阻塞，不把错误文本当成功结果。
7. 集成任务串行执行。
8. Agent 可以提议拆分、转交或新增任务，但由 Control Plane 记录并按策略接受，不能只存在于聊天文本中。

## 9. 幂等、取消和恢复

```text
queued → preparing_workspace → dispatching → running
       → completed → verifying → verified → integrating → integrated
       ↘ failed
       ↘ rejected
       ↘ cancel_requested → cancelled
       ↘ interrupted / outcome_unknown
```

- `(project, task, request_id)` 同键同摘要返回原任务，同键异内容冲突。
- Host 事务写 Task + Outbox 后再派发。
- Attempt 使用稳定 message ID；Worker 在 spawn 前完成去重登记。
- `cancel_requested` 不等于 `cancelled`；真实进程退出后才能收口。
- 服务重启后查询 remote task，不自动重新执行已有副作用的 attempt。
- 外部 CLI 无法保证 exactly-once；产品承诺是：**幂等受理、自动启动至多一次、结果不确定不盲重放。**

## 10. 现有 grid 与 rooms 如何融合

### 从 rooms 保留

- Room/event/cursor 隔离；
- 持久 native session；
- checkpoint CAS 和分页恢复；
- HTTP、A2A、MCP 入口；
- 中断 turn 不盲目重放。

### 从 grid 保留

- A2A Agent Card 和统一调用；
- subprocess adapter 基础能力；
- 输入净化、stdin/file payload、进程树终止；
- DAG、依赖和写冲突校验思想；
- SQLite task store 和启动对账原则。

### 必须新增

- Project / Workspace / Evidence；
- adapter 和 agent registry；
- Task / Attempt / Outbox；
- workspace manager；
- scheduler 和 integration queue；
- 真实 cancel、远端幂等和结构化终态；
- Agent-to-Agent task proposal / handoff；
- 可验证的完成条件。

### 已完成的收敛

- `grid/room.py` 已删除：持久协作统一由 `rooms/` 承担。
- 旧 `grid/roundtable.py` CLI 已删除；DAG、环检测与 `write_scope` 冲突校验保留为 `grid/planning.py` 纯模块。

### 后续仍需收敛

- 固定四 Agent server：替换为配置驱动的通用 worker factory。
- 只输出文本、不暴露任务状态的生产调用路径。

## 11. 开源仓库策略

当前代码位置：

- `patchcrew`：当前唯一源码真源，由原本机融合实验台演进而来。
- `a2a-roundtable`：原公开仓库历史，已合并进 PatchCrew。
- `a2a-framework`：早期通用副本，已经分叉。

建议：

1. 保留 `a2a-roundtable` 的公开发布历史和 `v0.3.0` tag。
2. 融合开发历史已并入 `patchcrew`；后续 dogfood、发布和部署都从同一仓库进行。
3. `a2a-framework` 停止独立演进，迁移有价值代码后归档。
4. 本机 WSL、绝对路径、私有 Agent 和模型配置放入 ignored local overlay。
5. 最终本机也安装公开包，不维护另一套运行源码。

## 12. 明确不做的事情

首版不做以下内容，因为它们不能直接缩短开发交付时间：

- 登录、账号、租户和用户体系；
- OAuth、SSO、RBAC、mTLS 和公网 federation；
- 复杂审批流和 capability token；
- 长期知识库、向量记忆和组织治理；
- 为展示而做的大型 Web IDE；
- 无边界的自动递归 spawn；
- 重写已有成熟 CLI 的认证和模型调用。

Workbench 直接使用用户已经安装并可运行的 Agent CLI。配置只回答三个问题：Agent 命令是什么、能力是什么、最大并发是多少。

## 13. 开源路线图

### M0：陌生用户可运行

- 确定公开仓库、项目名和 package layout。
- 清除个人路径和固定 Agent 假设。
- Windows/Linux/macOS 可 import、测试和运行核心服务。
- 提供 fake/echo A2A workers，使 demo 不依赖用户已安装某个具体 Agent。
- 建立 CLI：`workbench init / agent add / project add / room create / task submit / status`。

验收：陌生用户 clone 后按 README，在十分钟内看到两个 fake worker 并行完成互不覆盖的任务。

### M1：开放 Agent 网络

- Agent registry + Agent Card discovery。
- Generic remote A2A adapter 和 command adapter。
- Claude/Codex 作为官方 native adapters。
- Agent 之间可发起请求、交接任务和引用事件。
- 房间和原生会话跨重启恢复。

验收：两个不同客户端中的 Agent 可以相互发现、协作，不存在父子所有权。

### M2：隔离并行开发

- Project / Workspace Manager。
- 每个写任务自动创建独立 worktree。
- DAG scheduler、write/resource scope 检测。
- 每 Agent 并发上限。
- commit/diff/test evidence 收集。

验收：两个 Agent 并行修改同一仓库的不同任务，彼此文件零覆盖，成果可独立审查。

### M3：可信执行与恢复

- 非零退出码正确失败。
- 输入超限拒绝。
- 真正进程取消。
- Task/Attempt/Outbox 和远端幂等。
- 重启恢复、`outcome_unknown`。

验收：并发重复提交只 spawn 一次；取消杀完整进程树；崩溃不造成静默重复执行。

### M4：验证与集成闭环

- acceptance contract；
- verifier worker；
- integration queue；
- 冲突回派；
- 串行推进目标分支；
- 最终报告和证据导出。

验收：多 Agent 并行开发后，只有通过验收的 commit 能进入集成队列，目标分支不会被并发覆盖。

## 14. 首屏价值表达建议

> **Open collaboration for coding agents.**  
> Connect independent agents through A2A, let them develop in parallel workspaces, and integrate verified results without overwriting each other's work.

中文：

> **面向 Coding Agent 的开放协作工作台。**  
> 让彼此独立的 Agent 通过 A2A 互相发现和协作，在隔离工作区中并行开发，并安全集成经过验证的成果。

三个首屏卖点：

- **Open, not parent-owned**：Agent 不被锁在某个主 Agent 的 subagent 树里。
- **Parallel, without overwrite**：写任务默认隔离，成果通过受控集成进入主线。
- **Durable and verifiable**：任务、上下文、状态和证据可恢复、可追踪。

## 15. 完成定义

项目融合完成的标准不是本机四个端口都在线，而是：

1. 任意兼容 A2A 的 Agent 可以注册和被其他 Agent 调用。
2. Agent 不依附于单一父会话，可跨客户端和运行时协作。
3. 多个写任务默认获得独立 workspace，不会互相覆盖。
4. 任务依赖、交接、结果和验证证据进入统一状态模型。
5. 重试、取消和重启不会制造假成功或盲目重复执行。
6. 集成目标分支始终只有一个明确 owner。
7. 新用户不改源码即可跑通公开 demo 并接入自己的 Agent。
