"""Точка входа: сборка диспетчера и запуск бота."""
from __future__ import annotations

import asyncio
import logging
import os

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

import time

from aiogram.client.session.aiohttp import AiohttpSession

from .config import config
from .db.base import SessionMaker, init_db
from .db.seed import ensure_bootstrap_admins, seed
from .handlers import (
    actions, admin, approval, create, diag, fallback, groups,
    help as help_handlers, lists, org, reports, start,
)
from .middlewares.context import (
    AccessMiddleware,
    DbSessionMiddleware,
    PerfMiddleware,
    UserMiddleware,
)
from .scheduler import setup_scheduler

logging.basicConfig(
    level=getattr(logging, config.log_level.upper(), logging.INFO),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
# по сообщению на каждое нажатие кнопки — на сервере это лишняя нагрузка
logging.getLogger("aiogram.event").setLevel(logging.WARNING)
log = logging.getLogger("uccp")


def _use_uvloop() -> bool:
    """uvloop ускоряет сетевые операции в 2-4 раза. Если не установлен — работаем как есть."""
    try:
        import uvloop
    except ImportError:
        return False
    uvloop.install()
    return True

COMMANDS = [
    BotCommand(command="start", description="Начать работу / регистрация"),
    BotCommand(command="menu", description="Главное меню"),
    BotCommand(command="help", description="Инструкция и помощь"),
    BotCommand(command="cancel", description="Отменить текущее действие"),
    BotCommand(command="id", description="Показать мой Telegram ID"),
    BotCommand(command="ping", description="Скорость работы (для администратора)"),
    BotCommand(command="perf", description="Замеры по этапам (для администратора)"),
]


class TimedSession(AiohttpSession):
    """Сессия Telegram с замером времени каждого вызова API."""

    async def make_request(self, bot, method, timeout=None):
        from .services import perf

        started = time.perf_counter()
        try:
            return await super().make_request(bot, method, timeout=timeout)
        finally:
            perf.add_api(time.perf_counter() - started)


def acquire_single_instance_lock() -> object:
    """Не даём запустить второго бота с тем же токеном.

    Два бота на одном токене делят обновления между собой: половина нажатий
    уходит «не туда» и остаётся без ответа — со стороны это выглядит как
    сильные тормоза и потерянные кнопки.
    """
    import fcntl
    import tempfile

    path = os.path.join(
        tempfile.gettempdir(), f"uccp_bot_{config.bot_token.split(':')[0]}.lock"
    )
    handle = open(path, "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise RuntimeError(
            "Бот уже запущен на этом компьютере (блокировка " + path + ").\n"
            "Два бота с одним токеном делят обновления между собой и работают "
            "с задержками. Остановите лишний процесс: pkill -f uccp_bot.bot"
        )
    handle.write(str(os.getpid()))
    handle.flush()
    return handle


def build_session() -> TimedSession:
    """Таймауты подобраны так, чтобы обрыв связи не подвешивал бота надолго."""
    session = TimedSession(timeout=config.api_timeout, proxy=config.telegram_proxy or None)
    return session


def build_dispatcher() -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())

    dp.update.middleware(PerfMiddleware())
    dp.update.middleware(DbSessionMiddleware(SessionMaker))
    dp.update.middleware(UserMiddleware())
    # доступ к рабочему функционалу — только после подтверждения администратором
    dp.message.outer_middleware(AccessMiddleware())
    dp.callback_query.outer_middleware(AccessMiddleware())

    # Личные чаты
    private = (start.router, approval.router, diag.router, help_handlers.router,
               create.router,
               lists.router, org.router, actions.router, admin.router,
               reports.router, fallback.router)
    for router in private:
        router.message.filter(F.chat.type == ChatType.PRIVATE)
        dp.include_router(router)

    # Группы
    dp.include_router(groups.router)
    return dp


async def on_startup(bot: Bot) -> None:
    await init_db()
    async with SessionMaker() as session:
        await seed(session)
        if config.bootstrap_admin_ids:
            await ensure_bootstrap_admins(session, config.bootstrap_admin_ids)
    await bot.set_my_commands(COMMANDS)
    me = await bot.me()
    log.info(
        "Бот запущен: @%s | python %s | uvloop: %s",
        me.username,
        ".".join(map(str, __import__("sys").version_info[:3])),
        "да" if _UVLOOP else "нет",
    )


async def main() -> None:
    config.validate()
    lock = acquire_single_instance_lock()      # держим до завершения процесса
    bot = Bot(
        token=config.bot_token,
        session=build_session(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = build_dispatcher()

    await on_startup(bot)
    scheduler = setup_scheduler(SessionMaker, bot)
    scheduler.start()
    try:
        await dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types(),
            polling_timeout=config.polling_timeout,
            handle_signals=True,
        )
    finally:
        scheduler.shutdown(wait=False)
        await bot.session.close()


_UVLOOP = _use_uvloop()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Бот остановлен")
