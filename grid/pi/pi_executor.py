"""Pi Agent 执行器：调 pi.cmd --print。

支持 A2A 请求经由 ``SendMessageRequest.metadata`` 传入模型选择：

    metadata = {"model": "deepseek-v4-flash (self hosted)", "provider": "habi"}

pi.cmd 本身多模型（--model / --provider / --models），因此 A2A 调用方可以
按次指定模型，而不是把 pi 锁死成单一模型。校验逻辑在基类
``SubprocessAgentExecutor._safe_meta_value`` 里统一做白名单，防止注入。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from subprocess_executor import IS_WINDOWS, SubprocessAgentExecutor


class PiExecutor(SubprocessAgentExecutor):
    BIN = (
        r"C:\Users\zhen.yuan\AppData\Roaming\npm\pi.cmd"
        if IS_WINDOWS
        else "/usr/local/bin/pi"
    )
    ARGS_PREFIX = ["--print"]
    USE_SHELL = True  # 仅 Windows 的 pi.cmd shim 需要 shell；POSIX 直接 exec
    QUERY_VIA_STDIN = True  # pi --print 读 stdin
    TIMEOUT = 600
    WORKING_TEXT = "pi 正在处理..."
    # pi.cmd 支持 provider + model 双开关，让 A2A 调用方按次切换模型。
    MODEL_FLAG = "--model"
    PROVIDER_FLAG = "--provider"
