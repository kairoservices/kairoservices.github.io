"""Inline keyboards and their typed callback payloads."""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.models import DEFAULT_DECIMALS, DEFAULT_SUPPLY

RAYDIUM_CREATE_POOL_URL = "https://raydium.io/liquidity/create-pool/"


class LaunchModeCb(CallbackData, prefix="mode"):
    mode: str  # "spl" | "pump"


class DevBuyCb(CallbackData, prefix="devbuy"):
    sol: str  # decimal string, e.g. "0.2"; "0" = no dev buy


class TokenomicsCb(CallbackData, prefix="tok"):
    choice: str  # "default" | "custom"


class AuthorityCb(CallbackData, prefix="auth"):
    action: str  # "mint" | "freeze" | "update" | "done"


class ConfirmCb(CallbackData, prefix="confirm"):
    action: str  # "deploy" | "cancel"


class DashboardCb(CallbackData, prefix="dash"):
    action: str  # "close"


def skip_kb(label: str = "Skip") -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=f"⏭ {label}", callback_data="skip")
    return kb.as_markup()


def launch_mode_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📈 pump.fun bonding curve (+ dev buy)", callback_data=LaunchModeCb(mode="pump"))
    kb.button(text="🪙 Standard SPL token (custom supply)", callback_data=LaunchModeCb(mode="spl"))
    kb.adjust(1)
    return kb.as_markup()


DEV_BUY_PRESETS = ("0.1", "0.2", "0.5", "1")


def dev_buy_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for sol in DEV_BUY_PRESETS:
        kb.button(text=f"{sol} SOL", callback_data=DevBuyCb(sol=sol))
    kb.button(text="No dev buy", callback_data=DevBuyCb(sol="0"))
    kb.adjust(len(DEV_BUY_PRESETS), 1)
    return kb.as_markup()


def tokenomics_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(
        text=f"✅ Recommended: {DEFAULT_DECIMALS} decimals, {DEFAULT_SUPPLY:,} supply",
        callback_data=TokenomicsCb(choice="default"),
    )
    kb.button(text="✏️ Custom", callback_data=TokenomicsCb(choice="custom"))
    kb.adjust(1)
    return kb.as_markup()


def authorities_kb(revoke_mint: bool, revoke_freeze: bool, revoke_update: bool) -> InlineKeyboardMarkup:
    def label(title: str, revoked: bool) -> str:
        return f"{'🔒 Revoke' if revoked else '🔑 Keep'} {title}"

    kb = InlineKeyboardBuilder()
    kb.button(text=label("Mint authority", revoke_mint), callback_data=AuthorityCb(action="mint"))
    kb.button(text=label("Freeze authority", revoke_freeze), callback_data=AuthorityCb(action="freeze"))
    kb.button(text=label("Update authority", revoke_update), callback_data=AuthorityCb(action="update"))
    kb.button(text="➡️ Continue", callback_data=AuthorityCb(action="done"))
    kb.adjust(1)
    return kb.as_markup()


def confirm_kb(retry: bool = False) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🔁 Retry deploy" if retry else "🚀 Deploy", callback_data=ConfirmCb(action="deploy"))
    kb.button(text="✖️ Cancel", callback_data=ConfirmCb(action="cancel"))
    kb.adjust(2)
    return kb.as_markup()


def _cluster_query(cluster: str) -> str:
    return "" if cluster == "mainnet-beta" else f"?cluster={cluster}"


def explorer_address_url(address: str, cluster: str) -> str:
    return f"https://explorer.solana.com/address/{address}{_cluster_query(cluster)}"


def solscan_token_url(mint: str, cluster: str) -> str:
    return f"https://solscan.io/token/{mint}{_cluster_query(cluster)}"


def solscan_tx_url(signature: str, cluster: str) -> str:
    return f"https://solscan.io/tx/{signature}{_cluster_query(cluster)}"


def success_kb(mint: str, cluster: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="💧 Create Liquidity Pool", url=RAYDIUM_CREATE_POOL_URL)
    kb.button(text="🔎 View on Explorer", url=explorer_address_url(mint, cluster))
    kb.button(text="📊 View on Solscan", url=solscan_token_url(mint, cluster))
    kb.button(text="✖️ Close", callback_data=DashboardCb(action="close"))
    kb.adjust(1, 2, 1)
    return kb.as_markup()


def pump_coin_url(mint: str) -> str:
    return f"https://pump.fun/coin/{mint}"


def dexscreener_url(mint: str) -> str:
    return f"https://dexscreener.com/solana/{mint}"


def pump_success_kb(mint: str, cluster: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📈 View on pump.fun", url=pump_coin_url(mint))
    kb.button(text="🦅 DexScreener", url=dexscreener_url(mint))
    kb.button(text="📊 Solscan", url=solscan_token_url(mint, cluster))
    kb.button(text="✖️ Close", callback_data=DashboardCb(action="close"))
    kb.adjust(1, 2, 1)
    return kb.as_markup()
