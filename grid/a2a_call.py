"""通用 A2A 调用工具：任何 agent/脚本用它调任何注册的 agent。

用法：
    python a2a_call.py <agent名> "<消息>" [--stream] [--model <id>] [--provider <name>]

例：
    python a2a_call.py pi "帮我分析这段代码"
    python a2a_call.py claude "走查这个仓库" --stream   # 流式接收
    python a2a_call.py pi "分析这段代码" --model "deepseek-v4-flash (self hosted)" --provider habi

机制：读 agents.json 拿 agent 地址 → 发现 Agent Card → 发消息 → 打印回复。
这是「pi/claude/codex 互相用」的统一入口。

模型选择：pi 本身多模型，A2A 调用时可用 ``--model``/``--provider`` 按次指定；
这两个值经 ``SendMessageRequest.metadata`` 透传给执行器，由 executor 白名单校验
后拼成 ``pi.cmd --model ... --provider ...``，不回落到命令行注入。
"""
import asyncio
import json
import sys
from pathlib import Path

import httpx

# Windows consoles default stdout/stderr to the system codepage (often GBK on
# this machine), which raises UnicodeEncodeError on any character it can't
# represent (emoji, checkmarks, em-dashes, Greek letters -- all common in LLM
# replies). That crash happens *after* the agent call already succeeded, so a
# caller capturing stdout sees nothing and misreads it as "no reply".
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from a2a.client import A2ACardResolver, ClientConfig, create_client
from a2a.helpers import new_text_message
from a2a.types import Role, SendMessageRequest, TaskState

BASE = Path(__file__).resolve().parent
CATALOG = BASE / "agents.json"

# 服务端的 FAILED/CANCELED 必须传播到调用方：master 的自动编排全靠区分成败。
_TERMINAL_BAD = {
    TaskState.TASK_STATE_FAILED: "FAILED",
    TaskState.TASK_STATE_CANCELED: "CANCELED",
}


class AgentTaskFailed(RuntimeError):
    """远端任务落在失败终态；text 为可读原因（来自任务 artifact/status）。"""

    def __init__(self, state: str, text: str) -> None:
        super().__init__(f"[{state}] {text}")
        self.state = state
        self.text = text


def finalize_reply(final_state: object, parts: list[str]) -> str:
    """把(终态, 文本片段)收敛为结果：失败终态抛异常，其余返回文本。

    旧实现只拼 artifact 文本、丢弃任务终态——服务端诚实的 FAILED 在客户端
    变成 exit 0 的"正常输出"，master 会把错误文本当成功结果继续用。
    """
    text = "\n".join(parts) if parts else "(无文本回复)"
    if final_state in _TERMINAL_BAD:
        raise AgentTaskFailed(_TERMINAL_BAD[final_state], text)
    return text


def load_catalog() -> dict:
    return json.loads(CATALOG.read_text(encoding="utf-8"))


async def call_agent(
    name: str,
    text: str,
    streaming: bool = False,
    model: str | None = None,
    provider: str | None = None,
    cwd: str | None = None,
) -> str:
    catalog = load_catalog()
    target = next((a for a in catalog["agents"] if a["name"] == name), None)
    if target is None:
        raise SystemExit(f"未注册的 agent：{name}，可选：{[a['name'] for a in catalog['agents']]}")

    base_url = f"http://127.0.0.1:{target['port']}"
    hc = httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=10.0))
    try:
        card = await A2ACardResolver(httpx_client=hc, base_url=base_url).get_agent_card()
        client = await create_client(
            agent=card, client_config=ClientConfig(streaming=streaming, httpx_client=hc)
        )
        # 模型选择走请求元数据（A2A 原生通道），由执行器白名单校验后透传。
        # cwd 把执行钉到指定工作区（如 git worktree），并行派活互不踩踏。
        meta: dict[str, str] = {}
        if model:
            meta["model"] = model
        if provider:
            meta["provider"] = provider
        if cwd:
            meta["cwd"] = cwd
        request = SendMessageRequest(
            message=new_text_message(text, role=Role.ROLE_USER),
            metadata=meta or None,
        )
        parts: list[str] = []
        final_state = None
        async for chunk in client.send_message(request):
            task = getattr(chunk, "task", None)
            status = getattr(task, "status", None)
            if status is not None:
                final_state = status.state
            for artifact in getattr(task, "artifacts", []) or []:
                for part in artifact.parts:
                    if getattr(part, "text", ""):
                        parts.append(part.text)
        await client.close()
        return finalize_reply(final_state, parts)
    finally:
        await hc.aclose()


if __name__ == "__main__":
    args = [a for a in sys.argv[1:]]
    if "--list" in args or "-l" in args:
        catalog = load_catalog()
        print("已注册的 agent：")
        for a in catalog["agents"]:
            print(f"  {a['name']:<8} port={a['port']}  {a['description']}  [{a.get('status','active')}]")
        raise SystemExit(0)
    use_stream = "--stream" in args
    args = [a for a in args if a != "--stream"]
    model = None
    provider = None
    cwd = None
    if "--model" in args:
        i = args.index("--model")
        if i + 1 < len(args):
            model = args[i + 1]
            del args[i:i + 2]
    if "--provider" in args:
        i = args.index("--provider")
        if i + 1 < len(args):
            provider = args[i + 1]
            del args[i:i + 2]
    if "--cwd" in args:
        i = args.index("--cwd")
        if i + 1 < len(args):
            cwd = args[i + 1]
            del args[i:i + 2]
    if len(args) < 2:
        raise SystemExit('用法：python a2a_call.py <agent名> "<消息>" [--stream] [--model <id>] [--provider <name>] [--cwd <目录>]\n      python a2a_call.py --list')
    agent_name, message = args[0], args[1]
    try:
        reply = asyncio.run(
            call_agent(
                agent_name,
                message,
                streaming=use_stream,
                model=model,
                provider=provider,
                cwd=cwd,
            )
        )
    except AgentTaskFailed as failure:
        # 失败原因照常打印（调用方要看），但退出码必须非零——
        # 让 shell 编排、CI 和 master 都能用 $? 分辨成败。
        print(failure.text)
        print(f"task state: {failure.state}", file=sys.stderr)
        raise SystemExit(1) from None
    print(reply)
