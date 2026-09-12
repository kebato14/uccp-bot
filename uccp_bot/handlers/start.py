"""Регистрация пользователя.

Человек сообщает только сведения о себе: имя, фамилия, должность и место работы.
Роль, область доступа и направления мастера назначает администратор бота при
подтверждении заявки. До подтверждения рабочий функционал закрыт.
"""
from __future__ import annotations

from typing import List, Optional

from aiogram import Bot, F, Router, types
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import texts
from ..callbacks import ApprCB, RegCB
from ..db.models import Brand, Outlet, RoleCode, User, UserStatus
from ..keyboards.common import BTN_CANCEL, cancel_kb, main_menu, remove_kb
from ..services import notify, routing
from ..states import Registration
from ..utils import esc, fmt_dt, utcnow

router = Router(name="start")


async def _link_pending_account(
    session: AsyncSession, tg_user: types.User
) -> Optional[User]:
    """Мастер, заведённый администратором заранее, узнаётся по @username."""
    if not tg_user.username:
        return None
    user = await session.scalar(
        select(User).where(
            User.tg_id.is_(None),
            func.lower(User.username) == tg_user.username.lower(),
        )
    )
    if user is None:
        return None
    user.tg_id = tg_user.id
    user.is_registered = True
    user.username = tg_user.username
    await session.flush()
    return user


@router.message(CommandStart())
async def cmd_start(
    message: types.Message,
    state: FSMContext,
    session: AsyncSession,
    user: Optional[User],
    bot: Bot,
) -> None:
    await state.clear()

    if user is None:
        user = await _link_pending_account(session, message.from_user)
        if user is not None and user.is_approved:
            await session.commit()
            await message.answer(
                f"👋 Здравствуйте, <b>{esc(user.display_name)}</b>!\n"
                f"Ваш профиль уже заведён администратором.\n"
                f"Роль: <b>{user.role_title}</b>\n\n"
                "Теперь заявки будут приходить вам сюда, в личные сообщения.",
                reply_markup=main_menu(user),
            )
            return

    if user is not None:
        if user.is_approved:
            await message.answer(
                f"С возвращением, <b>{esc(user.display_name)}</b>!\n"
                f"Роль: <b>{user.role_title}</b>\n\n" + texts.MAIN_MENU_HINT,
                reply_markup=main_menu(user),
            )
            return
        if user.status == UserStatus.PENDING:
            await message.answer(texts.PENDING_HINT, reply_markup=remove_kb())
            return
        if user.status == UserStatus.BLOCKED:
            await message.answer(texts.BLOCKED_HINT, reply_markup=remove_kb())
            return
        if user.status == UserStatus.DISABLED:
            await message.answer(texts.DISABLED_HINT, reply_markup=remove_kb())
            return
        if user.status == UserStatus.REJECTED:
            reason = (
                f"Причина: <i>{esc(user.reject_reason)}</i>"
                if user.reject_reason
                else ""
            )
            kb = InlineKeyboardBuilder()
            kb.button(text="🔄 Подать заявку заново", callback_data=RegCB(step="restart").pack())
            await message.answer(
                texts.REJECTED_HINT.format(reason=reason), reply_markup=kb.as_markup()
            )
            return

    await state.update_data(existing_user_id=user.id if user else None)
    await _begin(message, state)


async def _begin(message: types.Message, state: FSMContext) -> None:
    await state.set_state(Registration.first_name)
    await message.answer(texts.WELCOME, reply_markup=remove_kb())
    await message.answer(texts.ASK_FIRST_NAME, reply_markup=cancel_kb())


@router.callback_query(RegCB.filter(F.step == "restart"))
async def restart_registration(
    call: types.CallbackQuery, state: FSMContext, user: Optional[User]
) -> None:
    await state.clear()
    await state.update_data(existing_user_id=user.id if user else None)
    await call.answer()
    await _begin(call.message, state)


@router.message(StateFilter(Registration), F.text == BTN_CANCEL)
async def cancel_registration(message: types.Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "Регистрация отменена. Чтобы начать заново — отправьте /start.",
        reply_markup=remove_kb(),
    )


@router.message(Registration.first_name, F.text)
async def reg_first_name(message: types.Message, state: FSMContext) -> None:
    value = message.text.strip()
    if len(value) < 2:
        await message.answer("Введите, пожалуйста, имя — минимум 2 символа.")
        return
    await state.update_data(first_name=value)
    await state.set_state(Registration.last_name)
    await message.answer(texts.ASK_LAST_NAME)


@router.message(Registration.last_name, F.text)
async def reg_last_name(message: types.Message, state: FSMContext) -> None:
    value = message.text.strip()
    if len(value) < 2:
        await message.answer("Введите, пожалуйста, фамилию — минимум 2 символа.")
        return
    await state.update_data(last_name=value)
    await state.set_state(Registration.position)
    await message.answer(texts.ASK_POSITION)


@router.message(Registration.position, F.text)
async def reg_position(
    message: types.Message, state: FSMContext, session: AsyncSession
) -> None:
    value = message.text.strip()
    if len(value) < 3:
        await message.answer("Опишите должность чуть подробнее.")
        return
    await state.update_data(position=value)

    brands = (
        await session.scalars(
            select(Brand).where(Brand.is_active.is_(True)).order_by(Brand.sort_order)
        )
    ).all()
    kb = InlineKeyboardBuilder()
    for brand in brands:
        kb.button(text=brand.name, callback_data=RegCB(step="brand", value=str(brand.id)).pack())
    kb.button(text="Оба бренда / офис", callback_data=RegCB(step="brand", value="0").pack())
    kb.adjust(1)
    await state.set_state(Registration.brand)
    await message.answer(texts.ASK_BRAND_REG, reply_markup=kb.as_markup())


@router.callback_query(Registration.brand, RegCB.filter(F.step == "brand"))
async def reg_brand(
    call: types.CallbackQuery,
    callback_data: RegCB,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
    user: Optional[User],
) -> None:
    brand_id = int(callback_data.value) or None
    await state.update_data(brand_id=brand_id)
    await call.answer()

    if brand_id is None:
        await _submit(call.message, state, session, call.from_user, bot)
        return

    outlets = (
        await session.scalars(
            select(Outlet)
            .where(Outlet.brand_id == brand_id, Outlet.is_active.is_(True))
            .order_by(Outlet.sort_order, Outlet.name)
        )
    ).all()
    kb = InlineKeyboardBuilder()
    for outlet in outlets:
        kb.button(
            text=outlet.name, callback_data=RegCB(step="outlet", value=str(outlet.id)).pack()
        )
    kb.button(text="Несколько точек", callback_data=RegCB(step="outlet", value="0").pack())
    kb.adjust(1)
    await state.set_state(Registration.outlet)
    await call.message.edit_text(texts.ASK_OUTLET_REG, reply_markup=kb.as_markup())


@router.callback_query(Registration.outlet, RegCB.filter(F.step == "outlet"))
async def reg_outlet(
    call: types.CallbackQuery,
    callback_data: RegCB,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
) -> None:
    await state.update_data(outlet_id=int(callback_data.value) or None)
    await call.answer()
    await _submit(call.message, state, session, call.from_user, bot)


async def _submit(
    message: types.Message,
    state: FSMContext,
    session: AsyncSession,
    tg_user: types.User,
    bot: Bot,
) -> None:
    """Сохраняем заявку и отправляем её администраторам."""
    data = await state.get_data()

    user: Optional[User] = None
    if data.get("existing_user_id"):
        user = await session.get(User, data["existing_user_id"])
    if user is None:
        user = await session.scalar(select(User).where(User.tg_id == tg_user.id))
    if user is None:
        user = User()
        session.add(user)

    user.tg_id = tg_user.id
    user.username = tg_user.username
    user.first_name = data["first_name"]
    user.last_name = data["last_name"]
    user.full_name = f"{data['last_name']} {data['first_name']}"
    user.position_text = data["position"]
    user.brand_id = data.get("brand_id")
    user.outlet_id = data.get("outlet_id")
    user.status = UserStatus.PENDING
    user.is_registered = True
    user.reject_reason = None
    user.applied_at = utcnow()
    user.sync_flags()
    await session.flush()

    brand = await session.get(Brand, user.brand_id) if user.brand_id else None
    outlet = await session.get(Outlet, user.outlet_id) if user.outlet_id else None
    place = " · ".join(
        p for p in (
            brand.name if brand else "оба бренда / офис",
            outlet.name if outlet else ("несколько точек" if brand else None),
        ) if p
    )

    summary = "\n".join(
        [
            f"Ф.И.О.: <b>{esc(user.full_name)}</b>",
            f"Должность: {esc(user.position_text)}",
            f"Место работы: {esc(place)}",
        ]
    )
    await session.commit()
    await state.clear()
    await message.answer(texts.REG_SUBMITTED.format(summary=summary))

    # уведомляем администраторов
    kb = InlineKeyboardBuilder()
    kb.button(
        text="✅ Подтвердить", callback_data=ApprCB(act="approve", user_id=user.id).pack()
    )
    kb.button(
        text="❌ Отклонить", callback_data=ApprCB(act="reject", user_id=user.id).pack()
    )
    kb.adjust(2)
    text = texts.ADMIN_NEW_APPLICATION.format(
        number=user.id,
        name=esc(user.full_name),
        position=esc(user.position_text),
        place=esc(place),
        telegram=f"@{esc(user.username)}" if user.username else "без username",
        applied=fmt_dt(user.applied_at),
    )
    sent = await notify.notify_admins(session, bot, text, kb.as_markup())
    if not sent:
        log_admins = await routing.admins(session)
        if not log_admins:
            await message.answer(texts.NO_ADMINS)


@router.message(Registration.brand)
@router.message(Registration.outlet)
async def reg_use_buttons(message: types.Message) -> None:
    await message.answer("Выберите вариант кнопкой выше.")


@router.message(Command("menu"))
async def cmd_menu(message: types.Message, state: FSMContext, user: Optional[User]) -> None:
    await state.clear()
    if user is None or not user.is_registered:
        await message.answer(texts.NOT_REGISTERED)
        return
    if not user.is_approved:
        await message.answer(texts.PENDING_HINT)
        return
    await message.answer(texts.MAIN_MENU_HINT, reply_markup=main_menu(user))


@router.message(Command("id"))
async def cmd_id(message: types.Message) -> None:
    await message.answer(f"Ваш Telegram ID: <code>{message.from_user.id}</code>")
