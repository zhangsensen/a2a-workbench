"""dispatch 收口工具回归：worktree 隔离、终态区分、证据来自 git 而非回复文本。"""
import asyncio
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from a2a_call import AgentTaskFailed  # noqa: E402
from dispatch import load_tasks, run_dispatch  # noqa: E402


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *args],
                   check=True, capture_output=True, timeout=60)


class TestDispatch(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.repo = Path(self.dir.name) / "repo"
        self.repo.mkdir()
        _git(Path(self.dir.name), "init", "-q", str(self.repo))
        (self.repo / "a.txt").write_text("v1\n", encoding="utf-8")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "init")
        self.out = Path(self.dir.name) / "out"

    def tearDown(self):
        self.dir.cleanup()

    def test_ok_failed_and_nochange_are_distinguished(self):
        async def fake_caller(agent, prompt, cwd=None, **_kw):
            tree = Path(cwd)
            if "t-fail" in prompt:
                raise AgentTaskFailed("FAILED", "(cwd 无效: ...)")
            if "t-noop" in prompt:
                return "我声称做完了但什么都没改"  # 声明不算数
            (tree / "b.txt").write_text("done\n", encoding="utf-8")
            _git(tree, "add", "-A")
            _git(tree, "commit", "-q", "-m", "t-ok: add b")
            return "done\n证据：已提交"

        tasks = [
            {"agent": "codex", "name": "t-ok", "task": "写文件"},
            {"agent": "dsh", "name": "t-fail", "task": "t-fail 场景"},
            {"agent": "claude", "name": "t-noop", "task": "t-noop 场景"},
        ]
        rows = asyncio.run(run_dispatch(self.repo, tasks, self.out, keep=False, caller=fake_caller))
        by = {r["name"]: r for r in rows}

        self.assertEqual(by["t-ok"]["state"], "ok")
        self.assertIn("t-ok: add b", by["t-ok"]["commit"])
        patch = Path(by["t-ok"]["patch"]).read_text(encoding="utf-8")
        self.assertIn("+done", patch)

        self.assertEqual(by["t-fail"]["state"], "failed")
        self.assertIn("FAILED", by["t-fail"]["detail"])

        # 空口声明"完成"但 git 无任何改动 → no-change，不是 ok。
        self.assertEqual(by["t-noop"]["state"], "no-change")

        # 现场默认清理，补丁保留。
        self.assertFalse(list(self.out.glob("work-*")))
        self.assertTrue(list(self.out.glob("*-t-ok.patch")))

    def test_bad_tasks_rejected(self):
        bad = Path(self.dir.name) / "tasks.json"
        bad.write_text('[{"agent":"nobody","name":"x","task":"y"}]', encoding="utf-8")
        with self.assertRaises(SystemExit):
            load_tasks(bad)


if __name__ == "__main__":
    unittest.main(verbosity=2)
