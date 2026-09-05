"""在任务 worktree 中执行可复现的机器验收检查。"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path


def _tail(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    return value[-1000:]


def _command_output(stdout: str | bytes | None, stderr: str | bytes | None) -> str:
    return f"stdout:\n{_tail(stdout)}\nstderr:\n{_tail(stderr)}"


def run_checks(worktree: Path | str, checks: list[dict]) -> list[dict]:
    """运行检查并返回逐项证据；命令始终直接执行 argv，不经过 shell。"""
    root = Path(worktree)
    results: list[dict] = []
    for check in checks:
        check_type = check.get("type")
        if check_type == "command":
            argv = check.get("argv")
            timeout = check.get("timeout", 120)
            detail = f"argv={json.dumps(argv, ensure_ascii=False)}"
            try:
                completed = subprocess.run(
                    argv,
                    cwd=root,
                    shell=False,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=timeout,
                )
                results.append({
                    "type": "command",
                    "detail": f"{detail} exit={completed.returncode}",
                    "passed": completed.returncode == 0,
                    "output": _command_output(completed.stdout, completed.stderr),
                })
            except subprocess.TimeoutExpired as exc:
                results.append({
                    "type": "command",
                    "detail": f"{detail} timeout={timeout}",
                    "passed": False,
                    "output": _command_output(exc.stdout, exc.stderr),
                })
            except Exception as exc:  # noqa: BLE001 - 运行失败也应成为验收证据
                results.append({
                    "type": "command",
                    "detail": detail,
                    "passed": False,
                    "output": f"{type(exc).__name__}: {exc}",
                })
        elif check_type == "file":
            raw_path = check.get("path", "")
            relative = Path(raw_path)
            valid = bool(raw_path) and not relative.is_absolute() and ".." not in relative.parts
            passed = valid and (root / relative).exists()
            detail = str(raw_path) if valid else f"非法相对路径：{raw_path!r}"
            results.append({
                "type": "file",
                "detail": detail,
                "passed": passed,
                "output": "存在" if passed else "不存在",
            })
        else:
            results.append({
                "type": str(check_type),
                "detail": f"不支持的检查类型：{check_type!r}",
                "passed": False,
                "output": "",
            })
    return results
