"""Карточка заявки (п.16, п.47)."""
from __future__ import annotations

from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..db.models import Attachment, Comment, Request, Status
from ..utils import esc, fmt_dt, fmt_money

REQUEST_LOAD_OPTIONS = (
    selectinload(Request.author),
    selectinload(Request.executor),
    selectinload(Request.brand),
    selectinload(Request.outlet),
    selectinload(Request.category),
    selectinload(Request.equipment),
    selectinload(Request.attachments),
)


async def load_request(session: AsyncSession, request_id: int) -> Optional[Request]:
    return await session.scalar(
        select(Request).where(Request.id == request_id).options(*REQUEST_LOAD_OPTIONS)
    )


def media_summary(request: Request, stage: str = "before") -> str:
    items = [a for a in request.attachments if a.stage == stage]
    if not items:
        return "нет"
    photos = sum(1 for a in items if a.media_type == "photo")
    videos = sum(1 for a in items if a.media_type == "video")
    parts = []
    if photos:
        parts.append(f"фото — {photos}")
    if videos:
        parts.append(f"видео — {videos}")
    return ", ".join(parts) if parts else f"файлов — {len(items)}"


def render_card(
    request: Request,
    *,
    full: bool = True,
    title: Optional[str] = None,
) -> str:
    head = title or f"Заявка <b>{esc(request.number)}</b>"
    lines = [
        head,
        f"Статус: <b>{request.status_title}</b>",
        "",
        f"🏷 Бренд: {esc(request.brand.name)}",
        f"🏬 Точка: {esc(request.outlet.name)}",
        f"🗂 Категория: {esc(request.category.label)}",
    ]
    if request.equipment:
        lines.append(f"⚙️ Оборудование: {esc(request.equipment.name)}")
    lines += [
        f"📝 Проблема: {esc(request.description)}",
        f"❗️ Приоритет: {request.priority_title}",
        f"📅 Срок: {fmt_dt(request.due_at)}",
        f"📎 Фото/видео: {media_summary(request, 'before')}",
    ]
    if full:
        lines += [
            "",
            f"👤 Инициатор: {esc(request.author.full_name)}"
            + (f" ({esc(request.author.phone)})" if request.author.phone else ""),
            f"👷 Исполнитель: "
            + (esc(request.executor.full_name) if request.executor else "не назначен"),
            f"🕐 Создана: {fmt_dt(request.created_at)}",
        ]
        if request.proposed_due_at and request.status == Status.RESCHEDULE_PROPOSED:
            lines.append(f"📅 Предложен новый срок: <b>{fmt_dt(request.proposed_due_at)}</b>")
        if request.started_at:
            lines.append(f"▶️ Начата: {fmt_dt(request.started_at)}")
        if request.done_at:
            lines.append(f"✅ Выполнена: {fmt_dt(request.done_at)}")
        if request.closed_at:
            lines.append(f"🔒 Закрыта: {fmt_dt(request.closed_at)}")
        if request.executor_comment:
            lines.append(f"💬 Комментарий исполнителя: {esc(request.executor_comment)}")
        if request.manager_comment:
            lines.append(f"💬 Комментарий менеджера: {esc(request.manager_comment)}")
        if request.reject_reason:
            lines.append(f"❌ Причина отклонения: {esc(request.reject_reason)}")
        if request.return_reason:
            lines.append(f"🔄 Причина возврата: {esc(request.return_reason)}")
        if request.work_cost is not None or request.material_cost is not None:
            lines += [
                "",
                f"💰 Работы: {fmt_money(request.work_cost)}",
                f"🧰 Материалы: {fmt_money(request.material_cost)}",
                f"Σ Итого: <b>{fmt_money(request.total_cost)}</b>",
            ]
        after = media_summary(request, "after")
        if after != "нет":
            lines.append(f"📸 Фото результата: {after}")
    return "\n".join(lines)


def render_preview(data: dict, executor_label: str) -> str:
    """Предпросмотр до сохранения в БД (п.16)."""
    media_count = len(data.get("media", []))
    media_text = f"прикреплено ({media_count})" if media_count else "не прикреплено"
    return "\n".join(
        [
            "<b>Проверьте заявку</b>",
            "",
            f"🏷 Бренд: {esc(data.get('brand_name'))}",
            f"🏬 Точка: {esc(data.get('outlet_name'))}",
            f"🗂 Категория: {esc(data.get('category_name'))}",
            f"📝 Проблема: {esc(data.get('description'))}",
            f"❗️ Приоритет: {esc(data.get('priority_title'))}",
            f"📅 Срок: {esc(data.get('due_title'))}",
            f"📎 Фото/видео: {media_text}",
            f"👷 Исполнитель: {esc(executor_label)}",
        ]
    )


async def render_comments(session: AsyncSession, request_id: int) -> str:
    rows = (
        await session.scalars(
            select(Comment)
            .where(Comment.request_id == request_id)
            .options(selectinload(Comment.user))
            .order_by(Comment.created_at)
        )
    ).all()
    if not rows:
        return ""
    lines = ["", "💬 <b>Комментарии</b>"]
    for row in rows:
        who = esc(row.user.full_name) if row.user else "Система"
        lines.append(f"• {fmt_dt(row.created_at)} — {who}: {esc(row.text)}")
    return "\n".join(lines)


def attachments_of(request: Request, stage: str) -> List[Attachment]:
    return [a for a in request.attachments if a.stage == stage]
