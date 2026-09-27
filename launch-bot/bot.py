"""
Solana launch-management Telegram bot.

Admin-only Telegram bot that drives a token launch lifecycle on Solana:

    1. Create an SPL token (mint + supply) with Metaplex metadata.
    2. Create a Raydium CPMM pool (TOKEN/SOL) and seed initial liquidity.
    3. Inspect, withdraw, or permanently burn the resulting LP position.

Every state-changing action is two-step: the bot previews exactly what it will
sign, and nothing is sent until an authorised admin taps "Confirm".

Targets: aiogram 3.x, solana-py 0.40.x, solders 0.2x. Run on devnet first.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import inspect
import json
import logging
import os
import secrets
import struct
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Awaitable, Callable, Sequence

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    ErrorEvent,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
)
from dotenv import load_dotenv
from solana.exceptions import SolanaRpcException
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solana.rpc.core import RPCException
from solana.rpc.models import TxOpts
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.system_program import (
    CreateAccountParams,
    TransferParams,
    create_account,
    transfer,
)
from solders.transaction import VersionedTransaction
from solders.transaction_status import TransactionConfirmationStatus
from spl.token.constants import (
    ASSOCIATED_TOKEN_PROGRAM_ID,
    MINT_LEN,
    TOKEN_2022_PROGRAM_ID,
    TOKEN_PROGRAM_ID,
    WRAPPED_SOL_MINT,
)
from spl.token.instructions import (
    burn,
    close_account,
    create_idempotent_associated_token_account,
    get_associated_token_address,
    initialize_mint,
    mint_to,
    set_authority,
    sync_native,
)
from spl.token.models import (
    AuthorityType,
    BurnParams,
    CloseAccountParams,
    InitializeMintParams,
    MintToParams,
    SetAuthorityParams,
    SyncNativeParams,
)

log = logging.getLogger("launch-bot")

LAMPORTS_PER_SOL = 1_000_000_000
U64_MAX = 2**64 - 1

SYSTEM_PROGRAM_ID = Pubkey.from_string("11111111111111111111111111111111")
SYSVAR_RENT_ID = Pubkey.from_string("SysvarRent111111111111111111111111111111111")
MEMO_PROGRAM_ID = Pubkey.from_string("MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr")
TOKEN_METADATA_PROGRAM_ID = Pubkey.from_string("metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s")

# Raydium CPMM (raydium-cp-swap). Mainnet defaults; override for devnet via env.
RAYDIUM_CPMM_MAINNET = "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C"
RAYDIUM_CPMM_FEE_RECEIVER_MAINNET = "DNXgeM9EiiaAbaWvwjHj9fQQLAX5ZsfHyvmYUNRAdNC8"


# ════════════════════════════════════════════════════════════════════════════
# Configuration
# ════════════════════════════════════════════════════════════════════════════


def _id_set(raw: str) -> frozenset[int]:
    return frozenset(int(x) for x in raw.replace(" ", "").split(",") if x)


@dataclass(frozen=True)
class Config:
    bot_token: str
    rpc_url: str
    admin_ids: frozenset[int]  # may create tokens / pools
    owner_ids: frozenset[int]  # may additionally withdraw / burn LP / move SOL
    wallets_dir: Path
    master_wallet: str
    state_file: Path
    cpmm_program: Pubkey
    cpmm_fee_receiver: Pubkey
    cpmm_config_index: int
    priority_fee_micro_lamports: int
    confirm_timeout_s: int
    pending_ttl_s: int

    @classmethod
    def from_env(cls) -> "Config":
        load_dotenv()

        def need(key: str) -> str:
            val = os.getenv(key, "").strip()
            if not val:
                raise SystemExit(f"Missing required env var: {key}")
            return val

        admins = _id_set(need("ADMIN_IDS"))
        owners = _id_set(os.getenv("OWNER_IDS", "")) or admins
        if not owners <= admins:
            raise SystemExit("OWNER_IDS must be a subset of ADMIN_IDS")

        return cls(
            bot_token=need("BOT_TOKEN"),
            rpc_url=os.getenv("RPC_URL", "https://api.devnet.solana.com"),
            admin_ids=admins,
            owner_ids=owners,
            wallets_dir=Path(os.getenv("WALLETS_DIR", "./wallets")),
            master_wallet=os.getenv("MASTER_WALLET", "master"),
            state_file=Path(os.getenv("STATE_FILE", "./launches.json")),
            cpmm_program=Pubkey.from_string(os.getenv("RAYDIUM_CPMM_PROGRAM", RAYDIUM_CPMM_MAINNET)),
            cpmm_fee_receiver=Pubkey.from_string(
                os.getenv("RAYDIUM_CPMM_FEE_RECEIVER", RAYDIUM_CPMM_FEE_RECEIVER_MAINNET)
            ),
            cpmm_config_index=int(os.getenv("RAYDIUM_CPMM_CONFIG_INDEX", "0")),
            priority_fee_micro_lamports=int(os.getenv("PRIORITY_FEE_MICRO_LAMPORTS", "50000")),
            confirm_timeout_s=int(os.getenv("CONFIRM_TIMEOUT_S", "90")),
            pending_ttl_s=int(os.getenv("PENDING_TTL_S", "120")),
        )


# ════════════════════════════════════════════════════════════════════════════
# Errors & helpers
# ════════════════════════════════════════════════════════════════════════════


class BotError(Exception):
    """An error whose message is safe to show the operator verbatim."""


class TxFailed(BotError):
    def __init__(self, msg: str, logs: Sequence[str] | None = None):
        super().__init__(msg)
        self.logs = list(logs or [])


# solana-py wraps HTTP transport failures (timeouts, 5xx, 429, resets) in SolanaRpcException.
TRANSIENT_ERRORS = (SolanaRpcException, asyncio.TimeoutError, ConnectionError)


async def with_retry(
    fn: Callable[[], Awaitable[Any]],
    *,
    attempts: int = 4,
    base_delay: float = 0.5,
    what: str = "rpc call",
) -> Any:
    """Retry transient transport-level RPC failures with exponential backoff.

    Only use for idempotent calls (reads, or re-broadcasting an already-signed tx).
    """
    for i in range(attempts):
        try:
            return await fn()
        except TRANSIENT_ERRORS as exc:
            if i == attempts - 1:
                raise BotError(f"RPC unavailable during {what}: {exc!r}") from exc
            delay = base_delay * 2**i
            log.warning("%s failed (%r), retrying in %.1fs", what, exc, delay)
            await asyncio.sleep(delay)


def to_raw(amount: Decimal, decimals: int) -> int:
    raw = amount * (Decimal(10) ** decimals)
    if raw != raw.to_integral_value():
        raise BotError(f"Amount {amount} has more than {decimals} decimal places")
    raw_int = int(raw)
    if not 0 < raw_int <= U64_MAX:
        raise BotError("Amount out of range (must be > 0 and fit in u64)")
    return raw_int


def from_raw(raw: int, decimals: int) -> Decimal:
    return Decimal(raw) / (Decimal(10) ** decimals)


def parse_decimal(text: str) -> Decimal:
    try:
        val = Decimal(text.replace("_", "").replace(",", ""))
    except InvalidOperation as exc:
        raise BotError(f"Not a number: {text!r}") from exc
    if not val.is_finite() or val <= 0:
        raise BotError(f"Must be a positive number: {text!r}")
    return val


def parse_pubkey(text: str) -> Pubkey:
    try:
        return Pubkey.from_string(text.strip())
    except ValueError as exc:
        raise BotError(f"Invalid address: {text!r}") from exc


def anchor_discriminator(name: str) -> bytes:
    return hashlib.sha256(f"global:{name}".encode()).digest()[:8]


def pda(seeds: Sequence[bytes], program: Pubkey) -> Pubkey:
    return Pubkey.find_program_address(list(seeds), program)[0]


def e(text: Any) -> str:
    return html.escape(str(text))


def short(pk: Pubkey | str) -> str:
    s = str(pk)
    return f"{s[:4]}…{s[-4:]}"


def explorer(sig: Signature | str, rpc_url: str) -> str:
    cluster = "" if "mainnet" in rpc_url else "?cluster=devnet" if "devnet" in rpc_url else ""
    return f'<a href="https://solscan.io/tx/{sig}{cluster}">{short(str(sig))}</a>'


# ════════════════════════════════════════════════════════════════════════════
# Wallets
# ════════════════════════════════════════════════════════════════════════════


class WalletRegistry:
    """Loads named keypairs from `<wallets_dir>/<name>.json` (solana-keygen format).

    Private keys never leave this process: they are not logged, not echoed to
    Telegram, and cannot be imported/exported through the bot.
    """

    def __init__(self, directory: Path, master: str):
        self._wallets: dict[str, Keypair] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        if not directory.is_dir():
            raise SystemExit(f"Wallet directory not found: {directory}")
        for path in sorted(directory.glob("*.json")):
            if path.stat().st_mode & 0o077:
                log.warning("Wallet file %s is readable by group/others; chmod 600 it", path)
            try:
                secret = bytes(json.loads(path.read_text()))
                self._wallets[path.stem] = Keypair.from_bytes(secret)
                self._locks[path.stem] = asyncio.Lock()
            except (ValueError, TypeError) as exc:
                raise SystemExit(f"Could not load wallet {path}: {exc}") from exc
        if master not in self._wallets:
            raise SystemExit(f"Master wallet '{master}.json' not found in {directory}")
        self.master_name = master

    @property
    def names(self) -> list[str]:
        return list(self._wallets)

    def get(self, name: str) -> Keypair:
        try:
            return self._wallets[name]
        except KeyError:
            raise BotError(f"Unknown wallet '{name}'. Known: {', '.join(self._wallets)}") from None

    def lock(self, name: str) -> asyncio.Lock:
        """Per-wallet lock so one wallet never has two txs in flight from this bot."""
        return self._locks[name]

    def name_of(self, pubkey: Pubkey) -> str | None:
        return next((n for n, kp in self._wallets.items() if kp.pubkey() == pubkey), None)


# ════════════════════════════════════════════════════════════════════════════
# Persistent launch state
# ════════════════════════════════════════════════════════════════════════════


class LaunchStore:
    """Small JSON store keyed by mint address. Writes are atomic (tmp + rename)."""

    def __init__(self, path: Path):
        self._path = path
        self._lock = asyncio.Lock()
        self._data: dict[str, dict[str, Any]] = json.loads(path.read_text()) if path.exists() else {}

    def get(self, mint: str) -> dict[str, Any] | None:
        return self._data.get(mint)

    def all(self) -> dict[str, dict[str, Any]]:
        return dict(self._data)

    async def upsert(self, mint: str, **fields: Any) -> None:
        async with self._lock:
            self._data.setdefault(mint, {}).update(fields)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, indent=2))
            tmp.replace(self._path)


# ════════════════════════════════════════════════════════════════════════════
# Solana RPC service: build, simulate, send, confirm
# ════════════════════════════════════════════════════════════════════════════


class SolanaService:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.client = AsyncClient(cfg.rpc_url, commitment=Confirmed, timeout=30)

    async def close(self) -> None:
        await self.client.close()

    # ── reads ──────────────────────────────────────────────────────────────

    async def sol_balance(self, pubkey: Pubkey) -> int:
        resp = await with_retry(lambda: self.client.get_balance(pubkey), what="get_balance")
        return resp.value

    async def token_balance(self, token_account: Pubkey) -> int:
        """Raw token balance; 0 if the account does not exist."""
        try:
            resp = await with_retry(
                lambda: self.client.get_token_account_balance(token_account),
                what="get_token_account_balance",
            )
        except RPCException:
            return 0
        return int(resp.value.amount)

    async def account_data(self, pubkey: Pubkey) -> bytes | None:
        resp = await with_retry(lambda: self.client.get_account_info(pubkey), what="get_account_info")
        return bytes(resp.value.data) if resp.value else None

    async def rent_exempt(self, size: int) -> int:
        resp = await with_retry(
            lambda: self.client.get_minimum_balance_for_rent_exemption(size), what="rent"
        )
        return resp.value

    # ── writes ─────────────────────────────────────────────────────────────

    async def send(
        self,
        payer: Keypair,
        ixs: Sequence[Instruction],
        extra_signers: Sequence[Keypair] = (),
        *,
        compute_units: int = 200_000,
    ) -> Signature:
        """Simulate, then send and confirm a v0 transaction.

        * Simulation runs first so program errors surface (with logs) before any
          fee is paid.
        * The signed tx is re-broadcast until it confirms or its blockhash
          expires. Re-sending identical signed bytes is idempotent (same
          signature), so this can never double-execute.
        """
        budget = [
            set_compute_unit_limit(compute_units),
            set_compute_unit_price(self.cfg.priority_fee_micro_lamports),
        ]
        bh = await with_retry(lambda: self.client.get_latest_blockhash(), what="get_latest_blockhash")
        blockhash, last_valid = bh.value.blockhash, bh.value.last_valid_block_height

        msg = MessageV0.try_compile(payer.pubkey(), [*budget, *ixs], [], blockhash)
        tx = VersionedTransaction(msg, [payer, *extra_signers])
        raw = bytes(tx)
        if len(raw) > 1232:
            raise BotError(f"Transaction too large ({len(raw)} bytes); shorten metadata")

        sim = await with_retry(
            lambda: self.client.simulate_transaction(tx, sig_verify=True), what="simulate"
        )
        if sim.value.err is not None:
            raise TxFailed(f"Simulation failed: {sim.value.err}", sim.value.logs)

        sig = tx.signatures[0]
        opts = TxOpts(skip_preflight=True, max_retries=0)
        deadline = time.monotonic() + self.cfg.confirm_timeout_s
        log.info("sending %s", sig)

        while True:
            try:
                await self.client.send_raw_transaction(raw, opts=opts)
            except TRANSIENT_ERRORS + (RPCException,) as exc:
                # The tx may still land from an earlier broadcast; keep polling.
                log.warning("broadcast error for %s: %r", sig, exc)

            await asyncio.sleep(2)
            status = await with_retry(
                lambda: self.client.get_signature_statuses([sig]), what="get_signature_statuses"
            )
            st = status.value[0]
            if st is not None:
                if st.err is not None:
                    raise TxFailed(f"Transaction {sig} failed on-chain: {st.err}")
                if st.confirmation_status in (
                    TransactionConfirmationStatus.Confirmed,
                    TransactionConfirmationStatus.Finalized,
                ):
                    log.info("confirmed %s", sig)
                    return sig

            height = await with_retry(lambda: self.client.get_block_height(), what="get_block_height")
            if height.value > last_valid:
                raise TxFailed(f"Transaction {sig} expired (blockhash too old) and did not land")
            if time.monotonic() > deadline:
                raise TxFailed(
                    f"Timed out waiting for {sig}. It may still land; check the explorer before retrying."
                )


# ════════════════════════════════════════════════════════════════════════════
# 1. Token creation + Metaplex metadata
# ════════════════════════════════════════════════════════════════════════════


def _borsh_string(s: str) -> bytes:
    b = s.encode()
    return struct.pack("<I", len(b)) + b


def metadata_pda(mint: Pubkey) -> Pubkey:
    return pda([b"metadata", bytes(TOKEN_METADATA_PROGRAM_ID), bytes(mint)], TOKEN_METADATA_PROGRAM_ID)


def create_metadata_v3_ix(
    *,
    mint: Pubkey,
    mint_authority: Pubkey,
    payer: Pubkey,
    update_authority: Pubkey,
    name: str,
    symbol: str,
    uri: str,
    is_mutable: bool,
) -> Instruction:
    """Metaplex Token Metadata `CreateMetadataAccountV3` (instruction #33)."""
    data = (
        bytes([33])
        # DataV2
        + _borsh_string(name)
        + _borsh_string(symbol)
        + _borsh_string(uri)
        + struct.pack("<H", 0)  # seller_fee_basis_points
        + b"\x00"  # creators: None
        + b"\x00"  # collection: None
        + b"\x00"  # uses: None
        # args
        + bytes([1 if is_mutable else 0])
        + b"\x00"  # collection_details: None
    )
    accounts = [
        AccountMeta(metadata_pda(mint), is_signer=False, is_writable=True),
        AccountMeta(mint, is_signer=False, is_writable=False),
        AccountMeta(mint_authority, is_signer=True, is_writable=False),
        AccountMeta(payer, is_signer=True, is_writable=True),
        AccountMeta(update_authority, is_signer=True, is_writable=False),
        AccountMeta(SYSTEM_PROGRAM_ID, is_signer=False, is_writable=False),
    ]
    return Instruction(TOKEN_METADATA_PROGRAM_ID, data, accounts)


@dataclass
class TokenSpec:
    name: str
    symbol: str
    decimals: int
    supply: Decimal
    uri: str
    revoke_mint: bool = True
    mutable_metadata: bool = True

    def validate(self) -> None:
        # Metaplex hard limits
        if not 0 < len(self.name.encode()) <= 32:
            raise BotError("Name must be 1–32 bytes")
        if not 0 < len(self.symbol.encode()) <= 10:
            raise BotError("Symbol must be 1–10 bytes")
        if len(self.uri.encode()) > 200:
            raise BotError("URI must be ≤ 200 bytes")
        if not self.uri.startswith(("https://", "ipfs://", "ar://")):
            raise BotError("URI should be an https://, ipfs:// or ar:// link to the metadata JSON")
        if not 0 <= self.decimals <= 9:
            raise BotError("Decimals must be 0–9")
        to_raw(self.supply, self.decimals)  # range check


class TokenService:
    def __init__(self, sol: SolanaService):
        self.sol = sol

    async def create(self, payer: Keypair, spec: TokenSpec) -> tuple[Pubkey, Signature]:
        """Create mint, mint full supply to payer's ATA, attach metadata.

        Freeze authority is never set (a freeze authority is a red flag for
        holders and is rejected by many aggregators). Mint authority is revoked
        in the same transaction when `spec.revoke_mint` is set, so supply is
        provably fixed from the first block.
        """
        spec.validate()
        mint = Keypair()
        owner = payer.pubkey()
        ata = get_associated_token_address(owner, mint.pubkey())
        rent = await self.sol.rent_exempt(MINT_LEN)

        ixs = [
            create_account(
                CreateAccountParams(
                    from_pubkey=owner,
                    to_pubkey=mint.pubkey(),
                    lamports=rent,
                    space=MINT_LEN,
                    owner=TOKEN_PROGRAM_ID,
                )
            ),
            initialize_mint(
                InitializeMintParams(
                    decimals=spec.decimals,
                    program_id=TOKEN_PROGRAM_ID,
                    mint=mint.pubkey(),
                    mint_authority=owner,
                    freeze_authority=None,
                )
            ),
            create_idempotent_associated_token_account(owner, owner, mint.pubkey()),
            mint_to(
                MintToParams(
                    program_id=TOKEN_PROGRAM_ID,
                    mint=mint.pubkey(),
                    dest=ata,
                    mint_authority=owner,
                    amount=to_raw(spec.supply, spec.decimals),
                )
            ),
            create_metadata_v3_ix(
                mint=mint.pubkey(),
                mint_authority=owner,
                payer=owner,
                update_authority=owner,
                name=spec.name,
                symbol=spec.symbol,
                uri=spec.uri,
                is_mutable=spec.mutable_metadata,
            ),
        ]
        if spec.revoke_mint:
            ixs.append(
                set_authority(
                    SetAuthorityParams(
                        program_id=TOKEN_PROGRAM_ID,
                        account=mint.pubkey(),
                        authority=AuthorityType.MINT_TOKENS,
                        current_authority=owner,
                        new_authority=None,
                    )
                )
            )

        sig = await self.sol.send(payer, ixs, [mint], compute_units=250_000)
        return mint.pubkey(), sig


# ════════════════════════════════════════════════════════════════════════════
# 2 & 3. Raydium CPMM: create pool, inspect, withdraw
# ════════════════════════════════════════════════════════════════════════════


@dataclass
class CpmmPoolKeys:
    program: Pubkey
    amm_config: Pubkey
    authority: Pubkey
    pool: Pubkey
    lp_mint: Pubkey
    mint0: Pubkey
    mint1: Pubkey
    vault0: Pubkey
    vault1: Pubkey
    observation: Pubkey


@dataclass
class CpmmPoolState:
    keys: CpmmPoolKeys
    lp_supply: int
    mint0_decimals: int
    mint1_decimals: int
    reserve0: int  # vault balance minus accrued protocol/fund fees
    reserve1: int
    open_time: int


class RaydiumCpmm:
    """Minimal client for Raydium's constant-product AMM (raydium-cp-swap).

    Instruction layouts follow the program's Anchor IDL. Account ordering is
    load-bearing; if Raydium ships a program upgrade, re-check against
    https://github.com/raydium-io/raydium-cp-swap before using on mainnet.
    """

    # PoolState byte offsets (after the 8-byte Anchor discriminator).
    _OFF_MINT0 = 8 + 32 * 5
    _OFF_MINT1 = 8 + 32 * 6
    _OFF_DECIMALS = 8 + 32 * 10 + 3  # lp_mint_decimals, mint0_decimals, mint1_decimals
    _OFF_LP_SUPPLY = 8 + 32 * 10 + 5
    _OFF_FEES = _OFF_LP_SUPPLY + 8  # protocol0, protocol1, fund0, fund1, open_time

    def __init__(self, sol: SolanaService, cfg: Config):
        self.sol = sol
        self.program = cfg.cpmm_program
        self.fee_receiver = cfg.cpmm_fee_receiver
        self.config_index = cfg.cpmm_config_index

    def derive(self, token_a: Pubkey, token_b: Pubkey) -> CpmmPoolKeys:
        mint0, mint1 = sorted((token_a, token_b), key=bytes)  # program requires mint0 < mint1
        p = self.program
        amm_config = pda([b"amm_config", self.config_index.to_bytes(2, "big")], p)
        pool = pda([b"pool", bytes(amm_config), bytes(mint0), bytes(mint1)], p)
        return CpmmPoolKeys(
            program=p,
            amm_config=amm_config,
            authority=pda([b"vault_and_lp_mint_auth_seed"], p),
            pool=pool,
            lp_mint=pda([b"pool_lp_mint", bytes(pool)], p),
            mint0=mint0,
            mint1=mint1,
            vault0=pda([b"pool_vault", bytes(pool), bytes(mint0)], p),
            vault1=pda([b"pool_vault", bytes(pool), bytes(mint1)], p),
            observation=pda([b"observation", bytes(pool)], p),
        )

    @staticmethod
    def _wrap_sol_ixs(owner: Pubkey, lamports: int) -> tuple[Pubkey, list[Instruction]]:
        wsol = get_associated_token_address(owner, WRAPPED_SOL_MINT)
        return wsol, [
            create_idempotent_associated_token_account(owner, owner, WRAPPED_SOL_MINT),
            transfer(TransferParams(from_pubkey=owner, to_pubkey=wsol, lamports=lamports)),
            sync_native(SyncNativeParams(program_id=TOKEN_PROGRAM_ID, account=wsol)),
        ]

    @staticmethod
    def _unwrap_sol_ix(owner: Pubkey) -> Instruction:
        return close_account(
            CloseAccountParams(
                program_id=TOKEN_PROGRAM_ID,
                account=get_associated_token_address(owner, WRAPPED_SOL_MINT),
                dest=owner,
                owner=owner,
            )
        )

    # ── create pool + initial liquidity ────────────────────────────────────

    async def create_pool(
        self,
        payer: Keypair,
        token_mint: Pubkey,
        token_amount: int,
        sol_lamports: int,
        open_time: int = 0,
    ) -> tuple[CpmmPoolKeys, Signature]:
        owner = payer.pubkey()
        keys = self.derive(token_mint, WRAPPED_SOL_MINT)

        if await self.sol.account_data(keys.pool) is not None:
            raise BotError(f"A CPMM pool already exists for this pair: {keys.pool}")

        token_ata = get_associated_token_address(owner, token_mint)
        have = await self.sol.token_balance(token_ata)
        if have < token_amount:
            raise BotError(f"Wallet holds {have} raw tokens, need {token_amount}")

        _, wrap_ixs = self._wrap_sol_ixs(owner, sol_lamports)
        amount0, amount1 = (
            (token_amount, sol_lamports) if keys.mint0 == token_mint else (sol_lamports, token_amount)
        )

        init = Instruction(
            self.program,
            anchor_discriminator("initialize") + struct.pack("<QQQ", amount0, amount1, open_time),
            [
                AccountMeta(owner, True, True),  # creator
                AccountMeta(keys.amm_config, False, False),
                AccountMeta(keys.authority, False, False),
                AccountMeta(keys.pool, False, True),
                AccountMeta(keys.mint0, False, False),
                AccountMeta(keys.mint1, False, False),
                AccountMeta(keys.lp_mint, False, True),
                AccountMeta(get_associated_token_address(owner, keys.mint0), False, True),
                AccountMeta(get_associated_token_address(owner, keys.mint1), False, True),
                AccountMeta(get_associated_token_address(owner, keys.lp_mint), False, True),
                AccountMeta(keys.vault0, False, True),
                AccountMeta(keys.vault1, False, True),
                AccountMeta(self.fee_receiver, False, True),  # create_pool_fee
                AccountMeta(keys.observation, False, True),
                AccountMeta(TOKEN_PROGRAM_ID, False, False),  # token_program (LP)
                AccountMeta(TOKEN_PROGRAM_ID, False, False),  # token_0_program
                AccountMeta(TOKEN_PROGRAM_ID, False, False),  # token_1_program
                AccountMeta(ASSOCIATED_TOKEN_PROGRAM_ID, False, False),
                AccountMeta(SYSTEM_PROGRAM_ID, False, False),
                AccountMeta(SYSVAR_RENT_ID, False, False),
            ],
        )

        # Wrap SOL -> initialize (deposits both sides, mints LP) -> unwrap leftovers.
        ixs = [*wrap_ixs, init, self._unwrap_sol_ix(owner)]
        sig = await self.sol.send(payer, ixs, compute_units=400_000)
        return keys, sig

    # ── read pool ──────────────────────────────────────────────────────────

    async def fetch(self, pool: Pubkey) -> CpmmPoolState:
        data = await self.sol.account_data(pool)
        if data is None:
            raise BotError(f"Pool {pool} not found")
        mint0 = Pubkey.from_bytes(data[self._OFF_MINT0 : self._OFF_MINT0 + 32])
        mint1 = Pubkey.from_bytes(data[self._OFF_MINT1 : self._OFF_MINT1 + 32])
        _, dec0, dec1 = data[self._OFF_DECIMALS : self._OFF_DECIMALS + 3]
        (lp_supply,) = struct.unpack_from("<Q", data, self._OFF_LP_SUPPLY)
        p0, p1, f0, f1, open_time = struct.unpack_from("<QQQQQ", data, self._OFF_FEES)

        keys = self.derive(mint0, mint1)
        if keys.pool != pool:
            raise BotError("Pool is not on the configured AMM config; refusing to operate on it")

        v0, v1 = await asyncio.gather(
            self.sol.token_balance(keys.vault0), self.sol.token_balance(keys.vault1)
        )
        return CpmmPoolState(
            keys=keys,
            lp_supply=lp_supply,
            mint0_decimals=dec0,
            mint1_decimals=dec1,
            reserve0=max(v0 - p0 - f0, 0),
            reserve1=max(v1 - p1 - f1, 0),
            open_time=open_time,
        )

    # ── withdraw liquidity ─────────────────────────────────────────────────

    @staticmethod
    def quote_withdraw(state: CpmmPoolState, lp_amount: int, slippage_bps: int) -> tuple[int, int, int, int]:
        """Return (expected0, expected1, min0, min1) for burning `lp_amount` LP."""
        if state.lp_supply == 0:
            raise BotError("Pool has zero LP supply")
        exp0 = lp_amount * state.reserve0 // state.lp_supply
        exp1 = lp_amount * state.reserve1 // state.lp_supply
        keep = 10_000 - slippage_bps
        return exp0, exp1, exp0 * keep // 10_000, exp1 * keep // 10_000

    async def withdraw(
        self, payer: Keypair, state: CpmmPoolState, lp_amount: int, min0: int, min1: int
    ) -> Signature:
        """Burn LP and receive both underlying assets; any WSOL is unwrapped to SOL.

        The min amounts are enforced on-chain, so a pool that moved between
        quote and execution makes the tx fail instead of filling at a bad price.
        """
        owner = payer.pubkey()
        k = state.keys
        ixs = [
            create_idempotent_associated_token_account(owner, owner, k.mint0),
            create_idempotent_associated_token_account(owner, owner, k.mint1),
            Instruction(
                self.program,
                anchor_discriminator("withdraw") + struct.pack("<QQQ", lp_amount, min0, min1),
                [
                    AccountMeta(owner, True, False),
                    AccountMeta(k.authority, False, False),
                    AccountMeta(k.pool, False, True),
                    AccountMeta(get_associated_token_address(owner, k.lp_mint), False, True),
                    AccountMeta(get_associated_token_address(owner, k.mint0), False, True),
                    AccountMeta(get_associated_token_address(owner, k.mint1), False, True),
                    AccountMeta(k.vault0, False, True),
                    AccountMeta(k.vault1, False, True),
                    AccountMeta(TOKEN_PROGRAM_ID, False, False),
                    AccountMeta(TOKEN_2022_PROGRAM_ID, False, False),
                    AccountMeta(k.mint0, False, False),
                    AccountMeta(k.mint1, False, False),
                    AccountMeta(k.lp_mint, False, True),
                    AccountMeta(MEMO_PROGRAM_ID, False, False),
                ],
            ),
        ]
        if WRAPPED_SOL_MINT in (k.mint0, k.mint1):
            ixs.append(self._unwrap_sol_ix(owner))
        return await self.sol.send(payer, ixs, compute_units=300_000)

    async def burn_lp(self, payer: Keypair, keys: CpmmPoolKeys, lp_amount: int) -> Signature:
        """Permanently destroy LP tokens, locking that share of liquidity forever."""
        owner = payer.pubkey()
        return await self.sol.send(
            payer,
            [
                burn(
                    BurnParams(
                        program_id=TOKEN_PROGRAM_ID,
                        account=get_associated_token_address(owner, keys.lp_mint),
                        mint=keys.lp_mint,
                        owner=owner,
                        amount=lp_amount,
                    )
                )
            ],
        )


# ════════════════════════════════════════════════════════════════════════════
# Telegram layer
# ════════════════════════════════════════════════════════════════════════════


@dataclass
class Services:
    cfg: Config
    wallets: WalletRegistry
    store: LaunchStore
    sol: SolanaService
    tokens: TokenService
    cpmm: RaydiumCpmm
    active_wallet: dict[int, str] = field(default_factory=dict)  # tg user -> wallet name
    pending: dict[str, "PendingOp"] = field(default_factory=dict)

    def wallet_for(self, user_id: int) -> tuple[str, Keypair]:
        name = self.active_wallet.get(user_id, self.wallets.master_name)
        return name, self.wallets.get(name)


@dataclass
class PendingOp:
    user_id: int
    wallet: str
    summary: str
    run: Callable[[], Awaitable[str]]
    owner_only: bool
    expires: float


class ConfirmCb(CallbackData, prefix="op"):
    op_id: str
    ok: bool


class AccessMiddleware(BaseMiddleware):
    """Drop every update that isn't from a configured admin, silently."""

    def __init__(self, admin_ids: frozenset[int]):
        self.admin_ids = admin_ids

    async def __call__(self, handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        user = data.get("event_from_user")
        if user is None or user.id not in self.admin_ids:
            log.warning("Rejected update from user %s", getattr(user, "id", None))
            return None
        return await handler(event, data)


class NewToken(StatesGroup):
    name = State()
    symbol = State()
    decimals = State()
    supply = State()
    uri = State()


router = Router()

HELP = """<b>Launch manager</b>

<b>Wallets</b>
/wallets – list wallets &amp; SOL balances
/use &lt;name&gt; – switch active wallet
/fund &lt;name&gt; &lt;sol&gt; – master → wallet <i>(owner)</i>
/sweep &lt;name&gt; – wallet → master, leaves rent <i>(owner)</i>

<b>Token</b>
/newtoken – guided SPL token + metadata creation
/launches – tokens created by this bot

<b>Pool</b>
/createpool &lt;mint&gt; &lt;sol&gt; &lt;tokens|all&gt; – Raydium CPMM + initial liquidity
/pool &lt;mint&gt; – reserves, LP supply, your share
/withdraw &lt;mint&gt; &lt;percent&gt; [slippage_bps] – remove liquidity <i>(owner)</i>
/burnlp &lt;mint&gt; &lt;percent&gt; – burn LP to lock liquidity permanently <i>(owner)</i>

/cancel – abort current flow
Every on-chain action asks for confirmation first."""


# ── confirmation plumbing ──────────────────────────────────────────────────


async def ask_confirm(
    msg: Message,
    svc: Services,
    *,
    wallet: str,
    summary: str,
    run: Callable[[], Awaitable[str]],
    owner_only: bool = False,
) -> None:
    user_id = msg.from_user.id
    if owner_only and user_id not in svc.cfg.owner_ids:
        await msg.answer("⛔ This action is restricted to owners.")
        return
    now = time.time()
    for k in [k for k, v in svc.pending.items() if v.expires < now]:
        del svc.pending[k]
    op_id = secrets.token_urlsafe(8)
    svc.pending[op_id] = PendingOp(user_id, wallet, summary, run, owner_only, now + svc.cfg.pending_ttl_s)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Confirm", callback_data=ConfirmCb(op_id=op_id, ok=True).pack()),
                InlineKeyboardButton(text="✖️ Cancel", callback_data=ConfirmCb(op_id=op_id, ok=False).pack()),
            ]
        ]
    )
    await msg.answer(
        f"{summary}\n\n<i>Signing wallet: <b>{e(wallet)}</b>. Expires in {svc.cfg.pending_ttl_s}s.</i>",
        reply_markup=kb,
    )


@router.callback_query(ConfirmCb.filter())
async def on_confirm(cb: CallbackQuery, callback_data: ConfirmCb, svc: Services) -> None:
    op = svc.pending.get(callback_data.op_id)
    if op is not None and op.user_id != cb.from_user.id:
        # Only the admin who requested the action can confirm it.
        await cb.answer("Only the requester can confirm this.", show_alert=True)
        return
    svc.pending.pop(callback_data.op_id, None)
    await cb.message.edit_reply_markup(reply_markup=None)
    if op is None or op.expires < time.time():
        await cb.answer("Expired or already handled.", show_alert=True)
        return
    if not callback_data.ok:
        await cb.answer("Cancelled")
        await cb.message.answer("Cancelled.")
        return

    await cb.answer("Submitting…")
    status = await cb.message.answer("⏳ Submitting transaction…")
    lock = svc.wallets.lock(op.wallet)
    if lock.locked():
        await status.edit_text("⏳ Waiting for this wallet's previous transaction…")
    try:
        async with lock:
            result = await op.run()
        await status.edit_text(result, disable_web_page_preview=True)
    except TxFailed as exc:
        tail = "\n".join(exc.logs[-8:])
        await status.edit_text(
            f"❌ {e(exc)}" + (f"\n<pre>{e(tail)}</pre>" if tail else ""), disable_web_page_preview=True
        )
    except BotError as exc:
        await status.edit_text(f"❌ {e(exc)}")
    except Exception:
        log.exception("Unhandled error in confirmed op")
        await status.edit_text("❌ Unexpected error; see server logs.")


@router.errors()
async def on_error(event: ErrorEvent) -> bool:
    log.error("Handler error: %r", event.exception, exc_info=event.exception)
    return True


def user_errors(handler):
    """Turn BotError raised during input parsing into a chat reply."""

    accepted = set(inspect.signature(handler).parameters) - {"msg"}

    # aiogram passes every context value to a **kwargs handler; forward only
    # the ones the wrapped handler declares.
    async def wrapper(msg: Message, **kwargs):
        try:
            return await handler(msg, **{k: v for k, v in kwargs.items() if k in accepted})
        except BotError as exc:
            await msg.answer(f"❌ {e(exc)}")

    wrapper.__name__ = handler.__name__
    return wrapper


# ── general / wallets ──────────────────────────────────────────────────────


@router.message(Command("start", "help"))
async def cmd_help(msg: Message) -> None:
    await msg.answer(HELP)


@router.message(Command("cancel"))
async def cmd_cancel(msg: Message, state: FSMContext) -> None:
    await state.clear()
    await msg.answer("Cancelled.")


@router.message(Command("wallets"))
@user_errors
async def cmd_wallets(msg: Message, svc: Services) -> None:
    active, _ = svc.wallet_for(msg.from_user.id)
    names = svc.wallets.names
    balances = await asyncio.gather(*(svc.sol.sol_balance(svc.wallets.get(n).pubkey()) for n in names))
    lines = []
    for n, bal in zip(names, balances):
        tag = " 👑" if n == svc.wallets.master_name else ""
        cur = " ◀ active" if n == active else ""
        lines.append(
            f"<b>{e(n)}</b>{tag} <code>{svc.wallets.get(n).pubkey()}</code>\n"
            f"   {from_raw(bal, 9):.4f} SOL{cur}"
        )
    await msg.answer("\n".join(lines))


@router.message(Command("use"))
@user_errors
async def cmd_use(msg: Message, command: CommandObject, svc: Services) -> None:
    name = (command.args or "").strip()
    svc.wallets.get(name)
    svc.active_wallet[msg.from_user.id] = name
    await msg.answer(f"Active wallet: <b>{e(name)}</b>")


@router.message(Command("fund"))
@user_errors
async def cmd_fund(msg: Message, command: CommandObject, svc: Services) -> None:
    args = (command.args or "").split()
    if len(args) != 2:
        raise BotError("Usage: /fund <wallet> <sol>")
    target = svc.wallets.get(args[0]).pubkey()
    lamports = to_raw(parse_decimal(args[1]), 9)
    master = svc.wallets.get(svc.wallets.master_name)

    async def run() -> str:
        sig = await svc.sol.send(
            master,
            [transfer(TransferParams(from_pubkey=master.pubkey(), to_pubkey=target, lamports=lamports))],
        )
        return f"✅ Sent {e(args[1])} SOL to {e(args[0])}: {explorer(sig, svc.cfg.rpc_url)}"

    await ask_confirm(
        msg, svc, wallet=svc.wallets.master_name, owner_only=True, run=run,
        summary=f"<b>Fund wallet</b>\n{e(args[1])} SOL → <b>{e(args[0])}</b> <code>{target}</code>",
    )


@router.message(Command("sweep"))
@user_errors
async def cmd_sweep(msg: Message, command: CommandObject, svc: Services) -> None:
    name = (command.args or "").strip()
    if name == svc.wallets.master_name:
        raise BotError("Cannot sweep the master wallet into itself")
    src = svc.wallets.get(name)
    master_pk = svc.wallets.get(svc.wallets.master_name).pubkey()

    async def run() -> str:
        bal = await svc.sol.sol_balance(src.pubkey())
        amount = bal - 1_000_000  # leave 0.001 SOL for fees
        if amount <= 0:
            raise BotError("Nothing to sweep")
        sig = await svc.sol.send(
            src, [transfer(TransferParams(from_pubkey=src.pubkey(), to_pubkey=master_pk, lamports=amount))]
        )
        return f"✅ Swept {from_raw(amount, 9)} SOL to master: {explorer(sig, svc.cfg.rpc_url)}"

    await ask_confirm(
        msg, svc, wallet=name, owner_only=True, run=run,
        summary=f"<b>Sweep</b> SOL from <b>{e(name)}</b> → master (leaves 0.001 SOL)",
    )


# ── token creation flow ────────────────────────────────────────────────────


@router.message(Command("newtoken"))
async def cmd_newtoken(msg: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(NewToken.name)
    await msg.answer("Token <b>name</b>? (≤ 32 bytes)  /cancel to abort")


@router.message(NewToken.name, F.text)
async def nt_name(msg: Message, state: FSMContext) -> None:
    await state.update_data(name=msg.text.strip())
    await state.set_state(NewToken.symbol)
    await msg.answer("<b>Symbol</b>? (≤ 10 bytes)")


@router.message(NewToken.symbol, F.text)
async def nt_symbol(msg: Message, state: FSMContext) -> None:
    await state.update_data(symbol=msg.text.strip().upper())
    await state.set_state(NewToken.decimals)
    await msg.answer("<b>Decimals</b>? (0–9, 6 is typical for memecoins)")


@router.message(NewToken.decimals, F.text)
async def nt_decimals(msg: Message, state: FSMContext) -> None:
    if not msg.text.strip().isdigit() or not 0 <= int(msg.text) <= 9:
        await msg.answer("Enter an integer 0–9.")
        return
    await state.update_data(decimals=int(msg.text))
    await state.set_state(NewToken.supply)
    await msg.answer("Total <b>supply</b> (whole tokens, e.g. 1000000000)?")


@router.message(NewToken.supply, F.text)
async def nt_supply(msg: Message, state: FSMContext) -> None:
    try:
        supply = parse_decimal(msg.text)
        to_raw(supply, (await state.get_data())["decimals"])
    except BotError as exc:
        await msg.answer(f"❌ {e(exc)}")
        return
    await state.update_data(supply=str(supply))
    await state.set_state(NewToken.uri)
    await msg.answer(
        "Metadata <b>URI</b>? Link to the off-chain JSON "
        "(<code>{\"name\",\"symbol\",\"description\",\"image\"}</code>) on IPFS/Arweave/https."
    )


@router.message(NewToken.uri, F.text)
async def nt_uri(msg: Message, state: FSMContext, svc: Services) -> None:
    data = await state.get_data()
    await state.clear()
    spec = TokenSpec(
        name=data["name"],
        symbol=data["symbol"],
        decimals=data["decimals"],
        supply=Decimal(data["supply"]),
        uri=msg.text.strip(),
    )
    try:
        spec.validate()
    except BotError as exc:
        await msg.answer(f"❌ {e(exc)}\nStart again with /newtoken.")
        return

    wallet_name, payer = svc.wallet_for(msg.from_user.id)

    async def run() -> str:
        mint, sig = await svc.tokens.create(payer, spec)
        await svc.store.upsert(
            str(mint),
            name=spec.name,
            symbol=spec.symbol,
            decimals=spec.decimals,
            supply=str(spec.supply),
            uri=spec.uri,
            creator=str(payer.pubkey()),
            wallet=wallet_name,
            mint_revoked=spec.revoke_mint,
            create_sig=str(sig),
            created_at=int(time.time()),
        )
        return (
            f"✅ <b>{e(spec.symbol)}</b> created\n"
            f"Mint: <code>{mint}</code>\nTx: {explorer(sig, svc.cfg.rpc_url)}"
        )

    await ask_confirm(
        msg, svc, wallet=wallet_name, run=run,
        summary=(
            f"<b>Create token</b>\n"
            f"Name: {e(spec.name)}\nSymbol: {e(spec.symbol)}\nDecimals: {spec.decimals}\n"
            f"Supply: {spec.supply:,}\nURI: <code>{e(spec.uri)}</code>\n"
            f"Mint authority: <b>revoked</b> (fixed supply) · Freeze authority: <b>none</b>"
        ),
    )


@router.message(Command("launches"))
async def cmd_launches(msg: Message, svc: Services) -> None:
    items = svc.store.all()
    if not items:
        await msg.answer("No launches yet.")
        return
    lines = [
        f"<b>{e(v.get('symbol'))}</b> <code>{m}</code>\n   pool: "
        + (f"<code>{v['pool']}</code>" if v.get("pool") else "—")
        for m, v in items.items()
    ]
    await msg.answer("\n".join(lines))


# ── pool management ────────────────────────────────────────────────────────


def _pool_for(svc: Services, mint: Pubkey) -> Pubkey:
    rec = svc.store.get(str(mint))
    if rec and rec.get("pool"):
        return Pubkey.from_string(rec["pool"])
    return svc.cpmm.derive(mint, WRAPPED_SOL_MINT).pool


@router.message(Command("createpool"))
@user_errors
async def cmd_createpool(msg: Message, command: CommandObject, svc: Services) -> None:
    args = (command.args or "").split()
    if len(args) != 3:
        raise BotError("Usage: /createpool <mint> <sol_amount> <token_amount|all>")
    mint = parse_pubkey(args[0])
    rec = svc.store.get(str(mint))
    if rec is None:
        raise BotError("Unknown mint (only tokens created by this bot are supported)")
    decimals = rec["decimals"]
    lamports = to_raw(parse_decimal(args[1]), 9)

    wallet_name, payer = svc.wallet_for(msg.from_user.id)
    owner = payer.pubkey()
    held = await svc.sol.token_balance(get_associated_token_address(owner, mint))
    token_raw = held if args[2].lower() == "all" else to_raw(parse_decimal(args[2]), decimals)
    if token_raw <= 0 or token_raw > held:
        raise BotError(f"Wallet {wallet_name} holds {from_raw(held, decimals)} tokens")

    # Pool creation fee (~0.15 SOL on mainnet) + rent for pool/vault/LP accounts.
    needed = lamports + int(0.25 * LAMPORTS_PER_SOL)
    bal = await svc.sol.sol_balance(owner)
    if bal < needed:
        raise BotError(f"Need ≈{from_raw(needed, 9)} SOL (deposit + fees/rent), have {from_raw(bal, 9)}")

    keys = svc.cpmm.derive(mint, WRAPPED_SOL_MINT)
    price = Decimal(lamports) / LAMPORTS_PER_SOL / from_raw(token_raw, decimals)

    async def run() -> str:
        keys_, sig = await svc.cpmm.create_pool(payer, mint, token_raw, lamports)
        await svc.store.upsert(
            str(mint), pool=str(keys_.pool), lp_mint=str(keys_.lp_mint),
            pool_wallet=wallet_name, pool_sig=str(sig),
        )
        return (
            f"✅ Pool live\nPool: <code>{keys_.pool}</code>\nLP mint: <code>{keys_.lp_mint}</code>\n"
            f"Tx: {explorer(sig, svc.cfg.rpc_url)}"
        )

    await ask_confirm(
        msg, svc, wallet=wallet_name, run=run,
        summary=(
            f"<b>Create Raydium CPMM pool</b> {e(rec['symbol'])}/SOL\n"
            f"Deposit: {from_raw(token_raw, decimals):,} {e(rec['symbol'])} + {from_raw(lamports, 9)} SOL\n"
            f"Initial price: {price:.12f} SOL per token\n"
            f"Pool: <code>{keys.pool}</code>\n"
            f"<i>Includes a one-time Raydium pool creation fee.</i>"
        ),
    )


@router.message(Command("pool"))
@user_errors
async def cmd_pool(msg: Message, command: CommandObject, svc: Services) -> None:
    mint = parse_pubkey(command.args or "")
    state = await svc.cpmm.fetch(_pool_for(svc, mint))
    _, kp = svc.wallet_for(msg.from_user.id)
    lp_held = await svc.sol.token_balance(get_associated_token_address(kp.pubkey(), state.keys.lp_mint))
    share = Decimal(lp_held) / state.lp_supply * 100 if state.lp_supply else Decimal(0)
    k = state.keys
    await msg.answer(
        f"<b>Pool</b> <code>{k.pool}</code>\n"
        f"{short(k.mint0)}: {from_raw(state.reserve0, state.mint0_decimals):,}\n"
        f"{short(k.mint1)}: {from_raw(state.reserve1, state.mint1_decimals):,}\n"
        f"LP supply: {state.lp_supply:,}\nYour LP: {lp_held:,} ({share:.2f}%)"
    )


def _parse_percent(text: str) -> Decimal:
    pct = parse_decimal(text)
    if pct > 100:
        raise BotError("Percent must be in (0, 100]")
    return pct


@router.message(Command("withdraw"))
@user_errors
async def cmd_withdraw(msg: Message, command: CommandObject, svc: Services) -> None:
    args = (command.args or "").split()
    if len(args) not in (2, 3):
        raise BotError("Usage: /withdraw <mint> <percent> [slippage_bps=100]")
    mint = parse_pubkey(args[0])
    pct = _parse_percent(args[1])
    slippage_bps = int(args[2]) if len(args) == 3 else 100
    if not 0 <= slippage_bps <= 5_000:
        raise BotError("slippage_bps must be 0–5000")

    wallet_name, payer = svc.wallet_for(msg.from_user.id)
    pool = _pool_for(svc, mint)
    state = await svc.cpmm.fetch(pool)
    lp_held = await svc.sol.token_balance(get_associated_token_address(payer.pubkey(), state.keys.lp_mint))
    lp_amount = int(Decimal(lp_held) * pct / 100)
    if lp_amount <= 0:
        raise BotError(f"Wallet {wallet_name} holds no LP for this pool")
    exp0, exp1, _, _ = svc.cpmm.quote_withdraw(state, lp_amount, slippage_bps)

    async def run() -> str:
        # Re-quote at execution time; the preview may be stale by now.
        fresh = await svc.cpmm.fetch(pool)
        _, _, min0, min1 = svc.cpmm.quote_withdraw(fresh, lp_amount, slippage_bps)
        sig = await svc.cpmm.withdraw(payer, fresh, lp_amount, min0, min1)
        return f"✅ Liquidity withdrawn to {e(wallet_name)}: {explorer(sig, svc.cfg.rpc_url)}"

    k = state.keys
    await ask_confirm(
        msg, svc, wallet=wallet_name, owner_only=True, run=run,
        summary=(
            f"<b>Remove liquidity</b> ({pct}% of your LP)\n"
            f"LP to redeem: {lp_amount:,}\n"
            f"≈ {from_raw(exp0, state.mint0_decimals):,} of {short(k.mint0)}\n"
            f"≈ {from_raw(exp1, state.mint1_decimals):,} of {short(k.mint1)}\n"
            f"Max slippage: {slippage_bps / 100:.2f}% · WSOL auto-unwrapped to SOL"
        ),
    )


@router.message(Command("burnlp"))
@user_errors
async def cmd_burnlp(msg: Message, command: CommandObject, svc: Services) -> None:
    args = (command.args or "").split()
    if len(args) != 2:
        raise BotError("Usage: /burnlp <mint> <percent>")
    mint = parse_pubkey(args[0])
    pct = _parse_percent(args[1])
    wallet_name, payer = svc.wallet_for(msg.from_user.id)
    keys = svc.cpmm.derive(mint, WRAPPED_SOL_MINT)
    lp_held = await svc.sol.token_balance(get_associated_token_address(payer.pubkey(), keys.lp_mint))
    lp_amount = int(Decimal(lp_held) * pct / 100)
    if lp_amount <= 0:
        raise BotError(f"Wallet {wallet_name} holds no LP for this pool")

    async def run() -> str:
        sig = await svc.cpmm.burn_lp(payer, keys, lp_amount)
        await svc.store.upsert(str(mint), lp_burned_pct=str(pct))
        return f"🔥 Burned {lp_amount:,} LP. {explorer(sig, svc.cfg.rpc_url)}"

    await ask_confirm(
        msg, svc, wallet=wallet_name, owner_only=True, run=run,
        summary=(
            f"<b>⚠️ Burn LP — irreversible</b>\n{lp_amount:,} LP ({pct}%) will be destroyed. "
            f"The underlying liquidity becomes permanently unwithdrawable."
        ),
    )


# ════════════════════════════════════════════════════════════════════════════
# Entrypoint
# ════════════════════════════════════════════════════════════════════════════


async def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cfg = Config.from_env()
    wallets = WalletRegistry(cfg.wallets_dir, cfg.master_wallet)
    sol = SolanaService(cfg)
    svc = Services(
        cfg=cfg,
        wallets=wallets,
        store=LaunchStore(cfg.state_file),
        sol=sol,
        tokens=TokenService(sol),
        cpmm=RaydiumCpmm(sol, cfg),
    )

    health = await with_retry(lambda: sol.client.get_version(), what="get_version")
    log.info("RPC %s ok (solana-core %s)", cfg.rpc_url, health.value.solana_core)
    for n in wallets.names:
        log.info("wallet %-12s %s", n, wallets.get(n).pubkey())

    bot = Bot(cfg.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    access = AccessMiddleware(cfg.admin_ids)
    dp.message.outer_middleware(access)
    dp.callback_query.outer_middleware(access)
    dp.include_router(router)
    dp["svc"] = svc

    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await sol.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
