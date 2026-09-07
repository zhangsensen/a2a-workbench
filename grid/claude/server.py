"""Claude Agent 的 A2A 服务端（端口 10001）。"""
import sys
from pathlib import Path

import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill
from executor import ClaudeExecutor
from starlette.applications import Starlette
from build_info import version_stamp
from common.dedup_handler import DedupRequestHandler
from common.idempotency import RequestDedup
from common.reconcile import reconcile_stale_tasks
from task_store import make_store


skill = AgentSkill(
    id="claude-coding",
    name="Claude Code",
    description="Claude Code：仓库走查、工程交付、交叉审核",
    input_modes=["text/plain"],
    output_modes=["text/plain"],
    tags=["claude", "a2a", "coding"],
    examples=["帮我走查这个仓库", "做一次代码审查"],
)

card = AgentCard(
    name="Claude Agent",
    description="本机 Claude Code agent，通过 A2A 对外提供能力",
    version=version_stamp(),
    default_input_modes=["text/plain"],
    default_output_modes=["text/plain"],
    capabilities=AgentCapabilities(streaming=True),
    supported_interfaces=[
        AgentInterface(
            protocol_binding="JSONRPC",
            url="http://127.0.0.1:10001",
            protocol_version="1.0",
        )
    ],
    skills=[skill],
)

task_store = make_store("claude")

handler = DedupRequestHandler(
    agent_executor=ClaudeExecutor(),
    task_store=task_store,
    agent_card=card,
    request_dedup=RequestDedup("claude"),
)

routes = []
routes.extend(create_agent_card_routes(card))
routes.extend(create_jsonrpc_routes(handler, "/"))

app = Starlette(routes=routes)

async def _reconcile_on_boot() -> None:
    """启动对账：上次进程中断时卡在非终态的任务标成 FAILED。"""
    fixed = await reconcile_stale_tasks(task_store)
    if fixed:
        print(f"reconcile: {fixed} stale task(s) marked FAILED")


if __name__ == "__main__":
    import asyncio

    asyncio.run(_reconcile_on_boot())
    print("Claude Agent start: http://127.0.0.1:10001")
    try:
        uvicorn.run(app, host="127.0.0.1", port=10001, log_level="warning")
    except Exception as e:  # port-in-use (10048) must exit, not hang
        import sys as _s
        print(f"claude server failed: {e}", file=_s.stderr)
        _s.exit(1)
