"""Persist request IDs so retried A2A sends reuse their original task."""
from __future__ import annotations

import re
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path


DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_REQUEST_ID = re.compile(r"^[A-Za-z0-9_-]{1,100}$")


class RequestDedup:
    """SQLite-backed requestId to task ID mapping for one agent."""

    def __init__(self, agent_name: str) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.db_path = DATA_DIR / f"{agent_name}-requests.db"
        self._lock = threading.Lock()
        with self._lock, closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS requests (
                    request_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    created REAL
                )
                """
            )

    @staticmethod
    def _valid(request_id: object) -> bool:
        return (
            isinstance(request_id, str)
            and _REQUEST_ID.fullmatch(request_id) is not None
        )

    def lookup(self, request_id: object) -> str | None:
        """Return the remembered task ID, or None for missing/invalid IDs."""
        if not self._valid(request_id):
            return None
        # closing() 显式关连接：sqlite3 的 with 只管事务不管关闭，靠引用计数
        # 回收在 Windows 上时机不定，会把 db 文件一直锁到 GC（实测导致测试的
        # 临时目录清理 PermissionError）。
        with self._lock, closing(sqlite3.connect(self.db_path)) as db, db:
            row = db.execute(
                "SELECT task_id FROM requests WHERE request_id = ?", (request_id,)
            ).fetchone()
        return row[0] if row else None

    def remember(self, request_id: object, task_id: str) -> None:
        """Persist a valid request ID, replacing a stale task mapping if needed."""
        if not self._valid(request_id):
            return
        with self._lock, closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute(
                """
                INSERT INTO requests(request_id, task_id, created)
                VALUES (?, ?, ?)
                ON CONFLICT(request_id) DO UPDATE SET
                    task_id = excluded.task_id,
                    created = excluded.created
                """,
                (request_id, task_id, time.time()),
            )
