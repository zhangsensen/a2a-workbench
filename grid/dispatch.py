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
      "mode": "modify",
      "verify": [{"type": "command", "argv": ["python", "-m", "unittest"]}]}]

mode 可选，'modify'（默认）要求任务确实产生了 commit 或 diff，否则即使 verify
全过也只标 no-change（不给 verified）——防止"要求实现功能、agent 啥都没
做、verify 只是查到了本来就存在的文件"这种误判；'inspect' 用于本就不要求
改动的检查类任务，不改动也可以 verified。

约定（派活纪律，注入每个任务提示词）：
- agent 必须在自己的 worktree 里 git commit（改动即提交，可追溯）；
- 回复末尾必须附验证证据（测试/检查命令的实际输出）。
无 verify 时，有改动的成功任务记为 delivered；有 verify 时，
机器检查全过为 verified，任一失败为 refuted。agent 的完成声明从来不是证据。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from pathlib import Path
from typing import Awaitable, Callable

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from a2a_call import AgentTaskFailed, call_agent, load_catalog  # noqa: E402
from workbench_core.delivery import (  # noqa: E402
    build_card,
    collect_git_evidence,
    compare_protected,
    new_row,
    refuted_log,
    snapshot_protected,
    write_contract,
    write_evidence,
    write_failure_log,
    write_run_report,
)
from workbench_core.verification import (  # noqa: E402
    contract_digest,
    freeze_contract,
    protected_paths,
    run_checks,
    valid_relative_path,
)
from workbench_core.workspace import (  # noqa: E402
    cleanup_task_workspaces,
    create_clean_verifier_worktree,
    create_task_workspaces,
)

Caller = Callable[..., Awaitable[str]]

DISCIPLINE = (
    "\n\n[派活纪律] 你在一个隔离的 git worktree 里工作：\n"
    "1) 完成后必须 git add -A && git commit -m '<任务名>: <改动摘要>'；\n"
    "2) 回复末尾必须附上验证证据（你实际运行的测试/检查命令及其输出要点）；\n"
    "3) 只做本任务范围内的事，不转派、不顺手改无关内容。"
)


def _valid_relative_path(raw_path: object) -> bool:
    return valid_relative_path(raw_path)


def _protected_paths(task: dict) -> list[str]:
    """合并显式 protected 与 file 检查路径；集合按路径排序以稳定摘要。"""
    return protected_paths(task.get("verify"), task.get("protected"))


def _freeze_contract(task: dict) -> dict:
    """仅保留会影响验收语义的字段，后续检查不得再读取原始 task。"""
    return freeze_contract(
        task["task"], task.get("mode", "modify"), task.get("verify"),
        task.get("protected"),
    )


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
        mode = task.get("mode", "modify")
        if mode not in {"modify", "inspect"}:
            raise SystemExit(f"任务 {name} 的 mode 不支持：{mode!r}（可选：modify, inspect）")
        if "protected" in task:
            protected = task["protected"]
            if not isinstance(protected, list):
                raise SystemExit(f"任务 {name} 的 protected 必须是数组")
            for index, raw_path in enumerate(protected, 1):
                if not _valid_relative_path(raw_path):
                    raise SystemExit(
                        f"任务 {name} 的 protected[{index}] 必须是 worktree 内相对路径"
                    )
        if "verify" in task:
            checks = task["verify"]
            if not isinstance(checks, list):
                raise SystemExit(f"任务 {name} 的 verify 必须是数组")
            if not checks:
                raise SystemExit(
                    f"任务 {name} 的 verify 不能为空：要么给真实检查，要么删掉该字段"
                    "（结果记为 delivered）"
                )
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
                    if not _valid_relative_path(raw_path):
                        raise SystemExit(f"任务 {name} 的 verify[{index}].path 必须是 worktree 内相对路径")
                else:
                    raise SystemExit(f"任务 {name} 的 verify[{index}].type 不支持：{check_type!r}")
        task["protected"] = _protected_paths(task)
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
    run_started = time.time()
    run_clock_started = time.perf_counter()
    frozen_tasks: list[dict] = []
    for task in tasks:
        contract = json.loads(json.dumps(_freeze_contract(task), ensure_ascii=False))
        frozen_tasks.append({
            "name": task["name"],
            "agent": task["agent"],
            "contract": contract,
            "contract_digest": contract_digest(contract),
        })

    stamp = time.strftime("%Y%m%d-%H%M%S")
    workspaces = create_task_workspaces(
        repo, out, stamp, (task["name"] for task in frozen_tasks),
    )

    for task in frozen_tasks:
        tree = workspaces.trees[task["name"]]
        task["protected_before"] = snapshot_protected(
            tree, task["contract"]["protected"],
        )

    contract_path = write_contract(
        out, stamp, workspaces.base_sha, frozen_tasks,
    )

    verifier_trees: dict[str, Path] = {}
    verifier_tree_lock = asyncio.Lock()

    async def one(task: dict) -> dict:
        name, tree = task["name"], workspaces.trees[task["name"]]
        contract = task["contract"]
        prompt = f"任务 {name}：{contract['task']}" + DISCIPLINE
        row = new_row(name, task["agent"], contract_path)
        print(f"[进度] {name} 开始 (agent={task['agent']})", flush=True)

        async def record_failure_log(text: str) -> None:
            row["log"] = await asyncio.to_thread(
                write_failure_log, out, stamp, name, text,
            )

        async def collect_delivery() -> dict:
            return await asyncio.to_thread(
                collect_git_evidence,
                tree,
                workspaces.base_sha,
                out,
                stamp,
                name,
            )

        async def timed_checks(check_tree: Path) -> list[dict]:
            started = time.perf_counter()
            try:
                return await asyncio.to_thread(run_checks, check_tree, contract["verify"])
            finally:
                row["verify_seconds"] += time.perf_counter() - started

        row["agent_started"] = time.time()
        agent_clock_started = time.perf_counter()
        try:
            try:
                reply = await caller(task["agent"], prompt, cwd=str(tree))
            finally:
                row["agent_finished"] = time.time()
                row["agent_seconds"] = time.perf_counter() - agent_clock_started
            row["reply_tail"] = reply[-800:]
        except AgentTaskFailed as failure:
            row.update(state="failed", detail=f"[{failure.state}] {failure.text[:500]}")
            await record_failure_log(failure.text)
            try:
                row.update(await collect_delivery())
            except Exception:  # noqa: BLE001 —— 收集失败不覆盖原失败状态，静默降级
                pass
        except Exception as exc:  # noqa: BLE001
            error_text = f"{type(exc).__name__}: {exc}"
            row.update(state="error", detail=error_text[:500])
            await record_failure_log(error_text)
            try:
                row.update(await collect_delivery())
            except Exception:  # noqa: BLE001 —— 同上，静默降级
                pass
        else:
            try:
                row.update(await collect_delivery())
                mode = contract["mode"]
                no_change = row["commit"] == "(未提交)" and row["diffstat"] == "(无改动)"
                protected = await asyncio.to_thread(
                    compare_protected, tree, task["protected_before"],
                )
                contract_changed = any(item["changed"] for item in protected)
                row["protected_changed"] = [
                    item["path"] for item in protected if item["changed"]
                ]

                if contract_changed:
                    row.update(state="contract-changed", detail="protected 文件与冻结合约不一致")
                    if contract["verify"] is not None:
                        evidence = {
                            "contract_digest": task["contract_digest"],
                            "mode": mode,
                            "protected": protected,
                            "candidate": [],
                            "baseline": None,
                            "verdict": "contract-changed",
                        }
                        row["evidence"] = await asyncio.to_thread(
                            write_evidence, out, stamp, name, evidence,
                        )
                elif contract["verify"] is None:
                    if no_change:
                        row.update(state="no-change", detail="任务完成但没有任何文件改动")
                    else:
                        row["state"] = "delivered"
                else:
                    candidate = await timed_checks(tree)
                    baseline = None
                    verdict = "refuted"
                    if all(item["passed"] for item in candidate):
                        verifier_tree = workspaces.work / f"wt-{name}-verify"
                        verifier_trees[name] = verifier_tree
                        async with verifier_tree_lock:
                            await asyncio.to_thread(
                                create_clean_verifier_worktree,
                                workspaces,
                                verifier_tree,
                                Path(row["patch"]),
                                task["protected_before"],
                            )
                        baseline = await timed_checks(verifier_tree)
                        if all(item["passed"] for item in baseline):
                            verdict = "no-change" if mode == "modify" and no_change else "verified"

                    row["verification_failures"] = [
                        {"side": side, "index": index}
                        for side, results in (("candidate", candidate), ("baseline", baseline or []))
                        for index, item in enumerate(results, 1)
                        if not item["passed"]
                    ]

                    evidence = {
                        "contract_digest": task["contract_digest"],
                        "mode": mode,
                        "protected": protected,
                        "candidate": candidate,
                        "baseline": baseline,
                        "verdict": verdict,
                    }
                    row["evidence"] = await asyncio.to_thread(
                        write_evidence, out, stamp, name, evidence,
                    )
                    if verdict == "refuted":
                        row.update(state="refuted", detail="机器验收失败")
                        await record_failure_log(
                            refuted_log(evidence, row.get("reply_tail", "")),
                        )
                    elif verdict == "no-change":
                        row.update(
                            state="no-change",
                            detail="modify 模式要求实质改动：verify 全过但无 commit 无 diff",
                        )
                    else:
                        row["state"] = "verified"
            except Exception as exc:  # noqa: BLE001
                error_text = f"{type(exc).__name__}: {exc}"
                row.update(state="error", detail=error_text[:500])
                await record_failure_log(error_text)
        finally:
            print(f"[进度] {name} → {row['state']}", flush=True)
        return row

    rows = await asyncio.gather(*(one(t) for t in frozen_tasks))

    if not keep:
        cleanup_task_workspaces(workspaces, verifier_trees.values())

    write_run_report(out, stamp, run_started, run_clock_started, rows)
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
        failed |= row["state"] in {
            "failed", "error", "refuted", "no-change", "contract-changed",
        }
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
    for row in rows:
        print("\n" + build_card(row, rows))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
