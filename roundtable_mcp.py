"""Small stdio MCP client for the always-on local room; never launches models."""
import json
import sys
import urllib.request
import urllib.parse
import uuid

from settings import BASE_URL as BASE
TOOLS = [
    {'name': 'roundtable_status', 'description': 'Read live process/session/queue status of the persistent local Codex/Claude/ZCode roundtable.', 'inputSchema': {'type': 'object', 'properties': {}}},
    {'name': 'roundtable_rooms', 'description': 'List persistent discussion rooms.', 'inputSchema': {'type': 'object', 'properties': {}}},
    {'name': 'roundtable_create_room', 'description': 'Create a separate persistent topic. Reuse an existing room for continuing discussion.', 'inputSchema': {'type': 'object', 'properties': {'id': {'type':'string'}, 'title':{'type':'string'}}, 'required':['id','title']}},
    {'name': 'roundtable_post', 'description': 'Post to an existing roundtable topic and schedule 1-5 rounds. Reuses each member native conversation. Returns a job id immediately; read roundtable_job for actual answers. Requires user authorization to initiate discussion.', 'inputSchema': {'type':'object','properties':{'room':{'type':'string','description':'Required explicit room ID; never infer a default from another task'},'text':{'type':'string'},'members':{'type':'array','items':{'enum':['codex','claude','zcode']}},'rounds':{'type':'integer','minimum':1,'maximum':5,'default':1},'requestId':{'type':'string','description':'Reuse this id if retrying an uncertain submission'}},'required':['room','text']}},
    {'name':'roundtable_job','description':'Read discussion job state and replies without consuming them.', 'inputSchema':{'type':'object','properties':{'room':{'type':'string'},'id':{'type':'string'}},'required':['room','id']}},
    {'name':'roundtable_history','description':'Read room messages after a sequence number. Does not clear or consume messages.', 'inputSchema':{'type':'object','properties':{'room':{'type':'string','description':'Required explicit room ID; never infer a default from another task'},'after':{'type':'integer','default':0}},'required':['room']}},
    {'name':'roundtable_cancel','description':'Stop an explicitly selected queued or running discussion, retaining native context and completed replies.', 'inputSchema':{'type':'object','properties':{'room':{'type':'string'},'id':{'type':'string'}},'required':['room','id']}},
]


def http(path, data=None):
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(BASE + path, data=body, headers={'Content-Type':'application/json'})
    with urllib.request.urlopen(req, timeout=120) as response:
        return json.load(response)


def call(name, args):
    if name in {'roundtable_post', 'roundtable_history', 'roundtable_job', 'roundtable_cancel'}:
        if not isinstance(args.get('room'), str) or not args['room'].strip():
            raise ValueError('Explicit room is required; use roundtable_rooms to choose the intended topic')
    quote = lambda s: urllib.parse.quote(str(s), safe='')
    if name == 'roundtable_status': return http('/healthz')
    if name == 'roundtable_rooms': return http('/api/rooms')
    if name == 'roundtable_create_room': return http('/api/rooms', args)
    if name == 'roundtable_post':
        args = dict(args)
        room = args.pop('room')
        args.setdefault('requestId', str(uuid.uuid4()))
        return http('/api/rooms/' + quote(room) + '/messages', args)
    if name == 'roundtable_job': return http('/api/jobs/' + quote(args['id']) + '?room=' + quote(args['room']))
    if name == 'roundtable_history': return http('/api/rooms/' + quote(args['room']) + '/messages?after=' + str(int(args.get('after',0))))
    if name == 'roundtable_cancel': return http('/api/jobs/' + quote(args['id']) + '/cancel?room=' + quote(args['room']), {})
    raise ValueError('Unknown tool')


def main():
    for line in sys.stdin:
        request = {}
        try:
            request = json.loads(line)
            if 'id' not in request: continue
            method = request.get('method')
            if method == 'initialize':
                result = {'protocolVersion': request.get('params',{}).get('protocolVersion','2024-11-05'), 'capabilities': {'tools': {}}, 'serverInfo': {'name':'a2a-roundtable','version':'0.3.0'}}
            elif method == 'tools/list': result = {'tools': TOOLS}
            elif method == 'ping': result = {}
            elif method == 'tools/call':
                params = request['params']
                result = {'content':[{'type':'text','text':json.dumps(call(params['name'], params.get('arguments',{})),ensure_ascii=False)}]}
            else:
                raise ValueError('Unknown method')
            response = {'jsonrpc':'2.0','id':request['id'],'result':result}
        except ValueError as exc:
            response = {'jsonrpc':'2.0','id':request.get('id'),'error':{'code':-32602,'message':str(exc)}}
        except Exception as exc:
            response = {'jsonrpc':'2.0','id':request.get('id'),'error':{'code':-32603,'message':f'Roundtable request failed ({type(exc).__name__}); verify local service status'}}
        print(json.dumps(response,ensure_ascii=False),flush=True)


if __name__ == '__main__': main()
