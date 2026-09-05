"""Codex Agent executor: send UTF-8 prompts to ``codex exec -`` via stdin."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from subprocess_executor import ExecutorFailure, IS_WINDOWS, SubprocessAgentExecutor


MAX_CODEX_INPUT_BYTES = 256 * 1024


class CodexExecutor(SubprocessAgentExecutor):
    BIN = (
        r"C:\Users\zhen.yuan\AppData\Roaming\npm\codex.cmd"
        if IS_WINDOWS
        else "/usr/local/bin/codex"
    )
    ARGS_PREFIX = ["exec", "--skip-git-repo-check", "-"]
    USE_SHELL = True  # 仅 Windows 的 codex.cmd shim 需要 shell；两平台 prompt 都走 stdin
    QUERY_VIA_STDIN = True
    TIMEOUT = 600
    WORKING_TEXT = "codex 正在处理..."
    # codex exec 支持 -m/--model；provider 走 -c/--config 覆盖，无单一开关，故仅声明 model。
    MODEL_FLAG = "--model"

    def _sanitize(self, text: str) -> str:
        """Remove disallowed controls and reject, never truncate, oversized UTF-8."""
        if not isinstance(text, str):
            return ""
        cleaned = "".join(
            ch
            for ch in text
            if (ch >= " " or ch in "\n\t")
            and not ("\ud800" <= ch <= "\udfff")
        )
        size = len(cleaned.encode("utf-8"))
        if size > MAX_CODEX_INPUT_BYTES:
            raise ValueError(
                f"Codex payload {size} bytes exceeds {MAX_CODEX_INPUT_BYTES}-byte limit"
            )
        return cleaned

    def _run(self, query: str, extra_args: list[str] | None = None,
             cwd_request: object = None) -> str:
        try:
            return super()._run(query, extra_args, cwd_request)
        except ValueError as exc:
            # Oversized input is a definitive rejection, not a result: raise so
            # execute() records a retrievable FAILED terminal state instead of
            # shipping the refusal text as a "completed" answer (SENY-162 #4).
            raise ExecutorFailure(f"(输入过长: {exc})") from exc
