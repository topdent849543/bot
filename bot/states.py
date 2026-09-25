from aiogram.fsm.state import State, StatesGroup


class AdminStates(StatesGroup):
    task_title = State()
    task_url = State()
    prize_name = State()
    prize_description = State()
    target_user_id = State()
    custom_outcome = State()
