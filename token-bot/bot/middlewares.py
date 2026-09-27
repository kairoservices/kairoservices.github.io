"""Access control: only allow-listed Telegram users may spend the payer wallet's SOL."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject, User


class AllowlistMiddleware(BaseMiddleware):
    def __init__(self, allowed_user_ids: frozenset[int]) -> None:
        self._allowed = allowed_user_ids

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user: User | None = data.get("event_from_user")
        if user is not None and user.id in self._allowed:
            return await handler(event, data)

        if isinstance(event, Message):
            await event.answer("⛔ You are not authorized to use this bot.")
        elif isinstance(event, CallbackQuery):
            await event.answer("⛔ Not authorized.", show_alert=True)
        return None
