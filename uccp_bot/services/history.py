"""Журнал действий по заявке (п.24). Записи не удаляются."""
from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..db.models import Request, RequestStatusHistory, Status, User
from ..utils import esc, fmt_dt

ACTION_TITLES = {
    "created": "создал заявку",
    "auto_assigned": "система назначила исполнителя",
    "assigned": "назначил исполнителя",
    "reassigned": "перенаправил заявку",
    "accepted": "принял заявку",
    "rejected": "отклонил заявку",
    "propose_due": "предложил новый срок",
    "due_approved": "согласовал новый срок",
    "due_declined": "отклонил новый срок",
    "started": "начал работу",
    "done": "отметил выполнение",
    "confirmed": "подтвердил выполнение",
    "returned": "вернул на доработку",
    "closed": "закрыл заявку",
    "cancelled": "отменил заявку",
    "comment": "добавил комментарий",
    "category_changed": "изменил категорию",
    "due_changed": "изменил срок",
    "overdue": "заявка просрочена",
    "escalated": "передана администратору",
}


async def log(
    session: AsyncSession,
    request: Request,
    action: str,
    user: Optional[User] = None,
    old_status: Optional[str] = None,
    new_status: Optional[str] = None,
    details: Optional[str] = None,
) -> RequestStatusHistory:
    entry = RequestStatusHistory(
        request_id=request.id,
        user_id=user.id if user else None,
        action=action,
        old_status=old_status,
        new_status=new_status,
        details=details,
    )
    session.add(entry)
    await session.flush()
    return entry


async def render_history(session: AsyncSession, request_id: int, limit: int = 40) -> str:
    rows = (
        await session.scalars(
            select(RequestStatusHistory)
            .where(RequestStatusHistory.request_id == request_id)
            .options(selectinload(RequestStatusHistory.user))
            .order_by(RequestStatusHistory.created_at.asc(), RequestStatusHistory.id.asc())
            .limit(limit)
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
            line += f" → {Status.title(row.new_status)}"
        if row.details:
            line += f"\n   <i>{esc(row.details)}</i>"
        lines.append(line)
    return "\n".join(lines)
