"""Инлайн-календарь и выбор времени (п.11, п.45)."""
from __future__ import annotations

import calendar as pycal
import datetime as dt
from typing import Optional

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ..callbacks import CalendarCB, WizardCB
from ..utils import MONTHS_RU, WEEKDAYS_RU, now_local

IGNORE = CalendarCB(act="ignore").pack()


def calendar_kb(
    year: Optional[int] = None,
    month: Optional[int] = None,
    min_date: Optional[dt.date] = None,
    with_cancel: bool = True,
) -> InlineKeyboardMarkup:
    today = now_local().date()
    min_date = min_date or today
    year = year or today.year
    month = month or today.month

    kb = InlineKeyboardBuilder()
    prev_month = dt.date(year, month, 1) - dt.timedelta(days=1)
    next_month = dt.date(year, month, pycal.monthrange(year, month)[1]) + dt.timedelta(days=1)

    kb.row(
        InlineKeyboardButton(
            text="‹",
            callback_data=CalendarCB(
                act="nav", year=prev_month.year, month=prev_month.month
            ).pack(),
        ),
        InlineKeyboardButton(
            text=f"{MONTHS_RU[month - 1].capitalize()} {year}", callback_data=IGNORE
        ),
        InlineKeyboardButton(
            text="›",
            callback_data=CalendarCB(
                act="nav", year=next_month.year, month=next_month.month
            ).pack(),
        ),
    )
    kb.row(*[InlineKeyboardButton(text=d, callback_data=IGNORE) for d in WEEKDAYS_RU])

    for week in pycal.Calendar(firstweekday=0).monthdatescalendar(year, month):
        row = []
        for day in week:
            if day.month != month or day < min_date:
                row.append(InlineKeyboardButton(text=" ", callback_data=IGNORE))
            else:
                mark = f"·{day.day}·" if day == today else str(day.day)
                row.append(
                    InlineKeyboardButton(
                        text=mark,
                        callback_data=CalendarCB(
                            act="day", year=day.year, month=day.month, day=day.day
                        ).pack(),
                    )
                )
        kb.row(*row)

    tomorrow = today + dt.timedelta(days=1)
    kb.row(
        InlineKeyboardButton(
            text="Сегодня",
            callback_data=CalendarCB(
                act="day", year=today.year, month=today.month, day=today.day
            ).pack(),
        ),
        InlineKeyboardButton(
            text="Завтра",
            callback_data=CalendarCB(
                act="day", year=tomorrow.year, month=tomorrow.month, day=tomorrow.day
            ).pack(),
        ),
    )
    nav = [InlineKeyboardButton(text="⬅️ Назад", callback_data=WizardCB(step="back").pack())]
    if with_cancel:
        nav.append(
            InlineKeyboardButton(text="❌ Отменить", callback_data=WizardCB(step="cancel").pack())
        )
    kb.row(*nav)
    return kb.as_markup()


HOURS = [8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23]


def time_kb(with_cancel: bool = True) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for hour in HOURS:
        kb.button(
            text=f"{hour:02d}:00",
            callback_data=CalendarCB(act="time", hour=hour, minute=0).pack(),
        )
    kb.adjust(4)
    kb.row(
        InlineKeyboardButton(
            text="Без точного времени (до 18:00)",
            callback_data=CalendarCB(act="time", hour=18, minute=0).pack(),
        )
    )
    nav = [InlineKeyboardButton(text="⬅️ Назад", callback_data=WizardCB(step="back").pack())]
    if with_cancel:
        nav.append(
            InlineKeyboardButton(text="❌ Отменить", callback_data=WizardCB(step="cancel").pack())
        )
    kb.row(*nav)
    return kb.as_markup()
