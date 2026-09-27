"""Custom filters."""

from __future__ import annotations

from typing import Any

from aiogram.filters import Filter
from aiogram.types import CallbackQuery, Message


class HasMessage(Filter):
    """Pass only callbacks whose message is still accessible; inject it as `msg`.

    Telegram returns InaccessibleMessage for buttons on messages older than 48h.
    """

    async def __call__(self, cb: CallbackQuery) -> bool | dict[str, Any]:
        return {"msg": cb.message} if isinstance(cb.message, Message) else False
