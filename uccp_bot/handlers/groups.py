"""Работа в рабочих Telegram-группах (п.29-31).

Базовый разбор сообщений без навязчивости: бот отвечает только когда уверен,
что речь о новой проблеме, и не чаще одного раза в несколько минут на группу.
Полный анализ переписки — этап 3.
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Dict, Optional, Tuple

from aiogram import Bot, F, Router, types
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import texts
from ..db.models import Category, Outlet, TelegramGroup, User
from ..utils import esc, now_local

router = Router(name="groups")
router.message.filter(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))

COOLDOWN = dt.timedelta(minutes=10)
_last_reply: Dict[int, dt.datetime] = {}

PROBLEM_MARKERS = [
    "не работает", "неисправ", "сломал", "сломан", "теч", "протек", "потек",
    "не включ", "не холодит", "не морозит", "не греет", "засор", "капает",
    "искрит", "коротит", "нет света", "нет воды", "гудит", "заявка", "почин",
    "ремонт", "срочно надо", "надо починить", "нужен мастер", "вызов мастера",
]

CATEGORY_HINTS = {
    "plumbing": ["сантех", "вод", "труб", "кран", "унитаз", "засор", "слив", "теч", "канализ"],
    "electric": ["электр", "свет", "розетк", "проводк", "автомат", "щит", "искрит", "коротит"],
    "equipment": ["холодильник", "печ", "гриль", "кофемашин", "миксер", "оборудован",
                  "витрин", "морозил", "тестомес"],
    "ventilation": ["вытяжк", "вентиляц", "кондиционер", "воздух", "сплит"],
    "it": ["касс", "компьютер", "принтер", "интернет", "wifi", "wi-fi", "сервер", "терминал"],
    "repair": ["мебел", "стол", "стул", "дверь", "плитк", "покрас", "стен"],
}

DATE_MARKERS = re.compile(
    r"(сегодня|завтра|послезавтра|срочно|\d{1,2}[.:/]\d{1,2}|\d{1,2}\s*(час|числ))",
    re.IGNORECASE,
)


def _looks_like_request(text: str) -> bool:
    low = text.lower()
    return any(marker in low for marker in PROBLEM_MARKERS)


def _guess_category(text: str) -> Optional[str]:
    low = text.lower()
    best: Tuple[Optional[str], int] = (None, 0)
    for code, words in CATEGORY_HINTS.items():
        score = sum(1 for word in words if word in low)
        if score > best[1]:
            best = (code, score)
    return best[0]


async def _missing_parts(session: AsyncSession, message: types.Message) -> list:
    text = message.text or message.caption or ""
    low = text.lower()
    missing = []

    outlets = (await session.scalars(select(Outlet).where(Outlet.is_active.is_(True)))).all()
    if not any(_outlet_mentioned(o.name, low) for o in outlets):
        missing.append("торговую точку")
    if len(text.strip()) < 25:
        missing.append("подробное описание проблемы")
    if not DATE_MARKERS.search(text):
        missing.append("срок выполнения")
    if not (message.photo or message.video or message.document):
        missing.append("фото")
    return missing


def _outlet_mentioned(name: str, low_text: str) -> bool:
    tokens = [t.lower() for t in re.split(r"[\s/,-]+", name) if len(t) > 3]
    return any(token in low_text for token in tokens)


def _cooldown_passed(chat_id: int) -> bool:
    last = _last_reply.get(chat_id)
    if last and now_local() - last < COOLDOWN:
        return False
    _last_reply[chat_id] = now_local()
    return True


async def _ensure_group(session: AsyncSession, chat: types.Chat) -> TelegramGroup:
    group = await session.scalar(select(TelegramGroup).where(TelegramGroup.chat_id == chat.id))
    if group is None:
        group = TelegramGroup(chat_id=chat.id, title=chat.title or str(chat.id))
        session.add(group)
        await session.flush()
    elif chat.title and group.title != chat.title:
        group.title = chat.title
    return group


def _bot_link_kb(bot_username: Optional[str]) -> Optional[types.InlineKeyboardMarkup]:
    if not bot_username:
        return None
    kb = InlineKeyboardBuilder()
    kb.button(text="📝 Оформить заявку через бот", url=f"https://t.me/{bot_username}")
    return kb.as_markup()


@router.message(F.new_chat_members)
async def on_added(message: types.Message, session: AsyncSession, bot: Bot) -> None:
    me = await bot.me()
    if not any(m.id == me.id for m in message.new_chat_members):
        return
    await _ensure_group(session, message.chat)
    await message.answer(
        "👋 Бот заявок УЦЦП подключён к этой группе.\n\n"
        "Я подскажу, если заявка оформлена не полностью, и помогу передать её "
        "нужному исполнителю. Полное оформление — в личных сообщениях бота.\n\n"
        "Администратор может привязать группу к направлению командой /bind.",
        reply_markup=_bot_link_kb(me.username),
    )


@router.message(Command("bind"))
async def cmd_bind(
    message: types.Message, session: AsyncSession, user: Optional[User]
) -> None:
    """Привязка группы к категории: /bind сантехника"""
    if user is None or not user.is_admin:
        await message.reply(texts.NO_ACCESS)
        return
    group = await _ensure_group(session, message.chat)
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        cats = (await session.scalars(select(Category).order_by(Category.sort_order))).all()
        await message.reply(
            "Укажите направление группы, например: <code>/bind сантехника</code>\n\n"
            "Доступные: " + ", ".join(c.name for c in cats)
        )
        return

    query = parts[1].strip().lower()
    cats = (await session.scalars(select(Category))).all()
    match = next((c for c in cats if query in c.name.lower() or query == c.code), None)
    if match is None:
        await message.reply("Направление не найдено. Проверьте название.")
        return
    group.category_id = match.id
    await session.commit()
    await message.reply(
        f"✅ Группа привязана к направлению: <b>{esc(match.label)}</b>.\n"
        "Заявки из этой группы будут сверяться с этим направлением."
    )


@router.message(Command("status"))
async def cmd_group_status(message: types.Message, session: AsyncSession) -> None:
    group = await session.scalar(
        select(TelegramGroup).where(TelegramGroup.chat_id == message.chat.id)
    )
    if group is None:
        await message.reply("Группа ещё не зарегистрирована. Отправьте любое сообщение боту.")
        return
    category = await session.get(Category, group.category_id) if group.category_id else None
    await message.reply(
        f"Группа: <b>{esc(group.title)}</b>\n"
        f"Направление: {esc(category.label) if category else 'не привязано'}"
    )


@router.message(F.text | F.caption)
async def watch_group(message: types.Message, session: AsyncSession, bot: Bot) -> None:
    text = message.text or message.caption or ""
    if text.startswith("/"):
        return
    if not _looks_like_request(text):
        return

    group = await _ensure_group(session, message.chat)
    await session.commit()
    if not _cooldown_passed(message.chat.id):
        return

    me = await bot.me()
    guessed_code = _guess_category(text)
    notes = []

    # п.31 — заявка попала не в ту группу
    if group.category_id and guessed_code:
        group_category = await session.get(Category, group.category_id)
        guessed = await session.scalar(select(Category).where(Category.code == guessed_code))
        if guessed and group_category and guessed.id != group_category.id:
            notes.append(
                f"Данная заявка относится к категории «{esc(guessed.name)}». "
                "Заявка будет перенаправлена соответствующему исполнителю."
            )

    missing = await _missing_parts(session, message)
    if missing:
        notes.append(
            "Заявка оформлена не полностью. Пожалуйста, добавьте недостающие данные: "
            + ", ".join(missing)
            + ". После этого заявка будет принята в работу."
        )

    if not notes:
        return
    notes.append(texts.GROUP_OFFER_BOT)
    await message.reply("\n\n".join(notes), reply_markup=_bot_link_kb(me.username))
