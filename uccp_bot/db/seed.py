"""Первичное наполнение справочников.

Всё, что здесь создаётся, дальше редактируется администратором прямо в боте
(п.6, п.7, п.15, п.33). Повторный запуск безопасен — существующие записи не трогаем.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import (
    Brand,
    Category,
    ExecutorAssignment,
    Outlet,
    Role,
    RoleCode,
    User,
    UserStatus,
)

# Третья группа — общие объекты компании: они не относятся ни к Hotdogger,
# ни к Moose Café, но заявки по ним оформляются так же.
SHARED_GROUP = "Общие объекты"

BRANDS = [
    ("Hotdogger", 10),
    ("Moose Café", 20),
    (SHARED_GROUP, 30),
]

OUTLETS = {
    "Hotdogger": [
        "Hotdogger 82-мк",
        "Hotdogger Сиема мол",
        "Hotdogger Куруши Кабир",
        "Hotdogger Созидание",
        "Hotdogger Аэропорт",
    ],
    "Moose Café": [
        "Moose TCell",
        "Moose Opera",
        "Moose 92 мк",
        "Moose Кохи Вахдат",
        "Moose Зайнаб Мол",
    ],
    SHARED_GROUP: [
        "Офис",
        "Центральный склад Зайнаб Мол",
        "Цех Пекарня",
        "Цех Кондитерский",
    ],
}

CATEGORIES = [
    # code, name, emoji, photo_required, sort
    ("plumbing", "Сантехника", "🚰", False, 10),
    ("electric", "Электрика", "⚡", False, 20),
    ("equipment", "Обслуживание оборудования", "🔧", False, 30),
    ("ventilation", "Вентиляция", "🌬", False, 40),
    ("it", "IT", "💻", False, 50),
    ("repair", "Ремонт / мебель", "🪑", False, 60),
    ("other", "Другое", "📦", False, 70),
]

# Исполнители из п.13 ТЗ. tg_id не заполняем — он подставится, когда мастер нажмёт /start.
# brands=None означает «оба бренда» (п.15).
#
# Контактные данные (телефон, @username) здесь намеренно не хранятся:
# их вносит администратор в боте — ⚙️ Администрирование → 👥 Пользователи.
# Так личные данные сотрудников остаются только в рабочей базе.
EXECUTORS = [
    {
        "full_name": "Асрор",
        "username": None,
        "phone": None,
        "categories": ["plumbing"],
        "brands": None,
    },
    {
        "full_name": "Алишер",
        "username": None,
        "phone": None,
        "categories": ["equipment"],
        # Мастер по Т.О. и ремонту оборудования обоих брендов — Hotdogger и Moose.
        "brands": None,
    },
    {
        "full_name": "Назрулло",
        "username": None,
        "phone": None,
        "categories": ["electric"],
        "brands": None,
    },
]


async def _get_or_create_brand(session: AsyncSession, name: str, sort: int) -> Brand:
    brand = await session.scalar(select(Brand).where(Brand.name == name))
    if brand is None:
        brand = Brand(name=name, sort_order=sort)
        session.add(brand)
        await session.flush()
    return brand


async def seed(session: AsyncSession) -> None:
    # --- роли ---
    for i, code in enumerate(RoleCode.ALL):
        exists = await session.scalar(select(Role).where(Role.code == code))
        if exists is None:
            session.add(Role(code=code, title=RoleCode.TITLES[code], sort_order=(i + 1) * 10))

    # --- бренды и точки ---
    # Точки создаём только при первом наполнении бренда. Иначе переименованная
    # или отключённая администратором точка появлялась бы заново после
    # каждого перезапуска (и в справочнике возникали дубли).
    for name, sort in BRANDS:
        brand = await _get_or_create_brand(session, name, sort)
        has_outlets = await session.scalar(
            select(func.count(Outlet.id)).where(Outlet.brand_id == brand.id)
        )
        if has_outlets:
            continue
        for idx, outlet_name in enumerate(OUTLETS.get(name, [])):
            session.add(
                Outlet(brand_id=brand.id, name=outlet_name, sort_order=(idx + 1) * 10)
            )

    # --- категории ---
    for code, name, emoji, photo_required, sort in CATEGORIES:
        exists = await session.scalar(select(Category).where(Category.code == code))
        if exists is None:
            session.add(
                Category(
                    code=code,
                    name=name,
                    emoji=emoji,
                    photo_required=photo_required,
                    sort_order=sort,
                )
            )

    await session.flush()

    # --- исполнители из ТЗ ---
    # Только при первом наполнении: если в системе уже есть хотя бы один мастер,
    # справочником управляет администратор — удалённые или переименованные
    # им записи заново не создаём.
    has_executors = await session.scalar(
        select(func.count(User.id)).where(User.role_code == RoleCode.EXECUTOR)
    )
    if has_executors:
        await session.commit()
        return

    for spec in EXECUTORS:
        user = await session.scalar(
            select(User).where(User.full_name == spec["full_name"], User.role_code == RoleCode.EXECUTOR)
        )
        if user is None:
            user = User(
                full_name=spec["full_name"],
                last_name=spec["full_name"],
                username=spec["username"],
                phone=spec["phone"],
                position_text="Мастер (из ТЗ)",
                role_code=RoleCode.EXECUTOR,
                status=UserStatus.ACTIVE,   # мастера из ТЗ уже согласованы
                is_active=True,
                is_registered=False,        # но бот ещё не активирован
            )
            session.add(user)
            await session.flush()

        brand_ids = [None]
        if spec["brands"]:
            brand_ids = []
            for brand_name in spec["brands"]:
                brand = await session.scalar(select(Brand).where(Brand.name == brand_name))
                if brand:
                    brand_ids.append(brand.id)

        for cat_code in spec["categories"]:
            category = await session.scalar(select(Category).where(Category.code == cat_code))
            if category is None:
                continue
            if spec["brands"] is None:
                # «оба бренда» — снимаем прежние привязки к отдельному бренду
                narrow = (
                    await session.scalars(
                        select(ExecutorAssignment).where(
                            ExecutorAssignment.user_id == user.id,
                            ExecutorAssignment.category_id == category.id,
                            ExecutorAssignment.brand_id.is_not(None),
                        )
                    )
                ).all()
                for item in narrow:
                    await session.delete(item)
                await session.flush()
            for brand_id in brand_ids:
                exists = await session.scalar(
                    select(ExecutorAssignment).where(
                        ExecutorAssignment.user_id == user.id,
                        ExecutorAssignment.category_id == category.id,
                        ExecutorAssignment.brand_id.is_(brand_id) if brand_id is None
                        else ExecutorAssignment.brand_id == brand_id,
                    )
                )
                if exists is None:
                    session.add(
                        ExecutorAssignment(
                            user_id=user.id,
                            category_id=category.id,
                            brand_id=brand_id,
                        )
                    )

    await session.commit()


async def ensure_bootstrap_admins(session: AsyncSession, admin_ids) -> None:
    """Telegram ID из .env получают роль администратора бота."""
    for tg_id in admin_ids:
        user: Optional[User] = await session.scalar(select(User).where(User.tg_id == tg_id))
        if user is None:
            session.add(
                User(
                    tg_id=tg_id,
                    full_name=f"Администратор {tg_id}",
                    last_name=f"Администратор {tg_id}",
                    role_code=RoleCode.ADMIN,
                    status=UserStatus.ACTIVE,
                    is_active=True,
                    is_registered=False,
                )
            )
        else:
            user.role_code = RoleCode.ADMIN
            user.status = UserStatus.ACTIVE
            user.is_active = True
    await session.commit()
