"""requestId persistence and A2A handler deduplication regressions."""
import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from a2a.helpers import new_text_message  # noqa: E402
from a2a.server.request_handlers import DefaultRequestHandler  # noqa: E402
from a2a.types import Role, SendMessageRequest, Task, TaskState, TaskStatus  # noqa: E402

from common import idempotency  # noqa: E402
from common.dedup_handler import DedupRequestHandler  # noqa: E402
from common.idempotency import RequestDedup  # noqa: E402


class TestRequestDedup(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp_dir.name)
        self.data_patch = patch.object(idempotency, "DATA_DIR", self.data_dir)
        self.data_patch.start()

    def tearDown(self):
        self.data_patch.stop()
        self.temp_dir.cleanup()

    def test_lookup_remember_and_replace(self):
        dedup = RequestDedup("test")
        self.assertIsNone(dedup.lookup("req-1"))
        dedup.remember("req-1", "task-1")
        self.assertEqual(dedup.lookup("req-1"), "task-1")
        dedup.remember("req-1", "task-2")
        self.assertEqual(dedup.lookup("req-1"), "task-2")

    def test_invalid_request_ids_are_ignored(self):
        dedup = RequestDedup("test")
        invalid = ["", "has space", "../escape", "x" * 101, None, 123]
        for request_id in invalid:
            with self.subTest(request_id=request_id):
                dedup.remember(request_id, "task-bad")
                self.assertIsNone(dedup.lookup(request_id))


class FakeTaskStore:
    def __init__(self):
        self.tasks = {}

    async def get(self, task_id, _context):
        return self.tasks.get(task_id)


class TestDedupRequestHandler(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_patch = patch.object(idempotency, "DATA_DIR", Path(self.temp_dir.name))
        self.data_patch.start()
        self.store = FakeTaskStore()
        self.handler = object.__new__(DedupRequestHandler)
        self.handler.task_store = self.store
        self.handler.request_dedup = RequestDedup("handler")
        self.executions = 0

    def tearDown(self):
        self.data_patch.stop()
        self.temp_dir.cleanup()

    @staticmethod
    def request(request_id=None):
        metadata = {"requestId": request_id} if request_id is not None else None
        return SendMessageRequest(
            message=new_text_message("work", role=Role.ROLE_USER),
            metadata=metadata,
        )

    def task(self, task_id):
        return Task(
            id=task_id,
            context_id="ctx",
            status=TaskStatus(state=TaskState.TASK_STATE_COMPLETED),
        )

    def send(self, request):
        async def fake_parent_send(_handler, _params, _context):
            self.executions += 1
            task = self.task(f"task-{self.executions}")
            self.store.tasks[task.id] = task
            return task

        with patch.object(
            DefaultRequestHandler, "on_message_send", new=fake_parent_send
        ):
            return asyncio.run(self.handler.on_message_send(request, object()))

    def test_same_request_id_returns_existing_task_without_second_execution(self):
        first = self.send(self.request("req-1"))
        second = self.send(self.request("req-1"))
        self.assertEqual(first.id, second.id)
        self.assertEqual(self.executions, 1)

    def test_request_without_id_is_not_deduplicated(self):
        first = self.send(self.request())
        second = self.send(self.request())
        self.assertNotEqual(first.id, second.id)
        self.assertEqual(self.executions, 2)

    def test_missing_original_task_runs_again_and_replaces_mapping(self):
        first = self.send(self.request("req-stale"))
        del self.store.tasks[first.id]
        second = self.send(self.request("req-stale"))
        self.assertNotEqual(first.id, second.id)
        self.assertEqual(self.executions, 2)
        self.assertEqual(self.handler.request_dedup.lookup("req-stale"), second.id)


if __name__ == "__main__":
    unittest.main(verbosity=2)
