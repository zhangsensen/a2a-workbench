"""dispatch 收口工具回归：worktree 隔离、终态区分、证据来自 git 而非回复文本。"""
import asyncio
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

POSIX_ONLY = unittest.skipIf(os.name == "nt", "verify 用例依赖 /bin/sh（与 test_cancel 同为 POSIX 专用）")

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from a2a_call import AgentTaskFailed  # noqa: E402
from dispatch import init_tasks, load_tasks, main, run_dispatch  # noqa: E402


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
        full_failure = "远端失败详情：" + "x" * 900

        async def fake_caller(agent, prompt, cwd=None, **_kw):
            tree = Path(cwd)
            if "t-fail" in prompt:
                # agent 失败前已经留下未提交的部分工作（新文件），失败分支
                # 也不该把它丢掉。
                (tree / "partial.txt").write_text("partial-work\n", encoding="utf-8")
                raise AgentTaskFailed("FAILED", full_failure)
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
        self.assertEqual(Path(by["t-fail"]["log"]).read_text(encoding="utf-8").rstrip("\n"),
                         full_failure)
        # 失败分支也要收集证据：未提交的新文件正文要进 patch，不能因为
        # 任务失败就把 agent 已完成的部分工作丢掉。
        fail_patch = Path(by["t-fail"]["patch"]).read_text(encoding="utf-8")
        self.assertIn("+partial-work", fail_patch)

        # 空口声明"完成"但 git 无任何改动 → no-change，不是 ok。
        self.assertEqual(by["t-noop"]["state"], "no-change")

        # 现场默认清理，补丁保留（含失败分支的补丁）。
        self.assertFalse(list(self.out.glob("work-*")))
        self.assertTrue(list(self.out.glob("*-t-ok.patch")))
        self.assertTrue(list(self.out.glob("*-t-fail.patch")))

    @POSIX_ONLY
    def test_verified_writes_machine_evidence(self):
        async def fake_caller(_agent, _prompt, cwd=None, **_kw):
            tree = Path(cwd)
            (tree / "b.txt").write_text("done\n", encoding="utf-8")
            _git(tree, "add", "-A")
            _git(tree, "commit", "-q", "-m", "verified: add b")
            return "done with evidence"

        tasks = [{
            "agent": "codex",
            "name": "verified",
            "task": "写文件并验收",
            "verify": [{"type": "command", "argv": ["/bin/sh", "-c", "test -f b.txt"]}],
        }]
        row = asyncio.run(
            run_dispatch(self.repo, tasks, self.out, keep=False, caller=fake_caller)
        )[0]

        self.assertEqual(row["state"], "verified")
        evidence = json.loads(Path(row["evidence"]).read_text(encoding="utf-8"))
        self.assertTrue(evidence[0]["passed"])
        self.assertEqual(evidence[0]["type"], "command")

    @POSIX_ONLY
    def test_refuted_writes_evidence_and_log(self):
        reply = "agent reply tail for failed verification"

        async def fake_caller(_agent, _prompt, cwd=None, **_kw):
            tree = Path(cwd)
            (tree / "b.txt").write_text("done\n", encoding="utf-8")
            _git(tree, "add", "-A")
            _git(tree, "commit", "-q", "-m", "refuted: add b")
            return reply

        tasks = [{
            "agent": "codex",
            "name": "refuted",
            "task": "制造验收失败",
            "verify": [{"type": "command", "argv": ["/bin/sh", "-c", "exit 9"]}],
        }]
        row = asyncio.run(
            run_dispatch(self.repo, tasks, self.out, keep=False, caller=fake_caller)
        )[0]

        self.assertEqual(row["state"], "refuted")
        evidence = json.loads(Path(row["evidence"]).read_text(encoding="utf-8"))
        self.assertFalse(evidence[0]["passed"])
        self.assertEqual(Path(row["log"]).read_text(encoding="utf-8").strip(), reply)

    def test_inspect_mode_can_verify_an_unchanged_worktree(self):
        # inspect 模式本就是"检查现状"，不要求改动，verify 全过即 verified。
        async def fake_caller(_agent, _prompt, cwd=None, **_kw):
            return "existing repository state is already correct"

        tasks = [{
            "agent": "codex",
            "name": "already-good",
            "task": "验证已有文件",
            "mode": "inspect",
            "verify": [{"type": "file", "path": "a.txt"}],
        }]
        row = asyncio.run(
            run_dispatch(self.repo, tasks, self.out, keep=False, caller=fake_caller)
        )[0]

        self.assertEqual(row["state"], "verified")

    def test_modify_mode_without_change_is_no_change_even_if_verified(self):
        # modify 模式（默认）：要求实现功能，但 agent 什么都没改，verify 只是
        # 查到了本来就存在的文件 → 不能算 verified，必须是 no-change。
        async def fake_caller(_agent, _prompt, cwd=None, **_kw):
            return "existing repository state is already correct"

        tasks = [{
            "agent": "codex",
            "name": "modify-noop",
            "task": "实现一个功能",
            "verify": [{"type": "file", "path": "a.txt"}],
        }]
        row = asyncio.run(
            run_dispatch(self.repo, tasks, self.out, keep=False, caller=fake_caller)
        )[0]

        self.assertEqual(row["state"], "no-change")
        # 机器验收证据依然写盘，只是不给 verified 判定。
        evidence = json.loads(Path(row["evidence"]).read_text(encoding="utf-8"))
        self.assertTrue(evidence[0]["passed"])

    def test_check_mode_only_validates_tasks(self):
        tasks_path = Path(self.dir.name) / "tasks.json"
        tasks_path.write_text(
            json.dumps([{
                "agent": "codex",
                "name": "check-me",
                "task": "校验配置",
                "verify": [{"type": "file", "path": "result.txt"}],
            }]),
            encoding="utf-8",
        )

        self.assertEqual(load_tasks(tasks_path)[0]["name"], "check-me")
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main([str(tasks_path), "--check"]), 0)
        self.assertEqual(output.getvalue().strip(), "OK")
        self.assertFalse(self.out.exists())

    def test_init_generates_loadable_skeleton(self):
        tasks_path = Path(self.dir.name) / "new" / "tasks.json"
        init_tasks(tasks_path)
        tasks = load_tasks(tasks_path)
        self.assertIn("verify", tasks[0])
        self.assertIn("_comment", tasks[0])

    def test_bad_tasks_rejected(self):
        bad = Path(self.dir.name) / "tasks.json"
        bad.write_text('[{"agent":"nobody","name":"x","task":"y"}]', encoding="utf-8")
        with self.assertRaises(SystemExit):
            load_tasks(bad)

    def test_bad_mode_rejected(self):
        bad = Path(self.dir.name) / "tasks.json"
        bad.write_text(
            '[{"agent":"codex","name":"x","task":"y","mode":"delete-everything"}]',
            encoding="utf-8",
        )
        with self.assertRaises(SystemExit):
            load_tasks(bad)


if __name__ == "__main__":
    unittest.main(verbosity=2)
