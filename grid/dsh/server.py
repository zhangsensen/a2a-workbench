"""DeepSeek Agent 的 A2A 服务端（端口 10003）。"""
import sys
from pathlib import Path

import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill
from executor import DSHExecutor
from starlette.applications import Starlette
from build_info import version_stamp
from common.reconcile import reconcile_stale_tasks
from task_store import make_store


skill = AgentSkill(
    id="dsh-coding",
    name="DeepSeek CLI",
    description="DeepSeek official harness CLI：推理、代码、长上下文任务",
    input_modes=["text/plain"],
    output_modes=["text/plain"],
    tags=["dsh", "a2a", "coding"],
    examples=["帮我推理这个问题", "写一段代码"],
)

card = AgentCard(
    name="DeepSeek Agent",
    description="本机 DeepSeek dsh agent，通过 A2A 对外提供能力",
    version=version_stamp(),
    default_input_modes=["text/plain"],
    default_output_modes=["text/plain"],
    capabilities=AgentCapabilities(streaming=True),
    supported_interfaces=[
        AgentInterface(
            protocol_binding="JSONRPC",
            url="http://127.0.0.1:10003",
            protocol_version="1.0",
        )
    ],
    skills=[skill],
)

task_store = make_store("dsh")

handler = DefaultRequestHandler(
    agent_executor=DSHExecutor(),
    task_store=task_store,
    agent_card=card,
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
    print("DeepSeek Agent start: http://127.0.0.1:10003")
    try:
        uvicorn.run(app, host="127.0.0.1", port=10003, log_level="warning")
    except Exception as e:  # port-in-use (10048) must exit, not hang
        import sys as _s
        print(f"dsh server failed: {e}", file=_s.stderr)
        _s.exit(1)
