"""Автоматический ежемесячный отчёт УЦЦП в Google Sheets.

Каждый месяц бот выгружает все заявки в отдельный лист таблицы: детализацию
по каждой заявке и сводную часть (количество, просрочки, расходы по брендам,
точкам и исполнителям, итог за месяц).

Настройка — см. README, раздел «Google Sheets». Если ключ сервисного аккаунта
не подложен, выгрузка пропускается: бот сообщает об этом администратору,
а отчёт всегда доступен в Excel.
"""
from __future__ import annotations

import asyncio
import calendar as pycal
import datetime as dt
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import config
from ..db.models import Request, Setting, Status
from ..utils import MONTHS_RU, fmt_dt, now_local
from . import reports

log = logging.getLogger(__name__)

SETTING_SHEET_ID = "google_sheet_id"
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

DETAIL_HEADERS = [
    "№ заявки",
    "Дата заявки",
    "Бренд",
    "Торговая точка",
    "Категория",
    "Описание",
    "Исполнитель",
    "Приоритет",
    "Срок",
    "Статус",
    "Дата выполнения",
    "Дата закрытия",
    "Стоимость работ",
    "Стоимость материалов",
    "Итого",
    "Просрочка",
]


class GoogleSheetsNotConfigured(RuntimeError):
    pass


@dataclass
class ExportResult:
    url: str
    worksheet: str
    rows: int
    total_cost: float
    org_worksheet: str = ""
    org_rows: int = 0
    org_cost: float = 0.0


def month_bounds(year: int, month: int) -> Tuple[dt.date, dt.date]:
    last_day = pycal.monthrange(year, month)[1]
    return dt.date(year, month, 1), dt.date(year, month, last_day)


def month_title(year: int, month: int) -> str:
    return f"{MONTHS_RU[month - 1]} {year}".capitalize()


def worksheet_title(year: int, month: int) -> str:
    return f"{year}-{month:02d}"


def org_worksheet_title(year: int, month: int) -> str:
    """Организационные заявки — отдельный лист, статистика не смешивается."""
    return f"{year}-{month:02d}-УЦЦП"


ORG_DETAIL_HEADERS = [
    "№ заявки",
    "Дата заявки",
    "Объект",
    "Бренд",
    "Задача",
    "Автор",
    "Ответственный",
    "Приоритет",
    "Срок",
    "Статус",
    "Дата выполнения",
    "Дата закрытия",
    "Затраты",
    "Результат",
    "Просрочка",
]


def previous_month(today: Optional[dt.date] = None) -> Tuple[int, int]:
    today = today or now_local().date()
    first = today.replace(day=1)
    last_prev = first - dt.timedelta(days=1)
    return last_prev.year, last_prev.month


# --------------------------------------------------------------------------- #
# Формирование содержимого листа
# --------------------------------------------------------------------------- #
def _money(value) -> float:
    return round(float(value or 0), 2)


def _group(requests: List[Request], key) -> Dict[str, List[float]]:
    """{название: [кол-во, работы, материалы, итого]}"""
    store: Dict[str, List[float]] = {}
    for req in requests:
        # у ремонтных заявок затраты делятся на работы и материалы,
        # у организационных — одна сумма
        if hasattr(req, "work_cost"):
            work = _money(req.work_cost)
            material = _money(req.material_cost)
        else:
            work = _money(getattr(req, "cost", 0))
            material = 0.0
        name = key(req)
        row = store.setdefault(name, [0, 0.0, 0.0, 0.0])
        row[0] += 1
        row[1] += work
        row[2] += material
        row[3] += _money(req.total_cost)
    return dict(sorted(store.items(), key=lambda kv: (-kv[1][3], -kv[1][0], kv[0])))


def _group_block(title: str, store: Dict[str, List[float]]) -> List[List[Any]]:
    rows: List[List[Any]] = [[], [title], ["Наименование", "Заявок", "Работы", "Материалы", "Итого"]]
    for name, (count, work, material, total) in store.items():
        rows.append([name, count, round(work, 2), round(material, 2), round(total, 2)])
    if not store:
        rows.append(["нет данных", 0, 0, 0, 0])
    return rows


def build_month_payload(
    requests: List[Request], year: int, month: int
) -> List[List[Any]]:
    """Полное содержимое листа: детализация + сводка."""
    payload: List[List[Any]] = [
        [f"Отчёт УЦЦП за {month_title(year, month)}"],
        [f"Сформирован автоматически: {now_local().strftime('%d.%m.%Y %H:%M')}",
         f"Валюта: {config.currency}"],
        [],
        ["ДЕТАЛИЗАЦИЯ ЗАЯВОК"],
        list(DETAIL_HEADERS),
    ]

    closed = in_work = overdue = cancelled = 0
    for req in requests:
        if req.status in (Status.CLOSED, Status.CONFIRMED):
            closed += 1
        elif req.status in (Status.CANCELLED, Status.REJECTED):
            cancelled += 1
        else:
            in_work += 1
        if req.is_overdue and req.status in Status.OPEN:
            overdue += 1

        payload.append(
            [
                req.number,
                fmt_dt(req.created_at),
                req.brand.name if req.brand else "",
                req.outlet.name if req.outlet else "",
                req.category.name if req.category else "",
                req.description,
                req.executor.full_name if req.executor else "не назначен",
                req.priority_title,
                fmt_dt(req.due_at),
                Status.title(req.status),
                fmt_dt(req.done_at),
                fmt_dt(req.closed_at),
                "фикс. оплата" if req.cost_exempt else _money(req.work_cost),
                "фикс. оплата" if req.cost_exempt else _money(req.material_cost),
                "фикс. оплата" if req.cost_exempt else _money(req.total_cost),
                "да" if req.is_overdue else "нет",
            ]
        )

    if not requests:
        payload.append(["За этот месяц заявок не было"])

    total_work = sum(_money(r.work_cost) for r in requests)
    total_material = sum(_money(r.material_cost) for r in requests)
    total_cost = round(total_work + total_material, 2)
    paid = [r for r in requests if r.total_cost > 0]
    avg_cost = round(total_cost / len(paid), 2) if paid else 0

    payload += [
        [],
        ["СВОДКА ЗА МЕСЯЦ"],
        ["Всего заявок", len(requests)],
        ["Выполнено и закрыто", closed],
        ["Не закрыто (в работе)", in_work],
        ["Просрочено", overdue],
        ["Отменено / отклонено", cancelled],
        ["Стоимость работ", round(total_work, 2)],
        ["Стоимость материалов", round(total_material, 2)],
        ["ОБЩАЯ СУММА РАСХОДОВ", total_cost],
        ["Средняя стоимость заявки", avg_cost],
    ]

    payload += _group_block(
        "РАСХОДЫ ПО БРЕНДАМ", _group(requests, lambda r: r.brand.name if r.brand else "—")
    )
    payload += _group_block(
        "РАСХОДЫ ПО ТОРГОВЫМ ТОЧКАМ",
        _group(requests, lambda r: r.outlet.name if r.outlet else "—"),
    )
    payload += _group_block(
        "РАСХОДЫ ПО ИСПОЛНИТЕЛЯМ",
        _group(requests, lambda r: r.executor.full_name if r.executor else "не назначен"),
    )
    payload += _group_block(
        "РАСХОДЫ ПО КАТЕГОРИЯМ",
        _group(requests, lambda r: r.category.name if r.category else "—"),
    )
    return payload


def build_org_month_payload(requests: List, year: int, month: int) -> List[List[Any]]:
    """Лист организационных заявок УЦЦП за месяц: детализация + своя сводка."""
    from ..db.models import OrgStatus

    payload: List[List[Any]] = [
        [f"Организационные заявки УЦЦП за {month_title(year, month)}"],
        [f"Сформирован автоматически: {now_local().strftime('%d.%m.%Y %H:%M')}",
         f"Валюта: {config.currency}"],
        [],
        ["ДЕТАЛИЗАЦИЯ ЗАЯВОК"],
        list(ORG_DETAIL_HEADERS),
    ]

    closed = in_work = overdue = cancelled = 0
    for req in requests:
        if req.status == OrgStatus.CLOSED:
            closed += 1
        elif req.status == OrgStatus.CANCELLED:
            cancelled += 1
        else:
            in_work += 1
        if req.is_overdue and req.status in OrgStatus.OPEN:
            overdue += 1

        payload.append(
            [
                req.number,
                fmt_dt(req.created_at),
                req.object_name,
                req.brand.name if req.brand else "",
                req.description,
                req.author.full_name if req.author else "",
                req.assignee.full_name if req.assignee else "не назначен",
                req.priority_title,
                fmt_dt(req.due_at),
                OrgStatus.title(req.status),
                fmt_dt(req.done_at),
                fmt_dt(req.closed_at),
                _money(req.cost),
                req.result_comment or "",
                "да" if req.is_overdue else "нет",
            ]
        )

    if not requests:
        payload.append(["За этот месяц организационных заявок не было"])

    total_cost = round(sum(_money(r.cost) for r in requests), 2)
    payload += [
        [],
        ["СВОДКА ЗА МЕСЯЦ"],
        ["Всего заявок", len(requests)],
        ["Закрыто", closed],
        ["В работе", in_work],
        ["Просрочено", overdue],
        ["Отменено", cancelled],
        ["ОБЩИЕ ЗАТРАТЫ", total_cost],
    ]
    payload += _group_block(
        "ЗАТРАТЫ ПО ОБЪЕКТАМ", _group(requests, lambda r: r.object_name)
    )
    payload += _group_block(
        "ЗАТРАТЫ ПО ОТВЕТСТВЕННЫМ",
        _group(requests, lambda r: r.assignee.full_name if r.assignee else "не назначен"),
    )
    payload += _group_block(
        "ЗАЯВКИ ПО АВТОРАМ",
        _group(requests, lambda r: r.author.full_name if r.author else "—"),
    )
    return payload


async def org_month_requests(session: AsyncSession, year: int, month: int) -> List:
    date_from, date_to = month_bounds(year, month)
    filters = reports.ReportFilters(
        date_from=date_from,
        date_to=date_to,
        period_title=f"за {month_title(year, month)}",
    )
    return await reports.fetch_org_requests(session, filters)


async def month_requests(session: AsyncSession, year: int, month: int) -> List[Request]:
    date_from, date_to = month_bounds(year, month)
    filters = reports.ReportFilters(
        date_from=date_from,
        date_to=date_to,
        period_title=f"за {month_title(year, month)}",
    )
    return await reports.fetch_requests(session, filters)


# --------------------------------------------------------------------------- #
# Работа с Google API (синхронная, выполняется в отдельном потоке)
# --------------------------------------------------------------------------- #
def load_credentials():
    """Учётные данные Google: ключ сервисного аккаунта либо вход пользователя (ADC)."""
    if _valid_key_file(config.google_credentials_file):
        from google.oauth2.service_account import Credentials

        return Credentials.from_service_account_file(
            config.google_credentials_file, scopes=SCOPES
        )
    if os.path.exists(adc_path()):
        import google.auth

        creds, _ = google.auth.default(scopes=SCOPES)
        return creds
    raise GoogleSheetsNotConfigured(
        "Не найдены учётные данные Google: ни ключ сервисного аккаунта "
        f"({config.google_credentials_file}), ни ADC ({adc_path()}). "
        "См. README, раздел «Google Sheets»."
    )


def _client():
    """Авторизация двумя способами.

    1. Ключ сервисного аккаунта (`google_credentials.json`) — для сервера.
    2. Учётные данные пользователя (ADC), выданные командой
       `gcloud auth application-default login` — работает там, где политика
       организации запрещает создавать ключи сервисных аккаунтов.
       В этом случае таблица создаётся на Google-диске самого пользователя.
    """
    import gspread

    return gspread.authorize(load_credentials())


def _valid_key_file(path: str) -> bool:
    """Пустой или битый файл ключа игнорируем — иначе бот упадёт при выгрузке."""
    try:
        if not os.path.exists(path) or os.path.getsize(path) < 100:
            return False
        import json

        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return bool(data.get("client_email") and data.get("private_key"))
    except Exception:
        return False


def adc_path() -> str:
    """Путь к пользовательским учётным данным gcloud (ADC)."""
    explicit = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    if explicit:
        return explicit
    return os.path.join(
        os.path.expanduser("~"), ".config", "gcloud", "application_default_credentials.json"
    )


def credentials_available() -> bool:
    return _valid_key_file(config.google_credentials_file) or os.path.exists(adc_path())


def _open_spreadsheet(client, sheet_id: Optional[str]):
    """Открывает таблицу по ID либо создаёт новую. Возвращает (таблица, новый_id)."""
    import gspread

    if sheet_id:
        try:
            return client.open_by_key(sheet_id), None
        except gspread.exceptions.SpreadsheetNotFound:
            log.warning("Таблица %s не найдена, создаю новую", sheet_id)

    folder_id = config.google_drive_folder_id or None
    try:
        spreadsheet = client.create(config.google_sheet_title, folder_id=folder_id)
    except Exception as exc:
        if not folder_id:
            raise
        log.warning("Не удалось создать таблицу в папке %s (%s), создаю в корне диска",
                    folder_id, exc)
        spreadsheet = client.create(config.google_sheet_title)
    if config.google_share_email:
        try:
            spreadsheet.share(
                config.google_share_email, perm_type="user", role="writer", notify=False
            )
        except Exception as exc:  # доступ можно выдать и вручную
            log.warning("Не удалось выдать доступ %s: %s", config.google_share_email, exc)
    return spreadsheet, spreadsheet.id


def _write_sheet(sheet_id: Optional[str], title: str, payload: List[List[Any]]):
    client = _client()
    spreadsheet, created_id = _open_spreadsheet(client, sheet_id)

    try:
        worksheet = spreadsheet.worksheet(title)
        worksheet.clear()
    except Exception:
        worksheet = spreadsheet.add_worksheet(
            title=title, rows=max(len(payload) + 20, 100), cols=len(DETAIL_HEADERS) + 2
        )

    worksheet.update(payload, "A1", value_input_option="USER_ENTERED")

    # оформление: заголовок отчёта и шапка таблицы
    try:
        worksheet.format("A1:P1", {"textFormat": {"bold": True, "fontSize": 12}})
        worksheet.format("A4:P5", {"textFormat": {"bold": True}})
        worksheet.freeze(rows=5)
    except Exception as exc:  # оформление не критично
        log.warning("Не удалось применить оформление листа: %s", exc)

    # первый лист «Sheet1» из новой таблицы больше не нужен
    if created_id:
        for extra in spreadsheet.worksheets():
            if extra.title in ("Sheet1", "Лист1") and extra.id != worksheet.id:
                try:
                    spreadsheet.del_worksheet(extra)
                except Exception:
                    pass

    return spreadsheet.url, created_id


async def _stored_sheet_id(session: AsyncSession) -> Optional[str]:
    if config.google_sheet_id:
        return config.google_sheet_id
    setting = await session.get(Setting, SETTING_SHEET_ID)
    return setting.value if setting and setting.value else None


async def _store_sheet_id(session: AsyncSession, sheet_id: str) -> None:
    setting = await session.get(Setting, SETTING_SHEET_ID)
    if setting is None:
        session.add(Setting(key=SETTING_SHEET_ID, value=sheet_id))
    else:
        setting.value = sheet_id
    await session.commit()


async def export_month(session: AsyncSession, year: int, month: int) -> ExportResult:
    """Выгружает месяц в Google Sheets двумя листами.

    «ГГГГ-ММ» — технические и ремонтные заявки,
    «ГГГГ-ММ-УЦЦП» — организационные. Листы перезаписываются при повторе.
    """
    requests = await month_requests(session, year, month)
    org_requests = await org_month_requests(session, year, month)

    payload = build_month_payload(requests, year, month)
    org_payload = build_org_month_payload(org_requests, year, month)

    sheet_id = await _stored_sheet_id(session)
    title = worksheet_title(year, month)
    org_title = org_worksheet_title(year, month)

    url, created_id = await asyncio.to_thread(_write_sheet, sheet_id, title, payload)
    if created_id:
        await _store_sheet_id(session, created_id)
        sheet_id = created_id
    await asyncio.to_thread(_write_sheet, sheet_id, org_title, org_payload)

    total = round(sum(_money(r.total_cost) for r in requests), 2)
    org_total = round(sum(_money(r.cost) for r in org_requests), 2)
    log.info(
        "Google Sheets: %s — %s ремонтных, %s — %s организационных",
        title, len(requests), org_title, len(org_requests),
    )
    return ExportResult(
        url=url,
        worksheet=title,
        rows=len(requests),
        total_cost=total,
        org_worksheet=org_title,
        org_rows=len(org_requests),
        org_cost=org_total,
    )
