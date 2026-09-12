"""Организационные заявки УЦЦП: карточка, журнал, уведомления.

Модуль намеренно отделён от ремонтных заявок (services/flow.py) — данные,
статусы и статистика не смешиваются.
"""
from __future__ import annotations

from typing import List, Optional

from aiogram import Bot
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..db.models import OrgRequest, OrgRequestHistory, OrgStatus, User
from ..utils import esc, fmt_dt, fmt_money
from . import notify

ORG_LOAD_OPTIONS = (
    selectinload(OrgRequest.author),
    selectinload(OrgRequest.assignee),
    selectinload(OrgRequest.outlet),
    selectinload(OrgRequest.brand),
)

ACTION_TITLES = {
    "created": "создал заявку",
    "assigned": "назначил ответственного",
    "reassigned": "сменил ответственного",
    "started": "взял в работу",
    "done": "отметил выполнение",
    "closed": "подтвердил и закрыл",
    "returned": "вернул на доработку",
    "cancelled": "отменил заявку",
    "comment": "добавил комментарий",
    "due_changed": "изменил срок",
    "overdue": "заявка просрочена",
}


async def load(session: AsyncSession, request_id: int) -> Optional[OrgRequest]:
    return await session.scalar(
        select(OrgRequest).where(OrgRequest.id == request_id).options(*ORG_LOAD_OPTIONS)
    )


async def log(
    session: AsyncSession,
    request: OrgRequest,
    action: str,
    user: Optional[User] = None,
    old_status: Optional[str] = None,
    new_status: Optional[str] = None,
    details: Optional[str] = None,
) -> None:
    session.add(
        OrgRequestHistory(
            request_id=request.id,
            user_id=user.id if user else None,
            action=action,
            old_status=old_status,
            new_status=new_status,
            details=details,
        )
    )
    await session.flush()


async def render_history(session: AsyncSession, request_id: int) -> str:
    rows = (
        await session.scalars(
            select(OrgRequestHistory)
            .where(OrgRequestHistory.request_id == request_id)
            .options(selectinload(OrgRequestHistory.user))
            .order_by(OrgRequestHistory.created_at, OrgRequestHistory.id)
        )
    ).all()
    if not rows:
        return "История пуста."
    lines = ["🕓 <b>История изменений</b>"]
    for row in rows:
        who = esc(row.user.full_name) if row.user else "Система"
        what = ACTION_TITLES.get(row.action, row.action)
        line = f"• {fmt_dt(row.created_at)} — {who}: {what}"
        if row.new_status and row.new_status != row.old_status:
            line += f" → {OrgStatus.title(row.new_status)}"
        if row.details:
            line += f"\n   <i>{esc(row.details)}</i>"
        lines.append(line)
    return "\n".join(lines)


def render_card(request: OrgRequest, title: Optional[str] = None) -> str:
    head = title or f"Организационная заявка <b>{esc(request.number)}</b>"
    lines = [
        head,
        f"Статус: <b>{request.status_title}</b>",
        "",
        f"🏢 Объект: {esc(request.object_name)}",
        f"📝 Задача: {esc(request.description)}",
        f"❗️ Приоритет: {request.priority_title}",
        f"📅 Срок: {fmt_dt(request.due_at)}",
        f"👤 Автор: {esc(request.author.full_name) if request.author else '—'}",
        "🎯 Ответственный: "
        + (esc(request.assignee.full_name) if request.assignee else "не назначен"),
        f"🕐 Создана: {fmt_dt(request.created_at)}",
    ]
    if request.done_at:
        lines.append(f"✅ Выполнена: {fmt_dt(request.done_at)}")
    if request.closed_at:
        lines.append(f"🔒 Закрыта: {fmt_dt(request.closed_at)}")
    if request.result_comment:
        lines.append(f"💬 Результат: {esc(request.result_comment)}")
    if request.author_comment:
        lines.append(f"💬 Комментарий автора: {esc(request.author_comment)}")
    if request.cost is not None:
        lines.append(f"💰 Затраты: {fmt_money(request.cost)}")
    return "\n".join(lines)


def scope_condition(user: User):
    """Область видимости организационных заявок — та же логика ролей."""
    if user.is_admin:
        return None
    if user.is_ops_director:
        return (
            or_(OrgRequest.brand_id == user.brand_id, OrgRequest.author_id == user.id)
            if user.brand_id
            else or_(OrgRequest.author_id == user.id, OrgRequest.assignee_id == user.id)
        )
    if user.is_outlet_admin and user.outlet_id:
        return or_(
            OrgRequest.outlet_id == user.outlet_id,
            OrgRequest.author_id == user.id,
            OrgRequest.assignee_id == user.id,
        )
    return or_(OrgRequest.author_id == user.id, OrgRequest.assignee_id == user.id)


def can_view(user: User, request: OrgRequest) -> bool:
    if user.is_admin:
        return True
    if request.author_id == user.id or request.assignee_id == user.id:
        return True
    if user.is_ops_director:
        return request.brand_id == user.brand_id
    if user.is_outlet_admin:
        return request.outlet_id == user.outlet_id
    return False


def can_create(user: User) -> bool:
    """Организационные задачи ставят руководители и администраторы."""
    return user.is_approved and (
        user.is_admin or user.is_ops_director or user.is_outlet_admin
    )


def can_close(user: User, request: OrgRequest) -> bool:
    """Закрывает автор или администратор — ответственный сам себя не закрывает."""
    return user.is_admin or request.author_id == user.id


async def notify_assignee(
    session: AsyncSession, bot: Bot, request: OrgRequest, keyboard=None
) -> bool:
    text = "🗂 <b>Новая организационная заявка УЦЦП</b>\n\n" + render_card(request)
    sent = await notify.send_to_user(bot, request.assignee, text, keyboard)
    if not sent:
        await notify.notify_admins(
            session,
            bot,
            "⚠️ Ответственный по организационной заявке не получил уведомление "
            "(не активировал бота).\n\n" + render_card(request),
        )
    return sent
