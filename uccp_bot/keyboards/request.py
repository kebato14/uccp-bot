"""Клавиатуры действий по заявке — зависят от роли и статуса."""
from __future__ import annotations

from typing import List, Optional

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ..callbacks import ReqCB
from ..db.models import Request, RoleCode, Status, User


def _btn(text: str, act: str, request_id: int, value: str = "") -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text=text, callback_data=ReqCB(act=act, request_id=request_id, value=value).pack()
    )


def executor_kb(request: Request) -> InlineKeyboardMarkup:
    rid = request.id
    kb = InlineKeyboardBuilder()
    status = request.status

    if status in (Status.NEW, Status.AWAITING_ASSIGNMENT):
        kb.row(_btn("✅ Принять", "accept", rid))
        kb.row(_btn("📅 Предложить другое время", "propose", rid))
        kb.row(_btn("❌ Отклонить", "reject", rid))
    elif status == Status.RESCHEDULE_PROPOSED:
        kb.row(_btn("💬 Добавить комментарий", "comment", rid))
    elif status == Status.ACCEPTED:
        kb.row(_btn("▶️ Начать работу", "start", rid))
        kb.row(_btn("💬 Добавить комментарий", "comment", rid))
        kb.row(_btn("📅 Предложить другое время", "propose", rid))
    elif status in (Status.IN_PROGRESS, Status.RETURNED):
        kb.row(_btn("✅ Работа выполнена", "done", rid))
        kb.row(_btn("💬 Добавить комментарий", "comment", rid))
    kb.row(_btn("🕓 История", "history", rid), _btn("📎 Файлы", "media", rid))
    return kb.as_markup()


def initiator_kb(request: Request, user: User) -> InlineKeyboardMarkup:
    rid = request.id
    kb = InlineKeyboardBuilder()

    if request.status == Status.DONE and user.can_confirm:
        kb.row(_btn("✅ Подтвердить", "confirm", rid))
        kb.row(_btn("🔄 Вернуть на доработку", "return", rid))
    if request.status == Status.RESCHEDULE_PROPOSED and (
        user.id == request.author_id or user.can_confirm
    ):
        kb.row(_btn("✅ Согласовать новый срок", "due_ok", rid))
        kb.row(_btn("❌ Оставить прежний срок", "due_no", rid))
    if request.status in Status.OPEN:
        kb.row(_btn("💬 Добавить комментарий", "comment", rid))
    if request.status in (Status.NEW, Status.AWAITING_ASSIGNMENT) and (
        user.id == request.author_id or user.is_admin
    ):
        kb.row(_btn("🚫 Отменить заявку", "cancel", rid))
    kb.row(_btn("🕓 История", "history", rid), _btn("📎 Файлы", "media", rid))
    return kb.as_markup()


def admin_assign_kb(request: Request) -> InlineKeyboardMarkup:
    """Клавиатура для администратора при отсутствии исполнителя (п.14)."""
    rid = request.id
    kb = InlineKeyboardBuilder()
    kb.row(_btn("👤 Назначить исполнителя", "assign", rid))
    kb.row(_btn("➡️ Перенаправить", "assign", rid))
    kb.row(_btn("✏️ Изменить категорию", "change_category", rid))
    kb.row(_btn("❌ Отменить", "cancel", rid))
    kb.row(_btn("🕓 История", "history", rid), _btn("📎 Файлы", "media", rid))
    return kb.as_markup()


def admin_kb(request: Request) -> InlineKeyboardMarkup:
    rid = request.id
    kb = InlineKeyboardBuilder()
    if request.status not in Status.FINAL:
        kb.row(_btn("➡️ Перенаправить", "assign", rid))
        kb.row(_btn("✏️ Изменить категорию", "change_category", rid))
        kb.row(_btn("📅 Изменить срок", "change_due", rid))
    if request.status == Status.DONE:
        kb.row(_btn("✅ Подтвердить", "confirm", rid))
        kb.row(_btn("🔄 Вернуть на доработку", "return", rid))
    if request.status in Status.OPEN:
        kb.row(_btn("💬 Комментарий", "comment", rid), _btn("🚫 Отменить", "cancel", rid))
    kb.row(_btn("🕓 История", "history", rid), _btn("📎 Файлы", "media", rid))
    return kb.as_markup()


def card_kb(request: Request, user: User) -> InlineKeyboardMarkup:
    """Подбирает клавиатуру по роли пользователя относительно заявки."""
    if user.is_executor and request.executor_id == user.id:
        return executor_kb(request)
    if user.is_admin:
        if request.status == Status.AWAITING_ASSIGNMENT:
            return admin_assign_kb(request)
        return admin_kb(request)
    return initiator_kb(request, user)


def executors_pick_kb(
    request_id: int, executors: List[User], back_act: str = "card"
) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for ex in executors:
        mark = "" if ex.tg_id else " ⚠️"
        kb.button(
            text=f"{ex.full_name}{mark}",
            callback_data=ReqCB(act="assign_to", request_id=request_id, value=str(ex.id)).pack(),
        )
    kb.adjust(1)
    kb.row(_btn("⬅️ Назад", back_act, request_id))
    return kb.as_markup()


def categories_pick_kb(request_id: int, categories) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for cat in categories:
        kb.button(
            text=cat.label,
            callback_data=ReqCB(
                act="set_category", request_id=request_id, value=str(cat.id)
            ).pack(),
        )
    kb.adjust(1)
    kb.row(_btn("⬅️ Назад", "card", request_id))
    return kb.as_markup()


def simple_card_kb(request_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(_btn("⬅️ К заявке", "card", request_id))
    return kb.as_markup()
