"""/launch conversation: gather parameters, confirm, deploy, show dashboard."""

from __future__ import annotations

import logging
from html import escape
from typing import Any
from urllib.parse import urlparse

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, LinkPreviewOptions, Message
from solders.pubkey import Pubkey

from bot.chain.deployer import DeploymentError, DeploymentUnconfirmed, TokenDeployer
from bot.config import Config
from bot.filters import HasMessage
from bot.keyboards import (
    AuthorityCb,
    ConfirmCb,
    TokenomicsCb,
    authorities_kb,
    confirm_kb,
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
    TokenParams,
)
from bot.services.storage import PinataStorage, StorageError, build_offchain_metadata
from bot.states import LaunchToken

log = logging.getLogger(__name__)
router = Router(name="launch")

MAX_LOGO_BYTES = 5 * 1024 * 1024
MAX_DESCRIPTION_LEN = 1000

# Users with a deployment currently running; guards against double-clicks
# that could otherwise mint two tokens before the FSM state updates.
_deploying_users: set[int] = set()


# --------------------------------------------------------------------------- helpers


def _utf8_len(text: str) -> int:
    return len(text.encode("utf-8"))


def _parse_int(text: str) -> int | None:
    cleaned = text.strip().replace(",", "").replace("_", "").replace(" ", "")
    return int(cleaned) if cleaned.isdigit() else None


def _summary(data: dict[str, Any]) -> str:
    def auth(revoked: bool) -> str:
        return "🔒 revoked" if revoked else "🔑 kept (your wallet)"

    if data.get("logo_file_id"):
        logo = "uploaded image"
    elif data.get("logo_url"):
        logo = escape(data["logo_url"])
    else:
        logo = "none"

    return (
        "📋 <b>Token summary</b>\n\n"
        f"<b>Name:</b> {escape(data['name'])}\n"
        f"<b>Symbol:</b> {escape(data['symbol'])}\n"
        f"<b>Description:</b> {escape(data.get('description') or '—')}\n"
        f"<b>Logo:</b> {logo}\n"
        f"<b>Decimals:</b> {data['decimals']}\n"
        f"<b>Total supply:</b> {data['supply']:,}\n"
        f"<b>Recipient:</b> <code>{data['recipient']}</code>\n\n"
        f"<b>Mint authority:</b> {auth(data['revoke_mint'])}\n"
        f"<b>Freeze authority:</b> {auth(data['revoke_freeze'])}\n"
        f"<b>Update authority:</b> {auth(data['revoke_update'])}"
    )


async def _ask_recipient(message: Message, state: FSMContext) -> None:
    await state.set_state(LaunchToken.recipient)
    await message.answer(
        "👛 <b>Step 6/7 — Recipient wallet</b>\n\n"
        "Send the Solana address that should receive the full supply. "
        "Any authority you choose to keep is also transferred to this wallet."
    )


async def _ask_authorities(message: Message, state: FSMContext) -> None:
    await state.set_state(LaunchToken.authorities)
    data = await state.get_data()
    await message.answer(
        "🛡 <b>Step 7/7 — Authorities</b>\n\n"
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


# --------------------------------------------------------------------------- flow


@router.message(Command("launch"))
async def cmd_launch(message: Message, state: FSMContext) -> None:
    if await state.get_state() == LaunchToken.deploying.state:
        await message.answer("⏳ A deployment is already running.")
        return
    await state.clear()
    # Recommended defaults: fixed supply, no freeze, metadata still editable.
    await state.update_data(revoke_mint=True, revoke_freeze=True, revoke_update=False)
    await state.set_state(LaunchToken.name)
    await message.answer(
        "🚀 <b>Launch a new token</b>\n\n"
        f"<b>Step 1/7 — Name</b>\nSend the token name (max {MAX_NAME_LEN} bytes).\n\n"
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
        f"🔤 <b>Step 2/7 — Symbol</b>\nSend the ticker (max {MAX_SYMBOL_LEN} chars, e.g. <code>MOON</code>)."
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
        "📝 <b>Step 3/7 — Description</b>\nSend a short description, or skip.",
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
        "🖼 <b>Step 4/7 — Logo</b>\n"
        "Upload an image (send as <i>file</i> to keep full quality), paste an image URL, or skip.\n"
        "Recommended: square PNG, 512×512 or larger.",
        reply_markup=skip_kb(),
    )


@router.message(LaunchToken.logo, F.photo)
async def on_logo_photo(message: Message, state: FSMContext) -> None:
    photo = message.photo[-1]  # largest size
    if (photo.file_size or 0) > MAX_LOGO_BYTES:
        await message.answer("⚠️ Image too large (max 5 MB).")
        return
    await state.update_data(logo_file_id=photo.file_id, logo_mime="image/jpeg", logo_url=None)
    await _ask_tokenomics(message, state)


@router.message(LaunchToken.logo, F.document)
async def on_logo_document(message: Message, state: FSMContext) -> None:
    doc = message.document
    if not (doc.mime_type or "").startswith("image/"):
        await message.answer("⚠️ That file isn't an image. Send PNG, JPG, GIF, WEBP or SVG.")
        return
    if (doc.file_size or 0) > MAX_LOGO_BYTES:
        await message.answer("⚠️ Image too large (max 5 MB).")
        return
    await state.update_data(logo_file_id=doc.file_id, logo_mime=doc.mime_type, logo_url=None)
    await _ask_tokenomics(message, state)


@router.message(LaunchToken.logo, F.text)
async def on_logo_url(message: Message, state: FSMContext) -> None:
    url = message.text.strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        await message.answer("⚠️ Send a valid http(s) image URL, upload an image, or tap Skip.")
        return
    await state.update_data(logo_url=url, logo_file_id=None, logo_mime=None)
    await _ask_tokenomics(message, state)


@router.callback_query(HasMessage(), LaunchToken.logo, F.data == "skip")
async def on_logo_skip(cb: CallbackQuery, msg: Message, state: FSMContext) -> None:
    await cb.answer()
    await state.update_data(logo_url=None, logo_file_id=None, logo_mime=None)
    await msg.edit_reply_markup(reply_markup=None)
    await _ask_tokenomics(msg, state)


async def _ask_tokenomics(message: Message, state: FSMContext) -> None:
    await state.set_state(LaunchToken.tokenomics)
    await message.answer(
        "📈 <b>Step 5/7 — Tokenomics</b>\nUse recommended values or set your own.",
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
async def on_recipient(message: Message, state: FSMContext) -> None:
    try:
        recipient = Pubkey.from_string(message.text.strip())
    except ValueError:
        await message.answer("⚠️ That isn't a valid Solana address. Try again.")
        return
    await state.update_data(recipient=str(recipient))
    await _ask_authorities(message, state)


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
            await msg.edit_text(
                f"{summary}\n\n⚠️ <b>Transaction sent but not confirmed</b> (RPC unreachable).\n"
                f"Mint: <code>{exc.mint}</code>\n"
                f"<a href=\"{solscan_tx_url(str(exc.signature), config.cluster)}\">Check the transaction</a> "
                "before launching again, or you may create a duplicate token.",
                reply_markup=success_kb(str(exc.mint), config.cluster),
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
        mint = str(result.mint)
        await msg.edit_text(
            "🎉 <b>Your token has been created!</b>\n\n"
            f"<b>{escape(data['name'])}</b> (${escape(data['symbol'])}){network}\n\n"
            f"<b>Mint address:</b>\n<code>{mint}</code>\n\n"
            f"<b>Supply:</b> {data['supply']:,} sent to <code>{data['recipient']}</code>\n"
            f"<b>Transaction:</b> <a href=\"{solscan_tx_url(result.signature, config.cluster)}\">view</a>",
            reply_markup=success_kb(mint, config.cluster),
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
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
)
async def on_unexpected_input(message: Message) -> None:
    await message.answer("⚠️ Unexpected input for this step. Please follow the prompt, or /cancel.")


@router.callback_query()
async def on_stale_button(cb: CallbackQuery) -> None:
    """Buttons from finished/cancelled flows or messages Telegram no longer exposes."""
    await cb.answer("This button is no longer active. Send /launch to start again.", show_alert=True)
