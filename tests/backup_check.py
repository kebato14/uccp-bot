"""Резервное копирование базы: корректность копии, ротация, восстановление.

Запуск: .venv/bin/python tests/backup_check.py
"""
from __future__ import annotations

import asyncio
import datetime as dt
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WORK = tempfile.mkdtemp()
os.environ["BOT_TOKEN"] = "test:token"
os.environ["DB_URL"] = "sqlite+aiosqlite:///" + os.path.join(WORK, "uccp.db")
os.environ["BACKUP_DIR"] = os.path.join(WORK, "backups")
os.environ["BACKUP_KEEP"] = "3"

from sqlalchemy import select  # noqa: E402

from uccp_bot.db.base import SessionMaker, init_db  # noqa: E402
from uccp_bot.db.models import (  # noqa: E402
    Brand, Category, Outlet, Priority, Request, RoleCode, Status, User, UserStatus,
)
from uccp_bot.db.seed import seed  # noqa: E402
from uccp_bot.services import backup  # noqa: E402
from uccp_bot.services.numbering import new_external_id, next_request_number  # noqa: E402
from uccp_bot.utils import utcnow  # noqa: E402


def check(condition: bool, label: str) -> None:
    print(("✅ " if condition else "❌ ") + label)
    if not condition:
        raise AssertionError(label)


async def main() -> None:
    await init_db()
    async with SessionMaker() as session:
        await seed(session)
        moose = await session.scalar(select(Brand).where(Brand.name == "Moose Café"))
        opera = await session.scalar(select(Outlet).where(Outlet.name == "Moose Opera"))
        electric = await session.scalar(select(Category).where(Category.code == "electric"))
        author = User(tg_id=4242, full_name="Тестов Тест", last_name="Тестов",
                      role_code=RoleCode.OUTLET_ADMIN, status=UserStatus.ACTIVE,
                      is_registered=True)
        author.sync_flags()
        session.add(author)
        await session.flush()
        req = Request(
            number=await next_request_number(session), external_id=new_external_id(),
            author_id=author.id, brand_id=moose.id, outlet_id=opera.id,
            category_id=electric.id, description="Проверка бэкапа",
            priority=Priority.HIGH, status=Status.NEW,
            due_at=utcnow() + dt.timedelta(hours=3),
        )
        session.add(req)
        await session.commit()
        number = req.number

    print("=== Создание копии ===")
    check(backup.db_file_path().endswith("uccp.db"), "Путь к базе определён")
    result = await backup.run_backup()
    check(os.path.exists(result.path), f"Копия создана: {os.path.basename(result.path)}")
    check(result.size > 10000, f"Размер копии: {result.size_kb} КБ")
    check(not backup.drive_available(),
          "Без доступа к Google копия делается локально и не падает")
    check(result.drive_error == "", "Ошибок выгрузки нет — выгрузка просто пропущена")

    print("\n=== Копия пригодна для восстановления ===")
    conn = sqlite3.connect(result.path)
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        check(integrity == "ok", f"SQLite проверка целостности: {integrity}")
        rows = conn.execute("SELECT number, description FROM requests").fetchall()
        check(len(rows) == 1 and rows[0][0] == number, f"Заявка {number} в копии на месте")
        users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        check(users >= 4, f"Пользователи в копии: {users}")
        outlets = conn.execute("SELECT COUNT(*) FROM outlets").fetchone()[0]
        check(outlets >= 14, f"Справочник объектов в копии: {outlets}")
    finally:
        conn.close()

    print("\n=== Копия снимается на работающем боте ===")
    async with SessionMaker() as session:
        # пишем в базу и одновременно снимаем копию
        author = await session.scalar(select(User).where(User.tg_id == 4242))
        author.full_name = "Изменённый во время копии"
        task = asyncio.create_task(backup.run_backup(tag="parallel"))
        await session.commit()
        hot = await task
    check(os.path.exists(hot.path), "Копия снята без остановки бота")
    conn = sqlite3.connect(hot.path)
    try:
        check(conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok",
              "Горячая копия не повреждена")
    finally:
        conn.close()

    print("\n=== Ротация ===")
    for day in range(5):
        backup.make_backup(tag=f"день{day}")
    files = backup.list_backups()
    check(len(files) == 3, f"Хранятся только последние 3 копии: {len(files)}")
    check(all(f.startswith("uccp_backup_") for f in files), "Имена копий единообразны")

    print("\n=== Восстановление ===")
    import shutil

    latest = os.path.join(os.environ["BACKUP_DIR"], backup.list_backups()[0])
    restored = os.path.join(WORK, "restored.db")
    shutil.copy(latest, restored)
    conn = sqlite3.connect(restored)
    try:
        got = conn.execute("SELECT number FROM requests").fetchone()[0]
        check(got == number, "База восстанавливается простым копированием файла")
        check(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] >= 4,
              "Пользователи после восстановления на месте")
    finally:
        conn.close()

    print("\n🎉 Резервное копирование работает.")


if __name__ == "__main__":
    asyncio.run(main())
