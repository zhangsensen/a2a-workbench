"""共享的 SQLite 任务持久化工厂：让每个 agent 的任务历史落盘，重启不丢。"""
from pathlib import Path

from a2a.server.tasks.database_task_store import DatabaseTaskStore
from sqlalchemy.ext.asyncio import create_async_engine

DATA_DIR = Path(__file__).resolve().parent / "data"


def make_store(agent_name: str) -> DatabaseTaskStore:
    """为指定 agent 建一个独立的 SQLite 任务库。"""
    DATA_DIR.mkdir(exist_ok=True)
    db_path = DATA_DIR / f"{agent_name}-tasks.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    return DatabaseTaskStore(engine=engine, create_table=True)
