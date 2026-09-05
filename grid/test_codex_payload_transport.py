import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "codex"))

from codex.executor import CodexExecutor, MAX_CODEX_INPUT_BYTES


class CodexPayloadTransportTests(unittest.TestCase):
    def test_exact_256k_utf8_payload_is_not_truncated(self):
        executor = CodexExecutor()
        payload = "x" * MAX_CODEX_INPUT_BYTES
        self.assertEqual(executor._sanitize(payload), payload)

    def test_over_limit_is_rejected_not_silently_truncated(self):
        executor = CodexExecutor()
        with self.assertRaisesRegex(ValueError, "exceeds"):
            executor._sanitize("x" * (MAX_CODEX_INPUT_BYTES + 1))

    def test_control_characters_are_removed_without_utf8_damage(self):
        executor = CodexExecutor()
        self.assertEqual(executor._sanitize("前🙂\x00\x01\t\r\n后"), "前🙂\t\n后")

    def test_codex_exec_uses_explicit_stdin_and_exact_utf8_bytes(self):
        """Unchanged contract, now asserted on Popen.

        The shared executor moved from ``subprocess.run`` to
        ``Popen`` + ``communicate(timeout=...)`` so a timeout can taskkill the
        whole process tree (SENY-162 #4). The transport guarantees this test
        exists for are identical: the prompt travels on stdin as exact UTF-8
        bytes and never appears on the command line.
        """
        executor = CodexExecutor()
        payload = "BEGIN🙂\nMID\nEND"

        class FakePopen:
            def __init__(self):
                self.pid = 1234
                self.stdin = None
                self.stdout = None
                self.stderr = None
                self.sent = None
                self.timeout = None
                self.returncode = 0

            def communicate(self, input=None, timeout=None):  # noqa: A002
                self.sent = input
                self.timeout = timeout
                return b"ok", b""

        fake = FakePopen()
        with patch("subprocess_executor.subprocess.Popen", return_value=fake) as popen:
            self.assertEqual(executor._run(payload), "ok")
        command = popen.call_args.args[0]
        self.assertIn("exec", command)
        self.assertTrue(command.rstrip().endswith(" -"), command)
        self.assertNotIn(payload, command)
        self.assertEqual(popen.call_args.kwargs["stdin"], subprocess.PIPE)
        self.assertEqual(fake.sent, payload.encode("utf-8"))
        self.assertEqual(fake.timeout, CodexExecutor.TIMEOUT)


if __name__ == "__main__":
    unittest.main()
