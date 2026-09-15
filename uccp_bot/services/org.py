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

from ..db.models import (
    OrgAttachment,
    OrgRequest,
    OrgRequestHistory,
    OrgStatus,
    OrgType,
    RoleCode,
    User,
)
from ..utils import esc, fmt_dt, fmt_money
from . import notify

ORG_LOAD_OPTIONS = (
    selectinload(OrgRequest.author),
    selectinload(OrgRequest.assignee),
    selectinload(OrgRequest.accepted_by),
    selectinload(OrgRequest.outlet),
    selectinload(OrgRequest.brand),
    selectinload(OrgRequest.attachments),
)

ACTION_TITLES = {
    "created": "направил обращение в УЦЦП",
    "accepted": "УЦЦП приняло обращение",
    "assigned": "назначил ответственного",
    "reassigned": "сменил ответственного",
    "started": "взял в работу",
    "done": "отметил выполнение",
    "closed": "подтвердил и закрыл",
    "returned": "вернул на доработку",
    "clarify": "запросил уточнение у автора",
    "clarified": "автор дополнил данные",
    "rejected": "отклонил обращение",
    "cancelled": "отменил обращение",
    "comment": "добавил комментарий",
    "due_changed": "изменил срок",
    "overdue": "обращение просрочено",
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
    head = title or f"<b>ЗАЯВКА В УЦЦП {esc(request.number)}</b>"
    lines = [
        head,
        f"Статус: <b>{request.status_title}</b>",
        "",
        f"Бренд: {esc(request.brand.name) if request.brand else '—'}",
        f"Точка: {esc(request.object_name)}",
        f"Автор: {esc(request.author.full_name) if request.author else '—'}",
        f"Должность: {esc(request.author_position or (request.author.position_text if request.author else '') or '—')}",
        f"Тип: {esc(request.type_title)}",
        f"Запрос: {esc(request.description)}",
    ]
    if request.request_type == OrgType.INVENTORY:
        lines.append(f"Количество: {esc(request.quantity or '—')}")
    if request.reason:
        lines.append(f"Обоснование: {esc(request.reason)}")
    lines.append(f"Срок: {fmt_dt(request.due_at)}")

    files = [a for a in request.attachments if a.stage == "request"]
    lines.append(f"Фото/файл: {'приложено (' + str(len(files)) + ')' if files else 'нет'}")

    if request.accepted_by:
        lines.append(f"\nПринял в УЦЦП: {esc(request.accepted_by.full_name)}")
    lines.append(
        "Ответственный УЦЦП: "
        + (esc(request.assignee.full_name) if request.assignee else "не назначен")
    )
    lines.append(f"Создана: {fmt_dt(request.created_at)}")

    if request.clarify_question and request.status == OrgStatus.CLARIFY:
        lines.append(f"\n❓ УЦЦП запросило уточнение: <i>{esc(request.clarify_question)}</i>")
    if request.clarify_answer:
        lines.append(f"✍️ Ответ автора: {esc(request.clarify_answer)}")
    if request.done_at:
        lines.append(f"✅ Выполнено: {fmt_dt(request.done_at)}")
    if request.result_comment:
        lines.append(f"💬 Результат: {esc(request.result_comment)}")
    if request.reject_reason:
        lines.append(f"❌ Причина отклонения: {esc(request.reject_reason)}")
    if request.closed_at:
        lines.append(f"🔒 Закрыта: {fmt_dt(request.closed_at)}")
    if request.cost is not None:
        lines.append(f"💰 Затраты: {fmt_money(request.cost)}")
    return "\n".join(lines)


def render_preview(data: dict, author: User) -> str:
    """Итоговая карточка до отправки в УЦЦП."""
    files = len(data.get("media", []))
    lines = [
        "<b>ЗАЯВКА В УЦЦП</b>",
        "",
        f"Бренд: {esc(data.get('brand_name') or '—')}",
        f"Точка: {esc(data.get('outlet_name') or '—')}",
        f"Автор: {esc(author.full_name)}",
        f"Должность: {esc(author.position_text or '—')}",
        f"Тип: {esc(OrgType.title(data['request_type']))}",
        f"Запрос: {esc(data.get('description'))}",
    ]
    if data["request_type"] == OrgType.INVENTORY:
        lines.append(f"Количество: {esc(data.get('quantity') or '—')}")
    if data.get("reason"):
        lines.append(f"Обоснование: {esc(data['reason'])}")
    lines += [
        f"Срок: {esc(data.get('due_title') or '—')}",
        f"Фото/файл: {'приложено (' + str(files) + ')' if files else 'не приложено'}",
    ]
    return "\n".join(lines)


def is_uccp(user: User) -> bool:
    """Сотрудник УЦЦП: видит и обрабатывает все обращения точек."""
    return user.is_approved and user.role_code in (RoleCode.UCCP_STAFF, RoleCode.ADMIN)


def scope_condition(user: User):
    """Кто какие обращения видит.

    Сотрудники УЦЦП — все. Точка — только свои (как автор или ответственный).
    Операционный директор — обращения своего бренда.
    """
    if is_uccp(user):
        return None
    if user.is_ops_director and user.brand_id:
        return or_(
            OrgRequest.brand_id == user.brand_id,
            OrgRequest.author_id == user.id,
            OrgRequest.assignee_id == user.id,
        )
    if user.is_outlet_admin and user.outlet_id:
        return or_(
            OrgRequest.outlet_id == user.outlet_id,
            OrgRequest.author_id == user.id,
            OrgRequest.assignee_id == user.id,
        )
    return or_(OrgRequest.author_id == user.id, OrgRequest.assignee_id == user.id)


def can_view(user: User, request: OrgRequest) -> bool:
    if is_uccp(user):
        return True
    if request.author_id == user.id or request.assignee_id == user.id:
        return True
    if user.is_ops_director:
        return request.brand_id == user.brand_id
    if user.is_outlet_admin:
        return request.outlet_id == user.outlet_id
    return False


# Кто вправе обращаться в УЦЦП: управляющие и администраторы точек.
CREATOR_ROLES = (RoleCode.OUTLET_ADMIN, RoleCode.OPS_DIRECTOR, RoleCode.ADMIN)


def can_create(user: User) -> bool:
    return user.is_approved and user.role_code in CREATOR_ROLES


def can_process(user: User) -> bool:
    """Принимать, назначать и отклонять может только УЦЦП."""
    return is_uccp(user)


def can_close(user: User, request: OrgRequest) -> bool:
    """Закрывает автор обращения или сотрудник УЦЦП."""
    return is_uccp(user) or request.author_id == user.id


async def uccp_staff(session: AsyncSession) -> List[User]:
    """Сотрудники УЦЦП, которым уходят новые обращения."""
    from ..db.models import UserStatus

    rows = (
        await session.scalars(
            select(User)
            .where(
                User.role_code.in_((RoleCode.UCCP_STAFF, RoleCode.ADMIN)),
                User.status == UserStatus.ACTIVE,
                User.tg_id.is_not(None),
            )
            .order_by(User.role_code, User.id)
        )
    ).all()
    return list(rows)


async def notify_uccp(
    session: AsyncSession, bot: Bot, request: OrgRequest, keyboard=None
) -> int:
    """Новое обращение уходит в УЦЦП, а не мастерам и не управляющему точки."""
    import asyncio

    text = "🗂 <b>Новое обращение в УЦЦП</b>\n\n" + render_card(request)
    targets = await uccp_staff(session)
    if not targets:
        log.warning("Нет ни одного сотрудника УЦЦП — обращение %s ждёт", request.number)
        return 0
    results = await asyncio.gather(
        *(notify.send_to_user(bot, person, text, keyboard) for person in targets),
        return_exceptions=True,
    )
    return sum(1 for r in results if r is True)


async def notify_assignee(
    session: AsyncSession, bot: Bot, request: OrgRequest, keyboard=None
) -> bool:
    """Уведомление назначенному ответственному внутри УЦЦП."""
    text = "🎯 <b>Вас назначили ответственным по обращению УЦЦП</b>\n\n" + render_card(request)
    sent = await notify.send_to_user(bot, request.assignee, text, keyboard)
    if not sent:
        await notify.notify_admins(
            session,
            bot,
            "⚠️ Ответственный по обращению УЦЦП не получил уведомление "
            "(не активировал бота).\n\n" + render_card(request),
        )
    return sent


async def send_attachments(bot: Bot, chat_id: int, request: OrgRequest, stage: str = "request"):
    """Пересылаем приложенные фото, видео и документы."""
    from aiogram.exceptions import TelegramAPIError

    for att in [a for a in request.attachments if a.stage == stage]:
        try:
            if att.media_type == "video":
                await bot.send_video(chat_id, att.file_id, caption=request.number)
            elif att.media_type == "document":
                await bot.send_document(chat_id, att.file_id, caption=request.number)
            else:
                await bot.send_photo(chat_id, att.file_id, caption=request.number)
        except TelegramAPIError as exc:
            log.warning("Не удалось переслать вложение: %s", exc)
