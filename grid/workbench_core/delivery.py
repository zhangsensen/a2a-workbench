"""dispatch 的交付证据、报告与交付卡生成。"""
from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
import re
import time

from .verification import file_digest
from .workspace import git


def new_row(name: str, agent: str, contract_path: Path) -> dict:
    """建立稳定的单任务交付记录 schema。"""
    return {
        "name": name,
        "agent": agent,
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


def snapshot_protected(tree: Path, protected_paths: list[str]) -> dict[str, str]:
    return {path: file_digest(tree, path) for path in protected_paths}


def compare_protected(tree: Path, protected_before: dict[str, str]) -> list[dict]:
    changes = []
    for path, before in protected_before.items():
        after = file_digest(tree, path)
        changes.append({
            "path": path,
            "before": before,
            "after": after,
            "changed": before != after,
        })
    return changes


def write_contract(
    out: Path, stamp: str, base_sha: str, frozen_tasks: list[dict],
) -> Path:
    contract_path = out / f"{stamp}-contract.json"
    document = {
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
        json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    return contract_path


def collect_git_evidence(
    tree: Path, base_sha: str, out: Path, stamp: str, name: str,
) -> dict:
    """从文件系统与 git 收集 commit/diff/patch，不信任 agent 回复文本。

    agent 结束后先 git add -A，把未提交的新文件也纳入可重放补丁；因此失败
    分支同样能保留已经完成的部分工作。
    """
    git(tree, "add", "-A")
    commit = git(tree, "log", "--oneline", f"{base_sha}..HEAD") or "(未提交)"
    diff = git(tree, "diff", "--cached", base_sha)
    diffstat = git(tree, "diff", "--cached", "--stat", base_sha) or "(无改动)"
    changed = git(tree, "diff", "--cached", "--name-only", base_sha)
    numstat = git(tree, "diff", "--cached", "--numstat", base_sha)
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


def write_failure_log(out: Path, stamp: str, name: str, text: str) -> str:
    log = out / f"{stamp}-{name}.log"
    log.write_text(
        text + ("\n" if text and not text.endswith("\n") else ""), encoding="utf-8",
    )
    return str(log)


def write_evidence(out: Path, stamp: str, name: str, evidence: dict) -> str:
    evidence_path = out / f"{stamp}-{name}-evidence.json"
    evidence_path.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    return str(evidence_path)


def refuted_log(evidence: dict, reply_tail: str) -> str:
    outputs = [
        item.get("output", "")
        for side in (evidence["candidate"], evidence["baseline"] or [])
        for item in side
        if not item.get("passed", False)
    ]
    body = "\n".join(outputs)
    if body and not body.endswith("\n"):
        body += "\n"
    return body + "--- agent 回复摘要 ---\n" + reply_tail


def write_run_report(
    out: Path,
    stamp: str,
    run_started: float,
    run_clock_started: float,
    rows: list[dict],
) -> Path:
    run_finished = time.time()
    report = {
        "run_started": run_started,
        "run_finished": run_finished,
        "total_seconds": time.perf_counter() - run_clock_started,
        "state_counts": dict(sorted(Counter(row["state"] for row in rows).items())),
        "tasks": list(rows),
    }
    run_path = out / f"{stamp}-run.json"
    run_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    return run_path


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
