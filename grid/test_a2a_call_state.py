"""a2a_call 状态传播回归：服务端 FAILED/CANCELED 必须传播为客户端异常与非零退出码。

审计实锤的缺陷：服务端把任务诚实落成 FAILED，客户端却只拼 artifact 文本、
打印错误后 exit 0——master 的自动编排把错误文本当成功结果继续用。
"""
import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from a2a.types import TaskState  # noqa: E402

from a2a_call import AgentTaskFailed, finalize_reply  # noqa: E402


class TestFinalizeReply(unittest.TestCase):
    def test_completed_returns_text(self):
        self.assertEqual(
            finalize_reply(TaskState.TASK_STATE_COMPLETED, ["答案"]), "答案"
        )

    def test_failed_raises_with_reason(self):
        with self.assertRaises(AgentTaskFailed) as caught:
            finalize_reply(TaskState.TASK_STATE_FAILED, ["(cwd 无效: 目录不存在)"])
        self.assertEqual(caught.exception.state, "FAILED")
        self.assertIn("cwd 无效", caught.exception.text)

    def test_canceled_raises(self):
        with self.assertRaises(AgentTaskFailed) as caught:
            finalize_reply(TaskState.TASK_STATE_CANCELED, [])
        self.assertEqual(caught.exception.state, "CANCELED")

    def test_unknown_state_keeps_text_behavior(self):
        # 无终态信息（如纯消息回复）保持旧行为：返回文本，不误报失败。
        self.assertEqual(finalize_reply(None, []), "(无文本回复)")


if __name__ == "__main__":
    unittest.main(verbosity=2)
