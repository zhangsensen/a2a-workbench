"""dispatch 使用的隔离工作区创建、验证树重放与清理。"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import stat
import subprocess


@dataclass(frozen=True)
class TaskWorkspaces:
    """一次 dispatch 运行创建的基础仓库与任务 worktree。"""

    work: Path
    base: Path
    base_sha: str
    trees: dict[str, Path]


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode:
        raise RuntimeError(f"git {' '.join(args)} 失败: {result.stderr.strip()[:500]}")
    return result.stdout.strip()


def _rmtree_force(path: Path) -> None:
    """删除整棵目录树，容忍 Windows 上 git 对象文件的只读位。"""
    def _clear_readonly(func, target, _exc):
        os.chmod(target, stat.S_IWRITE)
        func(target)

    shutil.rmtree(path, onexc=_clear_readonly)


def create_task_workspaces(
    repo: Path, out: Path, stamp: str, task_names: Iterable[str],
) -> TaskWorkspaces:
    """克隆源仓库，并为每个任务创建独立分支和 worktree。"""
    work = out / f"work-{stamp}"
    base = work / "base"
    out.mkdir(parents=True, exist_ok=True)
    git(Path.cwd(), "clone", "-q", str(repo), str(base))
    base_sha = git(base, "rev-parse", "HEAD")

    trees: dict[str, Path] = {}
    for name in task_names:
        tree = work / f"wt-{name}"
        git(base, "worktree", "add", "-q", str(tree), "-b", f"dispatch/{name}")
        trees[name] = tree
    return TaskWorkspaces(work=work, base=base, base_sha=base_sha, trees=trees)


def create_clean_verifier_worktree(
    workspaces: TaskWorkspaces,
    verifier_tree: Path,
    patch: Path,
    protected_before: dict[str, str],
) -> None:
    """在干净 detached worktree 重放补丁，并恢复冻结的保护路径。"""
    git(
        workspaces.base, "worktree", "add", "-q", "--detach",
        str(verifier_tree), workspaces.base_sha,
    )
    if patch.stat().st_size:
        git(verifier_tree, "apply", str(patch))
    for path, before in protected_before.items():
        target = verifier_tree / path
        if before == "absent":
            if target.is_symlink() or target.is_file():
                target.unlink()
            elif target.exists():
                shutil.rmtree(target)
        else:
            git(
                verifier_tree, "--literal-pathspecs", "checkout",
                workspaces.base_sha, "--", path,
            )


def cleanup_task_workspaces(
    workspaces: TaskWorkspaces, verifier_trees: Iterable[Path] = (),
) -> None:
    """移除任务/验证 worktree 以及本轮临时 clone。"""
    for tree in [*verifier_trees, *workspaces.trees.values()]:
        subprocess.run(
            [
                "git", "-C", str(workspaces.base), "worktree", "remove", "--force",
                str(tree),
            ],
            capture_output=True,
            timeout=120,
        )
    _rmtree_force(workspaces.work)
