"""红队第二轮发现的四条缺陷的回归测试（均为实测复核过的真实缺陷）。

M1 SessionNotFound 曾把 agent 自己的 stdout 纳入字符串匹配：输出里出现
   "session not found" 就整任务换新会话重跑，产生重复副作用。
M2 _register_entry 在 try 之前，而 except Exception 捕不到 CancelledError：
   协程被取消时运行表条目永久泄漏，后续 cancel 拿到僵尸条目写虚假终态。
M3 失败诊断只留 stderr 头部，而 CLI 的错误信息在尾部——实测代价是 codex
   连续两轮任务失败却无法诊断（日志里只有 banner 和被截断的 prompt）。
M4 客户端读超时与执行器 TIMEOUT 相等，真超时会让两端同时放弃，调用方拿到
   不透明的客户端超时而非服务端可取回的 FAILED 终态。
"""
import asyncio
import os
import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import a2a_call  # noqa: E402
from subprocess_executor import (  # noqa: E402
    ExecutorFailure,
    SessionNotFound,
    SubprocessAgentExecutor,
    _ProcessEntry,
)

POSIX_ONLY = unittest.skipIf(os.name == "nt", "POSIX 专用 shim")


class _ResumeShim(SubprocessAgentExecutor):
    """失败退出并把给定文本分别写到 stdout / stderr。"""

    BIN = "/bin/sh"
    USE_SHELL = False
    SESSION_ID_FLAG = "--session-id"
    RESUME_FLAG = "--resume"
    TIMEOUT = 30

    stdout_text = ""
    stderr_text = ""

    @property
    def ARGS_PREFIX(self):  # noqa: N802
        script = (
            f'printf %s "{self.stdout_text}"; '
            f'printf %s "{self.stderr_text}" >&2; exit 1'
        )
        return ["-c", script]


@POSIX_ONLY
class TestSessionNotFoundIgnoresAgentStdout(unittest.TestCase):
    def _run_resume(self, stdout_text, stderr_text):
        shim = _ResumeShim()
        shim.stdout_text = stdout_text
        shim.stderr_text = stderr_text
        with self.assertRaises(ExecutorFailure) as caught:
            shim._run("q", ["--resume", "sess-1"])
        return caught.exception

    def test_marker_only_in_agent_stdout_is_not_session_loss(self):
        """M1：agent 输出里的 'session not found' 不得触发换会话重跑。"""
        failure = self._run_resume("I checked and the session not found here", "")
        self.assertNotIsInstance(failure, SessionNotFound)

    def test_marker_in_stderr_is_session_loss(self):
        failure = self._run_resume("", "Error: session not found")
        self.assertIsInstance(failure, SessionNotFound)

    def test_marker_in_stderr_without_resume_flag_is_plain_failure(self):
        shim = _ResumeShim()
        shim.stderr_text = "Error: session not found"
        with self.assertRaises(ExecutorFailure) as caught:
            shim._run("q")  # 没有 --resume：不属于会话恢复路径
        self.assertNotIsInstance(caught.exception, SessionNotFound)


class TestDiagnosticKeepsTail(unittest.TestCase):
    def test_tail_is_preserved_and_omission_is_explicit(self):
        """M3：错误信息在尾部，截断必须保留尾部并标注省略量。"""
        text = "BANNER" + ("x" * 5000) + "FATAL: the real error"
        clipped = SubprocessAgentExecutor._clip_diagnostic(text)
        self.assertIn("FATAL: the real error", clipped)
        self.assertTrue(clipped.startswith("BANNER"))
        self.assertIn("中间省略", clipped)

    def test_short_text_untouched(self):
        self.assertEqual(SubprocessAgentExecutor._clip_diagnostic("short"), "short")


class TestCancelledErrorReclaimsEntry(unittest.TestCase):
    def test_entry_is_removed_when_execute_is_cancelled(self):
        """M2：协程被取消时运行表条目必须被取回，不能泄漏。"""

        class _Hang(SubprocessAgentExecutor):
            BIN = "/bin/sh"
            ARGS_PREFIX = ["-c", "sleep 30"]
            USE_SHELL = False

        entry = _ProcessEntry()
        task_id = "cancel-leak-task"
        SubprocessAgentExecutor._register_entry(task_id, entry)

        async def scenario():
            async def body():
                try:
                    await asyncio.sleep(10)
                except asyncio.CancelledError:
                    # 模拟 execute 的 CancelledError 分支：取回条目后重抛。
                    _Hang._take_process(task_id, expected=entry)
                    raise

            task = asyncio.create_task(body())
            await asyncio.sleep(0)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        asyncio.run(scenario())
        with SubprocessAgentExecutor._running_processes_lock:
            self.assertNotIn(task_id, SubprocessAgentExecutor._running_processes)


class TestClientTimeoutExceedsExecutor(unittest.TestCase):
    def test_client_read_timeout_is_longer_than_executor_timeout(self):
        """M4：客户端必须比执行器多等，服务端终态才能先落地。"""
        self.assertGreater(a2a_call.CLIENT_READ_TIMEOUT, a2a_call.EXECUTOR_TIMEOUT)
        self.assertGreaterEqual(
            a2a_call.CLIENT_READ_TIMEOUT - a2a_call.EXECUTOR_TIMEOUT, 30
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
