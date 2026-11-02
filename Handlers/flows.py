from aiogram.fsm.state import State, StatesGroup


class ActivationFlow(StatesGroup):
    code = State()


class AppealFlow(StatesGroup):
    explanation = State()


class AdminFlow(StatesGroup):
    reason = State()
    duration = State()
    limit = State()
    confirm = State()
    search = State()
