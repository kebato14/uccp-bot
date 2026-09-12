"""Нумерация заявок: REQ-2026-000145 (п.17). Номер после создания не меняется."""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import Counter
from ..utils import now_local


async def next_request_number(session: AsyncSession) -> str:
    year = now_local().year
    key = f"request:{year}"
    counter = await session.get(Counter, key)
    if counter is None:
        counter = Counter(key=key, value=0)
        session.add(counter)
        await session.flush()
    counter.value += 1
    await session.flush()
    return f"REQ-{year}-{counter.value:06d}"


async def next_org_number(session: AsyncSession) -> str:
    """Отдельная нумерация организационных заявок УЦЦП."""
    year = now_local().year
    key = f"org:{year}"
    counter = await session.get(Counter, key)
    if counter is None:
        counter = Counter(key=key, value=0)
        session.add(counter)
        await session.flush()
    counter.value += 1
    await session.flush()
    return f"ORG-{year}-{counter.value:06d}"


def new_external_id() -> str:
    """Уникальный внешний ID для интеграции — защищает от дублей (п.39)."""
    return uuid.uuid4().hex
