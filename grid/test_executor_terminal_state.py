"""SENY-162 Stage 1 #4 regression: timeout/failure must reach a retrievable
FAILED terminal state, must not silently truncate, and must not leave the
executor occupied by an orphaned process tree.

What the old shared executor did wrong:

1. ``subprocess.run(..., timeout=TIMEOUT)`` returned the string
   ``"(调用超时 >600s)"``, which ``execute()`` then shipped as an artifact and
   marked ``TASK_STATE_COMPLETED``. A caller could only tell a timeout from a
   real answer by matching that Chinese sentence.
2. On Windows, killing a ``shell=True`` child kills only the ``cmd.exe`` shim.
   The ``node.exe`` / real CLI descendants survived, kept burning CPU and API
   quota, and their inherited pipe handles could block a later
   ``communicate()`` forever.
3. Output over ``MAX_OUTPUT`` was clipped with no marker at all.
4. An unexpected exception inside ``_run`` escaped ``execute()``, leaving the
   task pinned in ``WORKING`` with no terminal state to fetch.
"""
import asyncio
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import subprocess_executor as se  # noqa: E402
from subprocess_executor import (  # noqa: E402
    MAX_OUTPUT,
    ExecutorFailure,
    ExecutorTimeout,
    SubprocessAgentExecutor,
)


class _SleepyShim(SubprocessAgentExecutor):
    """A shell child that outlives TIMEOUT, with a grandchild to orphan."""

    # `ping -n` is the portable "sleep" on Windows cmd. The `&&` chain gives
    # cmd.exe a real descendant, which is what the tree kill must reach.
    BIN = "cmd.exe"
    ARGS_PREFIX = ["/c", "ping -n 60 127.0.0.1 >nul && echo done"]
    USE_SHELL = True
    QUERY_VIA_STDIN = False
    TIMEOUT = 3
    KILL_GRACE_SECONDS = 20


class _EchoShim(SubprocessAgentExecutor):
    BIN = "cmd.exe"
    ARGS_PREFIX = ["/c", "echo hello-a2a"]
    USE_SHELL = True
    QUERY_VIA_STDIN = False
    TIMEOUT = 30


class TestTimeoutIsATerminalFailure(unittest.TestCase):
    def test_timeout_raises_executor_timeout(self):
        with self.assertRaises(ExecutorTimeout) as caught:
            _SleepyShim()._run("anything")
        self.assertIn("调用超时", caught.exception.text)
        self.assertIn(str(_SleepyShim.TIMEOUT), caught.exception.text)

    def test_timeout_leaves_no_orphaned_descendant(self):
        """The whole point of taskkill /T: no `ping` survives the timeout."""

        def _ping_count():
            out = subprocess.run(
                ["tasklist.exe", "/FI", "IMAGENAME eq PING.EXE", "/NH"],
                capture_output=True, text=True, timeout=30,
            ).stdout
            return out.upper().count("PING.EXE")

        before = _ping_count()
        with self.assertRaises(ExecutorTimeout):
            _SleepyShim()._run("anything")
        # taskkill is synchronous, but Windows needs a beat to reap.
        for _ in range(20):
            if _ping_count() <= before:
                break
            import time

            time.sleep(0.5)
        self.assertLessEqual(
            _ping_count(), before,
            "timeout must not leave the shim's descendants running",
        )


class TestHappyPathStillWorks(unittest.TestCase):
    def test_shell_child_output_is_returned(self):
        self.assertIn("hello-a2a", _EchoShim()._run("ignored"))

    def test_stdin_payload_reaches_the_child(self):
        class _CatShim(SubprocessAgentExecutor):
            # `findstr /r .` echoes stdin lines back, proving the payload is
            # delivered through the pipe and never through the command line.
            BIN = "cmd.exe"
            ARGS_PREFIX = ["/c", "findstr /r ."]
            USE_SHELL = True
            QUERY_VIA_STDIN = True
            TIMEOUT = 30

        out = _CatShim()._run("line-one\nline-two")
        self.assertIn("line-one", out)
        self.assertIn("line-two", out)


class TestOutputTruncationIsExplicit(unittest.TestCase):
    def test_short_output_untouched(self):
        self.assertEqual(SubprocessAgentExecutor._clip_output("abc"), "abc")

    def test_boundary_is_not_annotated(self):
        exact = "x" * MAX_OUTPUT
        self.assertEqual(SubprocessAgentExecutor._clip_output(exact), exact)

    def test_over_limit_is_annotated_with_real_sizes(self):
        total = MAX_OUTPUT + 1234
        clipped = SubprocessAgentExecutor._clip_output("y" * total)
        self.assertTrue(clipped.startswith("y" * MAX_OUTPUT))
        self.assertIn("输出已截断", clipped)
        self.assertIn(str(MAX_OUTPUT), clipped)
        self.assertIn(str(total), clipped)


class _Updater:
    """Records what execute() reported, standing in for TaskUpdater."""

    def __init__(self, **_kw):
        self.states = []
        self.artifacts = []

    async def update_status(self, state=None, message=None):
        self.states.append((state, message))

    async def add_artifact(self, parts=None):
        self.artifacts.append(parts)


class TestExecuteMapsFailuresToTerminalState(unittest.TestCase):
    def _drive(self, run_side_effect):
        from a2a.types import TaskState

        updater = _Updater()
        executor = _EchoShim()
        context = MagicMock()
        context.current_task = MagicMock(id="t-1", context_id="c-1")
        queue = MagicMock()

        async def _enqueue(_event):
            return None

        queue.enqueue_event = _enqueue

        with patch.object(se, "TaskUpdater", lambda **kw: updater), \
                patch.object(se, "get_message_text", lambda _m: "q"), \
                patch.object(se, "new_text_message", lambda t: t), \
                patch.object(se, "new_text_part", lambda text: text), \
                patch.object(type(executor), "_run", side_effect=run_side_effect,
                             autospec=False):
            asyncio.run(executor.execute(context, queue))
        return updater, TaskState

    def test_timeout_reports_failed_and_keeps_the_reason_retrievable(self):
        updater, TaskState = self._drive(
            ExecutorTimeout("(调用超时 >3s)")
        )
        final_state, final_message = updater.states[-1]
        self.assertEqual(final_state, TaskState.TASK_STATE_FAILED)
        self.assertIn("调用超时", final_message)
        # Retrievable: the reason is also an artifact on the task.
        self.assertEqual(updater.artifacts, [["(调用超时 >3s)"]])
        self.assertNotIn(
            TaskState.TASK_STATE_COMPLETED, [s for s, _ in updater.states]
        )

    def test_oversized_input_reports_failed(self):
        updater, TaskState = self._drive(
            ExecutorFailure("(输入过长: 999 bytes exceeds ...)")
        )
        self.assertEqual(updater.states[-1][0], TaskState.TASK_STATE_FAILED)
        self.assertIn("输入过长", updater.artifacts[-1][0])

    def test_unexpected_exception_still_reaches_a_terminal_state(self):
        updater, TaskState = self._drive(RuntimeError("boom"))
        final_state, final_message = updater.states[-1]
        self.assertEqual(final_state, TaskState.TASK_STATE_FAILED)
        self.assertIn("内部错误", final_message)
        self.assertIn("RuntimeError", final_message)

    def test_success_still_completes(self):
        updater, TaskState = self._drive(None)  # side_effect=None -> returns MagicMock
        self.assertEqual(updater.states[-1][0], TaskState.TASK_STATE_COMPLETED)


class TestSubclassRejectionsAreFailures(unittest.TestCase):
    def test_codex_oversized_payload_raises(self):
        from codex.executor import MAX_CODEX_INPUT_BYTES, CodexExecutor

        with self.assertRaises(ExecutorFailure) as caught:
            CodexExecutor()._run("A" * (MAX_CODEX_INPUT_BYTES + 1))
        self.assertIn("输入过长", caught.exception.text)

    def test_codex_under_limit_is_not_rejected_by_sanitize(self):
        from codex.executor import MAX_CODEX_INPUT_BYTES, CodexExecutor

        payload = "B" * (MAX_CODEX_INPUT_BYTES - 1)
        # _sanitize must return the payload whole — never a truncated prefix.
        self.assertEqual(len(CodexExecutor()._sanitize(payload)), len(payload))

    def test_dsh_oversized_payload_raises(self):
        from dsh.executor import MAX_DSH_INPUT_BYTES, DSHExecutor

        with self.assertRaises(ExecutorFailure) as caught:
            DSHExecutor()._run("C" * (MAX_DSH_INPUT_BYTES + 1))
        self.assertIn("输入过长", caught.exception.text)

    def test_dsh_reuses_the_shared_process_tree_kill(self):
        from dsh.executor import DSHExecutor

        self.assertIs(
            DSHExecutor._kill_process_tree.__func__,
            SubprocessAgentExecutor._kill_process_tree.__func__,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
