"""Buy more / sell buttons on the pump.fun launch dashboard. Trades use the bot wallet."""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from html import escape

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, LinkPreviewOptions, Message
from solders.pubkey import Pubkey

from bot.chain import pump
from bot.chain.deployer import DeploymentError, DeploymentUnconfirmed, TokenDeployer
from bot.config import LAMPORTS_PER_SOL, Config
from bot.filters import HasMessage
from bot.handlers.launch import MIN_DEV_BUY_LAMPORTS, _fmt_sol
from bot.keyboards import TradeCb, pump_success_kb, solscan_tx_url
from bot.models import PumpTradeResult
from bot.services.price import SolPriceFeed
from bot.states import TradeToken

log = logging.getLogger(__name__)
router = Router(name="trade")

# One trade at a time per user: stops double taps from selling twice.
_trading_users: set[int] = set()


def _result_text(r: PumpTradeResult, config: Config, sol_usd: float | None) -> str:
    tokens = r.token_amount / 10**pump.PUMP_DECIMALS
    if r.side == "sell":
        head = f"🔴 <b>Sold</b> {tokens:,.0f} tokens for at least {_fmt_sol(r.sol_lamports, sol_usd)}"
    else:
        head = f"🟢 <b>Bought</b> {tokens:,.0f} tokens for at most {_fmt_sol(r.sol_lamports, sol_usd)}"
    lines = [head, f"<b>Bot wallet holds:</b> {r.tokens_left / 10**pump.PUMP_DECIMALS:,.0f} tokens"]
    if r.market_cap_lamports is not None:
        lines.append(f"<b>Market cap:</b> {_fmt_sol(r.market_cap_lamports, sol_usd)}")
    if r.curve_progress is not None:
        lines.append(f"<b>Bonding curve:</b> {r.curve_progress:.2%} to graduation")
    lines.append(f"<b>Transaction:</b> <a href=\"{solscan_tx_url(r.signature, config.cluster)}\">view</a>")
    return "\n".join(lines)


async def _run_trade(
    message: Message, user_id: int, mint: str, config: Config, prices: SolPriceFeed, trade
) -> None:
    if user_id in _trading_users:
        await message.answer("⏳ A trade is already running.")
        return
    _trading_users.add(user_id)
    status = await message.answer("⏳ Sending trade…")
    try:
        result = await trade()
    except DeploymentUnconfirmed as exc:
        await status.edit_text(
            "⚠️ Trade sent but not confirmed. "
            f"<a href=\"{solscan_tx_url(str(exc.signature), config.cluster)}\">Check it</a> before trading again.",
            reply_markup=pump_success_kb(mint, config.cluster),
        )
        return
    except DeploymentError as exc:
        await status.edit_text(
            f"❌ Trade failed: {escape(str(exc))}", reply_markup=pump_success_kb(mint, config.cluster)
        )
        return
    except Exception:
        log.exception("Unexpected trade error for user %s", user_id)
        await status.edit_text("❌ Unexpected error. Check the wallet before trading again.")
        return
    finally:
        _trading_users.discard(user_id)
    await status.edit_text(
        _result_text(result, config, await prices.usd()),
        reply_markup=pump_success_kb(mint, config.cluster),
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )


@router.callback_query(HasMessage(), TradeCb.filter(F.action == "sell"))
async def on_sell(
    cb: CallbackQuery,
    msg: Message,
    callback_data: TradeCb,
    deployer: TokenDeployer,
    config: Config,
    prices: SolPriceFeed,
) -> None:
    await cb.answer(f"Selling {callback_data.pct}%…")
    mint = Pubkey.from_string(callback_data.mint)
    await _run_trade(
        msg, cb.from_user.id, callback_data.mint, config, prices,
        lambda: deployer.pump_sell(mint, callback_data.pct),
    )


@router.callback_query(HasMessage(), TradeCb.filter(F.action == "buy"))
async def on_buy_more(
    cb: CallbackQuery, msg: Message, callback_data: TradeCb, state: FSMContext, config: Config
) -> None:
    await cb.answer()
    await state.set_state(TradeToken.buy_amount)
    await state.update_data(trade_mint=callback_data.mint)
    await msg.answer(
        "🟢 <b>Buy more</b>\n\nType the SOL amount to buy, e.g. <code>0.25</code>\n"
        f"Max {config.pump_max_dev_buy_lamports / LAMPORTS_PER_SOL:g} SOL. /cancel to abort."
    )


@router.message(TradeToken.buy_amount, F.text)
async def on_buy_amount(
    message: Message, state: FSMContext, deployer: TokenDeployer, config: Config, prices: SolPriceFeed
) -> None:
    try:
        sol = Decimal(message.text.strip().lower().removesuffix("sol").strip().replace(",", "."))
        lamports = int(sol * LAMPORTS_PER_SOL) if sol.is_finite() else -1
    except InvalidOperation:
        lamports = -1
    if not MIN_DEV_BUY_LAMPORTS <= lamports <= config.pump_max_dev_buy_lamports:
        await message.answer(
            f"⚠️ Enter between {MIN_DEV_BUY_LAMPORTS / LAMPORTS_PER_SOL:g} and "
            f"{config.pump_max_dev_buy_lamports / LAMPORTS_PER_SOL:g} SOL, or /cancel."
        )
        return
    mint = (await state.get_data())["trade_mint"]
    await state.clear()
    await _run_trade(
        message, message.from_user.id, mint, config, prices,
        lambda: deployer.pump_buy(Pubkey.from_string(mint), lamports),
    )
