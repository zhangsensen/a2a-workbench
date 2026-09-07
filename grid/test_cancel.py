"""POSIX cancellation regression tests for SubprocessAgentExecutor."""
import asyncio
import os
import subprocess
import sys
import threading
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


@unittest.skipIf(os.name == "nt", "POSIX 专用 shim")
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
                    if entry is not None and entry.process is not None:
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

    def test_cancel_before_spawn_never_calls_popen(self):
        updater = _Updater()
        sanitize_started = threading.Event()
        allow_sanitize = threading.Event()

        class _BlockedBeforeSpawnShim(_FastShim):
            def _sanitize(self, text: str) -> str:
                sanitize_started.set()
                self.assert_allow_sanitize()
                return super()._sanitize(text)

            @staticmethod
            def assert_allow_sanitize() -> None:
                if not allow_sanitize.wait(timeout=5):
                    raise AssertionError("test did not release sanitize")

        executor = _BlockedBeforeSpawnShim()

        async def scenario():
            execution = asyncio.create_task(
                executor.execute(_execute_context("pre-spawn-task"), object())
            )
            for _ in range(300):
                if sanitize_started.is_set():
                    break
                await asyncio.sleep(0.01)
            self.assertTrue(sanitize_started.is_set())

            await executor.cancel(_cancel_context("pre-spawn-task"), object())
            allow_sanitize.set()
            await asyncio.wait_for(execution, timeout=5)

        with self._patch_reporting(updater), patch.object(
            se.subprocess, "Popen"
        ) as popen:
            asyncio.run(scenario())

        popen.assert_not_called()
        self.assertEqual(
            [state for state, _ in updater.states],
            [
                se.TaskState.TASK_STATE_WORKING,
                se.TaskState.TASK_STATE_CANCELED,
            ],
        )
        self.assertEqual(updater.artifacts, [])

    def test_cancel_while_popen_returns_kills_spawned_process_tree(self):
        updater = _Updater()
        popen_started = threading.Event()
        allow_popen_return = threading.Event()
        process = SimpleNamespace(pid=1234)
        executor = _FastShim()

        def delayed_popen(*_args, **_kwargs):
            popen_started.set()
            if not allow_popen_return.wait(timeout=5):
                raise AssertionError("test did not release Popen")
            return process

        async def scenario():
            execution = asyncio.create_task(
                executor.execute(_execute_context("during-spawn-task"), object())
            )
            for _ in range(300):
                if popen_started.is_set():
                    break
                await asyncio.sleep(0.01)
            self.assertTrue(popen_started.is_set())

            await executor.cancel(_cancel_context("during-spawn-task"), object())
            allow_popen_return.set()
            await asyncio.wait_for(execution, timeout=5)

        with self._patch_reporting(updater), patch.object(
            se.subprocess, "Popen", side_effect=delayed_popen
        ) as popen, patch.object(
            executor, "_kill_process_tree"
        ) as kill_tree, patch.object(
            executor, "_reap_after_kill"
        ) as reap:
            asyncio.run(scenario())

        popen.assert_called_once()
        kill_tree.assert_called_once_with(process)
        reap.assert_called_once_with(process)
        self.assertEqual(
            [state for state, _ in updater.states],
            [
                se.TaskState.TASK_STATE_WORKING,
                se.TaskState.TASK_STATE_CANCELED,
            ],
        )
        self.assertEqual(updater.artifacts, [])

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
