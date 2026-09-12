"""Жизненный цикл заявки: маршрутизация, смена статусов, уведомления.

Главное правило (п.49): отсутствие исполнителя, группы или регистрации
никогда не приводит к потере заявки — она уходит администратору.
"""
from __future__ import annotations

import datetime as dt
from typing import Optional

from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import Request, Status, User
from ..keyboards.request import admin_assign_kb, executor_kb, initiator_kb
from ..utils import esc, fmt_dt, fmt_money, utcnow
from . import history, notify, routing
from .cards import render_card


async def dispatch_request(
    session: AsyncSession, bot: Bot, request: Request, actor: Optional[User] = None
) -> str:
    """Определяет исполнителя и рассылает уведомления. Возвращает код маршрута."""
    executor, reason = await routing.resolve_route(
        session, request.category_id, request.brand_id, request.outlet_id
    )
    old_status = request.status

    if reason == "ok":
        request.executor_id = executor.id
        request.executor = executor
        request.status = Status.NEW
        await history.log(
            session,
            request,
            "auto_assigned",
            user=actor,
            old_status=old_status,
            new_status=request.status,
            details=f"Исполнитель: {executor.full_name}",
        )
        await session.flush()
        text = "🔔 <b>Новая заявка</b>\n\n" + render_card(request)
        sent = await notify.send_to_user(bot, executor, text, executor_kb(request))
        if sent and executor.tg_id:
            await notify.send_attachments(
                bot, executor.tg_id, [a for a in request.attachments if a.stage == "before"]
            )
        if not sent:
            await _escalate(
                session,
                bot,
                request,
                "Исполнитель назначен, но уведомление не доставлено (бот заблокирован "
                "или чат не начат). Требуется контроль.",
            )
        return reason

    if reason == "unregistered":
        request.executor_id = executor.id
        request.executor = executor
        request.status = Status.AWAITING_ASSIGNMENT
        await history.log(
            session,
            request,
            "escalated",
            user=actor,
            old_status=old_status,
            new_status=request.status,
            details=(
                f"Исполнитель {executor.full_name} ещё не зарегистрирован в боте. "
                "Личное уведомление не отправлено."
            ),
        )
        await session.flush()
        await _escalate(
            session,
            bot,
            request,
            f"Исполнитель <b>{esc(executor.full_name)}</b> ещё не зарегистрирован в боте. "
            "Личное уведомление не отправлено. Заявка передана администратору для контроля.",
        )
        return reason

    request.status = Status.AWAITING_ASSIGNMENT
    await history.log(
        session,
        request,
        "escalated",
        user=actor,
        old_status=old_status,
        new_status=request.status,
        details="По направлению нет активного исполнителя.",
    )
    await session.flush()
    await _escalate(
        session,
        bot,
        request,
        "По данному направлению нет активного исполнителя.",
    )
    return reason


async def _escalate(session: AsyncSession, bot: Bot, request: Request, reason: str) -> None:
    text = (
        "⚠️ <b>Требуется назначить исполнителя</b>\n"
        f"{reason}\n\n" + render_card(request)
    )
    await notify.notify_admins(session, bot, text, admin_assign_kb(request))


async def assign_executor(
    session: AsyncSession, bot: Bot, request: Request, executor: User, actor: User
) -> bool:
    """Ручное назначение/перенаправление администратором. True — если мастер уведомлён."""
    old_status = request.status
    old_executor = request.executor
    request.executor_id = executor.id
    request.executor = executor
    request.status = Status.NEW
    request.accepted_at = None
    action = "reassigned" if old_executor and old_executor.id != executor.id else "assigned"
    await history.log(
        session,
        request,
        action,
        user=actor,
        old_status=old_status,
        new_status=request.status,
        details=f"Исполнитель: {executor.full_name}",
    )
    await session.flush()

    text = "🔔 <b>Новая заявка</b>\n\n" + render_card(request)
    sent = await notify.send_to_user(bot, executor, text, executor_kb(request))
    if sent and executor.tg_id:
        await notify.send_attachments(
            bot, executor.tg_id, [a for a in request.attachments if a.stage == "before"]
        )
    if old_executor and old_executor.id != executor.id:
        await notify.send_to_user(
            bot,
            old_executor,
            f"ℹ️ Заявка <b>{esc(request.number)}</b> передана другому исполнителю.",
        )
    await notify.send_to_user(
        bot,
        request.author,
        f"👷 По заявке <b>{esc(request.number)}</b> назначен исполнитель: "
        f"<b>{esc(executor.full_name)}</b>.",
    )
    return sent


async def mark_overdue(session: AsyncSession, bot: Bot, request: Request) -> None:
    request.is_overdue = True
    request.overdue_notified_at = utcnow()
    await history.log(
        session, request, "overdue", details=f"Срок: {fmt_dt(request.due_at)}"
    )
    text = (
        f"🔴 <b>Заявка просрочена</b>\n"
        f"{esc(request.number)} · {esc(request.outlet.name)}\n"
        f"Срок был: {fmt_dt(request.due_at)}\n"
        f"Текущий статус: {Status.title(request.status)}\n\n"
        f"📝 {esc(request.description)}"
    )
    await notify.send_to_user(bot, request.executor, text)
    await notify.notify_admins(session, bot, text)
    await notify.send_to_user(bot, request.author, text)


async def close_request(
    session: AsyncSession, bot: Bot, request: Request, actor: User
) -> None:
    """Подтверждение → Закрыта (п.23)."""
    old_status = request.status
    now = utcnow()
    request.confirmed_at = now
    request.status = Status.CONFIRMED
    await history.log(
        session, request, "confirmed", user=actor, old_status=old_status,
        new_status=Status.CONFIRMED,
    )
    request.status = Status.CLOSED
    request.closed_at = now
    await history.log(
        session, request, "closed", user=actor, old_status=Status.CONFIRMED,
        new_status=Status.CLOSED,
    )
    await session.flush()

    text = (
        f"🔒 Заявка <b>{esc(request.number)}</b> подтверждена и закрыта.\n"
        f"Проверил: {esc(actor.full_name)}\n"
        f"Стоимость: {fmt_money(request.total_cost)}"
    )
    await notify.send_to_user(bot, request.executor, text)
    if request.author_id != actor.id:
        await notify.send_to_user(bot, request.author, text)


async def return_request(
    session: AsyncSession, bot: Bot, request: Request, actor: User, reason: str
) -> None:
    old_status = request.status
    request.status = Status.RETURNED
    request.return_reason = reason
    request.done_at = None
    await history.log(
        session, request, "returned", user=actor, old_status=old_status,
        new_status=Status.RETURNED, details=reason,
    )
    await session.flush()
    await notify.send_to_user(
        bot,
        request.executor,
        f"🔄 Заявка <b>{esc(request.number)}</b> возвращена на доработку.\n"
        f"Причина: <i>{esc(reason)}</i>\n\n"
        "Исправьте замечание и снова нажмите «Работа выполнена».",
        executor_kb(request),
    )


async def cancel_request(
    session: AsyncSession, bot: Bot, request: Request, actor: User, reason: str
) -> None:
    old_status = request.status
    request.status = Status.CANCELLED
    request.closed_at = utcnow()
    await history.log(
        session, request, "cancelled", user=actor, old_status=old_status,
        new_status=Status.CANCELLED, details=reason,
    )
    await session.flush()
    text = (
        f"🚫 Заявка <b>{esc(request.number)}</b> отменена.\n"
        f"Кто: {esc(actor.full_name)}\nПричина: <i>{esc(reason)}</i>"
    )
    await notify.send_to_user(bot, request.executor, text)
    if request.author_id != actor.id:
        await notify.send_to_user(bot, request.author, text)
