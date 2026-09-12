"""Фабрики callback-данных (aiogram 3)."""
from __future__ import annotations

from typing import Optional

from aiogram.filters.callback_data import CallbackData


class WizardCB(CallbackData, prefix="wz"):
    """Шаги мастера создания заявки."""

    step: str          # brand | outlet | category | photo | priority | due | preview | edit
    value: str = ""


class CalendarCB(CallbackData, prefix="cal"):
    act: str           # day | month | time | nav | ignore | today | tomorrow
    year: int = 0
    month: int = 0
    day: int = 0
    hour: int = 0
    minute: int = 0


class ReqCB(CallbackData, prefix="rq"):
    """Действия по конкретной заявке."""

    act: str
    request_id: int
    value: str = ""


class ListCB(CallbackData, prefix="ls"):
    kind: str          # my | executor | admin | assigned
    page: int = 0
    value: str = ""


class AdminCB(CallbackData, prefix="ad"):
    act: str
    id: int = 0
    id2: int = 0
    value: str = ""


class OrgCB(CallbackData, prefix="og"):
    """Организационные заявки УЦЦП."""

    act: str
    id: int = 0
    value: str = ""


class ReportCB(CallbackData, prefix="rp"):
    act: str           # period | filter | excel | back
    value: str = ""


class HelpCB(CallbackData, prefix="hp"):
    topic: str


class RegCB(CallbackData, prefix="rg"):
    step: str          # brand | outlet | restart
    value: str = ""


class ApprCB(CallbackData, prefix="ap"):
    """Подтверждение регистрации администратором."""

    act: str           # open | approve | reject | role | brand | outlet | dir | save
    user_id: int = 0
    value: str = ""


def opt_int(value: str) -> Optional[int]:
    return int(value) if value not in ("", "none", None) else None
