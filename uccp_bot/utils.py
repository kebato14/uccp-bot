"""Вспомогательные функции: время, форматирование, экранирование."""
from __future__ import annotations

import datetime as dt
import html
import re
from typing import Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:  # py<3.9
    ZoneInfo = None  # type: ignore

from .config import config

_TZ = ZoneInfo(config.timezone) if ZoneInfo else None


def now_local() -> dt.datetime:
    """Текущее локальное время (naive, в часовом поясе компании)."""
    if _TZ:
        return dt.datetime.now(_TZ).replace(tzinfo=None)
    return dt.datetime.now()


def utc_from_local(value: dt.datetime) -> dt.datetime:
    """Локальное naive-время -> UTC naive (как храним в БД)."""
    if _TZ is None:
        return value
    aware = value.replace(tzinfo=_TZ)
    return aware.astimezone(dt.timezone.utc).replace(tzinfo=None)


def local_from_utc(value: Optional[dt.datetime]) -> Optional[dt.datetime]:
    if value is None:
        return None
    if _TZ is None:
        return value
    aware = value.replace(tzinfo=dt.timezone.utc)
    return aware.astimezone(_TZ).replace(tzinfo=None)


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def fmt_dt(value: Optional[dt.datetime], with_time: bool = True) -> str:
    """Формат даты/времени для пользователя (значение хранится в UTC)."""
    local = local_from_utc(value)
    if local is None:
        return "—"
    return local.strftime("%d.%m.%Y, %H:%M") if with_time else local.strftime("%d.%m.%Y")


def fmt_money(value) -> str:
    if value is None:
        return "—"
    return f"{float(value):,.2f}".replace(",", " ").replace(".", ",") + f" {config.currency}"


def esc(value: Optional[str]) -> str:
    return html.escape(value or "", quote=False)


PHONE_RE = re.compile(r"^\+?\d[\d\s\-()]{6,20}$")


def normalize_phone(raw: str) -> Optional[str]:
    raw = (raw or "").strip()
    if not PHONE_RE.match(raw):
        return None
    digits = re.sub(r"\D", "", raw)
    if raw.startswith("+") or len(digits) > 9:
        return "+" + digits
    return digits


def parse_amount(raw: str) -> Optional[float]:
    raw = (raw or "").strip().replace(" ", "").replace(",", ".")
    if raw in ("", "-"):
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value >= 0 else None


def parse_date(raw: str) -> Optional[dt.date]:
    raw = (raw or "").strip()
    for pattern in ("%d.%m.%Y", "%d.%m.%y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return dt.datetime.strptime(raw, pattern).date()
        except ValueError:
            continue
    return None


MONTHS_RU = [
    "январь", "февраль", "март", "апрель", "май", "июнь",
    "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь",
]
MONTHS_RU_GEN = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]
WEEKDAYS_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
