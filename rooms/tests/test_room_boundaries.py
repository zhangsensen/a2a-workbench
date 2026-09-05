import asyncio
import sqlite3

import pytest
from fastapi.testclient import TestClient
from google.protobuf.json_format import MessageToDict
from a2a.types import Message, Part, Role, SendMessageRequest

import room_store
import roundtable_mcp
from room_store import MEMBERS, RoomStore
from roundtable import Discussion, create_app
from test_roundtable import FakeMember


def test_native_session_cursor_and_reply_ownership(tmp_path):
    store = RoomStore(tmp_path / 'rooms.db')
    for room in ('a', 'b'): store.create_room(room, room)
    store.update_member('a', 'codex', native_id='native-a')
    with pytest.raises(ValueError): store.update_member('b', 'codex', native_id='native-a')
    with pytest.raises(ValueError): store.create_room('a', 'Different topic')
    with pytest.raises(ValueError): store.create_room('../a', 'Invalid')
    store.submit('a', 'Only in A', ['codex'], 1, 'job-a')
    store.begin('job-a')
    for room, name in [('b', 'codex'), ('a', 'claude')]:
        with pytest.raises(ValueError): store.complete_turn(room, name, 'wrong', 'job-a')
    with pytest.raises(ValueError): store.update_member('b', 'codex', cursor=store.events('a')[0]['seq'])
    assert store.events('b') == []
    assert store.member('b', 'codex')['cursor'] == 0
    store.complete_turn('a', 'codex', 'correct', 'job-a')
    store.finish('job-a', 'completed')
    with pytest.raises(ValueError): store.complete_turn('a', 'codex', 'late', 'job-a')
    with pytest.raises(KeyError): store.job('job-a', 'b')


def test_connections_closed_without_garbage_collection(tmp_path, monkeypatch):
    actual_connect = sqlite3.connect
    connections = []
    def tracked(*args, **kwargs):
        db = actual_connect(*args, **kwargs)
        connections.append(db)  # Keep references; GC cannot hide a missing close.
        return db
    monkeypatch.setattr(room_store.sqlite3, 'connect', tracked)
    store = RoomStore(tmp_path / 'rooms.db')
    store.create_room('a', 'A')
    for _ in range(100): store.room('a'); store.rooms()
    for db in connections:
        with pytest.raises(sqlite3.ProgrammingError, match='closed'): db.execute('SELECT 1')


def test_mcp_requires_room_before_any_network_request(monkeypatch):
    calls = []
    monkeypatch.setattr(roundtable_mcp, 'http', lambda *args: calls.append(args))
    for name in ['roundtable_post', 'roundtable_history', 'roundtable_job', 'roundtable_cancel']:
        tool = next(t for t in roundtable_mcp.TOOLS if t['name'] == name)
        assert 'room' in tool['inputSchema']['required']
        for args in ({}, {'id':'job-a', 'text':'hello'}, {'room':''}):
            with pytest.raises(ValueError, match='room'): roundtable_mcp.call(name, args)
    assert calls == []
    roundtable_mcp.call('roundtable_cancel', {'room':'b', 'id':'job-a'})
    assert calls == [('/api/jobs/job-a/cancel?room=b', {})]


def test_interleaved_rooms_all_members_and_restart(tmp_path):
    async def scenario():
        path = tmp_path / 'rooms.db'
        store = RoomStore(path)
        for room in ('a', 'b'): store.create_room(room, room)
        d = Discussion(store, FakeMember)
        await d.start()
        try:
            for number in range(3):
                for room in ('a', 'b'):
                    d.submit(room, room.upper() + '_PRIVATE_' + str(number), rounds=2, key=room+str(number))
            await asyncio.gather(*(d.wait(room+'2') for room in ('a', 'b')))
            ids = {(r, n):store.member(r, n)['native_id'] for r in ('a','b') for n in MEMBERS}
            assert len(set(ids.values())) == 6
            for room, other in [('a','b'),('b','a')]:
                assert all(e['room'] == room for e in store.events(room))
                for name in MEMBERS:
                    assert other.upper()+'_PRIVATE_' not in '\n'.join(d.member(room,name).prompts)
        finally: await d.close()
        d = Discussion(RoomStore(path), FakeMember)
        await d.start()
        try:
            for room in ('a','b'): d.submit(room, room.upper()+'_PRIVATE_FOLLOWUP', key=room+'next')
            await asyncio.gather(d.wait('anext'), d.wait('bnext'))
            for (room, name), native_id in ids.items():
                assert d.store.member(room,name)['native_id'] == native_id
                assert ('B' if room=='a' else 'A')+'_PRIVATE_' not in '\n'.join(d.member(room,name).prompts)
        finally: await d.close()
    asyncio.run(scenario())


def test_http_wrong_room_cannot_read_or_cancel_running_job(tmp_path):
    class Slow(FakeMember):
        async def ask(self, prompt, job): await asyncio.sleep(60)
    app = create_app(tmp_path, Slow)
    with TestClient(app) as client:
        client.post('/api/rooms', json={'id':'b','title':'B'})
        response = client.post('/api/rooms/lobby/messages', json={'text':'Only A','requestId':'a'})
        assert response.status_code == 200
        for suffix, method in [('',client.get),('/cancel',lambda url:client.post(url,json={}))]:
            assert method('/api/jobs/a'+suffix).status_code == 422
            assert method('/api/jobs/a'+suffix+'?room=b').status_code == 404
        assert client.get('/api/jobs/a?room=lobby').json()['state'] in {'running','queued'}
        assert client.get('/api/rooms/b/jobs').json() == []
        assert client.post('/api/jobs/a/cancel?room=lobby',json={}).json()['state'] == 'cancelled'


def test_a2a_explicit_room_and_task_scope(tmp_path):
    app = create_app(tmp_path, FakeMember)
    with TestClient(app) as client:
        def rpc(method, params, room=None):
            return client.post('/a2a/jsonrpc', json={'jsonrpc':'2.0','id':'test','method':method,'params':params}, headers={'A2A-Version':'1.0', **({'X-A2A-Room':room} if room else {})}).json()
        def send(room):
            request = SendMessageRequest(message=Message(message_id='m', context_id=room, role=Role.ROLE_USER, parts=[Part(text='Hello only this room')]))
            request.message.metadata.update({'rounds':2,'members':['codex','claude','zcode']})
            return rpc('SendMessage', MessageToDict(request))
        assert 'error' in send('')
        assert 'error' in send('unknown')
        assert [r['id'] for r in app.state.discussion.store.rooms()] == ['lobby']
        result = send('lobby')
        assert 'result' in result, result
        task = result['result']['task']
        assert task['contextId'] == 'lobby'
        assert len(task['artifacts']) == 6
        client.post('/api/rooms', json={'id':'b','title':'B'})
        assert 'error' in rpc('GetTask', {'id':task['id']})
        assert 'error' in rpc('GetTask', {'id':task['id']}, 'b')
        assert 'result' in rpc('GetTask', {'id':task['id']}, 'lobby')
        assert 'error' in rpc('ListTasks', {})
        assert 'result' in rpc('ListTasks', {'contextId':'b'})
        request = SendMessageRequest(message=Message(message_id='cross',context_id='b',task_id=task['id'],role=Role.ROLE_USER,parts=[Part(text='cross')]))
        assert 'error' in rpc('SendMessage', MessageToDict(request))
        assert app.state.discussion.store.events('b') == []


def test_provider_never_receives_wrong_room_job(tmp_path):
    from room_agents import Member
    async def scenario():
        store=RoomStore(tmp_path/'rooms.db')
        for room in ('a','b'): store.create_room(room,room)
        store.submit('a','A only',['codex'],1,'a-job');store.begin('a-job')
        member=Member(store,'b','codex')
        with pytest.raises(KeyError): await member.ask('Must not reach model','a-job')
        assert member.runtime is None
        assert store.member('b','codex')['attempts']==0
    asyncio.run(scenario())


def test_a2a_cancel_and_reference_cannot_cross_rooms(tmp_path):
    class Slow(FakeMember):
        async def ask(self,prompt,job): await asyncio.sleep(60)
    app=create_app(tmp_path,Slow)
    with TestClient(app) as client:
        def rpc(method,params,room=None):
            return client.post('/a2a/jsonrpc',json={'jsonrpc':'2.0','id':'test','method':method,'params':params},headers={'A2A-Version':'1.0',**({'X-A2A-Room':room} if room else {})}).json()
        client.post('/api/rooms',json={'id':'b','title':'B'})
        request=SendMessageRequest(message=Message(message_id='slow',context_id='lobby',role=Role.ROLE_USER,parts=[Part(text='A only')]))
        request.configuration.return_immediately=True
        result=rpc('SendMessage',MessageToDict(request))
        task=result['result']['task']
        assert 'error' in rpc('CancelTask',{'id':task['id']},'b')
        assert 'error' in rpc('CancelTask',{'id':task['id']})
        reference=SendMessageRequest(message=Message(message_id='ref',context_id='b',role=Role.ROLE_USER,parts=[Part(text='wrong reference')],reference_task_ids=[task['id']]))
        assert 'error' in rpc('SendMessage',MessageToDict(reference))
        assert app.state.discussion.store.events('b')==[]
        assert 'result' in rpc('CancelTask',{'id':task['id']},'lobby')
        assert app.state.discussion.store.job(task['id'])['state']=='cancelled'
