"""Панель просмотра: вход по коду, области видимости, режим только чтения.

Запуск: .venv/bin/python tests/web_check.py
"""
from __future__ import annotations

import asyncio
import datetime as dt
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["BOT_TOKEN"] = "test:token"
os.environ["DB_URL"] = "sqlite+aiosqlite:///" + os.path.join(tempfile.mkdtemp(), "w.db")
os.environ["WEB_SECRET"] = "test-secret"

from aiohttp.test_utils import TestClient, TestServer  # noqa: E402
from sqlalchemy import select  # noqa: E402

from uccp_bot.db.base import SessionMaker, init_db  # noqa: E402
from uccp_bot.db.models import (  # noqa: E402
    Brand, Category, CostStatus, Outlet, Priority, Request, RoleCode, Status,
    User, UserScope, UserStatus,
)
from uccp_bot.db.seed import seed  # noqa: E402
from uccp_bot.services import history  # noqa: E402
from uccp_bot.services.numbering import new_external_id, next_request_number  # noqa: E402
from uccp_bot.utils import utcnow  # noqa: E402
from uccp_bot.web import auth  # noqa: E402
from uccp_bot.web.app import build_app  # noqa: E402


def check(condition: bool, label: str) -> None:
    print(("✅ " if condition else "❌ ") + label)
    if not condition:
        raise AssertionError(label)


async def login(client: TestClient, user_id: int) -> None:
    code = auth.issue_code(user_id)
    resp = await client.post("/login", data={"code": code}, allow_redirects=False)
    assert resp.status in (302, 303), resp.status


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
        master.tg_id = 900
        await session.flush()

        def make(tg, name, role, brand=None, outlet=None, status=UserStatus.ACTIVE):
            u = User(tg_id=tg, full_name=name, last_name=name, role_code=role,
                     brand_id=brand.id if brand else None,
                     outlet_id=outlet.id if outlet else None,
                     status=status, is_registered=True, position_text="должность")
            u.sync_flags()
            session.add(u)
            return u

        admin = make(1, "Главный админ", RoleCode.ADMIN)
        chief = make(2, "Главный опердир", RoleCode.OPS_CHIEF)
        ops_hd = make(3, "Опердир Hotdogger", RoleCode.OPS_DIRECTOR, hd)
        staff = make(4, "Сотрудник точки", RoleCode.STAFF, moose, opera)
        blocked = make(5, "Отключённый", RoleCode.ADMIN, status=UserStatus.BLOCKED)
        await session.flush()

        async def add(outlet, author, cost=None):
            req = Request(
                number=await next_request_number(session), external_id=new_external_id(),
                author_id=author.id, brand_id=outlet.brand_id, outlet_id=outlet.id,
                category_id=electric.id, executor_id=master.id,
                description=f"Заявка по точке {outlet.name}",
                priority=Priority.HIGH, status=Status.CLOSED,
                due_at=utcnow() + dt.timedelta(hours=4),
                done_at=utcnow(), closed_at=utcnow(),
                work_cost=cost, material_cost=0,
                cost_status=CostStatus.APPROVED if cost else CostStatus.NONE,
            )
            session.add(req)
            await session.flush()
            await history.log(session, req, "created", user=author,
                              new_status=Status.NEW)
            return req

        r_moose = await add(opera, staff, 250)
        r_hd = await add(airport, staff, 400)
        await session.commit()
        moose_no, hd_no = r_moose.number, r_hd.number
        ids = {"moose": r_moose.id, "hd": r_hd.id}

    app = build_app(SessionMaker)
    async with TestClient(TestServer(app)) as client:
        print("=== Вход ===")
        resp = await client.get("/", allow_redirects=False)
        check(resp.status == 302, "Без входа панель закрыта (редирект на /login)")

        resp = await client.post("/login", data={"code": "000000"})
        check(resp.status == 401, "Неверный код не пускает")

        code = auth.issue_code(admin.id)
        resp = await client.post("/login", data={"code": code}, allow_redirects=False)
        check(resp.status == 302, "Правильный код пускает")
        resp = await client.post("/login", data={"code": code})
        check(resp.status == 401, "Код одноразовый — повторно не работает")

        print("\n=== Главный администратор видит всё ===")
        text = await (await client.get("/requests")).text()
        check(moose_no in text and hd_no in text, "В списке обе заявки")
        check("Сводка" in text and "Отчёты" in text, "Меню панели на месте")

        card = await (await client.get(f"/requests/{ids['moose']}")).text()
        check(moose_no in card, "Карточка заявки открывается")
        check("История действий" in card and "создал заявку" in card,
              "В карточке есть журнал действий")
        check("250" in card and "согласована" in card,
              "Видны стоимость и статус согласования")

        print("\n=== Только чтение ===")
        for method, path in (("post", "/requests"), ("post", f"/requests/{ids['moose']}"),
                             ("delete", f"/requests/{ids['moose']}"),
                             ("post", "/users")):
            resp = await getattr(client, method)(path)
            check(resp.status in (404, 405),
                  f"{method.upper()} {path} — изменений через панель нет ({resp.status})")

        print("\n=== Область видимости ===")
        await client.get("/logout")
        await login(client, chief.id)
        text = await (await client.get("/requests")).text()
        check(moose_no in text and hd_no in text,
              "Главный операционный директор видит оба бренда")
        resp = await client.get("/users")
        check(resp.status == 403, "Пользователи — только главному администратору")

        await client.get("/logout")
        await login(client, ops_hd.id)
        text = await (await client.get("/requests")).text()
        check(hd_no in text and moose_no not in text,
              "Операционный директор видит только свой бренд")
        resp = await client.get(f"/requests/{ids['moose']}")
        check(resp.status == 404, "Чужая заявка по прямой ссылке недоступна")

        async with SessionMaker() as session:
            session.add(UserScope(user_id=ops_hd.id, outlet_id=opera.id,
                                  brand_id=opera.brand_id, created_by_id=admin.id))
            await session.commit()
        text = await (await client.get("/requests")).text()
        check(moose_no in text, "После закрепления объекта заявка появляется")
        resp = await client.get(f"/requests/{ids['moose']}")
        check(resp.status == 200, "И карточка становится доступна")

        await client.get("/logout")
        await login(client, staff.id)
        resp = await client.get("/reports")
        check(resp.status == 403, "Сотруднику точки отчёты закрыты")
        text = await (await client.get("/requests")).text()
        check(moose_no in text and hd_no in text, "Автор видит свои заявки")

        print("\n=== Отчёты ===")
        await client.get("/logout")
        await login(client, admin.id)
        text = await (await client.get("/reports?period=all")).text()
        check("Эффективность исполнителей" in text, "Блок эффективности есть")
        check("Назрулло" in text, "Исполнитель в таблице эффективности")
        check("По брендам" in text and "По точкам" in text, "Разрезы отчёта на месте")

        print("\n=== Отключённый пользователь ===")
        await client.get("/logout")
        await login(client, blocked.id)
        resp = await client.get("/", allow_redirects=False)
        check(resp.status == 302, "Заблокированного в панель не пускает")

        print("\n=== Служебное ===")
        resp = await client.get("/healthz")
        check(resp.status == 200, "Проверка живости отвечает")

    print("\n🎉 Панель просмотра работает и соблюдает права доступа.")


if __name__ == "__main__":
    asyncio.run(main())
