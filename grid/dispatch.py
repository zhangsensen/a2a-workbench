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
from collections import Counter
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Awaitable, Callable

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from a2a_call import AgentTaskFailed, call_agent, load_catalog  # noqa: E402
from verification import contract_digest, file_digest, run_checks  # noqa: E402

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


def _rmtree_force(path: Path) -> None:
    """删除整棵目录树，容忍 Windows 上 git 对象文件的只读位。

    ignore_errors=True 在 Windows 会静默留下 .git/objects 的只读文件
    （清理断言随之失败）；正确做法是失败时清只读位重试。
    """
    def _clear_readonly(func, target, _exc):
        os.chmod(target, stat.S_IWRITE)
        func(target)

    shutil.rmtree(path, onexc=_clear_readonly)


def _valid_relative_path(raw_path: object) -> bool:
    return (isinstance(raw_path, str) and bool(raw_path) and
            not Path(raw_path).is_absolute() and ".." not in Path(raw_path).parts)


def _protected_paths(task: dict) -> list[str]:
    """合并显式 protected 与 file 检查路径；集合按路径排序以稳定摘要。"""
    paths = list(task.get("protected", []))
    for check in task.get("verify") or []:
        if check.get("type") == "file":
            paths.append(check["path"])
    return sorted(set(paths))


def _freeze_contract(task: dict) -> dict:
    """仅保留会影响验收语义的字段，后续检查不得再读取原始 task。"""
    return {
        "task": task["task"],
        "mode": task.get("mode", "modify"),
        "verify": task.get("verify"),
        "protected": _protected_paths(task),
    }


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


def _verification_conclusion(row: dict) -> str:
    state = row["state"]
    if state == "verified":
        return "双侧通过"
    if state == "refuted":
        failures = row.get("verification_failures", [])
        if not failures:
            return "验收失败（失败项未知）"
        return "；".join(
            f"{item['side']} 第{item['index']}项失败" for item in failures
        )
    if state == "delivered":
        return "未验证"
    if state == "contract-changed":
        paths = row.get("protected_changed", [])
        return "动了保护文件：" + ("、".join(paths) if paths else "未知")
    if state == "no-change":
        return "无改动"
    if state == "failed":
        return "agent 失败"
    if state == "error":
        return "执行错误"
    return row.get("detail") or state


def build_card(row: dict, all_rows: list[dict]) -> str:
    """纯函数渲染单任务交付卡；所有判断只依赖已收集的 row 数据。"""
    changed_files = row.get("changed_files", [])
    other_files = {
        path
        for other in all_rows
        if other.get("name") != row.get("name")
        for path in other.get("changed_files", [])
    }
    overlaps = sorted(set(changed_files) & other_files)
    diffstat_lines = row.get("diffstat_lines")
    if diffstat_lines is None:
        diffstat_lines = sum(
            int(value)
            for value in re.findall(
                r"(\d+) (?:insertion|deletion)s?\([+-]\)", row.get("diffstat", ""),
            )
        )
    large_change = len(changed_files) > 15 or diffstat_lines > 500

    risks = []
    if overlaps:
        risks.append("重叠文件：" + "、".join(overlaps))
    if large_change:
        risks.append("大改动")
    risk_text = "；".join(risks) if risks else "无"

    state = row["state"]
    if state in {"refuted", "contract-changed", "failed", "error", "no-change"}:
        suggestion = "驳回/打回"
    elif state == "delivered" or overlaps or large_change:
        suggestion = "需复核"
    elif state == "verified":
        suggestion = "可合并"
    else:
        suggestion = "需复核"

    commit = " | ".join(str(row.get("commit", "(无)")).splitlines()) or "(无)"
    lines = [
        "[交付卡]",
        f"  任务/agent/state : {row['name']} / {row['agent']} / {state}",
        f"  commit           : {commit}",
        "  改动文件         :",
    ]
    if changed_files:
        lines.extend(f"    - {path}" for path in changed_files[:10])
        if len(changed_files) > 10:
            lines.append(f"    +{len(changed_files) - 10} more")
    else:
        lines.append("    无")
    lines.extend([
        f"  验证结论         : {_verification_conclusion(row)}",
        f"  风险             : {risk_text}",
        f"  建议             : {suggestion}",
    ])
    return "\n".join(lines)


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
    work = out / f"work-{stamp}"
    base = work / "base"
    out.mkdir(parents=True, exist_ok=True)
    _git(Path.cwd(), "clone", "-q", str(repo), str(base))
    base_sha = _git(base, "rev-parse", "HEAD")

    trees: dict[str, Path] = {}
    for task in frozen_tasks:
        tree = work / f"wt-{task['name']}"
        _git(base, "worktree", "add", "-q", str(tree), "-b", f"dispatch/{task['name']}")
        trees[task["name"]] = tree

    for task in frozen_tasks:
        tree = trees[task["name"]]
        task["protected_before"] = {
            path: file_digest(tree, path) for path in task["contract"]["protected"]
        }

    contract_path = out / f"{stamp}-contract.json"
    contract_document = {
        "base_sha": base_sha,
        "tasks": [{
            "name": task["name"],
            "agent": task["agent"],
            "contract_digest": task["contract_digest"],
            "contract": task["contract"],
            "protected_files": [
                {"path": path, "sha256": digest}
                for path, digest in task["protected_before"].items()
            ],
        } for task in frozen_tasks],
    }
    contract_path.write_text(
        json.dumps(contract_document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )

    verifier_trees: dict[str, Path] = {}
    verifier_tree_lock = asyncio.Lock()

    async def one(task: dict) -> dict:
        name, tree = task["name"], trees[task["name"]]
        contract = task["contract"]
        prompt = f"任务 {name}：{contract['task']}" + DISCIPLINE
        row = {
            "name": name,
            "agent": task["agent"],
            "state": "delivered",
            "detail": "",
            "contract": str(contract_path),
            "agent_started": 0.0,
            "agent_finished": 0.0,
            "agent_seconds": 0.0,
            "verify_seconds": 0.0,
            "changed_files": [],
            "diffstat_lines": 0,
            "verification_failures": [],
            "protected_changed": [],
        }
        print(f"[进度] {name} 开始 (agent={task['agent']})", flush=True)

        def write_failure_log_sync(text: str) -> str:
            log = out / f"{stamp}-{name}.log"
            log.write_text(text + ("\n" if text and not text.endswith("\n") else ""), encoding="utf-8")
            return str(log)

        async def write_failure_log(text: str) -> None:
            row["log"] = await asyncio.to_thread(write_failure_log_sync, text)

        def collect_evidence_sync() -> dict:
            """收集 commit/diff/patch 证据，全部来自文件系统与 git，不信任
            回复文本。无论任务终态如何都可调用——失败分支也不该丢掉 agent
            已完成的部分工作（commit、未提交 diff、新文件）。先 git add -A
            把未跟踪文件纳入 index：此时 agent 已结束、worktree 归收集方
            所有，这一步是安全的；diff 改用 --cached 使新文件正文（而不只是
            文件名）进入 patch。"""
            _git(tree, "add", "-A")
            commit = _git(tree, "log", "--oneline", f"{base_sha}..HEAD") or "(未提交)"
            diff = _git(tree, "diff", "--cached", base_sha)
            diffstat = _git(tree, "diff", "--cached", "--stat", base_sha) or "(无改动)"
            changed = _git(tree, "diff", "--cached", "--name-only", base_sha)
            numstat = _git(tree, "diff", "--cached", "--numstat", base_sha)
            diffstat_lines = sum(
                int(value)
                for line in numstat.splitlines()
                for value in line.split("\t", 2)[:2]
                if value.isdigit()
            )
            patch = out / f"{stamp}-{name}.patch"
            patch.write_text(
                diff + ("\n" if diff and not diff.endswith("\n") else ""), encoding="utf-8",
            )
            return {
                "commit": commit,
                "diffstat": diffstat,
                "diffstat_lines": diffstat_lines,
                "changed_files": changed.splitlines() if changed else [],
                "patch": str(patch),
            }

        def compare_protected_sync() -> list[dict]:
            changes = []
            for path, before in task["protected_before"].items():
                after = file_digest(tree, path)
                changes.append({
                    "path": path,
                    "before": before,
                    "after": after,
                    "changed": before != after,
                })
            return changes

        def write_evidence_sync(evidence: dict) -> str:
            evidence_path = out / f"{stamp}-{name}-evidence.json"
            evidence_path.write_text(
                json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
            )
            return str(evidence_path)

        def prepare_clean_verifier_sync(verifier_tree: Path) -> None:
            _git(base, "worktree", "add", "-q", "--detach", str(verifier_tree), base_sha)
            patch = Path(row["patch"])
            if patch.stat().st_size:
                _git(verifier_tree, "apply", str(patch))
            for path, before in task["protected_before"].items():
                target = verifier_tree / path
                if before == "absent":
                    if target.is_symlink() or target.is_file():
                        target.unlink()
                    elif target.exists():
                        shutil.rmtree(target)
                else:
                    _git(
                        verifier_tree, "--literal-pathspecs", "checkout", base_sha, "--", path,
                    )

        def refuted_log(evidence: dict) -> str:
            outputs = [
                item.get("output", "")
                for side in (evidence["candidate"], evidence["baseline"] or [])
                for item in side
                if not item.get("passed", False)
            ]
            body = "\n".join(outputs)
            if body and not body.endswith("\n"):
                body += "\n"
            return body + "--- agent 回复摘要 ---\n" + row.get("reply_tail", "")

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
            await write_failure_log(failure.text)
            try:
                row.update(await asyncio.to_thread(collect_evidence_sync))
            except Exception:  # noqa: BLE001 —— 收集失败不覆盖原失败状态，静默降级
                pass
        except Exception as exc:  # noqa: BLE001
            error_text = f"{type(exc).__name__}: {exc}"
            row.update(state="error", detail=error_text[:500])
            await write_failure_log(error_text)
            try:
                row.update(await asyncio.to_thread(collect_evidence_sync))
            except Exception:  # noqa: BLE001 —— 同上，静默降级
                pass
        else:
            try:
                row.update(await asyncio.to_thread(collect_evidence_sync))
                mode = contract["mode"]
                no_change = row["commit"] == "(未提交)" and row["diffstat"] == "(无改动)"
                protected = await asyncio.to_thread(compare_protected_sync)
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
                            write_evidence_sync, evidence,
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
                        verifier_tree = work / f"wt-{name}-verify"
                        verifier_trees[name] = verifier_tree
                        async with verifier_tree_lock:
                            await asyncio.to_thread(prepare_clean_verifier_sync, verifier_tree)
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
                    row["evidence"] = await asyncio.to_thread(write_evidence_sync, evidence)
                    if verdict == "refuted":
                        row.update(state="refuted", detail="机器验收失败")
                        await write_failure_log(refuted_log(evidence))
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
                await write_failure_log(error_text)
        finally:
            print(f"[进度] {name} → {row['state']}", flush=True)
        return row

    rows = await asyncio.gather(*(one(t) for t in frozen_tasks))

    if not keep:
        for tree in [*verifier_trees.values(), *trees.values()]:
            subprocess.run(["git", "-C", str(base), "worktree", "remove", "--force", str(tree)],
                           capture_output=True, timeout=120)
        _rmtree_force(work)

    run_finished = time.time()
    run_report = {
        "run_started": run_started,
        "run_finished": run_finished,
        "total_seconds": time.perf_counter() - run_clock_started,
        "state_counts": dict(sorted(Counter(row["state"] for row in rows).items())),
        "tasks": list(rows),
    }
    run_path = out / f"{stamp}-run.json"
    run_path.write_text(
        json.dumps(run_report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
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
