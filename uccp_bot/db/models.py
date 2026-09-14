"""Модель данных УЦЦП (п.46 ТЗ).

Справочники (бренды, точки, категории, роли) хранятся в БД, а не в коде,
чтобы новые бренды/точки добавлялись без правки программы.
"""
from __future__ import annotations

import datetime as dt
from typing import List, Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


# --------------------------------------------------------------------------- #
# Справочник ролей и статусов
# --------------------------------------------------------------------------- #
class RoleCode:
    """Роли и объём доступа. Роль назначает только администратор бота."""

    STAFF = "staff"                  # Сотрудник / менеджер точки — свои заявки
    OUTLET_ADMIN = "outlet_admin"    # Администратор точки — все заявки своей точки
    OPS_DIRECTOR = "ops"             # Операционный директор — все точки своего бренда
    EXECUTOR = "executor"            # Мастер / исполнитель — только назначенные ему
    ADMIN = "admin"                  # Администратор системы — полный доступ

    ALL = (STAFF, OUTLET_ADMIN, OPS_DIRECTOR, EXECUTOR, ADMIN)
    TITLES = {
        STAFF: "Сотрудник / менеджер точки",
        OUTLET_ADMIN: "Администратор точки",
        OPS_DIRECTOR: "Операционный директор",
        EXECUTOR: "Мастер / исполнитель",
        ADMIN: "Администратор системы",
    }
    DESCRIPTIONS = {
        STAFF: "создаёт заявки и видит только свои",
        OUTLET_ADMIN: "видит все заявки своей торговой точки",
        OPS_DIRECTOR: "видит все заявки своего бренда",
        EXECUTOR: "видит только назначенные ему заявки",
        ADMIN: "полный доступ, подтверждает регистрации",
    }
    # старые коды ролей -> новые (миграция существующей базы)
    LEGACY = {"manager": STAFF}


class UserStatus:
    """Состояние доступа. Пока не ACTIVE — рабочий функционал закрыт."""

    PENDING = "pending"      # заявка на регистрацию ждёт подтверждения
    ACTIVE = "active"        # доступ выдан администратором
    BLOCKED = "blocked"      # временно заблокирован
    DISABLED = "disabled"    # отключён полностью
    REJECTED = "rejected"    # заявка отклонена

    TITLES = {
        PENDING: "⏳ Ожидает подтверждения",
        ACTIVE: "✅ Доступ выдан",
        BLOCKED: "⛔️ Временно заблокирован",
        DISABLED: "🚫 Отключён",
        REJECTED: "❌ Заявка отклонена",
    }


class Status:
    NEW = "new"                                  # Новая
    AWAITING_ASSIGNMENT = "awaiting_assignment"  # Ожидает назначения
    ACCEPTED = "accepted"                        # Принята
    RESCHEDULE_PROPOSED = "reschedule_proposed"  # Предложен новый срок
    IN_PROGRESS = "in_progress"                  # В процессе
    DONE = "done"                                # Выполнена
    RETURNED = "returned"                        # Возвращена на доработку
    CONFIRMED = "confirmed"                      # Подтверждена
    CLOSED = "closed"                            # Закрыта
    REJECTED = "rejected"                        # Отклонена
    CANCELLED = "cancelled"                      # Отменена

    TITLES = {
        NEW: "🆕 Новая",
        AWAITING_ASSIGNMENT: "⏳ Ожидает назначения исполнителя",
        ACCEPTED: "👍 Принята",
        RESCHEDULE_PROPOSED: "📅 Предложен новый срок",
        IN_PROGRESS: "🔧 В процессе",
        DONE: "✅ Выполнена",
        RETURNED: "🔄 Возвращена на доработку",
        CONFIRMED: "☑️ Подтверждена",
        CLOSED: "🔒 Закрыта",
        REJECTED: "❌ Отклонена",
        CANCELLED: "🚫 Отменена",
    }

    # Заявка считается активной (в работе) в этих статусах
    OPEN = (
        NEW,
        AWAITING_ASSIGNMENT,
        ACCEPTED,
        RESCHEDULE_PROPOSED,
        IN_PROGRESS,
        RETURNED,
        DONE,
    )
    FINAL = (CLOSED, REJECTED, CANCELLED)

    @classmethod
    def title(cls, code: str) -> str:
        return cls.TITLES.get(code, code)


class Priority:
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    TITLES = {
        LOW: "🟢 Низкий",
        MEDIUM: "🟡 Средний",
        HIGH: "🔴 Высокий",
        CRITICAL: "🚨 Критический",
    }
    ORDER = (LOW, MEDIUM, HIGH, CRITICAL)

    @classmethod
    def title(cls, code: str) -> str:
        return cls.TITLES.get(code, code)


# --------------------------------------------------------------------------- #
# Справочники
# --------------------------------------------------------------------------- #
class Role(Base):
    __tablename__ = "roles"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)
    title: Mapped[str] = mapped_column(String(128))
    sort_order: Mapped[int] = mapped_column(Integer, default=100)


class Brand(Base):
    __tablename__ = "brands"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=100)

    outlets: Mapped[List["Outlet"]] = relationship(back_populates="brand")


class Outlet(Base):
    __tablename__ = "outlets"

    id: Mapped[int] = mapped_column(primary_key=True)
    brand_id: Mapped[int] = mapped_column(ForeignKey("brands.id"))
    name: Mapped[str] = mapped_column(String(160))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=100)

    brand: Mapped[Brand] = relationship(back_populates="outlets")

    __table_args__ = (UniqueConstraint("brand_id", "name", name="uq_outlet_brand_name"),)


class Category(Base):
    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(128))
    emoji: Mapped[str] = mapped_column(String(8), default="📦")
    photo_required: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=100)

    @property
    def label(self) -> str:
        return f"{self.emoji} {self.name}".strip()


class Equipment(Base):
    """История оборудования (п.38) — заявку можно привязать к единице оборудования."""

    __tablename__ = "equipment"

    id: Mapped[int] = mapped_column(primary_key=True)
    outlet_id: Mapped[int] = mapped_column(ForeignKey("outlets.id"))
    name: Mapped[str] = mapped_column(String(160))
    code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    outlet: Mapped[Outlet] = relationship()


# --------------------------------------------------------------------------- #
# Пользователи и исполнители
# --------------------------------------------------------------------------- #
class User(Base):
    """Пользователь бота.

    Пользователь сам указывает только сведения о себе (имя, фамилия, должность,
    бренд и точка). Роль, область доступа и направления мастера назначает
    исключительно администратор при подтверждении регистрации.
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    # tg_id пустой, если мастера завёл администратор, а сам мастер ещё не нажал /start
    tg_id: Mapped[Optional[int]] = mapped_column(BigInteger, unique=True, nullable=True)
    username: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    first_name: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    last_name: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    full_name: Mapped[str] = mapped_column(String(160))
    phone: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    # должность своими словами — то, что человек написал о себе при регистрации
    position_text: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)

    role_code: Mapped[str] = mapped_column(String(32), default=RoleCode.STAFF)
    status: Mapped[str] = mapped_column(String(16), default=UserStatus.PENDING)
    brand_id: Mapped[Optional[int]] = mapped_column(ForeignKey("brands.id"), nullable=True)
    outlet_id: Mapped[Optional[int]] = mapped_column(ForeignKey("outlets.id"), nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    is_registered: Mapped[bool] = mapped_column(Boolean, default=False)
    # Мастер на фиксированной ежемесячной оплате (например, обслуживание
    # кофемашин): при закрытии заявки стоимость работ и материалов не спрашиваем.
    fixed_payment: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    applied_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)
    approved_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)
    approved_by_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )
    reject_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    brand: Mapped[Optional[Brand]] = relationship(foreign_keys=[brand_id])
    outlet: Mapped[Optional[Outlet]] = relationship(foreign_keys=[outlet_id])
    approved_by: Mapped[Optional["User"]] = relationship(
        remote_side=[id], foreign_keys=[approved_by_id]
    )
    assignments: Mapped[List["ExecutorAssignment"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        foreign_keys="ExecutorAssignment.user_id",
    )

    # ------------------------------------------------------------------ #
    # Отображение
    # ------------------------------------------------------------------ #
    @property
    def role_title(self) -> str:
        return RoleCode.TITLES.get(self.role_code, self.role_code)

    @property
    def status_title(self) -> str:
        return UserStatus.TITLES.get(self.status, self.status)

    @property
    def display_name(self) -> str:
        parts = [p for p in (self.last_name, self.first_name) if p]
        return " ".join(parts) if parts else self.full_name

    def compose_full_name(self) -> str:
        parts = [p for p in (self.last_name, self.first_name) if p]
        return " ".join(parts) if parts else (self.full_name or "")

    # ------------------------------------------------------------------ #
    # Роли
    # ------------------------------------------------------------------ #
    @property
    def is_admin(self) -> bool:
        return self.role_code == RoleCode.ADMIN

    @property
    def is_executor(self) -> bool:
        return self.role_code == RoleCode.EXECUTOR

    @property
    def is_outlet_admin(self) -> bool:
        return self.role_code == RoleCode.OUTLET_ADMIN

    @property
    def is_ops_director(self) -> bool:
        return self.role_code == RoleCode.OPS_DIRECTOR

    # ------------------------------------------------------------------ #
    # Доступ
    # ------------------------------------------------------------------ #
    @property
    def is_approved(self) -> bool:
        return self.status == UserStatus.ACTIVE

    @property
    def scope(self) -> str:
        """Что пользователь видит: all | brand | outlet | assigned | own."""
        if self.is_admin:
            return "all"
        if self.is_ops_director:
            return "brand"
        if self.is_outlet_admin:
            return "outlet"
        if self.is_executor:
            return "assigned"
        return "own"

    @property
    def can_confirm(self) -> bool:
        """Кто может подтверждать выполнение (п.22-23). Исполнитель — не может."""
        return self.is_approved and self.role_code in (
            RoleCode.STAFF,
            RoleCode.OUTLET_ADMIN,
            RoleCode.OPS_DIRECTOR,
            RoleCode.ADMIN,
        )

    @property
    def payment_title(self) -> str:
        return (
            "фиксированная ежемесячная"
            if self.fixed_payment
            else "по каждой заявке"
        )

    @property
    def can_see_reports(self) -> bool:
        return self.is_approved and self.role_code in (
            RoleCode.OUTLET_ADMIN,
            RoleCode.OPS_DIRECTOR,
            RoleCode.ADMIN,
        )

    def sync_flags(self) -> None:
        """is_active держим согласованным со статусом — им пользуется маршрутизация."""
        self.is_active = self.status == UserStatus.ACTIVE


class ExecutorAssignment(Base):
    """Привязка исполнителя к направлению (п.15).

    brand_id = NULL  -> исполнитель работает по обоим брендам
    outlet_id = NULL -> исполнитель работает по всем точкам бренда
    """

    __tablename__ = "executor_assignments"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id"))
    brand_id: Mapped[Optional[int]] = mapped_column(ForeignKey("brands.id"), nullable=True)
    outlet_id: Mapped[Optional[int]] = mapped_column(ForeignKey("outlets.id"), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    priority: Mapped[int] = mapped_column(Integer, default=100)  # меньше = выше приоритет
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)

    user: Mapped[User] = relationship(
        back_populates="assignments", foreign_keys=[user_id]
    )
    category: Mapped[Category] = relationship()
    brand: Mapped[Optional[Brand]] = relationship()
    outlet: Mapped[Optional[Outlet]] = relationship()


# --------------------------------------------------------------------------- #
# Заявки
# --------------------------------------------------------------------------- #
class Request(Base):
    __tablename__ = "requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[str] = mapped_column(String(32), unique=True)          # REQ-2026-000145
    external_id: Mapped[str] = mapped_column(String(64), unique=True)      # уникальный ID для интеграции (п.39)

    author_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    brand_id: Mapped[int] = mapped_column(ForeignKey("brands.id"))
    outlet_id: Mapped[int] = mapped_column(ForeignKey("outlets.id"))
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id"))
    equipment_id: Mapped[Optional[int]] = mapped_column(ForeignKey("equipment.id"), nullable=True)

    description: Mapped[str] = mapped_column(Text)
    priority: Mapped[str] = mapped_column(String(16), default=Priority.MEDIUM)
    status: Mapped[str] = mapped_column(String(32), default=Status.NEW)

    executor_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    due_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)
    proposed_due_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)
    accepted_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)
    started_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)
    done_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)
    confirmed_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)
    closed_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)

    work_cost: Mapped[Optional[float]] = mapped_column(Numeric(12, 2), nullable=True)
    material_cost: Mapped[Optional[float]] = mapped_column(Numeric(12, 2), nullable=True)

    executor_comment: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    manager_comment: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    reject_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    return_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    is_overdue: Mapped[bool] = mapped_column(Boolean, default=False)
    overdue_notified_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)
    source_chat_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    # Работа входит в фиксированную ежемесячную оплату мастера — отдельной
    # суммы к оплате по заявке нет. Отметка ставится в момент выполнения,
    # поэтому история остаётся верной даже если условия мастера поменяются.
    cost_exempt: Mapped[bool] = mapped_column(Boolean, default=False)

    author: Mapped[User] = relationship(foreign_keys=[author_id])
    executor: Mapped[Optional[User]] = relationship(foreign_keys=[executor_id])
    brand: Mapped[Brand] = relationship()
    outlet: Mapped[Outlet] = relationship()
    category: Mapped[Category] = relationship()
    equipment: Mapped[Optional[Equipment]] = relationship()
    attachments: Mapped[List["Attachment"]] = relationship(
        back_populates="request", cascade="all, delete-orphan"
    )
    comments: Mapped[List["Comment"]] = relationship(
        back_populates="request", cascade="all, delete-orphan"
    )
    history: Mapped[List["RequestStatusHistory"]] = relationship(
        back_populates="request", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_requests_status", "status"),
        Index("ix_requests_executor", "executor_id"),
        Index("ix_requests_created", "created_at"),
    )

    @property
    def total_cost(self) -> float:
        return float(self.work_cost or 0) + float(self.material_cost or 0)

    @property
    def cost_title(self) -> str:
        """Как показывать деньги в карточке и отчётах."""
        if self.cost_exempt:
            return "входит в фиксированную месячную оплату мастера"
        return ""

    @property
    def status_title(self) -> str:
        base = Status.title(self.status)
        if self.is_overdue and self.status in Status.OPEN:
            return f"{base} · 🔴 Просрочена"
        return base

    @property
    def priority_title(self) -> str:
        return Priority.title(self.priority)


class Attachment(Base):
    __tablename__ = "attachments"

    id: Mapped[int] = mapped_column(primary_key=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("requests.id", ondelete="CASCADE"))
    file_id: Mapped[str] = mapped_column(String(256))
    file_unique_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    media_type: Mapped[str] = mapped_column(String(16), default="photo")  # photo | video | document
    stage: Mapped[str] = mapped_column(String(16), default="before")      # before | after
    uploaded_by_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)

    request: Mapped[Request] = relationship(back_populates="attachments")


class Comment(Base):
    __tablename__ = "comments"

    id: Mapped[int] = mapped_column(primary_key=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("requests.id", ondelete="CASCADE"))
    user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)

    request: Mapped[Request] = relationship(back_populates="comments")
    user: Mapped[Optional[User]] = relationship()


class Cost(Base):
    """Детализация расходов. Итоговые суммы дублируются в Request для быстрых отчётов."""

    __tablename__ = "costs"

    id: Mapped[int] = mapped_column(primary_key=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("requests.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(16))  # work | material
    amount: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    added_by_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class RequestStatusHistory(Base):
    """Полный журнал действий (п.24). Записи не удаляются."""

    __tablename__ = "request_status_history"

    id: Mapped[int] = mapped_column(primary_key=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("requests.id", ondelete="CASCADE"))
    user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)
    action: Mapped[str] = mapped_column(String(64))
    old_status: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    new_status: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    details: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)

    request: Mapped[Request] = relationship(back_populates="history")
    user: Mapped[Optional[User]] = relationship()


class OrgStatus:
    """Статусы организационных заявок УЦЦП (отдельно от ремонтных)."""

    NEW = "new"                  # Новая
    IN_PROGRESS = "in_progress"  # В работе
    DONE = "done"                # Выполнена, ждёт подтверждения автора
    CLOSED = "closed"            # Закрыта
    CANCELLED = "cancelled"      # Отменена

    TITLES = {
        NEW: "🆕 Новая",
        IN_PROGRESS: "🔧 В работе",
        DONE: "✅ Выполнена",
        CLOSED: "🔒 Закрыта",
        CANCELLED: "🚫 Отменена",
    }
    OPEN = (NEW, IN_PROGRESS, DONE)
    FINAL = (CLOSED, CANCELLED)

    @classmethod
    def title(cls, code: str) -> str:
        return cls.TITLES.get(code, code)


class OrgRequest(Base):
    """Организационная заявка УЦЦП.

    Хранится отдельно от ремонтных заявок (таблица requests), чтобы статистика
    не смешивалась. Сводный отчёт по обоим модулям доступен администратору.

    Структура: заявка → точка/объект → описание задачи → срок → автор →
    ответственный → статус.
    """

    __tablename__ = "org_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[str] = mapped_column(String(32), unique=True)      # ORG-2026-000012
    external_id: Mapped[str] = mapped_column(String(64), unique=True)

    # объект: либо торговая точка из справочника, либо произвольный объект
    outlet_id: Mapped[Optional[int]] = mapped_column(ForeignKey("outlets.id"), nullable=True)
    brand_id: Mapped[Optional[int]] = mapped_column(ForeignKey("brands.id"), nullable=True)
    object_text: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)

    description: Mapped[str] = mapped_column(Text)
    due_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)

    author_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    assignee_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)

    status: Mapped[str] = mapped_column(String(32), default=OrgStatus.NEW)
    priority: Mapped[str] = mapped_column(String(16), default=Priority.MEDIUM)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    started_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)
    done_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)
    closed_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)

    result_comment: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    author_comment: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    cost: Mapped[Optional[float]] = mapped_column(Numeric(12, 2), nullable=True)

    is_overdue: Mapped[bool] = mapped_column(Boolean, default=False)
    overdue_notified_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)

    author: Mapped["User"] = relationship(foreign_keys=[author_id])
    assignee: Mapped[Optional["User"]] = relationship(foreign_keys=[assignee_id])
    outlet: Mapped[Optional[Outlet]] = relationship()
    brand: Mapped[Optional[Brand]] = relationship()
    history: Mapped[List["OrgRequestHistory"]] = relationship(
        back_populates="request", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_org_requests_status", "status"),
        Index("ix_org_requests_created", "created_at"),
    )

    @property
    def object_name(self) -> str:
        if self.outlet:
            return self.outlet.name
        return self.object_text or "—"

    @property
    def status_title(self) -> str:
        base = OrgStatus.title(self.status)
        if self.is_overdue and self.status in OrgStatus.OPEN:
            return f"{base} · 🔴 Просрочена"
        return base

    @property
    def priority_title(self) -> str:
        return Priority.title(self.priority)

    @property
    def total_cost(self) -> float:
        return float(self.cost or 0)


class OrgRequestHistory(Base):
    """Журнал действий по организационной заявке. Записи не удаляются."""

    __tablename__ = "org_request_history"

    id: Mapped[int] = mapped_column(primary_key=True)
    request_id: Mapped[int] = mapped_column(
        ForeignKey("org_requests.id", ondelete="CASCADE")
    )
    user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)
    action: Mapped[str] = mapped_column(String(64))
    old_status: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    new_status: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    details: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)

    request: Mapped[OrgRequest] = relationship(back_populates="history")
    user: Mapped[Optional["User"]] = relationship()


class TelegramGroup(Base):
    """Рабочие группы (п.13, п.29)."""

    __tablename__ = "telegram_groups"

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    title: Mapped[str] = mapped_column(String(200))
    brand_id: Mapped[Optional[int]] = mapped_column(ForeignKey("brands.id"), nullable=True)
    category_id: Mapped[Optional[int]] = mapped_column(ForeignKey("categories.id"), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)

    brand: Mapped[Optional[Brand]] = relationship()
    category: Mapped[Optional[Category]] = relationship()


class Counter(Base):
    """Сквозная нумерация заявок по годам (п.17)."""

    __tablename__ = "counters"

    key: Mapped[str] = mapped_column(String(32), primary_key=True)
    value: Mapped[int] = mapped_column(Integer, default=0)


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
