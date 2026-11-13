"""Матрица прав доступа и фиксация данных (ТЗ по ролям).

Запуск: .venv/bin/python tests/rights_check.py
"""
from __future__ import annotations

import asyncio
import datetime as dt
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["BOT_TOKEN"] = "test:token"
os.environ["DB_URL"] = "sqlite+aiosqlite:///" + os.path.join(tempfile.mkdtemp(), "r.db")

from sqlalchemy import select  # noqa: E402

from uccp_bot.db.base import SessionMaker, init_db  # noqa: E402
from uccp_bot.db.models import (  # noqa: E402
    Brand, Category, CostStatus, Outlet, Priority, Request, RequestStatusHistory,
    RoleCode, Status, User, UserScope, UserStatus,
)
from uccp_bot.db.seed import seed  # noqa: E402
from uccp_bot.keyboards.request import card_kb  # noqa: E402
from uccp_bot.services import scope  # noqa: E402
from uccp_bot.services.numbering import new_external_id, next_request_number  # noqa: E402
from uccp_bot.utils import utcnow  # noqa: E402


def check(condition: bool, label: str) -> None:
    print(("✅ " if condition else "❌ ") + label)
    if not condition:
        raise AssertionError(label)


def buttons(request, user) -> str:
    kb = card_kb(request, user)
    return " | ".join(b.text for row in kb.inline_keyboard for b in row)


async def main() -> None:
    await init_db()
    async with SessionMaker() as session:
        await seed(session)
        moose = await session.scalar(select(Brand).where(Brand.name == "Moose Café"))
        hd = await session.scalar(select(Brand).where(Brand.name == "Hotdogger"))
        opera = await session.scalar(select(Outlet).where(Outlet.name == "Moose Opera"))
        airport = await session.scalar(select(Outlet).where(Outlet.name == "Hotdogger Аэропорт"))
        electric = await session.scalar(select(Category).where(Category.code == "electric"))
        master = await session.scalar(select(User).where(User.full_name == "Назрулло"))
        master.tg_id = 777
        await session.flush()

        def make(tg, name, role, brand=None, outlet=None):
            u = User(tg_id=tg, full_name=name, last_name=name, role_code=role,
                     brand_id=brand.id if brand else None,
                     outlet_id=outlet.id if outlet else None,
                     status=UserStatus.ACTIVE, is_registered=True)
            u.sync_flags()
            session.add(u)
            return u

        admin = make(1, "Главный админ", RoleCode.ADMIN)
        chief = make(2, "Главный опердир", RoleCode.OPS_CHIEF)
        ops_hd = make(3, "Опердир Hotdogger", RoleCode.OPS_DIRECTOR, hd)
        outlet_admin = make(4, "Админ Оперы", RoleCode.OUTLET_ADMIN, moose, opera)
        await session.flush()

        async def add(outlet, executor=None, status=Status.NEW):
            req = Request(
                number=await next_request_number(session), external_id=new_external_id(),
                author_id=outlet_admin.id, brand_id=outlet.brand_id, outlet_id=outlet.id,
                category_id=electric.id, executor_id=executor.id if executor else None,
                description="Тест прав", priority=Priority.HIGH, status=status,
                due_at=utcnow() + dt.timedelta(hours=5),
            )
            session.add(req)
            await session.flush()
            return req

        r_moose = await add(opera, master)
        r_hd = await add(airport, master)
        await session.commit()

        print("=== Матрица прав ===")
        rows = [
            ("Главный админ", admin), ("Главный опердир", chief),
            ("Опердир бренда", ops_hd), ("Исполнитель", master),
        ]
        for label, u in rows:
            print(f"  {label:18} отчёты={u.can_see_reports!s:5} "
                  f"правка={u.can_edit_locked!s:5} удаление={u.can_delete_requests!s:5} "
                  f"права={u.can_manage_users!s:5} согл.стоимости={u.can_approve_cost}")

        check(admin.can_edit_locked and admin.can_delete_requests
              and admin.can_manage_users and admin.can_approve_cost,
              "Главный админ: полный доступ")
        check(chief.can_see_reports and not chief.can_edit_locked
              and not chief.can_delete_requests and not chief.can_manage_users,
              "Главный опердир: только просмотр и отчёты")
        check(ops_hd.can_see_reports and not ops_hd.can_edit_locked
              and not ops_hd.can_confirm,
              "Опердир бренда: только просмотр и отчёты")
        check(not master.can_see_reports, "Исполнителю отчёты недоступны")
        check(not master.can_delete_requests and not chief.can_delete_requests,
              "Удалять заявки не может никто, кроме главного админа")

        print("\n=== Область видимости ===")
        async def visible(u):
            stmt = select(Request)
            cond = await scope.request_condition(session, u)
            if cond is not None:
                stmt = stmt.where(cond)
            return {r.number for r in (await session.scalars(stmt)).all()}

        check(await visible(chief) == {r_moose.number, r_hd.number},
              "Главный опердир видит оба бренда")
        check(await visible(ops_hd) == {r_hd.number},
              "Опердир Hotdogger видит только свой бренд")
        check(await visible(outlet_admin) == {r_moose.number, r_hd.number},
              "Автор видит свои заявки по любым точкам")

        # администратор закрепляет дополнительный объект
        session.add(UserScope(user_id=ops_hd.id, outlet_id=opera.id,
                              brand_id=opera.brand_id, created_by_id=admin.id))
        await session.commit()
        check(await visible(ops_hd) == {r_hd.number, r_moose.number},
              "После закрепления объекта опердир видит и его")

        print("\n=== Наблюдатель ничего не меняет ===")
        r_moose = await session.scalar(
            select(Request).where(Request.id == r_moose.id).options(
                *__import__("uccp_bot.services.cards", fromlist=["x"]).REQUEST_LOAD_OPTIONS)
        )
        chief_buttons = buttons(r_moose, chief)
        check("История" in chief_buttons and "Файлы" in chief_buttons,
              "Руководителю доступны история и файлы")
        for forbidden in ("Принять", "Подтвердить", "Отменить", "Изменить",
                          "Назначить", "Согласовать"):
            check(forbidden not in chief_buttons,
                  f"В карточке у руководителя нет кнопки «{forbidden}»")

        print("\n=== Фиксация стоимости ===")
        r_moose.work_cost = 250
        r_moose.material_cost = 120
        r_moose.cost_status = CostStatus.PENDING
        r_moose.cost_submitted_at = utcnow()
        r_moose.status = Status.DONE
        await session.commit()

        check(not r_moose.executor_can_edit_cost(),
              "После отправки на согласование исполнитель стоимость не меняет")
        exec_buttons = buttons(r_moose, master)
        check("Заявить стоимость" not in exec_buttons,
              "Кнопки правки стоимости у исполнителя нет")

        r_moose.cost_status = CostStatus.REJECTED
        r_moose.cost_reject_reason = "Завышена цена материалов"
        await session.commit()
        check(r_moose.executor_can_edit_cost(),
              "После отклонения исполнитель может заявить сумму заново")
        check("Заявить стоимость заново" in buttons(r_moose, master),
              "Появляется кнопка повторной заявки")

        print("\n=== Фиксация срока ===")
        r_hd = await session.scalar(
            select(Request).where(Request.id == r_hd.id).options(
                *__import__("uccp_bot.services.cards", fromlist=["x"]).REQUEST_LOAD_OPTIONS)
        )
        check(r_hd.executor_can_edit_due(), "До отправки срок править можно")
        check("Предложить другое время" in buttons(r_hd, master),
              "Кнопка переноса срока доступна")
        r_hd.due_locked = True
        r_hd.status = Status.RESCHEDULE_PROPOSED
        await session.commit()
        check(not r_hd.executor_can_edit_due(),
              "После отправки на согласование срок исполнителю закрыт")
        check("Предложить другое время" not in buttons(r_hd, master),
              "Кнопка переноса срока исчезает")

        print("\n=== Журнал сохраняет первоначальные данные ===")
        from uccp_bot.services import history
        await history.log(
            session, r_moose, "cost_edited", user=admin,
            details="Было: работы 250,00 сомони, материалы 120,00 сомони. "
                    "Стало: работы 200,00 сомони, материалы 100,00 сомони. "
                    "Причина: пересчёт по накладной",
        )
        r_moose.work_cost = 200
        r_moose.material_cost = 100
        await session.commit()

        text = await history.render_history(session, r_moose.id)
        check("изменил зафиксированную стоимость" in text, "Правка записана в журнал")
        check("Было: работы 250" in text, "Первоначальные суммы сохранены в истории")
        check("Причина: пересчёт по накладной" in text, "Причина изменения сохранена")
        rows_count = len((await session.scalars(
            select(RequestStatusHistory).where(
                RequestStatusHistory.request_id == r_moose.id)
        )).all())
        check(rows_count >= 1, f"Записи журнала не удаляются: {rows_count}")

    print("\n🎉 Права доступа и фиксация данных соответствуют ТЗ.")


if __name__ == "__main__":
    asyncio.run(main())
