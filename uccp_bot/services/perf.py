"""Замер скорости по этапам обработки.

Для каждого обновления Telegram считаем отдельно:
  • сколько заняла вся обработка (от получения до отправки ответа);
  • сколько из этого ушло на запросы к базе;
  • сколько — на вызовы Telegram API и сколько их было.

Данные копятся в памяти (последние 500 обновлений) и показываются
командой /perf. Так видно, какой именно этап тормозит.
"""
from __future__ import annotations

import time
from collections import deque
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Deque, Dict, List, Optional, Tuple

# накопители на время обработки одного обновления
_db_time: ContextVar[float] = ContextVar("db_time", default=0.0)
_db_calls: ContextVar[int] = ContextVar("db_calls", default=0)
_api_time: ContextVar[float] = ContextVar("api_time", default=0.0)
_api_calls: ContextVar[int] = ContextVar("api_calls", default=0)

MAX_SAMPLES = 500


@dataclass
class Sample:
    kind: str          # message | callback_query | ...
    label: str         # текст команды или callback
    total_ms: float
    db_ms: float
    db_calls: int
    api_ms: float
    api_calls: int
    at: float


SAMPLES: Deque[Sample] = deque(maxlen=MAX_SAMPLES)


def reset() -> None:
    _db_time.set(0.0)
    _db_calls.set(0)
    _api_time.set(0.0)
    _api_calls.set(0)


def add_db(seconds: float) -> None:
    _db_time.set(_db_time.get() + seconds)
    _db_calls.set(_db_calls.get() + 1)


def add_api(seconds: float) -> None:
    _api_time.set(_api_time.get() + seconds)
    _api_calls.set(_api_calls.get() + 1)


def record(kind: str, label: str, total_seconds: float) -> Sample:
    sample = Sample(
        kind=kind,
        label=label[:40],
        total_ms=total_seconds * 1000,
        db_ms=_db_time.get() * 1000,
        db_calls=_db_calls.get(),
        api_ms=_api_time.get() * 1000,
        api_calls=_api_calls.get(),
        at=time.time(),
    )
    SAMPLES.append(sample)
    return sample


def _percentile(values: List[float], share: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(len(ordered) * share))
    return ordered[index]


def stats() -> Dict[str, float]:
    if not SAMPLES:
        return {}
    totals = [s.total_ms for s in SAMPLES]
    return {
        "count": len(SAMPLES),
        "total_p50": _percentile(totals, 0.5),
        "total_p95": _percentile(totals, 0.95),
        "total_max": max(totals),
        "db_p50": _percentile([s.db_ms for s in SAMPLES], 0.5),
        "db_max": max(s.db_ms for s in SAMPLES),
        "api_p50": _percentile([s.api_ms for s in SAMPLES], 0.5),
        "api_max": max(s.api_ms for s in SAMPLES),
        "api_calls_avg": sum(s.api_calls for s in SAMPLES) / len(SAMPLES),
        "own_p50": _percentile(
            [max(0.0, s.total_ms - s.db_ms - s.api_ms) for s in SAMPLES], 0.5
        ),
    }


def slowest(limit: int = 5) -> List[Sample]:
    return sorted(SAMPLES, key=lambda s: s.total_ms, reverse=True)[:limit]
