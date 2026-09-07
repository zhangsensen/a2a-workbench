from __future__ import annotations

import asyncio
import contextlib
import fcntl
import hashlib
import inspect
import json
import logging
import os
import re
import subprocess
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import uvicorn
from a2a.client import A2ACardResolver, ClientConfig, create_client
from a2a.helpers import new_text_message
from a2a.types import CancelTaskRequest, GetTaskRequest, Role, SendMessageRequest, TaskState
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator

from room_agents import Member
from settings import ROOT, DATA, PORT, BASE_URL, VERSION, EXECUTORS
from room_store import MEMBERS, RoomStore

LOGGER = logging.getLogger('a2a-roundtable')
EVENTS_JSON_BYTE_LIMIT = 60000
RECONCILE_INITIAL_DELAY = 2.0
RECONCILE_MAX_DELAY = 60.0

# 远端仍在推进：不是终态，也不是"结果不确定"，保持现状等下一轮。
REMOTE_STILL_RUNNING = frozenset({
    TaskState.TASK_STATE_WORKING,
    TaskState.TASK_STATE_SUBMITTED,
})
# 远端停在需要人介入的状态：对无人值守的执行手等同失败，如实落 failed。
REMOTE_NEEDS_HUMAN = frozenset({
    TaskState.TASK_STATE_REJECTED,
    TaskState.TASK_STATE_INPUT_REQUIRED,
    TaskState.TASK_STATE_AUTH_REQUIRED,
})


def bounded_events_json(events, limit=EVENTS_JSON_BYTE_LIMIT):
    """Serialize room events as JSON, dropping the oldest entries to stay within a UTF-8 byte budget.

    Returns (payload, omitted) where omitted is how many of the oldest events were dropped.
    """
    remaining = list(events)
    omitted = 0
    while True:
        payload = json.dumps([{'seq': e['seq'], 'speaker': e['speaker'], 'text': e['text']} for e in remaining], ensure_ascii=False)
        if len(payload.encode('utf-8')) <= limit or not remaining:
            return payload, omitted
        remaining = remaining[1:]
        omitted += 1


def execution_context(room, cwd):
    if not cwd:
        return room
    canonical_cwd = str(Path(cwd).resolve())
    digest = hashlib.sha256(canonical_cwd.encode()).hexdigest()[:8]
    return f'{room}-{digest}'


def task_state_name(state):
    try:
        return TaskState.Name(state)
    except (TypeError, ValueError):
        return getattr(state, 'name', str(state))


def task_text(task):
    parts = []
    for artifact in getattr(task, 'artifacts', []) or []:
        for part in getattr(artifact, 'parts', []) or []:
            if getattr(part, 'text', ''):
                parts.append(part.text)
    status_message = getattr(getattr(task, 'status', None), 'message', None)
    for part in getattr(status_message, 'parts', []) or []:
        if getattr(part, 'text', ''):
            parts.append(part.text)
    return '\n'.join(parts) if parts else '(no text returned by executor)'


async def call_executor(url, prompt, room, cwd=None, on_task_id=None):
    started = time.monotonic()
    # 读超时比执行手的 600s TIMEOUT 多 60s 宽限：两者相等时真超时会让本端与
    # 远端同时放弃，job 只能落成不透明的通信异常，拿不到执行手诚实的超时终态。
    http = httpx.AsyncClient(timeout=httpx.Timeout(660.0, connect=10.0))
    client = None
    try:
        card = await A2ACardResolver(httpx_client=http, base_url=url).get_agent_card()
        client = await create_client(
            agent=card, client_config=ClientConfig(streaming=False, httpx_client=http)
        )
        metadata = {'context': execution_context(room, cwd)}
        if cwd:
            metadata['cwd'] = cwd
        request = SendMessageRequest(
            message=new_text_message(prompt, role=Role.ROLE_USER),
            metadata=metadata,
        )
        parts = []
        final_state = None
        remote_task_id = None
        async for chunk in client.send_message(request):
            task = getattr(chunk, 'task', None)
            task_id = getattr(task, 'id', None)
            if task_id and remote_task_id is None:
                remote_task_id = task_id
                if on_task_id is not None:
                    callback_result = on_task_id(task_id)
                    if inspect.isawaitable(callback_result):
                        await callback_result
            status = getattr(task, 'status', None)
            if status is not None:
                final_state = status.state
            for artifact in getattr(task, 'artifacts', []) or []:
                for part in artifact.parts:
                    if getattr(part, 'text', ''):
                        parts.append(part.text)
        duration = time.monotonic() - started
        return final_state, '\n'.join(parts) if parts else '(no text returned by executor)', duration
    finally:
        if client is not None:
            await client.close()
        await http.aclose()


async def cancel_remote(url, task_id):
    http = httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=10.0))
    client = None
    try:
        async with asyncio.timeout(10):
            card = await A2ACardResolver(httpx_client=http, base_url=url).get_agent_card()
            client = await create_client(
                agent=card, client_config=ClientConfig(streaming=False, httpx_client=http)
            )
            task = await client.cancel_task(CancelTaskRequest(id=task_id))
            return task.status.state
    finally:
        if client is not None:
            await client.close()
        await http.aclose()


async def get_remote(url, task_id):
    http = httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=10.0))
    client = None
    try:
        async with asyncio.timeout(10):
            card = await A2ACardResolver(httpx_client=http, base_url=url).get_agent_card()
            client = await create_client(
                agent=card, client_config=ClientConfig(streaming=False, httpx_client=http)
            )
            return await client.get_task(GetTaskRequest(id=task_id))
    finally:
        if client is not None:
            await client.close()
        await http.aclose()


def execution_metadata(job, attempt, duration):
    metadata = {
        'remote_task_id': attempt['remote_task_id'],
        'cwd': job['cwd'],
        'duration': duration,
        'attempt': attempt['attempt'],
    }
    cwd = job['cwd']
    if cwd and Path(cwd).is_dir():
        commands = {
            'git_log': ['git', '-C', cwd, 'log', '-1', '--oneline'],
            'git_status': ['git', '-C', cwd, 'status', '--short'],
        }
        for field, command in commands.items():
            try:
                result = subprocess.run(
                    command, capture_output=True, text=True, timeout=5, check=False
                )
                if result.returncode == 0:
                    metadata[field] = result.stdout.strip()
            except (OSError, subprocess.TimeoutExpired):
                pass
    return metadata


class Discussion:
    def __init__(self, store, member_factory=Member):
        self.store = store
        self.member_factory = member_factory
        self.members = {}
        self.queue = asyncio.Queue()
        self.worker = None
        self.keeper = None
        self.current = None
        self.current_id = None
        self.stopping = False
        self.execute_locks = {}
        self.execute_tasks = {}
        self.reconcile_task = None

    def member(self, room, name):
        key = (room, name)
        if key not in self.members:
            self.members[key] = self.member_factory(self.store, room, name)
        return self.members[key]

    async def warm_room(self, room):
        results = await asyncio.gather(*(self.member(room, n).warm() for n in MEMBERS), return_exceptions=True)
        for name, result in zip(MEMBERS, results):
            if isinstance(result, Exception):
                LOGGER.warning('room=%s member=%s warm failed (%s)', room, name, type(result).__name__)

    async def close_room(self, room):
        # Archiving releases the warmed native processes for this room; new activity re-warms lazily.
        keys = [key for key in self.members if key[0] == room]
        closing = [self.members.pop(key).close() for key in keys]
        await asyncio.gather(*closing, return_exceptions=True)

    async def start(self):
        self.store.create_room('lobby', 'Lobby / 公共圆桌')
        for key in self.store.recover():
            if self.store.job(key)['kind'] == 'execute':
                self._schedule_execute(key)
            else:
                await self.queue.put(key)
        self.reconcile_task = asyncio.create_task(self._reconcile_executions())
        # Warm processes without spending tokens on fake conversation turns. Archived rooms stay cold.
        for room in self.store.rooms():
            if room['archived_at'] is None:
                await self.warm_room(room['id'])
        self.worker = asyncio.create_task(self._work())
        self.keeper = asyncio.create_task(self._keep_warm())

    async def _keep_warm(self):
        while True:
            await asyncio.sleep(30)
            active = {r['id'] for r in self.store.rooms() if r['archived_at'] is None}
            for member in list(self.members.values()):
                if member.room not in active:
                    continue
                status = member.status()
                if not status['processAlive'] and status['state'] == 'ready':
                    try:
                        await member.warm()
                    except Exception:
                        pass  # Error is durable/visible. No blind model retries.

    def submit(self, room, prompt, members=None, rounds=1, key=None, speaker='user'):
        job = self.store.submit(room, prompt, list(MEMBERS if members is None else members), rounds, key, speaker)
        if job['state'] == 'queued':
            self.queue.put_nowait(job['id'])
        return job

    # 安全不变量：执行 job 只能从外部入口（HTTP/MCP）进来，代码中不存在从成员回复文本到 submit_execute 的任何通路；成员消息处理路径一行都不要碰。
    def submit_execute(self, room, prompt, executor, key, speaker='master', cwd=None):
        job = self.store.submit_execute(room, prompt, executor, key, speaker, cwd)
        if job['state'] == 'queued' and job['id'] not in self.execute_tasks:
            self._schedule_execute(job['id'])
        return job

    def _schedule_execute(self, key):
        # Execute work is deliberately independent of the serial discussion queue.
        task = asyncio.create_task(self._run_execute(key))
        self.execute_tasks[key] = task
        task.add_done_callback(lambda done, job=key: self.execute_tasks.pop(job, None))

    async def _run_execute(self, key):
        job = self.store.job(key)
        lock = self.execute_locks.setdefault(job['executor'], asyncio.Lock())
        async with lock:
            if not self.store.begin(key):
                return
            endpoint = EXECUTORS[job['executor']]
            attempt_number = self.store.add_attempt(key, endpoint)
            started = time.monotonic()

            async def remember_remote_task(task_id):
                self.store.set_attempt_remote(key, attempt_number, task_id)
                if self.store.job(key)['state'] == 'cancel_requested':
                    await self._cancel_known_attempt(key, self.store.latest_attempt(key))

            try:
                final_state, text, duration = await call_executor(
                    endpoint, job['prompt'], job['room'], job['cwd'], remember_remote_task
                )
                if final_state == TaskState.TASK_STATE_COMPLETED:
                    await self._finish_execute(key, attempt_number, 'completed', text, duration)
                elif final_state == TaskState.TASK_STATE_FAILED:
                    await self._finish_execute(key, attempt_number, 'failed', text, duration, text)
                elif final_state == TaskState.TASK_STATE_CANCELED:
                    self.store.set_attempt_state(key, attempt_number, 'cancelled')
                    self.store.finish_if(
                        key, {'running', 'cancel_requested', 'outcome_unknown'}, 'cancelled'
                    )
                else:
                    state = task_state_name(final_state)
                    error = f'Executor ended in unexpected state {state}: {text}'
                    await self._finish_execute(
                        key, attempt_number, 'failed', error, duration, error
                    )
            except asyncio.CancelledError:
                if self.stopping and self.store.job(key)['state'] == 'running':
                    attempt = self.store.latest_attempt(key)
                    if attempt and attempt['remote_task_id']:
                        self.store.set_attempt_state(key, attempt_number, 'outcome_unknown')
                        self.store.finish(
                            key, 'outcome_unknown',
                            'Service stopped after remote execution started; reconciliation required',
                        )
                    else:
                        self.store.set_attempt_state(key, attempt_number, 'interrupted')
                        self.store.finish(key, 'interrupted', 'Service stopped during execution')
                raise
            except Exception as exc:
                error = str(exc).strip() or f'{type(exc).__name__} during execution'
                attempt = self.store.latest_attempt(key)
                if attempt and attempt['remote_task_id']:
                    reconciliation_error = (
                        'Executor communication failed after remote task started; '
                        f'reconciliation required: {error}'
                    )
                    changed = self.store.finish_if(
                        key,
                        {'running', 'cancel_requested', 'outcome_unknown'},
                        'outcome_unknown',
                        reconciliation_error,
                    )
                    if changed:
                        self.store.set_attempt_state(
                            key, attempt_number, 'outcome_unknown'
                        )
                else:
                    await self._finish_execute(
                        key,
                        attempt_number,
                        'failed',
                        error,
                        time.monotonic() - started,
                        error,
                    )
                LOGGER.error('job=%s executor=%s failed (%s)', key, job['executor'], type(exc).__name__)

    async def _finish_execute(self, key, attempt_number, state, text, duration, error=None):
        job = self.store.job(key)
        attempt = self.store.latest_attempt(key)
        metadata = await asyncio.to_thread(execution_metadata, job, attempt, duration)
        finished = self.store.finish_execute(key, state, text, error, metadata)
        if finished:
            self.store.set_attempt_state(key, attempt_number, state)
        return finished

    async def _cancel_known_attempt(self, key, attempt):
        try:
            remote_state = await cancel_remote(attempt['endpoint'], attempt['remote_task_id'])
        except Exception as exc:
            error = str(exc).strip() or type(exc).__name__
            self.store.set_attempt_state(key, attempt['attempt'], 'outcome_unknown')
            self.store.finish_if(
                key, {'cancel_requested', 'outcome_unknown'}, 'outcome_unknown',
                f'Remote cancellation outcome unknown: {error}',
            )
            return
        if remote_state == TaskState.TASK_STATE_CANCELED:
            self.store.set_attempt_state(key, attempt['attempt'], 'cancelled')
            self.store.finish_if(
                key, {'cancel_requested', 'outcome_unknown'}, 'cancelled'
            )
            return
        state = task_state_name(remote_state)
        self.store.set_attempt_state(key, attempt['attempt'], 'outcome_unknown')
        self.store.finish_if(
            key, {'cancel_requested', 'outcome_unknown'}, 'outcome_unknown',
            f'Remote cancellation returned {state}; outcome unknown',
        )

    async def _reconcile_executions(self):
        delay = RECONCILE_INITIAL_DELAY
        while True:
            keys = await self._reconcile_once()
            if keys:
                await asyncio.sleep(delay)
                delay = min(delay * 2, RECONCILE_MAX_DELAY)
            else:
                # 空闲时按上限空转；退避重置为初始值，这样下一个进入未决状态的
                # 执行能在 2 秒内被首次对账，而不是继承上一轮涨到 60 秒的退避。
                delay = RECONCILE_INITIAL_DELAY
                await asyncio.sleep(RECONCILE_MAX_DELAY)

    async def _reconcile_once(self):
        """对账一轮，返回本轮处理的 job 列表（便于测试确定性驱动）。"""
        # 只对账"没人照看"的 attempt：本进程仍持有活跃 execute task 的 job
        # 由 _run_execute 自己收敛。否则对账会与正在进行的执行抢状态——
        # 正常运行中的远端返回 WORKING，一旦被写成 outcome_unknown，
        # finish_execute（要求 state='running'）就再也写不进结果，
        # 执行手真实完成的产出会被静默丢弃。
        keys = [
            key for key in self.store.reconcilable_executions()
            if key not in self.execute_tasks
        ]
        for key in keys:
            attempt = self.store.latest_attempt(key)
            try:
                remote_task = await get_remote(
                    attempt['endpoint'], attempt['remote_task_id']
                )
                remote_state = remote_task.status.state
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.store.record_reconcile_check(key, attempt['attempt'])
                error = str(exc).strip() or type(exc).__name__
                changed = self.store.finish_if(
                    key,
                    {'running', 'cancel_requested', 'outcome_unknown'},
                    'outcome_unknown',
                    f'Remote task reconciliation failed: {error}',
                )
                if changed:
                    self.store.set_attempt_state(
                        key, attempt['attempt'], 'outcome_unknown'
                    )
                continue

            self.store.record_reconcile_check(key, attempt['attempt'])
            duration = max(0.0, time.time() - attempt['created'])
            if remote_state == TaskState.TASK_STATE_COMPLETED:
                await self._finish_execute(
                    key,
                    attempt['attempt'],
                    'completed',
                    task_text(remote_task),
                    duration,
                )
            elif remote_state == TaskState.TASK_STATE_FAILED:
                text = task_text(remote_task)
                await self._finish_execute(
                    key, attempt['attempt'], 'failed', text, duration, text
                )
            elif remote_state == TaskState.TASK_STATE_CANCELED:
                changed = self.store.finish_if(
                    key,
                    {'running', 'cancel_requested', 'outcome_unknown'},
                    'cancelled',
                )
                if changed:
                    self.store.set_attempt_state(
                        key, attempt['attempt'], 'cancelled'
                    )
            elif remote_state in REMOTE_STILL_RUNNING:
                # 远端还在跑：这不是"结果不确定"，等下一轮再看。把它写成
                # outcome_unknown 会误导 master（正常执行被报成不确定），
                # 而且此后 finish_execute 写不进结果，产出会被丢弃。
                continue
            elif remote_state in REMOTE_NEEDS_HUMAN:
                state = task_state_name(remote_state)
                text = f'Remote task ended in {state}; human intervention required'
                await self._finish_execute(
                    key, attempt['attempt'], 'failed', text, duration, text
                )
            else:
                state = task_state_name(remote_state)
                changed = self.store.finish_if(
                    key,
                    {'running', 'cancel_requested', 'outcome_unknown'},
                    'outcome_unknown',
                    f'Remote task is {state}; outcome unknown',
                )
                if changed:
                    self.store.set_attempt_state(
                        key, attempt['attempt'], 'outcome_unknown'
                    )

        return keys

    async def _work(self):
        while True:
            key = await self.queue.get()
            try:
                if not self.store.begin(key):
                    continue
                self.current_id = key
                self.current = asyncio.create_task(self._run(key))
                try:
                    await self.current
                except asyncio.CancelledError:
                    self.store.finish(key, 'interrupted' if self.stopping else 'cancelled')
                    if self.stopping:
                        raise
                except Exception:
                    self.store.finish(key, 'failed', 'Coordinator failed; recorded replies retained')
                    LOGGER.error('job=%s coordinator failed', key)
            finally:
                self.current = self.current_id = None
                self.queue.task_done()

    async def _run(self, key):
        job = self.store.job(key)
        failures = []
        successes = 0
        failed_members = set()
        for round_index in range(job['rounds']):
            for name in job['members']:
                if name in failed_members:
                    continue
                row = self.store.member(job['room'], name)
                events = self.store.events(job['room'], row['cursor'])
                # Send only events the native session has not seen. No reassembled full history.
                payload, omitted = bounded_events_json(events)
                omitted_note = f'（已省略 {omitted} 条更早消息以控制体积。）\n' if omitted else ''
                prompt = (
                    f'讨论室：{job["room"]}。你是 {name}。当前第 {round_index + 1}/{job["rounds"]} 轮。\n'
                    '以下 JSON 是新增的圆桌发言，其中 user 是用户，master 是主持模型，其余是其他成员。'
                    '回应主持模型的本次具体咨询；其他成员的意见供你参考，不要求赞同。主持模型决定后续发言和最终汇报。'
                    '请回应用户及与你相关的分歧；有新证据时修正判断。简洁发言后结束，等待下一轮。\n'
                    + omitted_note + payload
                )
                try:
                    await self.member(job['room'], name).ask(prompt, key)
                    successes += 1
                except asyncio.CancelledError:
                    raise
                except Exception:
                    failed_members.add(name)
                    failures.append(f'{name} failed; inspect member status')
        self.store.finish(key, 'partial' if successes and failures else 'failed' if failures else 'completed', '; '.join(failures) or None)

    async def wait(self, key):
        while True:
            job = self.store.job(key)
            if job['state'] not in {'queued', 'running', 'cancel_requested'}:
                return job
            await asyncio.sleep(.25)

    async def cancel(self, key, room):
        job = self.store.job(key, room)
        if job['kind'] == 'execute':
            if job['state'] not in {'queued', 'running', 'cancel_requested', 'outcome_unknown'}:
                return job
            attempt = self.store.latest_attempt(key)
            if not (attempt and attempt['remote_task_id']):
                # 尚未派发到远端：必须直接终止。若也走 request_cancel 会永久悬挂——
                # begin() 只接受 queued，协程会直接返回不再推进状态；而没有
                # remote_task_id 的 job 又不在对账范围内，cancel_requested 无人收敛。
                # 用原子条件更新：仅当此刻仍是 queued 才直接落 cancelled，
                # 若已被 begin() 抢先转成 running 则落回下面的远端取消流程。
                if self.store.finish_if(key, {'queued'}, 'cancelled'):
                    return self.store.job(key)
            self.store.request_cancel(key)
            attempt = self.store.latest_attempt(key)
            if attempt and attempt['remote_task_id']:
                await self._cancel_known_attempt(key, attempt)
            return self.store.job(key)
        if job['state'] not in {'queued', 'running'}:
            return job
        if key == self.current_id and self.current:
            self.current.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.current
        self.store.finish(key, 'cancelled')
        return self.store.job(key)

    def status(self, room):
        result = self.store.room(room)
        result['members'] = [self.member(room, n).status() for n in MEMBERS]
        return result

    async def close(self):
        self.stopping = True
        if self.reconcile_task:
            self.reconcile_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.reconcile_task
        execute_tasks = list(self.execute_tasks.values())
        for task in execute_tasks:
            task.cancel()
        await asyncio.gather(*execute_tasks, return_exceptions=True)
        for task in (self.keeper, self.worker):
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        await asyncio.gather(*(m.close() for m in self.members.values()), return_exceptions=True)


class RoomInput(BaseModel):
    id: str = Field(pattern=r'^[a-zA-Z0-9_-]{1,80}$')
    title: str = Field(min_length=1, max_length=200)


class MessageInput(BaseModel):
    text: str = Field(min_length=1, max_length=50000)
    members: list[str] = Field(default_factory=lambda: list(MEMBERS))
    rounds: int = Field(default=1, ge=1, le=5)
    requestId: str = Field(default_factory=lambda: str(uuid.uuid4()), min_length=1, max_length=100)


class ConsultInput(BaseModel):
    member: str = Field(pattern='^(' + '|'.join(re.escape(m) for m in MEMBERS) + ')$')
    text: str = Field(min_length=1, max_length=50000)
    requestId: str = Field(min_length=1, max_length=100)


class ExecuteInput(BaseModel):
    executor: str = Field(min_length=1)
    text: str = Field(min_length=1, max_length=50000)
    requestId: str = Field(min_length=1, max_length=100)
    cwd: str | None = None

    @field_validator('cwd')
    @classmethod
    def strip_nonempty_cwd(cls, value):
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError('cwd must be a nonempty string')
        return value


class CheckpointInput(BaseModel):
    expectedRevision: int = Field(ge=0, strict=True)
    goal: str = Field(min_length=1, max_length=2000)
    summary: str = Field(max_length=12000)
    openQuestions: list[str] = Field(max_length=30)
    nextAction: str = Field(max_length=2000)
    throughSeq: int = Field(ge=0, strict=True)


def create_app(data=DATA, member_factory=Member, allowed_origins=None):
    allowed_origins = allowed_origins or {BASE_URL, f'http://localhost:{PORT}'}
    store = RoomStore(Path(data) / 'rooms.sqlite3')
    discussion = Discussion(store, member_factory)

    @asynccontextmanager
    async def lifespan(app):
        await discussion.start()
        try:
            yield
        finally:
            await discussion.close()
            await app.state.a2a_handler.aclose()
            await app.state.a2a_engine.dispose()

    app = FastAPI(title='常驻 A2A 圆桌', lifespan=lifespan)
    app.state.discussion = discussion

    @app.middleware('http')
    async def local_only(request: Request, call_next):
        host = request.headers.get('host', '').split(':')[0]
        origin = request.headers.get('origin')
        if host not in {'localhost', '127.0.0.1', 'testserver'}:
            return JSONResponse({'error': 'Loopback host required'}, status_code=403)
        if origin and origin not in allowed_origins:
            return JSONResponse({'error': 'Cross-origin access denied'}, status_code=403)
        return await call_next(request)

    @app.exception_handler(KeyError)
    async def missing(request, exc):
        return JSONResponse({'error': 'Room or job not found'}, status_code=404)

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        return JSONResponse({'error': str(exc)}, status_code=400)

    @app.get('/')
    async def index():
        return FileResponse(ROOT / 'roundtable.html')

    @app.get('/roundtable_ui.js')
    async def ui_script():
        return FileResponse(ROOT / 'roundtable_ui.js', media_type='text/javascript')

    @app.get('/healthz')
    async def health():
        rooms = [discussion.status(r['id']) for r in store.rooms()]
        all_members = [m for r in rooms for m in r['members']]
        ready = bool(all_members) and all(m['processAlive'] and m['state'] in {'ready', 'busy'} for m in all_members)
        return {'serviceAlive': True, 'ready': ready, 'mode': 'persistent-roundtable',
                'pid': os.getpid(), 'currentJob': discussion.current_id,
                'queueSize': discussion.queue.qsize(), 'rooms': rooms, 'version': VERSION}

    @app.get('/api/rooms')
    async def rooms():
        return store.rooms()

    @app.post('/api/rooms')
    async def create_room(body: RoomInput):
        active_ids = {r['id'] for r in store.rooms() if r['archived_at'] is None}
        if len(active_ids) >= 8 and body.id not in active_ids:
            raise HTTPException(409, 'At most eight warm rooms are supported')
        result = store.create_room(body.id, body.title)
        await discussion.warm_room(body.id)
        return discussion.status(body.id)

    @app.post('/api/rooms/{room}/archive')
    async def archive_room(room: str):
        result = store.archive_room(room)
        await discussion.close_room(room)
        return result

    @app.delete('/api/rooms/{room}/archive')
    async def unarchive_room(room: str):
        result = store.unarchive_room(room)
        await discussion.warm_room(room)
        return result

    @app.get('/api/rooms/{room}')
    async def status(room: str):
        return discussion.status(room)

    @app.get('/api/rooms/{room}/messages')
    async def messages(room: str, after: int = 0):
        store.room(room)
        return store.events(room, after)

    @app.post('/api/rooms/{room}/messages')
    async def submit(room: str, body: MessageInput):
        return discussion.submit(room, body.text, body.members, body.rounds, body.requestId)

    @app.get('/api/rooms/{room}/jobs')
    async def room_jobs(room: str):
        return store.room_jobs(room)

    @app.post('/api/rooms/{room}/consult')
    async def consult(room: str, body: ConsultInput):
        # The calling model is the master. One selected peer replies once, then control returns.
        return discussion.submit(room, body.text, [body.member], 1, body.requestId, speaker='master')

    @app.post('/api/rooms/{room}/execute')
    async def execute(room: str, body: ExecuteInput):
        # This explicit external entry point is the only route from HTTP into execution.
        return discussion.submit_execute(
            room, body.text, body.executor, body.requestId, cwd=body.cwd
        )

    @app.get('/api/rooms/{room}/context')
    async def master_context(room: str, after: int | None = None, limit: int = 50):
        return store.master_context(room, after, limit)

    @app.post('/api/rooms/{room}/checkpoint')
    async def checkpoint(room: str, body: CheckpointInput):
        return store.checkpoint(room, body.expectedRevision, body.goal, body.summary,
                                body.openQuestions, body.nextAction, body.throughSeq)

    @app.get('/api/jobs/{key}')
    async def job(key: str, room: str, waitSeconds: int = 0):
        if not 0 <= waitSeconds <= 25:
            raise ValueError('waitSeconds must be between 0 and 25')
        store.job(key, room)  # Check ownership before waiting.
        if waitSeconds:
            try:
                await asyncio.wait_for(discussion.wait(key), waitSeconds)
            except TimeoutError:
                pass  # Still running is a receipt, not a failure and never a resubmission.
        return store.job(key, room)

    @app.post('/api/jobs/{key}/cancel')
    async def cancel(key: str, room: str):
        return await discussion.cancel(key, room)

    @app.post('/api/rooms/{room}/warm')
    async def warm(room: str):
        store.room(room)
        await discussion.warm_room(room)
        return discussion.status(room)

    add_standard_a2a(app, discussion, Path(data))
    return app


def add_standard_a2a(app, discussion, data):
    from a2a.server.agent_execution.agent_executor import AgentExecutor
    from a2a.server.request_handlers import DefaultRequestHandler
    from a2a.server.routes import add_a2a_routes_to_fastapi, create_agent_card_routes, create_jsonrpc_routes, create_rest_routes
    from a2a.server.tasks.database_task_store import DatabaseTaskStore
    from a2a.server.tasks.task_updater import TaskUpdater
    from a2a.types import AgentCard, AgentCapabilities, AgentInterface, AgentSkill, Part, Task, TaskState, TaskStatus
    from sqlalchemy.ext.asyncio import create_async_engine
    from google.protobuf.json_format import MessageToDict
    from a2a.utils.errors import InvalidParamsError, TaskNotFoundError

    class RoomRequestHandler(DefaultRequestHandler):
        def require_room(self, room):
            if not room:
                raise InvalidParamsError(message='Explicit contextId required; list or create a room first')
            try:
                discussion.store.room(room)
            except KeyError:
                raise InvalidParamsError(message='Unknown room; create it explicitly before sending')
            return room

        async def check_task_room(self, task_id, room, context):
            task = await self.task_store.get(task_id, context)
            if not task or task.context_id != room:
                raise TaskNotFoundError(message='Task not found in the specified room')

        async def _setup_active_task(self, params, context):
            room = self.require_room(params.message.context_id)
            metadata = MessageToDict(params.message.metadata)
            try:
                MessageInput(text='validate options', members=list(metadata.get('members', MEMBERS)), rounds=metadata.get('rounds', 1))
            except (ValueError, TypeError):
                raise InvalidParamsError(message='Invalid members or rounds')
            header = context.state.get('headers', {}).get('x-a2a-room')
            if header and header != room:
                raise InvalidParamsError(message='contextId and X-A2A-Room disagree')
            if params.message.task_id:
                await self.check_task_room(params.message.task_id, room, context)
            for task_id in params.message.reference_task_ids:
                await self.check_task_room(task_id, room, context)
            return await super()._setup_active_task(params, context)

        async def scoped_task(self, params, context):
            room = self.require_room(context.state.get('headers', {}).get('x-a2a-room'))
            await self.check_task_room(params.id, room, context)

        async def on_get_task(self, params, context):
            await self.scoped_task(params, context)
            return await super().on_get_task(params, context)

        async def on_cancel_task(self, params, context):
            await self.scoped_task(params, context)
            return await super().on_cancel_task(params, context)

        async def on_subscribe_to_task(self, params, context):
            await self.scoped_task(params, context)
            async for event in super().on_subscribe_to_task(params, context):
                yield event

        async def on_list_tasks(self, params, context):
            self.require_room(params.context_id)
            header = context.state.get('headers', {}).get('x-a2a-room')
            if header and header != params.context_id:
                raise InvalidParamsError(message='contextId and X-A2A-Room disagree')
            return await super().on_list_tasks(params, context)

    class Executor(AgentExecutor):
        async def cancel(self, context, event_queue):
            await discussion.cancel(context.task_id, context.context_id)
            await TaskUpdater(event_queue=event_queue, task_id=context.task_id, context_id=context.context_id).cancel()

        async def execute(self, context, event_queue):
            if not context.message or not context.task_id or not context.context_id:
                return
            room = context.context_id
            discussion.store.room(room)
            metadata = MessageToDict(context.message.metadata)
            discussion.submit(room, context.get_user_input(), metadata.get('members'), int(metadata.get('rounds', 1)), context.task_id)
            await event_queue.enqueue_event(Task(id=context.task_id, context_id=room,
                status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED), history=[context.message]))
            updater = TaskUpdater(event_queue=event_queue, task_id=context.task_id, context_id=room)
            await updater.start_work()
            result = await discussion.wait(context.task_id)
            for event in result['events']:
                if event['speaker'] != 'user':
                    await updater.add_artifact(parts=[Part(text=event['text'])], name=event['speaker'], last_chunk=True)
            if result['state'] == 'completed':
                await updater.complete()
            elif result['state'] == 'cancelled':
                await updater.cancel()
            else:
                await updater.failed(message=updater.new_agent_message(parts=[Part(text=result['error'] or result['state'])]))

    card = AgentCard(name='常驻 A2A 圆桌', description='必须显式指定已有房间 contextId；先通过 /api/rooms 创建房间。任务读取/取消/订阅要求 X-A2A-Room 请求头。每房间独立原生会话。',
        version=VERSION, capabilities=AgentCapabilities(streaming=False, push_notifications=False),
        default_input_modes=['text'], default_output_modes=['text'],
        skills=[AgentSkill(id='roundtable', name='持续圆桌讨论', description='共享新增发言，各成员保留原生会话。metadata.members 指定成员，metadata.rounds 指定轮数。', tags=['roundtable','persistent','discussion'])],
        supported_interfaces=[AgentInterface(protocol_binding='JSONRPC', protocol_version='1.0', url=BASE_URL + '/a2a/jsonrpc'),
                              AgentInterface(protocol_binding='HTTP+JSON', protocol_version='1.0', url=BASE_URL + '/a2a/rest')])
    engine = create_async_engine(f'sqlite+aiosqlite:///{data / "a2a_tasks.sqlite3"}')
    handler = RoomRequestHandler(agent_executor=Executor(), task_store=DatabaseTaskStore(engine), agent_card=card)
    app.state.a2a_handler = handler
    app.state.a2a_engine = engine
    add_a2a_routes_to_fastapi(app, agent_card_routes=create_agent_card_routes(agent_card=card),
        jsonrpc_routes=create_jsonrpc_routes(request_handler=handler, rpc_url='/a2a/jsonrpc'),
        rest_routes=create_rest_routes(request_handler=handler, path_prefix='/a2a/rest'))


if __name__ == '__main__':
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO)
    DATA.mkdir(parents=True, exist_ok=True)
    instance_lock = open(DATA / 'service.lock', 'a+')
    try:
        fcntl.flock(instance_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit('A roundtable service already owns this data directory')
    uvicorn.run(create_app(), host='127.0.0.1', port=PORT, access_log=False)
