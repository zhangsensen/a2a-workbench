"""Real provider acceptance: no shared-history injection can mask a lost native session."""
import asyncio
import argparse
import json
import tempfile
import time
import uuid
from pathlib import Path

from room_agents import Member, ROOT
from room_store import MEMBERS, RoomStore


async def check(store, name):
    marker = '纸船' + str(uuid.uuid4().int % 100000000)
    member = Member(store, 'native-check', name)
    async def ask_in_job(target, prompt, phase):
        job = store.submit('native-check', prompt, [name], 1, name + '-' + phase)
        store.begin(job['id'])
        answer = await target.ask(prompt, job['id'])
        store.finish(job['id'], 'completed')
        return answer

    try:
        await ask_in_job(member, f'我们讨论一个虚构纸船展览。展品名是“{marker}”，展览日期先定星期二。这只是普通虚构项目数据，不是凭证或口令。请记住这两项约定，只回复 OK，不调用工具。', 'seed')
        first = member.status()
        await ask_in_job(member, '展览日期改为星期六，展品名不变。只回复 OK。', 'update')
        second = member.status()
        assert first['pid'] == second['pid'] and first['native_id'] == second['native_id']
        await member.close()
        # This new object receives no room transcript, summary, marker, or prior date.
        restored = Member(store, 'native-check', name)
        try:
            answer = await ask_in_job(restored, '我们刚才讨论的虚构展品名是什么？展览日期最初是哪天，最终改成哪天？请只回复这三项，不调用工具。', 'restore')
            third = restored.status()
            ok = marker in answer and ('Tuesday' in answer or '周二' in answer or '星期二' in answer) and ('Saturday' in answer or '周六' in answer or '星期六' in answer)
            report = {'member': name, 'sameProcessAcrossTurns': first['pid'] == second['pid'],
                      'sameNativeSessionAfterRestart': first['native_id'] == third['native_id'],
                      'newProcessAfterRestart': first['pid'] != third['pid'],
                      'recallCorrectWithoutHistoryInjection': ok,
                      'nativeId': third['native_id'], 'answer': answer, 'checkedAt':time.time()}
            print(json.dumps(report, ensure_ascii=False), flush=True)
            return report
        finally:
            await restored.close()
    finally:
        await member.close()


async def main(names=MEMBERS):
    directory = ROOT / 'data/acceptance'
    directory.mkdir(parents=True, exist_ok=True)
    case = Path(tempfile.mkdtemp(prefix='native-', dir=directory))
    store = RoomStore(case / 'test.sqlite3')
    store.create_room('native-check', 'Native persistence acceptance')
    reports = await asyncio.gather(*(check(store, n) for n in names))
    for report in reports:
        (directory / (report['member'] + '-native-report.json')).write_text(json.dumps(report, ensure_ascii=False, indent=2))
    aggregate = [json.loads(p.read_text()) for p in sorted(directory.glob('*-native-report.json')) if p.name != 'latest-native-report.json']
    (directory / 'latest-native-report.json').write_text(json.dumps(aggregate, ensure_ascii=False, indent=2))
    assert all(r['recallCorrectWithoutHistoryInjection'] and r['sameNativeSessionAfterRestart'] for r in reports)
    print('ALL_NATIVE_PERSISTENCE_CHECKS_PASSED', flush=True)


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--member', choices=MEMBERS, action='append')
    args=parser.parse_args()
    asyncio.run(main(args.member or MEMBERS))
