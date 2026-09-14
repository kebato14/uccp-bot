"""Симуляция реальных диалогов бота без Telegram.

Прогоняется полный сценарий п.51 ТЗ: регистрация → создание заявки кнопками →
исполнитель принимает/начинает/выполняет → менеджер подтверждает → отчёт.
Telegram API подменён: все исходящие сообщения записываются, кнопки нажимаются
по их тексту — как это сделал бы живой пользователь.

Запуск: .venv/bin/python tests/ui_sim.py
"""
from __future__ import annotations

import asyncio
import datetime as dt
import os
import sys
import tempfile
from typing import Any, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["BOT_TOKEN"] = "123456789:AAEsTtEsTtEsTtEsTtEsTtEsTtEsTtEsTtEs"
os.environ["DB_URL"] = "sqlite+aiosqlite:///" + os.path.join(tempfile.mkdtemp(), "ui.db")

from aiogram import Bot  # noqa: E402
from aiogram.client.default import DefaultBotProperties  # noqa: E402
from aiogram.client.session.base import BaseSession  # noqa: E402
from aiogram.enums import ParseMode  # noqa: E402
from aiogram.types import (  # noqa: E402
    CallbackQuery, Chat, Contact, Message, PhotoSize, Update, User as TgUser,
)
from sqlalchemy import select  # noqa: E402

from uccp_bot.bot import build_dispatcher  # noqa: E402
from uccp_bot.db.base import SessionMaker, init_db  # noqa: E402
from uccp_bot.db.models import (
    Brand, Category, OrgRequest, OrgStatus, Outlet, Request, RoleCode, Status,
    User, UserStatus,
)
from uccp_bot.services import routing  # noqa: E402
from uccp_bot.db.seed import seed  # noqa: E402

BOT_ID = 123456789


class Outbox:
    def __init__(self) -> None:
        self.messages: List[dict] = []

    def add(self, chat_id: int, text: str, markup: Any, message_id: int) -> None:
        self.messages.append(
            {"chat_id": chat_id, "text": text or "", "markup": markup, "message_id": message_id}
        )

    def last_for(self, chat_id: int) -> Optional[dict]:
        for item in reversed(self.messages):
            if item["chat_id"] == chat_id:
                return item
        return None

    def texts_for(self, chat_id: int) -> str:
        return "\n---\n".join(m["text"] for m in self.messages if m["chat_id"] == chat_id)

    def clear(self) -> None:
        self.messages.clear()


OUT = Outbox()


class MockSession(BaseSession):
    """Подменённый транспорт Telegram: ничего не отправляет, всё записывает."""

    counter = 1000

    async def close(self) -> None:
        return None

    async def stream_content(self, *args, **kwargs):  # pragma: no cover
        yield b""

    async def make_request(self, bot, method, timeout=None):
        name = type(method).__name__
        MockSession.counter += 1
        mid = MockSession.counter

        if name == "GetMe":
            return TgUser(id=BOT_ID, is_bot=True, first_name="UCCP", username="uccp_test_bot")
        if name in ("SetMyCommands", "AnswerCallbackQuery", "DeleteMessage"):
            return True

        chat_id = getattr(method, "chat_id", None)
        text = getattr(method, "text", None) or getattr(method, "caption", None) or f"[{name}]"
        markup = getattr(method, "reply_markup", None)

        if name in ("EditMessageText", "EditMessageReplyMarkup"):
            mid = getattr(method, "message_id", mid) or mid
            # обновляем состояние уже отправленного сообщения
            for item in OUT.messages:
                if item["message_id"] == mid:
                    if name == "EditMessageText":
                        item["text"] = text
                    item["markup"] = markup
                    break
            else:
                OUT.add(chat_id, text, markup, mid)
        else:
            OUT.add(chat_id, text, markup, mid)

        return Message(
            message_id=mid,
            date=dt.datetime.now(),
            chat=Chat(id=chat_id or 0, type="private"),
            from_user=TgUser(id=BOT_ID, is_bot=True, first_name="UCCP"),
            text=text,
        ).as_(bot)


def tg_user(uid: int, name: str, username: Optional[str] = None) -> TgUser:
    return TgUser(id=uid, is_bot=False, first_name=name, username=username)


class Client:
    """Один пользователь Telegram."""

    _update_id = 1

    def __init__(self, bot: Bot, dp, uid: int, name: str, username: Optional[str] = None):
        self.bot, self.dp, self.uid, self.name = bot, dp, uid, name
        self.user = tg_user(uid, name, username)

    def _next_id(self) -> int:
        Client._update_id += 1
        return Client._update_id

    async def send(self, text: str) -> None:
        message = Message(
            message_id=self._next_id(),
            date=dt.datetime.now(),
            chat=Chat(id=self.uid, type="private"),
            from_user=self.user,
            text=text,
        )
        await self.dp.feed_update(self.bot, Update(update_id=self._next_id(), message=message))

    async def send_contact(self, phone: str) -> None:
        message = Message(
            message_id=self._next_id(),
            date=dt.datetime.now(),
            chat=Chat(id=self.uid, type="private"),
            from_user=self.user,
            contact=Contact(phone_number=phone, first_name=self.name, user_id=self.uid),
        )
        await self.dp.feed_update(self.bot, Update(update_id=self._next_id(), message=message))

    async def send_photo(self) -> None:
        message = Message(
            message_id=self._next_id(),
            date=dt.datetime.now(),
            chat=Chat(id=self.uid, type="private"),
            from_user=self.user,
            photo=[PhotoSize(file_id="PHOTO_1", file_unique_id="u1", width=100, height=100,
                             file_size=1000)],
        )
        await self.dp.feed_update(self.bot, Update(update_id=self._next_id(), message=message))

    def _find_button(self, label: str) -> str:
        item = OUT.last_for(self.uid)
        candidates = []
        for message in reversed(
            [m for m in OUT.messages if m["chat_id"] == self.uid and m["markup"]]
        ):
            for row in getattr(message["markup"], "inline_keyboard", []):
                for button in row:
                    if button.callback_data:
                        candidates.append((message["message_id"], button.text, button.callback_data))
            if candidates:
                break
        for mid, text, data in candidates:
            if label.lower() in text.lower():
                return mid, data
        raise AssertionError(
            f"Кнопка «{label}» не найдена. Доступны: {[c[1] for c in candidates]}"
        )

    def has_button(self, label: str) -> bool:
        try:
            self._find_button(label)
            return True
        except AssertionError:
            return False

    async def click(self, label: str) -> None:
        mid, data = self._find_button(label)
        message = Message(
            message_id=mid,
            date=dt.datetime.now(),
            chat=Chat(id=self.uid, type="private"),
            from_user=tg_user(BOT_ID, "UCCP"),
            text=(OUT.last_for(self.uid) or {}).get("text") or "…",
        )
        call = CallbackQuery(
            id=str(self._next_id()),
            from_user=self.user,
            chat_instance="ci",
            message=message,
            data=data,
        )
        await self.dp.feed_update(
            self.bot, Update(update_id=self._next_id(), callback_query=call)
        )


def check(condition: bool, label: str) -> None:
    print(("✅ " if condition else "❌ ") + label)
    if not condition:
        print("\n--- последние сообщения ---")
        for m in OUT.messages[-6:]:
            print(f"[{m['chat_id']}] {m['text'][:400]}")
        raise AssertionError(label)


async def main() -> None:
    await init_db()
    async with SessionMaker() as session:
        await seed(session)

    bot = Bot(
        token=os.environ["BOT_TOKEN"],
        session=MockSession(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = build_dispatcher()

    admin = Client(bot, dp, 5001, "Админ УЦЦП")
    manager = Client(bot, dp, 5002, "Рахимова Мадина")
    electrician = Client(bot, dp, 5003, "Назрулло Электрик", username="nazrullo")

    # ---------------- администратор из .env ----------------
    print("\n=== Доступ выдаёт только администратор ===")
    async with SessionMaker() as session:
        from uccp_bot.db.seed import ensure_bootstrap_admins
        await ensure_bootstrap_admins(session, [5001])

    await admin.send("/start")
    check("С возвращением" in OUT.texts_for(5001), "Администратор из .env получает доступ сразу")

    # ---------------- заявка на регистрацию ----------------
    print("\n=== Регистрация: сотрудник подаёт заявку ===")
    OUT.clear()
    await manager.send("/start")
    await manager.send("Мадина")
    await manager.send("Рахимова")
    await manager.send("Администратор точки Moose Opera")
    await manager.click("Moose Café")
    await manager.click("Moose Opera")
    check("Заявка на регистрацию отправлена" in OUT.texts_for(5002),
          "Пользователю сказано ждать подтверждения")
    check("роль" not in OUT.texts_for(5002).lower().split("ожидайте")[0],
          "Пользователь не выбирал себе роль")
    check("Новая регистрация" in OUT.texts_for(5001), "Заявка пришла администратору")
    check("Администратор точки Moose Opera" in OUT.texts_for(5001),
          "В заявке видна указанная должность")

    async with SessionMaker() as session:
        pending = await session.scalar(
            select(User).where(User.tg_id == 5002)
        )
        check(pending.status == UserStatus.PENDING, "Статус заявки: ожидает подтверждения")

    # до подтверждения функционал закрыт
    OUT.clear()
    await manager.send("➕ Новая заявка")
    check("на рассмотрении" in OUT.texts_for(5002), "До подтверждения функционал закрыт")
    check("Выберите бренд" not in OUT.texts_for(5002), "Мастер создания заявки не запустился")

    # ---------------- администратор подтверждает и назначает роль ----------------
    print("\n=== Администратор подтверждает и назначает роль ===")
    OUT.clear()
    await admin.send("⚙️ Администрирование")
    await admin.click("Заявки на регистрацию")
    await admin.click("Рахимова")
    await admin.click("Подтвердить")
    check("Назначьте" in OUT.texts_for(5001), "Администратору предложено назначить роль")
    await admin.click("Администратор точки")
    await admin.click("Moose Café")
    await admin.click("Moose Opera")
    check("подтверждена" in OUT.texts_for(5001), "Регистрация подтверждена")
    check("Регистрация подтверждена" in OUT.texts_for(5002),
          "Пользователь получил уведомление о доступе")
    check("Администратор точки" in OUT.texts_for(5002), "Пользователю сообщили его роль")

    async with SessionMaker() as session:
        approved = await session.scalar(select(User).where(User.tg_id == 5002))
        check(approved.status == UserStatus.ACTIVE, "Статус: доступ выдан")
        check(approved.role_code == RoleCode.OUTLET_ADMIN, "Роль назначена администратором")
        check(approved.outlet_id is not None, "Точка привязана")

    # подтверждение переживает перезапуск бота: состояние не в памяти, а в БД
    print("\n=== Устойчивость подтверждения к перезапуску ===")
    restart_client = Client(bot, dp, 5005, "Тестов Тест", username="testov")
    await restart_client.send("/start")
    await restart_client.send("Тест")
    await restart_client.send("Тестов")
    await restart_client.send("Менеджер смены")
    await restart_client.click("Moose Café")
    await restart_client.click("Moose TCell")

    OUT.clear()
    await admin.send("⚙️ Администрирование")
    await admin.click("Заявки на регистрацию")
    await admin.click("Тестов")
    await admin.click("Подтвердить")
    await admin.click("Администратор точки")

    # имитируем перезапуск бота: память FSM теряется
    await dp.fsm.get_context(bot, chat_id=5001, user_id=5001).clear()
    check(True, "Бот «перезапущен» посреди подтверждения")

    await admin.click("Moose Café")
    await admin.click("Moose TCell")
    async with SessionMaker() as session:
        restored = await session.scalar(select(User).where(User.tg_id == 5005))
        check(restored.status == UserStatus.ACTIVE,
              "Подтверждение доведено до конца после перезапуска")
        check(restored.role_code == RoleCode.OUTLET_ADMIN, "Роль сохранена")
        check(restored.outlet_id is not None, "Объект сохранён")

    # ---------------- мастер: та же схема ----------------
    print("\n=== Регистрация мастера ===")
    async with SessionMaker() as session:
        nazrullo = await session.scalar(select(User).where(User.full_name == "Назрулло"))
        nazrullo.username = "nazrullo"      # администратор указал username мастера
        await session.commit()

    OUT.clear()
    await electrician.send("/start")
    check("профиль уже заведён" in OUT.texts_for(5003),
          "Мастер из ТЗ узнан по @username")

    new_master = Client(bot, dp, 5004, "Сафаров Джамшед", username="safarov")
    OUT.clear()
    await new_master.send("/start")
    await new_master.send("Джамшед")
    await new_master.send("Сафаров")
    await new_master.send("Сантехник, работаю по обоим брендам")
    await new_master.click("Оба бренда")
    check("Заявка на регистрацию отправлена" in OUT.texts_for(5004), "Мастер подал заявку")
    check("Новая регистрация" in OUT.texts_for(5001), "Заявка мастера пришла администратору")

    OUT.clear()
    await admin.send("⚙️ Администрирование")
    await admin.click("Заявки на регистрацию")
    await admin.click("Сафаров")
    await admin.click("Подтвердить")
    await admin.click("Мастер / исполнитель")
    check("направления мастера" in OUT.texts_for(5001).lower(),
          "Администратор выбирает направление мастера")
    await admin.click("Вентиляция")
    await admin.click("Готово")
    await admin.click("Все бренды и объекты")
    check("Мастер — вентиляция" in OUT.texts_for(5004),
          "Мастеру сообщена роль с направлением")
    check("получать назначенные вам заявки" in OUT.texts_for(5004),
          "Мастеру объяснили, что дальше")

    async with SessionMaker() as session:
        master = await session.scalar(select(User).where(User.tg_id == 5004))
        check(master.role_code == RoleCode.EXECUTOR, "Роль мастера назначена")
        vent = await session.scalar(select(Category).where(Category.code == "ventilation"))
        moose = await session.scalar(select(Brand).where(Brand.name == "Moose Café"))
        opera = await session.scalar(select(Outlet).where(Outlet.name == "Moose Opera"))
        found = await routing.find_executor(session, vent.id, moose.id, opera.id)
        check(found is not None and found.id == master.id,
              "Заявки по вентиляции теперь уходят ему автоматически")

    # ---------------- создание заявки ----------------
    print("\n=== Сценарий п.51: создание заявки менеджером ===")
    OUT.clear()
    await manager.send("➕ Новая заявка")
    await manager.click("Moose Café")
    await manager.click("Moose Opera")
    await manager.click("Электрика")
    check("опишите" in OUT.texts_for(5002).lower(), "Бот просит описание")
    await manager.send("В зале не работает освещение над кассой, выбивает автомат.")
    await manager.send_photo()
    await manager.click("Готово")
    await manager.click("Высокий")
    tomorrow = dt.date.today() + dt.timedelta(days=1)
    await manager.click("Завтра")
    await manager.click("14:00")
    check("Проверьте заявку" in OUT.texts_for(5002), "Показан предпросмотр")
    check("Назрулло" in OUT.texts_for(5002), "В предпросмотре определён исполнитель")

    # правка описания из предпросмотра
    await manager.click("Изменить")
    await manager.click("Приоритет")
    await manager.click("Критический")
    check("Критический" in (OUT.last_for(5002) or {})["text"],
          "Правка из предпросмотра вернула к проверке")

    await manager.click("Отправить")
    check("создана" in OUT.texts_for(5002), "Заявка создана")
    check("отправлена исполнителю" in OUT.texts_for(5002), "Заявка ушла исполнителю")
    check("Новая заявка" in OUT.texts_for(5003), "Электрик получил заявку в личку")

    async with SessionMaker() as session:
        req = await session.scalar(select(Request))
        check(req is not None and req.status == Status.NEW, f"Статус: {req.status}")
        number = req.number
    print(f"   номер заявки: {number}")

    # ---------------- исполнитель ----------------
    print("\n=== Исполнитель: принять → начать → выполнить ===")
    await electrician.click("Принять")
    async with SessionMaker() as session:
        req = await session.scalar(select(Request))
        check(req.status == Status.ACCEPTED, "Статус «Принята»")
    check("принял заявку" in OUT.texts_for(5002), "Менеджер уведомлён о принятии")

    await electrician.click("Начать работу")
    async with SessionMaker() as session:
        req = await session.scalar(select(Request))
        check(req.status == Status.IN_PROGRESS, "Статус «В процессе»")

    await electrician.click("Работа выполнена")
    check("что было сделано" in OUT.texts_for(5003), "Запрошен комментарий")
    await electrician.send("Заменён автомат 16А, линия проверена под нагрузкой.")
    check("фото результата" in OUT.texts_for(5003), "Запрошено фото результата")
    await electrician.send("готово")
    check("обязательно" in OUT.texts_for(5003), "Без фото завершить нельзя")
    await electrician.send_photo()
    await electrician.send("готово")
    check("стоимость работ" in OUT.texts_for(5003), "Запрошена стоимость работ")
    await electrician.send("250")
    await electrician.send("120.50")

    async with SessionMaker() as session:
        req = await session.scalar(select(Request))
        check(req.status == Status.DONE, "Статус «Выполнена» (не закрыта)")
        check(abs(req.total_cost - 370.5) < 0.01, f"Стоимость сохранена: {req.total_cost}")
    check("проверьте результат" in OUT.texts_for(5002).lower(),
          "Менеджеру пришёл запрос на проверку")

    # ---------------- подтверждение ----------------
    print("\n=== Менеджер: возврат и подтверждение ===")
    OUT.clear()
    await manager.send("☑️ На подтверждение")
    await manager.click(number)
    await manager.click("Вернуть на доработку")
    await manager.send("Не убран мусор после работы.")
    async with SessionMaker() as session:
        req = await session.scalar(select(Request))
        check(req.status == Status.RETURNED, "Статус «Возвращена на доработку»")
    check("возвращена на доработку" in OUT.texts_for(5003).lower(),
          "Исполнитель получил причину возврата")

    await electrician.click("Работа выполнена")
    await electrician.send("Мусор убран, зона сдана.")
    await electrician.send_photo()
    await electrician.send("готово")
    await electrician.send("0")
    await electrician.send("0")

    OUT.clear()
    await manager.send("☑️ На подтверждение")
    await manager.click(number)
    await manager.click("Подтвердить")
    async with SessionMaker() as session:
        req = await session.scalar(select(Request))
        check(req.status == Status.CLOSED, "Заявка закрыта менеджером")
    check("закрыта" in OUT.texts_for(5003), "Исполнитель уведомлён о закрытии")

    # ---------------- отчёт ----------------
    print("\n=== Отчёты ===")
    OUT.clear()
    await manager.send("📊 Отчёты")
    await manager.click("Технические и ремонтные")
    await manager.click("Сегодня")
    report = OUT.texts_for(5002)
    check("Отчёт УЦЦП" in report, "Отчёт построен")
    check("Всего заявок: <b>1</b>" in report, "Заявка попала в отчёт")
    check("Moose Opera" in report, "Разрез по точкам есть")
    await manager.click("Скачать Excel")
    check("Выгрузка заявок" in OUT.texts_for(5002), "Excel-файл отправлен")

    # ---------------- сценарий без исполнителя ----------------
    print("\n=== Сценарий п.51-2: направление без мастера ===")
    OUT.clear()
    await manager.send("➕ Новая заявка")
    await manager.click("Moose Café")
    await manager.click("Moose Opera")
    await manager.click("Ремонт / мебель")
    await manager.send("Сломалась дверца шкафа в подсобке, нужно заменить петли.")
    await manager.click("Пропустить")
    await manager.click("Средний")
    await manager.click("Завтра")
    await manager.click("12:00")
    await manager.click("Отправить")
    check("администратору" in OUT.texts_for(5002), "Инициатору сказано, что заявка у админа")
    check("Требуется назначить исполнителя" in OUT.texts_for(5001),
          "Администратор получил заявку на назначение")

    async with SessionMaker() as session:
        req2 = await session.scalar(
            select(Request).where(Request.status == Status.AWAITING_ASSIGNMENT)
        )
        check(req2 is not None, "Заявка не потеряна: ожидает назначения")

    # администратор добавляет мастера и назначает
    OUT.clear()
    await admin.send("⚙️ Администрирование")
    await admin.click("Исполнители")
    await admin.click("Добавить исполнителя")
    await admin.send("Каримов Шухрат")
    await admin.send("+992 900 555 666")
    await admin.send("-")
    check("добавлен" in OUT.texts_for(5001), "Новый мастер добавлен администратором")

    OUT.clear()
    await admin.send("⚙️ Администрирование")
    await admin.click("Ожидают назначения")
    await admin.click(req2.number)
    await admin.click("Назначить исполнителя")
    await admin.click("Каримов")
    async with SessionMaker() as session:
        req2 = await session.scalar(select(Request).where(Request.id == req2.id))
        check(req2.executor_id is not None, "Исполнитель назначен вручную")
        check(req2.status == Status.NEW, "Цепочка продолжается без потери заявки")
    check("не зарегистрирован" in OUT.texts_for(5001),
          "Админ предупреждён: мастер не активировал бота")

    # ---------------- мастер на фиксированной оплате ----------------
    print("\n=== Мастер по кофемашинам: завершение без стоимости ===")
    coffee_master = Client(bot, dp, 5006, "Хакимов Фируз", username="firuz")
    OUT.clear()
    await coffee_master.send("/start")
    await coffee_master.send("Фируз")
    await coffee_master.send("Хакимов")
    await coffee_master.send("Мастер по кофемашинам")
    await coffee_master.click("Оба бренда")

    await admin.send("⚙️ Администрирование")
    await admin.click("Заявки на регистрацию")
    await admin.click("Хакимов")
    await admin.click("Подтвердить")
    await admin.click("Мастер / исполнитель")
    await admin.click("Кофемашины")
    await admin.click("Готово")
    await admin.click("Все бренды и объекты")

    # переводим на фиксированную месячную оплату
    OUT.clear()
    await admin.send("⚙️ Администрирование")
    await admin.click("Пользователи и роли")
    await admin.click("Хакимов")
    check(admin.has_button("Оплата: фиксированная"), "В карточке есть переключатель оплаты")
    await admin.click("Оплата: фиксированная")
    async with SessionMaker() as session:
        cm = await session.scalar(select(User).where(User.tg_id == 5006))
        check(cm.fixed_payment, "Мастер переведён на фиксированную оплату")
    check("стоимость вводить не нужно" in OUT.texts_for(5006),
          "Мастеру сообщили об изменении условий")

    # заявка по кофемашине
    OUT.clear()
    await manager.send("➕ Новая заявка")
    await manager.click("Moose Café")
    await manager.click("Moose Opera")
    await manager.click("Кофемашины")
    await manager.send("Кофемашина не держит давление, слабая экстракция.")
    await manager.click("Пропустить")
    await manager.click("Высокий")
    await manager.click("Завтра")
    await manager.click("10:00")
    await manager.click("Отправить")
    check("Новая заявка" in OUT.texts_for(5006), "Заявка ушла мастеру по кофемашинам")

    await coffee_master.click("Принять")
    await coffee_master.click("Начать работу")
    await coffee_master.click("Работа выполнена")
    check("какие работы проведены" in OUT.texts_for(5006).lower(),
          "Спрашивают только перечень работ")
    check("Шаг 1 из 2" in OUT.texts_for(5006), "Всего два шага вместо четырёх")
    check("Стоимость указывать не нужно" in OUT.texts_for(5006),
          "Мастеру сразу сказано, что деньги вводить не нужно")

    OUT.clear()
    await coffee_master.send("Промывка группы, замена фильтра, калибровка помола.")
    check("не обязательно" in OUT.texts_for(5006), "Фото необязательное")
    await coffee_master.send("пропустить")
    report_text = OUT.texts_for(5006)
    check("отмечена как выполненная" in report_text, "Заявка закрыта без фото и без сумм")
    check("стоимость" in report_text.lower() and "фиксированную" in report_text.lower(),
          "Мастеру объяснили, почему суммы нет")
    check("Укажите" not in report_text or "стоимость работ" not in report_text,
          "Вопроса о стоимости не было")

    async with SessionMaker() as session:
        creq = await session.scalar(
            select(Request).where(Request.status == Status.DONE).order_by(Request.id.desc())
        )
        check(creq.cost_exempt, "На заявке отметка «фиксированная оплата»")
        check(creq.work_cost is None and creq.material_cost is None,
              "Суммы не заведены вовсе")
        check("Промывка группы" in creq.executor_comment, "Перечень работ сохранён")

    # в карточке у менеджера — пометка вместо сумм
    check("фиксированная ежемесячная" in OUT.texts_for(5002).lower(),
          "В карточке заявки видно, что суммы нет")

    # ---------------- организационные заявки УЦЦП ----------------
    print("\n=== Модуль организационных заявок УЦЦП ===")
    OUT.clear()
    await manager.send("🗂 Заявки УЦЦП")
    check("Организационные заявки УЦЦП" in OUT.texts_for(5002), "Раздел открывается")
    await manager.click("Новая заявка УЦЦП")
    await manager.click("Moose Café")
    await manager.click("Moose Opera")
    check("Опишите" in OUT.texts_for(5002), "Бот просит описание задачи")
    await manager.send("Подготовить документы по продлению аренды до конца недели.")
    await manager.click("Средний")
    await manager.click("Завтра")
    await manager.click("12:00")
    check("ответственного" in OUT.texts_for(5002).lower(), "Бот просит выбрать ответственного")
    await manager.click("Назрулло")
    check("Проверьте организационную заявку" in OUT.texts_for(5002), "Показан предпросмотр")
    check("Рахимова" in OUT.texts_for(5002), "Автор подставлен из профиля автоматически")
    await manager.click("Отправить")
    check("создана" in OUT.texts_for(5002), "Заявка УЦЦП создана")
    check("Новая организационная заявка" in OUT.texts_for(5003),
          "Ответственный получил уведомление")

    async with SessionMaker() as session:
        org_req = await session.scalar(select(OrgRequest))
        check(org_req is not None and org_req.number.startswith("ORG-"),
              f"Своя нумерация: {org_req.number}")
        check(org_req.author_id is not None, "Автор сохранён")
        org_number = org_req.number

    # ответственный выполняет
    await electrician.click("Взять в работу")
    async with SessionMaker() as session:
        org_req = await session.scalar(select(OrgRequest))
        check(org_req.status == OrgStatus.IN_PROGRESS, "Статус «В работе»")

    await electrician.click("Выполнено")
    await electrician.send("Документы собраны и переданы в офис.")
    await electrician.send("0")
    async with SessionMaker() as session:
        org_req = await session.scalar(select(OrgRequest))
        check(org_req.status == OrgStatus.DONE, "Статус «Выполнена», но не закрыта")
    check("проверьте результат" in OUT.texts_for(5002).lower(),
          "Автору пришёл запрос на проверку")

    OUT.clear()
    await manager.send("🗂 Заявки УЦЦП")
    await manager.click("Мои заявки")
    await manager.click(org_number)
    await manager.click("Подтвердить и закрыть")
    async with SessionMaker() as session:
        org_req = await session.scalar(select(OrgRequest))
        check(org_req.status == OrgStatus.CLOSED, "Автор закрыл заявку УЦЦП")

    # статистика не смешивается
    print("\n=== Отчёты: модули раздельно ===")
    OUT.clear()
    await manager.send("📊 Отчёты")
    check(manager.has_button("Организационные УЦЦП"), "Предложен выбор модуля")
    await manager.click("Организационные УЦЦП")
    await manager.click("Сегодня")
    org_report = OUT.texts_for(5002)
    check("Организационные заявки УЦЦП" in org_report, "Отчёт по модулю УЦЦП")
    check("REQ-" not in org_report, "В отчёте УЦЦП нет ремонтных заявок")

    OUT.clear()
    await admin.send("📊 Отчёты")
    check(admin.has_button("Сводный отчёт"), "Сводный отчёт доступен администратору")
    await admin.click("Сводный отчёт")
    await admin.click("Сегодня")
    combined = OUT.texts_for(5001)
    check("Технические и ремонтные заявки" in combined and "Организационные" in combined,
          "Сводный отчёт показывает оба модуля раздельно")
    await admin.click("Скачать Excel")
    check("Каждый модуль — на своём листе" in OUT.texts_for(5001),
          "Сводная выгрузка Excel двумя листами")

    OUT.clear()
    await manager.send("📊 Отчёты")
    check(not manager.has_button("Сводный отчёт"),
          "Сводный отчёт скрыт от неадминистраторов")

    # ---------------- помощь ----------------
    print("\n=== Помощь ===")
    OUT.clear()
    await manager.send("📖 Инструкция / Помощь")
    await manager.click("Как создать заявку")
    check("Новая заявка" in OUT.texts_for(5002), "Инструкция открывается")

    await bot.session.close()
    print("\n🎉 Все диалоги отработали корректно.")


if __name__ == "__main__":
    asyncio.run(main())
