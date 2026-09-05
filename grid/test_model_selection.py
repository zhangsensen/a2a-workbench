"""Test A2A model-selection metadata -> command-arg translation across all executors.

Since the model/provider translation + whitelist validation now lives in the shared
base ``SubprocessAgentExecutor``, the same guarantees apply to pi/claude/codex/dsh
(only pi also maps ``provider``). This tests:

  - each agent's declared MODEL_FLAG / PROVIDER_FLAG
  - valid model/provider -> appended argv
  - injection / malformed values -> rejected (empty args), no CLI injection
  - unknown keys / non-dict metadata -> ignored
  - per-agent behavior: pi maps model+provider; claude/codex map model only;
    dsh ignores (returns [])

Run:
    python -m unittest test_pi_model_selection -v
"""
import importlib.util
import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from subprocess_executor import SubprocessAgentExecutor  # noqa: E402


def _load_executor(name: str, module_hash: str):
    """Load an executor module by explicit file path to avoid module-name collisions.

    claude/codex/dsh all name their executor file ``executor.py``, so a naive
    ``import executor`` is ambiguous. Load each directly by path instead.
    """
    path = BASE / name / (
        "pi_executor.py" if name == "pi" else "executor.py"
    )
    spec = importlib.util.spec_from_file_location(f"_{module_hash}_executor", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


PiExecutor = _load_executor("pi", "pi").PiExecutor
ClaudeExecutor = _load_executor("claude", "claude").ClaudeExecutor
CodexExecutor = _load_executor("codex", "codex").CodexExecutor
DSHExecutor = _load_executor("dsh", "dsh").DSHExecutor

import subprocess  # noqa: E402


class ModelSelectionTest(unittest.TestCase):
    def setUp(self):
        self.pi = PiExecutor()
        self.claude = ClaudeExecutor()
        self.codex = CodexExecutor()
        self.dsh = DSHExecutor()

    # --- per-agent flag declaration ---
    def test_flag_declarations(self):
        self.assertEqual(self.pi.MODEL_FLAG, "--model")
        self.assertEqual(self.pi.PROVIDER_FLAG, "--provider")
        self.assertEqual(self.claude.MODEL_FLAG, "--model")
        self.assertIsNone(self.claude.PROVIDER_FLAG)
        self.assertEqual(self.codex.MODEL_FLAG, "--model")
        self.assertIsNone(self.codex.PROVIDER_FLAG)
        # dsh: config-driven model, no CLI flag
        self.assertIsNone(self.dsh.MODEL_FLAG)
        self.assertIsNone(self.dsh.PROVIDER_FLAG)

    # --- valid translation ---
    def test_pi_model_and_provider(self):
        self.assertEqual(
            self.pi._executor_args_from_metadata(
                {"model": "deepseek-v4-flash (self hosted)", "provider": "habi"}
            ),
            ["--model", "deepseek-v4-flash (self hosted)", "--provider", "habi"],
        )

    def test_claude_model_only(self):
        self.assertEqual(
            self.claude._executor_args_from_metadata({"model": "claude-sonnet-4-6"}),
            ["--model", "claude-sonnet-4-6"],
        )
        # claude has no provider flag -> provider metadata ignored
        self.assertEqual(
            self.claude._executor_args_from_metadata({"model": "m", "provider": "x"}),
            ["--model", "m"],
        )

    def test_codex_model_only(self):
        self.assertEqual(
            self.codex._executor_args_from_metadata({"model": "gpt-5.6-sol"}),
            ["--model", "gpt-5.6-sol"],
        )

    def test_dsh_ignores_model(self):
        self.assertEqual(self.dsh._executor_args_from_metadata({"model": "x"}), [])
        self.assertEqual(
            self.dsh._executor_args_from_metadata({"model": "x", "provider": "y"}), []
        )

    def test_slash_and_dash_model(self):
        self.assertEqual(
            self.pi._executor_args_from_metadata({"model": "openai/gpt-4o"}),
            ["--model", "openai/gpt-4o"],
        )

    # --- injection / malformed (shared validator) ---
    def test_embedded_flag_rejected(self):
        for ex in (self.pi, self.claude, self.codex):
            self.assertEqual(ex._executor_args_from_metadata({"model": "x --system-prompt evil"}), [])
            self.assertEqual(ex._executor_args_from_metadata({"model": "--model flag"}), [])

    def test_shell_metachars_rejected(self):
        for bad in ('a"; del', "a|b", "a;b", "a&b", "a`rm", "a$b", "a<b"):
            for ex in (self.pi, self.claude, self.codex):
                self.assertEqual(ex._executor_args_from_metadata({"model": bad}), [], msg=bad)

    def test_leading_dash_rejected(self):
        for ex in (self.pi, self.claude, self.codex):
            self.assertEqual(ex._executor_args_from_metadata({"model": "-x"}), [])
            self.assertEqual(ex._executor_args_from_metadata({"provider": "-x"}), [])

    def test_newline_rejected(self):
        for ex in (self.pi, self.claude, self.codex):
            self.assertEqual(ex._executor_args_from_metadata({"model": "a\nb"}), [])

    def test_oversize_rejected(self):
        for ex in (self.pi, self.claude, self.codex):
            self.assertEqual(ex._executor_args_from_metadata({"model": "x" * 300}), [])

    # --- ignored ---
    def test_unknown_key_ignored(self):
        for ex in (self.pi, self.claude, self.codex):
            self.assertEqual(ex._executor_args_from_metadata({"foo": "bar"}), [])

    def test_non_dict_ignored(self):
        for ex in (self.pi, self.claude, self.codex):
            self.assertEqual(ex._executor_args_from_metadata(None), [])
            self.assertEqual(ex._executor_args_from_metadata("x"), [])
            self.assertEqual(ex._executor_args_from_metadata(["a"]), [])

    def test_empty_ignored(self):
        for ex in (self.pi, self.claude, self.codex):
            self.assertEqual(ex._executor_args_from_metadata({}), [])

    # --- safe value primitives ---
    def test_safe_value(self):
        S = SubprocessAgentExecutor
        self.assertEqual(S._safe_meta_value("claude-sonnet-4-6"), "claude-sonnet-4-6")
        self.assertIsNone(S._safe_meta_value("a\nb"))
        self.assertIsNone(S._safe_meta_value(123))
        self.assertIsNone(S._safe_meta_value("   "))
        self.assertIsNone(S._safe_meta_value("--flag"))

    # --- end-to-end argv construction ---
    def test_pi_cmd_build(self):
        args = self.pi._executor_args_from_metadata(
            {"model": "claude-sonnet-4-6", "provider": "habi"}
        )
        cmd = subprocess.list2cmdline([self.pi.BIN, *self.pi.ARGS_PREFIX, *args])
        self.assertIn("--model claude-sonnet-4-6", cmd)
        self.assertIn("--provider habi", cmd)
        base = subprocess.list2cmdline([self.pi.BIN, *self.pi.ARGS_PREFIX])
        self.assertNotIn("--model", base)

    def test_claude_cmd_build(self):
        args = self.claude._executor_args_from_metadata({"model": "claude-sonnet-4-6"})
        argv = [self.claude.BIN, *self.claude.ARGS_PREFIX, *args]
        self.assertIn("--model", argv)
        # USE_SHELL=False -> arg list (no shell) carries model cleanly
        self.assertEqual(argv[-2:], ["--model", "claude-sonnet-4-6"])


if __name__ == "__main__":
    unittest.main()
