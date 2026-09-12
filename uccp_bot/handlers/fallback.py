"""Ответ на непонятые сообщения — чтобы у пользователя не было тупика (п.42)."""
from __future__ import annotations

from typing import Optional

from aiogram import F, Router, types
from aiogram.filters import StateFilter

from .. import texts
from ..db.models import User
from ..keyboards.common import main_menu

router = Router(name="fallback")


@router.message(StateFilter(None))
async def unknown_message(message: types.Message, user: Optional[User]) -> None:
    if user is None or not user.is_registered:
        await message.answer(
            "Я вас пока не знаю. Отправьте /start — это займёт минуту, "
            "после этого можно создавать заявки."
        )
        return
    await message.answer(
        "Не понял команду. Выберите действие кнопкой меню ниже "
        "или откройте 📖 Инструкцию / Помощь.",
        reply_markup=main_menu(user),
    )


@router.callback_query()
async def stale_callback(call: types.CallbackQuery) -> None:
    await call.answer(
        "Это сообщение устарело. Откройте заявку заново через меню.", show_alert=True
    )
