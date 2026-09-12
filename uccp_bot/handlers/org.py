"""Организационные заявки УЦЦП — отдельный модуль.

Структура: заявка → точка/объект → описание задачи → срок → автор →
ответственный → статус. Автор определяется из профиля автоматически.
Данные и статистика не смешиваются с ремонтными заявками.
"""
from __future__ import annotations

import datetime as dt
from typing import List, Optional

from aiogram import Bot, F, Router, types
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import texts
from ..callbacks import CalendarCB, OrgCB
from ..db.models import (
    Brand,
    OrgRequest,
    OrgStatus,
    Outlet,
    Priority,
    User,
    UserStatus,
)
from ..keyboards.calendar import calendar_kb, time_kb
from ..keyboards.common import BTN_CANCEL, BTN_ORG, cancel_kb, main_menu
from ..services import notify, org
from ..services.numbering import new_external_id, next_org_number
from ..states import NewOrgRequest, OrgFlow
from ..utils import esc, fmt_dt, parse_amount, utc_from_local, utcnow

router = Router(name="org")

PAGE_SIZE = 8


# --------------------------------------------------------------------------- #
# Раздел и списки
# --------------------------------------------------------------------------- #
def _root_kb(user: User) -> types.InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if org.can_create(user):
        kb.button(text="➕ Новая заявка УЦЦП", callback_data=OrgCB(act="new").pack())
    kb.button(text="🎯 Назначенные мне", callback_data=OrgCB(act="list", value="assigned").pack())
    kb.button(text="📋 Мои заявки", callback_data=OrgCB(act="list", value="mine").pack())
    kb.button(text="📂 Активные", callback_data=OrgCB(act="list", value="open").pack())
    kb.button(text="🔴 Просроченные", callback_data=OrgCB(act="list", value="overdue").pack())
    kb.button(text="✅ Закрытые", callback_data=OrgCB(act="list", value="closed").pack())
    kb.adjust(1, 2, 2, 1)
    return kb.as_markup()


ROOT_TEXT = (
    "🗂 <b>Организационные заявки УЦЦП</b>\n\n"
    "Раздел для организационных задач по объектам — отдельно от технических "
    "и ремонтных заявок. Статистика двух модулей не смешивается.\n\n"
    "Выберите действие:"
)


@router.message(F.text == BTN_ORG)
async def org_root(
    message: types.Message, state: FSMContext, user: Optional[User]
) -> None:
    if user is None or not user.is_approved:
        await message.answer(texts.NOT_REGISTERED)
        return
    await state.clear()
    await message.answer(ROOT_TEXT, reply_markup=_root_kb(user))


@router.callback_query(OrgCB.filter(F.act == "root"))
async def cb_root(
    call: types.CallbackQuery, state: FSMContext, user: Optional[User]
) -> None:
    await state.clear()
    await call.answer()
    await call.message.edit_text(ROOT_TEXT, reply_markup=_root_kb(user))


LIST_TITLES = {
    "assigned": "🎯 <b>Назначенные мне заявки УЦЦП</b>",
    "mine": "📋 <b>Мои заявки УЦЦП</b>",
    "open": "📂 <b>Активные заявки УЦЦП</b>",
    "overdue": "🔴 <b>Просроченные заявки УЦЦП</b>",
    "closed": "✅ <b>Закрытые заявки УЦЦП</b>",
}
LIST_EMPTY = {
    "assigned": "На вас не назначено ни одной организационной заявки.",
    "mine": "Вы ещё не создавали организационных заявок.",
    "open": "Активных организационных заявок нет.",
    "overdue": "Просроченных организационных заявок нет. 👍",
    "closed": "Закрытых организационных заявок пока нет.",
}


async def _fetch(session: AsyncSession, kind: str, user: User, page: int):
    stmt = select(OrgRequest).options(*org.ORG_LOAD_OPTIONS)
    if kind == "assigned":
        stmt = stmt.where(OrgRequest.assignee_id == user.id)
    elif kind == "mine":
        stmt = stmt.where(OrgRequest.author_id == user.id)
    else:
        condition = org.scope_condition(user)
        if condition is not None:
            stmt = stmt.where(condition)
        if kind == "open":
            stmt = stmt.where(OrgRequest.status.in_(OrgStatus.OPEN))
        elif kind == "overdue":
            stmt = stmt.where(
                OrgRequest.is_overdue.is_(True), OrgRequest.status.in_(OrgStatus.OPEN)
            )
        elif kind == "closed":
            stmt = stmt.where(OrgRequest.status.in_(OrgStatus.FINAL))

    stmt = (
        stmt.order_by(OrgRequest.created_at.desc())
        .limit(PAGE_SIZE + 1)
        .offset(page * PAGE_SIZE)
    )
    rows = list((await session.scalars(stmt)).all())
    return rows[:PAGE_SIZE], len(rows) > PAGE_SIZE


@router.callback_query(OrgCB.filter(F.act == "list"))
async def cb_list(
    call: types.CallbackQuery,
    callback_data: OrgCB,
    session: AsyncSession,
    user: Optional[User],
) -> None:
    if user is None or not user.is_approved:
        await call.answer(texts.NO_ACCESS, show_alert=True)
        return
    kind, page = callback_data.value, callback_data.id
    rows, has_next = await _fetch(session, kind, user, page)
    await call.answer()

    if not rows:
        kb = InlineKeyboardBuilder()
        kb.button(text="⬅️ Назад", callback_data=OrgCB(act="root").pack())
        await call.message.edit_text(
            f"{LIST_TITLES.get(kind, '')}\n\n{LIST_EMPTY.get(kind, 'Ничего не найдено.')}",
            reply_markup=kb.as_markup(),
        )
        return

    lines = [LIST_TITLES.get(kind, "")]
    kb = InlineKeyboardBuilder()
    for req in rows:
        lines.append(
            f"\n<b>{esc(req.number)}</b> · {req.priority_title}\n"
            f"{esc(req.object_name)} · {req.status_title}\n"
            f"Ответственный: "
            f"{esc(req.assignee.full_name) if req.assignee else 'не назначен'} · "
            f"срок {fmt_dt(req.due_at)}"
        )
        kb.button(
            text=f"{req.number} · {req.object_name}"[:60],
            callback_data=OrgCB(act="card", id=req.id).pack(),
        )
    kb.adjust(1)

    nav = []
    if page:
        nav.append(
            InlineKeyboardButton(
                text="⬅️", callback_data=OrgCB(act="list", id=page - 1, value=kind).pack()
            )
        )
    if has_next:
        nav.append(
            InlineKeyboardButton(
                text="➡️", callback_data=OrgCB(act="list", id=page + 1, value=kind).pack()
            )
        )
    if nav:
        kb.row(*nav)
    kb.row(InlineKeyboardButton(text="⬅️ Назад", callback_data=OrgCB(act="root").pack()))
    await call.message.edit_text("\n".join(lines), reply_markup=kb.as_markup())


# --------------------------------------------------------------------------- #
# Карточка и действия
# --------------------------------------------------------------------------- #
def card_kb(request: OrgRequest, user: User) -> types.InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    rid = request.id
    is_assignee = request.assignee_id == user.id

    if request.status == OrgStatus.NEW and is_assignee:
        kb.button(text="▶️ Взять в работу", callback_data=OrgCB(act="start", id=rid).pack())
    if request.status in (OrgStatus.NEW, OrgStatus.IN_PROGRESS) and is_assignee:
        kb.button(text="✅ Выполнено", callback_data=OrgCB(act="done", id=rid).pack())
    if request.status == OrgStatus.DONE and org.can_close(user, request):
        kb.button(text="🔒 Подтвердить и закрыть", callback_data=OrgCB(act="close", id=rid).pack())
        kb.button(text="🔄 Вернуть на доработку", callback_data=OrgCB(act="return", id=rid).pack())
    if request.status in OrgStatus.OPEN:
        kb.button(text="💬 Комментарий", callback_data=OrgCB(act="comment", id=rid).pack())
    if request.status in OrgStatus.OPEN and (user.is_admin or request.author_id == user.id):
        kb.button(text="🎯 Сменить ответственного", callback_data=OrgCB(act="assign", id=rid).pack())
        kb.button(text="🚫 Отменить", callback_data=OrgCB(act="cancel", id=rid).pack())
    kb.adjust(1)
    kb.row(
        InlineKeyboardButton(text="🕓 История", callback_data=OrgCB(act="history", id=rid).pack()),
        InlineKeyboardButton(text="⬅️ Назад", callback_data=OrgCB(act="root").pack()),
    )
    return kb.as_markup()


async def _open_card(
    call: types.CallbackQuery, session: AsyncSession, user: User, request_id: int
) -> Optional[OrgRequest]:
    request = await org.load(session, request_id)
    if request is None or not org.can_view(user, request):
        await call.answer(texts.NO_ACCESS, show_alert=True)
        return None
    return request


@router.callback_query(OrgCB.filter(F.act == "card"))
async def cb_card(
    call: types.CallbackQuery,
    callback_data: OrgCB,
    state: FSMContext,
    session: AsyncSession,
    user: Optional[User],
) -> None:
    await state.clear()
    request = await _open_card(call, session, user, callback_data.id)
    if request is None:
        return
    await call.answer()
    try:
        await call.message.edit_text(
            org.render_card(request), reply_markup=card_kb(request, user)
        )
    except Exception:
        await call.message.answer(
            org.render_card(request), reply_markup=card_kb(request, user)
        )


@router.callback_query(OrgCB.filter(F.act == "history"))
async def cb_history(
    call: types.CallbackQuery,
    callback_data: OrgCB,
    session: AsyncSession,
    user: Optional[User],
) -> None:
    request = await _open_card(call, session, user, callback_data.id)
    if request is None:
        return
    await call.answer()
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ К заявке", callback_data=OrgCB(act="card", id=request.id).pack())
    await call.message.answer(
        f"Заявка <b>{esc(request.number)}</b>\n\n"
        + await org.render_history(session, request.id),
        reply_markup=kb.as_markup(),
    )


@router.callback_query(OrgCB.filter(F.act == "start"))
async def cb_start(
    call: types.CallbackQuery,
    callback_data: OrgCB,
    session: AsyncSession,
    user: Optional[User],
    bot: Bot,
) -> None:
    request = await _open_card(call, session, user, callback_data.id)
    if request is None:
        return
    if request.assignee_id != user.id:
        await call.answer("Заявка назначена другому ответственному.", show_alert=True)
        return
    old = request.status
    request.status = OrgStatus.IN_PROGRESS
    request.started_at = utcnow()
    await org.log(session, request, "started", user=user, old_status=old,
                  new_status=request.status)
    await session.commit()
    await call.answer("Взято в работу")
    await call.message.edit_text(org.render_card(request), reply_markup=card_kb(request, user))
    await notify.send_to_user(
        bot,
        request.author,
        f"🔧 По организационной заявке <b>{esc(request.number)}</b> начата работа.\n"
        f"Ответственный: {esc(user.full_name)}",
    )


@router.callback_query(OrgCB.filter(F.act == "done"))
async def cb_done(
    call: types.CallbackQuery,
    callback_data: OrgCB,
    state: FSMContext,
    session: AsyncSession,
    user: Optional[User],
) -> None:
    request = await _open_card(call, session, user, callback_data.id)
    if request is None:
        return
    if request.assignee_id != user.id:
        await call.answer("Заявка назначена другому ответственному.", show_alert=True)
        return
    await state.set_state(OrgFlow.done_comment)
    await state.update_data(request_id=request.id)
    await call.answer()
    await call.message.answer(
        "Опишите, <b>что сделано</b> по задаче:", reply_markup=cancel_kb()
    )


@router.message(OrgFlow.done_comment, F.text)
async def done_comment(
    message: types.Message, state: FSMContext, user: Optional[User]
) -> None:
    if message.text == BTN_CANCEL:
        await state.clear()
        await message.answer(texts.CANCELLED, reply_markup=main_menu(user))
        return
    if len(message.text.strip()) < 3:
        await message.answer("Опишите результат подробнее.")
        return
    await state.update_data(result=message.text.strip())
    await state.set_state(OrgFlow.cost)
    await message.answer(
        "Укажите <b>затраты</b> по задаче числом.\nЕсли затрат не было — отправьте <code>0</code>."
    )


@router.message(OrgFlow.cost, F.text)
async def done_cost(
    message: types.Message,
    state: FSMContext,
    session: AsyncSession,
    user: Optional[User],
    bot: Bot,
) -> None:
    if message.text == BTN_CANCEL:
        await state.clear()
        await message.answer(texts.CANCELLED, reply_markup=main_menu(user))
        return
    amount = parse_amount(message.text)
    if amount is None:
        await message.answer("Введите сумму числом, например: 150 или 150.50")
        return

    data = await state.get_data()
    request = await org.load(session, data["request_id"])
    old = request.status
    request.result_comment = data.get("result")
    request.cost = amount
    request.done_at = utcnow()
    request.status = OrgStatus.DONE
    await org.log(
        session, request, "done", user=user, old_status=old, new_status=OrgStatus.DONE,
        details=request.result_comment,
    )
    await session.commit()
    await state.clear()

    await message.answer(
        f"✅ Заявка <b>{esc(request.number)}</b> отмечена как выполненная.\n"
        "Автор проверит результат и закроет её.",
        reply_markup=main_menu(user),
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="🔒 Подтвердить и закрыть", callback_data=OrgCB(act="close", id=request.id).pack())
    kb.button(text="🔄 Вернуть на доработку", callback_data=OrgCB(act="return", id=request.id).pack())
    kb.adjust(1)
    await notify.send_to_user(
        bot,
        request.author,
        f"📣 Ответственный отметил организационную заявку <b>{esc(request.number)}</b> "
        "как выполненную. Проверьте результат.\n\n" + org.render_card(request),
        kb.as_markup(),
    )


@router.callback_query(OrgCB.filter(F.act == "close"))
async def cb_close(
    call: types.CallbackQuery,
    callback_data: OrgCB,
    session: AsyncSession,
    user: Optional[User],
    bot: Bot,
) -> None:
    request = await _open_card(call, session, user, callback_data.id)
    if request is None:
        return
    if not org.can_close(user, request):
        await call.answer("Закрыть заявку может автор или администратор.", show_alert=True)
        return
    old = request.status
    request.status = OrgStatus.CLOSED
    request.closed_at = utcnow()
    await org.log(session, request, "closed", user=user, old_status=old,
                  new_status=OrgStatus.CLOSED)
    await session.commit()
    await call.answer("Заявка закрыта")
    await call.message.edit_text(org.render_card(request), reply_markup=card_kb(request, user))
    await notify.send_to_user(
        bot,
        request.assignee,
        f"🔒 Организационная заявка <b>{esc(request.number)}</b> подтверждена и закрыта.",
    )


@router.callback_query(OrgCB.filter(F.act == "return"))
async def cb_return(
    call: types.CallbackQuery,
    callback_data: OrgCB,
    state: FSMContext,
    session: AsyncSession,
    user: Optional[User],
) -> None:
    request = await _open_card(call, session, user, callback_data.id)
    if request is None:
        return
    if not org.can_close(user, request):
        await call.answer(texts.NO_ACCESS, show_alert=True)
        return
    await state.set_state(OrgFlow.return_reason)
    await state.update_data(request_id=request.id)
    await call.answer()
    await call.message.answer(
        "Укажите <b>причину возврата</b> — ответственный получит её сразу:",
        reply_markup=cancel_kb(),
    )


@router.message(OrgFlow.return_reason, F.text)
async def return_reason(
    message: types.Message,
    state: FSMContext,
    session: AsyncSession,
    user: Optional[User],
    bot: Bot,
) -> None:
    if message.text == BTN_CANCEL:
        await state.clear()
        await message.answer(texts.CANCELLED, reply_markup=main_menu(user))
        return
    data = await state.get_data()
    request = await org.load(session, data["request_id"])
    reason = message.text.strip()
    old = request.status
    request.status = OrgStatus.IN_PROGRESS
    request.author_comment = reason
    request.done_at = None
    await org.log(session, request, "returned", user=user, old_status=old,
                  new_status=OrgStatus.IN_PROGRESS, details=reason)
    await session.commit()
    await state.clear()
    await message.answer(
        f"🔄 Заявка <b>{esc(request.number)}</b> возвращена на доработку.",
        reply_markup=main_menu(user),
    )
    await notify.send_to_user(
        bot,
        request.assignee,
        f"🔄 Организационная заявка <b>{esc(request.number)}</b> возвращена на доработку.\n"
        f"Причина: <i>{esc(reason)}</i>",
    )


@router.callback_query(OrgCB.filter(F.act == "comment"))
async def cb_comment(
    call: types.CallbackQuery,
    callback_data: OrgCB,
    state: FSMContext,
    session: AsyncSession,
    user: Optional[User],
) -> None:
    request = await _open_card(call, session, user, callback_data.id)
    if request is None:
        return
    await state.set_state(OrgFlow.comment)
    await state.update_data(request_id=request.id)
    await call.answer()
    await call.message.answer(
        f"Напишите комментарий к заявке <b>{esc(request.number)}</b>:",
        reply_markup=cancel_kb(),
    )


@router.message(OrgFlow.comment, F.text)
async def save_comment(
    message: types.Message,
    state: FSMContext,
    session: AsyncSession,
    user: Optional[User],
    bot: Bot,
) -> None:
    if message.text == BTN_CANCEL:
        await state.clear()
        await message.answer(texts.CANCELLED, reply_markup=main_menu(user))
        return
    data = await state.get_data()
    request = await org.load(session, data["request_id"])
    text = message.text.strip()
    await org.log(session, request, "comment", user=user, details=text)
    await session.commit()
    await state.clear()
    await message.answer("💬 Комментарий сохранён.", reply_markup=main_menu(user))

    note = (
        f"💬 Комментарий к организационной заявке <b>{esc(request.number)}</b>\n"
        f"{esc(user.full_name)}: <i>{esc(text)}</i>"
    )
    for target in (request.author, request.assignee):
        if target and target.id != user.id:
            await notify.send_to_user(bot, target, note)


@router.callback_query(OrgCB.filter(F.act == "cancel"))
async def cb_cancel(
    call: types.CallbackQuery,
    callback_data: OrgCB,
    state: FSMContext,
    session: AsyncSession,
    user: Optional[User],
) -> None:
    request = await _open_card(call, session, user, callback_data.id)
    if request is None:
        return
    if not (user.is_admin or request.author_id == user.id):
        await call.answer(texts.NO_ACCESS, show_alert=True)
        return
    await state.set_state(OrgFlow.cancel_reason)
    await state.update_data(request_id=request.id)
    await call.answer()
    await call.message.answer("Укажите <b>причину отмены</b>:", reply_markup=cancel_kb())


@router.message(OrgFlow.cancel_reason, F.text)
async def cancel_reason(
    message: types.Message,
    state: FSMContext,
    session: AsyncSession,
    user: Optional[User],
    bot: Bot,
) -> None:
    if message.text == BTN_CANCEL:
        await state.clear()
        await message.answer(texts.CANCELLED, reply_markup=main_menu(user))
        return
    data = await state.get_data()
    request = await org.load(session, data["request_id"])
    reason = message.text.strip()
    old = request.status
    request.status = OrgStatus.CANCELLED
    request.closed_at = utcnow()
    await org.log(session, request, "cancelled", user=user, old_status=old,
                  new_status=OrgStatus.CANCELLED, details=reason)
    await session.commit()
    await state.clear()
    await message.answer(
        f"🚫 Заявка <b>{esc(request.number)}</b> отменена.", reply_markup=main_menu(user)
    )
    await notify.send_to_user(
        bot,
        request.assignee,
        f"🚫 Организационная заявка <b>{esc(request.number)}</b> отменена.\n"
        f"Причина: <i>{esc(reason)}</i>",
    )


# --------------------------------------------------------------------------- #
# Создание заявки: объект → описание → приоритет → срок → ответственный
# --------------------------------------------------------------------------- #
@router.callback_query(OrgCB.filter(F.act == "new"))
async def cb_new(
    call: types.CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    user: Optional[User],
) -> None:
    if user is None or not org.can_create(user):
        await call.answer(
            "Организационные заявки создают администраторы точек, "
            "операционный директор и администратор системы.",
            show_alert=True,
        )
        return
    await state.clear()
    await state.set_state(NewOrgRequest.obj)
    await call.answer()
    await _ask_object(call.message, state, session, user, edit=True)


async def _ask_object(
    target: types.Message,
    state: FSMContext,
    session: AsyncSession,
    user: User,
    edit: bool,
) -> None:
    await state.set_state(NewOrgRequest.obj)
    brands = (
        await session.scalars(
            select(Brand).where(Brand.is_active.is_(True)).order_by(Brand.sort_order)
        )
    ).all()
    kb = InlineKeyboardBuilder()
    for brand in brands:
        if user.is_ops_director and user.brand_id and brand.id != user.brand_id:
            continue
        kb.button(text=brand.name, callback_data=OrgCB(act="obj_brand", id=brand.id).pack())
    kb.button(text="🏢 Другой объект (офис, склад, цех)", callback_data=OrgCB(act="obj_other").pack())
    kb.adjust(1)
    kb.row(InlineKeyboardButton(text="❌ Отменить", callback_data=OrgCB(act="root").pack()))
    text = "Шаг 1 из 5. Выберите <b>объект</b> — бренд торговой точки или другой объект:"
    if edit:
        await target.edit_text(text, reply_markup=kb.as_markup())
    else:
        await target.answer(text, reply_markup=kb.as_markup())


@router.callback_query(NewOrgRequest.obj, OrgCB.filter(F.act == "obj_brand"))
async def cb_obj_brand(
    call: types.CallbackQuery,
    callback_data: OrgCB,
    state: FSMContext,
    session: AsyncSession,
    user: Optional[User],
) -> None:
    await state.update_data(brand_id=callback_data.id)
    outlets = (
        await session.scalars(
            select(Outlet)
            .where(Outlet.brand_id == callback_data.id, Outlet.is_active.is_(True))
            .order_by(Outlet.sort_order, Outlet.name)
        )
    ).all()
    kb = InlineKeyboardBuilder()
    for outlet in outlets:
        if user.is_outlet_admin and user.outlet_id and outlet.id != user.outlet_id:
            continue
        kb.button(text=outlet.name, callback_data=OrgCB(act="obj_outlet", id=outlet.id).pack())
    kb.adjust(1)
    kb.row(InlineKeyboardButton(text="❌ Отменить", callback_data=OrgCB(act="root").pack()))
    await state.set_state(NewOrgRequest.obj_outlet)
    await call.answer()
    await call.message.edit_text(
        "Шаг 2 из 5. Выберите <b>торговую точку</b>:", reply_markup=kb.as_markup()
    )


@router.callback_query(NewOrgRequest.obj_outlet, OrgCB.filter(F.act == "obj_outlet"))
async def cb_obj_outlet(
    call: types.CallbackQuery,
    callback_data: OrgCB,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    outlet = await session.get(Outlet, callback_data.id)
    await state.update_data(
        outlet_id=outlet.id, brand_id=outlet.brand_id, object_name=outlet.name
    )
    await state.set_state(NewOrgRequest.description)
    await call.answer()
    await call.message.edit_text(
        "Шаг 3 из 5. Опишите <b>задачу</b>.\n"
        "Например: <i>Подготовить документы по продлению аренды.</i>"
    )


@router.callback_query(NewOrgRequest.obj, OrgCB.filter(F.act == "obj_other"))
async def cb_obj_other(call: types.CallbackQuery, state: FSMContext) -> None:
    await state.set_state(NewOrgRequest.obj_custom)
    await call.answer()
    await call.message.edit_text(
        "Введите <b>название объекта</b>.\n"
        "Например: <i>Офис УЦЦП</i>, <i>Склад</i>, <i>Цех кондитерка</i>."
    )


@router.message(NewOrgRequest.obj_custom, F.text)
async def set_obj_custom(message: types.Message, state: FSMContext) -> None:
    if message.text == BTN_CANCEL:
        await state.clear()
        await message.answer(texts.CANCELLED)
        return
    await state.update_data(object_text=message.text.strip(), object_name=message.text.strip())
    await state.set_state(NewOrgRequest.description)
    await message.answer("Шаг 3 из 5. Опишите <b>задачу</b>:")


@router.message(NewOrgRequest.description, F.text)
async def set_description(message: types.Message, state: FSMContext) -> None:
    if message.text == BTN_CANCEL:
        await state.clear()
        await message.answer(texts.CANCELLED)
        return
    if len(message.text.strip()) < 5:
        await message.answer("Опишите задачу подробнее — минимум 5 символов.")
        return
    await state.update_data(description=message.text.strip())
    await state.set_state(NewOrgRequest.priority)

    kb = InlineKeyboardBuilder()
    for code in Priority.ORDER:
        kb.button(text=Priority.TITLES[code], callback_data=OrgCB(act="prio", value=code).pack())
    kb.adjust(2)
    await message.answer("Шаг 4 из 5. Укажите <b>приоритет</b>:", reply_markup=kb.as_markup())


@router.callback_query(NewOrgRequest.priority, OrgCB.filter(F.act == "prio"))
async def set_priority(
    call: types.CallbackQuery, callback_data: OrgCB, state: FSMContext
) -> None:
    await state.update_data(
        priority=callback_data.value, priority_title=Priority.title(callback_data.value)
    )
    await state.set_state(NewOrgRequest.due_date)
    await call.answer()
    await call.message.edit_text(
        "Шаг 5 из 5. До какого числа нужно выполнить? Выберите <b>дату</b>:",
        reply_markup=calendar_kb(with_cancel=False),
    )


@router.callback_query(NewOrgRequest.due_date, CalendarCB.filter(F.act == "nav"))
async def org_cal_nav(call: types.CallbackQuery, callback_data: CalendarCB) -> None:
    await call.answer()
    await call.message.edit_reply_markup(
        reply_markup=calendar_kb(callback_data.year, callback_data.month, with_cancel=False)
    )


@router.callback_query(NewOrgRequest.due_date, CalendarCB.filter(F.act == "day"))
async def org_cal_day(
    call: types.CallbackQuery, callback_data: CalendarCB, state: FSMContext
) -> None:
    await state.update_data(
        due_year=callback_data.year, due_month=callback_data.month, due_day=callback_data.day
    )
    await state.set_state(NewOrgRequest.due_time)
    await call.answer()
    await call.message.edit_text("Выберите <b>время</b>:", reply_markup=time_kb(with_cancel=False))


@router.callback_query(NewOrgRequest.due_time, CalendarCB.filter(F.act == "time"))
async def org_cal_time(
    call: types.CallbackQuery,
    callback_data: CalendarCB,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    data = await state.get_data()
    due_local = dt.datetime(
        data["due_year"], data["due_month"], data["due_day"],
        callback_data.hour, callback_data.minute,
    )
    await state.update_data(
        due_local=due_local.isoformat(), due_title=due_local.strftime("%d.%m.%Y, %H:%M")
    )
    await call.answer()
    await _ask_assignee(call.message, state, session, page=0, edit=True)


async def _assignees(session: AsyncSession, page: int):
    stmt = (
        select(User)
        .where(User.status == UserStatus.ACTIVE)
        .order_by(User.role_code, User.full_name)
        .limit(PAGE_SIZE + 1)
        .offset(page * PAGE_SIZE)
    )
    rows = list((await session.scalars(stmt)).all())
    return rows[:PAGE_SIZE], len(rows) > PAGE_SIZE


async def _ask_assignee(
    target: types.Message, state: FSMContext, session: AsyncSession, page: int, edit: bool
) -> None:
    await state.set_state(NewOrgRequest.assignee)
    rows, has_next = await _assignees(session, page)
    kb = InlineKeyboardBuilder()
    for person in rows:
        mark = "" if person.tg_id else " ⚠️"
        kb.button(
            text=f"{person.full_name} · {person.role_title}{mark}"[:60],
            callback_data=OrgCB(act="assignee", id=person.id).pack(),
        )
    kb.adjust(1)
    nav = []
    if page:
        nav.append(
            InlineKeyboardButton(
                text="⬅️", callback_data=OrgCB(act="assignee_page", id=page - 1).pack()
            )
        )
    if has_next:
        nav.append(
            InlineKeyboardButton(
                text="➡️", callback_data=OrgCB(act="assignee_page", id=page + 1).pack()
            )
        )
    if nav:
        kb.row(*nav)
    kb.row(InlineKeyboardButton(text="❌ Отменить", callback_data=OrgCB(act="root").pack()))
    text = "Выберите <b>ответственного</b> за задачу:"
    if edit:
        await target.edit_text(text, reply_markup=kb.as_markup())
    else:
        await target.answer(text, reply_markup=kb.as_markup())


@router.callback_query(NewOrgRequest.assignee, OrgCB.filter(F.act == "assignee_page"))
async def assignee_page(
    call: types.CallbackQuery,
    callback_data: OrgCB,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    await call.answer()
    await _ask_assignee(call.message, state, session, callback_data.id, edit=True)


@router.callback_query(NewOrgRequest.assignee, OrgCB.filter(F.act == "assignee"))
async def pick_assignee(
    call: types.CallbackQuery,
    callback_data: OrgCB,
    state: FSMContext,
    session: AsyncSession,
    user: Optional[User],
) -> None:
    assignee = await session.get(User, callback_data.id)
    await state.update_data(assignee_id=assignee.id, assignee_name=assignee.full_name)
    await state.set_state(NewOrgRequest.preview)
    data = await state.get_data()
    await call.answer()

    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Отправить", callback_data=OrgCB(act="submit").pack())
    kb.button(text="❌ Отменить", callback_data=OrgCB(act="root").pack())
    kb.adjust(1)
    await call.message.edit_text(
        "<b>Проверьте организационную заявку</b>\n\n"
        f"🏢 Объект: {esc(data.get('object_name'))}\n"
        f"📝 Задача: {esc(data.get('description'))}\n"
        f"❗️ Приоритет: {esc(data.get('priority_title'))}\n"
        f"📅 Срок: {esc(data.get('due_title'))}\n"
        f"👤 Автор: {esc(user.full_name)}\n"
        f"🎯 Ответственный: {esc(assignee.full_name)}",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(NewOrgRequest.preview, OrgCB.filter(F.act == "submit"))
async def submit(
    call: types.CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    user: Optional[User],
    bot: Bot,
) -> None:
    data = await state.get_data()
    await call.answer("Создаю заявку…")

    request = OrgRequest(
        number=await next_org_number(session),
        external_id=new_external_id(),
        outlet_id=data.get("outlet_id"),
        brand_id=data.get("brand_id"),
        object_text=data.get("object_text"),
        description=data["description"],
        priority=data.get("priority", Priority.MEDIUM),
        due_at=utc_from_local(dt.datetime.fromisoformat(data["due_local"])),
        author_id=user.id,                      # автор — из профиля, автоматически
        assignee_id=data.get("assignee_id"),
        status=OrgStatus.NEW,
    )
    session.add(request)
    await session.flush()
    await org.log(session, request, "created", user=user, new_status=OrgStatus.NEW)
    await org.log(
        session, request, "assigned", user=user,
        details=f"Ответственный: {data.get('assignee_name')}",
    )
    await session.commit()

    request = await org.load(session, request.id)
    await state.clear()

    kb = InlineKeyboardBuilder()
    kb.button(text="▶️ Взять в работу", callback_data=OrgCB(act="start", id=request.id).pack())
    kb.button(text="✅ Выполнено", callback_data=OrgCB(act="done", id=request.id).pack())
    kb.adjust(1)
    sent = await org.notify_assignee(session, bot, request, kb.as_markup())

    suffix = (
        f"\n\nОтветственный уведомлён: <b>{esc(request.assignee.full_name)}</b>."
        if sent
        else "\n\n⚠️ Ответственный ещё не активировал бота — уведомление не доставлено, "
        "администратор об этом знает."
    )
    await call.message.edit_text(
        f"✅ Организационная заявка <b>{esc(request.number)}</b> создана.\n\n"
        + org.render_card(request)
        + suffix
    )
    await call.message.answer(ROOT_TEXT, reply_markup=_root_kb(user))


@router.callback_query(OrgCB.filter(F.act == "assign"))
async def cb_reassign(
    call: types.CallbackQuery,
    callback_data: OrgCB,
    state: FSMContext,
    session: AsyncSession,
    user: Optional[User],
) -> None:
    request = await _open_card(call, session, user, callback_data.id)
    if request is None:
        return
    if not (user.is_admin or request.author_id == user.id):
        await call.answer(texts.NO_ACCESS, show_alert=True)
        return
    rows, _ = await _assignees(session, 0)
    kb = InlineKeyboardBuilder()
    for person in rows:
        kb.button(
            text=f"{person.full_name} · {person.role_title}"[:60],
            callback_data=OrgCB(act="assign_to", id=request.id, value=str(person.id)).pack(),
        )
    kb.adjust(1)
    kb.row(
        InlineKeyboardButton(
            text="⬅️ Назад", callback_data=OrgCB(act="card", id=request.id).pack()
        )
    )
    await call.answer()
    await call.message.edit_text("Выберите нового ответственного:", reply_markup=kb.as_markup())


@router.callback_query(OrgCB.filter(F.act == "assign_to"))
async def cb_assign_to(
    call: types.CallbackQuery,
    callback_data: OrgCB,
    session: AsyncSession,
    user: Optional[User],
    bot: Bot,
) -> None:
    request = await _open_card(call, session, user, callback_data.id)
    if request is None:
        return
    new_assignee = await session.get(User, int(callback_data.value))
    old = request.assignee
    request.assignee_id = new_assignee.id
    await org.log(
        session, request, "reassigned", user=user,
        details=f"Ответственный: {new_assignee.full_name}",
    )
    await session.commit()
    request = await org.load(session, request.id)
    await call.answer("Ответственный изменён")
    await call.message.edit_text(org.render_card(request), reply_markup=card_kb(request, user))

    kb = InlineKeyboardBuilder()
    kb.button(text="▶️ Взять в работу", callback_data=OrgCB(act="start", id=request.id).pack())
    kb.button(text="✅ Выполнено", callback_data=OrgCB(act="done", id=request.id).pack())
    kb.adjust(1)
    await org.notify_assignee(session, bot, request, kb.as_markup())
    if old and old.id != new_assignee.id:
        await notify.send_to_user(
            bot, old,
            f"ℹ️ Организационная заявка <b>{esc(request.number)}</b> передана другому ответственному.",
        )


@router.message(StateFilter(NewOrgRequest, OrgFlow))
async def wrong_input(message: types.Message, state: FSMContext) -> None:
    current = await state.get_state()
    hints = {
        NewOrgRequest.obj.state: "Выберите объект кнопкой выше.",
        NewOrgRequest.obj_outlet.state: "Выберите точку кнопкой выше.",
        NewOrgRequest.obj_custom.state: "Введите название объекта текстом.",
        NewOrgRequest.description.state: "Опишите задачу текстом.",
        NewOrgRequest.priority.state: "Выберите приоритет кнопкой выше.",
        NewOrgRequest.due_date.state: "Выберите дату в календаре выше.",
        NewOrgRequest.due_time.state: "Выберите время кнопкой выше.",
        NewOrgRequest.assignee.state: "Выберите ответственного кнопкой выше.",
        NewOrgRequest.preview.state: "Нажмите «Отправить» или «Отменить».",
    }
    await message.answer("Для продолжения: " + hints.get(current, "используйте кнопки выше."))
