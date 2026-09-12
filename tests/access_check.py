"""Проверка разграничения доступа по ролям.

Запуск: .venv/bin/python tests/access_check.py
"""
from __future__ import annotations

import asyncio
import datetime as dt
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["BOT_TOKEN"] = "test:token"
os.environ["DB_URL"] = "sqlite+aiosqlite:///" + os.path.join(tempfile.mkdtemp(), "acc.db")

from sqlalchemy import select  # noqa: E402

from uccp_bot.db.base import SessionMaker, init_db  # noqa: E402
from uccp_bot.db.models import (  # noqa: E402
    Brand, Category, Outlet, Priority, Request, RoleCode, Status, User, UserStatus,
)
from uccp_bot.db.seed import seed  # noqa: E402
from uccp_bot.handlers.lists import _can_view, scope_condition  # noqa: E402
from uccp_bot.keyboards.common import (  # noqa: E402
    BTN_ADMIN, BTN_BRAND_REQUESTS, BTN_OUTLET_REQUESTS, BTN_REPORTS, main_menu,
)
from uccp_bot.services.numbering import new_external_id, next_request_number  # noqa: E402
from uccp_bot.utils import utcnow  # noqa: E402


def check(condition: bool, label: str) -> None:
    print(("✅ " if condition else "❌ ") + label)
    if not condition:
        raise AssertionError(label)


def menu_labels(user) -> str:
    kb = main_menu(user)
    return " ".join(b.text for row in kb.keyboard for b in row)


async def visible(session, user) -> set:
    stmt = select(Request)
    condition = scope_condition(user)
    if condition is not None:
        stmt = stmt.where(condition)
    return {r.number for r in (await session.scalars(stmt)).all()}


async def main() -> None:
    await init_db()
    async with SessionMaker() as session:
        await seed(session)

        moose = await session.scalar(select(Brand).where(Brand.name == "Moose Café"))
        hd = await session.scalar(select(Brand).where(Brand.name == "Hotdogger"))
        opera = await session.scalar(select(Outlet).where(Outlet.name == "Moose Opera"))
        tcell = await session.scalar(select(Outlet).where(Outlet.name == "Moose TCell"))
        airport = await session.scalar(select(Outlet).where(Outlet.name == "Hotdogger Аэропорт"))
        electric = await session.scalar(select(Category).where(Category.code == "electric"))
        nazrullo = await session.scalar(select(User).where(User.full_name == "Назрулло"))

        def make_user(tg, name, role, brand=None, outlet=None, status=UserStatus.ACTIVE):
            u = User(
                tg_id=tg, full_name=name, last_name=name, role_code=role,
                brand_id=brand.id if brand else None,
                outlet_id=outlet.id if outlet else None,
                status=status, is_registered=True,
            )
            u.sync_flags()
            session.add(u)
            return u

        staff = make_user(101, "Сотрудник Оперы", RoleCode.STAFF, moose, opera)
        staff2 = make_user(102, "Сотрудник TCell", RoleCode.STAFF, moose, tcell)
        outlet_admin = make_user(103, "Админ Оперы", RoleCode.OUTLET_ADMIN, moose, opera)
        ops = make_user(104, "Опердир Moose", RoleCode.OPS_DIRECTOR, moose)
        sysadmin = make_user(105, "Админ системы", RoleCode.ADMIN)
        pending = make_user(106, "Новичок", RoleCode.STAFF, moose, opera, UserStatus.PENDING)
        blocked = make_user(107, "Блокированный", RoleCode.STAFF, moose, opera, UserStatus.BLOCKED)
        await session.flush()

        async def add(author, outlet, executor=None):
            req = Request(
                number=await next_request_number(session),
                external_id=new_external_id(),
                author_id=author.id, brand_id=outlet.brand_id, outlet_id=outlet.id,
                category_id=electric.id, executor_id=executor.id if executor else None,
                description="Тест", priority=Priority.MEDIUM, status=Status.NEW,
                due_at=utcnow() + dt.timedelta(hours=5),
            )
            session.add(req)
            await session.flush()
            return req

        r_opera = await add(staff, opera, nazrullo)
        r_tcell = await add(staff2, tcell)
        r_hd = await add(staff2, airport)
        await session.commit()

        print("=== Видимость заявок ===")
        check(await visible(session, staff) == {r_opera.number},
              "Сотрудник видит только свои заявки")
        check(await visible(session, outlet_admin) == {r_opera.number},
              "Администратор точки видит заявки только своей точки")
        check(await visible(session, ops) == {r_opera.number, r_tcell.number},
              "Операционный директор видит все точки своего бренда")
        check(r_hd.number not in await visible(session, ops),
              "Операционный директор не видит чужой бренд")
        check(await visible(session, sysadmin) == {r_opera.number, r_tcell.number, r_hd.number},
              "Администратор системы видит всё")
        check(await visible(session, nazrullo) == {r_opera.number},
              "Мастер видит только назначенные ему заявки")

        print("\n=== Доступ к карточке ===")
        check(not _can_view(staff, r_tcell), "Сотрудник не откроет чужую заявку")
        check(not _can_view(outlet_admin, r_tcell), "Админ точки не откроет заявку другой точки")
        check(_can_view(ops, r_tcell), "Опердир откроет заявку своего бренда")
        check(not _can_view(ops, r_hd), "Опердир не откроет заявку чужого бренда")
        check(_can_view(sysadmin, r_hd), "Администратор системы откроет любую")

        print("\n=== Меню по ролям ===")
        check(BTN_REPORTS not in menu_labels(staff), "У сотрудника нет отчётов")
        check(BTN_OUTLET_REQUESTS in menu_labels(outlet_admin),
              "У администратора точки есть «Заявки точки»")
        check(BTN_REPORTS in menu_labels(outlet_admin), "У администратора точки есть отчёты")
        check(BTN_BRAND_REQUESTS in menu_labels(ops), "У опердира есть «Заявки бренда»")
        check(BTN_ADMIN not in menu_labels(ops), "У опердира нет панели администрирования")
        check(BTN_ADMIN in menu_labels(sysadmin), "У администратора системы есть панель")
        check(BTN_OUTLET_REQUESTS not in menu_labels(staff),
              "Сотрудник не видит заявки всей точки")

        print("\n=== Статусы доступа ===")
        check(menu_labels(pending).strip() == "📖 Инструкция / Помощь",
              "Неподтверждённый пользователь не получает рабочее меню")
        check(not pending.is_approved and not pending.is_active,
              "Заявка на регистрацию не даёт доступа")
        check(not blocked.is_approved, "Заблокированный пользователь отключён")
        check(not blocked.can_see_reports and not blocked.can_confirm,
              "У заблокированного нет прав")

        print("\n=== Подтверждение работ ===")
        check(not nazrullo.can_confirm, "Исполнитель не может подтверждать выполнение")
        check(outlet_admin.can_confirm and ops.can_confirm and staff.can_confirm,
              "Инициатор и руководители могут подтверждать")

    print("\n🎉 Разграничение доступа работает.")


if __name__ == "__main__":
    asyncio.run(main())
