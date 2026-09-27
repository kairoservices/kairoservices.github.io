from aiogram.fsm.state import State, StatesGroup


class LaunchToken(StatesGroup):
    name = State()
    symbol = State()
    description = State()
    logo = State()
    tokenomics = State()
    decimals = State()
    supply = State()
    recipient = State()
    authorities = State()
    confirm = State()
    deploying = State()
