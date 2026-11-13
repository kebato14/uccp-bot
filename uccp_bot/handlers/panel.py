"""Вход в панель просмотра: команда /panel выдаёт одноразовый код."""
from __future__ import annotations

from typing import Optional

from aiogram import Router, types
from aiogram.filters import Command

from .. import texts
from ..config import config
from ..db.models import User
from ..utils import esc
from ..web import auth

router = Router(name="panel")


def panel_url() -> str:
    if config.web_base_url:
        return config.web_base_url.rstrip("/")
    return f"http://{config.web_host}:{config.web_port}"


@router.message(Command("panel"))
async def cmd_panel(message: types.Message, user: Optional[User]) -> None:
    if user is None or not user.is_approved:
        await message.answer(texts.NOT_REGISTERED)
        return
    if not config.web_enabled:
        await message.answer("Панель просмотра сейчас отключена.")
        return
    if not user.can_see_reports and not user.is_admin:
        await message.answer(
            "Панель просмотра доступна руководителям и администраторам. "
            "Ваши заявки удобнее смотреть прямо здесь, в боте."
        )
        return

    code = auth.issue_code(user.id)
    await message.answer(
        "🔑 <b>Вход в панель просмотра</b>\n\n"
        f"Адрес: {esc(panel_url())}\n"
        f"Код: <code>{code}</code>\n\n"
        "Код действует 5 минут и работает один раз.\n\n"
        "В панели — только просмотр: списки с фильтрами, карточки заявок, "
        "история и отчёты. Назначение, статусы и согласование остаются здесь, "
        "в боте."
    )
