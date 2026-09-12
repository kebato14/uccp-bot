"""Организационные заявки УЦЦП: отдельное хранение и раздельная статистика.

Запуск: .venv/bin/python tests/org_check.py
"""
from __future__ import annotations

import asyncio
import datetime as dt
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["BOT_TOKEN"] = "test:token"
os.environ["DB_URL"] = "sqlite+aiosqlite:///" + os.path.join(tempfile.mkdtemp(), "org.db")

from sqlalchemy import select  # noqa: E402

from uccp_bot.db.base import SessionMaker, init_db  # noqa: E402
from uccp_bot.db.models import (  # noqa: E402
    Brand, Category, OrgRequest, OrgStatus, Outlet, Priority, Request, RoleCode,
    Status, User, UserStatus,
)
from uccp_bot.db.seed import seed  # noqa: E402
from uccp_bot.services import excel, gsheets, org, reports  # noqa: E402
from uccp_bot.services.numbering import (  # noqa: E402
    new_external_id, next_org_number, next_request_number,
)
from uccp_bot.utils import now_local, utcnow  # noqa: E402


def check(condition: bool, label: str) -> None:
    print(("✅ " if condition else "❌ ") + label)
    if not condition:
        raise AssertionError(label)


async def main() -> None:
    await init_db()
    async with SessionMaker() as session:
        await seed(session)

        moose = await session.scalar(select(Brand).where(Brand.name == "Moose Café"))
        hd = await session.scalar(select(Brand).where(Brand.name == "Hotdogger"))
        opera = await session.scalar(select(Outlet).where(Outlet.name == "Moose Opera"))
        tcell = await session.scalar(select(Outlet).where(Outlet.name == "Moose TCell"))
        electric = await session.scalar(select(Category).where(Category.code == "electric"))
        nazrullo = await session.scalar(select(User).where(User.full_name == "Назрулло"))

        def make(tg, name, role, brand=None, outlet=None):
            u = User(tg_id=tg, full_name=name, last_name=name, role_code=role,
                     brand_id=brand.id if brand else None,
                     outlet_id=outlet.id if outlet else None,
                     status=UserStatus.ACTIVE, is_registered=True)
            u.sync_flags()
            session.add(u)
            return u

        admin = make(9001, "Админ системы", RoleCode.ADMIN)
        ops = make(9002, "Опердир Moose", RoleCode.OPS_DIRECTOR, moose)
        outlet_admin = make(9003, "Админ Оперы", RoleCode.OUTLET_ADMIN, moose, opera)
        staff = make(9004, "Сотрудник Оперы", RoleCode.STAFF, moose, opera)
        await session.flush()

        print("=== Права на постановку задач ===")
        check(org.can_create(admin) and org.can_create(ops) and org.can_create(outlet_admin),
              "Организационные задачи ставят руководители и администратор")
        check(not org.can_create(staff), "Рядовой сотрудник задачи УЦЦП не создаёт")
        check(not org.can_create(nazrullo), "Мастер задачи УЦЦП не создаёт")

        # --- ремонтная заявка ---
        tech = Request(
            number=await next_request_number(session), external_id=new_external_id(),
            author_id=staff.id, brand_id=moose.id, outlet_id=opera.id,
            category_id=electric.id, executor_id=nazrullo.id,
            description="Не работает розетка", priority=Priority.HIGH,
            status=Status.CLOSED, due_at=utcnow(), work_cost=250, material_cost=120.5,
            closed_at=utcnow(),
        )
        session.add(tech)
        await session.flush()

        # --- организационные заявки ---
        async def add_org(author, assignee, outlet=None, object_text=None, brand=None,
                          status=OrgStatus.NEW, cost=None, overdue=False):
            req = OrgRequest(
                number=await next_org_number(session), external_id=new_external_id(),
                outlet_id=outlet.id if outlet else None,
                brand_id=(outlet.brand_id if outlet else (brand.id if brand else None)),
                object_text=object_text,
                description="Подготовить документы по объекту",
                priority=Priority.MEDIUM, due_at=utcnow() + dt.timedelta(days=2),
                author_id=author.id, assignee_id=assignee.id, status=status,
                cost=cost, is_overdue=overdue,
                closed_at=utcnow() if status == OrgStatus.CLOSED else None,
            )
            session.add(req)
            await session.flush()
            await org.log(session, req, "created", user=author, new_status=OrgStatus.NEW)
            return req

        o1 = await add_org(admin, outlet_admin, outlet=opera, status=OrgStatus.CLOSED, cost=300)
        o2 = await add_org(ops, staff, outlet=tcell, overdue=True)
        o3 = await add_org(admin, ops, object_text="Офис УЦЦП")
        await session.commit()

        print("\n=== Раздельное хранение ===")
        check(o1.number.startswith("ORG-"), f"Своя нумерация: {o1.number}")
        check(tech.number.startswith("REQ-"), f"У ремонтных своя: {tech.number}")
        check(OrgRequest.__tablename__ == "org_requests", "Отдельная таблица в БД")
        tech_count = len((await session.scalars(select(Request))).all())
        org_count = len((await session.scalars(select(OrgRequest))).all())
        check(tech_count == 1 and org_count == 3,
              f"Данные не смешаны: ремонтных {tech_count}, организационных {org_count}")
        check(o3.object_name == "Офис УЦЦП", "Объект вне торговых точек поддерживается")
        check(o1.author_id == admin.id, "Автор подставлен из профиля")

        print("\n=== Видимость ===")
        check(org.can_view(outlet_admin, o1), "Админ точки видит задачу своей точки")
        check(not org.can_view(outlet_admin, o3), "Админ точки не видит чужой объект")
        check(org.can_view(ops, o2), "Опердир видит задачи своего бренда")
        check(org.can_view(staff, o2), "Ответственный видит назначенную ему задачу")
        check(not org.can_view(staff, o1), "Посторонний задачу не видит")
        check(org.can_view(admin, o2), "Администратор видит всё")
        check(org.can_close(admin, o2) and org.can_close(ops, o2),
              "Закрывает автор или администратор")
        check(not org.can_close(staff, o2), "Ответственный сам себя не закрывает")

        print("\n=== Раздельная статистика ===")
        today = now_local().date()
        f = reports.ReportFilters(date_from=today, date_to=today, period_title="за сегодня")
        tech_report = await reports.build_report(session, f)
        org_report = await reports.build_org_report(session, f)

        check(tech_report.total == 1, f"В отчёте ремонтных только ремонтные: {tech_report.total}")
        check(abs(tech_report.total_cost - 370.5) < 0.01,
              f"Расходы ремонтных: {tech_report.total_cost}")
        check(org_report.total == 3, f"В отчёте УЦЦП только организационные: {org_report.total}")
        check(abs(org_report.total_cost - 300) < 0.01, f"Затраты УЦЦП: {org_report.total_cost}")
        check(org_report.closed == 1 and org_report.overdue == 1,
              "Свои статусы и просрочка считаются отдельно")
        check("Moose Opera" in org_report.by_object and "Офис УЦЦП" in org_report.by_object,
              "Разрез по объектам")
        check("Сотрудник Оперы" in org_report.by_assignee, "Разрез по ответственным")

        org_text = reports.render_org_report(org_report, f)
        check("Организационные заявки УЦЦП" in org_text, "Отчёт УЦЦП формируется")
        check("REQ-" not in org_text, "В отчёте УЦЦП нет ремонтных заявок")

        print("\n=== Сводный отчёт администратора ===")
        combined = reports.render_combined_report(tech_report, org_report, f)
        check("Технические и ремонтные заявки" in combined and
              "Организационные заявки УЦЦП" in combined,
              "Оба модуля показаны раздельными блоками")
        check("Заявок: <b>4</b>" in combined, "Сводный итог по количеству")
        check("670" in combined.replace(",", "."), "Сводный итог по расходам: 370,5 + 300")

        print("\n=== Выгрузки ===")
        org_rows = await reports.fetch_org_requests(session, f)
        tech_rows = await reports.fetch_requests(session, f)
        path_org = excel.export_org_requests(org_rows)
        path_all = excel.export_combined(tech_rows, org_rows)
        check(os.path.exists(path_org), "Excel по заявкам УЦЦП выгружен")
        from openpyxl import load_workbook
        names = load_workbook(path_all).sheetnames
        check(names == ["Ремонтные", "Организационные"],
              f"Сводный Excel: два отдельных листа {names}")

        print("\n=== Google Sheets ===")
        payload = gsheets.build_org_month_payload(org_rows, today.year, today.month)
        flat = "\n".join(" | ".join(str(c) for c in row) for row in payload)
        check(payload[0][0].startswith("Организационные заявки УЦЦП за"), "Заголовок листа УЦЦП")
        check(payload[4] == gsheets.ORG_DETAIL_HEADERS, "Шапка: объект, автор, ответственный, срок")
        check("ЗАТРАТЫ ПО ОБЪЕКТАМ" in flat and "ЗАТРАТЫ ПО ОТВЕТСТВЕННЫМ" in flat,
              "Сводная часть листа УЦЦП")
        check(gsheets.org_worksheet_title(2026, 9) == "2026-09-УЦЦП",
              "Отдельный лист месяца для УЦЦП")
        check("REQ-" not in flat, "На листе УЦЦП нет ремонтных заявок")

    print("\n🎉 Модуль организационных заявок работает раздельно.")


if __name__ == "__main__":
    asyncio.run(main())
