"""a2a_call 终态传播回归：只有 COMPLETED 算成功，其余状态分级退出码。

审计实锤的原始缺陷：服务端把任务诚实落成 FAILED，客户端却只拼 artifact 文本、
打印错误后 exit 0——master 的自动编排把错误文本当成功结果继续用。

第二轮加固（成功白名单）：原先用"FAILED/CANCELED 才算失败"的黑名单，
REJECTED / INPUT_REQUIRED / AUTH_REQUIRED 以及协议不完整时的 WORKING/None
都会被当成成功。白名单让未知状态默认失败，未来 SDK 新增状态也不会静默放行。
"""
import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from a2a.types import TaskState  # noqa: E402

from a2a_call import AgentTaskFailed, finalize_reply, state_name_and_exit  # noqa: E402


class TestOnlyCompletedIsSuccess(unittest.TestCase):
    def test_completed_returns_text(self):
        self.assertEqual(
            finalize_reply(TaskState.TASK_STATE_COMPLETED, ["答案"]), "答案"
        )

    def test_every_other_enum_value_raises(self):
        """枚举全集覆盖：除 COMPLETED 外，任一取值都必须抛异常。"""
        for name in TaskState.keys():
            value = TaskState.Value(name)
            if value == TaskState.TASK_STATE_COMPLETED:
                continue
            with self.subTest(state=name):
                with self.assertRaises(AgentTaskFailed):
                    finalize_reply(value, ["部分输出"])

    def test_missing_state_raises(self):
        with self.assertRaises(AgentTaskFailed) as caught:
            finalize_reply(None, [])
        self.assertEqual(caught.exception.state, "NO_TERMINAL_STATE")
        self.assertEqual(caught.exception.exit_code, 6)


class TestExitCodeMapping(unittest.TestCase):
    EXPECTED = {
        TaskState.TASK_STATE_FAILED: ("FAILED", 1),
        TaskState.TASK_STATE_CANCELED: ("CANCELED", 2),
        TaskState.TASK_STATE_REJECTED: ("REJECTED", 3),
        TaskState.TASK_STATE_INPUT_REQUIRED: ("INPUT_REQUIRED", 4),
        TaskState.TASK_STATE_AUTH_REQUIRED: ("AUTH_REQUIRED", 5),
    }

    def test_terminal_states_have_distinct_exit_codes(self):
        for state, expected in self.EXPECTED.items():
            with self.subTest(state=expected[0]):
                self.assertEqual(state_name_and_exit(state), expected)

    def test_failure_carries_state_and_exit_code(self):
        with self.assertRaises(AgentTaskFailed) as caught:
            finalize_reply(TaskState.TASK_STATE_FAILED, ["(cwd 无效: 目录不存在)"])
        self.assertEqual(caught.exception.state, "FAILED")
        self.assertEqual(caught.exception.exit_code, 1)
        self.assertIn("cwd 无效", caught.exception.text)

    def test_non_terminal_states_all_map_to_six(self):
        """WORKING/SUBMITTED/UNSPECIFIED 不是终态：拿不到结论就是基础设施问题。"""
        for name in ("TASK_STATE_WORKING", "TASK_STATE_SUBMITTED", "TASK_STATE_UNSPECIFIED"):
            with self.subTest(state=name):
                readable, code = state_name_and_exit(TaskState.Value(name))
                self.assertEqual(code, 6)
                self.assertEqual(readable, name)

    def test_unknown_future_value_defaults_to_six(self):
        # SDK 将来新增的取值不得被当成成功，也不得撞进已有退出码。
        readable, code = state_name_and_exit(9999)
        self.assertEqual(code, 6)
        self.assertTrue(readable)


if __name__ == "__main__":
    unittest.main(verbosity=2)
