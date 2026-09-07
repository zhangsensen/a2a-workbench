import unittest

try:
    from .planning import extract_json_object, validate_plan
except ImportError:  # direct unittest discovery from grid/
    from planning import extract_json_object, validate_plan


class PlanningTests(unittest.TestCase):
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

    def test_rejects_dependency_cycle(self):
        plan = {
            "tasks": [
                {"id": "T1", "owner": "pi", "task": "检查 A", "depends_on": ["T2"]},
                {"id": "T2", "owner": "codex", "task": "检查 B", "depends_on": ["T1"]},
            ]
        }
        with self.assertRaisesRegex(ValueError, "任务依赖存在环"):
            validate_plan(plan, ["pi", "codex"])

    def test_allows_transitively_serialized_writes_to_same_scope(self):
        plan = {
            "summary": "串行修改",
            "tasks": [
                {"id": "T1", "owner": "pi", "task": "改 A", "write_scope": "repo:x"},
                {"id": "T2", "owner": "codex", "task": "检查 A", "depends_on": ["T1"]},
                {
                    "id": "T3",
                    "owner": "pi",
                    "task": "改 B",
                    "mode": "EXECUTE",
                    "write_scope": "repo:x",
                    "depends_on": ["T2"],
                },
            ],
        }

        normalized = validate_plan(plan, ["pi", "codex"])

        self.assertEqual(normalized["summary"], "串行修改")
        self.assertEqual(normalized["tasks"][2]["mode"], "execute")


if __name__ == "__main__":
    unittest.main()
