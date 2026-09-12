"""Мастер создания заявки (п.5-п.17). Максимум кнопок, минимум ручного ввода."""
from __future__ import annotations

import datetime as dt
from typing import Any, Dict, Optional

from aiogram import Bot, F, Router, types
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import texts
from ..callbacks import CalendarCB, WizardCB
from ..db.models import (
    Attachment,
    Brand,
    Category,
    Outlet,
    Priority,
    Request,
    RoleCode,
    Status,
    User,
)
from ..keyboards.calendar import calendar_kb, time_kb
from ..keyboards.common import (
    BTN_NEW,
    edit_fields_kb,
    main_menu,
    media_kb,
    preview_kb,
    priority_kb,
    wizard_kb,
)
from ..services import flow, history, routing
from ..services.cards import render_card, render_preview
from ..services.numbering import new_external_id, next_request_number
from ..states import NewRequest
from ..utils import esc, fmt_dt, utc_from_local

router = Router(name="create")

MAX_MEDIA = 10


# --------------------------------------------------------------------------- #
# Точка входа
# --------------------------------------------------------------------------- #
@router.message(F.text == BTN_NEW)
async def start_new_request(
    message: types.Message, state: FSMContext, session: AsyncSession, user: Optional[User]
) -> None:
    if user is None or not user.is_registered:
        await message.answer(texts.NOT_REGISTERED)
        return
    await state.clear()
    await state.set_state(NewRequest.brand)
    await state.update_data(media=[])
    await ask_brand(message, state, session, user, edit=False)


async def ask_brand(
    target: types.Message,
    state: FSMContext,
    session: AsyncSession,
    user: Optional[User],
    edit: bool,
) -> None:
    brands = (
        await session.scalars(
            select(Brand).where(Brand.is_active.is_(True)).order_by(Brand.sort_order)
        )
    ).all()
    await state.set_state(NewRequest.brand)
    kb = wizard_kb("brand", brands, "name", per_row=1, with_back=False)
    if edit:
        await target.edit_text(texts.STEP_BRAND, reply_markup=kb)
    else:
        await target.answer(texts.STEP_BRAND, reply_markup=kb)


@router.callback_query(NewRequest.brand, WizardCB.filter(F.step == "brand"))
async def pick_brand(
    call: types.CallbackQuery,
    callback_data: WizardCB,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    brand = await session.get(Brand, int(callback_data.value))
    await state.update_data(brand_id=brand.id, brand_name=brand.name)
    await call.answer()
    await ask_outlet(call.message, state, session, edit=True)


async def ask_outlet(
    target: types.Message, state: FSMContext, session: AsyncSession, edit: bool
) -> None:
    data = await state.get_data()
    outlets = (
        await session.scalars(
            select(Outlet)
            .where(Outlet.brand_id == data["brand_id"], Outlet.is_active.is_(True))
            .order_by(Outlet.sort_order, Outlet.name)
        )
    ).all()
    await state.set_state(NewRequest.outlet)
    if not outlets:
        await target.answer(
            "У этого бренда пока нет активных торговых точек. "
            "Обратитесь к администратору УЦЦП."
        )
        return
    kb = wizard_kb("outlet", outlets, "name", per_row=1)
    if edit:
        await target.edit_text(texts.STEP_OUTLET, reply_markup=kb)
    else:
        await target.answer(texts.STEP_OUTLET, reply_markup=kb)


@router.callback_query(NewRequest.outlet, WizardCB.filter(F.step == "outlet"))
async def pick_outlet(
    call: types.CallbackQuery,
    callback_data: WizardCB,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    outlet = await session.get(Outlet, int(callback_data.value))
    await state.update_data(outlet_id=outlet.id, outlet_name=outlet.name)
    await call.answer()
    if await _return_to_preview(call.message, state, session):
        return
    await ask_category(call.message, state, session, edit=True)


async def ask_category(
    target: types.Message, state: FSMContext, session: AsyncSession, edit: bool
) -> None:
    categories = (
        await session.scalars(
            select(Category).where(Category.is_active.is_(True)).order_by(Category.sort_order)
        )
    ).all()
    await state.set_state(NewRequest.category)
    kb = wizard_kb("category", categories, "label", per_row=1)
    if edit:
        await target.edit_text(texts.STEP_CATEGORY, reply_markup=kb)
    else:
        await target.answer(texts.STEP_CATEGORY, reply_markup=kb)


@router.callback_query(NewRequest.category, WizardCB.filter(F.step == "category"))
async def pick_category(
    call: types.CallbackQuery,
    callback_data: WizardCB,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    category = await session.get(Category, int(callback_data.value))
    await state.update_data(
        category_id=category.id,
        category_name=category.label,
        photo_required=category.photo_required,
    )
    await call.answer()
    if await _return_to_preview(call.message, state, session):
        return
    await state.set_state(NewRequest.description)
    await call.message.edit_text(texts.STEP_DESCRIPTION)


@router.message(NewRequest.description, F.text)
async def set_description(
    message: types.Message, state: FSMContext, session: AsyncSession
) -> None:
    text = message.text.strip()
    if len(text) < 5:
        await message.answer(
            "Опишите проблему подробнее — минимум 5 символов. "
            "Что именно не работает и что нужно сделать?"
        )
        return
    await state.update_data(description=text)
    if await _return_to_preview(message, state, session):
        return
    await ask_media(message, state)


async def ask_media(target: types.Message, state: FSMContext) -> None:
    data = await state.get_data()
    await state.set_state(NewRequest.media)
    required = bool(data.get("photo_required"))
    count = len(data.get("media", []))
    prompt = texts.STEP_MEDIA
    if required:
        prompt += "\n\n" + texts.STEP_MEDIA_REQUIRED
    if count:
        prompt += f"\n\nУже прикреплено: {count}"
    await target.answer(prompt, reply_markup=media_kb(can_skip=not required))


@router.message(NewRequest.media, F.photo | F.video | F.document)
async def collect_media(message: types.Message, state: FSMContext) -> None:
    data = await state.get_data()
    media: list = list(data.get("media", []))
    if len(media) >= MAX_MEDIA:
        await message.answer(f"Можно прикрепить не более {MAX_MEDIA} файлов.")
        return

    if message.photo:
        item = {"file_id": message.photo[-1].file_id, "type": "photo",
                "unique": message.photo[-1].file_unique_id}
    elif message.video:
        item = {"file_id": message.video.file_id, "type": "video",
                "unique": message.video.file_unique_id}
    else:
        item = {"file_id": message.document.file_id, "type": "document",
                "unique": message.document.file_unique_id}

    media.append(item)
    await state.update_data(media=media)
    await message.answer(
        f"Принято ({len(media)}). Можно отправить ещё или нажать «Готово».",
        reply_markup=media_kb(can_skip=not data.get("photo_required")),
    )


@router.callback_query(NewRequest.media, WizardCB.filter(F.step.in_({"media_done", "media_skip"})))
async def media_done(
    call: types.CallbackQuery,
    callback_data: WizardCB,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    data = await state.get_data()
    if data.get("photo_required") and not data.get("media"):
        await call.answer(texts.STEP_MEDIA_REQUIRED, show_alert=True)
        return
    await call.answer()
    if await _return_to_preview(call.message, state, session):
        return
    await state.set_state(NewRequest.priority)
    await call.message.edit_text(texts.STEP_PRIORITY, reply_markup=priority_kb())


@router.callback_query(NewRequest.priority, WizardCB.filter(F.step == "priority"))
async def pick_priority(
    call: types.CallbackQuery,
    callback_data: WizardCB,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    await state.update_data(
        priority=callback_data.value, priority_title=Priority.title(callback_data.value)
    )
    await call.answer()
    if await _return_to_preview(call.message, state, session):
        return
    await state.set_state(NewRequest.due_date)
    await call.message.edit_text(texts.STEP_DUE_DATE, reply_markup=calendar_kb())


# --- календарь ---
@router.callback_query(CalendarCB.filter(F.act == "ignore"))
async def calendar_ignore(call: types.CallbackQuery) -> None:
    await call.answer()


@router.callback_query(NewRequest.due_date, CalendarCB.filter(F.act == "nav"))
async def calendar_nav(
    call: types.CallbackQuery, callback_data: CalendarCB
) -> None:
    await call.answer()
    await call.message.edit_reply_markup(
        reply_markup=calendar_kb(callback_data.year, callback_data.month)
    )


@router.callback_query(NewRequest.due_date, CalendarCB.filter(F.act == "day"))
async def calendar_day(
    call: types.CallbackQuery, callback_data: CalendarCB, state: FSMContext
) -> None:
    await state.update_data(
        due_year=callback_data.year, due_month=callback_data.month, due_day=callback_data.day
    )
    await state.set_state(NewRequest.due_time)
    await call.answer()
    date = dt.date(callback_data.year, callback_data.month, callback_data.day)
    await call.message.edit_text(
        f"Дата: <b>{date.strftime('%d.%m.%Y')}</b>\n\n" + texts.STEP_DUE_TIME,
        reply_markup=time_kb(),
    )


@router.callback_query(NewRequest.due_time, CalendarCB.filter(F.act == "time"))
async def calendar_time(
    call: types.CallbackQuery,
    callback_data: CalendarCB,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    data = await state.get_data()
    due_local = dt.datetime(
        data["due_year"], data["due_month"], data["due_day"],
        callback_data.hour, callback_data.minute,
    )
    await state.update_data(
        due_local=due_local.isoformat(),
        due_title=due_local.strftime("%d.%m.%Y, %H:%M"),
    )
    await call.answer()
    await show_preview(call.message, state, session, edit=True)


# --------------------------------------------------------------------------- #
# Предпросмотр и отправка
# --------------------------------------------------------------------------- #
async def show_preview(
    target: types.Message, state: FSMContext, session: AsyncSession, edit: bool
) -> None:
    data = await state.get_data()
    await state.set_state(NewRequest.preview)
    await state.update_data(edit_mode=False)

    executor, reason = await routing.resolve_route(
        session, data["category_id"], data["brand_id"], data["outlet_id"]
    )
    if reason == "none":
        executor_label = "не найден — заявку получит администратор УЦЦП"
    elif reason == "unregistered":
        executor_label = f"{executor.full_name} (ещё не зарегистрирован в боте)"
    else:
        executor_label = executor.full_name

    text = texts.PREVIEW_HINT + "\n\n" + render_preview(data, executor_label)
    if edit:
        await target.edit_text(text, reply_markup=preview_kb())
    else:
        await target.answer(text, reply_markup=preview_kb())


async def _return_to_preview(
    target: types.Message, state: FSMContext, session: AsyncSession
) -> bool:
    """Если пользователь правил поле из предпросмотра — возвращаем его туда же."""
    data = await state.get_data()
    if not data.get("edit_mode"):
        return False
    # Сообщение бота можно отредактировать, сообщение пользователя — нет.
    edit = bool(target.from_user and target.from_user.is_bot)
    await show_preview(target, state, session, edit=edit)
    return True


@router.callback_query(NewRequest.preview, WizardCB.filter(F.step == "edit"))
async def preview_edit(call: types.CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await call.message.edit_text(
        "Что нужно изменить?", reply_markup=edit_fields_kb()
    )


@router.callback_query(NewRequest.preview, WizardCB.filter(F.step == "preview"))
async def preview_back(
    call: types.CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    await call.answer()
    await show_preview(call.message, state, session, edit=True)


@router.callback_query(NewRequest.preview, WizardCB.filter(F.step == "edit_field"))
async def preview_edit_field(
    call: types.CallbackQuery,
    callback_data: WizardCB,
    state: FSMContext,
    session: AsyncSession,
    user: Optional[User],
) -> None:
    field = callback_data.value
    await state.update_data(edit_mode=True)
    await call.answer()

    if field == "brand":
        await state.update_data(edit_mode=False)  # смена бренда сбрасывает точку
        await ask_brand(call.message, state, session, user, edit=True)
    elif field == "outlet":
        await ask_outlet(call.message, state, session, edit=True)
    elif field == "category":
        await ask_category(call.message, state, session, edit=True)
    elif field == "description":
        await state.set_state(NewRequest.description)
        await call.message.edit_text(texts.STEP_DESCRIPTION)
    elif field == "media":
        await state.update_data(media=[])
        await call.message.edit_text("Прежние файлы удалены.")
        await ask_media(call.message, state)
    elif field == "priority":
        await state.set_state(NewRequest.priority)
        await call.message.edit_text(texts.STEP_PRIORITY, reply_markup=priority_kb())
    elif field == "due":
        await state.update_data(edit_mode=False)
        await state.set_state(NewRequest.due_date)
        await call.message.edit_text(texts.STEP_DUE_DATE, reply_markup=calendar_kb())


@router.callback_query(NewRequest.preview, WizardCB.filter(F.step == "submit"))
async def submit_request(
    call: types.CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    user: Optional[User],
    bot: Bot,
) -> None:
    data = await state.get_data()
    missing = [
        name
        for key, name in (
            ("brand_id", "бренд"),
            ("outlet_id", "торговая точка"),
            ("category_id", "категория"),
            ("description", "описание"),
            ("priority", "приоритет"),
            ("due_local", "срок"),
        )
        if not data.get(key)
    ]
    if missing:
        await call.answer(
            "Не заполнено: " + ", ".join(missing) + ". Заполните и повторите.",
            show_alert=True,
        )
        return

    await call.answer("Создаю заявку…")
    due_local = dt.datetime.fromisoformat(data["due_local"])

    request = Request(
        number=await next_request_number(session),
        external_id=new_external_id(),
        author_id=user.id,
        brand_id=data["brand_id"],
        outlet_id=data["outlet_id"],
        category_id=data["category_id"],
        description=data["description"],
        priority=data["priority"],
        due_at=utc_from_local(due_local),
        status=Status.NEW,
    )
    session.add(request)
    await session.flush()

    for item in data.get("media", []):
        session.add(
            Attachment(
                request_id=request.id,
                file_id=item["file_id"],
                file_unique_id=item.get("unique"),
                media_type=item["type"],
                stage="before",
                uploaded_by_id=user.id,
            )
        )
    await session.flush()
    await session.refresh(request, ["attachments", "brand", "outlet", "category", "author"])

    await history.log(
        session, request, "created", user=user, new_status=Status.NEW,
        details=f"Срок: {fmt_dt(request.due_at)}",
    )

    reason = await flow.dispatch_request(session, bot, request, actor=user)
    await session.commit()
    await state.clear()

    confirm = f"✅ Заявка <b>{esc(request.number)}</b> создана.\n\n" + render_card(request)
    if reason == "ok":
        confirm += (
            f"\n\nЗаявка отправлена исполнителю: <b>{esc(request.executor.full_name)}</b>. "
            "Вы получите уведомление, когда он её примет."
        )
    elif reason == "unregistered":
        confirm += "\n\n" + texts.EXECUTOR_NOT_REGISTERED
    else:
        confirm += "\n\n" + texts.NO_EXECUTOR_USER

    await call.message.edit_text(confirm)
    await call.message.answer(texts.MAIN_MENU_HINT, reply_markup=main_menu(user))


# --------------------------------------------------------------------------- #
# Навигация: назад / отмена
# --------------------------------------------------------------------------- #
BACK_MAP = {
    NewRequest.outlet.state: "brand",
    NewRequest.category.state: "outlet",
    NewRequest.description.state: "category",
    NewRequest.media.state: "description",
    NewRequest.priority.state: "media",
    NewRequest.due_date.state: "priority",
    NewRequest.due_time.state: "due_date",
}


@router.callback_query(StateFilter(NewRequest), WizardCB.filter(F.step == "back"))
async def wizard_back(
    call: types.CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    user: Optional[User],
) -> None:
    current = await state.get_state()
    target = BACK_MAP.get(current)
    await call.answer()

    if target == "brand":
        await ask_brand(call.message, state, session, user, edit=True)
    elif target == "outlet":
        await ask_outlet(call.message, state, session, edit=True)
    elif target == "category":
        await ask_category(call.message, state, session, edit=True)
    elif target == "description":
        await state.set_state(NewRequest.description)
        await call.message.edit_text(texts.STEP_DESCRIPTION)
    elif target == "media":
        await call.message.delete()
        await ask_media(call.message, state)
    elif target == "priority":
        await state.set_state(NewRequest.priority)
        await call.message.edit_text(texts.STEP_PRIORITY, reply_markup=priority_kb())
    elif target == "due_date":
        await state.set_state(NewRequest.due_date)
        await call.message.edit_text(texts.STEP_DUE_DATE, reply_markup=calendar_kb())
    else:
        await call.message.edit_text("Это первый шаг. Выберите бренд:")
        await ask_brand(call.message, state, session, user, edit=False)


@router.callback_query(StateFilter(NewRequest), WizardCB.filter(F.step == "cancel"))
async def wizard_cancel(
    call: types.CallbackQuery, state: FSMContext, user: Optional[User]
) -> None:
    await state.clear()
    await call.answer("Отменено")
    await call.message.edit_text("❌ Создание заявки отменено.")
    await call.message.answer(texts.MAIN_MENU_HINT, reply_markup=main_menu(user))


@router.message(StateFilter(NewRequest))
async def wizard_wrong_input(message: types.Message, state: FSMContext) -> None:
    current = await state.get_state()
    hints = {
        NewRequest.brand.state: "Выберите бренд кнопкой выше.",
        NewRequest.outlet.state: "Выберите торговую точку кнопкой выше.",
        NewRequest.category.state: "Выберите категорию кнопкой выше.",
        NewRequest.description.state: "Опишите проблему текстом одним сообщением.",
        NewRequest.media.state: "Отправьте фото/видео или нажмите «Готово».",
        NewRequest.priority.state: "Выберите приоритет кнопкой выше.",
        NewRequest.due_date.state: "Выберите дату в календаре выше.",
        NewRequest.due_time.state: "Выберите время кнопкой выше.",
        NewRequest.preview.state: "Нажмите «Отправить», «Изменить» или «Отменить».",
    }
    await message.answer("Для продолжения: " + hints.get(current, "используйте кнопки выше."))
