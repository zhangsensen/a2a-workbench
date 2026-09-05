"""DeepSeek Agent 执行器：调 dsh --profile headless。"""
from contextlib import contextmanager
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from subprocess_executor import (
    ExecutorFailure,
    ExecutorTimeout,
    IS_WINDOWS,
    SubprocessAgentExecutor,
)

MAX_DSH_INPUT_BYTES = 1_048_576
PAYLOAD_DIR = BASE / ".a2a-payloads"
_ORPHAN_MAX_AGE_SECONDS = 24 * 60 * 60


class DSHExecutor(SubprocessAgentExecutor):
    BIN = (
        r"C:\Users\zhen.yuan\AppData\Roaming\npm\dsh.cmd"
        if IS_WINDOWS
        else "/usr/local/bin/dsh"
    )
    ARGS_PREFIX = ["--profile", "headless"]
    USE_SHELL = True  # 仅 Windows 的 dsh.cmd shim 需要 shell；POSIX 直接 exec
    TIMEOUT = 600
    WORKING_TEXT = "dsh 正在处理..."

    def _sanitize_dsh(self, text: str) -> str:
        """Sanitize text and reject oversized UTF-8 input without truncation."""
        if not isinstance(text, str):
            return ""
        cleaned = "".join(
            ch
            for ch in text
            if (ch >= " " or ch in "\n\r\t")
            and not ("\ud800" <= ch <= "\udfff")
        )
        size = len(cleaned.encode("utf-8"))
        if size > MAX_DSH_INPUT_BYTES:
            raise ValueError(
                f"DSH payload {size} bytes exceeds {MAX_DSH_INPUT_BYTES}-byte limit"
            )
        return cleaned

    def _remove_stale_payloads(self) -> None:
        """Best-effort cleanup of transport files left by a crashed process."""
        cutoff = time.time() - _ORPHAN_MAX_AGE_SECONDS
        for path in PAYLOAD_DIR.glob("dsh-a2a-*.txt"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                pass

    @contextmanager
    def _payload_file(self, payload: str) -> Iterator[Path]:
        """Stage one payload atomically and always remove it on normal exit."""
        PAYLOAD_DIR.mkdir(parents=True, exist_ok=True)
        self._remove_stale_payloads()
        fd, raw_path = tempfile.mkstemp(
            prefix="dsh-a2a-", suffix=".txt", dir=PAYLOAD_DIR
        )
        path = Path(raw_path)
        try:
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                fd = -1
                handle.write(payload)
            yield path
        finally:
            if fd >= 0:
                os.close(fd)
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    # _kill_process_tree now lives on SubprocessAgentExecutor: the same
    # taskkill /T /F guarantee is needed by pi/claude/codex, so keeping a
    # DSH-private copy would let the shared path silently regress.

    def _run(self, query: str, extra_args: list[str] | None = None,
             cwd_request: object = None) -> str:
        """Run DSH with a short file-bridge task, avoiding cmd.exe's 8191 limit.

        ``extra_args``（模型选择等）暂不适用：dsh 的模型由 settings.yaml 的
        ``agent-default-model`` 决定，无单一 --model CLI 开关；传进来也忽略，
        避免用不确定的配置文件写入做并发不安全的全局改动。
        ``cwd_request``（工作区钉定）适用：并行 worktree 派活的基础。
        """
        del extra_args  # 显式忽略模型选择（dsh 模型为配置驱动）
        cwd = self._validated_cwd(cwd_request) or str(BASE)
        try:
            payload = self._sanitize_dsh(query)
        except ValueError as exc:
            raise ExecutorFailure(f"(输入过长: {exc})") from exc

        try:
            with self._payload_file(payload) as path:
                bridge = (
                    f'Read the complete UTF-8 user task from "{path}" and carry it out. '
                    "For integrity checks, use local code to hash requested byte ranges "
                    "directly from that file. This file is transport-only; do not modify "
                    "or delete it, and do not ask for the task again. Return the task result."
                )
                if IS_WINDOWS:
                    # .cmd shim 需要 shell；bridge 是固定文本（含临时文件路径），
                    # 不含用户可控内容，list2cmdline 转义足够。
                    cmd = subprocess.list2cmdline([self.BIN, *self.ARGS_PREFIX, bridge])
                    proc = subprocess.Popen(
                        cmd,
                        shell=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        cwd=cwd,
                        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
                    )
                else:
                    # POSIX：直接 exec，自成进程组供 killpg 整树终止。
                    proc = subprocess.Popen(
                        [self.BIN, *self.ARGS_PREFIX, bridge],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        cwd=cwd,
                        start_new_session=True,
                    )
                try:
                    stdout, stderr = proc.communicate(timeout=self.TIMEOUT)
                except subprocess.TimeoutExpired:
                    self._kill_process_tree(proc)
                    # _reap_after_kill deliberately never calls communicate()
                    # without a timeout: if the cmd shim dies but a node
                    # descendant survives, inherited pipe handles can keep it
                    # blocked forever.
                    self._reap_after_kill(proc)
                    raise ExecutorTimeout(
                        f"(调用超时 >{self.TIMEOUT}s)"
                    ) from None
        except ExecutorFailure:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ExecutorFailure(
                f"(调用失败: {type(exc).__name__}: {exc})"
            ) from exc

        out = self._clip_output(stdout.decode("utf-8", errors="replace").strip())
        err = stderr.decode("utf-8", errors="replace").strip()
        # 与基类同一契约：非零退出码是失败，半截 stdout 不算成功答案。
        if proc.returncode:
            raise ExecutorFailure(
                f"(退出码 {proc.returncode}) stderr: {err[:1000]}\nstdout: {out[:2000]}"
            )
        if out:
            return out
        return f"(无输出) stderr: {err[:500]}"
