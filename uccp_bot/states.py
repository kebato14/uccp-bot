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
    """Обращение точки в УЦЦП. Профиль автора подставляется автоматически."""

    request_type = State()
    description = State()
    quantity = State()
    reason = State()
    due_date = State()
    due_time = State()
    media = State()
    preview = State()


class OrgFlow(StatesGroup):
    done_comment = State()
    comment = State()
    cancel_reason = State()
    return_reason = State()
    clarify_question = State()   # УЦЦП спрашивает, каких данных не хватает
    clarify_answer = State()     # автор дополняет
    reject_reason = State()


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
