"""Панель администратора (п.15, п.33): пользователи, исполнители, точки, категории."""
from __future__ import annotations

from typing import Optional

from aiogram import Bot, F, Router, types
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from .. import texts
from ..callbacks import AdminCB, ApprCB
from ..db.models import (
    Brand,
    Category,
    ExecutorAssignment,
    Outlet,
    RoleCode,
    User,
    UserStatus,
)
from ..keyboards.common import BTN_ADMIN, cancel_kb, main_menu
from ..states import AdminFlow
from ..utils import esc, fmt_dt, normalize_phone, utcnow
from .lists import show_list

router = Router(name="admin")


def _require_admin(user: Optional[User]) -> bool:
    return user is not None and user.is_admin


def _root_kb() -> types.InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    buttons = [
        ("🆕 Заявки на регистрацию", "applications"),
        ("⏳ Ожидают назначения", "req_await"),
        ("🔴 Просроченные", "req_overdue"),
        ("📂 Активные заявки", "req_open"),
        ("👷 Исполнители", "executors"),
        ("👥 Пользователи и роли", "users"),
        ("🏬 Торговые точки", "outlets"),
        ("🗂 Категории", "categories"),
    ]
    for label, act in buttons:
        kb.button(text=label, callback_data=AdminCB(act=act).pack())
    kb.adjust(1)
    return kb.as_markup()


async def _root_text(session: AsyncSession) -> str:
    from .approval import pending_count

    count = await pending_count(session)
    suffix = f"\n\n🆕 Новых заявок на регистрацию: <b>{count}</b>" if count else ""
    return "⚙️ <b>Администрирование УЦЦП</b>\n\nВыберите раздел:" + suffix


def _back_kb(act: str = "root") -> types.InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад", callback_data=AdminCB(act=act).pack())
    return kb.as_markup()


@router.message(F.text == BTN_ADMIN)
async def admin_root(
    message: types.Message, state: FSMContext, session: AsyncSession, user: Optional[User]
) -> None:
    if not _require_admin(user):
        await message.answer(texts.NO_ACCESS)
        return
    await state.clear()
    await message.answer(await _root_text(session), reply_markup=_root_kb())


@router.callback_query(AdminCB.filter(F.act == "root"))
async def cb_root(
    call: types.CallbackQuery, state: FSMContext, session: AsyncSession, user: Optional[User]
) -> None:
    if not _require_admin(user):
        await call.answer(texts.NO_ACCESS, show_alert=True)
        return
    await state.clear()
    await call.answer()
    await call.message.edit_text(await _root_text(session), reply_markup=_root_kb())


@router.callback_query(ApprCB.filter(F.act == "root"))
async def cb_root_from_approval(
    call: types.CallbackQuery, state: FSMContext, session: AsyncSession, user: Optional[User]
) -> None:
    await cb_root(call, state, session, user)


@router.callback_query(AdminCB.filter(F.act == "applications"))
async def cb_applications(
    call: types.CallbackQuery, session: AsyncSession, user: Optional[User]
) -> None:
    from .approval import cb_list

    await cb_list(call, session, user)


# --------------------------------------------------------------------------- #
# Заявки
# --------------------------------------------------------------------------- #
@router.callback_query(AdminCB.filter(F.act.startswith("req_")))
async def cb_requests(
    call: types.CallbackQuery,
    callback_data: AdminCB,
    session: AsyncSession,
    user: Optional[User],
) -> None:
    if not _require_admin(user):
        await call.answer(texts.NO_ACCESS, show_alert=True)
        return
    kind = {"req_await": "await", "req_overdue": "overdue", "req_open": "open"}[
        callback_data.act
    ]
    await call.answer()
    await show_list(call.message, session, user, kind, edit=True)


# --------------------------------------------------------------------------- #
# Пользователи и роли
# --------------------------------------------------------------------------- #
@router.callback_query(AdminCB.filter(F.act == "users"))
async def cb_users(
    call: types.CallbackQuery, callback_data: AdminCB, session: AsyncSession, user: Optional[User]
) -> None:
    if not _require_admin(user):
        await call.answer(texts.NO_ACCESS, show_alert=True)
        return
    page = callback_data.id
    rows = (
        await session.scalars(
            select(User).order_by(User.full_name).limit(11).offset(page * 10)
        )
    ).all()
    has_next = len(rows) > 10
    rows = rows[:10]

    kb = InlineKeyboardBuilder()
    marks = {
        UserStatus.PENDING: "⏳",
        UserStatus.ACTIVE: "",
        UserStatus.BLOCKED: "⛔️",
        UserStatus.DISABLED: "🚫",
        UserStatus.REJECTED: "❌",
    }
    for row in rows:
        mark = marks.get(row.status, "")
        no_bot = "" if row.tg_id else " ⚠️"
        label = f"{mark} {row.full_name} · {row.role_title}{no_bot}".strip()
        kb.button(
            text=label[:60], callback_data=AdminCB(act="user_card", id=row.id).pack()
        )
    kb.adjust(1)
    nav = []
    if page:
        nav.append(
            InlineKeyboardButton(text="⬅️", callback_data=AdminCB(act="users", id=page - 1).pack())
        )
    if has_next:
        nav.append(
            InlineKeyboardButton(text="➡️", callback_data=AdminCB(act="users", id=page + 1).pack())
        )
    if nav:
        kb.row(*nav)
    kb.row(InlineKeyboardButton(text="⬅️ В меню", callback_data=AdminCB(act="root").pack()))
    await call.answer()
    await call.message.edit_text(
        "👥 <b>Пользователи</b>\n"
        "⏳ — ждёт подтверждения, ⛔️ — заблокирован, 🚫 — отключён, "
        "⚠️ — не активировал бота.\n\nВыберите пользователя:",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(AdminCB.filter(F.act == "user_card"))
async def cb_user_card(
    call: types.CallbackQuery, callback_data: AdminCB, session: AsyncSession, user: Optional[User]
) -> None:
    if not _require_admin(user):
        await call.answer(texts.NO_ACCESS, show_alert=True)
        return
    target = await session.get(User, callback_data.id)
    if target is None:
        await call.answer("Пользователь не найден", show_alert=True)
        return

    brand = await session.get(Brand, target.brand_id) if target.brand_id else None
    outlet = await session.get(Outlet, target.outlet_id) if target.outlet_id else None
    directions = (
        await session.scalars(
            select(ExecutorAssignment)
            .where(
                ExecutorAssignment.user_id == target.id,
                ExecutorAssignment.is_active.is_(True),
            )
            .options(selectinload(ExecutorAssignment.category))
        )
    ).all()

    lines = [
        f"👤 <b>{esc(target.full_name)}</b>",
        f"Статус: <b>{target.status_title}</b>",
        f"Роль: <b>{target.role_title}</b> — {RoleCode.DESCRIPTIONS.get(target.role_code, '')}",
        f"Указанная должность: {esc(target.position_text or '—')}",
        f"Телефон: {esc(target.phone or '—')}",
        "Telegram: " + (f"@{esc(target.username)}" if target.username else "—"),
        f"Бренд: {esc(brand.name) if brand else '—'}",
        f"Точка: {esc(outlet.name) if outlet else '—'}",
        f"Активировал бота: {'да' if target.tg_id else 'нет ⚠️'}",
    ]
    if target.approved_at:
        lines.append(f"Доступ выдан: {fmt_dt(target.approved_at)}")
    if target.reject_reason:
        lines.append(f"Причина отклонения: {esc(target.reject_reason)}")

    # заявки уходят только тем, у кого роль «Мастер» И есть направления
    if target.role_code == RoleCode.EXECUTOR and not directions:
        lines.append(
            "\n❗️ <b>У мастера не выбрано ни одного направления</b> — "
            "заявки ему приходить не будут. Нажмите «🗂 Направления мастера»."
        )
    elif target.role_code != RoleCode.EXECUTOR and directions:
        names = ", ".join(a.category.name for a in directions if a.category)
        lines.append(
            f"\n❗️ <b>Направления назначены ({esc(names)}), но роль не «Мастер»</b> — "
            "заявки этому человеку приходить не будут. Смените роль на "
            "«Мастер / исполнитель» или снимите направления."
        )

    kb = InlineKeyboardBuilder()
    if target.status == UserStatus.PENDING:
        kb.button(
            text="✅ Подтвердить регистрацию",
            callback_data=ApprCB(act="approve", user_id=target.id).pack(),
        )
        kb.button(
            text="❌ Отклонить", callback_data=ApprCB(act="reject", user_id=target.id).pack()
        )
    else:
        kb.button(text="🔁 Изменить роль", callback_data=AdminCB(act="role_pick", id=target.id).pack())
        kb.button(text="🏷 Бренд", callback_data=AdminCB(act="user_brand", id=target.id).pack())
        kb.button(text="🏬 Точка", callback_data=AdminCB(act="user_outlet", id=target.id).pack())
        if target.role_code == RoleCode.EXECUTOR:
            kb.button(
                text="🗂 Направления мастера",
                callback_data=AdminCB(act="exec_card", id=target.id).pack(),
            )
        kb.button(text="📱 Телефон", callback_data=AdminCB(act="user_phone", id=target.id).pack())
        if target.status == UserStatus.ACTIVE:
            kb.button(
                text="⛔️ Временно заблокировать",
                callback_data=AdminCB(act="user_status", id=target.id, value=UserStatus.BLOCKED).pack(),
            )
            kb.button(
                text="🚫 Отключить полностью",
                callback_data=AdminCB(act="user_status", id=target.id, value=UserStatus.DISABLED).pack(),
            )
        else:
            kb.button(
                text="✅ Вернуть доступ",
                callback_data=AdminCB(act="user_status", id=target.id, value=UserStatus.ACTIVE).pack(),
            )
    kb.adjust(1)
    kb.row(InlineKeyboardButton(text="⬅️ К списку", callback_data=AdminCB(act="users").pack()))
    await call.answer()
    await call.message.edit_text("\n".join(lines), reply_markup=kb.as_markup())


@router.callback_query(AdminCB.filter(F.act == "user_status"))
async def cb_user_status(
    call: types.CallbackQuery,
    callback_data: AdminCB,
    session: AsyncSession,
    user: Optional[User],
    bot: Bot,
) -> None:
    if not _require_admin(user):
        await call.answer(texts.NO_ACCESS, show_alert=True)
        return
    target = await session.get(User, callback_data.id)
    if target.id == user.id and callback_data.value != UserStatus.ACTIVE:
        await call.answer("Нельзя заблокировать самого себя.", show_alert=True)
        return
    target.status = callback_data.value
    target.sync_flags()
    await session.commit()
    await call.answer("Статус изменён")

    if target.tg_id:
        messages = {
            UserStatus.BLOCKED: texts.BLOCKED_HINT,
            UserStatus.DISABLED: texts.DISABLED_HINT,
            UserStatus.ACTIVE: "✅ Доступ к боту восстановлен.",
        }
        try:
            await bot.send_message(
                target.tg_id,
                messages.get(target.status, ""),
                reply_markup=main_menu(target),
            )
        except Exception:
            pass
    await cb_user_card(call, AdminCB(act="user_card", id=target.id), session, user)


@router.callback_query(AdminCB.filter(F.act == "user_brand"))
async def cb_user_brand(
    call: types.CallbackQuery, callback_data: AdminCB, session: AsyncSession, user: Optional[User]
) -> None:
    if not _require_admin(user):
        await call.answer(texts.NO_ACCESS, show_alert=True)
        return
    brands = (await session.scalars(select(Brand).order_by(Brand.sort_order))).all()
    kb = InlineKeyboardBuilder()
    for brand in brands:
        kb.button(
            text=brand.name,
            callback_data=AdminCB(act="user_brand_set", id=callback_data.id, id2=brand.id).pack(),
        )
    kb.button(
        text="Без привязки к бренду",
        callback_data=AdminCB(act="user_brand_set", id=callback_data.id, id2=0).pack(),
    )
    kb.adjust(1)
    kb.row(
        InlineKeyboardButton(
            text="⬅️ Назад", callback_data=AdminCB(act="user_card", id=callback_data.id).pack()
        )
    )
    await call.answer()
    await call.message.edit_text("Выберите бренд пользователя:", reply_markup=kb.as_markup())


@router.callback_query(AdminCB.filter(F.act == "user_brand_set"))
async def cb_user_brand_set(
    call: types.CallbackQuery, callback_data: AdminCB, session: AsyncSession, user: Optional[User]
) -> None:
    if not _require_admin(user):
        await call.answer(texts.NO_ACCESS, show_alert=True)
        return
    target = await session.get(User, callback_data.id)
    target.brand_id = callback_data.id2 or None
    if target.outlet_id:
        outlet = await session.get(Outlet, target.outlet_id)
        if outlet and outlet.brand_id != target.brand_id:
            target.outlet_id = None
    await session.commit()
    await call.answer("Бренд изменён")
    await cb_user_card(call, AdminCB(act="user_card", id=target.id), session, user)


@router.callback_query(AdminCB.filter(F.act == "user_outlet"))
async def cb_user_outlet(
    call: types.CallbackQuery, callback_data: AdminCB, session: AsyncSession, user: Optional[User]
) -> None:
    if not _require_admin(user):
        await call.answer(texts.NO_ACCESS, show_alert=True)
        return
    target = await session.get(User, callback_data.id)
    stmt = select(Outlet).where(Outlet.is_active.is_(True)).order_by(Outlet.sort_order)
    if target.brand_id:
        stmt = stmt.where(Outlet.brand_id == target.brand_id)
    outlets = (await session.scalars(stmt)).all()

    kb = InlineKeyboardBuilder()
    for outlet in outlets:
        kb.button(
            text=outlet.name,
            callback_data=AdminCB(act="user_outlet_set", id=target.id, id2=outlet.id).pack(),
        )
    kb.button(
        text="Без привязки к точке",
        callback_data=AdminCB(act="user_outlet_set", id=target.id, id2=0).pack(),
    )
    kb.adjust(1)
    kb.row(
        InlineKeyboardButton(
            text="⬅️ Назад", callback_data=AdminCB(act="user_card", id=target.id).pack()
        )
    )
    await call.answer()
    await call.message.edit_text("Выберите торговую точку:", reply_markup=kb.as_markup())


@router.callback_query(AdminCB.filter(F.act == "user_outlet_set"))
async def cb_user_outlet_set(
    call: types.CallbackQuery, callback_data: AdminCB, session: AsyncSession, user: Optional[User]
) -> None:
    if not _require_admin(user):
        await call.answer(texts.NO_ACCESS, show_alert=True)
        return
    target = await session.get(User, callback_data.id)
    target.outlet_id = callback_data.id2 or None
    if target.outlet_id:
        outlet = await session.get(Outlet, target.outlet_id)
        target.brand_id = outlet.brand_id
    await session.commit()
    await call.answer("Точка изменена")
    await cb_user_card(call, AdminCB(act="user_card", id=target.id), session, user)


@router.callback_query(AdminCB.filter(F.act == "user_phone"))
async def cb_user_phone(
    call: types.CallbackQuery, callback_data: AdminCB, state: FSMContext, user: Optional[User]
) -> None:
    if not _require_admin(user):
        await call.answer(texts.NO_ACCESS, show_alert=True)
        return
    await state.set_state(AdminFlow.set_phone)
    await state.update_data(target_id=callback_data.id)
    await call.answer()
    await call.message.answer(
        "Введите <b>номер телефона</b> пользователя (или «-», чтобы очистить):",
        reply_markup=cancel_kb(),
    )


@router.message(AdminFlow.set_phone, F.text)
async def set_phone(
    message: types.Message, state: FSMContext, session: AsyncSession, user: Optional[User]
) -> None:
    if message.text == "❌ Отмена":
        await state.clear()
        await message.answer(texts.CANCELLED, reply_markup=main_menu(user))
        return
    data = await state.get_data()
    target = await session.get(User, data["target_id"])
    raw = message.text.strip()
    target.phone = None if raw == "-" else (normalize_phone(raw) or raw)
    await session.commit()
    await state.clear()
    await message.answer(
        f"✅ Телефон пользователя <b>{esc(target.full_name)}</b>: {esc(target.phone or '—')}",
        reply_markup=main_menu(user),
    )


@router.callback_query(AdminCB.filter(F.act == "role_pick"))
async def cb_role_pick(
    call: types.CallbackQuery, callback_data: AdminCB, user: Optional[User]
) -> None:
    if not _require_admin(user):
        await call.answer(texts.NO_ACCESS, show_alert=True)
        return
    kb = InlineKeyboardBuilder()
    for code in RoleCode.ALL:
        kb.button(
            text=RoleCode.TITLES[code],
            callback_data=AdminCB(act="role_set", id=callback_data.id, value=code).pack(),
        )
    kb.adjust(1)
    kb.row(
        InlineKeyboardButton(
            text="⬅️ Назад", callback_data=AdminCB(act="user_card", id=callback_data.id).pack()
        )
    )
    await call.answer()
    await call.message.edit_text("Выберите новую роль:", reply_markup=kb.as_markup())


@router.callback_query(AdminCB.filter(F.act == "role_set"))
async def cb_role_set(
    call: types.CallbackQuery,
    callback_data: AdminCB,
    session: AsyncSession,
    user: Optional[User],
    bot: Bot,
) -> None:
    if not _require_admin(user):
        await call.answer(texts.NO_ACCESS, show_alert=True)
        return
    target = await session.get(User, callback_data.id)
    new_role = callback_data.value
    target.role_code = new_role
    if target.status == UserStatus.PENDING:
        target.status = UserStatus.ACTIVE
        target.approved_at = utcnow()
        target.approved_by_id = user.id
    target.sync_flags()
    await session.commit()

    has_directions = bool(
        await session.scalar(
            select(ExecutorAssignment.id).where(
                ExecutorAssignment.user_id == target.id,
                ExecutorAssignment.is_active.is_(True),
            )
        )
    )
    if new_role == RoleCode.EXECUTOR and not has_directions:
        await call.answer(
            "Роль изменена. Теперь выберите направления — без них заявки "
            "мастеру приходить не будут.",
            show_alert=True,
        )
        await cb_exec_card(call, AdminCB(act="exec_card", id=target.id), session, user)
        return
    if new_role != RoleCode.EXECUTOR and has_directions:
        await call.answer(
            "Роль изменена. Внимание: у человека остались направления мастера, "
            "но заявки по ним ему больше не пойдут.",
            show_alert=True,
        )
    else:
        await call.answer("Роль изменена")
    if target.tg_id:
        try:
            await bot.send_message(
                target.tg_id,
                f"ℹ️ Ваша роль изменена на: <b>{target.role_title}</b>.\n"
                + texts.ROLE_INSTRUCTIONS.get(target.role_code, ""),
                reply_markup=main_menu(target),
            )
        except Exception:
            pass
    await cb_user_card(call, AdminCB(act="user_card", id=target.id), session, user)


# --------------------------------------------------------------------------- #
# Исполнители и направления
# --------------------------------------------------------------------------- #
@router.callback_query(AdminCB.filter(F.act == "executors"))
async def cb_executors(
    call: types.CallbackQuery, session: AsyncSession, user: Optional[User]
) -> None:
    if not _require_admin(user):
        await call.answer(texts.NO_ACCESS, show_alert=True)
        return
    rows = (
        await session.scalars(
            select(User)
            .where(User.role_code == RoleCode.EXECUTOR)
            .order_by(User.full_name)
        )
    ).all()
    kb = InlineKeyboardBuilder()
    for row in rows:
        mark = "" if row.tg_id else " ⚠️"
        state_mark = "" if row.is_approved else " 🚫"
        kb.button(
            text=f"{row.full_name}{mark}{state_mark}",
            callback_data=AdminCB(act="exec_card", id=row.id).pack(),
        )
    kb.adjust(1)
    kb.row(
        InlineKeyboardButton(
            text="➕ Добавить исполнителя", callback_data=AdminCB(act="exec_add").pack()
        )
    )
    kb.row(InlineKeyboardButton(text="⬅️ В меню", callback_data=AdminCB(act="root").pack()))
    await call.answer()
    await call.message.edit_text(
        "👷 <b>Исполнители</b>\n⚠️ — мастер ещё не нажал /start, личные уведомления "
        "не доходят.\n\nВыберите исполнителя:",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(AdminCB.filter(F.act == "exec_card"))
async def cb_exec_card(
    call: types.CallbackQuery, callback_data: AdminCB, session: AsyncSession, user: Optional[User]
) -> None:
    if not _require_admin(user):
        await call.answer(texts.NO_ACCESS, show_alert=True)
        return
    target = await session.scalar(
        select(User)
        .where(User.id == callback_data.id)
        .options(
            selectinload(User.assignments).selectinload(ExecutorAssignment.category),
            selectinload(User.assignments).selectinload(ExecutorAssignment.brand),
            selectinload(User.assignments).selectinload(ExecutorAssignment.outlet),
        )
    )
    if target is None:
        await call.answer("Не найден", show_alert=True)
        return

    lines = [
        f"👷 <b>{esc(target.full_name)}</b>",
        f"Телефон: {esc(target.phone or '—')}",
        "Telegram: " + (f"@{esc(target.username)}" if target.username else "—"),
        f"Активировал бота: {'да' if target.tg_id else 'нет ⚠️'}",
        f"Статус: {target.status_title}",
        "",
        "<b>Направления:</b>",
    ]
    kb = InlineKeyboardBuilder()
    active = [a for a in target.assignments if a.is_active]
    if not active:
        lines.append("— не назначены")
    for a in active:
        scope = a.outlet.name if a.outlet else (a.brand.name if a.brand else "все бренды")
        lines.append(f"• {a.category.label} — {scope}")
        kb.button(
            text=f"🗑 {a.category.name} · {scope}",
            callback_data=AdminCB(act="exec_unassign", id=target.id, id2=a.id).pack(),
        )
    kb.adjust(1)
    kb.row(
        InlineKeyboardButton(
            text="➕ Добавить направление",
            callback_data=AdminCB(act="exec_assign", id=target.id).pack(),
        )
    )
    kb.row(
        InlineKeyboardButton(
            text="⛔️ Заблокировать" if target.is_approved else "✅ Вернуть доступ",
            callback_data=AdminCB(
                act="user_status",
                id=target.id,
                value=UserStatus.BLOCKED if target.is_approved else UserStatus.ACTIVE,
            ).pack(),
        )
    )
    kb.row(
        InlineKeyboardButton(text="⬅️ К списку", callback_data=AdminCB(act="executors").pack())
    )
    await call.answer()
    await call.message.edit_text("\n".join(lines), reply_markup=kb.as_markup())


@router.callback_query(AdminCB.filter(F.act == "exec_assign"))
async def cb_exec_assign(
    call: types.CallbackQuery, callback_data: AdminCB, session: AsyncSession, user: Optional[User]
) -> None:
    if not _require_admin(user):
        await call.answer(texts.NO_ACCESS, show_alert=True)
        return
    categories = (
        await session.scalars(
            select(Category).where(Category.is_active.is_(True)).order_by(Category.sort_order)
        )
    ).all()
    kb = InlineKeyboardBuilder()
    for cat in categories:
        kb.button(
            text=cat.label,
            callback_data=AdminCB(
                act="exec_scope", id=callback_data.id, id2=cat.id
            ).pack(),
        )
    kb.adjust(1)
    kb.row(
        InlineKeyboardButton(
            text="⬅️ Назад", callback_data=AdminCB(act="exec_card", id=callback_data.id).pack()
        )
    )
    await call.answer()
    await call.message.edit_text("Выберите направление:", reply_markup=kb.as_markup())


@router.callback_query(AdminCB.filter(F.act == "exec_scope"))
async def cb_exec_scope(
    call: types.CallbackQuery, callback_data: AdminCB, session: AsyncSession, user: Optional[User]
) -> None:
    if not _require_admin(user):
        await call.answer(texts.NO_ACCESS, show_alert=True)
        return
    brands = (await session.scalars(select(Brand).order_by(Brand.sort_order))).all()
    kb = InlineKeyboardBuilder()
    kb.button(
        text="Оба бренда (все точки)",
        callback_data=AdminCB(
            act="exec_save", id=callback_data.id, id2=callback_data.id2, value="all"
        ).pack(),
    )
    for brand in brands:
        kb.button(
            text=f"Только {brand.name}",
            callback_data=AdminCB(
                act="exec_save", id=callback_data.id, id2=callback_data.id2,
                value=f"b{brand.id}",
            ).pack(),
        )
        kb.button(
            text=f"Точка бренда {brand.name} →",
            callback_data=AdminCB(
                act="exec_outlets", id=callback_data.id, id2=callback_data.id2,
                value=f"b{brand.id}",
            ).pack(),
        )
    kb.adjust(1)
    kb.row(
        InlineKeyboardButton(
            text="⬅️ Назад", callback_data=AdminCB(act="exec_assign", id=callback_data.id).pack()
        )
    )
    await call.answer()
    await call.message.edit_text(
        "Область работы по этому направлению:", reply_markup=kb.as_markup()
    )


@router.callback_query(AdminCB.filter(F.act == "exec_outlets"))
async def cb_exec_outlets(
    call: types.CallbackQuery, callback_data: AdminCB, session: AsyncSession, user: Optional[User]
) -> None:
    if not _require_admin(user):
        await call.answer(texts.NO_ACCESS, show_alert=True)
        return
    brand_id = int(callback_data.value[1:])
    outlets = (
        await session.scalars(
            select(Outlet).where(Outlet.brand_id == brand_id).order_by(Outlet.sort_order)
        )
    ).all()
    kb = InlineKeyboardBuilder()
    for outlet in outlets:
        kb.button(
            text=outlet.name,
            callback_data=AdminCB(
                act="exec_save", id=callback_data.id, id2=callback_data.id2,
                value=f"o{outlet.id}",
            ).pack(),
        )
    kb.adjust(1)
    kb.row(
        InlineKeyboardButton(
            text="⬅️ Назад",
            callback_data=AdminCB(
                act="exec_scope", id=callback_data.id, id2=callback_data.id2
            ).pack(),
        )
    )
    await call.answer()
    await call.message.edit_text("Выберите точку:", reply_markup=kb.as_markup())


@router.callback_query(AdminCB.filter(F.act == "exec_save"))
async def cb_exec_save(
    call: types.CallbackQuery, callback_data: AdminCB, session: AsyncSession, user: Optional[User]
) -> None:
    if not _require_admin(user):
        await call.answer(texts.NO_ACCESS, show_alert=True)
        return
    brand_id = outlet_id = None
    value = callback_data.value
    if value.startswith("b"):
        brand_id = int(value[1:])
    elif value.startswith("o"):
        outlet_id = int(value[1:])
        outlet = await session.get(Outlet, outlet_id)
        brand_id = outlet.brand_id

    session.add(
        ExecutorAssignment(
            user_id=callback_data.id,
            category_id=callback_data.id2,
            brand_id=brand_id,
            outlet_id=outlet_id,
        )
    )
    await session.commit()
    await call.answer("Направление добавлено")
    await cb_exec_card(call, AdminCB(act="exec_card", id=callback_data.id), session, user)


@router.callback_query(AdminCB.filter(F.act == "exec_unassign"))
async def cb_exec_unassign(
    call: types.CallbackQuery, callback_data: AdminCB, session: AsyncSession, user: Optional[User]
) -> None:
    if not _require_admin(user):
        await call.answer(texts.NO_ACCESS, show_alert=True)
        return
    assignment = await session.get(ExecutorAssignment, callback_data.id2)
    if assignment is not None:
        await session.delete(assignment)
        await session.commit()
    await call.answer("Направление удалено")
    await cb_exec_card(call, AdminCB(act="exec_card", id=callback_data.id), session, user)


# --- добавление исполнителя вручную ---
@router.callback_query(AdminCB.filter(F.act == "exec_add"))
async def cb_exec_add(
    call: types.CallbackQuery, state: FSMContext, user: Optional[User]
) -> None:
    if not _require_admin(user):
        await call.answer(texts.NO_ACCESS, show_alert=True)
        return
    await state.set_state(AdminFlow.add_executor_name)
    await call.answer()
    await call.message.answer(
        "Введите <b>Ф.И.О. исполнителя</b>:", reply_markup=cancel_kb()
    )


@router.message(AdminFlow.add_executor_name, F.text)
async def exec_add_name(message: types.Message, state: FSMContext, user: Optional[User]) -> None:
    if message.text == "❌ Отмена":
        await state.clear()
        await message.answer(texts.CANCELLED, reply_markup=main_menu(user))
        return
    await state.update_data(full_name=message.text.strip())
    await state.set_state(AdminFlow.add_executor_phone)
    await message.answer("Введите <b>телефон</b> мастера (или «-», если неизвестен):")


@router.message(AdminFlow.add_executor_phone, F.text)
async def exec_add_phone(message: types.Message, state: FSMContext) -> None:
    phone = None if message.text.strip() == "-" else normalize_phone(message.text)
    await state.update_data(phone=phone)
    await state.set_state(AdminFlow.add_executor_username)
    await message.answer(
        "Введите <b>@username</b> мастера в Telegram (или «-»).\n\n"
        "Если указать username, бот узнает мастера автоматически, "
        "когда тот нажмёт /start, и привяжет к нему все направления."
    )


@router.message(AdminFlow.add_executor_username, F.text)
async def exec_add_username(
    message: types.Message, state: FSMContext, session: AsyncSession, user: Optional[User]
) -> None:
    raw = message.text.strip().lstrip("@")
    username = None if raw == "-" else raw
    data = await state.get_data()

    if username:
        exists = await session.scalar(
            select(User).where(func.lower(User.username) == username.lower())
        )
        if exists is not None:
            await message.answer(
                f"Пользователь @{esc(username)} уже есть в системе "
                f"({esc(exists.full_name)}). Введите другой username или «-»."
            )
            return

    executor = User(
        full_name=data["full_name"],
        last_name=data["full_name"],
        phone=data.get("phone"),
        username=username,
        position_text="Мастер (заведён администратором)",
        role_code=RoleCode.EXECUTOR,
        status=UserStatus.ACTIVE,   # администратор завёл лично — доступ выдан
        is_active=True,
        is_registered=False,
        approved_at=utcnow(),
    )
    session.add(executor)
    await session.commit()
    await state.clear()
    await message.answer(
        f"✅ Исполнитель <b>{esc(executor.full_name)}</b> добавлен.\n"
        "Теперь назначьте ему направления: ⚙️ Администрирование → 👷 Исполнители.\n\n"
        "⚠️ Пока мастер не нажал /start, личные уведомления ему не доходят — "
        "такие заявки бот передаёт администратору.",
        reply_markup=main_menu(user),
    )


# --------------------------------------------------------------------------- #
# Торговые точки
# --------------------------------------------------------------------------- #
@router.callback_query(AdminCB.filter(F.act == "outlets"))
async def cb_outlets(
    call: types.CallbackQuery, session: AsyncSession, user: Optional[User]
) -> None:
    if not _require_admin(user):
        await call.answer(texts.NO_ACCESS, show_alert=True)
        return
    brands = (await session.scalars(select(Brand).order_by(Brand.sort_order))).all()
    kb = InlineKeyboardBuilder()
    for brand in brands:
        kb.button(text=brand.name, callback_data=AdminCB(act="outlet_list", id=brand.id).pack())
    kb.adjust(1)
    kb.row(InlineKeyboardButton(text="⬅️ В меню", callback_data=AdminCB(act="root").pack()))
    await call.answer()
    await call.message.edit_text("🏬 Выберите бренд:", reply_markup=kb.as_markup())


@router.callback_query(AdminCB.filter(F.act == "outlet_list"))
async def cb_outlet_list(
    call: types.CallbackQuery, callback_data: AdminCB, session: AsyncSession, user: Optional[User]
) -> None:
    if not _require_admin(user):
        await call.answer(texts.NO_ACCESS, show_alert=True)
        return
    outlets = (
        await session.scalars(
            select(Outlet).where(Outlet.brand_id == callback_data.id).order_by(Outlet.sort_order)
        )
    ).all()
    kb = InlineKeyboardBuilder()
    for outlet in outlets:
        mark = "" if outlet.is_active else " 🚫"
        kb.button(
            text=f"{outlet.name}{mark}",
            callback_data=AdminCB(act="outlet_card", id=outlet.id).pack(),
        )
    kb.adjust(1)
    kb.row(
        InlineKeyboardButton(
            text="➕ Добавить точку",
            callback_data=AdminCB(act="outlet_add", id=callback_data.id).pack(),
        )
    )
    kb.row(InlineKeyboardButton(text="⬅️ Назад", callback_data=AdminCB(act="outlets").pack()))
    await call.answer()
    await call.message.edit_text(
        "🏬 <b>Точки бренда</b>\n🚫 — точка отключена.", reply_markup=kb.as_markup()
    )


@router.callback_query(AdminCB.filter(F.act == "outlet_card"))
async def cb_outlet_card(
    call: types.CallbackQuery, callback_data: AdminCB, session: AsyncSession, user: Optional[User]
) -> None:
    if not _require_admin(user):
        await call.answer(texts.NO_ACCESS, show_alert=True)
        return
    outlet = await session.get(Outlet, callback_data.id)
    brand = await session.get(Brand, outlet.brand_id)
    kb = InlineKeyboardBuilder()
    kb.button(
        text="🚫 Отключить" if outlet.is_active else "✅ Включить",
        callback_data=AdminCB(act="outlet_toggle", id=outlet.id).pack(),
    )
    kb.button(text="✏️ Переименовать", callback_data=AdminCB(act="outlet_rename", id=outlet.id).pack())
    kb.button(text="🔁 Сменить бренд", callback_data=AdminCB(act="outlet_brand", id=outlet.id).pack())
    kb.adjust(1)
    kb.row(
        InlineKeyboardButton(
            text="⬅️ Назад", callback_data=AdminCB(act="outlet_list", id=outlet.brand_id).pack()
        )
    )
    await call.answer()
    await call.message.edit_text(
        f"🏬 <b>{esc(outlet.name)}</b>\nБренд: {esc(brand.name)}\n"
        f"Активна: {'да' if outlet.is_active else 'нет'}",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(AdminCB.filter(F.act == "outlet_toggle"))
async def cb_outlet_toggle(
    call: types.CallbackQuery, callback_data: AdminCB, session: AsyncSession, user: Optional[User]
) -> None:
    if not _require_admin(user):
        await call.answer(texts.NO_ACCESS, show_alert=True)
        return
    outlet = await session.get(Outlet, callback_data.id)
    outlet.is_active = not outlet.is_active
    await session.commit()
    await call.answer("Готово")
    await cb_outlet_card(call, callback_data, session, user)


@router.callback_query(AdminCB.filter(F.act == "outlet_brand"))
async def cb_outlet_brand(
    call: types.CallbackQuery, callback_data: AdminCB, session: AsyncSession, user: Optional[User]
) -> None:
    if not _require_admin(user):
        await call.answer(texts.NO_ACCESS, show_alert=True)
        return
    outlet = await session.get(Outlet, callback_data.id)
    brands = (await session.scalars(select(Brand).order_by(Brand.sort_order))).all()
    other = [b for b in brands if b.id != outlet.brand_id]
    kb = InlineKeyboardBuilder()
    for brand in other:
        kb.button(
            text=brand.name,
            callback_data=AdminCB(act="outlet_brand_set", id=outlet.id, id2=brand.id).pack(),
        )
    kb.adjust(1)
    kb.row(
        InlineKeyboardButton(
            text="⬅️ Назад", callback_data=AdminCB(act="outlet_card", id=outlet.id).pack()
        )
    )
    await call.answer()
    await call.message.edit_text("Выберите новый бренд точки:", reply_markup=kb.as_markup())


@router.callback_query(AdminCB.filter(F.act == "outlet_brand_set"))
async def cb_outlet_brand_set(
    call: types.CallbackQuery, callback_data: AdminCB, session: AsyncSession, user: Optional[User]
) -> None:
    if not _require_admin(user):
        await call.answer(texts.NO_ACCESS, show_alert=True)
        return
    outlet = await session.get(Outlet, callback_data.id)
    outlet.brand_id = callback_data.id2
    await session.commit()
    await call.answer("Бренд изменён")
    await cb_outlet_card(call, AdminCB(act="outlet_card", id=outlet.id), session, user)


@router.callback_query(AdminCB.filter(F.act == "outlet_add"))
async def cb_outlet_add(
    call: types.CallbackQuery, callback_data: AdminCB, state: FSMContext, user: Optional[User]
) -> None:
    if not _require_admin(user):
        await call.answer(texts.NO_ACCESS, show_alert=True)
        return
    await state.set_state(AdminFlow.add_outlet_name)
    await state.update_data(brand_id=callback_data.id)
    await call.answer()
    await call.message.answer("Введите <b>название новой точки</b>:", reply_markup=cancel_kb())


@router.message(AdminFlow.add_outlet_name, F.text)
async def outlet_add_name(
    message: types.Message, state: FSMContext, session: AsyncSession, user: Optional[User]
) -> None:
    if message.text == "❌ Отмена":
        await state.clear()
        await message.answer(texts.CANCELLED, reply_markup=main_menu(user))
        return
    data = await state.get_data()
    name = message.text.strip()
    exists = await session.scalar(
        select(Outlet).where(Outlet.brand_id == data["brand_id"], Outlet.name == name)
    )
    if exists:
        await message.answer("Такая точка уже есть. Введите другое название.")
        return
    session.add(Outlet(brand_id=data["brand_id"], name=name))
    await session.commit()
    await state.clear()
    await message.answer(
        f"✅ Точка <b>{esc(name)}</b> добавлена.", reply_markup=main_menu(user)
    )


@router.callback_query(AdminCB.filter(F.act == "outlet_rename"))
async def cb_outlet_rename(
    call: types.CallbackQuery, callback_data: AdminCB, state: FSMContext, user: Optional[User]
) -> None:
    if not _require_admin(user):
        await call.answer(texts.NO_ACCESS, show_alert=True)
        return
    await state.set_state(AdminFlow.rename_outlet)
    await state.update_data(outlet_id=callback_data.id)
    await call.answer()
    await call.message.answer("Введите <b>новое название</b> точки:", reply_markup=cancel_kb())


@router.message(AdminFlow.rename_outlet, F.text)
async def outlet_rename(
    message: types.Message, state: FSMContext, session: AsyncSession, user: Optional[User]
) -> None:
    if message.text == "❌ Отмена":
        await state.clear()
        await message.answer(texts.CANCELLED, reply_markup=main_menu(user))
        return
    data = await state.get_data()
    outlet = await session.get(Outlet, data["outlet_id"])
    outlet.name = message.text.strip()
    await session.commit()
    await state.clear()
    await message.answer(
        f"✅ Название изменено на <b>{esc(outlet.name)}</b>.", reply_markup=main_menu(user)
    )


# --------------------------------------------------------------------------- #
# Категории
# --------------------------------------------------------------------------- #
@router.callback_query(AdminCB.filter(F.act == "categories"))
async def cb_categories(
    call: types.CallbackQuery, session: AsyncSession, user: Optional[User]
) -> None:
    if not _require_admin(user):
        await call.answer(texts.NO_ACCESS, show_alert=True)
        return
    rows = (await session.scalars(select(Category).order_by(Category.sort_order))).all()
    kb = InlineKeyboardBuilder()
    for row in rows:
        mark = "" if row.is_active else " 🚫"
        photo = " 📷" if row.photo_required else ""
        kb.button(
            text=f"{row.label}{photo}{mark}",
            callback_data=AdminCB(act="cat_card", id=row.id).pack(),
        )
    kb.adjust(1)
    kb.row(
        InlineKeyboardButton(
            text="➕ Добавить категорию", callback_data=AdminCB(act="cat_add").pack()
        )
    )
    kb.row(InlineKeyboardButton(text="⬅️ В меню", callback_data=AdminCB(act="root").pack()))
    await call.answer()
    await call.message.edit_text(
        "🗂 <b>Категории</b>\n📷 — фото обязательно, 🚫 — категория отключена.",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(AdminCB.filter(F.act == "cat_card"))
async def cb_cat_card(
    call: types.CallbackQuery, callback_data: AdminCB, session: AsyncSession, user: Optional[User]
) -> None:
    if not _require_admin(user):
        await call.answer(texts.NO_ACCESS, show_alert=True)
        return
    cat = await session.get(Category, callback_data.id)
    executors = (
        await session.scalars(
            select(User)
            .join(ExecutorAssignment, ExecutorAssignment.user_id == User.id)
            .where(
                ExecutorAssignment.category_id == cat.id,
                ExecutorAssignment.is_active.is_(True),
            )
            .distinct()
        )
    ).all()

    kb = InlineKeyboardBuilder()
    kb.button(
        text="🚫 Отключить" if cat.is_active else "✅ Включить",
        callback_data=AdminCB(act="cat_toggle", id=cat.id).pack(),
    )
    kb.button(
        text="📷 Фото не обязательно" if cat.photo_required else "📷 Сделать фото обязательным",
        callback_data=AdminCB(act="cat_photo", id=cat.id).pack(),
    )
    kb.button(text="✏️ Переименовать", callback_data=AdminCB(act="cat_rename", id=cat.id).pack())
    kb.adjust(1)
    kb.row(InlineKeyboardButton(text="⬅️ Назад", callback_data=AdminCB(act="categories").pack()))
    await call.answer()
    await call.message.edit_text(
        f"🗂 <b>{esc(cat.label)}</b>\n"
        f"Активна: {'да' if cat.is_active else 'нет'}\n"
        f"Фото обязательно: {'да' if cat.photo_required else 'нет'}\n"
        f"Исполнители: "
        + (", ".join(esc(e.full_name) for e in executors) if executors else "не назначены"),
        reply_markup=kb.as_markup(),
    )


@router.callback_query(AdminCB.filter(F.act.in_({"cat_toggle", "cat_photo"})))
async def cb_cat_toggle(
    call: types.CallbackQuery, callback_data: AdminCB, session: AsyncSession, user: Optional[User]
) -> None:
    if not _require_admin(user):
        await call.answer(texts.NO_ACCESS, show_alert=True)
        return
    cat = await session.get(Category, callback_data.id)
    if callback_data.act == "cat_toggle":
        cat.is_active = not cat.is_active
    else:
        cat.photo_required = not cat.photo_required
    await session.commit()
    await call.answer("Готово")
    await cb_cat_card(call, AdminCB(act="cat_card", id=cat.id), session, user)


@router.callback_query(AdminCB.filter(F.act == "cat_rename"))
async def cb_cat_rename(
    call: types.CallbackQuery, callback_data: AdminCB, state: FSMContext, user: Optional[User]
) -> None:
    if not _require_admin(user):
        await call.answer(texts.NO_ACCESS, show_alert=True)
        return
    await state.set_state(AdminFlow.rename_category)
    await state.update_data(category_id=callback_data.id)
    await call.answer()
    await call.message.answer(
        "Введите <b>новое название</b> категории:", reply_markup=cancel_kb()
    )


@router.message(AdminFlow.rename_category, F.text)
async def cat_rename(
    message: types.Message, state: FSMContext, session: AsyncSession, user: Optional[User]
) -> None:
    if message.text == "❌ Отмена":
        await state.clear()
        await message.answer(texts.CANCELLED, reply_markup=main_menu(user))
        return
    data = await state.get_data()
    cat = await session.get(Category, data["category_id"])
    cat.name = message.text.strip()
    await session.commit()
    await state.clear()
    await message.answer(
        f"✅ Категория переименована: <b>{esc(cat.label)}</b>", reply_markup=main_menu(user)
    )


@router.callback_query(AdminCB.filter(F.act == "cat_add"))
async def cb_cat_add(
    call: types.CallbackQuery, state: FSMContext, user: Optional[User]
) -> None:
    if not _require_admin(user):
        await call.answer(texts.NO_ACCESS, show_alert=True)
        return
    await state.set_state(AdminFlow.add_category_name)
    await call.answer()
    await call.message.answer(
        "Введите <b>название новой категории</b>:", reply_markup=cancel_kb()
    )


@router.message(AdminFlow.add_category_name, F.text)
async def cat_add_name(message: types.Message, state: FSMContext, user: Optional[User]) -> None:
    if message.text == "❌ Отмена":
        await state.clear()
        await message.answer(texts.CANCELLED, reply_markup=main_menu(user))
        return
    await state.update_data(cat_name=message.text.strip())
    await state.set_state(AdminFlow.add_category_emoji)
    await message.answer("Отправьте <b>эмодзи</b> для категории (или «-»):")


@router.message(AdminFlow.add_category_emoji, F.text)
async def cat_add_emoji(
    message: types.Message, state: FSMContext, session: AsyncSession, user: Optional[User]
) -> None:
    data = await state.get_data()
    emoji = message.text.strip()
    if emoji == "-" or len(emoji) > 4:
        emoji = "📦"
    name = data["cat_name"]
    code = f"custom_{abs(hash(name)) % 100000}"
    max_sort = await session.scalar(select(func.max(Category.sort_order))) or 0
    session.add(
        Category(code=code, name=name, emoji=emoji, sort_order=max_sort + 10)
    )
    await session.commit()
    await state.clear()
    await message.answer(
        f"✅ Категория <b>{emoji} {esc(name)}</b> добавлена.\n"
        "Не забудьте привязать к ней исполнителя: 👷 Исполнители → направления.",
        reply_markup=main_menu(user),
    )
