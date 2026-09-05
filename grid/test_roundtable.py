import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from roundtable import RoundTable, extract_json_object, validate_plan


class RoundTableTests(unittest.TestCase):
    def test_extract_fenced_json(self):
        value = extract_json_object('```json\n{"summary":"含 } 字符", "tasks": []}\n```')
        self.assertEqual(value["summary"], "含 } 字符")

    def test_rejects_parallel_writes_to_same_scope(self):
        plan = {
            "tasks": [
                {"id": "T1", "owner": "pi", "task": "改 A", "mode": "execute", "write_scope": "repo:x"},
                {"id": "T2", "owner": "codex", "task": "改 B", "mode": "execute", "write_scope": "repo:x"},
            ]
        }
        with self.assertRaisesRegex(ValueError, "必须用依赖串行"):
            validate_plan(plan, ["pi", "codex"])

    def test_full_roundtable_flow(self):
        calls = []

        async def fake_call(agent, prompt):
            calls.append((agent, prompt))
            if "只返回 JSON" in prompt:
                return json.dumps(
                    {
                        "summary": "两人并行只读检查",
                        "tasks": [
                            {"id": "T1", "owner": "pi", "task": "检查一", "mode": "analyze", "write_scope": "", "depends_on": [], "done_when": "返回一"},
                            {"id": "T2", "owner": "codex", "task": "检查二", "mode": "analyze", "write_scope": "", "depends_on": [], "done_when": "返回二"},
                        ],
                    },
                    ensure_ascii=False,
                )
            if "最终收口" in prompt:
                return "圆桌完成"
            if "交叉复核人" in prompt:
                return "通过"
            return f"{agent} 已完成"

        with tempfile.TemporaryDirectory() as temp:
            table = RoundTable(
                "验证圆桌",
                "claude",
                ["pi", "codex"],
                caller=fake_call,
                state_dir=Path(temp),
            )
            result = asyncio.run(table.run())
            saved = json.loads(table.state_path.read_text(encoding="utf-8"))

        self.assertEqual(result, "圆桌完成")
        self.assertEqual(saved["status"], "completed")
        self.assertEqual(set(saved["results"]), {"T1", "T2"})
        self.assertEqual(set(saved["reviews"]), {"T1", "T2"})
        self.assertEqual(len(calls), 6)


if __name__ == "__main__":
    unittest.main()
