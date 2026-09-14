"""Лёгкие миграции схемы для SQLite.

create_all() не добавляет новые колонки в существующие таблицы, поэтому
недостающие поля дописываем вручную. Запускается при каждом старте,
повторный запуск безопасен.
"""
from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

log = logging.getLogger(__name__)

# таблица -> (колонка, определение)
COLUMNS = [
    ("users", "first_name", "VARCHAR(80)"),
    ("users", "last_name", "VARCHAR(80)"),
    ("users", "position_text", "VARCHAR(200)"),
    ("users", "status", "VARCHAR(16) DEFAULT 'active'"),
    ("users", "applied_at", "DATETIME"),
    ("users", "approved_at", "DATETIME"),
    ("users", "approved_by_id", "INTEGER"),
    ("users", "reject_reason", "TEXT"),
    ("users", "fixed_payment", "BOOLEAN DEFAULT 0"),
    ("requests", "cost_exempt", "BOOLEAN DEFAULT 0"),
]


async def migrate(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        for table, column, definition in COLUMNS:
            rows = await conn.execute(text(f"PRAGMA table_info({table})"))
            existing = {row[1] for row in rows}
            if not existing:
                continue  # таблицы ещё нет — её создаст create_all
            if column not in existing:
                await conn.execute(
                    text(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
                )
                log.info("Миграция: добавлена колонка %s.%s", table, column)

        # старые роли -> новые
        await conn.execute(
            text("UPDATE users SET role_code = 'staff' WHERE role_code = 'manager'")
        )
        # у существующих пользователей доступ уже был — не блокируем их
        await conn.execute(
            text("UPDATE users SET status = 'active' WHERE status IS NULL OR status = ''")
        )
        await conn.execute(
            text(
                "UPDATE users SET status = 'disabled' "
                "WHERE is_active = 0 AND status = 'active'"
            )
        )
        await conn.execute(
            text("UPDATE users SET is_active = (status = 'active')")
        )
        # ФИО -> имя/фамилия там, где их ещё нет
        await conn.execute(
            text(
                "UPDATE users SET last_name = full_name "
                "WHERE (last_name IS NULL OR last_name = '') AND full_name IS NOT NULL"
            )
        )
