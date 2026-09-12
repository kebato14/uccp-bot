"""Раздел «Помощь / Инструкция» (п.26-28)."""
from __future__ import annotations

from typing import Optional

from aiogram import F, Router, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext

from .. import texts
from ..callbacks import HelpCB
from ..db.models import User
from ..keyboards.common import BTN_HELP, back_to_help_kb, help_kb, main_menu

router = Router(name="help")


@router.message(F.text == BTN_HELP)
@router.message(Command("help"))
async def open_help(
    message: types.Message, state: FSMContext, user: Optional[User]
) -> None:
    await state.clear()
    await message.answer(texts.HELP_ROOT, reply_markup=help_kb(user))


@router.callback_query(HelpCB.filter())
async def help_topic(
    call: types.CallbackQuery, callback_data: HelpCB, user: Optional[User]
) -> None:
    await call.answer()
    topic = callback_data.topic
    if topic == "root":
        await call.message.edit_text(texts.HELP_ROOT, reply_markup=help_kb(user))
        return
    if topic == "role":
        role = user.role_code if user else None
        body = texts.ROLE_INSTRUCTIONS.get(role, texts.NOT_REGISTERED)
        await call.message.edit_text(body, reply_markup=back_to_help_kb())
        return
    await call.message.edit_text(
        texts.HELP_TOPICS.get(topic, "Раздел не найден."), reply_markup=back_to_help_kb()
    )


@router.message(Command("cancel"))
async def cmd_cancel(
    message: types.Message, state: FSMContext, user: Optional[User]
) -> None:
    await state.clear()
    await message.answer(texts.CANCELLED, reply_markup=main_menu(user))
