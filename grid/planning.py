"""计划 JSON 提取与任务 DAG 校验。"""
from __future__ import annotations

import json


VALID_MODES = {"execute", "analyze", "review"}


def extract_json_object(text: str) -> dict:
    """从可能带 Markdown 围栏的模型回复中提取首个完整 JSON object。"""
    start = text.find("{")
    if start < 0:
        raise ValueError("回复中没有 JSON object")
    depth = 0
    quoted = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            continue
        if char == '"':
            quoted = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                value = json.loads(text[start : index + 1])
                if not isinstance(value, dict):
                    raise ValueError("计划必须是 JSON object")
                return value
    raise ValueError("JSON object 不完整")


def validate_plan(plan: dict, workers: list[str]) -> dict:
    """校验任务 DAG，并阻止同一写入范围无依赖并发。"""
    tasks = plan.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("计划必须包含非空 tasks")

    normalized: list[dict] = []
    ids: set[str] = set()
    for raw in tasks:
        if not isinstance(raw, dict):
            raise ValueError("每个 task 必须是 object")
        task_id = str(raw.get("id", "")).strip()
        owner = str(raw.get("owner", "")).strip()
        task_text = str(raw.get("task", "")).strip()
        mode = str(raw.get("mode", "analyze")).strip().lower()
        write_scope = str(raw.get("write_scope", "")).strip()
        done_when = str(raw.get("done_when", "")).strip()
        deps = raw.get("depends_on", [])
        if not task_id or task_id in ids:
            raise ValueError(f"task id 缺失或重复：{task_id!r}")
        if owner not in workers:
            raise ValueError(f"task {task_id} owner 未在成员中：{owner!r}")
        if not task_text:
            raise ValueError(f"task {task_id} 内容为空")
        if mode not in VALID_MODES:
            raise ValueError(f"task {task_id} mode 非法：{mode!r}")
        if not isinstance(deps, list):
            raise ValueError(f"task {task_id} depends_on 必须是数组")
        ids.add(task_id)
        normalized.append(
            {
                "id": task_id,
                "owner": owner,
                "task": task_text,
                "mode": mode,
                "write_scope": write_scope,
                "done_when": done_when,
                "depends_on": [str(item).strip() for item in deps],
            }
        )

    by_id = {task["id"]: task for task in normalized}
    for task in normalized:
        for dep in task["depends_on"]:
            if dep not in by_id or dep == task["id"]:
                raise ValueError(f"task {task['id']} 依赖无效：{dep!r}")

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(task_id: str) -> None:
        if task_id in visiting:
            raise ValueError("任务依赖存在环")
        if task_id in visited:
            return
        visiting.add(task_id)
        for dep in by_id[task_id]["depends_on"]:
            visit(dep)
        visiting.remove(task_id)
        visited.add(task_id)

    for task_id in by_id:
        visit(task_id)

    def depends_transitively(task_id: str, ancestor: str) -> bool:
        return any(
            dep == ancestor or depends_transitively(dep, ancestor)
            for dep in by_id[task_id]["depends_on"]
        )

    writers = [task for task in normalized if task["write_scope"]]
    for index, left in enumerate(writers):
        for right in writers[index + 1 :]:
            if left["write_scope"] != right["write_scope"]:
                continue
            ordered = depends_transitively(left["id"], right["id"]) or depends_transitively(
                right["id"], left["id"]
            )
            if not ordered:
                raise ValueError(
                    f"写入范围 {left['write_scope']!r} 的任务 "
                    f"{left['id']}/{right['id']} 必须用依赖串行"
                )

    return {"summary": str(plan.get("summary", "")).strip(), "tasks": normalized}
