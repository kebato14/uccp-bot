"""Отчёты УЦЦП (п.34-п.37)."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..config import config
from ..db.models import Brand, Category, Outlet, Request, Status, User
from ..utils import esc, fmt_money, now_local, utc_from_local


@dataclass
class ReportFilters:
    date_from: Optional[dt.date] = None
    date_to: Optional[dt.date] = None
    brand_id: Optional[int] = None
    outlet_id: Optional[int] = None
    category_id: Optional[int] = None
    executor_id: Optional[int] = None
    status: Optional[str] = None
    priority: Optional[str] = None
    only_overdue: bool = False
    period_title: str = "за всё время"

    def describe(self) -> str:
        parts = [self.period_title]
        if self.only_overdue:
            parts.append("только просроченные")
        return ", ".join(parts)


PERIODS = {
    "today": "сегодня",
    "yesterday": "вчера",
    "week": "за неделю",
    "month": "за месяц",
    "all": "за всё время",
}


def period_range(code: str) -> Tuple[Optional[dt.date], Optional[dt.date], str]:
    today = now_local().date()
    if code == "today":
        return today, today, "за сегодня"
    if code == "yesterday":
        day = today - dt.timedelta(days=1)
        return day, day, "за вчера"
    if code == "week":
        start = today - dt.timedelta(days=today.weekday())
        return start, today, "за текущую неделю"
    if code == "month":
        start = today.replace(day=1)
        return start, today, "за текущий месяц"
    return None, None, "за всё время"


def apply_filters(stmt, f: ReportFilters):
    if f.date_from:
        stmt = stmt.where(
            Request.created_at >= utc_from_local(dt.datetime.combine(f.date_from, dt.time.min))
        )
    if f.date_to:
        stmt = stmt.where(
            Request.created_at <= utc_from_local(dt.datetime.combine(f.date_to, dt.time.max))
        )
    if f.brand_id:
        stmt = stmt.where(Request.brand_id == f.brand_id)
    if f.outlet_id:
        stmt = stmt.where(Request.outlet_id == f.outlet_id)
    if f.category_id:
        stmt = stmt.where(Request.category_id == f.category_id)
    if f.executor_id:
        stmt = stmt.where(Request.executor_id == f.executor_id)
    if f.status:
        stmt = stmt.where(Request.status == f.status)
    if f.priority:
        stmt = stmt.where(Request.priority == f.priority)
    if f.only_overdue:
        stmt = stmt.where(Request.is_overdue.is_(True))
    return stmt


async def fetch_requests(session: AsyncSession, f: ReportFilters) -> List[Request]:
    stmt = select(Request).options(
        selectinload(Request.brand),
        selectinload(Request.outlet),
        selectinload(Request.category),
        selectinload(Request.author),
        selectinload(Request.executor),
    )
    stmt = apply_filters(stmt, f).order_by(Request.created_at.asc())
    return list((await session.scalars(stmt)).all())


@dataclass
class ReportData:
    total: int = 0
    closed: int = 0
    in_work: int = 0
    overdue: int = 0
    cancelled: int = 0
    awaiting: int = 0
    total_cost: float = 0.0
    avg_cost: float = 0.0
    by_brand: Dict[str, Tuple[int, float]] = field(default_factory=dict)
    by_outlet: Dict[str, Tuple[int, float]] = field(default_factory=dict)
    by_executor: Dict[str, Tuple[int, float]] = field(default_factory=dict)
    by_category: Dict[str, Tuple[int, float]] = field(default_factory=dict)


def _bump(store: Dict[str, Tuple[int, float]], key: str, cost: float) -> None:
    count, total = store.get(key, (0, 0.0))
    store[key] = (count + 1, total + cost)


async def build_report(session: AsyncSession, f: ReportFilters) -> ReportData:
    requests = await fetch_requests(session, f)
    data = ReportData(total=len(requests))
    paid = 0

    for req in requests:
        cost = req.total_cost
        if req.status in (Status.CLOSED, Status.CONFIRMED):
            data.closed += 1
        elif req.status == Status.CANCELLED or req.status == Status.REJECTED:
            data.cancelled += 1
        else:
            data.in_work += 1
        if req.status == Status.AWAITING_ASSIGNMENT:
            data.awaiting += 1
        if req.is_overdue and req.status in Status.OPEN:
            data.overdue += 1

        data.total_cost += cost
        if cost > 0:
            paid += 1

        _bump(data.by_brand, req.brand.name, cost)
        _bump(data.by_outlet, req.outlet.name, cost)
        _bump(data.by_category, req.category.name, cost)
        _bump(
            data.by_executor,
            req.executor.full_name if req.executor else "не назначен",
            cost,
        )

    data.avg_cost = data.total_cost / paid if paid else 0.0
    return data


def _section(title: str, store: Dict[str, Tuple[int, float]], limit: int = 10) -> List[str]:
    if not store:
        return []
    rows = sorted(store.items(), key=lambda kv: (-kv[1][0], kv[0]))[:limit]
    lines = ["", f"<b>{title}</b>"]
    for name, (count, cost) in rows:
        suffix = f" · {fmt_money(cost)}" if cost else ""
        lines.append(f"• {esc(name)}: {count}{suffix}")
    return lines


def render_report(data: ReportData, f: ReportFilters) -> str:
    lines = [
        f"📊 <b>Отчёт УЦЦП {esc(f.describe())}</b>",
        "",
        f"Всего заявок: <b>{data.total}</b>",
        f"Выполнено и закрыто: <b>{data.closed}</b>",
        f"В работе: <b>{data.in_work}</b>",
        f"Ожидают назначения: <b>{data.awaiting}</b>",
        f"Просрочено: <b>{data.overdue}</b>",
        f"Отменено / отклонено: <b>{data.cancelled}</b>",
        "",
        f"Общая стоимость: <b>{fmt_money(data.total_cost)}</b>",
        f"Средняя стоимость заявки: <b>{fmt_money(data.avg_cost)}</b>",
    ]
    lines += _section("По брендам", data.by_brand)
    lines += _section("По точкам", data.by_outlet)
    lines += _section("По исполнителям", data.by_executor)
    lines += _section("По категориям", data.by_category)
    if data.total == 0:
        lines.append("\nЗа выбранный период заявок нет.")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Организационные заявки УЦЦП — отдельная статистика
# --------------------------------------------------------------------------- #
@dataclass
class OrgReportData:
    total: int = 0
    closed: int = 0
    in_work: int = 0
    overdue: int = 0
    cancelled: int = 0
    total_cost: float = 0.0
    by_object: Dict[str, Tuple[int, float]] = field(default_factory=dict)
    by_assignee: Dict[str, Tuple[int, float]] = field(default_factory=dict)
    by_author: Dict[str, Tuple[int, float]] = field(default_factory=dict)
    by_brand: Dict[str, Tuple[int, float]] = field(default_factory=dict)


def apply_org_filters(stmt, f: ReportFilters):
    from ..db.models import OrgRequest

    if f.date_from:
        stmt = stmt.where(
            OrgRequest.created_at
            >= utc_from_local(dt.datetime.combine(f.date_from, dt.time.min))
        )
    if f.date_to:
        stmt = stmt.where(
            OrgRequest.created_at
            <= utc_from_local(dt.datetime.combine(f.date_to, dt.time.max))
        )
    if f.brand_id:
        stmt = stmt.where(OrgRequest.brand_id == f.brand_id)
    if f.outlet_id:
        stmt = stmt.where(OrgRequest.outlet_id == f.outlet_id)
    if f.executor_id:
        stmt = stmt.where(OrgRequest.assignee_id == f.executor_id)
    if f.priority:
        stmt = stmt.where(OrgRequest.priority == f.priority)
    if f.only_overdue:
        stmt = stmt.where(OrgRequest.is_overdue.is_(True))
    return stmt


async def fetch_org_requests(session: AsyncSession, f: ReportFilters) -> List:
    from ..db.models import OrgRequest
    from .org import ORG_LOAD_OPTIONS

    stmt = select(OrgRequest).options(*ORG_LOAD_OPTIONS)
    stmt = apply_org_filters(stmt, f).order_by(OrgRequest.created_at.asc())
    return list((await session.scalars(stmt)).all())


async def build_org_report(session: AsyncSession, f: ReportFilters) -> OrgReportData:
    from ..db.models import OrgStatus

    requests = await fetch_org_requests(session, f)
    data = OrgReportData(total=len(requests))

    for req in requests:
        cost = req.total_cost
        if req.status == OrgStatus.CLOSED:
            data.closed += 1
        elif req.status == OrgStatus.CANCELLED:
            data.cancelled += 1
        else:
            data.in_work += 1
        if req.is_overdue and req.status in OrgStatus.OPEN:
            data.overdue += 1
        data.total_cost += cost

        _bump(data.by_object, req.object_name, cost)
        _bump(
            data.by_assignee,
            req.assignee.full_name if req.assignee else "не назначен",
            cost,
        )
        _bump(data.by_author, req.author.full_name if req.author else "—", cost)
        _bump(data.by_brand, req.brand.name if req.brand else "вне брендов", cost)

    return data


def render_org_report(data: OrgReportData, f: ReportFilters) -> str:
    lines = [
        f"🗂 <b>Организационные заявки УЦЦП {esc(f.describe())}</b>",
        "",
        f"Всего заявок: <b>{data.total}</b>",
        f"Закрыто: <b>{data.closed}</b>",
        f"В работе: <b>{data.in_work}</b>",
        f"Просрочено: <b>{data.overdue}</b>",
        f"Отменено: <b>{data.cancelled}</b>",
        "",
        f"Затраты: <b>{fmt_money(data.total_cost)}</b>",
    ]
    lines += _section("По объектам", data.by_object)
    lines += _section("По ответственным", data.by_assignee)
    lines += _section("По авторам", data.by_author)
    if data.total == 0:
        lines.append("\nЗа выбранный период организационных заявок нет.")
    return "\n".join(lines)


def render_combined_report(
    tech: ReportData, org_data: OrgReportData, f: ReportFilters
) -> str:
    """Сводный отчёт для администратора: два модуля рядом, но раздельно."""
    grand_total = tech.total + org_data.total
    grand_cost = tech.total_cost + org_data.total_cost
    return "\n".join(
        [
            f"📋 <b>Сводный отчёт УЦЦП {esc(f.describe())}</b>",
            "",
            "🔧 <b>Технические и ремонтные заявки</b>",
            f"• Всего: <b>{tech.total}</b>",
            f"• Выполнено и закрыто: {tech.closed}",
            f"• В работе: {tech.in_work}",
            f"• Ожидают назначения: {tech.awaiting}",
            f"• Просрочено: {tech.overdue}",
            f"• Отменено / отклонено: {tech.cancelled}",
            f"• Расходы: <b>{fmt_money(tech.total_cost)}</b>",
            "",
            "🗂 <b>Организационные заявки УЦЦП</b>",
            f"• Всего: <b>{org_data.total}</b>",
            f"• Закрыто: {org_data.closed}",
            f"• В работе: {org_data.in_work}",
            f"• Просрочено: {org_data.overdue}",
            f"• Отменено: {org_data.cancelled}",
            f"• Затраты: <b>{fmt_money(org_data.total_cost)}</b>",
            "",
            "➕ <b>Итого по обоим модулям</b>",
            f"• Заявок: <b>{grand_total}</b>",
            f"• Общие расходы: <b>{fmt_money(grand_cost)}</b>",
            "",
            "<i>Статистика модулей ведётся раздельно — этот отчёт лишь "
            "показывает их рядом.</i>",
        ]
    )
