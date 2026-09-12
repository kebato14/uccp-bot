"""Проверка месячного отчёта для Google Sheets и маршрутизации по обоим брендам.

Запуск: .venv/bin/python tests/gsheets_check.py
Google API не вызывается — проверяется содержимое листа, которое бот отправляет.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["BOT_TOKEN"] = "test:token"
os.environ["DB_URL"] = "sqlite+aiosqlite:///" + os.path.join(tempfile.mkdtemp(), "gs.db")

from sqlalchemy import select  # noqa: E402

from uccp_bot.db.base import SessionMaker, init_db  # noqa: E402
from uccp_bot.db.models import (  # noqa: E402
    Brand, Category, Outlet, Priority, Request, RoleCode, Status, User, UserStatus,
)
from uccp_bot.db.seed import seed  # noqa: E402
from uccp_bot.services import gsheets, routing  # noqa: E402
from uccp_bot.services.numbering import new_external_id, next_request_number  # noqa: E402
from uccp_bot.utils import now_local, utcnow  # noqa: E402


def check(condition: bool, label: str) -> None:
    print(("✅ " if condition else "❌ ") + label)
    if not condition:
        raise AssertionError(label)


def find_row(payload, first_cell):
    for row in payload:
        if row and str(row[0]) == first_cell:
            return row
    return None


def section_rows(payload, title):
    """Строки блока до следующего пустого разделителя."""
    out, started = [], False
    for row in payload:
        if row and str(row[0]) == title:
            started = True
            continue
        if started:
            if not row:
                break
            out.append(row)
    return out[1:]  # без строки заголовков колонок


async def main() -> None:
    await init_db()
    async with SessionMaker() as session:
        await seed(session)

        moose = await session.scalar(select(Brand).where(Brand.name == "Moose Café"))
        hotdogger = await session.scalar(select(Brand).where(Brand.name == "Hotdogger"))
        opera = await session.scalar(select(Outlet).where(Outlet.name == "Moose Opera"))
        airport = await session.scalar(
            select(Outlet).where(Outlet.name == "Hotdogger Аэропорт")
        )
        equipment = await session.scalar(select(Category).where(Category.code == "equipment"))
        electric = await session.scalar(select(Category).where(Category.code == "electric"))

        # --- Алишер обслуживает оба бренда ---
        print("=== Маршрутизация по оборудованию ===")
        alisher = await session.scalar(select(User).where(User.full_name == "Алишер"))
        found_moose = await routing.find_executor(session, equipment.id, moose.id, opera.id)
        found_hd = await routing.find_executor(session, equipment.id, hotdogger.id, airport.id)
        check(found_moose is not None and found_moose.id == alisher.id,
              "Оборудование Moose → Алишер")
        check(found_hd is not None and found_hd.id == alisher.id,
              "Оборудование Hotdogger → Алишер")

        # --- заявки месяца ---
        author = User(tg_id=7001, full_name="Менеджер Тест",
                      role_code=RoleCode.OUTLET_ADMIN, status=UserStatus.ACTIVE,
                      is_active=True, is_registered=True)
        nazrullo = await session.scalar(select(User).where(User.full_name == "Назрулло"))
        session.add(author)
        await session.flush()

        async def add(brand, outlet, category, executor, status, work, material,
                      overdue=False):
            req = Request(
                number=await next_request_number(session),
                external_id=new_external_id(),
                author_id=author.id,
                brand_id=brand.id,
                outlet_id=outlet.id,
                category_id=category.id,
                executor_id=executor.id if executor else None,
                description="Тестовая заявка",
                priority=Priority.HIGH,
                status=status,
                due_at=utcnow() + dt.timedelta(hours=4),
                work_cost=work,
                material_cost=material,
                is_overdue=overdue,
                done_at=utcnow() if status in (Status.DONE, Status.CLOSED) else None,
                closed_at=utcnow() if status == Status.CLOSED else None,
            )
            session.add(req)
            await session.flush()
            return req

        await add(moose, opera, electric, nazrullo, Status.CLOSED, 250, 120.5)
        await add(moose, opera, equipment, alisher, Status.CLOSED, 400, 0)
        await add(hotdogger, airport, equipment, alisher, Status.IN_PROGRESS, 0, 0, overdue=True)
        await add(hotdogger, airport, electric, None, Status.AWAITING_ASSIGNMENT, 0, 0)
        await add(moose, opera, electric, nazrullo, Status.CANCELLED, 0, 0)
        await session.commit()

        # --- формирование листа ---
        print("\n=== Содержимое листа Google Sheets ===")
        today = now_local().date()
        year, month = today.year, today.month
        requests = await gsheets.month_requests(session, year, month)
        check(len(requests) == 5, f"В месяц попали все заявки: {len(requests)}")

        payload = gsheets.build_month_payload(requests, year, month)
        flat = "\n".join(" | ".join(str(c) for c in row) for row in payload)

        check(payload[0][0].startswith("Отчёт УЦЦП за"), f"Заголовок: {payload[0][0]}")
        check(payload[4] == gsheets.DETAIL_HEADERS, "Шапка детализации по ТЗ")
        for field in ("№ заявки", "Дата заявки", "Бренд", "Торговая точка", "Категория",
                      "Описание", "Исполнитель", "Приоритет", "Срок", "Статус",
                      "Дата выполнения", "Дата закрытия", "Стоимость работ",
                      "Стоимость материалов", "Просрочка"):
            check(field in gsheets.DETAIL_HEADERS, f"Колонка «{field}» есть")

        detail = [r for r in payload if r and str(r[0]).startswith("REQ-")]
        check(len(detail) == 5, f"Строк детализации: {len(detail)}")
        check(any(r[15] == "да" for r in detail), "Просрочка отмечена в детализации")

        check(find_row(payload, "Всего заявок")[1] == 5, "Сводка: всего заявок 5")
        check(find_row(payload, "Выполнено и закрыто")[1] == 2, "Сводка: закрыто 2")
        check(find_row(payload, "Не закрыто (в работе)")[1] == 2, "Сводка: не закрыто 2")
        check(find_row(payload, "Просрочено")[1] == 1, "Сводка: просрочено 1")
        check(find_row(payload, "Отменено / отклонено")[1] == 1, "Сводка: отменено 1")
        total = find_row(payload, "ОБЩАЯ СУММА РАСХОДОВ")[1]
        check(abs(total - 770.5) < 0.01, f"Сводка: общая сумма {total}")

        brands = {r[0]: r for r in section_rows(payload, "РАСХОДЫ ПО БРЕНДАМ")}
        check(abs(brands["Moose Café"][4] - 770.5) < 0.01,
              f"Расходы Moose: {brands['Moose Café'][4]}")
        check(abs(brands["Hotdogger"][4]) < 0.01, "Расходы Hotdogger: 0 (работы без затрат)")
        check(brands["Hotdogger"][1] == 2, "Заявок Hotdogger: 2")

        outlets = {r[0]: r for r in section_rows(payload, "РАСХОДЫ ПО ТОРГОВЫМ ТОЧКАМ")}
        check("Moose Opera" in outlets and "Hotdogger Аэропорт" in outlets,
              "Разрез по каждой точке есть")

        executors = {r[0]: r for r in section_rows(payload, "РАСХОДЫ ПО ИСПОЛНИТЕЛЯМ")}
        check(abs(executors["Алишер"][4] - 400) < 0.01, "Расходы по Алишеру: 400")
        check(abs(executors["Назрулло"][4] - 370.5) < 0.01, "Расходы по Назрулло: 370,5")
        check("не назначен" in executors, "Заявки без исполнителя видны отдельно")

        check("РАСХОДЫ ПО КАТЕГОРИЯМ" in flat, "Разрез по категориям есть")
        check(gsheets.worksheet_title(2026, 9) == "2026-09", "Имя листа вида ГГГГ-ММ")
        py, pm = gsheets.previous_month(dt.date(2026, 1, 15))
        check((py, pm) == (2025, 12), "Прошлый месяц считается верно через год")

        # пустой месяц не должен ломать выгрузку
        empty = gsheets.build_month_payload([], 2020, 5)
        check(any("заявок не было" in str(r[0]) for r in empty if r),
              "Пустой месяц формируется без ошибок")

    print("\n🎉 Месячный отчёт формируется корректно.")


if __name__ == "__main__":
    asyncio.run(main())
