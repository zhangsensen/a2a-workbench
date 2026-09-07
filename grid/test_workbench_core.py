from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from workbench_core import delivery as core_delivery
from workbench_core import verification as core_verification
from workbench_core import workspace as core_workspace


class TestWorkbenchCore(unittest.TestCase):
    def test_contract_freeze_validates_and_copies(self):
        checks = [{"type": "file", "path": "locked.txt"}]
        contract = core_verification.freeze_contract(
            "inspect", "inspect", checks, ["config.json"],
        )
        checks[0]["path"] = "changed.txt"
        self.assertEqual(contract["verify"][0]["path"], "locked.txt")
        self.assertEqual(contract["protected"], ["config.json", "locked.txt"])
        with self.assertRaisesRegex(ValueError, "worktree-relative"):
            core_verification.freeze_contract(
                "bad", verify=[{"type": "file", "path": "../escape"}],
            )

    def test_delivery_and_checks_share_one_git_implementation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo, out = root / "repo", root / "out"
            repo.mkdir()
            out.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(
                ["git", "-C", str(repo), "config", "user.email", "test@example.com"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(repo), "config", "user.name", "Test"],
                check=True,
            )
            (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "tracked.txt"], check=True)
            subprocess.run(
                ["git", "-C", str(repo), "commit", "-qm", "base"], check=True,
            )
            base_sha = core_workspace.git(repo, "rev-parse", "HEAD")
            (repo / "result.txt").write_text("ready\n", encoding="utf-8")

            result = core_delivery.collect_git_evidence(
                repo, base_sha, out, "stamp", "task",
            )
            checks = core_verification.run_checks(repo, [{
                "type": "command",
                "argv": [sys.executable, "-c", "from pathlib import Path; assert Path('result.txt').is_file()"],
            }])

            self.assertEqual(result["changed_files"], ["result.txt"])
            self.assertTrue(Path(result["patch"]).is_file())
            self.assertTrue(checks[0]["passed"])


if __name__ == "__main__":
    unittest.main()
