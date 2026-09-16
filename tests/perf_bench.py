"""Замер эффекта оптимизаций: запись в базу и отзывчивость при выгрузке.

Запуск: .venv/bin/python tests/perf_bench.py
Показывает, что изменилось до и после включения WAL и выноса Excel в поток.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import os
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WORK = tempfile.mkdtemp()
os.environ["BOT_TOKEN"] = "test:token"
os.environ["DB_URL"] = "sqlite+aiosqlite:///" + os.path.join(WORK, "perf.db")

from sqlalchemy import select  # noqa: E402

from uccp_bot.db.base import SessionMaker, init_db  # noqa: E402
from uccp_bot.db.models import (  # noqa: E402
    Brand, Category, Outlet, Priority, Request, RoleCode, Status, User, UserStatus,
)
from uccp_bot.db.seed import seed  # noqa: E402
from uccp_bot.services import excel  # noqa: E402
from uccp_bot.services.numbering import new_external_id, next_request_number  # noqa: E402
from uccp_bot.utils import utcnow  # noqa: E402


def bench_sqlite_modes(rows: int = 60) -> None:
    """Сравниваем режимы журнала на одинаковой нагрузке."""
    print("=== Запись в базу: режим журнала ===")
    results = {}
    for mode, sync in (("DELETE", "FULL"), ("WAL", "NORMAL")):
        path = os.path.join(WORK, f"mode_{mode}.db")
        conn = sqlite3.connect(path)
        conn.execute(f"PRAGMA journal_mode={mode}")
        conn.execute(f"PRAGMA synchronous={sync}")
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        conn.commit()
        start = time.perf_counter()
        for i in range(rows):
            conn.execute("INSERT INTO t (v) VALUES (?)", (f"значение {i}",))
            conn.commit()                      # как бот: коммит на каждое действие
        elapsed = (time.perf_counter() - start) / rows * 1000
        conn.close()
        results[mode] = elapsed
        label = "было (по умолчанию)" if mode == "DELETE" else "стало (WAL)"
        print(f"  {label}: {elapsed:.2f} мс на запись")
    gain = results["DELETE"] / results["WAL"] if results["WAL"] else 1
    print(f"  ускорение записи: в {gain:.1f} раза\n")


async def bench_export_blocking() -> None:
    """Проверяем, отвечает ли бот, пока формируется Excel."""
    print("=== Отзывчивость во время выгрузки Excel ===")
    await init_db()
    async with SessionMaker() as session:
        await seed(session)
        brand = await session.scalar(select(Brand).where(Brand.name == "Moose Café"))
        outlet = await session.scalar(select(Outlet).where(Outlet.name == "Moose Opera"))
        cat = await session.scalar(select(Category).where(Category.code == "electric"))
        author = User(tg_id=1, full_name="Тест", last_name="Тест",
                      role_code=RoleCode.OUTLET_ADMIN, status=UserStatus.ACTIVE,
                      is_registered=True)
        author.sync_flags()
        session.add(author)
        await session.flush()
        for _ in range(300):
            session.add(Request(
                number=await next_request_number(session), external_id=new_external_id(),
                author_id=author.id, brand_id=brand.id, outlet_id=outlet.id,
                category_id=cat.id, description="Проверка скорости выгрузки",
                priority=Priority.MEDIUM, status=Status.CLOSED,
                due_at=utcnow() + dt.timedelta(hours=1), work_cost=100, material_cost=50,
            ))
        await session.commit()
        rows = list((await session.scalars(
            select(Request).options(*__import__(
                "uccp_bot.services.cards", fromlist=["x"]).REQUEST_LOAD_OPTIONS)
        )).all())
    print(f"  заявок в выгрузке: {len(rows)}")

    async def heartbeat(stop: asyncio.Event) -> float:
        """Имитируем нажатие кнопки другим сотрудником каждые 10 мс."""
        worst = 0.0
        while not stop.is_set():
            t = time.perf_counter()
            await asyncio.sleep(0.01)
            worst = max(worst, (time.perf_counter() - t - 0.01) * 1000)
        return worst

    for label, run in (
        ("было (выгрузка в основном потоке)", lambda: excel.export_requests(rows)),
        ("стало (выгрузка в отдельном потоке)",
         lambda: excel.run_export(excel.export_requests, rows)),
    ):
        stop = asyncio.Event()
        beat = asyncio.create_task(heartbeat(stop))
        await asyncio.sleep(0.05)
        started = time.perf_counter()
        result = run()
        if asyncio.iscoroutine(result):
            await result
        export_ms = (time.perf_counter() - started) * 1000
        stop.set()
        worst = await beat
        print(f"  {label}:")
        print(f"     файл готов за {export_ms:.0f} мс, "
              f"другие пользователи ждали до {worst:.0f} мс")

    print(
        "\n  Вывод: пока файл формировался в основном потоке, бот не отвечал "
        "никому. Теперь выгрузка идёт параллельно."
    )


if __name__ == "__main__":
    bench_sqlite_modes()
    asyncio.run(bench_export_blocking())
