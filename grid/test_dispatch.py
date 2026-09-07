"""dispatch 收口工具回归：worktree 隔离、终态区分、证据来自 git 而非回复文本。"""
import asyncio
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import AsyncMock, patch

POSIX_ONLY = unittest.skipIf(os.name == "nt", "verify 用例依赖 /bin/sh（与 test_cancel 同为 POSIX 专用）")

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from a2a_call import AgentTaskFailed  # noqa: E402
from dispatch import build_card, init_tasks, load_tasks, main, run_dispatch  # noqa: E402


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

        self.assertEqual(by["t-ok"]["state"], "delivered")
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
        self.assertTrue(evidence["candidate"][0]["passed"])
        self.assertTrue(evidence["baseline"][0]["passed"])
        self.assertEqual(evidence["candidate"][0]["type"], "command")
        self.assertEqual(evidence["verdict"], "verified")

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
            "verify": [{
                "type": "command",
                "argv": ["/bin/sh", "-c", "echo acceptance-failed >&2; exit 9"],
            }],
        }]
        row = asyncio.run(
            run_dispatch(self.repo, tasks, self.out, keep=False, caller=fake_caller)
        )[0]

        self.assertEqual(row["state"], "refuted")
        evidence = json.loads(Path(row["evidence"]).read_text(encoding="utf-8"))
        self.assertFalse(evidence["candidate"][0]["passed"])
        self.assertIsNone(evidence["baseline"])
        log = Path(row["log"]).read_text(encoding="utf-8")
        self.assertIn("acceptance-failed", log)
        self.assertIn("--- agent 回复摘要 ---", log)
        self.assertIn(reply, log)

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
        self.assertTrue(evidence["candidate"][0]["passed"])
        self.assertTrue(evidence["baseline"][0]["passed"])
        self.assertEqual(evidence["verdict"], "no-change")

    def test_empty_verify_is_rejected_for_modify_and_inspect(self):
        tasks_path = Path(self.dir.name) / "tasks.json"
        for mode in ("modify", "inspect"):
            with self.subTest(mode=mode):
                tasks_path.write_text(json.dumps([{
                    "agent": "codex",
                    "name": f"empty-{mode}",
                    "task": "不能伪装成已验收",
                    "mode": mode,
                    "verify": [],
                }]), encoding="utf-8")
                with self.assertRaisesRegex(SystemExit, "verify 不能为空"):
                    load_tasks(tasks_path)

    def test_protected_paths_are_validated_and_file_checks_are_merged(self):
        tasks_path = Path(self.dir.name) / "tasks.json"
        task = {
            "agent": "codex",
            "name": "protected",
            "task": "冻结验收文件",
            "protected": ["a.txt"],
            "verify": [{"type": "file", "path": "checks/result.txt"}],
        }
        tasks_path.write_text(json.dumps([task]), encoding="utf-8")
        self.assertEqual(load_tasks(tasks_path)[0]["protected"], ["a.txt", "checks/result.txt"])

        task["protected"] = ["../escape"]
        tasks_path.write_text(json.dumps([task]), encoding="utf-8")
        with self.assertRaisesRegex(SystemExit, r"protected\[1\]"):
            load_tasks(tasks_path)

    @POSIX_ONLY
    def test_protected_change_short_circuits_checks(self):
        async def fake_caller(_agent, _prompt, cwd=None, **_kw):
            tree = Path(cwd)
            (tree / "a.txt").write_text("tampered\n", encoding="utf-8")
            _git(tree, "add", "-A")
            _git(tree, "commit", "-q", "-m", "tamper protected")
            return "changed contract"

        tasks = [{
            "agent": "codex",
            "name": "contract-change",
            "task": "不应修改验收基准",
            "protected": ["a.txt"],
            "verify": [{"type": "command", "argv": ["/bin/sh", "-c", "exit 0"]}],
        }]
        with patch("dispatch.run_checks") as mocked_checks:
            row = asyncio.run(
                run_dispatch(self.repo, tasks, self.out, keep=False, caller=fake_caller)
            )[0]

        self.assertEqual(row["state"], "contract-changed")
        mocked_checks.assert_not_called()
        evidence = json.loads(Path(row["evidence"]).read_text(encoding="utf-8"))
        self.assertEqual(evidence["verdict"], "contract-changed")
        self.assertEqual(evidence["candidate"], [])
        self.assertTrue(evidence["protected"][0]["changed"])

    @POSIX_ONLY
    def test_clean_verifier_refutes_unreplayable_validation_tampering(self):
        (self.repo / ".gitignore").write_text("acceptance.sh\n", encoding="utf-8")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "ignore local acceptance override")

        async def fake_caller(_agent, _prompt, cwd=None, **_kw):
            tree = Path(cwd)
            (tree / "feature.txt").write_text("implemented\n", encoding="utf-8")
            # agent 树里把验收脚本改成恒真；该本地忽略文件不会进入可重放 patch。
            (tree / "acceptance.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            _git(tree, "add", "-A")
            _git(tree, "commit", "-q", "-m", "implement with local test override")
            return "candidate says green"

        tasks = [{
            "agent": "codex",
            "name": "clean-verifier",
            "task": "实现并验收",
            "verify": [{
                "type": "command",
                "argv": ["/bin/sh", "-c", "/bin/sh acceptance.sh"],
            }],
        }]
        row = asyncio.run(
            run_dispatch(self.repo, tasks, self.out, keep=False, caller=fake_caller)
        )[0]

        self.assertEqual(row["state"], "refuted")
        evidence = json.loads(Path(row["evidence"]).read_text(encoding="utf-8"))
        self.assertTrue(evidence["candidate"][0]["passed"])
        self.assertFalse(evidence["baseline"][0]["passed"])
        self.assertEqual(evidence["verdict"], "refuted")
        self.assertIn("acceptance.sh", evidence["baseline"][0]["output"])
        log = Path(row["log"]).read_text(encoding="utf-8")
        self.assertIn("acceptance.sh", log)
        self.assertIn("candidate says green", log)

    def test_main_treats_no_change_and_contract_changed_as_failures(self):
        for state in ("no-change", "contract-changed"):
            with self.subTest(state=state):
                rows = [{"name": "x", "agent": "codex", "state": state, "detail": state}]
                with patch("dispatch.load_tasks", return_value=[]), patch(
                    "dispatch.run_dispatch", new=AsyncMock(return_value=rows),
                ), redirect_stdout(io.StringIO()):
                    self.assertEqual(main(["repo", "tasks.json"]), 1)

    def test_contract_file_is_written_and_digest_is_stable(self):
        async def fake_caller(_agent, _prompt, cwd=None, **_kw):
            tree = Path(cwd)
            (tree / "b.txt").write_text("done\n", encoding="utf-8")
            return "done"

        tasks = [{
            "agent": "codex",
            "name": "stable-contract",
            "task": "写文件",
            "protected": ["a.txt"],
        }]
        digests = []
        for suffix in ("one", "two"):
            out = Path(self.dir.name) / suffix
            row = asyncio.run(run_dispatch(self.repo, tasks, out, keep=False, caller=fake_caller))[0]
            self.assertEqual(row["state"], "delivered")
            self.assertFalse(list(out.glob("*-evidence.json")))
            contract = json.loads(next(out.glob("*-contract.json")).read_text(encoding="utf-8"))
            frozen = contract["tasks"][0]
            self.assertEqual(frozen["protected_files"][0]["path"], "a.txt")
            self.assertNotEqual(frozen["protected_files"][0]["sha256"], "absent")
            canonical = json.dumps(
                frozen["contract"], sort_keys=True, ensure_ascii=False,
            ).encode("utf-8")
            self.assertEqual(frozen["contract_digest"], hashlib.sha256(canonical).hexdigest())
            digests.append(frozen["contract_digest"])
        self.assertEqual(digests[0], digests[1])

    def test_run_report_contains_task_and_run_metrics(self):
        async def fake_caller(_agent, _prompt, cwd=None, **_kw):
            (Path(cwd) / "b.txt").write_text("done\n", encoding="utf-8")
            return "done"

        tasks = [{
            "agent": "codex",
            "name": "metrics",
            "task": "写文件并记录度量",
            "verify": [{
                "type": "command",
                "argv": [sys.executable, "-c", "from pathlib import Path; assert Path('b.txt').exists()"],
            }],
        }]
        row = asyncio.run(
            run_dispatch(self.repo, tasks, self.out, keep=False, caller=fake_caller)
        )[0]

        report_path = next(self.out.glob("*-run.json"))
        report = json.loads(report_path.read_text(encoding="utf-8"))
        measured = report["tasks"][0]
        self.assertEqual(measured["name"], row["name"])
        self.assertGreater(measured["agent_started"], 0)
        self.assertGreaterEqual(measured["agent_finished"], measured["agent_started"])
        self.assertGreaterEqual(measured["agent_seconds"], 0)
        self.assertGreaterEqual(measured["verify_seconds"], 0)
        self.assertEqual(measured["changed_files"], ["b.txt"])
        self.assertGreaterEqual(report["run_finished"], report["run_started"])
        self.assertGreaterEqual(report["total_seconds"], 0)
        self.assertEqual(report["state_counts"], {"verified": 1})

    def test_delivery_card_marks_overlapping_files_for_review(self):
        rows = [
            {
                "name": "one", "agent": "codex", "state": "verified",
                "commit": "abc one", "changed_files": ["shared.txt"],
                "diffstat_lines": 1,
            },
            {
                "name": "two", "agent": "dsh", "state": "verified",
                "commit": "def two", "changed_files": ["shared.txt"],
                "diffstat_lines": 1,
            },
        ]

        card = build_card(rows[0], rows)

        self.assertIn("重叠文件：shared.txt", card)
        self.assertIn("建议             : 需复核", card)

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
