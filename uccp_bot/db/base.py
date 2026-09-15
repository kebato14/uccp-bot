"""Подключение к БД и фабрика сессий."""
from __future__ import annotations

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from ..config import config
from .models import Base

engine = create_async_engine(config.db_url, echo=False, future=True)
SessionMaker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@event.listens_for(engine.sync_engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record):  # pragma: no cover
    """Настройки SQLite.

    WAL и synchronous=NORMAL критичны на сервере: без них каждая запись
    ждёт полной синхронизации с диском, и бот «задумывается» на каждое
    действие. busy_timeout убирает ошибки блокировки при одновременных
    записях (заявка + фоновая задача).
    """
    if "sqlite" not in config.db_url:
        return
    cursor = dbapi_connection.cursor()
    for pragma in (
        "PRAGMA foreign_keys=ON",
        "PRAGMA journal_mode=WAL",
        "PRAGMA synchronous=NORMAL",
        "PRAGMA busy_timeout=5000",
        "PRAGMA cache_size=-16000",     # 16 МБ кэша страниц
        "PRAGMA temp_store=MEMORY",
    ):
        cursor.execute(pragma)
    cursor.close()


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    from .migrate import migrate

    await migrate(engine)
