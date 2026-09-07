"""Pi Agent 的 A2A 服务端（端口 10000）。"""
import sys
from pathlib import Path

import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill
from pi_executor import PiExecutor
from starlette.applications import Starlette
from build_info import version_stamp
from common.dedup_handler import DedupRequestHandler
from common.idempotency import RequestDedup
from common.reconcile import reconcile_stale_tasks
from task_store import make_store

skill = AgentSkill(
    id="pi-coding",
    name="Pi 编程助手",
    description="Pi coding agent：需求讨论、导师判断、轻量执行；可按次指定模型",
    # models 元数据：调用方在 SendMessageRequest.metadata 里传 {"model": ..., "provider": ...}
    # 即按次切换到 pi 的多模型能力（见 pi_executor._executor_args_from_metadata）。
    # 示例：
    #   a2a_call.py pi "分析需求" --model "deepseek-v4-flash (self hosted)" --provider habi
    input_modes=["text/plain"],
    output_modes=["text/plain"],
    tags=["pi", "a2a", "coding"],
    examples=["帮我分析这段代码", "这个需求怎么拆"],
)

card = AgentCard(
    name="Pi Agent",
    description="本机 Pi coding agent，通过 A2A 对外提供能力；支持按次指定模型",
    version=version_stamp(),
    default_input_modes=["text/plain"],
    default_output_modes=["text/plain"],
    capabilities=AgentCapabilities(streaming=True),
    supported_interfaces=[
        AgentInterface(
            protocol_binding="JSONRPC",
            url="http://127.0.0.1:10000",
            protocol_version="1.0",
        )
    ],
    skills=[skill],
)

task_store = make_store("pi")

handler = DedupRequestHandler(
    agent_executor=PiExecutor(),
    task_store=task_store,
    agent_card=card,
    request_dedup=RequestDedup("pi"),
)

routes = []
routes.extend(create_agent_card_routes(card))
routes.extend(create_jsonrpc_routes(handler, "/"))

app = Starlette(routes=routes)

async def _reconcile_on_boot() -> None:
    """启动对账：上次进程中断时卡在非终态的任务标成 FAILED（结果不可取回≠无结果）。"""
    fixed = await reconcile_stale_tasks(task_store)
    if fixed:
        print(f"reconcile: {fixed} stale task(s) marked FAILED")


if __name__ == "__main__":
    import asyncio

    asyncio.run(_reconcile_on_boot())
    print("Pi Agent start: http://127.0.0.1:10000")
    try:
        uvicorn.run(app, host="127.0.0.1", port=10000, log_level="warning")
    except Exception as e:  # port-in-use (10048) must exit, not hang
        import sys as _s
        print(f"pi server failed: {e}", file=_s.stderr)
        _s.exit(1)
