"""Global commands: /start, /help, /cancel, and dashboard dismissal."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.keyboards import DashboardCb
from bot.states import LaunchToken

router = Router(name="common")

HELP_TEXT = (
    "🪙 <b>Token Deployment Assistant</b>\n\n"
    "I create an SPL token on Solana with Metaplex metadata, step by step.\n\n"
    "/launch — create a new token\n"
    "/cancel — abort the current flow\n"
    "/help — show this message"
)


@router.message(CommandStart())
@router.message(Command("help"))
async def cmd_start(message: Message) -> None:
    await message.answer(HELP_TEXT)


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    if await state.get_state() == LaunchToken.deploying.state:
        await message.answer("⏳ Deployment in progress; it cannot be cancelled now.")
        return
    await state.clear()
    await message.answer("✖️ Cancelled. Send /launch to start again.")


@router.callback_query(DashboardCb.filter(F.action == "close"))
async def on_dashboard_close(cb: CallbackQuery) -> None:
    await cb.answer()
    if isinstance(cb.message, Message):
        try:
            await cb.message.delete()
        except TelegramBadRequest:
            # Messages older than 48h can't be deleted; strip the keyboard instead.
            await cb.message.edit_reply_markup(reply_markup=None)
