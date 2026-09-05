"""并行派活收口工具：自动隔离 worktree + 并行调 agent + 收集终态与证据。

把此前手工验证过两次的流程固化成一条命令（clone → 每任务一个 worktree →
并行 A2A 派活 → 收终态/commit/diff → 导出补丁 → 汇总表）。只做收口，
不做常驻 Manager：无守护进程、无状态库，跑完即走，补丁目录就是全部产出。

用法（在 agent 可达的环境里跑，如 WSL）：
    python dispatch.py <源仓库路径> <tasks.json> [--out 目录] [--keep]
    python dispatch.py <tasks.json> --check
    python dispatch.py --init <tasks.json>

tasks.json 形如：
    [{"agent": "codex", "name": "fix-x", "task": "……做什么……",
      "verify": [{"type": "command", "argv": ["python", "-m", "unittest"]}]}]

约定（派活纪律，注入每个任务提示词）：
- agent 必须在自己的 worktree 里 git commit（改动即提交，可追溯）；
- 回复末尾必须附验证证据（测试/检查命令的实际输出）。
无 verify 时，汇总表里的 state=ok 只代表任务终态 COMPLETED；有 verify 时，
机器检查全过为 verified，任一失败为 refuted。agent 的完成声明从来不是证据。
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
from verification import run_checks  # noqa: E402

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
        if not isinstance(task, dict):
            raise SystemExit("tasks.json 的每一项都必须是对象")
        name = str(task.get("name", "")).strip()
        agent = str(task.get("agent", "")).strip()
        text = str(task.get("task", "")).strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", name) or name in seen:
            raise SystemExit(f"任务 name 非法或重复：{name!r}")
        if agent not in known:
            raise SystemExit(f"任务 {name} 的 agent 未注册：{agent!r}（可选：{sorted(known)}）")
        if not text:
            raise SystemExit(f"任务 {name} 内容为空")
        if "verify" in task:
            checks = task["verify"]
            if not isinstance(checks, list):
                raise SystemExit(f"任务 {name} 的 verify 必须是数组")
            for index, check in enumerate(checks, 1):
                if not isinstance(check, dict):
                    raise SystemExit(f"任务 {name} 的 verify[{index}] 必须是对象")
                check_type = check.get("type")
                if check_type == "command":
                    argv = check.get("argv")
                    if (not isinstance(argv, list) or not argv or
                            any(not isinstance(arg, str) or not arg for arg in argv)):
                        raise SystemExit(f"任务 {name} 的 verify[{index}].argv 必须是非空字符串数组")
                    timeout = check.get("timeout", 120)
                    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
                        raise SystemExit(f"任务 {name} 的 verify[{index}].timeout 必须是正数")
                elif check_type == "file":
                    raw_path = check.get("path")
                    if (not isinstance(raw_path, str) or not raw_path or
                            Path(raw_path).is_absolute() or ".." in Path(raw_path).parts):
                        raise SystemExit(f"任务 {name} 的 verify[{index}].path 必须是 worktree 内相对路径")
                else:
                    raise SystemExit(f"任务 {name} 的 verify[{index}].type 不支持：{check_type!r}")
        seen.add(name)
    return tasks


def init_tasks(path: Path) -> None:
    """生成可直接通过 load_tasks 校验的 tasks.json 示例。"""
    agents = [agent["name"] for agent in load_catalog()["agents"]]
    if not agents:
        raise SystemExit("agents.json 中没有可用 agent")
    if path.exists():
        raise SystemExit(f"拒绝覆盖已有文件：{path}")
    skeleton = [{
        "_comment": f"agent 候选值：{', '.join(agents)}",
        "agent": agents[0],
        "name": "example-task",
        "task": "描述任务范围、完成标准与验证方式",
        "verify": [
            {"type": "command", "argv": ["python", "-m", "unittest"], "timeout": 120},
            {"type": "file", "path": "path/to/expected-file"},
        ],
    }]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(skeleton, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


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
        print(f"[进度] {name} 开始 (agent={task['agent']})", flush=True)

        def write_failure_log(text: str) -> None:
            log = out / f"{stamp}-{name}.log"
            log.write_text(text + ("\n" if text and not text.endswith("\n") else ""), encoding="utf-8")
            row["log"] = str(log)

        try:
            reply = await caller(task["agent"], prompt, cwd=str(tree))
            row["reply_tail"] = reply[-800:]
        except AgentTaskFailed as failure:
            row.update(state="failed", detail=f"[{failure.state}] {failure.text[:500]}")
            write_failure_log(failure.text)
        except Exception as exc:  # noqa: BLE001
            error_text = f"{type(exc).__name__}: {exc}"
            row.update(state="error", detail=error_text[:500])
            write_failure_log(error_text)
        else:
            try:
                # 证据收集全部来自文件系统与 git，不信任回复文本。
                row["commit"] = _git(tree, "log", "--oneline", f"{base_sha}..HEAD") or "(未提交)"
                diff = _git(tree, "diff", base_sha)
                untracked = _git(tree, "status", "--short")
                row["diffstat"] = _git(tree, "diff", "--stat", base_sha) or untracked or "(无改动)"
                patch = out / f"{stamp}-{name}.patch"
                patch.write_text(
                    diff + ("\n" if diff and not diff.endswith("\n") else ""), encoding="utf-8",
                )
                row["patch"] = str(patch)
                if "verify" in task:
                    evidence = run_checks(tree, task["verify"])
                    evidence_path = out / f"{stamp}-{name}-evidence.json"
                    evidence_path.write_text(
                        json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
                    )
                    row["evidence"] = str(evidence_path)
                    if not all(item["passed"] for item in evidence):
                        row.update(state="refuted", detail="机器验收失败")
                        write_failure_log(row.get("reply_tail", ""))
                    else:
                        row["state"] = "verified"
                elif not diff and not untracked:
                    row.update(state="no-change", detail="任务完成但没有任何文件改动")
            except Exception as exc:  # noqa: BLE001
                error_text = f"{type(exc).__name__}: {exc}"
                row.update(state="error", detail=error_text[:500])
                write_failure_log(error_text)
        finally:
            print(f"[进度] {name} → {row['state']}", flush=True)
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="并行派活收口工具")
    parser.add_argument("repo", nargs="?", help="源仓库路径（clone 只读，不动源仓库）")
    parser.add_argument("tasks", nargs="?", help="tasks.json 路径")
    parser.add_argument("--out", default="dispatch-out", help="补丁与报告输出目录")
    parser.add_argument("--keep", action="store_true", help="保留 worktree 现场")
    parser.add_argument("--init", metavar="PATH", help="生成 tasks.json 骨架后退出")
    parser.add_argument("--check", action="store_true", help="只校验 tasks.json 后退出")
    args = parser.parse_args(argv)

    if args.init:
        if args.repo or args.tasks or args.check:
            parser.error("--init 不能和位置参数或 --check 同时使用")
        init_tasks(Path(args.init))
        print(f"已生成 {args.init}")
        return 0

    if args.check:
        tasks_path = args.tasks or args.repo
        if not tasks_path:
            parser.error("--check 需要 tasks.json 路径")
        load_tasks(Path(tasks_path))
        print("OK")
        return 0

    if not args.repo or not args.tasks:
        parser.error("需要源仓库路径和 tasks.json 路径")

    tasks = load_tasks(Path(args.tasks))
    rows = asyncio.run(run_dispatch(Path(args.repo), tasks, Path(args.out), args.keep))

    print(f"\n{'任务':<16}{'agent':<8}{'终态':<10}摘要")
    failed = False
    for row in rows:
        failed |= row["state"] in {"failed", "error", "refuted"}
        summary = row.get("detail") or row.get("commit", "")
        print(f"{row['name']:<16}{row['agent']:<8}{row['state']:<10}{summary[:80]}")
        if row.get("diffstat"):
            print(f"{'':<34}{row['diffstat'].splitlines()[-1].strip()}")
        if row.get("patch"):
            print(f"{'':<34}patch: {row['patch']}")
        if row.get("evidence"):
            print(f"{'':<34}evidence: {row['evidence']}")
        if row.get("log"):
            print(f"{'':<34}log: {row['log']}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
