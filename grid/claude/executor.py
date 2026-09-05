"""Claude Agent 执行器：Windows 调 claude.exe -p，Linux 调 /usr/local/bin/claude -p。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from subprocess_executor import IS_WINDOWS, SubprocessAgentExecutor


class ClaudeExecutor(SubprocessAgentExecutor):
    BIN = (
        r"C:\Users\zhen.yuan\.local\bin\claude.exe"
        if IS_WINDOWS
        else "/usr/local/bin/claude"
    )
    ARGS_PREFIX = ["-p"]
    USE_SHELL = False  # 两个平台都是真可执行文件，参数列表免 shell 注入
    TIMEOUT = 600
    WORKING_TEXT = "claude 正在处理..."
    # claude -p 支持 --model 按会话指定模型；provider 走配置，无单一 CLI 开关，故不声明。
    MODEL_FLAG = "--model"
