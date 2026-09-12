"""Точечные уведомления (п.41). Пишем только тем, кому информация действительно нужна."""
from __future__ import annotations

import logging
from typing import Iterable, List, Optional

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardMarkup, InputMediaPhoto, InputMediaVideo
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import Attachment, Request, User
from . import routing

log = logging.getLogger(__name__)


async def send_to_user(
    bot: Bot,
    user: Optional[User],
    text: str,
    keyboard: Optional[InlineKeyboardMarkup] = None,
) -> bool:
    """Отправка в личные сообщения. False — если мастер не нажимал /start или заблокировал бота."""
    if user is None or not user.tg_id:
        return False
    try:
        await bot.send_message(user.tg_id, text, reply_markup=keyboard)
        return True
    except TelegramAPIError as exc:
        log.warning("Не удалось отправить сообщение %s: %s", user.tg_id, exc)
        return False


async def send_to_chat(
    bot: Bot, chat_id: int, text: str, keyboard: Optional[InlineKeyboardMarkup] = None
) -> bool:
    try:
        await bot.send_message(chat_id, text, reply_markup=keyboard)
        return True
    except TelegramAPIError as exc:
        log.warning("Не удалось отправить сообщение в чат %s: %s", chat_id, exc)
        return False


async def notify_admins(
    session: AsyncSession,
    bot: Bot,
    text: str,
    keyboard: Optional[InlineKeyboardMarkup] = None,
    exclude_user_id: Optional[int] = None,
) -> int:
    sent = 0
    for admin in await routing.admins(session):
        if exclude_user_id and admin.id == exclude_user_id:
            continue
        if await send_to_user(bot, admin, text, keyboard):
            sent += 1
    return sent


async def send_attachments(
    bot: Bot, chat_id: int, attachments: Iterable[Attachment], caption: Optional[str] = None
) -> None:
    items = list(attachments)
    if not items:
        return
    media: List = []
    for idx, att in enumerate(items[:10]):
        cap = caption if idx == 0 else None
        if att.media_type == "video":
            media.append(InputMediaVideo(media=att.file_id, caption=cap))
        else:
            media.append(InputMediaPhoto(media=att.file_id, caption=cap))
    try:
        if len(media) == 1:
            item = items[0]
            if item.media_type == "video":
                await bot.send_video(chat_id, item.file_id, caption=caption)
            else:
                await bot.send_photo(chat_id, item.file_id, caption=caption)
        else:
            await bot.send_media_group(chat_id, media)
    except TelegramAPIError as exc:
        log.warning("Не удалось отправить вложения в %s: %s", chat_id, exc)


async def notify_request_watchers(
    session: AsyncSession,
    bot: Bot,
    request: Request,
    text: str,
    *,
    to_author: bool = False,
    to_executor: bool = False,
    to_admins: bool = False,
    keyboard: Optional[InlineKeyboardMarkup] = None,
    exclude_user_id: Optional[int] = None,
) -> None:
    if to_author and request.author and request.author.id != exclude_user_id:
        await send_to_user(bot, request.author, text, keyboard)
    if to_executor and request.executor and request.executor.id != exclude_user_id:
        await send_to_user(bot, request.executor, text, keyboard)
    if to_admins:
        await notify_admins(session, bot, text, keyboard, exclude_user_id=exclude_user_id)
