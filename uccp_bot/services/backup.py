"""Резервное копирование базы данных.

База `uccp.db` — единственное место, где живут пользователи, заявки, история
и стоимости. В репозиторий она намеренно не попадает, поэтому копия делается
по расписанию в три независимых места:

1. локальная папка `backups/` (всегда);
2. личные сообщения администраторам в Telegram (всегда — файл можно скачать);
3. папка на Google Диске рядом с отчётами (когда настроен доступ к Google).

Копия снимается средствами SQLite (`Connection.backup`), а не простым
копированием файла — поэтому её можно делать на работающем боте, не
останавливая приём заявок.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import re
import sqlite3
from dataclasses import dataclass, field
from typing import List, Optional

from ..config import config
from ..utils import now_local

log = logging.getLogger(__name__)

PREFIX = "uccp_backup_"
DRIVE_UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart"


@dataclass
class BackupResult:
    path: str
    size: int
    drive_url: str = ""
    drive_error: str = ""
    removed: List[str] = field(default_factory=list)

    @property
    def size_kb(self) -> int:
        return max(1, round(self.size / 1024))


def db_file_path() -> Optional[str]:
    """Путь к файлу SQLite из строки подключения."""
    url = config.db_url
    if "sqlite" not in url:
        return None
    raw = url.split("///", 1)[-1]
    return raw if os.path.isabs(raw) else os.path.join(os.getcwd(), raw)


def make_backup(tag: str = "") -> BackupResult:
    """Снимает копию базы. Безопасно на работающем боте."""
    source = db_file_path()
    if not source or not os.path.exists(source):
        raise FileNotFoundError(f"Файл базы не найден: {source}")

    os.makedirs(config.backup_dir, exist_ok=True)
    stamp = now_local().strftime("%Y-%m-%d")
    if tag:
        stamp = f"{stamp}_{tag}"
    target = os.path.join(config.backup_dir, f"{PREFIX}{stamp}.db")

    src = sqlite3.connect(source)
    try:
        dst = sqlite3.connect(target)
        try:
            src.backup(dst)          # согласованный снимок без остановки бота
        finally:
            dst.close()
    finally:
        src.close()

    removed = rotate(config.backup_keep)
    return BackupResult(path=target, size=os.path.getsize(target), removed=removed)


def rotate(keep: int) -> List[str]:
    """Оставляем последние `keep` копий, остальные удаляем."""
    if keep <= 0 or not os.path.isdir(config.backup_dir):
        return []
    files = sorted(
        (f for f in os.listdir(config.backup_dir) if f.startswith(PREFIX) and f.endswith(".db")),
        reverse=True,
    )
    removed = []
    for name in files[keep:]:
        try:
            os.remove(os.path.join(config.backup_dir, name))
            removed.append(name)
        except OSError as exc:
            log.warning("Не удалось удалить старую копию %s: %s", name, exc)
    return removed


def list_backups() -> List[str]:
    if not os.path.isdir(config.backup_dir):
        return []
    return sorted(
        (f for f in os.listdir(config.backup_dir) if f.startswith(PREFIX)), reverse=True
    )


# --------------------------------------------------------------------------- #
# Google Диск
# --------------------------------------------------------------------------- #
def drive_available() -> bool:
    from . import gsheets

    return bool(config.google_drive_folder_id) and gsheets.credentials_available()


def upload_to_drive(path: str) -> str:
    """Кладёт копию в папку на Google Диске. Возвращает ссылку на файл."""
    import json

    from google.auth.transport.requests import AuthorizedSession

    from . import gsheets

    creds = gsheets.load_credentials()
    session = AuthorizedSession(creds)

    metadata = {"name": os.path.basename(path)}
    if config.google_drive_folder_id:
        metadata["parents"] = [config.google_drive_folder_id]

    with open(path, "rb") as fh:
        payload = fh.read()

    boundary = "uccp-backup-boundary"
    body = (
        f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
        f"{json.dumps(metadata)}\r\n"
        f"--{boundary}\r\nContent-Type: application/octet-stream\r\n\r\n"
    ).encode("utf-8") + payload + f"\r\n--{boundary}--\r\n".encode("utf-8")

    response = session.post(
        DRIVE_UPLOAD_URL,
        data=body,
        headers={"Content-Type": f"multipart/related; boundary={boundary}"},
        timeout=120,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"Google Диск ответил {response.status_code}: {response.text[:200]}")
    file_id = response.json().get("id", "")
    return f"https://drive.google.com/file/d/{file_id}/view" if file_id else ""


async def run_backup(tag: str = "") -> BackupResult:
    """Полный цикл: копия + выгрузка на Диск (если доступна)."""
    import asyncio

    result = await asyncio.to_thread(make_backup, tag)
    if drive_available():
        try:
            result.drive_url = await asyncio.to_thread(upload_to_drive, result.path)
        except Exception as exc:
            result.drive_error = str(exc)[:300]
            log.warning("Не удалось выгрузить копию на Google Диск: %s", exc)
    return result
