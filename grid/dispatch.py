"""并行派活收口工具：自动隔离 worktree + 并行调 agent + 收集终态与证据。

把此前手工验证过两次的流程固化成一条命令（clone → 每任务一个 worktree →
并行 A2A 派活 → 收终态/commit/diff → 导出补丁 → 汇总表）。只做收口，
不做常驻 Manager：无守护进程、无状态库，跑完即走，补丁目录就是全部产出。

用法（在 agent 可达的环境里跑，如 WSL）：
    python dispatch.py <源仓库路径> <tasks.json> [--out 目录] [--keep]

tasks.json 形如：
    [{"agent": "codex", "name": "fix-x", "task": "……做什么、怎么验证……"}]

约定（派活纪律，注入每个任务提示词）：
- agent 必须在自己的 worktree 里 git commit（改动即提交，可追溯）；
- 回复末尾必须附验证证据（测试/检查命令的实际输出）。
汇总表里 state=ok 只代表任务终态 COMPLETED；改动是否合格由人（master）
读补丁决定——agent 的完成声明从来不是证据。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Awaitable, Callable

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from a2a_call import AgentTaskFailed, call_agent, load_catalog  # noqa: E402

Caller = Callable[..., Awaitable[str]]

DISCIPLINE = (
    "\n\n[派活纪律] 你在一个隔离的 git worktree 里工作：\n"
    "1) 完成后必须 git add -A && git commit -m '<任务名>: <改动摘要>'；\n"
    "2) 回复末尾必须附上验证证据（你实际运行的测试/检查命令及其输出要点）；\n"
    "3) 只做本任务范围内的事，不转派、不顺手改无关内容。"
)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode:
        raise RuntimeError(f"git {' '.join(args)} 失败: {result.stderr.strip()[:500]}")
    return result.stdout.strip()


def load_tasks(path: Path) -> list[dict]:
    tasks = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(tasks, list) or not tasks:
        raise SystemExit("tasks.json 必须是非空数组")
    known = {a["name"] for a in load_catalog()["agents"]}
    seen: set[str] = set()
    for task in tasks:
        name = str(task.get("name", "")).strip()
        agent = str(task.get("agent", "")).strip()
        text = str(task.get("task", "")).strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", name) or name in seen:
            raise SystemExit(f"任务 name 非法或重复：{name!r}")
        if agent not in known:
            raise SystemExit(f"任务 {name} 的 agent 未注册：{agent!r}（可选：{sorted(known)}）")
        if not text:
            raise SystemExit(f"任务 {name} 内容为空")
        seen.add(name)
    return tasks


async def run_dispatch(
    repo: Path, tasks: list[dict], out: Path, keep: bool, caller: Caller = call_agent,
) -> list[dict]:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    work = out / f"work-{stamp}"
    base = work / "base"
    out.mkdir(parents=True, exist_ok=True)
    _git(Path.cwd(), "clone", "-q", str(repo), str(base))
    base_sha = _git(base, "rev-parse", "HEAD")

    trees: dict[str, Path] = {}
    for task in tasks:
        tree = work / f"wt-{task['name']}"
        _git(base, "worktree", "add", "-q", str(tree), "-b", f"dispatch/{task['name']}")
        trees[task["name"]] = tree

    async def one(task: dict) -> dict:
        name, tree = task["name"], trees[task["name"]]
        prompt = f"任务 {name}：{task['task']}" + DISCIPLINE
        row = {"name": name, "agent": task["agent"], "state": "ok", "detail": ""}
        try:
            reply = await caller(task["agent"], prompt, cwd=str(tree))
            row["reply_tail"] = reply[-800:]
        except AgentTaskFailed as failure:
            row.update(state="failed", detail=f"[{failure.state}] {failure.text[:500]}")
            return row
        except Exception as exc:  # noqa: BLE001
            row.update(state="error", detail=f"{type(exc).__name__}: {exc}")
            return row
        # 证据收集全部来自文件系统与 git，不信任回复文本。
        row["commit"] = _git(tree, "log", "--oneline", f"{base_sha}..HEAD") or "(未提交)"
        diff = _git(tree, "diff", base_sha)
        untracked = _git(tree, "status", "--short")
        row["diffstat"] = _git(tree, "diff", "--stat", base_sha) or untracked or "(无改动)"
        patch = out / f"{stamp}-{name}.patch"
        patch.write_text(diff + ("\n" if diff and not diff.endswith("\n") else ""), encoding="utf-8")
        row["patch"] = str(patch)
        if not diff and not untracked:
            row.update(state="no-change", detail="任务完成但没有任何文件改动")
        return row

    rows = await asyncio.gather(*(one(t) for t in tasks))

    if not keep:
        for tree in trees.values():
            subprocess.run(["git", "-C", str(base), "worktree", "remove", "--force", str(tree)],
                           capture_output=True, timeout=120)
        subprocess.run(["rm", "-rf", str(work)] if sys.platform != "win32" else
                       ["cmd", "/c", "rmdir", "/s", "/q", str(work)],
                       capture_output=True, timeout=120)
    return list(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="并行派活收口工具")
    parser.add_argument("repo", help="源仓库路径（clone 只读，不动源仓库）")
    parser.add_argument("tasks", help="tasks.json 路径")
    parser.add_argument("--out", default="dispatch-out", help="补丁与报告输出目录")
    parser.add_argument("--keep", action="store_true", help="保留 worktree 现场")
    args = parser.parse_args()

    tasks = load_tasks(Path(args.tasks))
    rows = asyncio.run(run_dispatch(Path(args.repo), tasks, Path(args.out), args.keep))

    print(f"\n{'任务':<16}{'agent':<8}{'终态':<10}摘要")
    failed = False
    for row in rows:
        failed |= row["state"] in {"failed", "error"}
        summary = row.get("detail") or row.get("commit", "")
        print(f"{row['name']:<16}{row['agent']:<8}{row['state']:<10}{summary[:80]}")
        if row.get("diffstat"):
            print(f"{'':<34}{row['diffstat'].splitlines()[-1].strip()}")
        if row.get("patch"):
            print(f"{'':<34}patch: {row['patch']}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
