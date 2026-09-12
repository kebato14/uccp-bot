"""FSM-состояния."""
from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class Registration(StatesGroup):
    """Пользователь сообщает только сведения о себе. Роль назначает администратор."""

    first_name = State()
    last_name = State()
    position = State()
    brand = State()
    outlet = State()


class NewRequest(StatesGroup):
    brand = State()
    outlet = State()
    category = State()
    description = State()
    media = State()
    priority = State()
    due_date = State()
    due_time = State()
    preview = State()


class NewOrgRequest(StatesGroup):
    """Мастер создания организационной заявки УЦЦП."""

    obj = State()
    obj_outlet = State()
    obj_custom = State()
    description = State()
    priority = State()
    due_date = State()
    due_time = State()
    assignee = State()
    preview = State()


class OrgFlow(StatesGroup):
    done_comment = State()
    cost = State()
    comment = State()
    cancel_reason = State()
    return_reason = State()


class ExecutorFlow(StatesGroup):
    reject_reason = State()
    propose_date = State()
    propose_time = State()
    comment = State()
    done_comment = State()
    done_photo = State()
    done_work_cost = State()
    done_material_cost = State()


class ManagerFlow(StatesGroup):
    return_reason = State()
    comment = State()


class Approval(StatesGroup):
    """Подтверждение регистрации администратором."""

    role = State()
    brand = State()
    outlet = State()
    directions = State()
    reject_reason = State()


class AdminFlow(StatesGroup):
    add_outlet_name = State()
    rename_outlet = State()
    add_category_name = State()
    add_category_emoji = State()
    rename_category = State()
    add_executor_name = State()
    add_executor_phone = State()
    add_executor_username = State()
    search_user = State()
    assign_pick = State()
    cancel_reason = State()
    set_phone = State()


class ReportFlow(StatesGroup):
    custom_from = State()
    custom_to = State()
