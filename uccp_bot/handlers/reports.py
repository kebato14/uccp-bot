"""Отчёты УЦЦП и выгрузка в Excel (п.34-36)."""
from __future__ import annotations

import datetime as dt
from typing import Any, Dict, Optional

from aiogram import F, Router, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import FSInputFile, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import texts
from ..callbacks import ReportCB
from ..db.models import Brand, Category, Outlet, Priority, RoleCode, Status, User
from ..keyboards.common import BTN_REPORTS, cancel_kb, main_menu
from ..config import config
from ..services import excel, gsheets, reports
from ..states import ReportFlow
from ..utils import esc, now_local, parse_date

router = Router(name="reports")


def _periods_kb() -> types.InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for code, label in (
        ("today", "Сегодня"),
        ("yesterday", "Вчера"),
        ("week", "Неделя"),
        ("month", "Месяц"),
        ("all", "За всё время"),
    ):
        kb.button(text=label, callback_data=ReportCB(act="period", value=code).pack())
    kb.adjust(2)
    kb.row(
        InlineKeyboardButton(
            text="📆 Выбрать период", callback_data=ReportCB(act="custom").pack()
        )
    )
    kb.row(
        InlineKeyboardButton(
            text="📤 Месячный отчёт в Google Sheets",
            callback_data=ReportCB(act="gsheets").pack(),
        )
    )
    return kb.as_markup()


def _report_kb(has_rows: bool) -> types.InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🔎 Фильтры", callback_data=ReportCB(act="filters").pack())
    if has_rows:
        kb.button(text="📥 Скачать Excel", callback_data=ReportCB(act="excel").pack())
    kb.adjust(2)
    kb.row(
        InlineKeyboardButton(text="⬅️ Периоды", callback_data=ReportCB(act="back").pack()),
        InlineKeyboardButton(text="🗂 Другой модуль", callback_data=ReportCB(act="modules").pack()),
    )
    return kb.as_markup()


def _filters_kb(user: Optional[User] = None) -> types.InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    options = [
        ("f_brand", "Бренд"),
        ("f_outlet", "Точка"),
        ("f_category", "Категория"),
        ("f_executor", "Исполнитель"),
        ("f_status", "Статус"),
        ("f_priority", "Приоритет"),
    ]
    if user is not None and not user.is_admin:
        # область уже задана ролью — эти фильтры не нужны
        if user.is_outlet_admin:
            options = [o for o in options if o[0] not in ("f_brand", "f_outlet")]
        elif user.is_ops_director:
            options = [o for o in options if o[0] != "f_brand"]
    for act, label in options:
        kb.button(text=label, callback_data=ReportCB(act=act).pack())
    kb.adjust(2)
    kb.row(
        InlineKeyboardButton(
            text="🔴 Только просроченные", callback_data=ReportCB(act="f_overdue").pack()
        )
    )
    kb.row(
        InlineKeyboardButton(text="♻️ Сбросить фильтры", callback_data=ReportCB(act="reset").pack()),
        InlineKeyboardButton(text="⬅️ К отчёту", callback_data=ReportCB(act="show").pack()),
    )
    return kb.as_markup()


def _filters_from_state(data: Dict[str, Any]) -> reports.ReportFilters:
    raw = data.get("report_filters", {})
    return reports.ReportFilters(
        date_from=dt.date.fromisoformat(raw["date_from"]) if raw.get("date_from") else None,
        date_to=dt.date.fromisoformat(raw["date_to"]) if raw.get("date_to") else None,
        brand_id=raw.get("brand_id"),
        outlet_id=raw.get("outlet_id"),
        category_id=raw.get("category_id"),
        executor_id=raw.get("executor_id"),
        status=raw.get("status"),
        priority=raw.get("priority"),
        only_overdue=bool(raw.get("only_overdue")),
        period_title=raw.get("period_title", "за всё время"),
    )


def _apply_scope(filters: reports.ReportFilters, user: Optional[User]) -> reports.ReportFilters:
    """Отчёт всегда ограничен областью доступа роли."""
    if user is None or user.is_admin or user.role_code == RoleCode.UCCP_STAFF:
        return filters
    if user.is_ops_director:
        filters.brand_id = user.brand_id
        filters.outlet_id = None
    elif user.is_outlet_admin:
        filters.outlet_id = user.outlet_id
        filters.brand_id = user.brand_id
    return filters


async def _save_filter(state: FSMContext, **kwargs) -> None:
    data = await state.get_data()
    raw = dict(data.get("report_filters", {}))
    raw.update(kwargs)
    await state.update_data(report_filters=raw)


def _modules_kb(user: Optional[User]) -> types.InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(
        text="🔧 Технические и ремонтные",
        callback_data=ReportCB(act="module", value="tech").pack(),
    )
    kb.button(
        text="🗂 Организационные УЦЦП",
        callback_data=ReportCB(act="module", value="org").pack(),
    )
    if user is not None and user.is_admin:
        kb.button(
            text="📋 Сводный отчёт по обоим модулям",
            callback_data=ReportCB(act="module", value="all").pack(),
        )
    kb.adjust(1)
    return kb.as_markup()


MODULE_TITLES = {
    "tech": "🔧 Технические и ремонтные заявки",
    "org": "🗂 Организационные заявки УЦЦП",
    "all": "📋 Сводный отчёт",
}


@router.message(F.text == BTN_REPORTS)
async def open_reports(
    message: types.Message, state: FSMContext, user: Optional[User]
) -> None:
    if user is None or not user.can_see_reports:
        await message.answer(texts.NO_ACCESS)
        return
    await state.clear()
    await message.answer(
        "📊 <b>Отчёты УЦЦП</b>\n\n"
        "Статистика двух модулей ведётся раздельно. Выберите, по чему отчёт:",
        reply_markup=_modules_kb(user),
    )


@router.callback_query(ReportCB.filter(F.act == "module"))
async def pick_module(
    call: types.CallbackQuery, callback_data: ReportCB, state: FSMContext
) -> None:
    await state.update_data(report_module=callback_data.value, report_filters={})
    await call.answer()
    await call.message.edit_text(
        f"📊 <b>{MODULE_TITLES[callback_data.value]}</b>\n\nВыберите период:",
        reply_markup=_periods_kb(),
    )


@router.callback_query(ReportCB.filter(F.act == "modules"))
async def back_to_modules(
    call: types.CallbackQuery, state: FSMContext, user: Optional[User]
) -> None:
    await state.update_data(report_filters={})
    await call.answer()
    await call.message.edit_text(
        "📊 <b>Отчёты УЦЦП</b>\n\nВыберите, по чему отчёт:",
        reply_markup=_modules_kb(user),
    )


@router.callback_query(ReportCB.filter(F.act == "back"))
async def back_to_periods(call: types.CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    module = data.get("report_module", "tech")
    await state.update_data(report_filters={})
    await call.answer()
    await call.message.edit_text(
        f"📊 <b>{MODULE_TITLES.get(module, '')}</b>\n\nВыберите период:",
        reply_markup=_periods_kb(),
    )


@router.callback_query(ReportCB.filter(F.act == "period"))
async def pick_period(
    call: types.CallbackQuery,
    callback_data: ReportCB,
    state: FSMContext,
    session: AsyncSession,
    user: Optional[User],
) -> None:
    date_from, date_to, title = reports.period_range(callback_data.value)
    await _save_filter(
        state,
        date_from=date_from.isoformat() if date_from else None,
        date_to=date_to.isoformat() if date_to else None,
        period_title=title,
    )
    await call.answer()
    await _render(call.message, state, session, user, edit=True)


@router.callback_query(ReportCB.filter(F.act == "custom"))
async def custom_period(call: types.CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ReportFlow.custom_from)
    await call.answer()
    await call.message.answer(
        "Введите <b>дату начала</b> периода в формате ДД.ММ.ГГГГ\nНапример: 01.09.2026",
        reply_markup=cancel_kb(),
    )


@router.message(ReportFlow.custom_from, F.text)
async def custom_from(message: types.Message, state: FSMContext) -> None:
    date = parse_date(message.text)
    if date is None:
        await message.answer("Не понял дату. Формат: ДД.ММ.ГГГГ")
        return
    await _save_filter(state, date_from=date.isoformat())
    await state.set_state(ReportFlow.custom_to)
    await message.answer("Теперь введите <b>дату окончания</b> периода (ДД.ММ.ГГГГ):")


@router.message(ReportFlow.custom_to, F.text)
async def custom_to(
    message: types.Message, state: FSMContext, session: AsyncSession, user: Optional[User]
) -> None:
    date = parse_date(message.text)
    if date is None:
        await message.answer("Не понял дату. Формат: ДД.ММ.ГГГГ")
        return
    data = await state.get_data()
    start = data.get("report_filters", {}).get("date_from")
    if start and dt.date.fromisoformat(start) > date:
        await message.answer("Дата окончания раньше даты начала. Введите корректную дату.")
        return
    await _save_filter(
        state,
        date_to=date.isoformat(),
        period_title=f"с {dt.date.fromisoformat(start).strftime('%d.%m.%Y')} "
        f"по {date.strftime('%d.%m.%Y')}" if start else f"по {date.strftime('%d.%m.%Y')}",
    )
    await state.set_state(None)
    await message.answer("Готовлю отчёт…", reply_markup=main_menu(user))
    await _render(message, state, session, user, edit=False)


async def _render(
    target: types.Message,
    state: FSMContext,
    session: AsyncSession,
    user: Optional[User],
    edit: bool,
) -> None:
    data = await state.get_data()
    module = data.get("report_module", "tech")
    filters = _apply_scope(_filters_from_state(data), user)

    if module == "org":
        report = await reports.build_org_report(session, filters)
        text = reports.render_org_report(report, filters)
        has_rows = report.total > 0
    elif module == "all":
        tech = await reports.build_report(session, filters)
        org_data = await reports.build_org_report(session, filters)
        text = reports.render_combined_report(tech, org_data, filters)
        has_rows = (tech.total + org_data.total) > 0
    else:
        report = await reports.build_report(session, filters)
        text = reports.render_report(report, filters)
        has_rows = report.total > 0
    kb = _report_kb(has_rows)
    if edit:
        await target.edit_text(text, reply_markup=kb)
    else:
        await target.answer(text, reply_markup=kb)


@router.callback_query(ReportCB.filter(F.act == "show"))
async def show_report(
    call: types.CallbackQuery, state: FSMContext, session: AsyncSession, user: Optional[User]
) -> None:
    await call.answer()
    await _render(call.message, state, session, user, edit=True)


@router.callback_query(ReportCB.filter(F.act == "filters"))
async def open_filters(call: types.CallbackQuery, user: Optional[User]) -> None:
    await call.answer()
    await call.message.edit_text(
        "🔎 <b>Фильтры отчёта</b>\nВыберите, что уточнить:", reply_markup=_filters_kb(user)
    )


@router.callback_query(ReportCB.filter(F.act == "reset"))
async def reset_filters(
    call: types.CallbackQuery, state: FSMContext, session: AsyncSession, user: Optional[User]
) -> None:
    data = await state.get_data()
    raw = dict(data.get("report_filters", {}))
    for key in ("brand_id", "outlet_id", "category_id", "executor_id", "status",
                "priority", "only_overdue"):
        raw.pop(key, None)
    await state.update_data(report_filters=raw)
    await call.answer("Фильтры сброшены")
    await _render(call.message, state, session, user, edit=True)


@router.callback_query(ReportCB.filter(F.act == "f_overdue"))
async def filter_overdue(
    call: types.CallbackQuery, state: FSMContext, session: AsyncSession, user: Optional[User]
) -> None:
    data = await state.get_data()
    current = bool(data.get("report_filters", {}).get("only_overdue"))
    await _save_filter(state, only_overdue=not current)
    await call.answer("Фильтр обновлён")
    await _render(call.message, state, session, user, edit=True)


@router.callback_query(ReportCB.filter(F.act.startswith("f_")))
async def filter_pick(
    call: types.CallbackQuery, callback_data: ReportCB, session: AsyncSession
) -> None:
    kind = callback_data.act[2:]
    kb = InlineKeyboardBuilder()

    if kind == "brand":
        rows = (await session.scalars(select(Brand).order_by(Brand.sort_order))).all()
        for row in rows:
            kb.button(text=row.name, callback_data=ReportCB(act="set", value=f"brand:{row.id}").pack())
    elif kind == "outlet":
        rows = (await session.scalars(select(Outlet).order_by(Outlet.name))).all()
        for row in rows:
            kb.button(text=row.name, callback_data=ReportCB(act="set", value=f"outlet:{row.id}").pack())
    elif kind == "category":
        rows = (await session.scalars(select(Category).order_by(Category.sort_order))).all()
        for row in rows:
            kb.button(text=row.label, callback_data=ReportCB(act="set", value=f"category:{row.id}").pack())
    elif kind == "executor":
        rows = (
            await session.scalars(
                select(User).where(User.role_code == RoleCode.EXECUTOR).order_by(User.full_name)
            )
        ).all()
        for row in rows:
            kb.button(
                text=row.full_name,
                callback_data=ReportCB(act="set", value=f"executor:{row.id}").pack(),
            )
    elif kind == "status":
        for code, title in Status.TITLES.items():
            kb.button(text=title, callback_data=ReportCB(act="set", value=f"status:{code}").pack())
    elif kind == "priority":
        for code in Priority.ORDER:
            kb.button(
                text=Priority.TITLES[code],
                callback_data=ReportCB(act="set", value=f"priority:{code}").pack(),
            )
    else:
        await call.answer()
        return

    kb.adjust(1)
    kb.row(
        InlineKeyboardButton(text="⬅️ Назад", callback_data=ReportCB(act="filters").pack())
    )
    await call.answer()
    await call.message.edit_text("Выберите значение фильтра:", reply_markup=kb.as_markup())


@router.callback_query(ReportCB.filter(F.act == "set"))
async def filter_set(
    call: types.CallbackQuery,
    callback_data: ReportCB,
    state: FSMContext,
    session: AsyncSession,
    user: Optional[User],
) -> None:
    kind, _, value = callback_data.value.partition(":")
    field = {
        "brand": "brand_id",
        "outlet": "outlet_id",
        "category": "category_id",
        "executor": "executor_id",
        "status": "status",
        "priority": "priority",
    }[kind]
    parsed: Any = int(value) if field.endswith("_id") else value
    await _save_filter(state, **{field: parsed})
    await call.answer("Фильтр применён")
    await _render(call.message, state, session, user, edit=True)


@router.callback_query(ReportCB.filter(F.act == "excel"))
async def download_excel(
    call: types.CallbackQuery, state: FSMContext, session: AsyncSession, user: Optional[User]
) -> None:
    if user is None or not user.can_see_reports:
        await call.answer(texts.NO_ACCESS, show_alert=True)
        return
    await call.answer("Готовлю файл…")
    data = await state.get_data()
    module = data.get("report_module", "tech")
    filters = _apply_scope(_filters_from_state(data), user)

    if module == "org":
        rows = await reports.fetch_org_requests(session, filters)
        if not rows:
            await call.message.answer("За выбранный период заявок УЦЦП нет.")
            return
        path = excel.export_org_requests(rows, title=filters.describe())
        caption = f"📥 Организационные заявки УЦЦП {filters.describe()} — {len(rows)} шт."
    elif module == "all":
        tech_rows = await reports.fetch_requests(session, filters)
        org_rows = await reports.fetch_org_requests(session, filters)
        if not tech_rows and not org_rows:
            await call.message.answer("За выбранный период заявок нет.")
            return
        path = excel.export_combined(tech_rows, org_rows)
        caption = (
            f"📥 Сводная выгрузка {filters.describe()}\n"
            f"Ремонтные: {len(tech_rows)} · Организационные: {len(org_rows)}\n"
            "Каждый модуль — на своём листе."
        )
    else:
        rows = await reports.fetch_requests(session, filters)
        if not rows:
            await call.message.answer("За выбранный период заявок нет — выгружать нечего.")
            return
        path = excel.export_requests(rows, title=filters.describe())
        caption = f"📥 Выгрузка заявок {filters.describe()} — {len(rows)} шт."

    await call.message.answer_document(FSInputFile(path), caption=caption)


# --------------------------------------------------------------------------- #
# Ежемесячный отчёт в Google Sheets
# --------------------------------------------------------------------------- #
def _gsheets_kb() -> types.InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    prev_year, prev_month = gsheets.previous_month()
    now = now_local()
    kb.button(
        text=f"📄 {gsheets.month_title(prev_year, prev_month)} (прошлый месяц)",
        callback_data=ReportCB(act="gs_run", value=f"{prev_year}-{prev_month}").pack(),
    )
    kb.button(
        text=f"📄 {gsheets.month_title(now.year, now.month)} (текущий)",
        callback_data=ReportCB(act="gs_run", value=f"{now.year}-{now.month}").pack(),
    )
    kb.adjust(1)
    kb.row(
        InlineKeyboardButton(text="⬅️ Назад", callback_data=ReportCB(act="back").pack())
    )
    return kb.as_markup()


@router.callback_query(ReportCB.filter(F.act == "gsheets"))
async def gsheets_menu(call: types.CallbackQuery, user: Optional[User]) -> None:
    if user is None or not user.can_see_reports:
        await call.answer(texts.NO_ACCESS, show_alert=True)
        return
    await call.answer()
    status = (
        "✅ Google Sheets подключён — отчёт за прошедший месяц выгружается "
        f"автоматически {config.monthly_report_day}-го числа."
        if gsheets.credentials_available()
        else "⚠️ Google Sheets пока не подключён: нужен ключ сервисного аккаунта "
        "(см. README, раздел «Google Sheets»). Пока отчёт придёт файлом Excel."
    )
    await call.message.edit_text(
        "📤 <b>Месячный отчёт УЦЦП</b>\n\n"
        "В таблицу попадают все заявки месяца: номер, дата, бренд, точка, категория, "
        "описание, исполнитель, приоритет, срок, статус, даты выполнения и закрытия, "
        "стоимость работ и материалов, отметка просрочки — плюс сводка по брендам, "
        "точкам и исполнителям.\n\n"
        f"{status}\n\nВыберите месяц для выгрузки вручную:",
        reply_markup=_gsheets_kb(),
    )


@router.callback_query(ReportCB.filter(F.act == "gs_run"))
async def gsheets_run(
    call: types.CallbackQuery,
    callback_data: ReportCB,
    session: AsyncSession,
    user: Optional[User],
) -> None:
    if user is None or not user.can_see_reports:
        await call.answer(texts.NO_ACCESS, show_alert=True)
        return
    year, _, month = callback_data.value.partition("-")
    year, month = int(year), int(month)
    label = gsheets.month_title(year, month)

    if not gsheets.credentials_available():
        await call.answer()
        rows = await gsheets.month_requests(session, year, month)
        if not rows:
            await call.message.answer(f"За {label} заявок нет — выгружать нечего.")
            return
        path = excel.export_requests(rows, title=label)
        await call.message.answer_document(
            FSInputFile(path),
            caption=(
                f"📄 Отчёт УЦЦП за {label} — {len(rows)} заявок.\n"
                "Google Sheets не подключён, выгрузка сделана в Excel."
            ),
        )
        return

    await call.answer("Выгружаю в Google Sheets…")
    try:
        result = await gsheets.export_month(session, year, month)
    except Exception as exc:
        await call.message.answer(
            f"❌ Не удалось выгрузить отчёт за {label}.\nПричина: <code>{esc(str(exc)[:300])}</code>"
        )
        return
    await call.message.answer(
        f"✅ Отчёт за <b>{label}</b> выгружен в Google Sheets.\n"
        f"Лист: <b>{esc(result.worksheet)}</b> · заявок: <b>{result.rows}</b> · "
        f"расходы: <b>{result.total_cost}</b> {esc(config.currency)}\n\n"
        f"{esc(result.url)}"
    )


@router.message(Command("gsheets"))
async def cmd_gsheets(
    message: types.Message, state: FSMContext, user: Optional[User]
) -> None:
    if user is None or not user.can_see_reports:
        await message.answer(texts.NO_ACCESS)
        return
    await state.clear()
    await message.answer(
        "📤 <b>Месячный отчёт УЦЦП в Google Sheets</b>\nВыберите месяц:",
        reply_markup=_gsheets_kb(),
    )
