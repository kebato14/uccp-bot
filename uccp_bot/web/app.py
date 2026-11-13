"""Панель просмотра в браузере.

Только чтение: никаких действий с заявками здесь нет — назначение,
статусы, согласование и подтверждение выполняются в Telegram, где
работают уведомления и журнал действий.

Область видимости берётся из тех же правил, что и в боте
(`services/scope.py`), поэтому руководитель видит ровно свои объекты.
"""
from __future__ import annotations

import datetime as dt
from typing import List, Optional

from aiohttp import web
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..config import config
from ..db.models import (
    Brand,
    Category,
    CostStatus,
    OrgRequest,
    OrgStatus,
    Outlet,
    Priority,
    Request,
    RequestStatusHistory,
    Status,
    User,
    UserStatus,
)
from ..services import org as org_service
from ..services import reports as reports_service
from ..services import scope
from ..services.cards import REQUEST_LOAD_OPTIONS
from ..services.history import ACTION_TITLES
from ..utils import esc, fmt_dt, fmt_money, parse_date
from . import auth, views

PAGE_SIZE = 25
routes = web.RouteTableDef()


def _maker(request: web.Request) -> async_sessionmaker:
    return request.app["session_maker"]


async def _guard(request: web.Request) -> Optional[User]:
    user = await auth.current_user(request)
    if user is None:
        raise web.HTTPFound("/login")
    return user


# --------------------------------------------------------------------------- #
# Вход
# --------------------------------------------------------------------------- #
@routes.get("/login")
async def login_form(request: web.Request) -> web.Response:
    if await auth.current_user(request) is not None:
        raise web.HTTPFound("/")
    return web.Response(text=views.login_page(), content_type="text/html")


@routes.post("/login")
async def login_submit(request: web.Request) -> web.Response:
    data = await request.post()
    code = str(data.get("code", ""))
    ip = request.headers.get("X-Forwarded-For", request.remote or "?").split(",")[0]
    user_id = auth.check_code(code, ip)
    if user_id is None:
        return web.Response(
            text=views.login_page("Код неверный или истёк. Запросите новый: /panel"),
            content_type="text/html",
            status=401,
        )
    response = web.HTTPFound("/")
    auth.set_cookie(response, user_id)
    return response


@routes.get("/logout")
async def logout(request: web.Request) -> web.Response:
    response = web.HTTPFound("/login")
    auth.drop_cookie(response)
    return response


# --------------------------------------------------------------------------- #
# Сводка
# --------------------------------------------------------------------------- #
@routes.get("/")
async def dashboard(request: web.Request) -> web.Response:
    user = await _guard(request)
    async with _maker(request)() as session:
        condition = await scope.request_condition(session, user)

        async def count(*extra):
            stmt = select(func.count(Request.id))
            if condition is not None:
                stmt = stmt.where(condition)
            for clause in extra:
                stmt = stmt.where(clause)
            return int(await session.scalar(stmt) or 0)

        total = await count()
        open_count = await count(Request.status.in_(Status.OPEN))
        overdue = await count(Request.is_overdue.is_(True), Request.status.in_(Status.OPEN))
        closed = await count(Request.status.in_((Status.CLOSED, Status.CONFIRMED)))
        awaiting = await count(Request.status == Status.AWAITING_ASSIGNMENT)
        cost_pending = await count(Request.cost_status == CostStatus.PENDING)

        stmt = select(Request).options(*REQUEST_LOAD_OPTIONS)
        if condition is not None:
            stmt = stmt.where(condition)
        latest = list(
            (await session.scalars(stmt.order_by(Request.created_at.desc()).limit(10))).all()
        )

        org_condition = await scope.org_condition(session, user)
        org_stmt = select(func.count(OrgRequest.id)).where(
            OrgRequest.status.in_(OrgStatus.OPEN)
        )
        if org_condition is not None:
            org_stmt = org_stmt.where(org_condition)
        org_open = int(await session.scalar(org_stmt) or 0)

    cards = "".join([
        views.stat_card(total, "всего заявок"),
        views.stat_card(open_count, "в работе"),
        views.stat_card(overdue, "просрочено", "warn" if overdue else ""),
        views.stat_card(closed, "закрыто", "ok"),
        views.stat_card(awaiting, "ждут назначения", "warn" if awaiting else ""),
        views.stat_card(cost_pending, "стоимость на согласовании"),
        views.stat_card(org_open, "обращений в УЦЦП"),
    ])
    body = (
        f"<h1>Сводка</h1><div class=cards>{cards}</div>"
        "<h2>Последние заявки</h2>"
        + views.table(
            ["Номер", "Создана", "Точка", "Категория", "Описание", "Исполнитель",
             "Приоритет", "Статус", "Срок", "Стоимость"],
            [views.request_row(r) for r in latest],
        )
        + '<p class="note">Панель работает только на просмотр. Назначение, '
        "статусы, согласование стоимости и подтверждение выполняются в Telegram.</p>"
    )
    return web.Response(
        text=views.page("Сводка", body, user, "home"), content_type="text/html"
    )


# --------------------------------------------------------------------------- #
# Заявки
# --------------------------------------------------------------------------- #
@routes.get("/requests")
async def requests_list(request: web.Request) -> web.Response:
    user = await _guard(request)
    q = request.query
    page_no = max(0, int(q.get("page", 0) or 0))

    async with _maker(request)() as session:
        condition = await scope.request_condition(session, user)
        stmt = select(Request).options(*REQUEST_LOAD_OPTIONS)
        if condition is not None:
            stmt = stmt.where(condition)

        if q.get("brand"):
            stmt = stmt.where(Request.brand_id == int(q["brand"]))
        if q.get("outlet"):
            stmt = stmt.where(Request.outlet_id == int(q["outlet"]))
        if q.get("category"):
            stmt = stmt.where(Request.category_id == int(q["category"]))
        if q.get("executor"):
            stmt = stmt.where(Request.executor_id == int(q["executor"]))
        if q.get("status"):
            stmt = stmt.where(Request.status == q["status"])
        if q.get("priority"):
            stmt = stmt.where(Request.priority == q["priority"])
        if q.get("overdue") == "1":
            stmt = stmt.where(Request.is_overdue.is_(True))
        if q.get("search"):
            like = f"%{q['search'].strip()}%"
            stmt = stmt.where(
                Request.description.ilike(like) | Request.number.ilike(like)
            )
        date_from = parse_date(q.get("from", ""))
        date_to = parse_date(q.get("to", ""))
        if date_from:
            stmt = stmt.where(Request.created_at >= dt.datetime.combine(date_from, dt.time.min))
        if date_to:
            stmt = stmt.where(Request.created_at <= dt.datetime.combine(date_to, dt.time.max))

        rows = list((await session.scalars(
            stmt.order_by(Request.created_at.desc())
            .limit(PAGE_SIZE + 1).offset(page_no * PAGE_SIZE)
        )).all())
        has_next = len(rows) > PAGE_SIZE
        rows = rows[:PAGE_SIZE]

        brands = list((await session.scalars(select(Brand).order_by(Brand.sort_order))).all())
        outlets = list((await session.scalars(select(Outlet).order_by(Outlet.name))).all())
        categories = list((await session.scalars(
            select(Category).order_by(Category.sort_order))).all())
        executors = list((await session.scalars(
            select(User).where(User.role_code == "executor").order_by(User.full_name))).all())

    filters = (
        '<form class="filters" method="get">'
        + views.select_field("brand", "Бренд",
                             [("", "все")] + [(str(b.id), b.name) for b in brands],
                             q.get("brand", ""))
        + views.select_field("outlet", "Точка",
                             [("", "все")] + [(str(o.id), o.name) for o in outlets],
                             q.get("outlet", ""))
        + views.select_field("category", "Категория",
                             [("", "все")] + [(str(c.id), c.name) for c in categories],
                             q.get("category", ""))
        + views.select_field("executor", "Исполнитель",
                             [("", "все")] + [(str(e.id), e.full_name) for e in executors],
                             q.get("executor", ""))
        + views.select_field("status", "Статус",
                             [("", "все")] + [(k, v) for k, v in Status.TITLES.items()],
                             q.get("status", ""))
        + views.select_field("priority", "Приоритет",
                             [("", "все")] + [(k, Priority.TITLES[k]) for k in Priority.ORDER],
                             q.get("priority", ""))
        + f'<div><label>С даты</label><input name="from" placeholder="дд.мм.гггг" '
          f'value="{esc(q.get("from", ""))}"></div>'
        + f'<div><label>По дату</label><input name="to" placeholder="дд.мм.гггг" '
          f'value="{esc(q.get("to", ""))}"></div>'
        + f'<div><label>Поиск</label><input name="search" placeholder="номер или текст" '
          f'value="{esc(q.get("search", ""))}"></div>'
        + '<div><label>Только просроченные</label>'
          f'<select name="overdue"><option value="">нет</option>'
          f'<option value="1"{" selected" if q.get("overdue") == "1" else ""}>да</option>'
          "</select></div>"
        + "<button type=submit>Показать</button>"
        + '<a class="tag" href="/requests">сбросить</a>'
        + "</form>"
    )

    pager = ""
    if page_no or has_next:
        base = "&".join(f"{k}={esc(v)}" for k, v in q.items() if k != "page")
        prev_link = (f'<a class="tag" href="/requests?{base}&page={page_no - 1}">← назад</a>'
                     if page_no else "")
        next_link = (f'<a class="tag" href="/requests?{base}&page={page_no + 1}">вперёд →</a>'
                     if has_next else "")
        pager = f'<div class="pager">{prev_link}{next_link}' \
                f'<span class="muted">страница {page_no + 1}</span></div>'

    body = (
        f"<h1>Заявки</h1>{filters}"
        + views.table(
            ["Номер", "Создана", "Точка", "Категория", "Описание", "Исполнитель",
             "Приоритет", "Статус", "Срок", "Стоимость"],
            [views.request_row(r) for r in rows],
        )
        + pager
    )
    return web.Response(
        text=views.page("Заявки", body, user, "requests"), content_type="text/html"
    )


@routes.get("/requests/{rid}")
async def request_card(request: web.Request) -> web.Response:
    user = await _guard(request)
    rid = int(request.match_info["rid"])
    async with _maker(request)() as session:
        req = await session.scalar(
            select(Request).where(Request.id == rid).options(*REQUEST_LOAD_OPTIONS)
        )
        if req is None or not await scope.can_view_request(session, user, req):
            raise web.HTTPNotFound(text="Заявка не найдена или недоступна")
        events = list((await session.scalars(
            select(RequestStatusHistory)
            .where(RequestStatusHistory.request_id == rid)
            .order_by(RequestStatusHistory.created_at)
        )).all())
        authors = {
            u.id: u.full_name
            for u in (await session.scalars(select(User))).all()
        }

    cost_block = "—"
    if req.cost_exempt:
        cost_block = "входит в фиксированную месячную оплату мастера"
    elif req.work_cost is not None or req.material_cost is not None:
        cost_block = (
            f"работы {esc(fmt_money(req.work_cost))}, "
            f"материалы {esc(fmt_money(req.material_cost))}, "
            f"итого <b>{esc(fmt_money(req.total_cost))}</b> — "
            f"{esc(req.cost_status_title)}"
        )

    pairs = [
        ("Статус", views.tag(Status.title(req.status), views.STATUS_TAG.get(req.status, ""))
         + (' <span class="tag red">просрочена</span>' if req.is_overdue else "")),
        ("Бренд", esc(req.brand.name if req.brand else "—")),
        ("Точка", esc(req.outlet.name if req.outlet else "—")),
        ("Категория", esc(req.category.label if req.category else "—")),
        ("Описание", esc(req.description)),
        ("Приоритет", views.tag(Priority.title(req.priority),
                                views.PRIORITY_TAG.get(req.priority, ""))),
        ("Инициатор", esc(req.author.full_name if req.author else "—")),
        ("Исполнитель", esc(req.executor.full_name if req.executor else "не назначен")),
        ("Создана", esc(fmt_dt(req.created_at))),
        ("Срок", esc(fmt_dt(req.due_at))),
        ("Выполнена", esc(fmt_dt(req.done_at)) if req.done_at else ""),
        ("Закрыта", esc(fmt_dt(req.closed_at)) if req.closed_at else ""),
        ("Стоимость", cost_block),
        ("Комментарий исполнителя", esc(req.executor_comment or "")),
        ("Комментарий менеджера", esc(req.manager_comment or "")),
        ("Причина возврата", esc(req.return_reason or "")),
        ("Фото и файлы", f"{len(req.attachments)} шт. — смотреть в Telegram"
         if req.attachments else "нет"),
    ]

    timeline = "".join(
        f'<div class="ev"><b>{esc(fmt_dt(e.created_at))}</b> — '
        f"{esc(authors.get(e.user_id, 'Система'))}: "
        f"{esc(ACTION_TITLES.get(e.action, e.action))}"
        + (f"<br><span class=muted>{esc(e.details)}</span>" if e.details else "")
        + "</div>"
        for e in events
    ) or '<div class="muted">Записей нет</div>'

    body = (
        f'<h1>Заявка {esc(req.number)}</h1>'
        f'<p><a href="/requests">← ко всем заявкам</a></p>'
        + views.definition_list(pairs)
        + "<h2>История действий</h2>"
        + f'<div class="timeline">{timeline}</div>'
    )
    return web.Response(
        text=views.page(req.number, body, user, "requests"), content_type="text/html"
    )


# --------------------------------------------------------------------------- #
# Заявки в УЦЦП
# --------------------------------------------------------------------------- #
@routes.get("/org")
async def org_list(request: web.Request) -> web.Response:
    user = await _guard(request)
    q = request.query
    page_no = max(0, int(q.get("page", 0) or 0))

    async with _maker(request)() as session:
        condition = await scope.org_condition(session, user)
        stmt = select(OrgRequest).options(*org_service.ORG_LOAD_OPTIONS)
        if condition is not None:
            stmt = stmt.where(condition)
        if q.get("status"):
            stmt = stmt.where(OrgRequest.status == q["status"])
        if q.get("type"):
            stmt = stmt.where(OrgRequest.request_type == q["type"])
        rows = list((await session.scalars(
            stmt.order_by(OrgRequest.created_at.desc())
            .limit(PAGE_SIZE + 1).offset(page_no * PAGE_SIZE)
        )).all())
        has_next = len(rows) > PAGE_SIZE
        rows = rows[:PAGE_SIZE]

    from ..db.models import OrgType

    filters = (
        '<form class="filters" method="get">'
        + views.select_field("type", "Тип обращения",
                             [("", "все")] + [(k, v) for k, v in OrgType.TITLES.items()],
                             q.get("type", ""))
        + views.select_field("status", "Статус",
                             [("", "все")] + [(k, v) for k, v in OrgStatus.TITLES.items()],
                             q.get("status", ""))
        + "<button type=submit>Показать</button>"
        + '<a class="tag" href="/org">сбросить</a></form>'
    )
    table_rows = [
        [
            f'<a href="/org/{r.id}"><b>{esc(r.number)}</b></a>',
            esc(fmt_dt(r.created_at)),
            esc(r.object_name),
            esc(r.type_title),
            esc((r.description or "")[:60]),
            esc(r.author.full_name if r.author else "—"),
            esc(r.assignee.full_name if r.assignee else "не назначен"),
            views.tag(OrgStatus.title(r.status), views.STATUS_TAG.get(r.status, "")),
            esc(fmt_dt(r.due_at)),
        ]
        for r in rows
    ]
    pager = ""
    if page_no or has_next:
        base = "&".join(f"{k}={esc(v)}" for k, v in q.items() if k != "page")
        pager = '<div class="pager">' + (
            f'<a class="tag" href="/org?{base}&page={page_no - 1}">← назад</a>' if page_no else ""
        ) + (
            f'<a class="tag" href="/org?{base}&page={page_no + 1}">вперёд →</a>' if has_next else ""
        ) + "</div>"

    body = (
        "<h1>Заявки в УЦЦП</h1>"
        '<p class="muted">Обращения торговых точек в центр — отдельно от '
        "технических заявок мастерам.</p>" + filters
        + views.table(
            ["Номер", "Создано", "Объект", "Тип", "Запрос", "Автор",
             "Ответственный", "Статус", "Срок"],
            table_rows,
        )
        + pager
    )
    return web.Response(text=views.page("Заявки в УЦЦП", body, user, "org"),
                        content_type="text/html")


@routes.get("/org/{rid}")
async def org_card(request: web.Request) -> web.Response:
    user = await _guard(request)
    rid = int(request.match_info["rid"])
    async with _maker(request)() as session:
        req = await org_service.load(session, rid)
        if req is None or not org_service.can_view(user, req):
            raise web.HTTPNotFound(text="Обращение не найдено или недоступно")
        text_card = await org_service.render_history(session, rid)

    pairs = [
        ("Статус", views.tag(OrgStatus.title(req.status),
                             views.STATUS_TAG.get(req.status, ""))),
        ("Тип", esc(req.type_title)),
        ("Бренд", esc(req.brand.name if req.brand else "—")),
        ("Объект", esc(req.object_name)),
        ("Автор", esc(req.author.full_name if req.author else "—")),
        ("Должность", esc(req.author_position or "—")),
        ("Запрос", esc(req.description)),
        ("Количество", esc(req.quantity or "")),
        ("Обоснование", esc(req.reason or "")),
        ("Срок", esc(fmt_dt(req.due_at))),
        ("Принял в УЦЦП", esc(req.accepted_by.full_name) if req.accepted_by else ""),
        ("Ответственный", esc(req.assignee.full_name) if req.assignee else "не назначен"),
        ("Запрос уточнения", esc(req.clarify_question or "")),
        ("Ответ автора", esc(req.clarify_answer or "")),
        ("Результат", esc(req.result_comment or "")),
        ("Файлы", f"{len(req.attachments)} шт. — смотреть в Telegram"
         if req.attachments else "нет"),
    ]
    body = (
        f"<h1>Обращение {esc(req.number)}</h1>"
        f'<p><a href="/org">← ко всем обращениям</a></p>'
        + views.definition_list(pairs)
        + "<h2>История действий</h2>"
        + '<div class="timeline">'
        + text_card.replace("🕓 <b>История изменений</b>", "").replace("\n", "<br>")
        + "</div>"
    )
    return web.Response(text=views.page(req.number, body, user, "org"),
                        content_type="text/html")


# --------------------------------------------------------------------------- #
# Отчёты
# --------------------------------------------------------------------------- #
@routes.get("/reports")
async def reports_page(request: web.Request) -> web.Response:
    user = await _guard(request)
    if not user.can_see_reports:
        raise web.HTTPForbidden(text="Отчёты вам недоступны")

    q = request.query
    period = q.get("period", "month")
    date_from, date_to, title = reports_service.period_range(period)
    filters = reports_service.ReportFilters(
        date_from=date_from, date_to=date_to, period_title=title
    )
    # та же область видимости, что и в боте
    if not (user.is_admin or user.is_ops_chief or user.role_code == "uccp_staff"):
        if user.is_ops_director:
            filters.brand_id = user.brand_id
        elif user.is_outlet_admin:
            filters.outlet_id = user.outlet_id

    async with _maker(request)() as session:
        data = await reports_service.build_report(session, filters)
        org_data = await reports_service.build_org_report(session, filters)

    cards = "".join([
        views.stat_card(data.total, "заявок за период"),
        views.stat_card(data.closed, "закрыто", "ok"),
        views.stat_card(data.in_work, "в работе"),
        views.stat_card(data.overdue, "просрочено", "warn" if data.overdue else ""),
        views.stat_card(esc(fmt_money(data.total_cost)), "расходы"),
        views.stat_card(org_data.total, "обращений в УЦЦП"),
    ])

    def breakdown(title_text: str, store) -> str:
        rows = [
            [esc(name), str(count), esc(fmt_money(cost)) if cost else "—"]
            for name, (count, cost) in sorted(
                store.items(), key=lambda kv: (-kv[1][0], kv[0])
            )[:15]
        ]
        return f"<h2>{esc(title_text)}</h2>" + views.table(
            ["Наименование", "Заявок", "Расходы"], rows
        )

    eff_rows = [
        [
            esc(s.name), str(s.total), str(s.closed),
            f"{s.on_time_share}%" if (s.on_time + s.overdue) else "—",
            str(s.overdue),
            f"{s.avg_hours:.1f} ч" if s.avg_hours else "—",
            esc(fmt_money(s.cost)) if s.cost else "—",
        ]
        for s in sorted(
            (v for k, v in data.executors.items() if k != "не назначен"),
            key=lambda s: -s.total,
        )
    ]

    periods = [("today", "Сегодня"), ("yesterday", "Вчера"), ("week", "Неделя"),
               ("month", "Месяц"), ("all", "Всё время")]
    switch = " ".join(
        f'<a class="tag {"blue" if code == period else ""}" '
        f'href="/reports?period={code}">{esc(label)}</a>'
        for code, label in periods
    )

    body = (
        f"<h1>Отчёт {esc(title)}</h1><p>{switch}</p>"
        f"<div class=cards>{cards}</div>"
        + "<h2>Эффективность исполнителей</h2>"
        + views.table(
            ["Исполнитель", "Заявок", "Закрыто", "В срок", "Просрочено",
             "Среднее время", "Расходы"],
            eff_rows,
        )
        + breakdown("По брендам", data.by_brand)
        + breakdown("По точкам", data.by_outlet)
        + breakdown("По категориям", data.by_category)
        + '<p class="note">Полная выгрузка в Excel и Google Sheets — '
        "в боте: 📊 Отчёты.</p>"
    )
    return web.Response(text=views.page("Отчёты", body, user, "reports"),
                        content_type="text/html")


# --------------------------------------------------------------------------- #
# Пользователи (только главный администратор)
# --------------------------------------------------------------------------- #
@routes.get("/users")
async def users_page(request: web.Request) -> web.Response:
    user = await _guard(request)
    if not user.can_manage_users:
        raise web.HTTPForbidden(text="Раздел доступен только главному администратору")
    async with _maker(request)() as session:
        rows = list((await session.scalars(select(User).order_by(User.full_name))).all())
        brands = {b.id: b.name for b in (await session.scalars(select(Brand))).all()}
        outlets = {o.id: o.name for o in (await session.scalars(select(Outlet))).all()}

    status_kind = {UserStatus.ACTIVE: "green", UserStatus.PENDING: "amber",
                   UserStatus.BLOCKED: "red", UserStatus.DISABLED: "red",
                   UserStatus.REJECTED: "red"}
    table_rows = [
        [
            esc(u.full_name),
            esc(u.role_title),
            views.tag(UserStatus.TITLES.get(u.status, u.status),
                      status_kind.get(u.status, "")),
            esc(u.position_text or "—"),
            esc(brands.get(u.brand_id, "—")),
            esc(outlets.get(u.outlet_id, "—")),
            esc(u.phone or "—"),
            "да" if u.tg_id else "нет",
        ]
        for u in rows
    ]
    body = (
        "<h1>Пользователи</h1>"
        '<p class="muted">Изменение ролей и прав — в боте: '
        "⚙️ Администрирование → 👥 Пользователи.</p>"
        + views.table(
            ["Ф.И.О.", "Роль", "Статус", "Должность", "Бренд", "Точка",
             "Телефон", "Бот активирован"],
            table_rows,
        )
    )
    return web.Response(text=views.page("Пользователи", body, user, "users"),
                        content_type="text/html")


@routes.get("/healthz")
async def healthz(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


def build_app(session_maker: async_sessionmaker) -> web.Application:
    app = web.Application()
    app["session_maker"] = session_maker
    app.add_routes(routes)
    return app


async def start_panel(session_maker: async_sessionmaker) -> Optional[web.AppRunner]:
    """Поднимает панель рядом с ботом в том же процессе."""
    if not config.web_enabled:
        return None
    app = build_app(session_maker)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, config.web_host, config.web_port)
    await site.start()
    return runner
