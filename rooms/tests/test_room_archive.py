import asyncio
import json
import re
import time

from fastapi.testclient import TestClient

import room_store
import roundtable
from room_store import RoomStore
from roundtable import Discussion, bounded_events_json, create_app
from test_roundtable import FakeMember


# --- 子项1：归档/自动复活生命周期 -----------------------------------------

def test_archive_frees_slot_closes_members_and_submit_auto_revives(tmp_path):
    app = create_app(tmp_path, FakeMember)
    discussion = app.state.discussion
    with TestClient(app) as client:
        for i in range(7):
            assert client.post('/api/rooms', json={'id': f'r{i}', 'title': f'R{i}'}).status_code == 200
        # lobby + r0..r6 = 8 active rooms; the cap is already reached.
        active = {r['id'] for r in discussion.store.rooms() if r['archived_at'] is None}
        assert len(active) == 8
        assert client.post('/api/rooms', json={'id': 'r7', 'title': 'R7'}).status_code == 409
        assert any(key[0] == 'r0' for key in discussion.members)  # warmed when created

        archived = client.post('/api/rooms/r0/archive').json()
        assert archived['archived_at'] is not None
        assert not any(key[0] == 'r0' for key in discussion.members)  # closed and released

        # Archived rooms no longer count toward the eight-room cap: a 9th room now fits.
        ninth = client.post('/api/rooms', json={'id': 'r7', 'title': 'R7'})
        assert ninth.status_code == 200

        # New activity in the archived room auto-revives it.
        assert client.post('/api/rooms/r0/messages', json={
            'text': 'back to life', 'members': ['codex'], 'requestId': 'revive-1',
        }).status_code == 200
        job = client.get('/api/jobs/revive-1?room=r0&waitSeconds=5').json()
        assert job['state'] == 'completed'
        assert discussion.store.room('r0')['archived_at'] is None

        # Archiving again after revival still works (repeatable lifecycle).
        re_archived = client.post('/api/rooms/r0/archive').json()
        assert re_archived['archived_at'] is not None
        assert not any(key[0] == 'r0' for key in discussion.members)

        # Explicit DELETE also unarchives and re-warms.
        unarchived = client.delete('/api/rooms/r0/archive').json()
        assert unarchived['archived_at'] is None
        assert any(key[0] == 'r0' for key in discussion.members)


def test_submit_execute_also_auto_revives_archived_room(tmp_path, monkeypatch):
    monkeypatch.setattr(room_store, 'EXECUTORS', {'codex': 'http://127.0.0.1:10002'})
    monkeypatch.setattr(roundtable, 'EXECUTORS', {'codex': 'http://127.0.0.1:10002'})

    async def fake_call(url, prompt, room, cwd, on_task_id):
        from a2a.types import TaskState
        return TaskState.TASK_STATE_COMPLETED, 'done', .1

    monkeypatch.setattr(roundtable, 'call_executor', fake_call)
    app = create_app(tmp_path, FakeMember)
    store = app.state.discussion.store
    with TestClient(app) as client:
        store.archive_room('lobby')
        assert store.room('lobby')['archived_at'] is not None
        response = client.post('/api/rooms/lobby/execute', json={
            'executor': 'codex', 'text': 'Build it', 'requestId': 'exec-revive',
        })
        assert response.status_code == 200
        assert store.room('lobby')['archived_at'] is None


def test_create_room_revives_archived_room_with_same_id_and_title(tmp_path):
    store = RoomStore(tmp_path / 'rooms.sqlite3')
    store.create_room('proj', 'Project')
    store.archive_room('proj')
    assert store.room('proj')['archived_at'] is not None
    store.create_room('proj', 'Project')  # Same id+title recreation revives it.
    assert store.room('proj')['archived_at'] is None


def test_archive_and_unarchive_unknown_room_is_404(tmp_path):
    with TestClient(create_app(tmp_path, FakeMember)) as client:
        assert client.post('/api/rooms/missing/archive').status_code == 404
        assert client.delete('/api/rooms/missing/archive').status_code == 404


# --- 子项2：未读事件 prompt 字节上限 ---------------------------------------

def test_bounded_events_json_drops_oldest_events_first(tmp_path):
    events = [{'seq': i, 'speaker': 'user', 'text': 'x' * 3000} for i in range(1, 26)]
    payload, omitted = bounded_events_json(events, limit=60000)
    assert len(payload.encode('utf-8')) <= 60000
    assert omitted > 0
    kept = json.loads(payload)
    assert kept[0]['seq'] == omitted + 1  # exactly the oldest `omitted` events were dropped
    assert kept[-1]['seq'] == 25  # newest event always survives


def test_bounded_events_json_is_noop_within_budget():
    events = [{'seq': 1, 'speaker': 'user', 'text': 'short'}]
    payload, omitted = bounded_events_json(events, limit=60000)
    assert omitted == 0
    assert json.loads(payload) == events


def test_run_prompt_notes_omitted_events_when_over_byte_limit(tmp_path):
    async def scenario():
        store = RoomStore(tmp_path / 'rooms.sqlite3')
        d = Discussion(store, FakeMember)
        await d.start()
        try:
            with store.connect() as db:
                for i in range(40):
                    db.execute(
                        'INSERT INTO events(room,speaker,text,job,created) VALUES (?,?,?,?,?)',
                        ('lobby', 'user', f'segment-{i:03d}-' + 'x' * 3000, None, time.time()),
                    )
            job = d.submit('lobby', 'Summarize the above', members=['codex'], key='trunc')
            assert (await d.wait(job['id']))['state'] == 'completed'
            prompt = d.member('lobby', 'codex').prompts[0]
            match = re.search(r'已省略 (\d+) 条更早消息', prompt)
            assert match, prompt[:200]
            assert int(match.group(1)) > 0
            payload = prompt[prompt.index('['):]
            assert len(payload.encode('utf-8')) <= roundtable.EVENTS_JSON_BYTE_LIMIT
            assert 'segment-039-' in prompt  # newest kept
            assert 'segment-000-' not in prompt  # oldest dropped
        finally:
            await d.close()
    asyncio.run(scenario())


def test_run_prompt_has_no_omission_note_within_budget(tmp_path):
    async def scenario():
        d = Discussion(RoomStore(tmp_path / 'rooms.sqlite3'), FakeMember)
        await d.start()
        try:
            job = d.submit('lobby', 'Short question', members=['codex'], key='short')
            assert (await d.wait(job['id']))['state'] == 'completed'
            prompt = d.member('lobby', 'codex').prompts[0]
            assert '已省略' not in prompt
        finally:
            await d.close()
    asyncio.run(scenario())
