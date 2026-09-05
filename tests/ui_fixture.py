"""Temporary browser acceptance server. Fake models, isolated database, fixed test port."""
import asyncio
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import uvicorn
from roundtable import create_app
from test_roundtable import FakeMember

class SlowMember(FakeMember):
    async def ask(self, prompt, job):
        await asyncio.sleep(8)
        await super().ask(prompt, job)

app = create_app(Path(tempfile.mkdtemp(prefix='a2a-ui-')), SlowMember,
                 allowed_origins={'http://127.0.0.1:41242'})
store = app.state.discussion.store
for room in ('a','b'): store.create_room(room, '隔离测试 ' + room.upper())

@app.middleware('http')
async def delayed_a(request, call_next):
    response = await call_next(request)
    if request.url.path.startswith('/api/rooms/a'):
        await asyncio.sleep(3)
    return response

if __name__ == '__main__': uvicorn.run(app,host='127.0.0.1',port=41242,access_log=False)
