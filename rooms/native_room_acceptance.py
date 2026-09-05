"""Real six-session isolation test. Only synthetic facts, no user room transcripts."""
import asyncio
import json
import tempfile
import time
import uuid
from pathlib import Path

from room_agents import Member, ROOT
from room_store import RoomStore, MEMBERS


async def main():
    directory = ROOT / 'data/acceptance'
    directory.mkdir(parents=True, exist_ok=True)
    case = Path(tempfile.mkdtemp(prefix='rooms-', dir=directory))
    store = RoomStore(case / 'rooms.sqlite3')
    for room in ('isolation-a', 'isolation-b'): store.create_room(room, room)
    facts = {room: '纸船' + uuid.uuid4().hex[:10] for room in ('isolation-a','isolation-b')}
    members = {(room,name): Member(store,room,name) for room in facts for name in MEMBERS}
    snapshots = {}
    reports = []

    async def ask(key, phase, text):
        room, name = key
        job = store.submit(room, text, [name], 1, room+'-'+name+'-'+phase)
        store.begin(job['id'])
        answer = await members[key].ask(text,job['id'])
        store.finish(job['id'],'completed')
        return answer

    async def verify(key, phase):
        room, name = key
        answer = await ask(key,phase,'本房间刚才约定的虚构展品名称是什么？只回复展品名，不调用工具。')
        status = members[key].status()
        previous = snapshots[key]
        report = {'room':room,'member':name,'phase':phase,
                  'correctRoomRecall': facts[room] in answer,
                  'noOtherRoomFact': all(value not in answer for r,value in facts.items() if r!=room),
                  'sameNativeSession':status['native_id']==previous['native_id'],
                  'processBehaviorCorrect':(status['pid']==previous['pid']) == (phase=='warm'),
                  'nativeId':status['native_id']}
        reports.append(report)
        print(json.dumps(report),flush=True)
        assert all(report[k] for k in ('correctRoomRecall','noOtherRoomFact','sameNativeSession','processBehaviorCorrect'))

    try:
        # Both rooms are alive at once. Each provider receives two unrelated conversations.
        await asyncio.gather(*(ask(key,'seed',f'本房间讨论一个普通虚构展览，展品名称确定为“{facts[key[0]]}”。请在本房间记住这一名称，只回复 OK，不调用工具。') for key in members))
        snapshots = {key:m.status() for key,m in members.items()}
        assert len({s['native_id'] for s in snapshots.values()}) == 6
        await asyncio.gather(*(verify(key,'warm') for key in members))
        await asyncio.gather(*(m.close() for m in members.values()))
        # Reopen disk store and all runtimes. No transcript is injected into recall prompts.
        store = RoomStore(case / 'rooms.sqlite3')
        members = {key:Member(store,*key) for key in members}
        await asyncio.gather(*(verify(key,'restored') for key in members))
        output = {'checkedAt':time.time(),'uniqueNativeSessions':6,'reports':reports,'passed':True}
        (directory/'latest-room-isolation.json').write_text(json.dumps(output,ensure_ascii=False,indent=2))
        print('ALL_SIX_NATIVE_ROOM_ISOLATION_CHECKS_PASSED',flush=True)
    finally:
        await asyncio.gather(*(m.close() for m in members.values()),return_exceptions=True)


if __name__ == '__main__': asyncio.run(main())
