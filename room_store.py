from __future__ import annotations

import json
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

MEMBERS = ("codex", "claude", "zcode")


class RoomStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS rooms (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS members (
                    room TEXT, name TEXT, native_id TEXT, cursor INTEGER NOT NULL DEFAULT 0,
                    turns INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT 'new',
                    last_error TEXT, updated REAL, PRIMARY KEY(room,name));
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, room TEXT NOT NULL,
                    speaker TEXT NOT NULL, text TEXT NOT NULL, job TEXT, created REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS room_events ON events(room,seq);
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, room TEXT NOT NULL, prompt TEXT NOT NULL,
                    members TEXT NOT NULL, rounds INTEGER NOT NULL, state TEXT NOT NULL,
                    error TEXT, created REAL NOT NULL, updated REAL NOT NULL);
            """)
            if 'attempts' not in {r['name'] for r in db.execute('PRAGMA table_info(members)')}:
                db.execute('ALTER TABLE members ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0')
            db.execute('CREATE UNIQUE INDEX IF NOT EXISTS unique_native_session ON members(native_id) WHERE native_id IS NOT NULL')
        path.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def create_room(self, room, title):
        if not isinstance(room, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,80}', room):
            raise ValueError('Explicit valid room ID required')
        if not isinstance(title, str) or not 1 <= len(title.strip()) <= 200:
            raise ValueError('Room title required (1-200 characters)')
        with self.connect() as db:
            old = db.execute('SELECT title FROM rooms WHERE id=?', (room,)).fetchone()
            if old and old['title'] != title:
                raise ValueError('Room ID already belongs to a different title; choose a new ID')
            db.execute("INSERT OR IGNORE INTO rooms VALUES (?,?,?)", (room, title, time.time()))
            for name in MEMBERS:
                db.execute("INSERT OR IGNORE INTO members(room,name) VALUES (?,?)", (room, name))
        return self.room(room)

    def room(self, room):
        with self.connect() as db:
            row = db.execute("SELECT * FROM rooms WHERE id=?", (room,)).fetchone()
            if not row:
                raise KeyError(room)
            result = dict(row)
            result['members'] = [dict(r) for r in db.execute("SELECT * FROM members WHERE room=? ORDER BY name", (room,))]
            return result

    def rooms(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM rooms ORDER BY created")]

    def member(self, room, name):
        with self.connect() as db:
            return dict(db.execute("SELECT * FROM members WHERE room=? AND name=?", (room, name)).fetchone())

    def update_member(self, room, name, **values):
        allowed = {'native_id', 'cursor', 'turns', 'state', 'last_error', 'attempts'}
        if not values.keys() <= allowed:
            raise ValueError('Invalid member fields')
        values['updated'] = time.time()
        with self.connect() as db:
            if values.get('cursor') and not db.execute('SELECT 1 FROM events WHERE room=? AND seq=?', (room, values['cursor'])).fetchone():
                raise ValueError('Cursor does not belong to this room')
            try:
                result = db.execute("UPDATE members SET " + ','.join(f'{k}=?' for k in values) + " WHERE room=? AND name=?", (*values.values(), room, name))
            except sqlite3.IntegrityError as exc:
                raise ValueError('Native session already belongs to another room/member') from exc
            if not result.rowcount:
                raise KeyError((room, name))

    def events(self, room, after=0):
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM events WHERE room=? AND seq>? ORDER BY seq", (room, after))]

    def submit(self, room, prompt, members, rounds, key=None):
        self.room(room)
        if not prompt.strip() or not members or len(set(members)) != len(members) or not set(members) <= set(MEMBERS):
            raise ValueError('Supply a message and unique valid participants')
        if not 1 <= rounds <= 5:
            raise ValueError('Rounds must be between 1 and 5')
        key = key or str(uuid.uuid4())
        with self.connect() as db:
            previous = db.execute("SELECT * FROM jobs WHERE id=?", (key,)).fetchone()
            if previous:
                if (previous['room'], previous['prompt'], json.loads(previous['members']), previous['rounds']) != (room, prompt, members, rounds):
                    raise ValueError('Request ID already used for different content')
                return self.job(key)
            now = time.time()
            db.execute("INSERT INTO jobs VALUES (?,?,?,?,?,'queued',NULL,?,?)", (key, room, prompt, json.dumps(members), rounds, now, now))
        return self.job(key)

    def job(self, key, room=None):
        with self.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (key,)).fetchone()
            if not row or (room is not None and row['room'] != room):
                raise KeyError(key)
            result = dict(row)
            result['members'] = json.loads(result['members'])
            result['events'] = [dict(r) for r in db.execute("SELECT * FROM events WHERE job=? AND room=? ORDER BY seq", (key, row['room']))]
            return result

    def room_jobs(self, room):
        self.room(room)
        with self.connect() as db:
            # Metadata only; transcripts are read through a room-scoped job request.
            return [dict(r) for r in db.execute('SELECT id,room,state,error,created FROM jobs WHERE room=? ORDER BY created DESC LIMIT 100', (room,))]

    def begin(self, key):
        with self.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=? AND state='queued'", (key,)).fetchone()
            if not row:
                return False
            db.execute("UPDATE jobs SET state='running',updated=? WHERE id=?", (time.time(), key))
            db.execute("INSERT INTO events(room,speaker,text,job,created) VALUES (?,'user',?,?,?)", (row['room'], row['prompt'], key, time.time()))
        return True

    def require_turn(self, room, name, key):
        job = self.job(key, room)
        if job['state'] != 'running' or name not in job['members']:
            raise ValueError('Member is not scheduled in this running room job')
        return job

    def complete_turn(self, room, name, text, job):
        with self.connect() as db:
            scheduled = db.execute('SELECT * FROM jobs WHERE id=? AND room=?', (job, room)).fetchone()
            if not scheduled or scheduled['state'] != 'running' or name not in json.loads(scheduled['members']):
                raise ValueError('Reply does not belong to a running job in this room for this member')
            cursor = db.execute("INSERT INTO events(room,speaker,text,job,created) VALUES (?,?,?,?,?)", (room, name, text, job, time.time())).lastrowid
            db.execute("UPDATE members SET cursor=?,turns=turns+1,state='ready',last_error=NULL,updated=? WHERE room=? AND name=?", (cursor, time.time(), room, name))

    def finish(self, key, state, error=None):
        with self.connect() as db:
            db.execute("UPDATE jobs SET state=?,error=?,updated=? WHERE id=?", (state, error, time.time(), key))

    def recover(self):
        # An interrupted provider turn may have been billed/committed. Never replay it silently.
        with self.connect() as db:
            db.execute("UPDATE jobs SET state='interrupted',error='Service restarted during discussion; inspect recorded replies before continuing',updated=? WHERE state='running'", (time.time(),))
            db.execute("UPDATE members SET state='interrupted' WHERE state='busy'")
            return [r['id'] for r in db.execute("SELECT id FROM jobs WHERE state='queued' ORDER BY created")]
