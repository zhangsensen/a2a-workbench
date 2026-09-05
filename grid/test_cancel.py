"""POSIX cancellation regression tests for SubprocessAgentExecutor."""
import asyncio
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import subprocess_executor as se  # noqa: E402
from subprocess_executor import SubprocessAgentExecutor  # noqa: E402


class _SlowShim(SubprocessAgentExecutor):
    BIN = "/bin/sh"
    ARGS_PREFIX = ["-c", "sleep 30"]
    USE_SHELL = False
    TIMEOUT = 60
    KILL_GRACE_SECONDS = 5


class _FastShim(_SlowShim):
    ARGS_PREFIX = ["-c", "printf done"]


class _Updater:
    def __init__(self) -> None:
        self.states = []
        self.artifacts = []

    async def update_status(self, state=None, message=None) -> None:
        self.states.append((state, message))

    async def add_artifact(self, parts=None) -> None:
        self.artifacts.append(parts)


def _execute_context(task_id: str):
    task = SimpleNamespace(id=task_id, context_id="context-1")
    return SimpleNamespace(
        current_task=task,
        message=object(),
        metadata={},
    )


def _cancel_context(task_id: str):
    return SimpleNamespace(task_id=task_id, context_id="context-1")


class TestCancel(unittest.TestCase):
    def setUp(self) -> None:
        with SubprocessAgentExecutor._running_processes_lock:
            self.assertFalse(SubprocessAgentExecutor._running_processes)

    def _patch_reporting(self, updater: _Updater):
        return patch.multiple(
            se,
            TaskUpdater=lambda **_kw: updater,
            get_message_text=lambda _message: "ignored",
            new_text_message=lambda text: text,
            new_text_part=lambda text: text,
        )

    def test_cancel_kills_and_reaps_slow_process_without_double_terminal(self):
        updater = _Updater()
        executor = _SlowShim()

        async def scenario():
            execution = asyncio.create_task(
                executor.execute(_execute_context("slow-task"), object())
            )
            process = None
            for _ in range(300):
                with SubprocessAgentExecutor._running_processes_lock:
                    entry = SubprocessAgentExecutor._running_processes.get(
                        "slow-task"
                    )
                    if entry is not None:
                        process = entry.process
                        break
                await asyncio.sleep(0.01)
            self.assertIsNotNone(process, "slow process was never registered")

            await executor.cancel(_cancel_context("slow-task"), object())
            await asyncio.wait_for(execution, timeout=5)
            return process

        with self._patch_reporting(updater):
            process = asyncio.run(scenario())

        self.assertIsNotNone(process.poll())
        self.assertEqual(
            [state for state, _ in updater.states],
            [
                se.TaskState.TASK_STATE_WORKING,
                se.TaskState.TASK_STATE_CANCELED,
            ],
        )
        self.assertEqual(updater.artifacts, [])
        with SubprocessAgentExecutor._running_processes_lock:
            self.assertNotIn("slow-task", SubprocessAgentExecutor._running_processes)

    def test_cancel_after_natural_exit_is_quiet(self):
        updater = _Updater()
        executor = _FastShim()

        async def scenario():
            await executor.execute(_execute_context("fast-task"), object())
            await executor.cancel(_cancel_context("fast-task"), object())

        with self._patch_reporting(updater):
            asyncio.run(scenario())

        self.assertEqual(
            [state for state, _ in updater.states],
            [
                se.TaskState.TASK_STATE_WORKING,
                se.TaskState.TASK_STATE_COMPLETED,
            ],
        )
        self.assertEqual(updater.artifacts, [["done"]])

    def test_cancel_unknown_task_does_not_create_an_updater(self):
        executor = _SlowShim()
        with patch.object(
            se, "TaskUpdater", side_effect=AssertionError("must stay quiet")
        ):
            asyncio.run(executor.cancel(_cancel_context("missing-task"), object()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
