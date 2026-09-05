"""A2A 任务库启动对账：把上次服务器中断遗留的非终态任务标成 FAILED。

四个 server（pi/claude/codex/dsh）共用。之前只有 dsh 在 2026-08-25 被
SENY-162 一次性人工清过，其余库里留着永远 WORKING 的僵尸任务；服务器
崩溃/重启后异步轮询方会永远等一个没有结果的任务。

只在 server 启动时调用一次：此刻本 agent 的 server 是唯一写库方，
不会误伤运行中的任务。
"""
from a2a.server.context import ServerCallContext
from a2a.types import TaskState

TERMINAL_STATES = {
    TaskState.TASK_STATE_COMPLETED,
    TaskState.TASK_STATE_FAILED,
    TaskState.TASK_STATE_CANCELED,
    TaskState.TASK_STATE_REJECTED,
}
RECONCILE_REASON = "历史任务在服务器重启前被中断，已标失败。"


async def reconcile_stale_tasks(store) -> int:
    """把任务库里所有非终态任务标记为 FAILED，返回处理条数。"""
    from a2a.types.a2a_pb2 import ListTasksRequest, TASK_STATE_FAILED

    ctx = ServerCallContext()
    resp = await store.list(ListTasksRequest(page_size=1000), ctx)
    fixed = 0
    for task in resp.tasks:
        if task.status and task.status.state in TERMINAL_STATES:
            continue
        task.status.state = TASK_STATE_FAILED
        msg = task.status.message if task.status.HasField("message") else None
        if msg is not None and msg.parts:
            msg.parts[0].text = RECONCILE_REASON
        await store.save(task, ctx)
        fixed += 1
    return fixed
