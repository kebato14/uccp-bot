"""Вход в панель без паролей — по одноразовому коду из Telegram.

Пароли не заводим намеренно: личность пользователя уже подтверждена
в боте администратором. Человек нажимает «🔑 Вход в панель», бот выдаёт
шестизначный код на 5 минут, код вводится на странице входа.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from aiohttp import web
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import config
from ..db.models import User, UserStatus

COOKIE_NAME = "uccp_session"
CODE_TTL = 300          # код живёт 5 минут
SESSION_TTL = 12 * 3600  # вход действует 12 часов
MAX_ATTEMPTS = 5


@dataclass
class LoginCode:
    user_id: int
    created_at: float
    attempts: int = 0


# коды живут в памяти процесса: перезапуск бота их обнуляет — это нормально
_codes: Dict[str, LoginCode] = {}
# неудачные попытки по IP: (счётчик, время первой попытки)
_failures: Dict[str, Tuple[int, float]] = {}
FAIL_LIMIT = 10         # попыток
FAIL_WINDOW = 300       # за 5 минут


def _secret() -> bytes:
    """Ключ подписи куки. Если не задан — выводим из токена бота."""
    raw = config.web_secret or config.bot_token
    return hashlib.sha256(raw.encode()).digest()


def issue_code(user_id: int) -> str:
    _cleanup()
    code = f"{secrets.randbelow(1_000_000):06d}"
    _codes[code] = LoginCode(user_id=user_id, created_at=time.time())
    return code


def _cleanup() -> None:
    now = time.time()
    for key in [k for k, v in _codes.items() if now - v.created_at > CODE_TTL]:
        _codes.pop(key, None)


def check_code(code: str, client_ip: str) -> Optional[int]:
    """Проверка кода. Возвращает id пользователя либо None.

    Защита от перебора: не больше 10 неудачных попыток с одного адреса
    за 5 минут. Опечатка пользователю не мешает — ограничение считает
    именно ошибки, а не частоту запросов.
    """
    _cleanup()
    now = time.time()
    fails, since = _failures.get(client_ip, (0, now))
    if now - since > FAIL_WINDOW:         # окно истекло — счётчик сбрасывается
        fails, since = 0, now
    if fails >= FAIL_LIMIT:
        return None

    entry = _codes.pop(code.strip(), None)   # код одноразовый
    if entry is None:
        _failures[client_ip] = (fails + 1, since)
        return None
    _failures.pop(client_ip, None)
    return entry.user_id


def sign_session(user_id: int) -> str:
    payload = json.dumps({"uid": user_id, "exp": int(time.time()) + SESSION_TTL})
    body = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    signature = hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{body}.{signature}"


def read_session(raw: Optional[str]) -> Optional[int]:
    if not raw or "." not in raw:
        return None
    body, signature = raw.rsplit(".", 1)
    expected = hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(signature, expected):
        return None
    try:
        padded = body + "=" * (-len(body) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded))
    except Exception:
        return None
    if data.get("exp", 0) < time.time():
        return None
    return data.get("uid")


async def current_user(request: web.Request) -> Optional[User]:
    """Пользователь текущей сессии — всегда сверяется с базой."""
    user_id = read_session(request.cookies.get(COOKIE_NAME))
    if user_id is None:
        return None
    session_maker = request.app["session_maker"]
    async with session_maker() as session:  # type: AsyncSession
        user = await session.scalar(select(User).where(User.id == user_id))
        if user is None or user.status != UserStatus.ACTIVE:
            return None
        session.expunge(user)
        return user


def set_cookie(response: web.Response, user_id: int) -> None:
    response.set_cookie(
        COOKIE_NAME,
        sign_session(user_id),
        max_age=SESSION_TTL,
        httponly=True,
        samesite="Lax",
        secure=config.web_https,
    )


def drop_cookie(response: web.Response) -> None:
    response.del_cookie(COOKIE_NAME)
