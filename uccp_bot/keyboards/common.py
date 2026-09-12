"""Клавиатуры главного меню и общие инлайн-конструкторы."""
from __future__ import annotations

from typing import Iterable, List, Optional, Sequence

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

from ..callbacks import AdminCB, HelpCB, ListCB, RegCB, ReqCB, ReportCB, WizardCB
from ..db.models import Priority, RoleCode, User

# --- Кнопки главного меню (reply-клавиатура) ---
BTN_NEW = "➕ Новая заявка"
BTN_MY = "📋 Мои заявки"
BTN_STATUS = "🔍 Проверить статус"
BTN_TASKS = "🧰 Мои задачи"
BTN_CONFIRM_QUEUE = "☑️ На подтверждение"
BTN_ORG = "🗂 Заявки УЦЦП"
BTN_OUTLET_REQUESTS = "🏬 Заявки точки"
BTN_BRAND_REQUESTS = "🏢 Заявки бренда"
BTN_HELP = "📖 Инструкция / Помощь"
BTN_REPORTS = "📊 Отчёты"
BTN_ADMIN = "⚙️ Администрирование"
BTN_CANCEL = "❌ Отмена"


def main_menu(user: Optional[User]) -> ReplyKeyboardMarkup:
    """Меню зависит от роли: каждый видит только то, что ему разрешено."""
    kb = ReplyKeyboardBuilder()
    if user is None or not user.is_approved:
        kb.row(KeyboardButton(text=BTN_HELP))
        return kb.as_markup(resize_keyboard=True)

    role = user.role_code
    kb.row(KeyboardButton(text=BTN_NEW))

    if role == RoleCode.EXECUTOR:
        kb.row(KeyboardButton(text=BTN_TASKS), KeyboardButton(text=BTN_MY))
    else:
        kb.row(KeyboardButton(text=BTN_MY), KeyboardButton(text=BTN_CONFIRM_QUEUE))

    if role == RoleCode.OUTLET_ADMIN:
        kb.row(KeyboardButton(text=BTN_OUTLET_REQUESTS))
    elif role == RoleCode.OPS_DIRECTOR:
        kb.row(KeyboardButton(text=BTN_BRAND_REQUESTS))

    kb.row(KeyboardButton(text=BTN_ORG), KeyboardButton(text=BTN_STATUS))
    kb.row(KeyboardButton(text=BTN_HELP))

    extra: List[KeyboardButton] = []
    if user.can_see_reports:
        extra.append(KeyboardButton(text=BTN_REPORTS))
    if user.is_admin:
        extra.append(KeyboardButton(text=BTN_ADMIN))
    if extra:
        kb.row(*extra)
    return kb.as_markup(resize_keyboard=True, input_field_placeholder="Выберите действие")


def cancel_kb() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    kb.row(KeyboardButton(text=BTN_CANCEL))
    return kb.as_markup(resize_keyboard=True)


def remove_kb() -> ReplyKeyboardRemove:
    return ReplyKeyboardRemove()


def phone_kb() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    kb.row(KeyboardButton(text="📱 Отправить мой номер", request_contact=True))
    kb.row(KeyboardButton(text=BTN_CANCEL))
    return kb.as_markup(resize_keyboard=True)


# --- Инлайн-конструкторы ---
def _rows(builder: InlineKeyboardBuilder, per_row: int) -> InlineKeyboardMarkup:
    builder.adjust(per_row)
    return builder.as_markup()


def wizard_kb(
    step: str,
    items: Sequence,
    label_attr: str = "name",
    per_row: int = 1,
    with_back: bool = True,
    extra_buttons: Optional[Iterable[InlineKeyboardButton]] = None,
) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for item in items:
        label = getattr(item, label_attr, str(item))
        kb.button(text=label, callback_data=WizardCB(step=step, value=str(item.id)).pack())
    kb.adjust(per_row)
    if extra_buttons:
        kb.row(*extra_buttons)
    nav: List[InlineKeyboardButton] = []
    if with_back:
        nav.append(
            InlineKeyboardButton(text="⬅️ Назад", callback_data=WizardCB(step="back").pack())
        )
    nav.append(
        InlineKeyboardButton(text="❌ Отменить", callback_data=WizardCB(step="cancel").pack())
    )
    kb.row(*nav)
    return kb.as_markup()


def priority_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for code in Priority.ORDER:
        kb.button(
            text=Priority.TITLES[code],
            callback_data=WizardCB(step="priority", value=code).pack(),
        )
    kb.adjust(2)
    kb.row(
        InlineKeyboardButton(text="⬅️ Назад", callback_data=WizardCB(step="back").pack()),
        InlineKeyboardButton(text="❌ Отменить", callback_data=WizardCB(step="cancel").pack()),
    )
    return kb.as_markup()


def media_kb(can_skip: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Готово", callback_data=WizardCB(step="media_done").pack())
    if can_skip:
        kb.button(text="Пропустить", callback_data=WizardCB(step="media_skip").pack())
    kb.adjust(2)
    kb.row(
        InlineKeyboardButton(text="⬅️ Назад", callback_data=WizardCB(step="back").pack()),
        InlineKeyboardButton(text="❌ Отменить", callback_data=WizardCB(step="cancel").pack()),
    )
    return kb.as_markup()


def preview_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Отправить", callback_data=WizardCB(step="submit").pack())
    kb.button(text="✏️ Изменить", callback_data=WizardCB(step="edit").pack())
    kb.button(text="❌ Отменить", callback_data=WizardCB(step="cancel").pack())
    kb.adjust(1, 2)
    return kb.as_markup()


def edit_fields_kb() -> InlineKeyboardMarkup:
    fields = [
        ("Бренд", "brand"),
        ("Торговую точку", "outlet"),
        ("Категорию", "category"),
        ("Описание", "description"),
        ("Фото / видео", "media"),
        ("Приоритет", "priority"),
        ("Срок", "due"),
    ]
    kb = InlineKeyboardBuilder()
    for label, value in fields:
        kb.button(text=label, callback_data=WizardCB(step="edit_field", value=value).pack())
    kb.adjust(2)
    kb.row(
        InlineKeyboardButton(
            text="⬅️ К предпросмотру", callback_data=WizardCB(step="preview").pack()
        )
    )
    return kb.as_markup()


def help_kb(user: Optional[User]) -> InlineKeyboardMarkup:
    topics = [
        ("Как создать заявку?", "create"),
        ("Как принять заявку?", "accept"),
        ("Как изменить срок?", "reschedule"),
        ("Как завершить заявку?", "finish"),
        ("Как подтвердить работу?", "confirm"),
        ("Как получить отчёт?", "report"),
        ("Что такое заявки УЦЦП?", "org"),
    ]
    if user and user.is_admin:
        topics += [
            ("Как назначить исполнителя?", "assign"),
            ("Как зарегистрировать мастера?", "master"),
        ]
    topics.append(("К кому обратиться при ошибке?", "error"))

    kb = InlineKeyboardBuilder()
    for label, topic in topics:
        kb.button(text=label, callback_data=HelpCB(topic=topic).pack())
    kb.button(text="ℹ️ Что доступно мне", callback_data=HelpCB(topic="role").pack())
    kb.adjust(1)
    return kb.as_markup()


def back_to_help_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ К списку вопросов", callback_data=HelpCB(topic="root").pack())
    return kb.as_markup()


def yes_no_kb(yes_cb: str, no_cb: str, yes_text: str = "Да", no_text: str = "Нет") -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=yes_text, callback_data=yes_cb)
    kb.button(text=no_text, callback_data=no_cb)
    kb.adjust(2)
    return kb.as_markup()


__all__ = [
    "BTN_NEW", "BTN_MY", "BTN_STATUS", "BTN_TASKS", "BTN_CONFIRM_QUEUE",
    "BTN_OUTLET_REQUESTS", "BTN_BRAND_REQUESTS", "BTN_ORG",
    "BTN_HELP", "BTN_REPORTS", "BTN_ADMIN", "BTN_CANCEL",
    "main_menu", "cancel_kb", "remove_kb", "phone_kb", "wizard_kb", "priority_kb",
    "media_kb", "preview_kb", "edit_fields_kb", "help_kb", "back_to_help_kb", "yes_no_kb",
    "AdminCB", "ListCB", "RegCB", "ReqCB", "ReportCB",
]
