"""Подтверждение регистрации администратором и назначение роли.

Главный принцип: никто не назначает себе роль сам. Пользователь только
сообщает сведения о себе, администратор проверяет и выдаёт доступ.
"""
from __future__ import annotations

from typing import List, Optional

from aiogram import Bot, F, Router, types
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import texts
from ..callbacks import ApprCB
from ..db.models import (
    Brand,
    Category,
    ExecutorAssignment,
    Outlet,
    RoleCode,
    User,
    UserStatus,
)
from ..keyboards.common import cancel_kb, main_menu, BTN_CANCEL
from ..states import Approval
from ..utils import esc, fmt_dt, utcnow

router = Router(name="approval")


def _require_admin(user: Optional[User]) -> bool:
    return user is not None and user.is_admin and user.is_approved


async def pending_count(session: AsyncSession) -> int:
    return int(
        await session.scalar(
            select(func.count(User.id)).where(User.status == UserStatus.PENDING)
        )
        or 0
    )


async def application_text(session: AsyncSession, target: User) -> str:
    brand = await session.get(Brand, target.brand_id) if target.brand_id else None
    outlet = await session.get(Outlet, target.outlet_id) if target.outlet_id else None
    place = " · ".join(
        p for p in (
            brand.name if brand else "оба бренда / офис",
            outlet.name if outlet else ("несколько точек" if brand else None),
        ) if p
    )
    return texts.ADMIN_NEW_APPLICATION.format(
        number=target.id,
        name=esc(target.full_name),
        position=esc(target.position_text or "—"),
        place=esc(place),
        telegram=f"@{esc(target.username)}" if target.username else "без username",
        applied=fmt_dt(target.applied_at),
    )


# --------------------------------------------------------------------------- #
# Список заявок
# --------------------------------------------------------------------------- #
@router.callback_query(ApprCB.filter(F.act == "list"))
async def cb_list(
    call: types.CallbackQuery, session: AsyncSession, user: Optional[User]
) -> None:
    if not _require_admin(user):
        await call.answer(texts.NO_ACCESS, show_alert=True)
        return
    rows = (
        await session.scalars(
            select(User)
            .where(User.status == UserStatus.PENDING)
            .order_by(User.applied_at)
        )
    ).all()
    if not rows:
        await call.answer()
        await call.message.edit_text(
            "🆕 <b>Заявки на регистрацию</b>\n\nНовых заявок нет.",
            reply_markup=_back_kb(),
        )
        return

    kb = InlineKeyboardBuilder()
    for row in rows:
        kb.button(
            text=f"№{row.id} · {row.full_name} · {row.position_text or '—'}"[:60],
            callback_data=ApprCB(act="open", user_id=row.id).pack(),
        )
    kb.adjust(1)
    kb.row(InlineKeyboardButton(text="⬅️ В меню", callback_data=ApprCB(act="root").pack()))
    await call.answer()
    await call.message.edit_text(
        f"🆕 <b>Заявки на регистрацию</b> — {len(rows)} шт.\n\nВыберите заявку:",
        reply_markup=kb.as_markup(),
    )


def _back_kb() -> types.InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ В меню", callback_data=ApprCB(act="root").pack())
    return kb.as_markup()


@router.callback_query(ApprCB.filter(F.act == "open"))
async def cb_open(
    call: types.CallbackQuery,
    callback_data: ApprCB,
    session: AsyncSession,
    user: Optional[User],
) -> None:
    if not _require_admin(user):
        await call.answer(texts.NO_ACCESS, show_alert=True)
        return
    target = await session.get(User, callback_data.user_id)
    if target is None:
        await call.answer("Заявка не найдена", show_alert=True)
        return

    kb = InlineKeyboardBuilder()
    if target.status == UserStatus.PENDING:
        kb.button(
            text="✅ Подтвердить", callback_data=ApprCB(act="approve", user_id=target.id).pack()
        )
        kb.button(
            text="❌ Отклонить", callback_data=ApprCB(act="reject", user_id=target.id).pack()
        )
    kb.adjust(2)
    kb.row(InlineKeyboardButton(text="⬅️ К заявкам", callback_data=ApprCB(act="list").pack()))
    await call.answer()
    text = await application_text(session, target)
    if target.status != UserStatus.PENDING:
        text += f"\n\nТекущий статус: <b>{target.status_title}</b>"
    await call.message.edit_text(text, reply_markup=kb.as_markup())


# --------------------------------------------------------------------------- #
# Подтверждение: роль → бренд → точка / направления
# --------------------------------------------------------------------------- #
@router.callback_query(ApprCB.filter(F.act == "approve"))
async def cb_approve(
    call: types.CallbackQuery,
    callback_data: ApprCB,
    state: FSMContext,
    session: AsyncSession,
    user: Optional[User],
) -> None:
    if not _require_admin(user):
        await call.answer(texts.NO_ACCESS, show_alert=True)
        return
    target = await session.get(User, callback_data.user_id)
    if target is None:
        await call.answer("Пользователь не найден", show_alert=True)
        return

    await state.clear()
    await call.answer()
    await call.message.edit_text(
        f"Подтверждение регистрации <b>№{target.id}</b>\n"
        f"{esc(target.full_name)} · {esc(target.position_text or '—')}\n\n"
        "Шаг 1. Назначьте <b>роль</b> — от неё зависит объём доступа:",
        reply_markup=_roles_kb(target.id),
    )


def _roles_kb(user_id: int) -> types.InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for code in RoleCode.ALL:
        kb.button(
            text=f"{RoleCode.TITLES[code]} — {RoleCode.DESCRIPTIONS[code]}",
            callback_data=ApprCB(act="role", user_id=user_id, value=code).pack(),
        )
    kb.adjust(1)
    kb.row(
        InlineKeyboardButton(
            text="⬅️ Отмена", callback_data=ApprCB(act="open", user_id=user_id).pack()
        )
    )
    return kb.as_markup()


@router.callback_query(ApprCB.filter(F.act == "role"))
async def cb_role(
    call: types.CallbackQuery,
    callback_data: ApprCB,
    session: AsyncSession,
    user: Optional[User],
    bot: Bot,
) -> None:
    """Роль сохраняется сразу; доступ откроется только на последнем шаге."""
    if not _require_admin(user):
        await call.answer(texts.NO_ACCESS, show_alert=True)
        return
    target = await session.get(User, callback_data.user_id)
    if target is None:
        await call.answer("Пользователь не найден", show_alert=True)
        return

    role = callback_data.value
    target.role_code = role
    await session.commit()
    await call.answer()

    # администратор системы и сотрудник УЦЦП не привязаны к бренду и точке
    if role in (RoleCode.ADMIN, RoleCode.UCCP_STAFF):
        await _finish(call, session, user, bot, target)
        return
    if role == RoleCode.EXECUTOR:
        await call.message.edit_text(
            f"{esc(target.full_name)} — <b>{RoleCode.TITLES[role]}</b>\n\n"
            "Шаг 2. Отметьте <b>направления мастера</b> — по ним ему будут "
            "автоматически приходить заявки:",
            reply_markup=await _directions_kb(session, target.id),
        )
        return

    await call.message.edit_text(
        f"{esc(target.full_name)} — <b>{RoleCode.TITLES[role]}</b>\n\n"
        "Шаг 2. Выберите <b>бренд</b> или группу объектов:",
        reply_markup=await _brands_kb(session, target.id, role),
    )


async def _brands_kb(
    session: AsyncSession, user_id: int, role: str
) -> types.InlineKeyboardMarkup:
    brands = (
        await session.scalars(
            select(Brand).where(Brand.is_active.is_(True)).order_by(Brand.sort_order)
        )
    ).all()
    kb = InlineKeyboardBuilder()
    for brand in brands:
        kb.button(
            text=brand.name,
            callback_data=ApprCB(act="brand", user_id=user_id, value=str(brand.id)).pack(),
        )
    if role == RoleCode.EXECUTOR:
        kb.button(
            text="Все бренды и объекты",
            callback_data=ApprCB(act="brand", user_id=user_id, value="0").pack(),
        )
    kb.adjust(1)
    kb.row(
        InlineKeyboardButton(
            text="⬅️ Назад", callback_data=ApprCB(act="approve", user_id=user_id).pack()
        )
    )
    return kb.as_markup()


async def _selected_directions(session: AsyncSession, user_id: int) -> List[int]:
    rows = (
        await session.scalars(
            select(ExecutorAssignment.category_id).where(
                ExecutorAssignment.user_id == user_id
            )
        )
    ).all()
    return list(rows)


async def _directions_kb(
    session: AsyncSession, user_id: int
) -> types.InlineKeyboardMarkup:
    """Отметки читаются из БД — состояние переживает перезапуск бота."""
    selected = await _selected_directions(session, user_id)
    categories = (
        await session.scalars(
            select(Category).where(Category.is_active.is_(True)).order_by(Category.sort_order)
        )
    ).all()
    kb = InlineKeyboardBuilder()
    for cat in categories:
        mark = "✅ " if cat.id in selected else "▫️ "
        kb.button(
            text=f"{mark}{cat.label}",
            callback_data=ApprCB(act="dir", user_id=user_id, value=str(cat.id)).pack(),
        )
    kb.adjust(1)
    kb.row(
        InlineKeyboardButton(
            text="Готово ➡️", callback_data=ApprCB(act="dir_done", user_id=user_id).pack()
        )
    )
    return kb.as_markup()


@router.callback_query(ApprCB.filter(F.act == "dir"))
async def cb_direction(
    call: types.CallbackQuery,
    callback_data: ApprCB,
    session: AsyncSession,
    user: Optional[User],
) -> None:
    if not _require_admin(user):
        await call.answer(texts.NO_ACCESS, show_alert=True)
        return
    cat_id = int(callback_data.value)
    existing = await session.scalar(
        select(ExecutorAssignment).where(
            ExecutorAssignment.user_id == callback_data.user_id,
            ExecutorAssignment.category_id == cat_id,
        )
    )
    if existing is not None:
        await session.delete(existing)
    else:
        session.add(
            ExecutorAssignment(user_id=callback_data.user_id, category_id=cat_id)
        )
    await session.commit()
    await call.answer()
    await call.message.edit_reply_markup(
        reply_markup=await _directions_kb(session, callback_data.user_id)
    )


@router.callback_query(ApprCB.filter(F.act == "dir_done"))
async def cb_directions_done(
    call: types.CallbackQuery,
    callback_data: ApprCB,
    session: AsyncSession,
    user: Optional[User],
) -> None:
    if not _require_admin(user):
        await call.answer(texts.NO_ACCESS, show_alert=True)
        return
    if not await _selected_directions(session, callback_data.user_id):
        await call.answer("Отметьте хотя бы одно направление", show_alert=True)
        return
    await call.answer()
    await call.message.edit_text(
        "Шаг 3. По каким брендам и объектам работает мастер?",
        reply_markup=await _brands_kb(session, callback_data.user_id, RoleCode.EXECUTOR),
    )


@router.callback_query(ApprCB.filter(F.act == "brand"))
async def cb_brand(
    call: types.CallbackQuery,
    callback_data: ApprCB,
    session: AsyncSession,
    user: Optional[User],
    bot: Bot,
) -> None:
    if not _require_admin(user):
        await call.answer(texts.NO_ACCESS, show_alert=True)
        return
    target = await session.get(User, callback_data.user_id)
    brand_id = int(callback_data.value) or None
    target.brand_id = brand_id
    if target.role_code == RoleCode.EXECUTOR:
        # направления мастера привязываем к выбранному бренду
        for assignment in (
            await session.scalars(
                select(ExecutorAssignment).where(
                    ExecutorAssignment.user_id == target.id
                )
            )
        ).all():
            assignment.brand_id = brand_id
    await session.commit()
    await call.answer()

    if target.role_code in (RoleCode.OPS_DIRECTOR, RoleCode.EXECUTOR) or brand_id is None:
        target.outlet_id = None
        await session.commit()
        await _finish(call, session, user, bot, target)
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
            text=outlet.name,
            callback_data=ApprCB(act="outlet", user_id=target.id, value=str(outlet.id)).pack(),
        )
    kb.adjust(1)
    kb.row(
        InlineKeyboardButton(
            text="⬅️ Назад", callback_data=ApprCB(act="role", user_id=target.id,
                                                 value=target.role_code).pack()
        )
    )
    await call.message.edit_text(
        "Шаг 3. Выберите <b>объект</b>:", reply_markup=kb.as_markup()
    )


@router.callback_query(ApprCB.filter(F.act == "outlet"))
async def cb_outlet(
    call: types.CallbackQuery,
    callback_data: ApprCB,
    session: AsyncSession,
    user: Optional[User],
    bot: Bot,
) -> None:
    if not _require_admin(user):
        await call.answer(texts.NO_ACCESS, show_alert=True)
        return
    target = await session.get(User, callback_data.user_id)
    target.outlet_id = int(callback_data.value) or None
    await session.commit()
    await call.answer()
    await _finish(call, session, user, bot, target)


async def _finish(
    call: types.CallbackQuery,
    session: AsyncSession,
    admin: User,
    bot: Bot,
    target: User,
) -> None:
    """Последний шаг: выдаём доступ и уведомляем человека."""
    target.status = UserStatus.ACTIVE
    target.approved_at = utcnow()
    target.approved_by_id = admin.id
    target.reject_reason = None
    target.sync_flags()
    await session.commit()

    role = target.role_code
    brand = await session.get(Brand, target.brand_id) if target.brand_id else None
    outlet = await session.get(Outlet, target.outlet_id) if target.outlet_id else None

    names: List[str] = []
    if role == RoleCode.EXECUTOR:
        for cat_id in await _selected_directions(session, target.id):
            category = await session.get(Category, cat_id)
            if category:
                names.append(category.name)

    scope_lines = {
        RoleCode.STAFF: f"Объект: {outlet.name if outlet else '—'}",
        RoleCode.OUTLET_ADMIN: f"Объект: {outlet.name if outlet else '—'}",
        RoleCode.OPS_DIRECTOR: f"Бренд: {brand.name if brand else '—'}",
        RoleCode.EXECUTOR: "Направления: " + (", ".join(names) if names else "—")
        + (f"\nБренд: {brand.name}" if brand else "\nБренды и объекты: все"),
        RoleCode.UCCP_STAFF: "Доступ: все обращения точек в УЦЦП",
        RoleCode.ADMIN: "Доступ: полный",
    }
    scope = scope_lines.get(role, "")

    role_label = target.role_title
    if role == RoleCode.EXECUTOR and names:
        role_label = f"Мастер — {names[0].lower()}" if len(names) == 1 else "Мастер"

    if target.tg_id:
        try:
            await bot.send_message(
                target.tg_id,
                texts.APPROVED_NOTICE.format(
                    role=esc(role_label),
                    scope=esc(scope),
                    hint=(
                        "Теперь вы будете получать назначенные вам заявки."
                        if role == RoleCode.EXECUTOR
                        else "Рабочее меню открыто — можно создавать заявки."
                    ),
                ),
                reply_markup=main_menu(target),
            )
            await bot.send_message(target.tg_id, texts.ROLE_INSTRUCTIONS.get(role, ""))
        except Exception:
            pass

    warn = "" if target.tg_id else "\n\n⚠️ Пользователь ещё не активировал бота."
    await call.message.edit_text(
        f"✅ Регистрация <b>№{target.id}</b> подтверждена.\n\n"
        f"{esc(target.full_name)}\n"
        f"Роль: <b>{esc(role_label)}</b>\n{esc(scope)}{warn}",
        reply_markup=_back_kb(),
    )


# --------------------------------------------------------------------------- #
# Отклонение
# --------------------------------------------------------------------------- #
@router.callback_query(ApprCB.filter(F.act == "reject"))
async def cb_reject(
    call: types.CallbackQuery,
    callback_data: ApprCB,
    state: FSMContext,
    user: Optional[User],
) -> None:
    if not _require_admin(user):
        await call.answer(texts.NO_ACCESS, show_alert=True)
        return
    await state.set_state(Approval.reject_reason)
    await state.update_data(target_id=callback_data.user_id)
    await call.answer()
    await call.message.answer(
        "Укажите <b>причину отклонения</b> — её увидит пользователь.\n"
        "Например: <i>Нет такого сотрудника на точке.</i>",
        reply_markup=cancel_kb(),
    )


@router.message(Approval.reject_reason, F.text)
async def reject_reason(
    message: types.Message,
    state: FSMContext,
    session: AsyncSession,
    user: Optional[User],
    bot: Bot,
) -> None:
    if message.text == BTN_CANCEL:
        await state.clear()
        await message.answer(texts.CANCELLED, reply_markup=main_menu(user))
        return
    data = await state.get_data()
    target = await session.get(User, data["target_id"])
    reason = message.text.strip()

    target.status = UserStatus.REJECTED
    target.reject_reason = reason
    target.sync_flags()
    await session.commit()
    await state.clear()

    if target.tg_id:
        try:
            await bot.send_message(
                target.tg_id,
                texts.REJECTED_HINT.format(reason=f"Причина: <i>{esc(reason)}</i>"),
            )
        except Exception:
            pass
    await message.answer(
        f"❌ Заявка <b>№{target.id}</b> отклонена. Пользователь уведомлён.",
        reply_markup=main_menu(user),
    )
