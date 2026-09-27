from aiogram.fsm.state import State, StatesGroup


class LaunchToken(StatesGroup):
    name = State()
    symbol = State()
    description = State()
    logo = State()
    mode = State()
    tokenomics = State()
    decimals = State()
    supply = State()
    recipient = State()
    dev_buy = State()
    authorities = State()
    confirm = State()
    deploying = State()


class TradeToken(StatesGroup):
    buy_amount = State()
