"""/launch conversation: gather parameters, confirm, deploy, show dashboard."""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from html import escape
from typing import Any
from urllib.parse import urlparse

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, LinkPreviewOptions, Message
from solders.pubkey import Pubkey

from bot.chain import pump
from bot.chain.deployer import DeploymentError, DeploymentUnconfirmed, TokenDeployer
from bot.config import LAMPORTS_PER_SOL, Config
from bot.filters import HasMessage
from bot.keyboards import (
    AuthorityCb,
    ConfirmCb,
    DevBuyCb,
    LaunchModeCb,
    TokenomicsCb,
    authorities_kb,
    confirm_kb,
    dev_buy_kb,
    launch_mode_kb,
    pump_success_kb,
    skip_kb,
    solscan_tx_url,
    success_kb,
    tokenomics_kb,
)
from bot.models import (
    DEFAULT_DECIMALS,
    DEFAULT_SUPPLY,
    MAX_DECIMALS,
    MAX_NAME_LEN,
    MAX_SYMBOL_LEN,
    MAX_URI_LEN,
    U64_MAX,
    PumpLaunchParams,
    TokenParams,
)
from bot.services.price import SolPriceFeed
from bot.services.storage import PinataStorage, StorageError, build_offchain_metadata
from bot.states import LaunchToken

log = logging.getLogger(__name__)
router = Router(name="launch")

MAX_LOGO_BYTES = 5 * 1024 * 1024
MAX_DESCRIPTION_LEN = 1000

MIN_DEV_BUY_LAMPORTS = 1_000_000  # 0.001 SOL

# Users with a deployment currently running; guards against double-clicks
# that could otherwise mint two tokens before the FSM state updates.
_deploying_users: set[int] = set()


# --------------------------------------------------------------------------- helpers


def _utf8_len(text: str) -> int:
    return len(text.encode("utf-8"))


def _parse_int(text: str) -> int | None:
    cleaned = text.strip().replace(",", "").replace("_", "").replace(" ", "")
    return int(cleaned) if cleaned.isdigit() else None


def _fmt_sol(lamports: int, sol_usd: float | None = None) -> str:
    text = f"{lamports / LAMPORTS_PER_SOL:,.3f} SOL"
    if sol_usd:
        text += f" (~${lamports / LAMPORTS_PER_SOL * sol_usd:,.0f})"
    return text


def _is_pump(data: dict[str, Any]) -> bool:
    return data.get("mode") == "pump"


def _summary(data: dict[str, Any]) -> str:
    def auth(revoked: bool) -> str:
        return "🔒 revoked" if revoked else "🔑 kept (your wallet)"

    if data.get("logo_file_id"):
        logo = "uploaded image"
    elif data.get("logo_url"):
        logo = escape(data["logo_url"])
    else:
        logo = "none"

    head = (
        "📋 <b>Token summary</b>\n\n"
        f"<b>Name:</b> {escape(data['name'])}\n"
        f"<b>Symbol:</b> {escape(data['symbol'])}\n"
        f"<b>Description:</b> {escape(data.get('description') or '—')}\n"
        f"<b>Logo:</b> {logo}\n"
    )
    if _is_pump(data):
        sol_usd = data.get("sol_usd")
        text = (
            head + "<b>Launch:</b> pump.fun bonding curve\n"
            f"<b>Supply:</b> {pump.PUMP_TOTAL_SUPPLY:,} ({pump.PUMP_DECIMALS} decimals, fixed)\n"
            "<b>Mint/freeze authority:</b> revoked by pump.fun\n"
            f"<b>Creator wallet:</b> <code>{data['recipient']}</code>\n\n"
        )
        if data.get("initial_mcap"):
            text += f"<b>Starting market cap:</b> {_fmt_sol(data['initial_mcap'], sol_usd)}\n"
        if data["dev_buy_lamports"]:
            text += f"<b>Dev buy:</b> {_fmt_sol(data['dev_buy_lamports'], sol_usd)} max, incl. fees\n"
            if data.get("est_tokens"):
                share = data["est_tokens"] / (pump.PUMP_TOTAL_SUPPLY * 10**pump.PUMP_DECIMALS)
                text += (
                    f"  ≈ {data['est_tokens'] / 10**pump.PUMP_DECIMALS:,.0f} tokens ({share:.2%} of supply)\n"
                    f"  Market cap after buy ≈ {_fmt_sol(data['est_mcap'], sol_usd)}\n"
                )
        else:
            text += "<b>Dev buy:</b> none\n"
        return text.rstrip("\n")

    return head + (
        f"<b>Decimals:</b> {data['decimals']}\n"
        f"<b>Total supply:</b> {data['supply']:,}\n"
        f"<b>Recipient:</b> <code>{data['recipient']}</code>\n\n"
        f"<b>Mint authority:</b> {auth(data['revoke_mint'])}\n"
        f"<b>Freeze authority:</b> {auth(data['revoke_freeze'])}\n"
        f"<b>Update authority:</b> {auth(data['revoke_update'])}"
    )


async def _ask_recipient(message: Message, state: FSMContext) -> None:
    await state.set_state(LaunchToken.recipient)
    if _is_pump(await state.get_data()):
        await message.answer(
            "👛 <b>Step 6 — Creator wallet</b>\n\n"
            "Send the Solana address to register as the coin's creator. "
            "It earns pump.fun creator fees and receives the dev-buy tokens."
        )
        return
    await message.answer(
        "👛 <b>Step 6 — Recipient wallet</b>\n\n"
        "Send the Solana address that should receive the full supply. "
        "Any authority you choose to keep is also transferred to this wallet."
    )


async def _ask_dev_buy(message: Message, state: FSMContext, config: Config) -> None:
    await state.set_state(LaunchToken.dev_buy)
    await message.answer(
        "💸 <b>Step 7 — Dev buy</b>\n\n"
        "Enter initial dev buy amount in SOL (e.g., 0.2 SOL to seed the chart):\n\n"
        f"Executes in the same transaction as the launch, from the bot wallet. "
        f"Fees included. Max {config.pump_max_dev_buy_lamports / LAMPORTS_PER_SOL:g} SOL, 0 to skip.\n"
        "The buy is public on-chain and shows as the creator's holding.",
        reply_markup=dev_buy_kb(),
    )


async def _set_dev_buy(
    message: Message,
    state: FSMContext,
    raw: str,
    *,
    config: Config,
    deployer: TokenDeployer,
    prices: SolPriceFeed,
) -> None:
    try:
        sol = Decimal(raw.strip().lower().removesuffix("sol").strip().replace(",", "."))
    except InvalidOperation:
        sol = Decimal(-1)
    lamports = int(sol * LAMPORTS_PER_SOL) if sol.is_finite() else -1
    if lamports < 0 or lamports > config.pump_max_dev_buy_lamports or 0 < lamports < MIN_DEV_BUY_LAMPORTS:
        await message.answer(
            f"⚠️ Enter 0, or between {MIN_DEV_BUY_LAMPORTS / LAMPORTS_PER_SOL:g} and "
            f"{config.pump_max_dev_buy_lamports / LAMPORTS_PER_SOL:g} SOL."
        )
        return

    update: dict[str, Any] = {"dev_buy_lamports": lamports, "sol_usd": await prices.usd()}
    try:
        g = await deployer.fetch_pump_global()
        update["initial_mcap"] = g.initial_market_cap_lamports
        if lamports:
            tokens = pump.quote_first_buy(g, lamports, config.pump_fee_bps)
            update["est_tokens"] = tokens
            update["est_mcap"] = pump.market_cap_after_first_buy(g, tokens)
    except DeploymentError as exc:
        log.warning("Could not quote dev buy: %s", exc)  # summary just omits the estimate
    await state.update_data(update)
    await _show_confirm(message, state)


async def _ask_authorities(message: Message, state: FSMContext) -> None:
    await state.set_state(LaunchToken.authorities)
    data = await state.get_data()
    await message.answer(
        "🛡 <b>Step 7 — Authorities</b>\n\n"
        "Tap to toggle. Revoking is permanent.\n"
        "• <b>Mint</b>: revoke = fixed supply (recommended)\n"
        "• <b>Freeze</b>: revoke = holders can't be frozen (required by most DEX listings)\n"
        "• <b>Update</b>: revoke = name/symbol/logo locked forever",
        reply_markup=authorities_kb(data["revoke_mint"], data["revoke_freeze"], data["revoke_update"]),
    )


async def _show_confirm(message: Message, state: FSMContext, *, edit: bool = False) -> None:
    await state.set_state(LaunchToken.confirm)
    text = _summary(await state.get_data()) + "\n\nDeploy this token?"
    if edit:
        await message.edit_text(text, reply_markup=confirm_kb())
    else:
        await message.answer(text, reply_markup=confirm_kb())


async def _upload_metadata(data: dict[str, Any], bot: Bot, storage: PinataStorage) -> str:
    """Upload logo (if a file) and the off-chain JSON; return the JSON URI."""
    image_uri: str | None = data.get("logo_url")
    image_mime: str | None = data.get("logo_mime")

    if data.get("logo_file_id"):
        buffer = await bot.download(data["logo_file_id"])
        if buffer is None:
            raise StorageError("Could not download the logo from Telegram")
        ext = (image_mime or "image/png").split("/")[-1]
        image_uri = await storage.upload_file(
            buffer.read(), filename=f"{data['symbol']}.{ext}", content_type=image_mime or "image/png"
        )

    content = build_offchain_metadata(
        name=data["name"],
        symbol=data["symbol"],
        description=data.get("description") or "",
        image_uri=image_uri,
        image_mime=image_mime,
    )
    uri = await storage.upload_json(content, name=f"{data['symbol']}-metadata.json")
    if _utf8_len(uri) > MAX_URI_LEN:
        raise StorageError(f"Metadata URI exceeds {MAX_URI_LEN} bytes; use a shorter gateway URL")
    return uri


async def _deploy_spl(
    data: dict[str, Any], uri: str, deployer: TokenDeployer, config: Config, network: str
) -> tuple[str, InlineKeyboardMarkup]:
    params = TokenParams(
        name=data["name"],
        symbol=data["symbol"],
        metadata_uri=uri,
        decimals=data["decimals"],
        supply=data["supply"],
        recipient=Pubkey.from_string(data["recipient"]),
        revoke_mint=data["revoke_mint"],
        revoke_freeze=data["revoke_freeze"],
        revoke_update=data["revoke_update"],
    )
    result = await deployer.deploy(params)
    mint = str(result.mint)
    text = (
        "🎉 <b>Your token has been created!</b>\n\n"
        f"<b>{escape(data['name'])}</b> (${escape(data['symbol'])}){network}\n\n"
        f"<b>Mint address:</b>\n<code>{mint}</code>\n\n"
        f"<b>Supply:</b> {data['supply']:,} sent to <code>{data['recipient']}</code>\n"
        f"<b>Transaction:</b> <a href=\"{solscan_tx_url(result.signature, config.cluster)}\">view</a>"
    )
    return text, success_kb(mint, config.cluster)


async def _deploy_pump(
    data: dict[str, Any],
    uri: str,
    deployer: TokenDeployer,
    prices: SolPriceFeed,
    config: Config,
    network: str,
) -> tuple[str, InlineKeyboardMarkup]:
    params = PumpLaunchParams(
        name=data["name"],
        symbol=data["symbol"],
        metadata_uri=uri,
        recipient=Pubkey.from_string(data["recipient"]),
        dev_buy_lamports=data["dev_buy_lamports"],
    )
    result = await deployer.deploy_pump(params)
    mint = str(result.mint)
    sol_usd = await prices.usd()
    lines = [
        "🎉 <b>Your token has been created!</b>\n",
        f"<b>{escape(data['name'])}</b> (${escape(data['symbol'])}) on pump.fun{network}\n",
        f"<b>Mint address:</b>\n<code>{mint}</code>\n",
    ]
    if result.dev_tokens:
        share = result.dev_tokens / (pump.PUMP_TOTAL_SUPPLY * 10**pump.PUMP_DECIMALS)
        lines.append(
            f"<b>Dev buy:</b> {result.dev_tokens / 10**pump.PUMP_DECIMALS:,.0f} tokens ({share:.2%}) "
            f"sent to <code>{data['recipient']}</code>"
        )
    if result.market_cap_lamports is not None:
        lines.append(f"<b>Market cap:</b> {_fmt_sol(result.market_cap_lamports, sol_usd)}")
    else:
        lines.append(f"<b>Starting market cap:</b> {_fmt_sol(result.initial_market_cap_lamports, sol_usd)}")
    if result.curve_progress is not None:
        lines.append(f"<b>Bonding curve:</b> {result.curve_progress:.2%} to graduation")
    lines.append(f"<b>Transaction:</b> <a href=\"{solscan_tx_url(result.signature, config.cluster)}\">view</a>")
    return "\n".join(lines), pump_success_kb(mint, config.cluster)


# --------------------------------------------------------------------------- flow


@router.message(Command("launch"))
async def cmd_launch(message: Message, state: FSMContext) -> None:
    if await state.get_state() == LaunchToken.deploying.state:
        await message.answer("⏳ A deployment is already running.")
        return
    await state.clear()
    # Default: revoke all three (fixed supply, no freeze, locked metadata).
    await state.update_data(revoke_mint=True, revoke_freeze=True, revoke_update=True)
    await state.set_state(LaunchToken.name)
    await message.answer(
        "🚀 <b>Launch a new token</b>\n\n"
        f"<b>Step 1 — Name</b>\nSend the token name (max {MAX_NAME_LEN} bytes).\n\n"
        "Send /cancel at any time to abort."
    )


@router.message(LaunchToken.name, F.text)
async def on_name(message: Message, state: FSMContext) -> None:
    name = message.text.strip()
    if not name or _utf8_len(name) > MAX_NAME_LEN:
        await message.answer(f"⚠️ Name must be 1–{MAX_NAME_LEN} bytes. Try again.")
        return
    await state.update_data(name=name)
    await state.set_state(LaunchToken.symbol)
    await message.answer(
        f"🔤 <b>Step 2 — Symbol</b>\nSend the ticker (max {MAX_SYMBOL_LEN} chars, e.g. <code>MOON</code>)."
    )


@router.message(LaunchToken.symbol, F.text)
async def on_symbol(message: Message, state: FSMContext) -> None:
    symbol = message.text.strip().lstrip("$").upper()
    if not symbol or " " in symbol or _utf8_len(symbol) > MAX_SYMBOL_LEN:
        await message.answer(f"⚠️ Symbol must be 1–{MAX_SYMBOL_LEN} bytes with no spaces. Try again.")
        return
    await state.update_data(symbol=symbol)
    await state.set_state(LaunchToken.description)
    await message.answer(
        "📝 <b>Step 3 — Description</b>\nSend a short description, or skip.",
        reply_markup=skip_kb(),
    )


@router.message(LaunchToken.description, F.text)
async def on_description(message: Message, state: FSMContext) -> None:
    description = message.text.strip()
    if len(description) > MAX_DESCRIPTION_LEN:
        await message.answer(f"⚠️ Keep it under {MAX_DESCRIPTION_LEN} characters.")
        return
    await state.update_data(description=description)
    await _ask_logo(message, state)


@router.callback_query(HasMessage(), LaunchToken.description, F.data == "skip")
async def on_description_skip(cb: CallbackQuery, msg: Message, state: FSMContext) -> None:
    await cb.answer()
    await state.update_data(description="")
    await msg.edit_reply_markup(reply_markup=None)
    await _ask_logo(msg, state)


async def _ask_logo(message: Message, state: FSMContext) -> None:
    await state.set_state(LaunchToken.logo)
    await message.answer(
        "🖼 <b>Step 4 — Logo</b>\n"
        "Upload an image (send as <i>file</i> to keep full quality), paste an image URL, or skip.\n"
        "Recommended: square PNG, 512×512 or larger.",
        reply_markup=skip_kb(),
    )


@router.message(LaunchToken.logo, F.photo)
async def on_logo_photo(message: Message, state: FSMContext, config: Config) -> None:
    photo = message.photo[-1]  # largest size
    if (photo.file_size or 0) > MAX_LOGO_BYTES:
        await message.answer("⚠️ Image too large (max 5 MB).")
        return
    await state.update_data(logo_file_id=photo.file_id, logo_mime="image/jpeg", logo_url=None)
    await _ask_mode(message, state, config)


@router.message(LaunchToken.logo, F.document)
async def on_logo_document(message: Message, state: FSMContext, config: Config) -> None:
    doc = message.document
    if not (doc.mime_type or "").startswith("image/"):
        await message.answer("⚠️ That file isn't an image. Send PNG, JPG, GIF, WEBP or SVG.")
        return
    if (doc.file_size or 0) > MAX_LOGO_BYTES:
        await message.answer("⚠️ Image too large (max 5 MB).")
        return
    await state.update_data(logo_file_id=doc.file_id, logo_mime=doc.mime_type, logo_url=None)
    await _ask_mode(message, state, config)


@router.message(LaunchToken.logo, F.text)
async def on_logo_url(message: Message, state: FSMContext, config: Config) -> None:
    url = message.text.strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        await message.answer("⚠️ Send a valid http(s) image URL, upload an image, or tap Skip.")
        return
    await state.update_data(logo_url=url, logo_file_id=None, logo_mime=None)
    await _ask_mode(message, state, config)


@router.callback_query(HasMessage(), LaunchToken.logo, F.data == "skip")
async def on_logo_skip(cb: CallbackQuery, msg: Message, state: FSMContext, config: Config) -> None:
    await cb.answer()
    await state.update_data(logo_url=None, logo_file_id=None, logo_mime=None)
    await msg.edit_reply_markup(reply_markup=None)
    await _ask_mode(msg, state, config)


async def _ask_mode(message: Message, state: FSMContext, config: Config) -> None:
    if not config.pump_enabled:
        await state.update_data(mode="spl")
        await _ask_tokenomics(message, state)
        return
    await state.set_state(LaunchToken.mode)
    await message.answer(
        "🧭 <b>Step 5 — Launch type</b>\n\n"
        "<b>pump.fun bonding curve</b>: tradable immediately, starts at the curve's base market cap, "
        "optional dev buy in the same transaction. Supply fixed at 1B (6 decimals).\n\n"
        "<b>Standard SPL</b>: custom supply and authorities. No price until you add a liquidity pool.",
        reply_markup=launch_mode_kb(),
    )


@router.callback_query(HasMessage(), LaunchToken.mode, LaunchModeCb.filter())
async def on_mode(cb: CallbackQuery, msg: Message, callback_data: LaunchModeCb, state: FSMContext) -> None:
    await cb.answer()
    await msg.edit_reply_markup(reply_markup=None)
    if callback_data.mode == "pump":
        await state.update_data(mode="pump")
        await _ask_recipient(msg, state)
    else:
        await state.update_data(mode="spl")
        await _ask_tokenomics(msg, state)


async def _ask_tokenomics(message: Message, state: FSMContext) -> None:
    await state.set_state(LaunchToken.tokenomics)
    await message.answer(
        "📈 <b>Step 5 — Tokenomics</b>\nUse recommended values or set your own.",
        reply_markup=tokenomics_kb(),
    )


@router.callback_query(HasMessage(), LaunchToken.tokenomics, TokenomicsCb.filter())
async def on_tokenomics(cb: CallbackQuery, msg: Message, callback_data: TokenomicsCb, state: FSMContext) -> None:
    await cb.answer()
    await msg.edit_reply_markup(reply_markup=None)
    if callback_data.choice == "default":
        await state.update_data(decimals=DEFAULT_DECIMALS, supply=DEFAULT_SUPPLY)
        await _ask_recipient(msg, state)
    else:
        await state.set_state(LaunchToken.decimals)
        await msg.answer(f"🔢 Send <b>decimals</b> (0–{MAX_DECIMALS}). Default: {DEFAULT_DECIMALS}.")


@router.message(LaunchToken.decimals, F.text)
async def on_decimals(message: Message, state: FSMContext) -> None:
    decimals = _parse_int(message.text)
    if decimals is None or decimals > MAX_DECIMALS:
        await message.answer(f"⚠️ Decimals must be an integer from 0 to {MAX_DECIMALS}.")
        return
    await state.update_data(decimals=decimals)
    await state.set_state(LaunchToken.supply)
    await message.answer(f"💰 Send <b>total supply</b> in whole tokens. Default: {DEFAULT_SUPPLY:,}.")


@router.message(LaunchToken.supply, F.text)
async def on_supply(message: Message, state: FSMContext) -> None:
    supply = _parse_int(message.text)
    decimals = (await state.get_data())["decimals"]
    if not supply:
        await message.answer("⚠️ Supply must be a positive whole number.")
        return
    if supply * 10**decimals > U64_MAX:
        max_supply = U64_MAX // 10**decimals
        await message.answer(f"⚠️ With {decimals} decimals the maximum supply is {max_supply:,}.")
        return
    await state.update_data(supply=supply)
    await _ask_recipient(message, state)


@router.message(LaunchToken.recipient, F.text)
async def on_recipient(message: Message, state: FSMContext, config: Config) -> None:
    try:
        recipient = Pubkey.from_string(message.text.strip())
    except ValueError:
        await message.answer("⚠️ That isn't a valid Solana address. Try again.")
        return
    await state.update_data(recipient=str(recipient))
    if _is_pump(await state.get_data()):
        await _ask_dev_buy(message, state, config)
    else:
        await _ask_authorities(message, state)


@router.message(LaunchToken.dev_buy, F.text)
async def on_dev_buy_text(
    message: Message, state: FSMContext, config: Config, deployer: TokenDeployer, prices: SolPriceFeed
) -> None:
    await _set_dev_buy(message, state, message.text, config=config, deployer=deployer, prices=prices)


@router.callback_query(HasMessage(), LaunchToken.dev_buy, DevBuyCb.filter())
async def on_dev_buy_preset(
    cb: CallbackQuery,
    msg: Message,
    callback_data: DevBuyCb,
    state: FSMContext,
    config: Config,
    deployer: TokenDeployer,
    prices: SolPriceFeed,
) -> None:
    await cb.answer()
    await msg.edit_reply_markup(reply_markup=None)
    if callback_data.sol == "custom":
        await msg.answer("✏️ Type the exact SOL amount for the dev buy, e.g. <code>0.37</code>")
        return
    await _set_dev_buy(msg, state, callback_data.sol, config=config, deployer=deployer, prices=prices)


@router.callback_query(HasMessage(), LaunchToken.authorities, AuthorityCb.filter())
async def on_authority(cb: CallbackQuery, msg: Message, callback_data: AuthorityCb, state: FSMContext) -> None:
    await cb.answer()
    if callback_data.action == "done":
        await _show_confirm(msg, state, edit=True)
        return
    key = f"revoke_{callback_data.action}"
    data = await state.get_data()
    data[key] = not data[key]
    await state.update_data({key: data[key]})
    await msg.edit_reply_markup(
        reply_markup=authorities_kb(data["revoke_mint"], data["revoke_freeze"], data["revoke_update"])
    )


@router.callback_query(HasMessage(), LaunchToken.confirm, ConfirmCb.filter(F.action == "cancel"))
async def on_confirm_cancel(cb: CallbackQuery, msg: Message, state: FSMContext) -> None:
    await cb.answer()
    await state.clear()
    await msg.edit_text("✖️ Cancelled. Send /launch to start again.")


@router.callback_query(HasMessage(), LaunchToken.confirm, ConfirmCb.filter(F.action == "deploy"))
async def on_confirm_deploy(
    cb: CallbackQuery,
    msg: Message,
    state: FSMContext,
    bot: Bot,
    deployer: TokenDeployer,
    ipfs: PinataStorage,
    prices: SolPriceFeed,
    config: Config,
) -> None:
    user_id = cb.from_user.id
    if user_id in _deploying_users:
        await cb.answer("⏳ Already deploying…")
        return
    _deploying_users.add(user_id)
    await cb.answer()
    try:
        await state.set_state(LaunchToken.deploying)
        data = await state.get_data()
        summary = _summary(data)
        network = "" if config.is_mainnet else f" ({config.cluster})"

        try:
            # Reuse the metadata URI from a previous failed attempt, if any.
            uri = data.get("metadata_uri")
            if not uri:
                await msg.edit_text(f"{summary}\n\n⏳ Uploading logo and metadata to IPFS…")
                uri = await _upload_metadata(data, bot, ipfs)
                await state.update_data(metadata_uri=uri)

            await msg.edit_text(f"{summary}\n\n⏳ Deploying on Solana{network}…")
            if _is_pump(data):
                text, markup = await _deploy_pump(data, uri, deployer, prices, config, network)
            else:
                text, markup = await _deploy_spl(data, uri, deployer, config, network)

        except (StorageError, DeploymentError) as exc:
            # Nothing landed on-chain: let the user retry with the same parameters.
            log.warning("Deploy failed for user %s: %s", user_id, exc)
            await state.set_state(LaunchToken.confirm)
            await msg.edit_text(
                f"{summary}\n\n❌ <b>Deployment failed</b>\n{escape(str(exc))}",
                reply_markup=confirm_kb(retry=True),
            )
            return

        except DeploymentUnconfirmed as exc:
            # Possibly landed: never offer a blind retry.
            log.error("Unconfirmed deploy for user %s: mint %s tx %s", user_id, exc.mint, exc.signature)
            await state.clear()
            links = pump_success_kb if _is_pump(data) else success_kb
            await msg.edit_text(
                f"{summary}\n\n⚠️ <b>Transaction sent but not confirmed</b> (RPC unreachable).\n"
                f"Mint: <code>{exc.mint}</code>\n"
                f"<a href=\"{solscan_tx_url(str(exc.signature), config.cluster)}\">Check the transaction</a> "
                "before launching again, or you may create a duplicate token.",
                reply_markup=links(str(exc.mint), config.cluster),
            )
            return

        except Exception:
            log.exception("Unexpected deploy error for user %s", user_id)
            await state.clear()
            await msg.edit_text(
                f"{summary}\n\n❌ <b>Unexpected error.</b> Check your wallet before trying /launch again."
            )
            return

        await state.clear()
        await msg.edit_text(text, reply_markup=markup, link_preview_options=LinkPreviewOptions(is_disabled=True))
    finally:
        _deploying_users.discard(user_id)


# Catch-all for unexpected input while a step is waiting (e.g. a sticker).
@router.message(LaunchToken.deploying)
async def on_busy(message: Message) -> None:
    await message.answer("⏳ Deployment in progress, please wait…")


@router.message(
    LaunchToken.name,
    LaunchToken.symbol,
    LaunchToken.description,
    LaunchToken.logo,
    LaunchToken.decimals,
    LaunchToken.supply,
    LaunchToken.recipient,
    LaunchToken.dev_buy,
)
async def on_unexpected_input(message: Message) -> None:
    await message.answer("⚠️ Unexpected input for this step. Please follow the prompt, or /cancel.")


@router.callback_query()
async def on_stale_button(cb: CallbackQuery) -> None:
    """Buttons from finished/cancelled flows or messages Telegram no longer exposes."""
    await cb.answer("This button is no longer active. Send /launch to start again.", show_alert=True)
