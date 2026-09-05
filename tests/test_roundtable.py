import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from room_store import RoomStore
from roundtable import Discussion, create_app


class FakeMember:
    def __init__(self, store, room, name):
        self.store, self.room, self.name = store, room, name
        self.prompts = []
        self.alive = False

    async def warm(self):
        self.alive = True
        row = self.store.member(self.room, self.name)
        self.store.update_member(self.room, self.name, native_id=row['native_id'] or f'{self.room}-{self.name}-native', state='ready')

    async def ask(self, prompt, job):
        await self.warm()
        self.prompts.append(prompt)
        self.store.complete_turn(self.room, self.name, self.name + '-answer', job)

    def status(self):
        return {**self.store.member(self.room, self.name), 'processAlive':self.alive,'pid':123 if self.alive else None,'model':'test'}

    async def close(self):
        self.alive = False


def test_delta_context_and_room_isolation(tmp_path):
    async def scenario():
        store = RoomStore(tmp_path / 'test.sqlite3')
        d = Discussion(store, FakeMember)
        await d.start()
        try:
            first = d.submit('lobby','Remember cobalt',rounds=2,key='first')
            assert (await d.wait(first['id']))['state'] == 'completed'
            c = d.member('lobby','codex')
            z = d.member('lobby','zcode')
            assert 'Remember cobalt' in c.prompts[0]
            assert 'Remember cobalt' not in c.prompts[1]
            assert 'claude-answer' in c.prompts[1]
            assert 'codex-answer' in z.prompts[0]
            first_id = store.member('lobby','codex')['native_id']
            d.submit('lobby','What word?',members=['codex'],key='next')
            await d.wait('next')
            assert store.member('lobby','codex')['native_id'] == first_id
            assert 'Remember cobalt' not in c.prompts[-1]
            store.create_room('separate','Separate topic')
            d.submit('separate','Independent',members=['codex'],key='other')
            await d.wait('other')
            assert 'cobalt' not in d.member('separate','codex').prompts[0]
            assert store.member('separate','codex')['native_id'] != first_id
        finally: await d.close()
    asyncio.run(scenario())


def test_idempotent_submission_and_restart_interruption(tmp_path):
    store=RoomStore(tmp_path/'test.sqlite3')
    store.create_room('r','Room')
    one=store.submit('r','question',['claude'],1,'request-1')
    assert store.submit('r','question',['claude'],1,'request-1')['id']==one['id']
    with pytest.raises(ValueError):store.submit('r','different',['claude'],1,'request-1')
    assert store.begin('request-1')
    assert not store.begin('request-1')
    store.update_member('r','claude',native_id='original',state='busy')
    reopened=RoomStore(tmp_path/'test.sqlite3')
    assert reopened.recover()==[]
    assert reopened.job('request-1')['state']=='interrupted'
    assert reopened.member('r','claude')['native_id']=='original'
    assert len(reopened.events('r'))==1


def test_one_failed_member_does_not_erase_other_replies(tmp_path):
    class Failing(FakeMember):
        async def ask(self,prompt,job):
            if self.name=='claude':raise RuntimeError('model unavailable')
            await super().ask(prompt,job)
    async def scenario():
        d=Discussion(RoomStore(tmp_path/'test.sqlite3'),Failing)
        await d.start()
        try:
            d.submit('lobby','Discuss',rounds=2,key='f')
            result=await d.wait('f')
            assert result['state']=='partial'
            assert [e['speaker'] for e in result['events']]==['user','codex','zcode','codex','zcode']
        finally:await d.close()
    asyncio.run(scenario())


def test_cancel_stops_active_turn_and_queue_continues(tmp_path):
    class Slow(FakeMember):
        async def ask(self,prompt,job):
            if job=='slow':await asyncio.sleep(60)
            await super().ask(prompt,job)
    async def scenario():
        d=Discussion(RoomStore(tmp_path/'test.sqlite3'),Slow)
        await d.start()
        try:
            d.submit('lobby','Slow',key='slow')
            while d.store.job('slow')['state']!='running':await asyncio.sleep(.01)
            await d.cancel('slow', 'lobby')
            d.submit('lobby','Next',members=['codex'],key='next')
            assert (await d.wait('next'))['state']=='completed'
            assert d.store.job('slow')['state']=='cancelled'
        finally:await d.close()
    asyncio.run(scenario())


def test_http_host_origin_and_a2a_card(tmp_path):
    with TestClient(create_app(tmp_path,FakeMember)) as client:
        assert client.get('/healthz').json()['ready']
        assert client.get('/.well-known/agent-card.json').status_code==200
        assert client.get('/healthz',headers={'Host':'evil.example'}).status_code==403
        assert client.post('/api/rooms/lobby/messages',headers={'Origin':'https://evil.example'},json={'text':'blocked'}).status_code==403
        assert client.post('/api/rooms/lobby/messages',json={'text':'ok','members':['claude','claude']}).status_code==400
        assert client.post('/api/rooms/lobby/messages',json={'text':'ok','requestId':'same'}).status_code==200
        assert client.get('/api/rooms/missing').status_code==404
