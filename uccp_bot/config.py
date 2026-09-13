"""Конфигурация бота. Все значения читаются из .env / переменных окружения."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # python-dotenv не обязателен, если переменные заданы в окружении
    pass

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _int_list(raw: str) -> List[int]:
    out: List[int] = []
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if chunk.lstrip("-").isdigit():
            out.append(int(chunk))
    return out


@dataclass
class Config:
    bot_token: str = os.getenv("BOT_TOKEN", "")
    db_url: str = os.getenv("DB_URL", "sqlite+aiosqlite:///uccp.db")
    timezone: str = os.getenv("TIMEZONE", "Asia/Dushanbe")
    currency: str = os.getenv("CURRENCY", "сомони")
    overdue_check_minutes: int = int(os.getenv("OVERDUE_CHECK_MINUTES", "15"))
    bootstrap_admin_ids: List[int] = field(
        default_factory=lambda: _int_list(os.getenv("BOOTSTRAP_ADMIN_IDS", ""))
    )
    export_dir: str = os.path.join(BASE_DIR, "exports")

    # --- Google Sheets: автоматический месячный отчёт ---
    google_credentials_file: str = os.getenv(
        "GOOGLE_CREDENTIALS_FILE", os.path.join(BASE_DIR, "google_credentials.json")
    )
    google_sheet_id: str = os.getenv("GOOGLE_SHEET_ID", "")
    google_sheet_title: str = os.getenv("GOOGLE_SHEET_TITLE", "Отчёты УЦЦП")
    google_share_email: str = os.getenv("GOOGLE_SHARE_EMAIL", "")
    google_drive_folder_id: str = os.getenv("GOOGLE_DRIVE_FOLDER_ID", "")
    backup_dir: str = os.getenv("BACKUP_DIR", os.path.join(BASE_DIR, "backups"))
    backup_keep: int = int(os.getenv("BACKUP_KEEP", "14"))
    backup_hour: int = int(os.getenv("BACKUP_HOUR", "3"))
    backup_to_telegram: bool = os.getenv("BACKUP_TO_TELEGRAM", "1") not in ("0", "false", "")
    monthly_report_day: int = int(os.getenv("MONTHLY_REPORT_DAY", "1"))
    monthly_report_hour: int = int(os.getenv("MONTHLY_REPORT_HOUR", "1"))

    @property
    def google_enabled(self) -> bool:
        """Есть ключ сервисного аккаунта либо учётные данные пользователя (ADC)."""
        adc = os.getenv("GOOGLE_APPLICATION_CREDENTIALS") or os.path.join(
            os.path.expanduser("~"), ".config", "gcloud",
            "application_default_credentials.json",
        )
        key = self.google_credentials_file
        has_key = os.path.exists(key) and os.path.getsize(key) > 100
        return has_key or os.path.exists(adc)

    def validate(self) -> None:
        if not self.bot_token:
            raise RuntimeError(
                "Не задан BOT_TOKEN. Скопируйте .env.example в .env и укажите токен бота."
            )


config = Config()
