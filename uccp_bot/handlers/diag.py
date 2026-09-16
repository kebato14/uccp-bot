"""Диагностика скорости: /ping и /diag для администратора.

Показывает, где именно теряется время — в сети до Telegram, в базе
или в самом боте. Это важно: «бот тормозит» почти всегда означает
медленный канал до серверов Telegram, а не медленный код.
"""
from __future__ import annotations

import os
import platform
import sys
import time
from typing import Optional

from aiogram import Bot, Router, types
from aiogram.filters import Command
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import texts
from ..config import config
from ..db.models import OrgRequest, Request, User
from ..services import backup
from ..utils import esc, now_local

router = Router(name="diag")

STARTED_AT = time.monotonic()


def _verdict(api_ms: float) -> str:
    if api_ms < 300:
        return "🟢 Канал до Telegram быстрый — задержек быть не должно."
    if api_ms < 800:
        return "🟡 Канал до Telegram средний. Бот отвечает с небольшой паузой."
    if api_ms < 2000:
        return (
            "🟠 Канал до Telegram медленный. Основная задержка — сеть сервера, "
            "а не бот."
        )
    return (
        "🔴 Очень медленный канал до Telegram. Нужен сервер с лучшей связностью "
        "или прокси — иначе быстрее не станет."
    )


@router.message(Command("ping"))
async def cmd_ping(
    message: types.Message, session: AsyncSession, user: Optional[User], bot: Bot
) -> None:
    if user is None or not user.is_admin:
        await message.answer(texts.NO_ACCESS)
        return

    # 1) сеть до Telegram: три запроса, берём лучший и средний
    api_times = []
    for _ in range(3):
        start = time.perf_counter()
        await bot.get_chat(message.chat.id)
        api_times.append((time.perf_counter() - start) * 1000)
    api_best, api_avg = min(api_times), sum(api_times) / len(api_times)

    # 2) база
    start = time.perf_counter()
    users = await session.scalar(select(func.count(User.id)))
    reqs = await session.scalar(select(func.count(Request.id)))
    orgs = await session.scalar(select(func.count(OrgRequest.id)))
    db_ms = (time.perf_counter() - start) * 1000

    # 3) запись в базу
    start = time.perf_counter()
    await session.execute(select(User).limit(1))
    await session.commit()
    write_ms = (time.perf_counter() - start) * 1000

    db_path = backup.db_file_path()
    db_size = os.path.getsize(db_path) / 1024 if db_path and os.path.exists(db_path) else 0
    uptime = int(time.monotonic() - STARTED_AT)
    hours, rest = divmod(uptime, 3600)
    minutes = rest // 60

    await message.answer(
        "⚡️ <b>Скорость работы</b>\n\n"
        f"📡 Telegram API: <b>{api_best:.0f} мс</b> (лучший) · "
        f"{api_avg:.0f} мс (средний из 3)\n"
        f"🗄 Чтение из базы: <b>{db_ms:.0f} мс</b>\n"
        f"💾 Запись в базу: <b>{write_ms:.0f} мс</b>\n\n"
        f"{_verdict(api_best)}\n\n"
        f"<b>Сервер</b>\n"
        f"Python {sys.version.split()[0]} · {platform.system()} {platform.machine()}\n"
        f"Ядер: {os.cpu_count()} · время сервера: {now_local().strftime('%H:%M:%S')}\n"
        f"Бот работает: {hours} ч {minutes} мин\n\n"
        f"<b>Данные</b>\n"
        f"Пользователей: {users} · заявок: {reqs} · заявок УЦЦП: {orgs}\n"
        f"Размер базы: {db_size:.0f} КБ"
    )


@router.message(Command("diag"))
async def cmd_diag(
    message: types.Message, session: AsyncSession, user: Optional[User], bot: Bot
) -> None:
    """Подробности: как настроена база и что может тормозить."""
    if user is None or not user.is_admin:
        await message.answer(texts.NO_ACCESS)
        return

    from sqlalchemy import text as sql_text

    pragmas = {}
    for name in ("journal_mode", "synchronous", "busy_timeout", "cache_size"):
        row = await session.execute(sql_text(f"PRAGMA {name}"))
        pragmas[name] = row.scalar()

    try:
        import uvloop  # noqa: F401

        loop_info = "uvloop ✅ (ускоренный)"
    except ImportError:
        loop_info = "стандартный asyncio (можно ускорить: pip install uvloop)"

    wal = "✅" if str(pragmas.get("journal_mode", "")).lower() == "wal" else "⚠️ не WAL"

    await message.answer(
        "🔧 <b>Настройки быстродействия</b>\n\n"
        f"Журнал SQLite: <b>{esc(str(pragmas.get('journal_mode')))}</b> {wal}\n"
        f"Синхронизация: {esc(str(pragmas.get('synchronous')))} "
        "(1 = NORMAL, быстро и безопасно)\n"
        f"Ожидание блокировки: {esc(str(pragmas.get('busy_timeout')))} мс\n"
        f"Кэш страниц: {esc(str(pragmas.get('cache_size')))}\n"
        f"Цикл событий: {esc(loop_info)}\n"
        f"Уровень логов: {esc(config.log_level)}\n\n"
        "Если Telegram API в /ping показывает больше 800 мс — дело в канале "
        "сервера, а не в боте. Помогает: сервер ближе к Европе, "
        "либо прокси или туннель до Telegram."
    )


@router.message(Command("perf"))
async def cmd_perf(message: types.Message, user: Optional[User]) -> None:
    """Скорость по этапам на реальных нажатиях пользователей."""
    if user is None or not user.is_admin:
        await message.answer(texts.NO_ACCESS)
        return

    from ..services import perf

    data = perf.stats()
    if not data:
        await message.answer(
            "📊 Данных пока нет — бот только запустился.\n"
            "Понажимайте кнопки несколько минут и повторите /perf."
        )
        return

    lines = [
        f"📊 <b>Скорость обработки</b> (последние {int(data['count'])} действий)",
        "",
        "<b>Полный цикл</b> — от получения до ответа:",
        f"• обычно: <b>{data['total_p50']:.0f} мс</b>",
        f"• в худших 5%: {data['total_p95']:.0f} мс",
        f"• максимум: {data['total_max']:.0f} мс",
        "",
        "<b>Из чего складывается:</b>",
        f"🗄 база данных: {data['db_p50']:.0f} мс (макс {data['db_max']:.0f})",
        f"📡 Telegram API: {data['api_p50']:.0f} мс (макс {data['api_max']:.0f}), "
        f"{data['api_calls_avg']:.1f} вызова на действие",
        f"⚙️ логика бота: {data['own_p50']:.0f} мс",
        "",
    ]

    api_share = data["api_p50"] / data["total_p50"] * 100 if data["total_p50"] else 0
    if api_share > 60:
        lines.append(
            f"<b>Вывод:</b> {api_share:.0f}% времени уходит на связь с Telegram. "
            "Ускорять нужно канал сервера, код здесь ни при чём."
        )
    elif data["db_p50"] > data["total_p50"] * 0.4:
        lines.append("<b>Вывод:</b> основное время — база данных.")
    else:
        lines.append("<b>Вывод:</b> узких мест нет, бот отвечает быстро.")

    slow = perf.slowest(3)
    if slow and slow[0].total_ms > 1500:
        lines += ["", "<b>Самые долгие действия:</b>"]
        for s in slow:
            lines.append(
                f"• {esc(s.label or s.kind)} — {s.total_ms:.0f} мс "
                f"(API {s.api_ms:.0f}, база {s.db_ms:.0f})"
            )
    await message.answer("\n".join(lines))
