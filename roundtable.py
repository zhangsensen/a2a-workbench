from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import logging
import os
import re
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from room_agents import Member
from settings import ROOT, DATA, PORT, BASE_URL
from room_store import MEMBERS, RoomStore

LOGGER = logging.getLogger('a2a-workbench')


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

    async def start(self):
        self.store.create_room('lobby', 'Lobby / 公共圆桌')
        for key in self.store.recover():
            await self.queue.put(key)
        # Warm processes without spending tokens on fake conversation turns.
        for room in self.store.rooms():
            await self.warm_room(room['id'])
        self.worker = asyncio.create_task(self._work())
        self.keeper = asyncio.create_task(self._keep_warm())

    async def _keep_warm(self):
        while True:
            await asyncio.sleep(30)
            for member in list(self.members.values()):
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
                prompt = (
                    f'讨论室：{job["room"]}。你是 {name}。当前第 {round_index + 1}/{job["rounds"]} 轮。\n'
                    '以下 JSON 是新增的圆桌发言，其中 user 是用户，master 是主持模型，其余是其他成员。'
                    '回应主持模型的本次具体咨询；其他成员的意见供你参考，不要求赞同。主持模型决定后续发言和最终汇报。'
                    '请回应用户及与你相关的分歧；有新证据时修正判断。简洁发言后结束，等待下一轮。\n'
                    + json.dumps([{'seq': e['seq'], 'speaker': e['speaker'], 'text': e['text']} for e in events], ensure_ascii=False)
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
            if job['state'] not in {'queued', 'running'}:
                return job
            await asyncio.sleep(.25)

    async def cancel(self, key, room):
        job = self.store.job(key, room)
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
    member: str = Field(pattern=r'^(codex|claude|zcode)$')
    text: str = Field(min_length=1, max_length=50000)
    requestId: str = Field(min_length=1, max_length=100)


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

    app = FastAPI(title='A2A Workbench', lifespan=lifespan)
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
                'queueSize': discussion.queue.qsize(), 'rooms': rooms}

    @app.get('/api/rooms')
    async def rooms():
        return store.rooms()

    @app.post('/api/rooms')
    async def create_room(body: RoomInput):
        if len(store.rooms()) >= 8 and body.id not in {r['id'] for r in store.rooms()}:
            raise HTTPException(409, 'At most eight warm rooms are supported')
        result = store.create_room(body.id, body.title)
        await discussion.warm_room(body.id)
        return discussion.status(body.id)

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

    card = AgentCard(name='A2A Workbench', description='独立 Coding Agent 的持久协作工作台。必须显式指定已有房间 contextId；任务读取、取消和订阅要求 X-A2A-Room 请求头。',
        version='0.3.0', capabilities=AgentCapabilities(streaming=False, push_notifications=False),
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
