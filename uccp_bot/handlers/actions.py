"""Действия по заявке: исполнитель (п.19-21), менеджер (п.22-23), администратор (п.14)."""
from __future__ import annotations

import datetime as dt
from typing import Optional

from aiogram import Bot, F, Router, types
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import texts
from ..callbacks import CalendarCB, ReqCB
from ..db.models import Attachment, Category, Comment, Cost, Request, Status, User
from ..keyboards.calendar import calendar_kb, time_kb
from ..keyboards.common import cancel_kb, main_menu, remove_kb, BTN_CANCEL
from ..keyboards.request import (
    admin_assign_kb,
    card_kb,
    categories_pick_kb,
    executor_kb,
    executors_pick_kb,
    initiator_kb,
    simple_card_kb,
)
from ..services import flow, history, notify, routing
from ..services.cards import load_request, render_card
from ..states import ExecutorFlow, ManagerFlow, AdminFlow
from ..utils import esc, fmt_dt, fmt_money, parse_amount, utc_from_local, utcnow

router = Router(name="actions")


async def _get(session: AsyncSession, call: types.CallbackQuery, request_id: int):
    request = await load_request(session, request_id)
    if request is None:
        await call.answer("Заявка не найдена", show_alert=True)
    return request


def _is_executor_of(user: User, request: Request) -> bool:
    return user is not None and request.executor_id == user.id


async def _refresh_card(call: types.CallbackQuery, request: Request, user: User) -> None:
    try:
        await call.message.edit_text(render_card(request), reply_markup=card_kb(request, user))
    except Exception:
        await call.message.answer(render_card(request), reply_markup=card_kb(request, user))


# --------------------------------------------------------------------------- #
# Исполнитель: принять / отклонить / предложить срок / начать
# --------------------------------------------------------------------------- #
@router.callback_query(ReqCB.filter(F.act == "accept"))
async def act_accept(
    call: types.CallbackQuery,
    callback_data: ReqCB,
    session: AsyncSession,
    user: Optional[User],
    bot: Bot,
) -> None:
    request = await _get(session, call, callback_data.request_id)
    if request is None:
        return
    if not _is_executor_of(user, request):
        await call.answer(texts.NO_ACCESS, show_alert=True)
        return
    if request.status not in (Status.NEW, Status.AWAITING_ASSIGNMENT):
        await call.answer("Заявка уже в работе.", show_alert=True)
        return

    old = request.status
    request.status = Status.ACCEPTED
    request.accepted_at = utcnow()
    await history.log(
        session, request, "accepted", user=user, old_status=old, new_status=Status.ACCEPTED
    )
    await session.commit()
    await call.answer("Заявка принята")
    await _refresh_card(call, request, user)
    await notify.send_to_user(
        bot,
        request.author,
        f"👍 Исполнитель <b>{esc(user.full_name)}</b> принял заявку "
        f"<b>{esc(request.number)}</b>.\nСрок: {fmt_dt(request.due_at)}",
    )


@router.callback_query(ReqCB.filter(F.act == "start"))
async def act_start(
    call: types.CallbackQuery,
    callback_data: ReqCB,
    session: AsyncSession,
    user: Optional[User],
    bot: Bot,
) -> None:
    request = await _get(session, call, callback_data.request_id)
    if request is None:
        return
    if not _is_executor_of(user, request):
        await call.answer(texts.NO_ACCESS, show_alert=True)
        return

    old = request.status
    request.status = Status.IN_PROGRESS
    request.started_at = utcnow()
    await history.log(
        session, request, "started", user=user, old_status=old, new_status=Status.IN_PROGRESS
    )
    await session.commit()
    await call.answer("Статус: В процессе")
    await _refresh_card(call, request, user)
    await notify.send_to_user(
        bot,
        request.author,
        f"🔧 По заявке <b>{esc(request.number)}</b> начаты работы.\n"
        f"Исполнитель: {esc(user.full_name)}",
    )


@router.callback_query(ReqCB.filter(F.act == "reject"))
async def act_reject(
    call: types.CallbackQuery,
    callback_data: ReqCB,
    session: AsyncSession,
    state: FSMContext,
    user: Optional[User],
) -> None:
    request = await _get(session, call, callback_data.request_id)
    if request is None:
        return
    if not _is_executor_of(user, request):
        await call.answer(texts.NO_ACCESS, show_alert=True)
        return
    await state.set_state(ExecutorFlow.reject_reason)
    await state.update_data(request_id=request.id)
    await call.answer()
    await call.message.answer(
        "Укажите <b>причину отклонения</b> — это обязательное поле.\n"
        "Например: <i>Требуется замена узла, нужен другой специалист.</i>",
        reply_markup=cancel_kb(),
    )


@router.message(ExecutorFlow.reject_reason, F.text)
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
    reason = message.text.strip()
    if len(reason) < 3:
        await message.answer("Опишите причину подробнее.")
        return
    data = await state.get_data()
    request = await load_request(session, data["request_id"])
    old = request.status
    request.status = Status.REJECTED
    request.reject_reason = reason
    await history.log(
        session, request, "rejected", user=user, old_status=old,
        new_status=Status.REJECTED, details=reason,
    )
    await session.flush()

    text = (
        f"❌ Исполнитель <b>{esc(user.full_name)}</b> отклонил заявку "
        f"<b>{esc(request.number)}</b>.\nПричина: <i>{esc(reason)}</i>\n\n"
        "Требуется назначить другого исполнителя."
    )
    await notify.send_to_user(bot, request.author, text)
    request.status = Status.AWAITING_ASSIGNMENT
    await history.log(
        session, request, "escalated", old_status=Status.REJECTED,
        new_status=Status.AWAITING_ASSIGNMENT, details="Отклонена исполнителем",
    )
    await session.flush()
    await notify.notify_admins(
        session, bot, text + "\n\n" + render_card(request), admin_assign_kb(request)
    )
    await session.commit()
    await state.clear()
    await message.answer(
        "Заявка отклонена. Администратор назначит другого исполнителя.",
        reply_markup=main_menu(user),
    )


# --- предложение нового срока ---
@router.callback_query(ReqCB.filter(F.act == "propose"))
async def act_propose(
    call: types.CallbackQuery,
    callback_data: ReqCB,
    session: AsyncSession,
    state: FSMContext,
    user: Optional[User],
) -> None:
    request = await _get(session, call, callback_data.request_id)
    if request is None:
        return
    if not _is_executor_of(user, request):
        await call.answer(texts.NO_ACCESS, show_alert=True)
        return
    await state.set_state(ExecutorFlow.propose_date)
    await state.update_data(request_id=request.id)
    await call.answer()
    await call.message.answer(
        f"Текущий срок: <b>{fmt_dt(request.due_at)}</b>\nВыберите новую дату:",
        reply_markup=calendar_kb(),
    )


@router.callback_query(ExecutorFlow.propose_date, CalendarCB.filter(F.act == "nav"))
async def propose_nav(call: types.CallbackQuery, callback_data: CalendarCB) -> None:
    await call.answer()
    await call.message.edit_reply_markup(
        reply_markup=calendar_kb(callback_data.year, callback_data.month)
    )


@router.callback_query(ExecutorFlow.propose_date, CalendarCB.filter(F.act == "day"))
async def propose_day(
    call: types.CallbackQuery, callback_data: CalendarCB, state: FSMContext
) -> None:
    await state.update_data(
        year=callback_data.year, month=callback_data.month, day=callback_data.day
    )
    await state.set_state(ExecutorFlow.propose_time)
    await call.answer()
    await call.message.edit_text("Выберите время:", reply_markup=time_kb())


@router.callback_query(ExecutorFlow.propose_time, CalendarCB.filter(F.act == "time"))
async def propose_time(
    call: types.CallbackQuery,
    callback_data: CalendarCB,
    state: FSMContext,
    session: AsyncSession,
    user: Optional[User],
    bot: Bot,
) -> None:
    data = await state.get_data()
    request = await load_request(session, data["request_id"])
    proposed_local = dt.datetime(
        data["year"], data["month"], data["day"], callback_data.hour, callback_data.minute
    )
    request.proposed_due_at = utc_from_local(proposed_local)
    old = request.status
    request.status = Status.RESCHEDULE_PROPOSED
    await history.log(
        session, request, "propose_due", user=user, old_status=old,
        new_status=Status.RESCHEDULE_PROPOSED,
        details=f"Новый срок: {fmt_dt(request.proposed_due_at)}",
    )
    await session.commit()
    await state.clear()
    await call.answer("Предложение отправлено")
    await call.message.edit_text(
        f"📅 Новый срок предложен: <b>{fmt_dt(request.proposed_due_at)}</b>.\n"
        "Ожидайте подтверждения инициатора."
    )
    await notify.send_to_user(
        bot,
        request.author,
        f"📅 Исполнитель предлагает перенести срок по заявке <b>{esc(request.number)}</b>.\n"
        f"Было: {fmt_dt(request.due_at)}\nПредложено: <b>{fmt_dt(request.proposed_due_at)}</b>\n\n"
        "Согласуйте новый срок или оставьте прежний.",
        initiator_kb(request, request.author),
    )


@router.callback_query(ReqCB.filter(F.act.in_({"due_ok", "due_no"})))
async def act_due_decision(
    call: types.CallbackQuery,
    callback_data: ReqCB,
    session: AsyncSession,
    user: Optional[User],
    bot: Bot,
) -> None:
    request = await _get(session, call, callback_data.request_id)
    if request is None:
        return
    if user is None or not (user.id == request.author_id or user.can_confirm):
        await call.answer(texts.NO_ACCESS, show_alert=True)
        return
    if request.status != Status.RESCHEDULE_PROPOSED:
        await call.answer("Решение уже принято.", show_alert=True)
        return

    approved = callback_data.act == "due_ok"
    if approved:
        old_due = request.due_at
        request.due_at = request.proposed_due_at
        request.is_overdue = False
        await history.log(
            session, request, "due_approved", user=user,
            details=f"{fmt_dt(old_due)} → {fmt_dt(request.due_at)}",
        )
        message = (
            f"✅ Новый срок по заявке <b>{esc(request.number)}</b> согласован: "
            f"<b>{fmt_dt(request.due_at)}</b>."
        )
    else:
        await history.log(
            session, request, "due_declined", user=user,
            details=f"Оставлен прежний срок {fmt_dt(request.due_at)}",
        )
        message = (
            f"❌ Перенос срока по заявке <b>{esc(request.number)}</b> отклонён. "
            f"Срок прежний: <b>{fmt_dt(request.due_at)}</b>."
        )
    request.proposed_due_at = None
    request.status = Status.ACCEPTED if request.accepted_at else Status.NEW
    await session.commit()
    await call.answer()
    await _refresh_card(call, request, user)
    await notify.send_to_user(bot, request.executor, message, executor_kb(request))


# --------------------------------------------------------------------------- #
# Завершение работы (п.21)
# --------------------------------------------------------------------------- #
@router.callback_query(ReqCB.filter(F.act == "done"))
async def act_done(
    call: types.CallbackQuery,
    callback_data: ReqCB,
    session: AsyncSession,
    state: FSMContext,
    user: Optional[User],
) -> None:
    request = await _get(session, call, callback_data.request_id)
    if request is None:
        return
    if not _is_executor_of(user, request):
        await call.answer(texts.NO_ACCESS, show_alert=True)
        return
    fixed = bool(user.fixed_payment)
    await state.set_state(ExecutorFlow.done_comment)
    await state.update_data(request_id=request.id, done_media=[], fixed_payment=fixed)
    await call.answer()
    if fixed:
        await call.message.answer(
            "Шаг 1 из 2. Опишите, <b>какие работы проведены</b>.\n"
            "Например: <i>Промывка группы, замена фильтра, калибровка помола.</i>\n\n"
            "Стоимость указывать не нужно — ваша работа входит в фиксированную "
            "ежемесячную оплату.",
            reply_markup=cancel_kb(),
        )
    else:
        await call.message.answer(
            "Шаг 1 из 4. Опишите, <b>что было сделано</b>.\n"
            "Например: <i>Заменён подшипник двигателя вытяжки, проверена тяга.</i>",
            reply_markup=cancel_kb(),
        )


@router.message(ExecutorFlow.done_comment, F.text)
async def done_comment(message: types.Message, state: FSMContext, user: Optional[User]) -> None:
    if message.text == BTN_CANCEL:
        await state.clear()
        await message.answer(texts.CANCELLED, reply_markup=main_menu(user))
        return
    if len(message.text.strip()) < 3:
        await message.answer("Опишите выполненную работу подробнее.")
        return
    data = await state.get_data()
    await state.update_data(done_comment=message.text.strip())
    await state.set_state(ExecutorFlow.done_photo)

    if data.get("fixed_payment"):
        await message.answer(
            "Шаг 2 из 2. Приложите <b>фото</b>, если нужно — это не обязательно.\n"
            "Можно отправить несколько снимков. Когда закончите, напишите "
            "<b>готово</b>, а если фото не требуется — <b>пропустить</b>."
        )
    else:
        await message.answer(
            "Шаг 2 из 4. Прикрепите <b>фото результата</b> — это обязательно.\n"
            "Можно отправить несколько снимков, затем напишите «готово»."
        )


@router.message(ExecutorFlow.done_photo, F.photo | F.video)
async def done_photo(message: types.Message, state: FSMContext) -> None:
    data = await state.get_data()
    media = list(data.get("done_media", []))
    if message.photo:
        media.append({"file_id": message.photo[-1].file_id, "type": "photo",
                      "unique": message.photo[-1].file_unique_id})
    else:
        media.append({"file_id": message.video.file_id, "type": "video",
                      "unique": message.video.file_unique_id})
    await state.update_data(done_media=media)
    await message.answer(
        f"Принято ({len(media)}). Отправьте ещё фото или напишите «готово»."
    )


@router.message(ExecutorFlow.done_photo, F.text)
async def done_photo_text(
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
    fixed = bool(data.get("fixed_payment"))
    answer = message.text.strip().lower()
    done_words = ("готово", "done", "ок", "ok")
    skip_words = ("пропустить", "без фото", "нет", "skip")

    if answer not in done_words + (skip_words if fixed else ()):
        if fixed:
            await message.answer(
                "Приложите фото или напишите «готово» / «пропустить»."
            )
        else:
            await message.answer("Отправьте фото результата или напишите «готово».")
        return

    if fixed:
        # стоимость не спрашиваем — работа по фиксированной месячной оплате
        await _finalize_done(message, state, session, user, bot, cost_exempt=True)
        return

    if not data.get("done_media"):
        await message.answer(
            "⚠️ Фото результата обязательно. Отправьте хотя бы один снимок."
        )
        return
    await state.set_state(ExecutorFlow.done_work_cost)
    await message.answer(
        "Шаг 3 из 4. Укажите <b>фактическую стоимость работ</b> (только число).\n"
        "Если работы бесплатные — отправьте <code>0</code>."
    )


@router.message(ExecutorFlow.done_work_cost, F.text)
async def done_work_cost(
    message: types.Message, state: FSMContext, user: Optional[User]
) -> None:
    if message.text == BTN_CANCEL:
        await state.clear()
        await message.answer(texts.CANCELLED, reply_markup=main_menu(user))
        return
    amount = parse_amount(message.text)
    if amount is None:
        await message.answer("Введите сумму числом, например: 250 или 250.50")
        return
    await state.update_data(work_cost=amount)
    await state.set_state(ExecutorFlow.done_material_cost)
    await message.answer(
        "Шаг 4 из 4. Укажите <b>стоимость материалов</b> (только число).\n"
        "Если материалов не было — отправьте <code>0</code>."
    )


@router.message(ExecutorFlow.done_material_cost, F.text)
async def done_material_cost(
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
        await message.answer("Введите сумму числом, например: 120 или 120.75")
        return
    await _finalize_done(message, state, session, user, bot, material_cost=amount)


async def _finalize_done(
    message: types.Message,
    state: FSMContext,
    session: AsyncSession,
    user: User,
    bot: Bot,
    material_cost: Optional[float] = None,
    cost_exempt: bool = False,
) -> None:
    """Общий финал для обоих сценариев: с указанием стоимости и без него."""
    data = await state.get_data()
    request = await load_request(session, data["request_id"])
    old = request.status

    request.executor_comment = data.get("done_comment")
    request.done_at = utcnow()
    request.status = Status.DONE
    request.return_reason = None
    request.cost_exempt = cost_exempt

    if cost_exempt:
        # работа входит в фиксированную месячную оплату — суммы не заводим
        request.work_cost = None
        request.material_cost = None
    else:
        request.work_cost = data.get("work_cost", 0)
        request.material_cost = material_cost or 0
        session.add(
            Cost(request_id=request.id, kind="work",
                 amount=request.work_cost, added_by_id=user.id)
        )
        session.add(
            Cost(request_id=request.id, kind="material",
                 amount=request.material_cost, added_by_id=user.id)
        )

    for item in data.get("done_media", []):
        session.add(
            Attachment(
                request_id=request.id,
                file_id=item["file_id"],
                file_unique_id=item.get("unique"),
                media_type=item["type"],
                stage="after",
                uploaded_by_id=user.id,
            )
        )

    details = (
        "Работа по фиксированной месячной оплате — стоимость по заявке не считается"
        if cost_exempt
        else (
            f"Работы: {fmt_money(request.work_cost)}, "
            f"материалы: {fmt_money(request.material_cost)}"
        )
    )
    await history.log(
        session, request, "done", user=user, old_status=old, new_status=Status.DONE,
        details=details,
    )
    await session.commit()
    await session.refresh(request, ["attachments"])
    await state.clear()

    summary = (
        "Стоимость по этой заявке не считается — работа входит в вашу "
        "фиксированную ежемесячную оплату."
        if cost_exempt
        else f"Стоимость: {fmt_money(request.total_cost)}"
    )
    await message.answer(
        f"✅ Заявка <b>{esc(request.number)}</b> отмечена как выполненная.\n"
        f"{summary}\n\n"
        "Заявка отправлена инициатору на проверку. Закрывает заявку инициатор или менеджер.",
        reply_markup=main_menu(user),
    )

    text = (
        f"📣 Исполнитель отметил заявку <b>{esc(request.number)}</b> как выполненную. "
        "Проверьте результат.\n\n" + render_card(request)
    )
    await notify.send_to_user(bot, request.author, text, initiator_kb(request, request.author))
    if request.author.tg_id:
        await notify.send_attachments(
            bot,
            request.author.tg_id,
            [a for a in request.attachments if a.stage == "after"],
            caption=f"{request.number} — фото результата",
        )
    await notify.notify_admins(session, bot, text, exclude_user_id=request.author_id)


# --------------------------------------------------------------------------- #
# Подтверждение / возврат (п.22-23)
# --------------------------------------------------------------------------- #
@router.callback_query(ReqCB.filter(F.act == "confirm"))
async def act_confirm(
    call: types.CallbackQuery,
    callback_data: ReqCB,
    session: AsyncSession,
    user: Optional[User],
    bot: Bot,
) -> None:
    request = await _get(session, call, callback_data.request_id)
    if request is None:
        return
    if user is None or not user.can_confirm:
        await call.answer(texts.NO_ACCESS, show_alert=True)
        return
    if user.is_executor:
        await call.answer("Исполнитель не может закрывать свою заявку.", show_alert=True)
        return
    if request.status != Status.DONE:
        await call.answer("Заявка не в статусе «Выполнена».", show_alert=True)
        return

    await flow.close_request(session, bot, request, user)
    await session.commit()
    await call.answer("Заявка закрыта")
    await _refresh_card(call, request, user)


@router.callback_query(ReqCB.filter(F.act == "return"))
async def act_return(
    call: types.CallbackQuery,
    callback_data: ReqCB,
    session: AsyncSession,
    state: FSMContext,
    user: Optional[User],
) -> None:
    request = await _get(session, call, callback_data.request_id)
    if request is None:
        return
    if user is None or not user.can_confirm:
        await call.answer(texts.NO_ACCESS, show_alert=True)
        return
    await state.set_state(ManagerFlow.return_reason)
    await state.update_data(request_id=request.id)
    await call.answer()
    await call.message.answer(
        "Укажите <b>причину возврата</b> — это обязательное поле.\n"
        "Исполнитель получит её сразу.",
        reply_markup=cancel_kb(),
    )


@router.message(ManagerFlow.return_reason, F.text)
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
    reason = message.text.strip()
    if len(reason) < 3:
        await message.answer("Опишите причину подробнее.")
        return
    data = await state.get_data()
    request = await load_request(session, data["request_id"])
    await flow.return_request(session, bot, request, user, reason)
    await session.commit()
    await state.clear()
    await message.answer(
        f"🔄 Заявка <b>{esc(request.number)}</b> возвращена на доработку. "
        "Исполнитель уведомлён.",
        reply_markup=main_menu(user),
    )


# --------------------------------------------------------------------------- #
# Комментарии
# --------------------------------------------------------------------------- #
@router.callback_query(ReqCB.filter(F.act == "comment"))
async def act_comment(
    call: types.CallbackQuery,
    callback_data: ReqCB,
    session: AsyncSession,
    state: FSMContext,
    user: Optional[User],
) -> None:
    request = await _get(session, call, callback_data.request_id)
    if request is None:
        return
    await state.set_state(ManagerFlow.comment)
    await state.update_data(request_id=request.id)
    await call.answer()
    await call.message.answer(
        f"Напишите комментарий к заявке <b>{esc(request.number)}</b>:",
        reply_markup=cancel_kb(),
    )


@router.message(ManagerFlow.comment, F.text)
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
    request = await load_request(session, data["request_id"])
    text = message.text.strip()
    session.add(Comment(request_id=request.id, user_id=user.id, text=text))
    if user.is_executor:
        request.executor_comment = text
    elif user.can_confirm:
        request.manager_comment = text
    await history.log(session, request, "comment", user=user, details=text)
    await session.commit()
    await state.clear()
    await message.answer("💬 Комментарий сохранён.", reply_markup=main_menu(user))

    note = (
        f"💬 Новый комментарий к заявке <b>{esc(request.number)}</b>\n"
        f"{esc(user.full_name)}: <i>{esc(text)}</i>"
    )
    await notify.notify_request_watchers(
        session, bot, request, note,
        to_author=True, to_executor=True, exclude_user_id=user.id,
    )


# --------------------------------------------------------------------------- #
# Администратор: назначение, категория, срок, отмена (п.14, п.33)
# --------------------------------------------------------------------------- #
@router.callback_query(ReqCB.filter(F.act == "assign"))
async def act_assign(
    call: types.CallbackQuery,
    callback_data: ReqCB,
    session: AsyncSession,
    user: Optional[User],
) -> None:
    request = await _get(session, call, callback_data.request_id)
    if request is None:
        return
    if user is None or not user.is_admin:
        await call.answer(texts.NO_ACCESS, show_alert=True)
        return

    preferred = await routing.find_executors_for_category(session, request.category_id)
    others = [e for e in await routing.all_executors(session) if e not in preferred]
    executors = preferred + others
    if not executors:
        await call.answer(
            "В системе нет активных исполнителей. Сначала добавьте мастера "
            "в ⚙️ Администрирование.",
            show_alert=True,
        )
        return
    await call.answer()
    await call.message.edit_text(
        f"Выберите исполнителя для заявки <b>{esc(request.number)}</b>\n"
        f"Категория: {esc(request.category.label)}\n\n"
        "⚠️ — мастер ещё не активировал бота, уведомление в личные сообщения не уйдёт.",
        reply_markup=executors_pick_kb(request.id, executors),
    )


@router.callback_query(ReqCB.filter(F.act == "assign_to"))
async def act_assign_to(
    call: types.CallbackQuery,
    callback_data: ReqCB,
    session: AsyncSession,
    user: Optional[User],
    bot: Bot,
) -> None:
    request = await _get(session, call, callback_data.request_id)
    if request is None:
        return
    if user is None or not user.is_admin:
        await call.answer(texts.NO_ACCESS, show_alert=True)
        return
    executor = await session.get(User, int(callback_data.value))
    sent = await flow.assign_executor(session, bot, request, executor, user)
    await session.commit()
    await call.answer("Исполнитель назначен")
    note = "" if sent else "\n\n⚠️ " + texts.EXECUTOR_NOT_REGISTERED
    await call.message.edit_text(
        f"👤 Исполнитель назначен: <b>{esc(executor.full_name)}</b>{note}\n\n"
        + render_card(request),
        reply_markup=card_kb(request, user),
    )


@router.callback_query(ReqCB.filter(F.act == "change_category"))
async def act_change_category(
    call: types.CallbackQuery,
    callback_data: ReqCB,
    session: AsyncSession,
    user: Optional[User],
) -> None:
    request = await _get(session, call, callback_data.request_id)
    if request is None:
        return
    if user is None or not user.is_admin:
        await call.answer(texts.NO_ACCESS, show_alert=True)
        return
    categories = (
        await session.scalars(
            select(Category).where(Category.is_active.is_(True)).order_by(Category.sort_order)
        )
    ).all()
    await call.answer()
    await call.message.edit_text(
        f"Текущая категория: {esc(request.category.label)}\nВыберите новую:",
        reply_markup=categories_pick_kb(request.id, categories),
    )


@router.callback_query(ReqCB.filter(F.act == "set_category"))
async def act_set_category(
    call: types.CallbackQuery,
    callback_data: ReqCB,
    session: AsyncSession,
    user: Optional[User],
    bot: Bot,
) -> None:
    request = await _get(session, call, callback_data.request_id)
    if request is None:
        return
    if user is None or not user.is_admin:
        await call.answer(texts.NO_ACCESS, show_alert=True)
        return
    old_label = request.category.label
    category = await session.get(Category, int(callback_data.value))
    request.category_id = category.id
    request.category = category
    await history.log(
        session, request, "category_changed", user=user,
        details=f"{old_label} → {category.label}",
    )
    await session.flush()
    reason = await flow.dispatch_request(session, bot, request, actor=user)
    await session.commit()
    await call.answer("Категория изменена")
    suffix = {
        "ok": "Заявка направлена исполнителю по новому направлению.",
        "unregistered": texts.EXECUTOR_NOT_REGISTERED,
        "none": "Исполнителя по новому направлению нет — назначьте вручную.",
    }[reason]
    await call.message.edit_text(
        render_card(request) + f"\n\n{suffix}", reply_markup=card_kb(request, user)
    )


@router.callback_query(ReqCB.filter(F.act == "change_due"))
async def act_change_due(
    call: types.CallbackQuery,
    callback_data: ReqCB,
    session: AsyncSession,
    state: FSMContext,
    user: Optional[User],
) -> None:
    request = await _get(session, call, callback_data.request_id)
    if request is None:
        return
    if user is None or not user.is_admin:
        await call.answer(texts.NO_ACCESS, show_alert=True)
        return
    await state.set_state(AdminFlow.assign_pick)
    await state.update_data(request_id=request.id, mode="due_date")
    await call.answer()
    await call.message.answer(
        f"Текущий срок: <b>{fmt_dt(request.due_at)}</b>\nВыберите новую дату:",
        reply_markup=calendar_kb(),
    )


@router.callback_query(AdminFlow.assign_pick, CalendarCB.filter(F.act == "nav"))
async def admin_due_nav(call: types.CallbackQuery, callback_data: CalendarCB) -> None:
    await call.answer()
    await call.message.edit_reply_markup(
        reply_markup=calendar_kb(callback_data.year, callback_data.month)
    )


@router.callback_query(AdminFlow.assign_pick, CalendarCB.filter(F.act == "day"))
async def admin_due_day(
    call: types.CallbackQuery, callback_data: CalendarCB, state: FSMContext
) -> None:
    await state.update_data(
        year=callback_data.year, month=callback_data.month, day=callback_data.day
    )
    await call.answer()
    await call.message.edit_text("Выберите время:", reply_markup=time_kb())


@router.callback_query(AdminFlow.assign_pick, CalendarCB.filter(F.act == "time"))
async def admin_due_time(
    call: types.CallbackQuery,
    callback_data: CalendarCB,
    state: FSMContext,
    session: AsyncSession,
    user: Optional[User],
    bot: Bot,
) -> None:
    data = await state.get_data()
    request = await load_request(session, data["request_id"])
    old_due = request.due_at
    new_local = dt.datetime(
        data["year"], data["month"], data["day"], callback_data.hour, callback_data.minute
    )
    request.due_at = utc_from_local(new_local)
    request.is_overdue = False
    await history.log(
        session, request, "due_changed", user=user,
        details=f"{fmt_dt(old_due)} → {fmt_dt(request.due_at)}",
    )
    await session.commit()
    await state.clear()
    await call.answer("Срок изменён")
    await call.message.edit_text(
        f"📅 Новый срок: <b>{fmt_dt(request.due_at)}</b>\n\n" + render_card(request),
        reply_markup=card_kb(request, user),
    )
    note = (
        f"📅 Срок по заявке <b>{esc(request.number)}</b> изменён администратором: "
        f"{fmt_dt(old_due)} → <b>{fmt_dt(request.due_at)}</b>"
    )
    await notify.notify_request_watchers(
        session, bot, request, note, to_author=True, to_executor=True, exclude_user_id=user.id
    )


@router.callback_query(ReqCB.filter(F.act == "cancel"))
async def act_cancel(
    call: types.CallbackQuery,
    callback_data: ReqCB,
    session: AsyncSession,
    state: FSMContext,
    user: Optional[User],
) -> None:
    request = await _get(session, call, callback_data.request_id)
    if request is None:
        return
    if user is None or not (user.is_admin or request.author_id == user.id):
        await call.answer(texts.NO_ACCESS, show_alert=True)
        return
    await state.set_state(AdminFlow.cancel_reason)
    await state.update_data(request_id=request.id)
    await call.answer()
    await call.message.answer(
        "Укажите <b>причину отмены</b> заявки:", reply_markup=cancel_kb()
    )


@router.message(AdminFlow.cancel_reason, F.text)
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
    request = await load_request(session, data["request_id"])
    await flow.cancel_request(session, bot, request, user, message.text.strip())
    await session.commit()
    await state.clear()
    await message.answer(
        f"🚫 Заявка <b>{esc(request.number)}</b> отменена.", reply_markup=main_menu(user)
    )


# Только состояния этого роутера — состояния админ-панели обрабатывает handlers/admin.py
@router.message(
    StateFilter(ExecutorFlow, ManagerFlow, AdminFlow.cancel_reason, AdminFlow.assign_pick)
)
async def flow_wrong_input(message: types.Message, state: FSMContext) -> None:
    current = await state.get_state()
    if current == ExecutorFlow.done_photo.state:
        await message.answer("Отправьте фото результата или напишите «готово».")
    else:
        await message.answer("Для продолжения отправьте текст сообщением или нажмите «❌ Отмена».")
