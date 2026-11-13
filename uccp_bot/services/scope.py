"""Область видимости заявок по роли и закреплённым объектам.

Матрица прав из ТЗ:
  • главный администратор — все объекты;
  • главный операционный директор — все объекты обоих брендов, только просмотр;
  • операционный директор — свой бренд плюс объекты, закреплённые администратором;
  • администратор точки — своя точка;
  • исполнитель — только назначенные ему заявки;
  • сотрудник точки — только свои заявки.
"""
from __future__ import annotations

from typing import List, Optional, Tuple

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..db.models import OrgRequest, Request, RoleCode, User, UserScope


async def extra_scopes(session: AsyncSession, user: User) -> Tuple[List[int], List[int]]:
    """Дополнительно закреплённые бренды и точки."""
    rows = (
        await session.scalars(select(UserScope).where(UserScope.user_id == user.id))
    ).all()
    brands = [r.brand_id for r in rows if r.brand_id and not r.outlet_id]
    outlets = [r.outlet_id for r in rows if r.outlet_id]
    return brands, outlets


async def request_condition(session: AsyncSession, user: User):
    """Условие выборки технических заявок. None — видно всё."""
    if user.is_admin or user.is_ops_chief:
        return None

    extra_brands, extra_outlets = await extra_scopes(session, user)

    if user.is_ops_director:
        allowed_brands = [b for b in [user.brand_id, *extra_brands] if b]
        clauses = []
        if allowed_brands:
            clauses.append(Request.brand_id.in_(allowed_brands))
        if extra_outlets:
            clauses.append(Request.outlet_id.in_(extra_outlets))
        clauses.append(Request.author_id == user.id)
        return or_(*clauses)

    if user.is_outlet_admin:
        allowed_outlets = [o for o in [user.outlet_id, *extra_outlets] if o]
        clauses = []
        if allowed_outlets:
            clauses.append(Request.outlet_id.in_(allowed_outlets))
        if extra_brands:
            clauses.append(Request.brand_id.in_(extra_brands))
        clauses.append(Request.author_id == user.id)
        return or_(*clauses)

    if user.is_executor:
        return Request.executor_id == user.id

    return or_(Request.author_id == user.id, Request.executor_id == user.id)


async def can_view_request(session: AsyncSession, user: User, request: Request) -> bool:
    if user.is_admin or user.is_ops_chief:
        return True
    if request.author_id == user.id or request.executor_id == user.id:
        return True
    extra_brands, extra_outlets = await extra_scopes(session, user)
    if user.is_ops_director:
        return request.brand_id in [user.brand_id, *extra_brands] or (
            request.outlet_id in extra_outlets
        )
    if user.is_outlet_admin:
        return request.outlet_id in [user.outlet_id, *extra_outlets] or (
            request.brand_id in extra_brands
        )
    return False


async def org_condition(session: AsyncSession, user: User):
    """То же для обращений в УЦЦП."""
    from .org import is_uccp

    if is_uccp(user) or user.is_ops_chief:
        return None

    extra_brands, extra_outlets = await extra_scopes(session, user)
    clauses = [
        OrgRequest.author_id == user.id,
        OrgRequest.assignee_id == user.id,
    ]
    if user.is_ops_director:
        allowed = [b for b in [user.brand_id, *extra_brands] if b]
        if allowed:
            clauses.append(OrgRequest.brand_id.in_(allowed))
        if extra_outlets:
            clauses.append(OrgRequest.outlet_id.in_(extra_outlets))
    elif user.is_outlet_admin:
        allowed = [o for o in [user.outlet_id, *extra_outlets] if o]
        if allowed:
            clauses.append(OrgRequest.outlet_id.in_(allowed))
    return or_(*clauses)
