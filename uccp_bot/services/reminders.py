"""Напоминания о незакрытых задачах.

Каждому человеку уходит одна личная сводка только по тем задачам, где
ход за ним — а не поток отдельных сообщений по каждой заявке.

Кто за что отвечает:
  • исполнитель — принять заявку, выполнить, доделать после возврата;
  • инициатор — подтвердить выполненную работу и закрыть заявку;
  • главный администратор — назначить исполнителя и согласовать стоимость;
  • сотрудник УЦЦП — обработать обращения точек.
"""
from __future__ import annotations

import datetime as dt
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import (
    CostStatus,
    OrgRequest,
    OrgStatus,
    Request,
    RoleCode,
    Status,
    User,
    UserStatus,
)
from ..services import org as org_service
from ..services.cards import REQUEST_LOAD_OPTIONS
from ..utils import esc, fmt_dt, now_local, utcnow


@dataclass
class Digest:
    """Что напомнить одному человеку."""

    user: User
    blocks: Dict[str, List[str]] = field(default_factory=lambda: defaultdict(list))
    total: int = 0

    def add(self, block: str, line: str) -> None:
        self.blocks[block].append(line)
        self.total += 1

    def render(self) -> str:
        head = (
            "🔔 <b>Незакрытые задачи</b>\n"
            f"На вас числится: <b>{self.total}</b>\n"
        )
        parts = [head]
        for title, lines in self.blocks.items():
            parts.append(f"\n<b>{title}</b>")
            parts.extend(lines[:15])
            if len(lines) > 15:
                parts.append(f"<i>…и ещё {len(lines) - 15}</i>")
        parts.append(
            "\nОткройте нужную заявку через меню бота и завершите действие. "
            "Если работа уже сделана — отметьте это, чтобы заявка не висела "
            "в просроченных."
        )
        return "\n".join(parts)


def _age(moment: Optional[dt.datetime]) -> str:
    if moment is None:
        return ""
    days = (utcnow() - moment).days
    if days <= 0:
        return "сегодня"
    if days == 1:
        return "1 день назад"
    if days < 5:
        return f"{days} дня назад"
    return f"{days} дней назад"


def _line(req: Request, extra: str = "") -> str:
    overdue = " 🔴 просрочена" if req.is_overdue else ""
    tail = f" · {extra}" if extra else ""
    return (
        f"• <b>{esc(req.number)}</b> — {esc(req.outlet.name if req.outlet else '—')}"
        f" · {esc(req.category.name if req.category else '—')}\n"
        f"  срок {fmt_dt(req.due_at)}{overdue}{tail}"
    )


async def build_digests(session: AsyncSession) -> Dict[int, Digest]:
    """Собирает персональные сводки по всем, за кем числятся незакрытые задачи."""
    digests: Dict[int, Digest] = {}

    def digest_for(user: Optional[User]) -> Optional[Digest]:
        if user is None or not user.tg_id or user.status != UserStatus.ACTIVE:
            return None
        if user.id not in digests:
            digests[user.id] = Digest(user=user)
        return digests[user.id]

    open_requests = list(
        (
            await session.scalars(
                select(Request)
                .where(Request.status.in_(Status.OPEN))
                .options(*REQUEST_LOAD_OPTIONS)
                .order_by(Request.due_at)
            )
        ).all()
    )

    admins = list(
        (
            await session.scalars(
                select(User).where(
                    User.role_code == RoleCode.ADMIN,
                    User.status == UserStatus.ACTIVE,
                    User.tg_id.is_not(None),
                )
            )
        ).all()
    )

    for req in open_requests:
        # 1. ход за исполнителем
        if req.status == Status.NEW and req.executor:
            if (d := digest_for(req.executor)) is not None:
                d.add("⏳ Ждут вашего решения — примите или отклоните", _line(req))
        elif req.status in (Status.ACCEPTED, Status.IN_PROGRESS) and req.executor:
            if (d := digest_for(req.executor)) is not None:
                d.add("🔧 В работе — завершите и отметьте выполнение", _line(req))
        elif req.status == Status.RETURNED and req.executor:
            if (d := digest_for(req.executor)) is not None:
                d.add("🔄 Возвращены на доработку", _line(req))

        # 2. ход за инициатором: работа сделана, но заявка не закрыта
        elif req.status == Status.DONE:
            if (d := digest_for(req.author)) is not None:
                d.add(
                    "✅ Выполнены — подтвердите и закройте",
                    _line(req, f"выполнена {_age(req.done_at)}"),
                )

        # 3. предложенный срок ждёт решения инициатора
        if req.status == Status.RESCHEDULE_PROPOSED:
            if (d := digest_for(req.author)) is not None:
                d.add(
                    "📅 Исполнитель просит перенести срок",
                    _line(req, f"предложено {fmt_dt(req.proposed_due_at)}"),
                )

        # 4. без исполнителя — к администратору
        if req.status == Status.AWAITING_ASSIGNMENT:
            for admin in admins:
                if (d := digest_for(admin)) is not None:
                    d.add("👤 Нужно назначить исполнителя", _line(req))

        # 5. стоимость ждёт согласования
        if req.cost_status == CostStatus.PENDING:
            for admin in admins:
                if (d := digest_for(admin)) is not None:
                    d.add(
                        "💰 Стоимость на согласовании",
                        _line(req, f"заявлена {_age(req.cost_submitted_at)}"),
                    )

    # 6. обращения в УЦЦП
    org_rows = list(
        (
            await session.scalars(
                select(OrgRequest)
                .where(OrgRequest.status.in_(OrgStatus.OPEN))
                .options(*org_service.ORG_LOAD_OPTIONS)
                .order_by(OrgRequest.due_at)
            )
        ).all()
    )
    uccp = await org_service.uccp_staff(session)

    for req in org_rows:
        line = (
            f"• <b>{esc(req.number)}</b> — {esc(req.object_name)}\n"
            f"  {esc(req.type_title)} · срок {fmt_dt(req.due_at)}"
            + (" 🔴 просрочено" if req.is_overdue else "")
        )
        if req.status == OrgStatus.NEW:
            for person in uccp:
                if (d := digest_for(person)) is not None:
                    d.add("🗂 Новые обращения в УЦЦП — примите в работу", line)
        elif req.status == OrgStatus.CLARIFY:
            if (d := digest_for(req.author)) is not None:
                d.add("❓ УЦЦП ждёт от вас уточнения", line)
        elif req.status in (OrgStatus.ACCEPTED, OrgStatus.ASSIGNED, OrgStatus.IN_PROGRESS):
            if (d := digest_for(req.assignee)) is not None:
                d.add("🗂 Обращения в работе", line)
        elif req.status == OrgStatus.DONE:
            if (d := digest_for(req.author)) is not None:
                d.add("🗂 Выполнены — подтвердите и закройте", line)

    return digests


def render_summary(digests: Dict[int, Digest]) -> str:
    """Короткая сводка для администратора: кому и сколько напомнили."""
    if not digests:
        return "🔔 Незакрытых задач нет — напоминать некому."
    rows = sorted(digests.values(), key=lambda d: -d.total)
    lines = [
        "🔔 <b>Напоминание о незакрытых задачах</b>",
        f"Получателей: <b>{len(rows)}</b> · задач всего: "
        f"<b>{sum(d.total for d in rows)}</b>",
        "",
    ]
    for d in rows:
        lines.append(f"• {esc(d.user.full_name)} ({esc(d.user.role_title)}) — {d.total}")
    return "\n".join(lines)
