import io
import subprocess
import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "dsh"))

from dsh.executor import DSHExecutor, MAX_DSH_INPUT_BYTES
from subprocess_executor import ExecutorTimeout


class DshPayloadTransportTests(unittest.TestCase):
    def test_256kb_utf8_payload_is_staged_exactly_and_cleaned(self):
        executor = DSHExecutor()
        payload = "开头🙂\r\n" + ("abc123\n" * 37000) + "末尾"
        self.assertLessEqual(len(payload.encode("utf-8")), MAX_DSH_INPUT_BYTES)
        sanitized = executor._sanitize_dsh(payload)
        with executor._payload_file(sanitized) as path:
            self.assertTrue(path.exists())
            self.assertEqual(path.read_bytes(), payload.encode("utf-8"))
            self.assertTrue(path.name.startswith("dsh-a2a-"))
            self.assertEqual(path.suffix, ".txt")
        self.assertFalse(path.exists())

    def test_control_characters_are_removed_without_utf8_damage(self):
        executor = DSHExecutor()
        self.assertEqual(executor._sanitize_dsh("前🙂\x00\x01\t\r\n后"), "前🙂\t\r\n后")

    def test_exact_limit_is_accepted_and_over_limit_is_rejected(self):
        executor = DSHExecutor()
        exact = "x" * MAX_DSH_INPUT_BYTES
        self.assertEqual(len(executor._sanitize_dsh(exact)), MAX_DSH_INPUT_BYTES)
        with self.assertRaisesRegex(ValueError, "exceeds"):
            executor._sanitize_dsh(exact + "x")

    def test_timeout_path_is_bounded_and_cleans_payload(self):
        executor = DSHExecutor()

        class FakeProcess:
            def __init__(self):
                self.pid = 4242
                self.stdin = None
                self.stdout = io.BytesIO()
                self.stderr = io.BytesIO()
                self.kill_called = False
                self.wait_calls = 0

            def communicate(self, input=None, timeout=None):  # noqa: A002
                raise subprocess.TimeoutExpired("dsh", timeout)

            def wait(self, timeout=None):
                self.wait_calls += 1
                if self.wait_calls == 1:
                    raise subprocess.TimeoutExpired("dsh", timeout)
                return 0

            def kill(self):
                self.kill_called = True

        fake = FakeProcess()
        before = set((ROOT / ".a2a-payloads").glob("dsh-a2a-*.txt"))
        with patch("dsh.executor.subprocess.Popen", return_value=fake), patch.object(
            executor, "_kill_process_tree"
        ) as kill_tree:
            # A timeout is now a terminal FAILURE, not a string that gets
            # shipped to the caller as a completed answer (SENY-162 #4).
            with self.assertRaises(ExecutorTimeout) as caught:
                executor._run("timeout-probe")
        after = set((ROOT / ".a2a-payloads").glob("dsh-a2a-*.txt"))
        self.assertIn("调用超时", caught.exception.text)
        kill_tree.assert_called_once_with(fake)
        self.assertTrue(fake.kill_called)
        self.assertTrue(fake.stdout.closed)
        self.assertTrue(fake.stderr.closed)
        self.assertEqual(before, after)

    def test_concurrent_payload_files_are_unique_and_cleaned(self):
        executor = DSHExecutor()
        workers = 8
        barrier = threading.Barrier(workers)

        def stage(index: int) -> str:
            payload = f"payload-{index}-🙂"
            with executor._payload_file(payload) as path:
                self.assertEqual(path.read_text(encoding="utf-8"), payload)
                barrier.wait(timeout=10)
                self.assertTrue(path.exists())
                return str(path)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            paths = list(pool.map(stage, range(workers)))
        self.assertEqual(len(set(paths)), workers)
        self.assertTrue(all(not Path(path).exists() for path in paths))


if __name__ == "__main__":
    unittest.main()
