"""Сквозная проверка бизнес-логики без Telegram (п.51).

Запуск:  .venv/bin/python tests/e2e_check.py
Проверяет оба обязательных сценария из ТЗ:
 1) Электрика → есть зарегистрированный исполнитель → полный цикл до закрытия и отчёта.
 2) Вентиляция → исполнителя нет → заявка уходит администратору → назначение → цикл продолжается.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["BOT_TOKEN"] = "test:token"
os.environ["DB_URL"] = "sqlite+aiosqlite:///" + os.path.join(
    tempfile.mkdtemp(), "e2e.db"
)

from sqlalchemy import func, select  # noqa: E402

from uccp_bot.db.base import SessionMaker, init_db  # noqa: E402
from uccp_bot.db.models import (  # noqa: E402
    Attachment, Brand, Category, Cost, Outlet, Priority, Request,
    ExecutorAssignment, RequestStatusHistory, RoleCode, Status, User, UserStatus,
)
from uccp_bot.db.seed import seed  # noqa: E402
from uccp_bot.services import excel, flow, history, reports, routing  # noqa: E402
from uccp_bot.services.cards import load_request, render_card  # noqa: E402
from uccp_bot.services.numbering import new_external_id, next_request_number  # noqa: E402
from uccp_bot.utils import utc_from_local, utcnow  # noqa: E402

SENT: list = []


class FakeBot:
    """Заглушка Telegram — просто записывает, кому что ушло."""

    async def send_message(self, chat_id, text, reply_markup=None, **kw):
        SENT.append((chat_id, text))
        return True

    async def send_photo(self, chat_id, file_id, caption=None, **kw):
        SENT.append((chat_id, f"[photo] {caption or ''}"))

    async def send_video(self, chat_id, file_id, caption=None, **kw):
        SENT.append((chat_id, f"[video] {caption or ''}"))

    async def send_media_group(self, chat_id, media, **kw):
        SENT.append((chat_id, f"[media group x{len(media)}]"))


def check(condition: bool, label: str) -> None:
    mark = "✅" if condition else "❌"
    print(f"{mark} {label}")
    if not condition:
        raise AssertionError(label)


async def make_request(session, author, brand, outlet, category, priority=Priority.HIGH,
                       hours=6, with_photo=True):
    request = Request(
        number=await next_request_number(session),
        external_id=new_external_id(),
        author_id=author.id,
        brand_id=brand.id,
        outlet_id=outlet.id,
        category_id=category.id,
        description="Не работает вытяжка над грилем, гудит и не тянет воздух.",
        priority=priority,
        due_at=utcnow() + dt.timedelta(hours=hours),
        status=Status.NEW,
    )
    session.add(request)
    await session.flush()
    if with_photo:
        session.add(
            Attachment(request_id=request.id, file_id="FILE_BEFORE", media_type="photo",
                       stage="before", uploaded_by_id=author.id)
        )
    await session.flush()
    await session.refresh(request, ["attachments", "brand", "outlet", "category", "author"])
    await history.log(session, request, "created", user=author, new_status=Status.NEW)
    return request


async def main() -> None:
    bot = FakeBot()
    await init_db()

    async with SessionMaker() as session:
        await seed(session)

        brand = await session.scalar(select(Brand).where(Brand.name == "Moose Café"))
        outlet = await session.scalar(
            select(Outlet).where(Outlet.name == "Moose Opera")
        )
        electric = await session.scalar(select(Category).where(Category.code == "electric"))
        vent = await session.scalar(select(Category).where(Category.code == "ventilation"))

        # --- участники ---
        admin = User(tg_id=1001, full_name="Админ УЦЦП", role_code=RoleCode.ADMIN,
                     status=UserStatus.ACTIVE, is_active=True, is_registered=True)
        manager = User(tg_id=1002, full_name="Менеджер Мус",
                       role_code=RoleCode.OUTLET_ADMIN, brand_id=brand.id,
                       outlet_id=outlet.id, status=UserStatus.ACTIVE, is_active=True,
                       is_registered=True)
        session.add_all([admin, manager])
        await session.flush()

        # Назрулло из сидов — электрик, ещё не нажимал /start
        nazrullo = await session.scalar(select(User).where(User.full_name == "Назрулло"))
        check(nazrullo is not None and nazrullo.tg_id is None,
              "Сид: электрик заведён, но бот не активирован")

        # --- Сценарий 0: мастер не зарегистрирован → администратор получает предупреждение ---
        req0 = await make_request(session, manager, brand, outlet, electric)
        reason = await flow.dispatch_request(session, bot, req0, actor=manager)
        await session.commit()
        check(reason == "unregistered", "Незарегистрированный мастер определён")
        check(req0.status == Status.AWAITING_ASSIGNMENT,
              "Заявка не потеряна: ожидает назначения")
        check(any("не зарегистрирован" in t for _, t in SENT if _ == 1001),
              "Администратор получил предупреждение")

        # Мастер нажал /start
        nazrullo.tg_id = 2001
        nazrullo.is_registered = True
        await session.commit()

        # --- Сценарий 1 (п.51): электрика, полный цикл ---
        print("\n--- Сценарий 1: электрика, исполнитель найден ---")
        req = await make_request(session, manager, brand, outlet, electric)
        reason = await flow.dispatch_request(session, bot, req, actor=manager)
        await session.commit()
        check(reason == "ok", "Исполнитель найден автоматически")
        check(req.executor_id == nazrullo.id, "Назначен электрик Назрулло")
        check(any(chat == 2001 for chat, _ in SENT), "Исполнитель получил заявку в личку")
        check(req.number.startswith("REQ-"), f"Номер присвоен: {req.number}")

        # принять → начать → выполнить
        req.status = Status.ACCEPTED
        req.accepted_at = utcnow()
        await history.log(session, req, "accepted", user=nazrullo,
                          old_status=Status.NEW, new_status=Status.ACCEPTED)
        req.status = Status.IN_PROGRESS
        req.started_at = utcnow()
        await history.log(session, req, "started", user=nazrullo,
                          old_status=Status.ACCEPTED, new_status=Status.IN_PROGRESS)
        req.executor_comment = "Заменён автомат, линия проверена."
        req.work_cost = 250
        req.material_cost = 120.5
        req.done_at = utcnow()
        req.status = Status.DONE
        session.add(Attachment(request_id=req.id, file_id="FILE_AFTER", media_type="photo",
                               stage="after", uploaded_by_id=nazrullo.id))
        session.add_all([
            Cost(request_id=req.id, kind="work", amount=250, added_by_id=nazrullo.id),
            Cost(request_id=req.id, kind="material", amount=120.5, added_by_id=nazrullo.id),
        ])
        await history.log(session, req, "done", user=nazrullo,
                          old_status=Status.IN_PROGRESS, new_status=Status.DONE)
        await session.commit()
        check(req.status == Status.DONE, "Статус «Выполнена» (ещё не закрыта)")

        # подтверждение менеджером → закрытие
        req = await load_request(session, req.id)
        await flow.close_request(session, bot, req, manager)
        await session.commit()
        check(req.status == Status.CLOSED, "Менеджер подтвердил → заявка закрыта")
        check(req.confirmed_at is not None and req.closed_at is not None,
              "Даты подтверждения и закрытия сохранены")
        check(abs(req.total_cost - 370.5) < 0.01, f"Стоимость посчитана: {req.total_cost}")

        hist = await session.scalar(
            select(func.count(RequestStatusHistory.id)).where(
                RequestStatusHistory.request_id == req.id
            )
        )
        check(hist >= 6, f"История изменений ведётся: {hist} записей")

        # --- Сценарий 2 (п.51): вентиляция, исполнителя нет ---
        print("\n--- Сценарий 2: вентиляция, исполнителя нет ---")
        SENT.clear()
        req2 = await make_request(session, manager, brand, outlet, vent)
        reason = await flow.dispatch_request(session, bot, req2, actor=manager)
        await session.commit()
        check(reason == "none", "Исполнитель по вентиляции не найден")
        check(req2.status == Status.AWAITING_ASSIGNMENT, "Статус «Ожидает назначения»")
        check(any(chat == 1001 and "Требуется назначить" in text for chat, text in SENT),
              "Администратор получил заявку на назначение")

        # администратор регистрирует мастера и назначает
        master = User(tg_id=3001, full_name="Мастер по вентиляции",
                      role_code=RoleCode.EXECUTOR, status=UserStatus.ACTIVE,
                      is_active=True, is_registered=True, phone="+992900000000")
        session.add(master)
        await session.flush()
        sent = await flow.assign_executor(session, bot, req2, master, admin)
        await session.commit()
        check(sent, "Назначенный мастер получил уведомление")
        check(req2.executor_id == master.id and req2.status == Status.NEW,
              "Заявка передана мастеру, цепочка продолжается")

        # маршрутизация после привязки направления
        from uccp_bot.db.models import ExecutorAssignment
        session.add(ExecutorAssignment(user_id=master.id, category_id=vent.id))
        await session.commit()
        found = await routing.find_executor(session, vent.id, brand.id, outlet.id)
        check(found is not None and found.id == master.id,
              "Следующие заявки по вентиляции уходят мастеру автоматически")

        # --- Общие объекты (офис, склад, цеха) ---
        print("\n--- Общие объекты ---")
        shared = await session.scalar(select(Brand).where(Brand.name == "Общие объекты"))
        check(shared is not None, "Группа «Общие объекты» есть в справочнике")
        names = {
            o.name
            for o in (
                await session.scalars(select(Outlet).where(Outlet.brand_id == shared.id))
            ).all()
        }
        check(
            {"Офис", "Центральный склад Зайнаб Мол", "Цех Пекарня", "Цех Кондитерский"}
            <= names,
            f"Объекты заведены: {len(names)}",
        )

        bakery = await session.scalar(select(Outlet).where(Outlet.name == "Цех Пекарня"))
        plumbing = await session.scalar(select(Category).where(Category.code == "plumbing"))
        asror = await session.scalar(select(User).where(User.full_name == "Асрор"))
        found = await routing.find_executor(session, plumbing.id, shared.id, bakery.id)
        check(
            found is not None and found.id == asror.id,
            "Сантехника по цеху Пекарня уходит мастеру по обоим брендам",
        )

        SENT.clear()
        req_shared = await make_request(session, manager, shared, bakery, plumbing)
        reason = await flow.dispatch_request(session, bot, req_shared, actor=manager)
        await session.commit()
        check(reason in ("ok", "unregistered"), "Заявка по общему объекту находит исполнителя")
        check(
            req_shared.outlet.name == "Цех Пекарня" and req_shared.brand.name == "Общие объекты",
            "Объект и группа сохранены в заявке",
        )

        # --- Фиксированная месячная оплата мастера ---
        print("\n--- Мастер на фиксированной оплате ---")
        coffee = await session.scalar(select(Category).where(Category.code == "coffee"))
        check(coffee is not None, "Категория «Кофемашины» есть в справочнике")

        barista_master = User(
            tg_id=4001, full_name="Мастер по кофемашинам", last_name="Мастер",
            role_code=RoleCode.EXECUTOR, status=UserStatus.ACTIVE, is_active=True,
            is_registered=True, fixed_payment=True,
        )
        session.add(barista_master)
        await session.flush()
        session.add(ExecutorAssignment(user_id=barista_master.id, category_id=coffee.id))
        await session.commit()

        found = await routing.find_executor(session, coffee.id, brand.id, outlet.id)
        check(found is not None and found.id == barista_master.id,
              "Заявки по кофемашинам уходят профильному мастеру")

        req_coffee = await make_request(session, manager, brand, outlet, coffee)
        req_coffee.executor_id = barista_master.id
        req_coffee.executor_comment = "Промывка группы, замена фильтра."
        req_coffee.done_at = utcnow()
        req_coffee.status = Status.DONE
        req_coffee.cost_exempt = True          # стоимость не тарифицируется
        await session.commit()

        check(req_coffee.work_cost is None and req_coffee.material_cost is None,
              "Суммы по заявке не заводятся")
        check(req_coffee.total_cost == 0, "В расходы такая заявка не попадает")
        card = render_card(req_coffee)
        check("фиксированная ежемесячная" in card.lower(),
              "В карточке пометка вместо стоимости")
        check("Работы:" not in card, "Строк со стоимостью в карточке нет")

        # --- Отчёты и Excel ---
        print("\n--- Отчёты ---")
        today = dt.date.today()
        filters = reports.ReportFilters(date_from=today, date_to=today,
                                        period_title="за сегодня")
        data = await reports.build_report(session, filters)
        check(data.total >= 3, f"Отчёт видит заявки: {data.total}")
        check(data.closed >= 1, f"Закрытых в отчёте: {data.closed}")
        check(data.total_cost >= 370.5, f"Сумма расходов: {data.total_cost}")
        text = reports.render_report(data, filters)
        check("Отчёт УЦЦП" in text, "Текст отчёта формируется")

        rows = await reports.fetch_requests(session, filters)
        path = excel.export_requests(rows)
        check(os.path.exists(path) and os.path.getsize(path) > 3000,
              f"Excel выгружен: {os.path.basename(path)}")

        # --- Просрочка ---
        print("\n--- Просрочка ---")
        SENT.clear()
        req3 = await make_request(session, manager, brand, outlet, electric, hours=-2)
        await flow.dispatch_request(session, bot, req3, actor=manager)
        await session.commit()
        req3 = await load_request(session, req3.id)
        await flow.mark_overdue(session, bot, req3)
        await session.commit()
        check(req3.is_overdue, "Заявка отмечена как просроченная")
        check(any("просрочена" in t.lower() for _, t in SENT),
              "Уведомления о просрочке отправлены")

        card = render_card(req3)
        check("Просрочена" in card, "В карточке видна отметка просрочки")

    print("\n🎉 Все проверки пройдены.")


if __name__ == "__main__":
    asyncio.run(main())
