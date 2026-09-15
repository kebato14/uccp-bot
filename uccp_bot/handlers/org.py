"""«Заявка в УЦЦП» — канал обращения торговой точки в центр.

Точка → УЦЦП → ответственный УЦЦП → результат.

Поток полностью отделён от технических заявок мастерам: здесь не появляются
сантехники и электрики, маршрут только один — в УЦЦП. Автор определяется
по профилю: Ф.И.О., должность, бренд и точку повторно вводить не нужно.

Обработка на стороне УЦЦП:
Новая → Принята УЦЦП → Назначен ответственный → В работе → Выполнена → Закрыта.
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
    OrgAttachment,
    OrgRequest,
    OrgStatus,
    OrgType,
    Outlet,
    User,
)
from ..keyboards.calendar import calendar_kb, time_kb
from ..keyboards.common import BTN_CANCEL, BTN_ORG, cancel_kb, main_menu
from ..services import notify, org
from ..services.numbering import new_external_id, next_org_number
from ..states import NewOrgRequest, OrgFlow
from ..utils import esc, fmt_dt, utc_from_local, utcnow

router = Router(name="org")

PAGE_SIZE = 8
MAX_MEDIA = 10


# --------------------------------------------------------------------------- #
# Раздел
# --------------------------------------------------------------------------- #
def _root_kb(user: User) -> types.InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if org.can_create(user):
        kb.button(text="➕ Новая заявка в УЦЦП", callback_data=OrgCB(act="new").pack())
    if org.is_uccp(user):
        kb.button(text="📥 Новые обращения", callback_data=OrgCB(act="list", value="inbox").pack())
        kb.button(text="🎯 Назначенные мне", callback_data=OrgCB(act="list", value="assigned").pack())
    kb.button(text="📋 Мои обращения", callback_data=OrgCB(act="list", value="mine").pack())
    kb.button(text="📂 В работе", callback_data=OrgCB(act="list", value="open").pack())
    kb.button(text="🔴 Просроченные", callback_data=OrgCB(act="list", value="overdue").pack())
    kb.button(text="✅ Закрытые", callback_data=OrgCB(act="list", value="closed").pack())
    kb.adjust(1, 2, 2, 2)
    return kb.as_markup()


def _root_text(user: User) -> str:
    if org.is_uccp(user):
        return (
            "🗂 <b>Заявки в УЦЦП</b>\n\n"
            "Обращения торговых точек: инвентарь, проблемы и организационные задачи.\n"
            "Вы обрабатываете их как сотрудник УЦЦП."
        )
    if org.can_create(user):
        return (
            "🗂 <b>Заявка в УЦЦП</b>\n\n"
            "Прямой канал обращения в УЦЦП: инвентарь, проблемы в зоне "
            "ответственности УЦЦП и организационные задачи.\n\n"
            "Для технических поломок используйте ➕ Новая заявка — там заявка "
            "уходит профильному мастеру."
        )
    return (
        "🗂 <b>Заявки в УЦЦП</b>\n\n"
        "Здесь видны обращения, где вы автор или ответственный.\n"
        "Направлять обращения в УЦЦП могут управляющие и администраторы точек."
    )


@router.message(F.text == BTN_ORG)
async def org_root(
    message: types.Message, state: FSMContext, user: Optional[User]
) -> None:
    if user is None or not user.is_approved:
        await message.answer(texts.NOT_REGISTERED)
        return
    await state.clear()
    await message.answer(_root_text(user), reply_markup=_root_kb(user))


@router.callback_query(OrgCB.filter(F.act == "root"))
async def cb_root(
    call: types.CallbackQuery, state: FSMContext, user: Optional[User]
) -> None:
    await state.clear()
    await call.answer()
    try:
        await call.message.edit_text(_root_text(user), reply_markup=_root_kb(user))
    except Exception:
        await call.message.answer(_root_text(user), reply_markup=_root_kb(user))


# --------------------------------------------------------------------------- #
# Списки
# --------------------------------------------------------------------------- #
LIST_TITLES = {
    "inbox": "📥 <b>Новые обращения в УЦЦП</b>",
    "assigned": "🎯 <b>Назначенные мне обращения</b>",
    "mine": "📋 <b>Мои обращения в УЦЦП</b>",
    "open": "📂 <b>Обращения в работе</b>",
    "overdue": "🔴 <b>Просроченные обращения</b>",
    "closed": "✅ <b>Закрытые обращения</b>",
}
LIST_EMPTY = {
    "inbox": "Новых обращений нет.",
    "assigned": "На вас не назначено ни одного обращения.",
    "mine": "Вы ещё не направляли обращений в УЦЦП.",
    "open": "Обращений в работе нет.",
    "overdue": "Просроченных обращений нет. 👍",
    "closed": "Закрытых обращений пока нет.",
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
        if kind == "inbox":
            stmt = stmt.where(OrgRequest.status.in_((OrgStatus.NEW, OrgStatus.CLARIFY)))
        elif kind == "open":
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

    kb = InlineKeyboardBuilder()
    if not rows:
        kb.button(text="⬅️ Назад", callback_data=OrgCB(act="root").pack())
        await call.message.edit_text(
            f"{LIST_TITLES.get(kind, '')}\n\n{LIST_EMPTY.get(kind, 'Ничего не найдено.')}",
            reply_markup=kb.as_markup(),
        )
        return

    lines = [LIST_TITLES.get(kind, "")]
    for req in rows:
        lines.append(
            f"\n<b>{esc(req.number)}</b> · {esc(req.type_title)}\n"
            f"{esc(req.object_name)} · {req.status_title}\n"
            f"{esc(req.description[:60])}{'…' if len(req.description) > 60 else ''}\n"
            f"Срок: {fmt_dt(req.due_at)}"
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
# Карточка и действия сторон
# --------------------------------------------------------------------------- #
def card_kb(request: OrgRequest, user: User) -> types.InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    rid = request.id
    uccp = org.is_uccp(user)
    is_author = request.author_id == user.id
    is_assignee = request.assignee_id == user.id

    # --- сторона УЦЦП ---
    if uccp and request.status == OrgStatus.NEW:
        kb.button(text="✅ Принять в УЦЦП", callback_data=OrgCB(act="accept", id=rid).pack())
        kb.button(text="❓ Запросить уточнение", callback_data=OrgCB(act="clarify", id=rid).pack())
        kb.button(text="❌ Отклонить", callback_data=OrgCB(act="reject", id=rid).pack())
    if uccp and request.status in (OrgStatus.ACCEPTED, OrgStatus.ASSIGNED, OrgStatus.IN_PROGRESS):
        kb.button(
            text="🎯 Назначить ответственного" if not request.assignee_id
            else "🎯 Сменить ответственного",
            callback_data=OrgCB(act="assign", id=rid).pack(),
        )
    if uccp and request.status in (OrgStatus.ACCEPTED, OrgStatus.ASSIGNED):
        kb.button(text="❓ Запросить уточнение", callback_data=OrgCB(act="clarify", id=rid).pack())

    # --- ответственный ---
    if is_assignee and request.status in (OrgStatus.ASSIGNED, OrgStatus.ACCEPTED):
        kb.button(text="▶️ Взять в работу", callback_data=OrgCB(act="start", id=rid).pack())
    if is_assignee and request.status in (OrgStatus.IN_PROGRESS, OrgStatus.ASSIGNED):
        kb.button(text="✅ Выполнено", callback_data=OrgCB(act="done", id=rid).pack())

    # --- автор ---
    if is_author and request.status == OrgStatus.CLARIFY:
        kb.button(text="✍️ Дополнить данные", callback_data=OrgCB(act="answer", id=rid).pack())
    if request.status == OrgStatus.DONE and org.can_close(user, request):
        kb.button(text="🔒 Подтвердить и закрыть", callback_data=OrgCB(act="close", id=rid).pack())
        kb.button(text="🔄 Вернуть на доработку", callback_data=OrgCB(act="return", id=rid).pack())
    if request.status in OrgStatus.OPEN:
        kb.button(text="💬 Комментарий", callback_data=OrgCB(act="comment", id=rid).pack())
    if is_author and request.status in (OrgStatus.NEW, OrgStatus.CLARIFY):
        kb.button(text="🚫 Отменить обращение", callback_data=OrgCB(act="cancel", id=rid).pack())

    kb.adjust(1)
    extra = [InlineKeyboardButton(text="🕓 История", callback_data=OrgCB(act="history", id=rid).pack())]
    if request.attachments:
        extra.append(
            InlineKeyboardButton(text="📎 Файлы", callback_data=OrgCB(act="files", id=rid).pack())
        )
    kb.row(*extra)
    kb.row(InlineKeyboardButton(text="⬅️ Назад", callback_data=OrgCB(act="root").pack()))
    return kb.as_markup()


async def _open(call: types.CallbackQuery, session: AsyncSession, user: User, rid: int):
    request = await org.load(session, rid)
    if request is None or user is None or not org.can_view(user, request):
        await call.answer(texts.NO_ACCESS, show_alert=True)
        return None
    return request


async def _show(call: types.CallbackQuery, request: OrgRequest, user: User) -> None:
    try:
        await call.message.edit_text(org.render_card(request), reply_markup=card_kb(request, user))
    except Exception:
        await call.message.answer(org.render_card(request), reply_markup=card_kb(request, user))


@router.callback_query(OrgCB.filter(F.act == "card"))
async def cb_card(
    call: types.CallbackQuery,
    callback_data: OrgCB,
    state: FSMContext,
    session: AsyncSession,
    user: Optional[User],
) -> None:
    await state.clear()
    request = await _open(call, session, user, callback_data.id)
    if request is None:
        return
    await call.answer()
    await _show(call, request, user)


@router.callback_query(OrgCB.filter(F.act == "history"))
async def cb_history(
    call: types.CallbackQuery, callback_data: OrgCB, session: AsyncSession, user: Optional[User]
) -> None:
    request = await _open(call, session, user, callback_data.id)
    if request is None:
        return
    await call.answer()
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ К обращению", callback_data=OrgCB(act="card", id=request.id).pack())
    await call.message.answer(
        f"Обращение <b>{esc(request.number)}</b>\n\n"
        + await org.render_history(session, request.id),
        reply_markup=kb.as_markup(),
    )


@router.callback_query(OrgCB.filter(F.act == "files"))
async def cb_files(
    call: types.CallbackQuery,
    callback_data: OrgCB,
    session: AsyncSession,
    user: Optional[User],
    bot: Bot,
) -> None:
    request = await _open(call, session, user, callback_data.id)
    if request is None:
        return
    await call.answer()
    if not request.attachments:
        await call.message.answer("К обращению не приложено файлов.")
        return
    await org.send_attachments(bot, call.message.chat.id, request, "request")
    await org.send_attachments(bot, call.message.chat.id, request, "result")


# --- УЦЦП: принять ---
@router.callback_query(OrgCB.filter(F.act == "accept"))
async def cb_accept(
    call: types.CallbackQuery,
    callback_data: OrgCB,
    session: AsyncSession,
    user: Optional[User],
    bot: Bot,
) -> None:
    request = await _open(call, session, user, callback_data.id)
    if request is None:
        return
    if not org.can_process(user):
        await call.answer("Принимать обращения может только УЦЦП.", show_alert=True)
        return
    old = request.status
    request.status = OrgStatus.ACCEPTED
    request.accepted_at = utcnow()
    request.accepted_by_id = user.id
    await org.log(session, request, "accepted", user=user, old_status=old,
                  new_status=OrgStatus.ACCEPTED)
    await session.commit()
    request = await org.load(session, request.id)
    await call.answer("Принято в УЦЦП")
    await _show(call, request, user)
    await notify.send_to_user(
        bot,
        request.author,
        f"👍 УЦЦП приняло ваше обращение <b>{esc(request.number)}</b>.\n"
        f"Принял: {esc(user.full_name)}\n\nСледующий шаг — назначение ответственного.",
    )


# --- УЦЦП: назначение ответственного ---
@router.callback_query(OrgCB.filter(F.act == "assign"))
async def cb_assign(
    call: types.CallbackQuery, callback_data: OrgCB, session: AsyncSession, user: Optional[User]
) -> None:
    request = await _open(call, session, user, callback_data.id)
    if request is None:
        return
    if not org.can_process(user):
        await call.answer(texts.NO_ACCESS, show_alert=True)
        return
    staff = await org.uccp_staff(session)
    if not staff:
        await call.answer(
            "В системе нет сотрудников УЦЦП. Назначьте роль «Сотрудник УЦЦП» "
            "в разделе Пользователи.",
            show_alert=True,
        )
        return
    kb = InlineKeyboardBuilder()
    for person in staff:
        kb.button(
            text=f"{person.full_name} · {person.role_title}"[:60],
            callback_data=OrgCB(act="assign_to", id=request.id, value=str(person.id)).pack(),
        )
    kb.adjust(1)
    kb.row(
        InlineKeyboardButton(text="⬅️ Назад", callback_data=OrgCB(act="card", id=request.id).pack())
    )
    await call.answer()
    await call.message.edit_text(
        "Кто в УЦЦП будет ответственным за это обращение?", reply_markup=kb.as_markup()
    )


@router.callback_query(OrgCB.filter(F.act == "assign_to"))
async def cb_assign_to(
    call: types.CallbackQuery,
    callback_data: OrgCB,
    session: AsyncSession,
    user: Optional[User],
    bot: Bot,
) -> None:
    request = await _open(call, session, user, callback_data.id)
    if request is None or not org.can_process(user):
        return
    new_assignee = await session.get(User, int(callback_data.value))
    old_assignee = request.assignee
    old = request.status
    request.assignee_id = new_assignee.id
    request.assigned_at = utcnow()
    if request.status in (OrgStatus.NEW, OrgStatus.ACCEPTED):
        request.status = OrgStatus.ASSIGNED
    await org.log(
        session, request,
        "reassigned" if old_assignee and old_assignee.id != new_assignee.id else "assigned",
        user=user, old_status=old, new_status=request.status,
        details=f"Ответственный: {new_assignee.full_name}",
    )
    await session.commit()
    request = await org.load(session, request.id)
    await call.answer("Ответственный назначен")
    await _show(call, request, user)

    kb = InlineKeyboardBuilder()
    kb.button(text="▶️ Взять в работу", callback_data=OrgCB(act="start", id=request.id).pack())
    kb.button(text="✅ Выполнено", callback_data=OrgCB(act="done", id=request.id).pack())
    kb.adjust(1)
    await org.notify_assignee(session, bot, request, kb.as_markup())
    await notify.send_to_user(
        bot,
        request.author,
        f"🎯 По обращению <b>{esc(request.number)}</b> назначен ответственный "
        f"в УЦЦП: <b>{esc(new_assignee.full_name)}</b>.",
    )


# --- ответственный: в работу ---
@router.callback_query(OrgCB.filter(F.act == "start"))
async def cb_start(
    call: types.CallbackQuery,
    callback_data: OrgCB,
    session: AsyncSession,
    user: Optional[User],
    bot: Bot,
) -> None:
    request = await _open(call, session, user, callback_data.id)
    if request is None:
        return
    if request.assignee_id != user.id:
        await call.answer("Обращение назначено другому сотруднику.", show_alert=True)
        return
    old = request.status
    request.status = OrgStatus.IN_PROGRESS
    request.started_at = utcnow()
    await org.log(session, request, "started", user=user, old_status=old,
                  new_status=OrgStatus.IN_PROGRESS)
    await session.commit()
    request = await org.load(session, request.id)
    await call.answer("Взято в работу")
    await _show(call, request, user)
    await notify.send_to_user(
        bot,
        request.author,
        f"🔧 По обращению <b>{esc(request.number)}</b> начата работа.\n"
        f"Ответственный: {esc(user.full_name)}",
    )


# --- уточнение: УЦЦП спрашивает, автор дополняет ---
@router.callback_query(OrgCB.filter(F.act == "clarify"))
async def cb_clarify(
    call: types.CallbackQuery,
    callback_data: OrgCB,
    state: FSMContext,
    session: AsyncSession,
    user: Optional[User],
) -> None:
    request = await _open(call, session, user, callback_data.id)
    if request is None:
        return
    if not org.can_process(user):
        await call.answer(texts.NO_ACCESS, show_alert=True)
        return
    await state.set_state(OrgFlow.clarify_question)
    await state.update_data(request_id=request.id)
    await call.answer()
    await call.message.answer(
        "Напишите, <b>каких именно данных не хватает</b> — автор получит "
        "этот вопрос дословно.\n"
        "Например: <i>Уточните количество и на какую точку, а также приложите "
        "фото полки.</i>",
        reply_markup=cancel_kb(),
    )


@router.message(OrgFlow.clarify_question, F.text)
async def clarify_question(
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
    question = message.text.strip()
    if len(question) < 5:
        await message.answer("Сформулируйте вопрос подробнее.")
        return
    data = await state.get_data()
    request = await org.load(session, data["request_id"])
    old = request.status
    request.status = OrgStatus.CLARIFY
    request.clarify_question = question
    await org.log(session, request, "clarify", user=user, old_status=old,
                  new_status=OrgStatus.CLARIFY, details=question)
    await session.commit()
    await state.clear()
    await message.answer(
        f"❓ Запрос на уточнение по <b>{esc(request.number)}</b> отправлен автору.",
        reply_markup=main_menu(user),
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="✍️ Дополнить данные", callback_data=OrgCB(act="answer", id=request.id).pack())
    await notify.send_to_user(
        bot,
        request.author,
        f"❓ <b>УЦЦП просит уточнить данные</b> по обращению "
        f"<b>{esc(request.number)}</b>.\n\n"
        f"<i>{esc(question)}</i>\n\n"
        "Нажмите «Дополнить данные» и отправьте недостающее.",
        kb.as_markup(),
    )


@router.callback_query(OrgCB.filter(F.act == "answer"))
async def cb_answer(
    call: types.CallbackQuery,
    callback_data: OrgCB,
    state: FSMContext,
    session: AsyncSession,
    user: Optional[User],
) -> None:
    request = await _open(call, session, user, callback_data.id)
    if request is None:
        return
    if request.author_id != user.id:
        await call.answer("Дополнить данные может только автор обращения.", show_alert=True)
        return
    await state.set_state(OrgFlow.clarify_answer)
    await state.update_data(request_id=request.id)
    await call.answer()
    await call.message.answer(
        f"❓ Запрос УЦЦП: <i>{esc(request.clarify_question or '—')}</i>\n\n"
        "Отправьте недостающие данные одним сообщением. "
        "Фото или файл можно приложить следующим сообщением.",
        reply_markup=cancel_kb(),
    )


@router.message(OrgFlow.clarify_answer, F.text)
async def clarify_answer(
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
    answer = message.text.strip()
    old = request.status
    request.clarify_answer = answer
    request.status = OrgStatus.NEW if request.accepted_at is None else OrgStatus.ACCEPTED
    await org.log(session, request, "clarified", user=user, old_status=old,
                  new_status=request.status, details=answer)
    await session.commit()
    request = await org.load(session, request.id)
    await state.clear()
    await message.answer(
        f"✅ Данные по <b>{esc(request.number)}</b> отправлены в УЦЦП.",
        reply_markup=main_menu(user),
    )
    await org.notify_uccp(
        session, bot, request,
        _card_kb_for_uccp(request),
    )


def _card_kb_for_uccp(request: OrgRequest) -> types.InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📄 Открыть обращение", callback_data=OrgCB(act="card", id=request.id).pack())
    return kb.as_markup()


# --- УЦЦП: отклонение ---
@router.callback_query(OrgCB.filter(F.act == "reject"))
async def cb_reject(
    call: types.CallbackQuery,
    callback_data: OrgCB,
    state: FSMContext,
    session: AsyncSession,
    user: Optional[User],
) -> None:
    request = await _open(call, session, user, callback_data.id)
    if request is None:
        return
    if not org.can_process(user):
        await call.answer(texts.NO_ACCESS, show_alert=True)
        return
    await state.set_state(OrgFlow.reject_reason)
    await state.update_data(request_id=request.id)
    await call.answer()
    await call.message.answer(
        "Укажите <b>причину отклонения</b> — автор её увидит:", reply_markup=cancel_kb()
    )


@router.message(OrgFlow.reject_reason, F.text)
async def reject_reason(
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
    request.status = OrgStatus.REJECTED
    request.reject_reason = reason
    request.closed_at = utcnow()
    await org.log(session, request, "rejected", user=user, old_status=old,
                  new_status=OrgStatus.REJECTED, details=reason)
    await session.commit()
    await state.clear()
    await message.answer(
        f"❌ Обращение <b>{esc(request.number)}</b> отклонено.", reply_markup=main_menu(user)
    )
    await notify.send_to_user(
        bot,
        request.author,
        f"❌ УЦЦП отклонило обращение <b>{esc(request.number)}</b>.\n"
        f"Причина: <i>{esc(reason)}</i>",
    )


# --- выполнение и закрытие ---
@router.callback_query(OrgCB.filter(F.act == "done"))
async def cb_done(
    call: types.CallbackQuery,
    callback_data: OrgCB,
    state: FSMContext,
    session: AsyncSession,
    user: Optional[User],
) -> None:
    request = await _open(call, session, user, callback_data.id)
    if request is None:
        return
    if request.assignee_id != user.id:
        await call.answer("Обращение назначено другому сотруднику.", show_alert=True)
        return
    await state.set_state(OrgFlow.done_comment)
    await state.update_data(request_id=request.id)
    await call.answer()
    await call.message.answer(
        "Опишите, <b>что сделано</b> по обращению:", reply_markup=cancel_kb()
    )


@router.message(OrgFlow.done_comment, F.text)
async def done_comment(
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
    if len(message.text.strip()) < 3:
        await message.answer("Опишите результат подробнее.")
        return
    data = await state.get_data()
    request = await org.load(session, data["request_id"])
    old = request.status
    request.result_comment = message.text.strip()
    request.done_at = utcnow()
    request.status = OrgStatus.DONE
    await org.log(session, request, "done", user=user, old_status=old,
                  new_status=OrgStatus.DONE, details=request.result_comment)
    await session.commit()
    request = await org.load(session, request.id)
    await state.clear()
    await message.answer(
        f"✅ Обращение <b>{esc(request.number)}</b> отмечено выполненным.\n"
        "Автор проверит результат и закроет его.",
        reply_markup=main_menu(user),
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="🔒 Подтвердить и закрыть", callback_data=OrgCB(act="close", id=request.id).pack())
    kb.button(text="🔄 Вернуть на доработку", callback_data=OrgCB(act="return", id=request.id).pack())
    kb.adjust(1)
    await notify.send_to_user(
        bot,
        request.author,
        f"📣 УЦЦП выполнило ваше обращение <b>{esc(request.number)}</b>. "
        "Проверьте результат.\n\n" + org.render_card(request),
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
    request = await _open(call, session, user, callback_data.id)
    if request is None:
        return
    if not org.can_close(user, request):
        await call.answer("Закрыть может автор обращения или УЦЦП.", show_alert=True)
        return
    old = request.status
    request.status = OrgStatus.CLOSED
    request.closed_at = utcnow()
    await org.log(session, request, "closed", user=user, old_status=old,
                  new_status=OrgStatus.CLOSED)
    await session.commit()
    request = await org.load(session, request.id)
    await call.answer("Обращение закрыто")
    await _show(call, request, user)
    await notify.send_to_user(
        bot,
        request.assignee,
        f"🔒 Обращение <b>{esc(request.number)}</b> подтверждено автором и закрыто.",
    )


@router.callback_query(OrgCB.filter(F.act == "return"))
async def cb_return(
    call: types.CallbackQuery,
    callback_data: OrgCB,
    state: FSMContext,
    session: AsyncSession,
    user: Optional[User],
) -> None:
    request = await _open(call, session, user, callback_data.id)
    if request is None:
        return
    if not org.can_close(user, request):
        await call.answer(texts.NO_ACCESS, show_alert=True)
        return
    await state.set_state(OrgFlow.return_reason)
    await state.update_data(request_id=request.id)
    await call.answer()
    await call.message.answer(
        "Укажите, <b>что именно доработать</b>:", reply_markup=cancel_kb()
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
        f"🔄 Обращение <b>{esc(request.number)}</b> возвращено в работу.",
        reply_markup=main_menu(user),
    )
    await notify.send_to_user(
        bot,
        request.assignee,
        f"🔄 Обращение <b>{esc(request.number)}</b> возвращено на доработку.\n"
        f"Замечание: <i>{esc(reason)}</i>",
    )


# --- комментарии и отмена ---
@router.callback_query(OrgCB.filter(F.act == "comment"))
async def cb_comment(
    call: types.CallbackQuery,
    callback_data: OrgCB,
    state: FSMContext,
    session: AsyncSession,
    user: Optional[User],
) -> None:
    request = await _open(call, session, user, callback_data.id)
    if request is None:
        return
    await state.set_state(OrgFlow.comment)
    await state.update_data(request_id=request.id)
    await call.answer()
    await call.message.answer(
        f"Комментарий к обращению <b>{esc(request.number)}</b>:", reply_markup=cancel_kb()
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
        f"💬 Комментарий к обращению <b>{esc(request.number)}</b>\n"
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
    request = await _open(call, session, user, callback_data.id)
    if request is None:
        return
    if request.author_id != user.id and not org.is_uccp(user):
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
        f"🚫 Обращение <b>{esc(request.number)}</b> отменено.", reply_markup=main_menu(user)
    )
    await org.notify_uccp(session, bot, request)


# --------------------------------------------------------------------------- #
# Подача обращения: профиль подставляется автоматически
# --------------------------------------------------------------------------- #
PROMPTS = {
    OrgType.INVENTORY: (
        "Шаг 1 из 5. Что необходимо?\n"
        "Например: <i>Стаканы 400 мл с крышками</i>."
    ),
    OrgType.PROBLEM: (
        "Шаг 1 из 3. Опишите <b>проблему</b>, которую должен решить УЦЦП.\n"
        "Например: <i>Поставщик второй раз привозит не тот сироп.</i>"
    ),
    OrgType.TASK: (
        "Шаг 1 из 3. Опишите, <b>что необходимо сделать</b> со стороны УЦЦП.\n"
        "Например: <i>Согласовать замену вывески и подготовить документы.</i>"
    ),
}


@router.callback_query(OrgCB.filter(F.act == "new"))
async def cb_new(
    call: types.CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    user: Optional[User],
) -> None:
    if user is None or not org.can_create(user):
        await call.answer(
            "Направлять обращения в УЦЦП могут управляющие и администраторы точек.",
            show_alert=True,
        )
        return

    brand = await session.get(Brand, user.brand_id) if user.brand_id else None
    outlet = await session.get(Outlet, user.outlet_id) if user.outlet_id else None
    await state.clear()
    await state.update_data(
        media=[],
        brand_id=user.brand_id,
        outlet_id=user.outlet_id,
        brand_name=brand.name if brand else None,
        outlet_name=outlet.name if outlet else None,
    )
    await state.set_state(NewOrgRequest.request_type)
    await call.answer()

    profile = "\n".join(
        [
            f"Автор: <b>{esc(user.full_name)}</b>",
            f"Должность: {esc(user.position_text or user.role_title)}",
            f"Бренд: {esc(brand.name) if brand else '—'}",
            f"Точка: {esc(outlet.name) if outlet else '—'}",
        ]
    )
    kb = InlineKeyboardBuilder()
    for code in OrgType.ALL:
        kb.button(
            text=OrgType.TITLES[code], callback_data=OrgCB(act="type", value=code).pack()
        )
    kb.adjust(1)
    kb.row(InlineKeyboardButton(text="❌ Отменить", callback_data=OrgCB(act="root").pack()))
    await call.message.edit_text(
        f"{profile}\n\nЭти данные подставлены из вашего профиля — вводить их не нужно.\n\n"
        "<b>Что вы хотите направить в УЦЦП?</b>",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(NewOrgRequest.request_type, OrgCB.filter(F.act == "type"))
async def pick_type(
    call: types.CallbackQuery, callback_data: OrgCB, state: FSMContext
) -> None:
    await state.update_data(request_type=callback_data.value)
    await state.set_state(NewOrgRequest.description)
    await call.answer()
    await call.message.edit_text(
        f"{OrgType.title(callback_data.value)}\n\n{PROMPTS[callback_data.value]}"
    )


@router.message(NewOrgRequest.description, F.text)
async def set_description(
    message: types.Message, state: FSMContext, session: AsyncSession, user: Optional[User]
) -> None:
    if message.text == BTN_CANCEL:
        await state.clear()
        await message.answer(texts.CANCELLED, reply_markup=main_menu(user))
        return
    if len(message.text.strip()) < 5:
        await message.answer("Опишите подробнее — минимум 5 символов.")
        return
    await state.update_data(description=message.text.strip())
    data = await state.get_data()

    if data["request_type"] == OrgType.INVENTORY:
        await state.set_state(NewOrgRequest.quantity)
        await message.answer(
            "Шаг 2 из 5. Укажите <b>количество</b>.\nНапример: <i>200 шт</i>, <i>5 упаковок</i>."
        )
        return
    if await _return_to_preview(message, state, session, user):
        return
    await _ask_due(message, state)


@router.message(NewOrgRequest.quantity, F.text)
async def set_quantity(
    message: types.Message, state: FSMContext, session: AsyncSession, user: Optional[User]
) -> None:
    if message.text == BTN_CANCEL:
        await state.clear()
        await message.answer(texts.CANCELLED, reply_markup=main_menu(user))
        return
    await state.update_data(quantity=message.text.strip())
    if await _return_to_preview(message, state, session, user):
        return
    await state.set_state(NewOrgRequest.reason)
    await message.answer(
        "Шаг 3 из 5. Укажите <b>причину или обоснование</b>.\n"
        "Например: <i>Остаток на 2 дня, продажи выросли.</i>"
    )


@router.message(NewOrgRequest.reason, F.text)
async def set_reason(
    message: types.Message, state: FSMContext, session: AsyncSession, user: Optional[User]
) -> None:
    if message.text == BTN_CANCEL:
        await state.clear()
        await message.answer(texts.CANCELLED, reply_markup=main_menu(user))
        return
    await state.update_data(reason=message.text.strip())
    if await _return_to_preview(message, state, session, user):
        return
    await _ask_due(message, state)


async def _ask_due(target: types.Message, state: FSMContext) -> None:
    data = await state.get_data()
    step = "4 из 5" if data["request_type"] == OrgType.INVENTORY else "2 из 3"
    await state.set_state(NewOrgRequest.due_date)
    await target.answer(
        f"Шаг {step}. Укажите <b>желаемый срок</b> — выберите дату:",
        reply_markup=calendar_kb(with_cancel=False),
    )


@router.callback_query(NewOrgRequest.due_date, CalendarCB.filter(F.act == "nav"))
async def due_nav(call: types.CallbackQuery, callback_data: CalendarCB) -> None:
    await call.answer()
    await call.message.edit_reply_markup(
        reply_markup=calendar_kb(callback_data.year, callback_data.month, with_cancel=False)
    )


@router.callback_query(NewOrgRequest.due_date, CalendarCB.filter(F.act == "day"))
async def due_day(
    call: types.CallbackQuery, callback_data: CalendarCB, state: FSMContext
) -> None:
    await state.update_data(
        due_year=callback_data.year, due_month=callback_data.month, due_day=callback_data.day
    )
    await state.set_state(NewOrgRequest.due_time)
    await call.answer()
    await call.message.edit_text("Выберите <b>время</b>:", reply_markup=time_kb(with_cancel=False))


@router.callback_query(NewOrgRequest.due_time, CalendarCB.filter(F.act == "time"))
async def due_time(
    call: types.CallbackQuery,
    callback_data: CalendarCB,
    state: FSMContext,
    session: AsyncSession,
    user: Optional[User],
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
    if await _return_to_preview(call.message, state, session, user):
        return
    await _ask_media(call.message, state, edit=True)


async def _ask_media(target: types.Message, state: FSMContext, edit: bool = False) -> None:
    data = await state.get_data()
    step = "5 из 5" if data["request_type"] == OrgType.INVENTORY else "3 из 3"
    await state.set_state(NewOrgRequest.media)
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Готово", callback_data=OrgCB(act="media_done").pack())
    kb.button(text="Пропустить", callback_data=OrgCB(act="media_skip").pack())
    kb.adjust(2)
    text = (
        f"Шаг {step}. Приложите <b>фото, видео или документ</b>, если нужно — "
        "это не обязательно.\nМожно отправить несколько файлов подряд."
    )
    if edit:
        await target.edit_text(text, reply_markup=kb.as_markup())
    else:
        await target.answer(text, reply_markup=kb.as_markup())


@router.message(NewOrgRequest.media, F.photo | F.video | F.document)
async def collect_media(message: types.Message, state: FSMContext) -> None:
    data = await state.get_data()
    media = list(data.get("media", []))
    if len(media) >= MAX_MEDIA:
        await message.answer(f"Можно приложить не более {MAX_MEDIA} файлов.")
        return
    if message.photo:
        item = {"file_id": message.photo[-1].file_id, "type": "photo",
                "unique": message.photo[-1].file_unique_id, "name": None}
    elif message.video:
        item = {"file_id": message.video.file_id, "type": "video",
                "unique": message.video.file_unique_id, "name": None}
    else:
        item = {"file_id": message.document.file_id, "type": "document",
                "unique": message.document.file_unique_id,
                "name": message.document.file_name}
    media.append(item)
    await state.update_data(media=media)
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Готово", callback_data=OrgCB(act="media_done").pack())
    kb.adjust(1)
    await message.answer(
        f"Принято ({len(media)}). Можно отправить ещё или нажать «Готово».",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(NewOrgRequest.media, OrgCB.filter(F.act.in_({"media_done", "media_skip"})))
async def media_done(
    call: types.CallbackQuery, state: FSMContext, session: AsyncSession, user: Optional[User]
) -> None:
    await call.answer()
    await _show_preview(call.message, state, user, edit=True)


async def _show_preview(
    target: types.Message, state: FSMContext, user: User, edit: bool
) -> None:
    data = await state.get_data()
    await state.set_state(NewOrgRequest.preview)
    await state.update_data(edit_mode=False)

    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Отправить в УЦЦП", callback_data=OrgCB(act="submit").pack())
    kb.button(text="✏️ Изменить", callback_data=OrgCB(act="edit").pack())
    kb.button(text="❌ Отменить", callback_data=OrgCB(act="root").pack())
    kb.adjust(1, 2)
    text = "Проверьте обращение перед отправкой:\n\n" + org.render_preview(data, user)
    if edit:
        await target.edit_text(text, reply_markup=kb.as_markup())
    else:
        await target.answer(text, reply_markup=kb.as_markup())


async def _return_to_preview(
    target: types.Message, state: FSMContext, session: AsyncSession, user: User
) -> bool:
    data = await state.get_data()
    if not data.get("edit_mode"):
        return False
    edit = bool(target.from_user and target.from_user.is_bot)
    await _show_preview(target, state, user, edit=edit)
    return True


@router.callback_query(NewOrgRequest.preview, OrgCB.filter(F.act == "edit"))
async def cb_edit(call: types.CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    kb = InlineKeyboardBuilder()
    kb.button(text="Тип обращения", callback_data=OrgCB(act="edit_f", value="type").pack())
    kb.button(text="Запрос", callback_data=OrgCB(act="edit_f", value="description").pack())
    if data["request_type"] == OrgType.INVENTORY:
        kb.button(text="Количество", callback_data=OrgCB(act="edit_f", value="quantity").pack())
        kb.button(text="Обоснование", callback_data=OrgCB(act="edit_f", value="reason").pack())
    kb.button(text="Срок", callback_data=OrgCB(act="edit_f", value="due").pack())
    kb.button(text="Фото / файлы", callback_data=OrgCB(act="edit_f", value="media").pack())
    kb.adjust(2)
    kb.row(
        InlineKeyboardButton(text="⬅️ К проверке", callback_data=OrgCB(act="preview").pack())
    )
    await call.answer()
    await call.message.edit_text("Что изменить?", reply_markup=kb.as_markup())


@router.callback_query(NewOrgRequest.preview, OrgCB.filter(F.act == "preview"))
async def cb_back_preview(
    call: types.CallbackQuery, state: FSMContext, user: Optional[User]
) -> None:
    await call.answer()
    await _show_preview(call.message, state, user, edit=True)


@router.callback_query(NewOrgRequest.preview, OrgCB.filter(F.act == "edit_f"))
async def cb_edit_field(
    call: types.CallbackQuery, callback_data: OrgCB, state: FSMContext
) -> None:
    field = callback_data.value
    data = await state.get_data()
    await state.update_data(edit_mode=True)
    await call.answer()

    if field == "type":
        await state.update_data(edit_mode=False)
        await state.set_state(NewOrgRequest.request_type)
        kb = InlineKeyboardBuilder()
        for code in OrgType.ALL:
            kb.button(text=OrgType.TITLES[code], callback_data=OrgCB(act="type", value=code).pack())
        kb.adjust(1)
        await call.message.edit_text(
            "<b>Что вы хотите направить в УЦЦП?</b>", reply_markup=kb.as_markup()
        )
    elif field == "description":
        await state.set_state(NewOrgRequest.description)
        await call.message.edit_text(PROMPTS[data["request_type"]])
    elif field == "quantity":
        await state.set_state(NewOrgRequest.quantity)
        await call.message.edit_text("Укажите <b>количество</b>:")
    elif field == "reason":
        await state.set_state(NewOrgRequest.reason)
        await call.message.edit_text("Укажите <b>причину или обоснование</b>:")
    elif field == "due":
        await state.set_state(NewOrgRequest.due_date)
        await call.message.edit_text(
            "Выберите новую <b>дату</b>:", reply_markup=calendar_kb(with_cancel=False)
        )
    elif field == "media":
        await state.update_data(media=[], edit_mode=False)
        await call.message.edit_text("Прежние файлы удалены.")
        await _ask_media(call.message, state)


@router.callback_query(NewOrgRequest.preview, OrgCB.filter(F.act == "submit"))
async def cb_submit(
    call: types.CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    user: Optional[User],
    bot: Bot,
) -> None:
    data = await state.get_data()
    missing = [
        name for key, name in (
            ("request_type", "тип обращения"),
            ("description", "описание"),
            ("due_local", "срок"),
        ) if not data.get(key)
    ]
    if data.get("request_type") == OrgType.INVENTORY and not data.get("quantity"):
        missing.append("количество")
    if missing:
        await call.answer("Не заполнено: " + ", ".join(missing), show_alert=True)
        return

    await call.answer("Отправляю в УЦЦП…")
    request = OrgRequest(
        number=await next_org_number(session),
        external_id=new_external_id(),
        request_type=data["request_type"],
        description=data["description"],
        quantity=data.get("quantity"),
        reason=data.get("reason"),
        due_at=utc_from_local(dt.datetime.fromisoformat(data["due_local"])),
        brand_id=data.get("brand_id"),
        outlet_id=data.get("outlet_id"),
        author_id=user.id,                       # автор — из профиля
        author_position=user.position_text or user.role_title,
        status=OrgStatus.NEW,
    )
    session.add(request)
    await session.flush()

    for item in data.get("media", []):
        session.add(
            OrgAttachment(
                request_id=request.id,
                file_id=item["file_id"],
                file_unique_id=item.get("unique"),
                media_type=item["type"],
                file_name=item.get("name"),
                stage="request",
                uploaded_by_id=user.id,
            )
        )
    await org.log(session, request, "created", user=user, new_status=OrgStatus.NEW)
    await session.commit()
    request = await org.load(session, request.id)
    await state.clear()

    sent = await org.notify_uccp(session, bot, request, _card_kb_for_uccp(request))
    for person in await org.uccp_staff(session):
        if person.tg_id:
            await org.send_attachments(bot, person.tg_id, request, "request")

    tail = (
        f"\n\nОбращение получили сотрудники УЦЦП ({sent}). Вам придёт уведомление, "
        "когда его примут в работу."
        if sent
        else "\n\n⚠️ В системе пока нет сотрудников УЦЦП. Обращение сохранено "
        "и будет обработано, как только появится ответственный."
    )
    await call.message.edit_text(
        f"✅ <b>Обращение {esc(request.number)} направлено в УЦЦП</b>\n\n"
        + org.render_card(request) + tail
    )
    await call.message.answer(_root_text(user), reply_markup=_root_kb(user))


@router.message(StateFilter(NewOrgRequest, OrgFlow))
async def wrong_input(message: types.Message, state: FSMContext) -> None:
    current = await state.get_state()
    hints = {
        NewOrgRequest.request_type.state: "Выберите тип обращения кнопкой выше.",
        NewOrgRequest.description.state: "Опишите обращение текстом.",
        NewOrgRequest.quantity.state: "Укажите количество текстом.",
        NewOrgRequest.reason.state: "Укажите обоснование текстом.",
        NewOrgRequest.due_date.state: "Выберите дату в календаре выше.",
        NewOrgRequest.due_time.state: "Выберите время кнопкой выше.",
        NewOrgRequest.media.state: "Приложите файл или нажмите «Готово».",
        NewOrgRequest.preview.state: "Нажмите «Отправить в УЦЦП», «Изменить» или «Отменить».",
    }
    await message.answer("Для продолжения: " + hints.get(current, "используйте кнопки выше."))
