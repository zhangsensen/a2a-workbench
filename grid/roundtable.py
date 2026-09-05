"""本地 A2A 圆桌调度：主持人分工、成员协作、交叉复核、最终收口。

用法：
    python roundtable.py "任务目标"
    python roundtable.py "任务目标" --moderator claude --agents pi codex dsh
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable

from a2a_call import call_agent, load_catalog


BASE = Path(__file__).resolve().parent
STATE_DIR = BASE / "data" / "roundtables"
VALID_MODES = {"execute", "analyze", "review"}
MAX_CONTEXT = 18000
Caller = Callable[[str, str], Awaitable[str]]


def _clip(text: str, limit: int = 4000) -> str:
    text = str(text)
    return text if len(text) <= limit else text[:limit] + "\n...(已截断)"


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
    """校验主持人的任务 DAG，并阻止同一写入范围无依赖并发。"""
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


class RoundTable:
    def __init__(
        self,
        objective: str,
        moderator: str,
        workers: list[str],
        *,
        peer_review: bool = True,
        caller: Caller = call_agent,
        state_dir: Path = STATE_DIR,
    ) -> None:
        if not objective.strip():
            raise ValueError("任务目标不能为空")
        if moderator in workers:
            raise ValueError("主持人不能同时作为执行成员")
        if not workers:
            raise ValueError("至少需要一个执行成员")
        self.caller = caller
        self.peer_review = peer_review
        self.state_dir = state_dir
        self.state = {
            "id": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8],
            "objective": objective.strip(),
            "moderator": moderator,
            "workers": workers,
            "status": "planning",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "plan": None,
            "results": {},
            "reviews": {},
            "final": None,
        }

    @property
    def state_path(self) -> Path:
        return self.state_dir / f"{self.state['id']}.json"

    def save(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        temp = self.state_path.with_suffix(".tmp")
        temp.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(self.state_path)

    async def _make_plan(self) -> dict:
        workers = self.state["workers"]
        prompt = f"""你是本地 A2A 圆桌主持人。请把目标拆成可执行、可验证的任务 DAG。

目标：{self.state['objective']}
执行成员：{', '.join(workers)}

只返回 JSON，不要 Markdown：
{{
  "summary": "分工摘要",
  "tasks": [
    {{
      "id": "T1",
      "owner": "成员名",
      "task": "明确任务",
      "mode": "execute|analyze|review",
      "write_scope": "会写入的同一仓库/服务/文件范围；纯只读则为空字符串",
      "depends_on": [],
      "done_when": "可验证完成条件"
    }}
  ]
}}

规则：这里只安排实质执行/分析任务，交叉复核和最终汇总由编排器自动完成，不要把它们再列成任务。任务要覆盖目标且尽量不重叠；可独立的任务并行。相同 write_scope 的任务必须用 depends_on 串行；同一成员不要安排可同时就绪的多个任务；不要让成员继续转派。"""
        reply = await self.caller(self.state["moderator"], prompt)
        try:
            return validate_plan(extract_json_object(reply), workers)
        except (ValueError, json.JSONDecodeError) as error:
            repair = f"""上一版分工计划校验失败：{error}
请严格按原 schema 修正，只返回 JSON。
目标：{self.state['objective']}
执行成员：{', '.join(workers)}
上一版：{_clip(reply, 7000)}"""
            repaired = await self.caller(self.state["moderator"], repair)
            return validate_plan(extract_json_object(repaired), workers)

    async def _execute_task(self, task: dict, dependency_results: dict[str, str]) -> str:
        deps = "\n\n".join(
            f"[{task_id}]\n{_clip(result)}" for task_id, result in dependency_results.items()
        ) or "无"
        prompt = f"""你是 A2A 圆桌成员 {task['owner']}，请完成主持人分给你的任务。

总目标：{self.state['objective']}
你的任务 {task['id']}：{task['task']}
模式：{task['mode']}
写入范围：{task['write_scope'] or '只读'}
完成条件：{task['done_when'] or '给出可核验结果'}
前置任务结果：
{deps}

直接执行属于当前任务且已获授权的工作；只处理你的范围，不再转派。回复结论、验证证据和未解决项。"""
        return await self.caller(task["owner"], prompt)

    async def _run_tasks(self, plan: dict) -> None:
        pending = {task["id"]: task for task in plan["tasks"]}
        results: dict[str, str] = self.state["results"]
        while pending:
            ready = [
                task
                for task in pending.values()
                if all(dep in results for dep in task["depends_on"])
            ]
            selected: list[dict] = []
            owners: set[str] = set()
            for task in ready:
                if task["owner"] not in owners:
                    selected.append(task)
                    owners.add(task["owner"])
            if not selected:
                raise RuntimeError("没有可执行任务，依赖图无法推进")

            calls = [
                self._execute_task(
                    task,
                    {dep: results[dep] for dep in task["depends_on"]},
                )
                for task in selected
            ]
            replies = await asyncio.gather(*calls, return_exceptions=True)
            for task, reply in zip(selected, replies):
                if isinstance(reply, BaseException):
                    results[task["id"]] = f"(执行失败: {type(reply).__name__}: {reply})"
                else:
                    results[task["id"]] = reply
                pending.pop(task["id"])
            self.save()

    async def _peer_reviews(self, plan: dict) -> None:
        tasks = plan["tasks"]
        workers = self.state["workers"]
        calls = []
        review_ids = []
        for index, task in enumerate(tasks):
            candidates = [worker for worker in workers if worker != task["owner"]]
            reviewer = candidates[index % len(candidates)] if candidates else self.state["moderator"]
            prompt = f"""你是 A2A 圆桌交叉复核人 {reviewer}。只做只读复核，不修改任何内容。

总目标：{self.state['objective']}
被复核任务 {task['id']}：{task['task']}
完成条件：{task['done_when']}
成员结果：
{_clip(self.state['results'][task['id']], 6000)}

指出结论是否有证据支撑、冲突或遗漏；给出“通过”或明确的补救建议。"""
            calls.append(self.caller(reviewer, prompt))
            review_ids.append(task["id"])

        replies = await asyncio.gather(*calls, return_exceptions=True)
        for task_id, reply in zip(review_ids, replies):
            if isinstance(reply, BaseException):
                self.state["reviews"][task_id] = f"(复核失败: {type(reply).__name__}: {reply})"
            else:
                self.state["reviews"][task_id] = reply
        self.save()

    async def _synthesize(self, plan: dict) -> str:
        sections = []
        for task in plan["tasks"]:
            task_id = task["id"]
            sections.append(
                f"[{task_id} / {task['owner']}]\n"
                f"结果：{_clip(self.state['results'][task_id])}\n"
                f"复核：{_clip(self.state['reviews'].get(task_id, '未启用'))}"
            )
        evidence = "\n\n".join(sections)
        prompt = f"""你是 A2A 圆桌主持人，请根据成员执行和交叉复核结果最终收口。

总目标：{self.state['objective']}
计划摘要：{plan['summary']}
成员材料：
{_clip(evidence, MAX_CONTEXT)}

输出面向用户的最终结论：已完成什么、关键验证证据、仍未解决什么。不要虚构，不要再次派单。"""
        return await self.caller(self.state["moderator"], prompt)

    async def run(self) -> str:
        self.save()
        try:
            plan = await self._make_plan()
            self.state["plan"] = plan
            self.state["status"] = "executing"
            self.save()
            await self._run_tasks(plan)
            if self.peer_review:
                self.state["status"] = "reviewing"
                self.save()
                await self._peer_reviews(plan)
            self.state["status"] = "synthesizing"
            self.save()
            self.state["final"] = await self._synthesize(plan)
            self.state["status"] = "completed"
            self.state["completed_at"] = datetime.now(timezone.utc).isoformat()
            self.save()
            return self.state["final"]
        except BaseException as error:
            self.state["status"] = "failed"
            self.state["error"] = f"{type(error).__name__}: {error}"
            self.save()
            raise


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="本地 A2A 圆桌协作")
    parser.add_argument("objective", help="需要圆桌协作完成的目标")
    parser.add_argument("--moderator", default="claude", help="主持人 agent（默认 claude）")
    parser.add_argument("--agents", nargs="+", help="执行成员；默认使用除主持人外全部已注册 agent")
    parser.add_argument("--no-peer-review", action="store_true", help="跳过成员交叉复核")
    return parser.parse_args()


async def main() -> int:
    args = parse_args()
    registered = [agent["name"] for agent in load_catalog()["agents"] if agent.get("status", "active") == "active"]
    workers = args.agents or [name for name in registered if name != args.moderator]
    unknown = [name for name in [args.moderator, *workers] if name not in registered]
    if unknown:
        raise SystemExit(f"未注册或未启用的 agent：{unknown}；可用：{registered}")
    if len(set(workers)) != len(workers):
        raise SystemExit("执行成员不能重复")

    table = RoundTable(
        args.objective,
        args.moderator,
        workers,
        peer_review=not args.no_peer_review,
    )
    print(f"roundtable_id={table.state['id']}", file=sys.stderr)
    print(await table.run())
    print(f"state={table.state_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
