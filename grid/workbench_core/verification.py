"""在任务 worktree 中执行可复现的机器验收检查。"""
from __future__ import annotations

import hashlib
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


def contract_digest(contract: dict) -> str:
    """返回冻结验收合约的稳定摘要。"""
    canonical = json.dumps(contract, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def valid_relative_path(raw_path: object) -> bool:
    """判断路径是否为 worktree 内不含上跳的非空相对路径。"""
    return (
        isinstance(raw_path, str)
        and bool(raw_path)
        and not Path(raw_path).is_absolute()
        and ".." not in Path(raw_path).parts
    )


def protected_paths(verify: list[dict] | None, protected: list[str] | None) -> list[str]:
    """合并显式保护路径与 file 检查路径，并稳定排序去重。"""
    paths = list(protected or [])
    for check in verify or []:
        if check.get("type") == "file":
            paths.append(check["path"])
    return sorted(set(paths))


def freeze_contract(
    task: str,
    mode: str = "modify",
    verify: list[dict] | None = None,
    protected: list[str] | None = None,
) -> dict:
    """校验并深拷贝影响验收语义的字段，供后续按冻结值执行。"""
    if mode not in {"modify", "inspect"}:
        raise ValueError("mode must be 'modify' or 'inspect'")
    if protected is not None and not isinstance(protected, list):
        raise ValueError("protected must be an array")
    for index, raw_path in enumerate(protected or [], 1):
        if not valid_relative_path(raw_path):
            raise ValueError(f"protected[{index}] must be a worktree-relative path")
    if verify is not None:
        if not isinstance(verify, list) or not verify:
            raise ValueError("verify must be a nonempty array")
        for index, check in enumerate(verify, 1):
            if not isinstance(check, dict):
                raise ValueError(f"verify[{index}] must be an object")
            check_type = check.get("type")
            if check_type == "command":
                argv = check.get("argv")
                if (
                    not isinstance(argv, list)
                    or not argv
                    or any(not isinstance(arg, str) or not arg for arg in argv)
                ):
                    raise ValueError(f"verify[{index}].argv must be a nonempty string array")
                timeout = check.get("timeout", 120)
                if (
                    isinstance(timeout, bool)
                    or not isinstance(timeout, (int, float))
                    or timeout <= 0
                ):
                    raise ValueError(f"verify[{index}].timeout must be positive")
            elif check_type == "file":
                if not valid_relative_path(check.get("path")):
                    raise ValueError(f"verify[{index}].path must be worktree-relative")
            else:
                raise ValueError(f"verify[{index}].type is unsupported: {check_type!r}")
    contract = {
        "task": task,
        "mode": mode,
        "verify": verify,
        "protected": protected_paths(verify, protected),
    }
    return json.loads(json.dumps(contract, ensure_ascii=False))


def file_digest(worktree: Path | str, relative_path: str) -> str:
    """计算 worktree 内文件摘要；基准中不存在的文件记作 absent。"""
    path = Path(worktree) / relative_path
    if not path.exists():
        return "absent"
    return hashlib.sha256(path.read_bytes()).hexdigest()


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
