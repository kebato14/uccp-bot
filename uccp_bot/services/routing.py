"""Автоматическое определение исполнителя (п.12, п.14, п.32).

Правило: категория → направление → зарегистрированный исполнитель.
Если подходящего активного исполнителя нет — заявка уходит администратору,
но никогда не теряется (п.49).
"""
from __future__ import annotations

from typing import List, Optional, Tuple

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..db.models import ExecutorAssignment, RoleCode, User


def _specificity(assignment: ExecutorAssignment) -> int:
    """Чем конкретнее привязка, тем выше приоритет: точка > бренд > всё."""
    if assignment.outlet_id:
        return 0
    if assignment.brand_id:
        return 1
    return 2


async def find_executor(
    session: AsyncSession,
    category_id: int,
    brand_id: int,
    outlet_id: Optional[int] = None,
) -> Optional[User]:
    stmt = (
        select(ExecutorAssignment)
        .join(User, ExecutorAssignment.user_id == User.id)
        .where(
            ExecutorAssignment.category_id == category_id,
            ExecutorAssignment.is_active.is_(True),
            User.is_active.is_(True),
            User.role_code == RoleCode.EXECUTOR,
            or_(ExecutorAssignment.brand_id.is_(None), ExecutorAssignment.brand_id == brand_id),
            or_(
                ExecutorAssignment.outlet_id.is_(None),
                ExecutorAssignment.outlet_id == outlet_id,
            ),
        )
        .options(selectinload(ExecutorAssignment.user))
    )
    assignments: List[ExecutorAssignment] = list((await session.scalars(stmt)).all())
    if not assignments:
        return None

    assignments.sort(key=lambda a: (_specificity(a), a.priority, a.id))
    # Зарегистрированный (нажавший /start) мастер получает приоритет — ему дойдёт уведомление.
    for assignment in assignments:
        if assignment.user.tg_id:
            return assignment.user
    return assignments[0].user


async def find_executors_for_category(
    session: AsyncSession, category_id: int
) -> List[User]:
    stmt = (
        select(User)
        .join(ExecutorAssignment, ExecutorAssignment.user_id == User.id)
        .where(
            ExecutorAssignment.category_id == category_id,
            ExecutorAssignment.is_active.is_(True),
            User.is_active.is_(True),
            User.role_code == RoleCode.EXECUTOR,
        )
        .distinct()
    )
    return list((await session.scalars(stmt)).all())


async def all_executors(session: AsyncSession) -> List[User]:
    stmt = (
        select(User)
        .where(User.role_code == RoleCode.EXECUTOR, User.is_active.is_(True))
        .order_by(User.full_name)
    )
    return list((await session.scalars(stmt)).all())


async def admins(session: AsyncSession) -> List[User]:
    stmt = (
        select(User)
        .where(
            User.role_code == RoleCode.ADMIN,
            User.is_active.is_(True),
            User.tg_id.is_not(None),
        )
        .order_by(User.id)
    )
    return list((await session.scalars(stmt)).all())


async def resolve_route(
    session: AsyncSession, category_id: int, brand_id: int, outlet_id: int
) -> Tuple[Optional[User], str]:
    """Возвращает (исполнитель, причина).

    Причины: 'ok' — мастер найден и зарегистрирован;
             'unregistered' — мастер найден, но ещё не нажал /start;
             'none' — исполнителя по направлению нет.
    """
    executor = await find_executor(session, category_id, brand_id, outlet_id)
    if executor is None:
        return None, "none"
    if not executor.tg_id:
        return executor, "unregistered"
    return executor, "ok"
