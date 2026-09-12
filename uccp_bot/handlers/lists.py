"""Списки заявок, карточка, история и вложения."""
from __future__ import annotations

import re
from typing import List, Optional, Sequence

from aiogram import Bot, F, Router, types
from aiogram.fsm.context import FSMContext
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import texts
from ..callbacks import ListCB, ReqCB
from ..db.models import Priority, Request, RoleCode, Status, User
from ..keyboards.common import (
    BTN_BRAND_REQUESTS,
    BTN_CONFIRM_QUEUE,
    BTN_MY,
    BTN_OUTLET_REQUESTS,
    BTN_STATUS,
    BTN_TASKS,
    main_menu,
)
from ..keyboards.request import card_kb, simple_card_kb
from ..services.cards import REQUEST_LOAD_OPTIONS, load_request, render_card, render_comments
from ..services.history import render_history
from ..services.notify import send_attachments
from ..utils import esc, fmt_dt

router = Router(name="lists")

PAGE_SIZE = 8
NUMBER_RE = re.compile(r"REQ-\d{4}-\d{4,8}", re.IGNORECASE)


def _base_query():
    return select(Request).options(*REQUEST_LOAD_OPTIONS)


def scope_condition(user: User):
    """Что пользователь вправе видеть (см. роли в db/models.py)."""
    if user.is_admin:
        return None
    if user.is_ops_director:
        return Request.brand_id == user.brand_id if user.brand_id else Request.id < 0
    if user.is_outlet_admin:
        return Request.outlet_id == user.outlet_id if user.outlet_id else Request.id < 0
    if user.is_executor:
        return Request.executor_id == user.id
    return or_(Request.author_id == user.id, Request.executor_id == user.id)


async def _fetch(session: AsyncSession, kind: str, user: User, page: int):
    stmt = _base_query()

    if kind == "my":
        stmt = stmt.where(Request.author_id == user.id)
    elif kind == "tasks":
        stmt = stmt.where(
            Request.executor_id == user.id, Request.status.in_(Status.OPEN)
        )
    else:
        condition = scope_condition(user)
        if condition is not None:
            stmt = stmt.where(condition)
        if kind == "confirm":
            stmt = stmt.where(Request.status == Status.DONE)
        elif kind in ("open", "outlet", "brand"):
            stmt = stmt.where(Request.status.in_(Status.OPEN))
        elif kind == "overdue":
            stmt = stmt.where(Request.is_overdue.is_(True), Request.status.in_(Status.OPEN))
        elif kind == "await":
            stmt = stmt.where(Request.status == Status.AWAITING_ASSIGNMENT)
        elif kind == "priority":
            stmt = stmt.where(
                Request.status.in_(Status.OPEN),
                Request.priority.in_((Priority.HIGH, Priority.CRITICAL)),
            )
        elif kind == "done":
            stmt = stmt.where(Request.status.in_((Status.DONE, Status.CONFIRMED, Status.CLOSED)))

    stmt = stmt.order_by(Request.created_at.desc()).limit(PAGE_SIZE + 1).offset(page * PAGE_SIZE)
    rows = list((await session.scalars(stmt)).all())
    has_next = len(rows) > PAGE_SIZE
    return rows[:PAGE_SIZE], has_next


def _list_kb(kind: str, rows: Sequence[Request], page: int, has_next: bool):
    kb = InlineKeyboardBuilder()
    for req in rows:
        kb.button(
            text=f"{req.number} · {req.outlet.name} · {Status.title(req.status)}",
            callback_data=ReqCB(act="card", request_id=req.id).pack(),
        )
    kb.adjust(1)
    nav = []
    if page > 0:
        nav.append(
            types.InlineKeyboardButton(
                text="⬅️", callback_data=ListCB(kind=kind, page=page - 1).pack()
            )
        )
    if has_next:
        nav.append(
            types.InlineKeyboardButton(
                text="➡️", callback_data=ListCB(kind=kind, page=page + 1).pack()
            )
        )
    if nav:
        kb.row(*nav)
    return kb.as_markup()


TITLES = {
    "my": "📋 <b>Мои заявки</b>",
    "outlet": "🏬 <b>Актуальные заявки точки</b>",
    "brand": "🏢 <b>Актуальные заявки бренда</b>",
    "priority": "❗️ <b>Приоритетные заявки</b>",
    "done": "✅ <b>Выполненные заявки</b>",
    "tasks": "🧰 <b>Мои задачи</b>",
    "confirm": "☑️ <b>Заявки, ожидающие вашего подтверждения</b>",
    "open": "📂 <b>Активные заявки</b>",
    "overdue": "🔴 <b>Просроченные заявки</b>",
    "await": "⏳ <b>Ожидают назначения исполнителя</b>",
}
EMPTY = {
    "my": "Вы ещё не создавали заявок. Нажмите ➕ Новая заявка.",
    "outlet": "На вашей точке нет активных заявок.",
    "brand": "По вашему бренду нет активных заявок.",
    "priority": "Приоритетных заявок нет.",
    "done": "Выполненных заявок пока нет.",
    "tasks": "Активных задач нет. Новые заявки придут сюда автоматически.",
    "confirm": "Нет заявок, ожидающих подтверждения.",
    "open": "Активных заявок нет.",
    "overdue": "Просроченных заявок нет. 👍",
    "await": "Все заявки распределены между исполнителями.",
}


async def show_list(
    target: types.Message,
    session: AsyncSession,
    user: User,
    kind: str,
    page: int = 0,
    edit: bool = False,
) -> None:
    rows, has_next = await _fetch(session, kind, user, page)
    if not rows:
        text = TITLES.get(kind, "") + "\n\n" + EMPTY.get(kind, "Ничего не найдено.")
        if edit:
            await target.edit_text(text)
        else:
            await target.answer(text)
        return

    lines = [TITLES.get(kind, "")]
    for req in rows:
        lines.append(
            f"\n<b>{esc(req.number)}</b> · {req.priority_title}\n"
            f"{esc(req.outlet.name)} · {esc(req.category.label)}\n"
            f"{req.status_title} · срок {fmt_dt(req.due_at)}"
        )
    lines.append("\nВыберите заявку, чтобы открыть карточку:")
    text = "\n".join(lines)
    kb = _list_kb(kind, rows, page, has_next)
    if edit:
        await target.edit_text(text, reply_markup=kb)
    else:
        await target.answer(text, reply_markup=kb)


@router.message(F.text == BTN_MY)
async def my_requests(message: types.Message, session: AsyncSession, user: Optional[User]) -> None:
    if user is None:
        await message.answer(texts.NOT_REGISTERED)
        return
    await show_list(message, session, user, "my")


@router.message(F.text == BTN_TASKS)
async def my_tasks(message: types.Message, session: AsyncSession, user: Optional[User]) -> None:
    if user is None:
        await message.answer(texts.NOT_REGISTERED)
        return
    await show_list(message, session, user, "tasks")


@router.message(F.text == BTN_CONFIRM_QUEUE)
async def confirm_queue(
    message: types.Message, session: AsyncSession, user: Optional[User]
) -> None:
    if user is None:
        await message.answer(texts.NOT_REGISTERED)
        return
    await show_list(message, session, user, "confirm")


@router.message(F.text == BTN_OUTLET_REQUESTS)
async def outlet_requests(
    message: types.Message, session: AsyncSession, user: Optional[User]
) -> None:
    if user is None or not user.is_approved:
        await message.answer(texts.NOT_REGISTERED)
        return
    if not (user.is_outlet_admin or user.is_admin):
        await message.answer(texts.NO_ACCESS)
        return
    await message.answer(
        "🏬 <b>Заявки вашей точки</b>\nВыберите, что показать:",
        reply_markup=_scope_kb("outlet"),
    )


@router.message(F.text == BTN_BRAND_REQUESTS)
async def brand_requests(
    message: types.Message, session: AsyncSession, user: Optional[User]
) -> None:
    if user is None or not user.is_approved:
        await message.answer(texts.NOT_REGISTERED)
        return
    if not (user.is_ops_director or user.is_admin):
        await message.answer(texts.NO_ACCESS)
        return
    await message.answer(
        "🏢 <b>Заявки вашего бренда</b>\nВыберите, что показать:",
        reply_markup=_scope_kb("brand"),
    )


def _scope_kb(base: str) -> types.InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for kind, label in (
        (base, "📂 Актуальные"),
        ("priority", "❗️ Приоритетные"),
        ("overdue", "🔴 Просроченные"),
        ("done", "✅ Выполненные"),
    ):
        kb.button(text=label, callback_data=ListCB(kind=kind, page=0).pack())
    kb.adjust(2)
    return kb.as_markup()


@router.message(F.text == BTN_STATUS)
async def check_status(
    message: types.Message, session: AsyncSession, user: Optional[User]
) -> None:
    if user is None:
        await message.answer(texts.NOT_REGISTERED)
        return
    await message.answer(
        "🔍 <b>Проверить статус</b>\n\n"
        "Отправьте номер заявки (например <code>REQ-2026-000012</code>) — "
        "бот покажет карточку.\nНиже — ваши последние заявки."
    )
    kind = "tasks" if user.is_executor else "my"
    await show_list(message, session, user, kind)


@router.callback_query(ListCB.filter())
async def paginate(
    call: types.CallbackQuery,
    callback_data: ListCB,
    session: AsyncSession,
    user: Optional[User],
) -> None:
    if user is None:
        await call.answer(texts.NOT_REGISTERED, show_alert=True)
        return
    await call.answer()
    await show_list(call.message, session, user, callback_data.kind, callback_data.page, edit=True)


@router.message(F.text.regexp(NUMBER_RE.pattern))
async def find_by_number(
    message: types.Message, session: AsyncSession, user: Optional[User]
) -> None:
    if user is None:
        await message.answer(texts.NOT_REGISTERED)
        return
    match = NUMBER_RE.search(message.text)
    request = await session.scalar(
        _base_query().where(Request.number == match.group(0).upper())
    )
    if request is None:
        await message.answer("Заявка с таким номером не найдена. Проверьте номер.")
        return
    if not _can_view(user, request):
        await message.answer(texts.NO_ACCESS)
        return
    await message.answer(render_card(request), reply_markup=card_kb(request, user))


def _can_view(user: User, request: Request) -> bool:
    if user.is_admin:
        return True
    if request.author_id == user.id or request.executor_id == user.id:
        return True
    if user.is_ops_director:
        return request.brand_id == user.brand_id
    if user.is_outlet_admin:
        return request.outlet_id == user.outlet_id
    return False


@router.callback_query(ReqCB.filter(F.act == "card"))
async def open_card(
    call: types.CallbackQuery,
    callback_data: ReqCB,
    session: AsyncSession,
    user: Optional[User],
    state: FSMContext,
) -> None:
    await state.clear()
    request = await load_request(session, callback_data.request_id)
    if request is None or user is None or not _can_view(user, request):
        await call.answer(texts.NO_ACCESS, show_alert=True)
        return
    await call.answer()
    text = render_card(request) + await render_comments(session, request.id)
    try:
        await call.message.edit_text(text, reply_markup=card_kb(request, user))
    except Exception:
        await call.message.answer(text, reply_markup=card_kb(request, user))


@router.callback_query(ReqCB.filter(F.act == "history"))
async def show_history(
    call: types.CallbackQuery,
    callback_data: ReqCB,
    session: AsyncSession,
    user: Optional[User],
) -> None:
    request = await load_request(session, callback_data.request_id)
    if request is None or user is None or not _can_view(user, request):
        await call.answer(texts.NO_ACCESS, show_alert=True)
        return
    await call.answer()
    text = f"Заявка <b>{esc(request.number)}</b>\n\n" + await render_history(session, request.id)
    await call.message.answer(text, reply_markup=simple_card_kb(request.id))


@router.callback_query(ReqCB.filter(F.act == "media"))
async def show_media(
    call: types.CallbackQuery,
    callback_data: ReqCB,
    session: AsyncSession,
    user: Optional[User],
    bot: Bot,
) -> None:
    request = await load_request(session, callback_data.request_id)
    if request is None or user is None or not _can_view(user, request):
        await call.answer(texts.NO_ACCESS, show_alert=True)
        return
    await call.answer()
    before = [a for a in request.attachments if a.stage == "before"]
    after = [a for a in request.attachments if a.stage == "after"]
    if not before and not after:
        await call.message.answer("К заявке не приложено файлов.")
        return
    if before:
        await send_attachments(
            bot, call.message.chat.id, before, caption=f"{request.number} — проблема"
        )
    if after:
        await send_attachments(
            bot, call.message.chat.id, after, caption=f"{request.number} — результат работ"
        )
