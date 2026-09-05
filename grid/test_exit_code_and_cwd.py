"""退出码与 cwd 元数据回归：

1. 非零退出码必须是 FAILED，即使 stdout 有内容 —— 旧实现从不检查
   ``proc.returncode``，CLI 报错退出时只要有半截 stdout 就被当成完整答案
   标 COMPLETED，调用方无从分辨。
2. cwd 元数据把执行钉到指定工作区（并行 worktree 派活的基础）；
   给了非法 cwd 必须响亮拒绝，不允许静默落回默认目录执行 ——
   "在错误的地方动手"比"拒绝执行"危险得多。
"""
import os
import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from subprocess_executor import (  # noqa: E402
    ExecutorFailure,
    SubprocessAgentExecutor,
)


class _FailingShim(SubprocessAgentExecutor):
    """stdout 有内容但退出码非零：必须按失败处理。"""

    BIN = "cmd.exe"
    ARGS_PREFIX = ["/c", "echo partial-output && exit /b 3"]
    USE_SHELL = True
    QUERY_VIA_STDIN = False
    TIMEOUT = 30


class _PwdShim(SubprocessAgentExecutor):
    """打印自身工作目录，验证 cwd 真正生效。query 走 stdin 免污染 argv。"""

    BIN = "cmd.exe"
    ARGS_PREFIX = ["/c", "cd"]
    USE_SHELL = True
    QUERY_VIA_STDIN = True
    TIMEOUT = 30


@unittest.skipUnless(os.name == "nt", "Windows 专用 shim")
class TestNonZeroExitIsFailure(unittest.TestCase):
    def test_nonzero_exit_raises_with_both_streams(self):
        with self.assertRaises(ExecutorFailure) as caught:
            _FailingShim()._run("ignored")
        self.assertIn("退出码 3", caught.exception.text)
        self.assertIn("partial-output", caught.exception.text)

    def test_zero_exit_still_succeeds(self):
        class _OkShim(_FailingShim):
            ARGS_PREFIX = ["/c", "echo fine"]

        self.assertIn("fine", _OkShim()._run("ignored"))


@unittest.skipUnless(os.name == "nt", "Windows 专用 shim")
class TestCwdPinning(unittest.TestCase):
    def test_valid_cwd_changes_working_directory(self):
        target = str(Path(__file__).resolve().parent.parent)
        out = _PwdShim()._run("ignored", cwd_request=target)
        self.assertEqual(out.strip().lower(), target.lower())

    def test_absent_cwd_keeps_default(self):
        out = _PwdShim()._run("ignored")
        self.assertTrue(out.strip())  # 有输出即可，默认目录由服务决定

    def test_missing_directory_is_rejected_loudly(self):
        with self.assertRaises(ExecutorFailure) as caught:
            _PwdShim()._run("ignored", cwd_request=r"C:\no\such\dir-a2a-test")
        self.assertIn("cwd 无效", caught.exception.text)

    def test_relative_path_is_rejected(self):
        with self.assertRaises(ExecutorFailure) as caught:
            _PwdShim()._run("ignored", cwd_request="relative/dir")
        self.assertIn("cwd 无效", caught.exception.text)

    def test_control_chars_and_leading_dash_rejected(self):
        # 尾部空白由 strip 归一化（合理）；内嵌控制符与前导 - 必须拒绝。
        for bad in ("-C:\\Windows", "C:\\Win\ndows"):
            with self.assertRaises(ExecutorFailure):
                _PwdShim()._run("ignored", cwd_request=bad)

    def test_non_string_cwd_rejected(self):
        with self.assertRaises(ExecutorFailure):
            _PwdShim()._run("ignored", cwd_request=123)


if __name__ == "__main__":
    unittest.main(verbosity=2)
