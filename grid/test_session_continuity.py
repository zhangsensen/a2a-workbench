"""POSIX regression tests for native CLI session continuity."""
import asyncio
import os
import re
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import subprocess_executor as se  # noqa: E402
from subprocess_executor import SubprocessAgentExecutor  # noqa: E402


class _SessionShim(SubprocessAgentExecutor):
    BIN = "/bin/sh"
    USE_SHELL = False
    SESSION_ID_FLAG = "--session-id"
    RESUME_FLAG = "--resume"


class _Updater:
    def __init__(self) -> None:
        self.states = []
        self.artifacts = []

    async def update_status(self, state=None, message=None) -> None:
        self.states.append((state, message))

    async def add_artifact(self, parts=None) -> None:
        self.artifacts.append(parts)


def _context(task_id: str, query: str, metadata: dict[str, str]):
    return SimpleNamespace(
        current_task=SimpleNamespace(id=task_id, context_id="a2a-context"),
        message=query,
        metadata=metadata,
    )


@unittest.skipIf(os.name == "nt", "POSIX 专用 shim")
class TestSessionContinuity(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.args_log = self.root / "args.log"
        self.events_log = self.root / "events.log"
        self.fail_resume = self.root / "fail-resume"
        self.sleep_marker = self.root / "sleep"
        self.shim = self.root / "shim.sh"
        self.shim.write_text(
            """args_log=$1
events_log=$2
fail_resume=$3
sleep_marker=$4
shift 4
for arg in "$@"; do printf '[%s]' "$arg"; done >> "$args_log"
printf '\n' >> "$args_log"
printf 'start\n' >> "$events_log"
if [ "${1-}" = "--resume" ] && [ -f "$fail_resume" ]; then
    printf 'end\n' >> "$events_log"
    failure=$(cat "$fail_resume")
    if [ "$failure" = "session" ]; then
        printf 'SeSsIoN NoT FoUnD\n' >&2
        exit 9
    fi
    if [ "$failure" = "timeout" ]; then
        sleep 5
        exit 9
    fi
    printf 'permission denied\n' >&2
    exit 1
fi
if [ -f "$sleep_marker" ]; then sleep 0.2; fi
printf 'end\n' >> "$events_log"
printf 'ok\n'
""",
            encoding="utf-8",
        )
        _SessionShim.ARGS_PREFIX = [
            str(self.shim),
            str(self.args_log),
            str(self.events_log),
            str(self.fail_resume),
            str(self.sleep_marker),
        ]
        _SessionShim.SESSION_DB_DIR = self.root / "data"
        _SessionShim.TIMEOUT = 600
        with SubprocessAgentExecutor._context_locks_lock:
            SubprocessAgentExecutor._context_locks.clear()

    def tearDown(self) -> None:
        with SubprocessAgentExecutor._context_locks_lock:
            SubprocessAgentExecutor._context_locks.clear()
        self.temp_dir.cleanup()

    @staticmethod
    def _reporting_patch(updater: _Updater):
        return patch.multiple(
            se,
            TaskUpdater=lambda **_kw: updater,
            get_message_text=lambda message: message,
            new_text_message=lambda text: text,
            new_text_part=lambda text: text,
        )

    def _execute(
        self, task_id: str, query: str, metadata: dict[str, str]
    ) -> _Updater:
        updater = _Updater()
        with self._reporting_patch(updater):
            asyncio.run(
                _SessionShim().execute(
                    _context(task_id, query, metadata), object()
                )
            )
        return updater

    def _lines(self, path: Path) -> list[str]:
        return path.read_text(encoding="utf-8").splitlines()

    def _stored_id(self, context_id: str) -> tuple[str, float]:
        with sqlite3.connect(_SessionShim._session_db_path()) as db:
            row = db.execute(
                "SELECT native_id, updated FROM sessions WHERE context = ?",
                (context_id,),
            ).fetchone()
        self.assertIsNotNone(row)
        return row

    def test_without_context_keeps_original_invocation(self):
        updater = self._execute("plain-task", "plain-query", {})

        self.assertEqual(self._lines(self.args_log), ["[plain-query]"])
        self.assertFalse(_SessionShim._session_db_path().exists())
        self.assertEqual(updater.artifacts, [["ok"]])

    def test_new_context_generates_and_stores_native_id(self):
        self._execute("new-task", "first", {"context": "project_1"})

        line = self._lines(self.args_log)[0]
        match = re.fullmatch(r"\[--session-id\]\[([0-9a-f-]{36})\]\[first\]", line)
        self.assertIsNotNone(match)
        native_id, updated = self._stored_id("project_1")
        self.assertEqual(native_id, match.group(1))
        self.assertGreater(updated, 0)

    def test_second_call_resumes_stored_native_id(self):
        self._execute("first-task", "first", {"context": "project-2"})
        native_id, _ = self._stored_id("project-2")
        self._execute("second-task", "second", {"context": "project-2"})

        self.assertEqual(
            self._lines(self.args_log)[1],
            f"[--resume][{native_id}][second]",
        )

    def test_session_not_found_retries_once_with_fresh_session(self):
        self._execute("first-task", "first", {"context": "project-3"})
        old_id, _ = self._stored_id("project-3")
        self.fail_resume.write_text("session", encoding="utf-8")

        updater = self._execute("retry-task", "retry", {"context": "project-3"})

        lines = self._lines(self.args_log)
        self.assertEqual(lines[1], f"[--resume][{old_id}][retry]")
        match = re.fullmatch(
            r"\[--session-id\]\[([0-9a-f-]{36})\]\[retry\]", lines[2]
        )
        self.assertIsNotNone(match)
        new_id, _ = self._stored_id("project-3")
        self.assertEqual(new_id, match.group(1))
        self.assertNotEqual(new_id, old_id)
        self.assertEqual(updater.states[-1][0], se.TaskState.TASK_STATE_COMPLETED)

    def test_ordinary_nonzero_resume_failure_does_not_retry(self):
        self._execute("first-task", "first", {"context": "ordinary"})
        old_id, _ = self._stored_id("ordinary")
        self.fail_resume.write_text("ordinary", encoding="utf-8")

        updater = self._execute(
            "ordinary-failure-task", "retry", {"context": "ordinary"}
        )

        lines = self._lines(self.args_log)
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[1], f"[--resume][{old_id}][retry]")
        self.assertEqual(self._stored_id("ordinary")[0], old_id)
        self.assertEqual(updater.states[-1][0], se.TaskState.TASK_STATE_FAILED)
        self.assertIn("permission denied", updater.artifacts[-1][0])

    def test_resume_timeout_does_not_retry(self):
        self._execute("first-task", "first", {"context": "timeout"})
        old_id, _ = self._stored_id("timeout")
        self.fail_resume.write_text("timeout", encoding="utf-8")
        _SessionShim.TIMEOUT = 0.05

        updater = self._execute(
            "timeout-task", "retry", {"context": "timeout"}
        )

        self.assertEqual(len(self._lines(self.args_log)), 2)
        self.assertEqual(
            self._lines(self.args_log)[1], f"[--resume][{old_id}][retry]"
        )
        self.assertEqual(self._stored_id("timeout")[0], old_id)
        self.assertEqual(updater.states[-1][0], se.TaskState.TASK_STATE_FAILED)
        self.assertIn("调用超时", updater.artifacts[-1][0])

    def test_fresh_context_skips_resume_and_overwrites_db(self):
        self._execute("first-task", "first", {"context": "project-4"})
        old_id, _ = self._stored_id("project-4")
        self._execute(
            "reset-task",
            "reset",
            {"context": "project-4", "contextReset": "1"},
        )

        line = self._lines(self.args_log)[1]
        match = re.fullmatch(
            r"\[--session-id\]\[([0-9a-f-]{36})\]\[reset\]", line
        )
        self.assertIsNotNone(match)
        new_id, _ = self._stored_id("project-4")
        self.assertEqual(new_id, match.group(1))
        self.assertNotEqual(new_id, old_id)

    def test_concurrent_calls_for_same_context_are_serialized(self):
        self.sleep_marker.touch()
        updater = _Updater()

        async def scenario() -> None:
            executor = _SessionShim()
            await asyncio.gather(
                executor.execute(
                    _context("concurrent-1", "one", {"context": "shared"}),
                    object(),
                ),
                executor.execute(
                    _context("concurrent-2", "two", {"context": "shared"}),
                    object(),
                ),
            )

        with self._reporting_patch(updater):
            asyncio.run(scenario())

        self.assertEqual(
            self._lines(self.events_log), ["start", "end", "start", "end"]
        )
        lines = self._lines(self.args_log)
        first = re.fullmatch(
            r"\[--session-id\]\[([0-9a-f-]{36})\]\[(one|two)\]", lines[0]
        )
        self.assertIsNotNone(first)
        self.assertRegex(
            lines[1], rf"^\[--resume\]\[{first.group(1)}\]\[(one|two)\]$"
        )

    def test_invalid_context_is_rejected_without_starting_child(self):
        updater = self._execute(
            "invalid-task", "ignored", {"context": "bad/context"}
        )

        self.assertFalse(self.args_log.exists())
        self.assertEqual(updater.states[-1][0], se.TaskState.TASK_STATE_FAILED)
        self.assertIn("context 无效", updater.artifacts[0][0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
