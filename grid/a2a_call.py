"""通用 A2A 调用工具：任何 agent/脚本用它调任何注册的 agent。

用法：
    python a2a_call.py <agent名> "<消息>" [--stream] [--model <id>] [--provider <name>] [--context <id>] [--request-id <id>] [--fresh-context]

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

# 执行器侧 TIMEOUT=600s；客户端多给 60s 宽限，让服务端的终态先落地。
EXECUTOR_TIMEOUT = 600.0
CLIENT_READ_TIMEOUT = EXECUTOR_TIMEOUT + 60.0

# 成功白名单：只有 COMPLETED 算成功。之前用的是"FAILED/CANCELED 才算失败"的
# 黑名单，任何新增或未预料的状态（REJECTED、INPUT_REQUIRED、AUTH_REQUIRED，以及
# 协议不完整导致的 WORKING/None）都会被当成成功返回——编排方据此继续推进就是
# 在错误的前提上工作。白名单让未知状态默认失败。
_SUCCESS = TaskState.TASK_STATE_COMPLETED

# 状态 → (可读名, CLI 退出码)。退出码分级让 shell/CI 能区分处置方式：
# 1 执行失败可查日志；2 被取消；3 被拒绝；4/5 需要人介入补输入或授权；
# 6 协议不完整（拿不到终态），属于基础设施问题而非任务结果。
_STATE_EXIT = {
    TaskState.TASK_STATE_FAILED: ("FAILED", 1),
    TaskState.TASK_STATE_CANCELED: ("CANCELED", 2),
    TaskState.TASK_STATE_REJECTED: ("REJECTED", 3),
    TaskState.TASK_STATE_INPUT_REQUIRED: ("INPUT_REQUIRED", 4),
    TaskState.TASK_STATE_AUTH_REQUIRED: ("AUTH_REQUIRED", 5),
}
_UNSETTLED_EXIT = 6  # WORKING/SUBMITTED/UNSPECIFIED/None：没有终态可依据


def state_name_and_exit(state: object) -> tuple[str, int]:
    """把任意终态映射成 (可读名, 退出码)；未知或非终态一律 6。"""
    if state in _STATE_EXIT:
        return _STATE_EXIT[state]
    if state is None:
        return "NO_TERMINAL_STATE", _UNSETTLED_EXIT
    try:
        name = TaskState.Name(state)
    except (TypeError, ValueError):
        name = str(state)
    return name, _UNSETTLED_EXIT


class AgentTaskFailed(RuntimeError):
    """远端任务未落在 COMPLETED；text 为可读原因（来自任务 artifact/status）。"""

    def __init__(self, state: str, text: str, exit_code: int = 1) -> None:
        super().__init__(f"[{state}] {text}")
        self.state = state
        self.text = text
        self.exit_code = exit_code


def finalize_reply(final_state: object, parts: list[str]) -> str:
    """把(终态, 文本片段)收敛为结果：只有 COMPLETED 返回文本，其余一律抛异常。

    旧实现只拼 artifact 文本、丢弃任务终态——服务端诚实的 FAILED 在客户端
    变成 exit 0 的"正常输出"，master 会把错误文本当成功结果继续用。
    """
    text = "\n".join(parts) if parts else "(无文本回复)"
    if final_state == _SUCCESS:
        return text
    name, exit_code = state_name_and_exit(final_state)
    raise AgentTaskFailed(name, text, exit_code)


def load_catalog() -> dict:
    return json.loads(CATALOG.read_text(encoding="utf-8"))


async def call_agent(
    name: str,
    text: str,
    streaming: bool = False,
    model: str | None = None,
    provider: str | None = None,
    cwd: str | None = None,
    context: str | None = None,
    fresh_context: bool = False,
    request_id: str | None = None,
) -> str:
    catalog = load_catalog()
    target = next((a for a in catalog["agents"] if a["name"] == name), None)
    if target is None:
        raise SystemExit(f"未注册的 agent：{name}，可选：{[a['name'] for a in catalog['agents']]}")

    base_url = f"http://127.0.0.1:{target['port']}"
    # 客户端读超时必须比执行器的 TIMEOUT（600s）更长：两者相等时真超时会让
    # 客户端与服务端同时放弃，调用方拿到不透明的 A2AClientTimeoutError，而不是
    # 服务端诚实的 "(调用超时 >600s)" FAILED 终态（可取回、有原因）。
    hc = httpx.AsyncClient(timeout=httpx.Timeout(CLIENT_READ_TIMEOUT, connect=10.0))
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
        if context:
            meta["context"] = context
        if fresh_context:
            meta["contextReset"] = "1"
        if request_id:
            meta["requestId"] = request_id
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
    fresh_context = "--fresh-context" in args
    args = [a for a in args if a != "--fresh-context"]
    model = None
    provider = None
    cwd = None
    context_id = None
    request_id = None
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
    if "--context" in args:
        i = args.index("--context")
        if i + 1 < len(args):
            context_id = args[i + 1]
            del args[i:i + 2]
    if "--request-id" in args:
        i = args.index("--request-id")
        if i + 1 < len(args):
            request_id = args[i + 1]
            del args[i:i + 2]
    if len(args) < 2:
        raise SystemExit('用法：python a2a_call.py <agent名> "<消息>" [--stream] [--model <id>] [--provider <name>] [--cwd <目录>] [--context <id>] [--request-id <id>] [--fresh-context]\n      python a2a_call.py --list')
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
                context=context_id,
                fresh_context=fresh_context,
                request_id=request_id,
            )
        )
    except AgentTaskFailed as failure:
        # 失败原因照常打印（调用方要看），但退出码必须非零且分级——
        # 让 shell 编排、CI 和 master 既能用 $? 分辨成败，也能区分处置方式。
        print(failure.text)
        print(f"task state: {failure.state} (exit {failure.exit_code})", file=sys.stderr)
        raise SystemExit(failure.exit_code) from None
    print(reply)
