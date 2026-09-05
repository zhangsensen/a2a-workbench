'use strict';
const $ = s => document.querySelector(s);
const el = (tag, text, cls) => {
  const n = document.createElement(tag);
  if (text !== undefined) n.textContent = text;
  if (cls) n.className = cls;
  return n;
};
let room = new URL(location.href).searchParams.get('room') || sessionStorage.getItem('a2a-selected-room') || null;
let refreshing = null;
let generation = 0, rooms = [], lastMessages = '', lastMembers = '', lastRooms = '';
let memberRoster = [];
const states = new Map();
const displayName = name => name ? name.charAt(0).toUpperCase() + name.slice(1) : name;
function state(id) {
  if (!states.has(id)) {
    let draft = {};
    try { draft = JSON.parse(sessionStorage.getItem('a2a-draft:' + id) || '{}'); } catch (_) {}
    states.set(id, {text: draft.text || '', members: draft.members || [...memberRoster],
      rounds: draft.rounds || 1, jobs: [], sending: false, loaded: false, notice: '', revision: 0});
  }
  return states.get(id);
}
function saveDraft(id) {
  const s = state(id);
  sessionStorage.setItem('a2a-draft:' + id, JSON.stringify({text: s.text, members: s.members, rounds: s.rounds}));
}
function captureDraft() {
  if (!room) return;
  const s = state(room);
  const next = {text: $('#prompt').value,
    members: [...document.querySelectorAll('[name=member]:checked')].map(n => n.value), rounds: Number($('#rounds').value)};
  if (s.text !== next.text || s.rounds !== next.rounds || JSON.stringify(s.members) !== JSON.stringify(next.members)) s.revision++;
  Object.assign(s, next);
  saveDraft(room);
}
function controls() {
  const s = room ? state(room) : null;
  const active = s?.jobs.filter(j => ['queued', 'running'].includes(j.state)) || [];
  $('#send').disabled = !s?.loaded || s.sending || active.length > 0;
  $('#cancel').hidden = !active.length;
  $('#cancel').textContent = '停止本房间任务 ' + (active[0]?.id.slice(0, 8) || '');
  $('#notice').textContent = s?.notice || (active.length ? `本房间有 ${active.length} 个任务等待或正在发言。` : '');
  const title = rooms.find(r => r.id === room)?.title || '';
  $('#target').textContent = room ? `发送到：${title} · ${room}` : '请先选择讨论室';
  $('#prompt').disabled = !room;
}
function renderRooms() {
  const signature = JSON.stringify([room, rooms]);
  if (signature === lastRooms) return;
  lastRooms = signature;
  $('#rooms').replaceChildren(...rooms.map(r => {
    const b = el('button', undefined, 'room' + (r.id === room ? ' active' : ''));
    b.append(el('div', r.title), el('small', r.id));
    b.onclick = () => selectRoom(r.id);
    return b;
  }));
}
function selectRoom(id) {
  captureDraft();
  room = id;
  generation++;
  sessionStorage.setItem('a2a-selected-room', room);
  const url = new URL(location.href); url.searchParams.set('room', room); history.replaceState(null, '', url);
  const s = state(room);
  $('#prompt').value = s.text;
  document.querySelectorAll('[name=member]').forEach(n => n.checked = s.members.includes(n.value));
  $('#rounds').value = String(s.rounds);
  $('#messages').replaceChildren(); $('#members').replaceChildren();
  $('#title').textContent = `${rooms.find(r => r.id === room)?.title || room} · ${room}`;
  $('#empty').hidden = false; $('#empty').textContent = '正在读取本房间…';
  lastMessages = lastMembers = '';
  renderRooms(); controls(); refresh();
}
async function api(path, body) {
  const response = await fetch(path, body === undefined ? {} : {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
  const data = await response.json();
  if (!response.ok) throw Error(data.error || data.detail || '请求失败');
  return data;
}
const roomPath = id => '/api/rooms/' + encodeURIComponent(id);
const jobPath = (id, target, suffix = '') => '/api/jobs/' + encodeURIComponent(id) + suffix + '?room=' + encodeURIComponent(target);
async function refresh() {
  if (refreshing?.room === room && refreshing.ticket === generation) return;
  const target = room, ticket = ++generation;
  refreshing = {room: target, ticket};
  try {
    const [inventory, health, status, messages, jobs] = await Promise.all([
      api('/api/rooms'), api('/healthz'),
      target ? api(roomPath(target)).catch(requestError => ({requestError})) : null,
      target ? api(roomPath(target) + '/messages').catch(requestError => ({requestError})) : [],
      target ? api(roomPath(target) + '/jobs').catch(requestError => ({requestError})) : []
    ]);
    if (ticket !== generation || target !== room) return;
    rooms = inventory; renderRooms();
    memberRoster = (health.rooms?.find(r => r.id === target) || health.rooms?.[0])?.members.map(m => m.name) || memberRoster;
    $('#service').textContent = health.serviceAlive ? '服务常驻运行' : '服务异常';
    if (!target) { $('#title').textContent = '选择一个讨论室'; controls(); return; }
    for (const result of [status, messages, jobs]) if (result.requestError) throw result.requestError;
    const s = state(target);
    s.loaded = true; s.jobs = jobs;
    if (s.connectionError) { s.notice = ''; s.connectionError = false; }
    $('#title').textContent = status.title + ' · ' + target;
    const memberSignature = JSON.stringify(status.members);
    if (memberSignature !== lastMembers) {
      $('#members').replaceChildren(...status.members.map(m => {
        const n = el('div', undefined, 'member');
        n.append(el('span', undefined, 'dot' + (!m.processAlive || m.state === 'error' ? ' bad' : m.state === 'busy' ? ' busy' : '')),
          el('strong', displayName(m.name)),
          el('div', m.state === 'busy' ? '正在发言' : m.processAlive && m.state === 'ready' ? `待命 · 已完成 ${m.turns} 次发言` : '需要处理'));
        const details = el('details');
        details.append(el('summary', m.model || '会话信息'), el('div', '所属房间：' + target),
          el('div', m.native_id || '尚未建立会话'), el('div', '进程 ' + (m.pid || '未运行')));
        if (m.last_error) details.append(el('div', m.last_error));
        n.append(details); return n;
      }));
      lastMembers = memberSignature;
    }
    const signature = JSON.stringify(messages.map(m => m.seq));
    if (signature !== lastMessages) {
      $('#messages').replaceChildren(...messages.map(m => {
        const n = el('article', undefined, 'message ' + m.speaker);
        n.append(el('strong', m.speaker === 'user' ? '你' : m.speaker),
          el('time', new Date(m.created * 1000).toLocaleString()), el('div', m.text, 'body'));
        return n;
      }));
      lastMessages = signature;
    }
    $('#empty').hidden = !!messages.length;
    $('#empty').textContent = '本房间还没有发言。提出一个问题，让大家开始讨论。';
    if (jobs.length && !jobs.some(j => ['queued', 'running'].includes(j.state)) && !s.sending) {
      const latest = jobs[0];
      s.notice = latest.error || (latest.state === 'completed' ? '本房间的讨论已完成。可以继续追问。' : '本房间最近任务状态：' + latest.state);
    }
    controls();
  } catch (error) {
    if (ticket !== generation || target !== room) return;
    $('#service').textContent = '连接或房间异常';
    if (target) { const s = state(target); s.loaded = false; s.connectionError = true; s.notice = error.message + '；未切换到其他房间。'; }
    controls();
  } finally { if (refreshing?.ticket === ticket) refreshing = null; }
}
$('#prompt').addEventListener('input', captureDraft);
$('#rounds').addEventListener('change', captureDraft);
document.querySelectorAll('[name=member]').forEach(n => n.addEventListener('change', captureDraft));
$('#composer').onsubmit = async event => {
  event.preventDefault();
  const target = room;
  if (!target || $('#send').disabled) return;
  captureDraft();
  const s = state(target), revision = s.revision;
  if (!s.members.length) { s.notice = '请至少选择一位成员'; controls(); return; }
  // Preserve the exact request ID after an uncertain network result, separately in each room.
  const content = {text: s.text, members: s.members, rounds: s.rounds};
  if (!s.retry || JSON.stringify(s.retry.content) !== JSON.stringify(content)) s.retry = {content, id: crypto.randomUUID()};
  const body = {...s.retry.content, requestId: s.retry.id};
  s.sending = true; s.notice = '正在提交到 ' + target; controls();
  try {
    const job = await api(roomPath(target) + '/messages', body);
    if (job.room !== target) throw Error('响应房间不匹配，已停止更新页面');
    s.jobs.unshift(job); s.retry = null; s.notice = '';
    if (s.revision === revision) { s.text = ''; saveDraft(target); if (room === target) $('#prompt').value = ''; }
  } catch (error) { s.notice = error.message + '；草稿已保留，重试会沿用本次请求 ID。'; }
  finally { s.sending = false; if (room === target) { generation++; controls(); refresh(); } }
};
$('#cancel').onclick = async () => {
  const target = room;
  const job = state(target).jobs.find(j => ['queued', 'running'].includes(j.state));
  if (!job) return;
  try { await api(jobPath(job.id, target, '/cancel'), {}); }
  catch (error) { state(target).notice = error.message; }
  if (room === target) { generation++; controls(); refresh(); }
};
$('#newroom').onsubmit = async event => {
  event.preventDefault();
  const id = 'room-' + crypto.randomUUID().slice(0, 12), title = $('#roomTitle').value;
  const button = $('#newroom button'); button.disabled = true;
  try { await api('/api/rooms', {id, title}); rooms.push({id, title}); selectRoom(id); $('#roomTitle').value = ''; }
  catch (error) { $('#notice').textContent = error.message; }
  finally { button.disabled = false; }
};
if (room) { const initial = room; room = null; selectRoom(initial); } else { generation++; controls(); refresh(); }
setInterval(refresh, 2000);
