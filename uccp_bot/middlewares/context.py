"""Middleware: сессия БД и текущий пользователь в каждом обработчике."""
from __future__ import annotations

import logging
import time
from typing import Any, Awaitable, Callable, Dict, Optional

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject, User as TgUser
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from ..db.models import User


log = logging.getLogger(__name__)


class DbSessionMiddleware(BaseMiddleware):
    def __init__(self, session_maker: async_sessionmaker) -> None:
        self.session_maker = session_maker

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        async with self.session_maker() as session:
            data["session"] = session
            result = await handler(event, data)
            await session.commit()
            return result


class UserMiddleware(BaseMiddleware):
    """Подкладывает объект User из БД и обновляет username при изменении."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        session = data.get("session")
        tg_user: Optional[TgUser] = data.get("event_from_user")
        user: Optional[User] = None
        if session is not None and tg_user is not None and not tg_user.is_bot:
            user = await session.scalar(select(User).where(User.tg_id == tg_user.id))
            if user is not None and tg_user.username and user.username != tg_user.username:
                user.username = tg_user.username
        data["user"] = user
        return await handler(event, data)


class AccessMiddleware(BaseMiddleware):
    """Закрывает рабочий функционал, пока администратор не подтвердил доступ.

    Пропускаем только /start и /id — всё остальное для неподтверждённого
    пользователя недоступно.
    """

    ALLOWED = ("/start", "/id")

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        from ..db.models import UserStatus
        from .. import texts

        user: Optional[User] = data.get("user")
        if user is None or user.is_approved:
            return await handler(event, data)

        if isinstance(event, Message):
            text = (event.text or "").strip()
            if any(text.startswith(cmd) for cmd in self.ALLOWED):
                return await handler(event, data)
            # незавершённая регистрация — даём её закончить
            state = data.get("state")
            if state is not None and await state.get_state():
                return await handler(event, data)
            await event.answer(_status_hint(user, texts, UserStatus))
            return None

        if isinstance(event, CallbackQuery):
            state = data.get("state")
            if state is not None and await state.get_state():
                return await handler(event, data)
            if event.data and event.data.startswith("rg:"):   # «подать заявку заново»
                return await handler(event, data)
            await event.answer(_status_hint(user, texts, UserStatus), show_alert=True)
            return None

        return await handler(event, data)


def _status_hint(user: "User", texts, UserStatus) -> str:
    if user.status == UserStatus.PENDING:
        return texts.PENDING_HINT
    if user.status == UserStatus.BLOCKED:
        return texts.BLOCKED_HINT
    if user.status == UserStatus.REJECTED:
        return "Заявка на регистрацию отклонена. Отправьте /start, чтобы подать заново."
    return texts.DISABLED_HINT



class PerfMiddleware(BaseMiddleware):
    """Замер полного времени обработки обновления по этапам."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        from ..services import perf

        perf.reset()
        started = time.perf_counter()
        try:
            return await handler(event, data)
        finally:
            update = data.get("event_update")
            kind, label = "update", ""
            if update is not None:
                if update.message is not None:
                    kind = "message"
                    label = (update.message.text or update.message.content_type or "")[:40]
                elif update.callback_query is not None:
                    kind = "callback"
                    label = (update.callback_query.data or "")[:40]
            sample = perf.record(kind, label, time.perf_counter() - started)
            if sample.total_ms > 3000:
                log.warning(
                    "Медленная обработка %s «%s»: %.0f мс "
                    "(база %.0f мс, Telegram API %.0f мс / %s вызовов)",
                    kind, label, sample.total_ms, sample.db_ms,
                    sample.api_ms, sample.api_calls,
                )
