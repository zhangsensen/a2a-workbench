import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

import roundtable_mcp
from room_store import RoomStore
from roundtable import create_app
from test_roundtable import FakeMember


def save(store, room, revision=0, through=0, summary='master summary'):
    return store.checkpoint(room, revision, 'Compare options', summary, ['Missing evidence'], 'Ask a peer', through)


def test_master_selects_peers_and_recovers_only_unread_events(tmp_path, monkeypatch):
    app = create_app(tmp_path, FakeMember)
    store = app.state.discussion.store
    with TestClient(app) as client:
        def http(path, data=None):
            response = client.get(path) if data is None else client.post(path, json=data)
            assert response.status_code == 200, response.text
            return response.json()
        monkeypatch.setattr(roundtable_mcp, 'http', http)
        call = roundtable_mcp.call
        call('roundtable_create_room', {'id':'design','title':'Design'})
        call('roundtable_create_room', {'id':'other','title':'Other'})
        initial = call('roundtable_context', {'room':'design'})
        assert initial['revision'] == 0 and initial['events'] == []
        consult = {'room':'design','member':'claude','text':'Find the weakest assumption','requestId':'first'}
        call('roundtable_consult', consult)
        answer = call('roundtable_job', {'room':'design','id':'first','waitSeconds':5})
        assert answer['state'] == 'completed'
        assert [e['speaker'] for e in answer['events']] == ['master','claude']
        assert len(app.state.discussion.member('design','codex').prompts) == 0
        assert call('roundtable_consult', consult)['id'] == 'first'
        assert len(store.events('design')) == 2  # Exact retries don't invoke a peer twice.
        context = call('roundtable_context', {'room':'design'})
        call('roundtable_checkpoint', {'room':'design','expectedRevision':0,'goal':'Compare options',
             'summary':'Claude challenged an assumption','openQuestions':['Need a second view'],
             'nextAction':'Ask Codex to assess the challenge','throughSeq':context['nextAfter']})
        call('roundtable_consult', {'room':'design','member':'codex','text':'Assess Claude’s challenge','requestId':'second'})
        assert call('roundtable_job', {'room':'design','id':'second','waitSeconds':5})['state'] == 'completed'
        resumed = call('roundtable_context', {'room':'design'})
        assert resumed['checkpoint']['summary'] == 'Claude challenged an assumption'
        assert [e['speaker'] for e in resumed['events']] == ['master','codex']
        assert 'claude-answer' in app.state.discussion.member('design','codex').prompts[0]
        assert call('roundtable_context', {'room':'other'})['events'] == []
        assert call('roundtable_context', {'room':'other'})['checkpoint'] is None
        assert client.get('/api/jobs/first?room=other&waitSeconds=25').status_code == 404
    reopened = RoomStore(store.path)
    assert reopened.master_context('design') == resumed


def test_checkpoint_retry_revision_conflict_and_native_state_preservation(tmp_path):
    store = RoomStore(tmp_path / 'rooms.db')
    for room in ('a','b'): store.create_room(room, room)
    store.update_member('a','codex',native_id='original-native')
    before = store.member('a','codex')
    saved = save(store, 'a')
    assert save(store, 'a') == saved  # Lost response is safely retried.
    with pytest.raises(ValueError, match='changed'): save(store, 'a', summary='stale competing master')
    updated = save(store, 'a', revision=1, summary='new summary')
    assert updated['revision'] == 2
    with pytest.raises(ValueError, match='changed'): save(store, 'a')
    assert store.master_context('b')['checkpoint'] is None
    assert store.member('a','codex') == before
    assert store.events('a') == []  # Saving a checkpoint never schedules or impersonates a peer.


def test_context_pagination_and_room_cursor_boundaries(tmp_path):
    store = RoomStore(tmp_path / 'rooms.db')
    for room in ('a','b'): store.create_room(room, room)
    for i, room in enumerate(('a','b','a','a')):
        store.submit(room, f'fact-{room}-{i}', ['codex'], 1, str(i), speaker='master')
        store.begin(str(i))
        store.complete_turn(room, 'codex', f'answer-{room}-{i}', str(i))
        store.finish(str(i), 'completed')
    page1 = store.master_context('a', limit=2)
    page2 = store.master_context('a', after=page1['nextAfter'], limit=2)
    page3 = store.master_context('a', after=page2['nextAfter'], limit=2)
    assert page1['hasMore'] and page2['hasMore'] and not page3['hasMore']
    combined = page1['events'] + page2['events'] + page3['events']
    assert combined == store.events('a')
    b_cursor = store.events('b')[-1]['seq']
    with pytest.raises(ValueError, match='cursor'): store.master_context('a', after=b_cursor)
    with pytest.raises(ValueError, match='cursor'): save(store, 'a', through=b_cursor)
    save(store, 'a', through=page2['nextAfter'])
    assert store.master_context('a')['events'] == page3['events']
    with pytest.raises(ValueError, match='backwards'): save(store, 'a', revision=1, through=page1['nextAfter'])
    assert len(store.master_context('a', after=0)['events']) == 6


def test_legacy_jobs_remain_user_attributed_and_interrupted_not_replayed(tmp_path):
    path = tmp_path / 'legacy.db'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE jobs (id TEXT PRIMARY KEY,room TEXT,prompt TEXT,members TEXT,rounds INTEGER,state TEXT,error TEXT,created REAL,updated REAL)')
        db.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?)', ('legacy','a','hello','["codex"]',1,'queued',None,1,1))
    store = RoomStore(path)
    store.create_room('a','A')
    assert store.begin('legacy')
    assert store.events('a')[0]['speaker'] == 'user'
    save(store, 'a', through=store.events('a')[0]['seq'])
    reopened = RoomStore(path)
    assert reopened.recover() == []
    assert reopened.job('legacy')['state'] == 'interrupted'
    assert reopened.master_context('a')['checkpoint']['revision'] == 1
    with pytest.raises(ValueError, match='different content'):
        reopened.submit('a','hello',['codex'],1,'legacy',speaker='master')


def test_new_mcp_tools_require_room_and_fields_before_network(monkeypatch):
    calls = []
    monkeypatch.setattr(roundtable_mcp, 'http', lambda *args: calls.append(args))
    for name in ('roundtable_context','roundtable_consult','roundtable_checkpoint'):
        for room in (None, '', ' '):
            with pytest.raises(ValueError): roundtable_mcp.call(name, {'room':room})
    with pytest.raises(ValueError): roundtable_mcp.call('roundtable_consult', {'room':'a','member':'claude','text':'Question'})
    with pytest.raises(ValueError): roundtable_mcp.call('roundtable_job', {'room':'a','id':'job','waitSeconds':26})
    assert not calls


def test_wait_timeout_is_a_receipt_and_cancel_keeps_master_event(tmp_path):
    import asyncio
    class Slow(FakeMember):
        async def ask(self, prompt, job): await asyncio.sleep(60)
    with TestClient(create_app(tmp_path, Slow)) as client:
        assert client.post('/api/rooms/lobby/consult', json={'member':'claude','text':'Question','requestId':'slow'}).status_code == 200
        result = client.get('/api/jobs/slow?room=lobby&waitSeconds=1').json()
        assert result['state'] == 'running'
        assert [e['speaker'] for e in result['events']] == ['master']
        assert client.post('/api/jobs/slow/cancel?room=lobby').json()['state'] == 'cancelled'
        assert client.get('/api/jobs/slow?room=lobby&waitSeconds=25').json()['state'] == 'cancelled'
