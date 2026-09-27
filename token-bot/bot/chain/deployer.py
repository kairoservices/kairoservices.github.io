"""SPL token + Metaplex metadata deployment in a single atomic transaction."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TypeVar

from solana.exceptions import SolanaRpcException
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed, Finalized
from solana.rpc.core import RPCException
from solana.rpc.models import TxOpts
from solders.address_lookup_table_account import AddressLookupTable, AddressLookupTableAccount
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.instruction import Instruction
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.system_program import ID as SYS_PROGRAM_ID
from solders.system_program import CreateAccountParams, create_account
from solders.transaction import VersionedTransaction
from solders.transaction_status import TransactionConfirmationStatus
from spl.token.constants import ASSOCIATED_TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID, TOKEN_PROGRAM_ID
from spl.token.instructions import (
    AuthorityType,
    create_associated_token_account,
    create_idempotent_associated_token_account,
    get_associated_token_address,
    initialize_mint,
    mint_to,
    set_authority,
    transfer_checked,
)
from spl.token.models import InitializeMintParams, MintToParams, SetAuthorityParams, TransferCheckedParams

from bot.chain import lookup_table, pump
from bot.chain.metaplex import create_metadata_account_v3, find_metadata_pda
from bot.models import DeployResult, PumpDeployResult, PumpLaunchParams, PumpTradeResult, TokenParams

log = logging.getLogger(__name__)

MINT_ACCOUNT_SIZE = 82
COMPUTE_UNIT_LIMIT = 300_000
PUMP_COMPUTE_UNIT_LIMIT = 500_000  # SDK guidance for create + buy
PACKET_DATA_SIZE = 1232
RPC_ATTEMPTS = 4

T = TypeVar("T")


class DeploymentError(Exception):
    """The transaction definitely did not land. Safe to retry."""


class DeploymentUnconfirmed(Exception):
    """The transaction may have landed. Do NOT blindly retry (would create a second token)."""

    def __init__(self, mint: Pubkey, signature: Signature) -> None:
        super().__init__(f"Transaction {signature} not confirmed")
        self.mint = mint
        self.signature = signature


def _describe_rpc_error(exc: RPCException) -> str:
    """Extract a short human message from a preflight / RPC error."""
    err = exc.args[0] if exc.args else exc
    message = getattr(err, "message", None) or str(err)
    logs = getattr(getattr(err, "data", None), "logs", None) or []
    # Program logs usually hold the real reason; keep the last informative line.
    detail = next((line for line in reversed(logs) if "failed" in line or "Error" in line), "")
    return f"{message} {detail}".strip()


class TokenDeployer:
    def __init__(
        self,
        rpc_url: str,
        payer: Keypair,
        *,
        priority_fee_micro_lamports: int,
        min_payer_balance_lamports: int,
        pump_fee_bps: int = 300,
        pump_slippage_bps: int = 1000,
        pump_lookup_table: Pubkey | None = None,
        on_lookup_table_created: Callable[[Pubkey], object] | None = None,
    ) -> None:
        self._client = AsyncClient(rpc_url, commitment=Confirmed)
        self._payer = payer
        self._priority_fee = priority_fee_micro_lamports
        self._min_balance = min_payer_balance_lamports
        self._pump_fee_bps = pump_fee_bps
        self._pump_slippage_bps = pump_slippage_bps
        self._pump_lut_address = pump_lookup_table
        self._pump_lut: AddressLookupTableAccount | None = None
        self._on_lut_created = on_lookup_table_created
        self._lut_lock = asyncio.Lock()

    @property
    def payer_pubkey(self) -> Pubkey:
        return self._payer.pubkey()

    async def close(self) -> None:
        await self._client.close()

    async def _with_retries(self, call: Callable[[], Awaitable[T]]) -> T:
        """Retry idempotent read calls on transport errors with exponential backoff."""
        for attempt in range(1, RPC_ATTEMPTS + 1):
            try:
                return await call()
            except SolanaRpcException as exc:
                if attempt == RPC_ATTEMPTS:
                    raise DeploymentError(f"RPC unreachable: {exc}") from exc
                delay = 2**attempt / 2
                log.warning("RPC error (attempt %d/%d): %s; retrying in %.1fs", attempt, RPC_ATTEMPTS, exc, delay)
                await asyncio.sleep(delay)
        raise AssertionError("unreachable")

    async def payer_balance(self) -> int:
        resp = await self._with_retries(lambda: self._client.get_balance(self.payer_pubkey))
        return resp.value

    def _build_instructions(self, p: TokenParams, mint: Pubkey, mint_rent: int) -> list[Instruction]:
        payer = self.payer_pubkey
        recipient_ata = get_associated_token_address(p.recipient, mint)

        ixs: list[Instruction] = [
            set_compute_unit_limit(COMPUTE_UNIT_LIMIT),
            set_compute_unit_price(self._priority_fee),
            # 1. Allocate the mint account, owned by the SPL Token program.
            create_account(
                CreateAccountParams(
                    from_pubkey=payer,
                    to_pubkey=mint,
                    lamports=mint_rent,
                    space=MINT_ACCOUNT_SIZE,
                    owner=TOKEN_PROGRAM_ID,
                )
            ),
            # 2. Init mint. Payer keeps mint authority until supply is minted.
            #    Freeze authority is either never set (revoked) or handed to the recipient.
            initialize_mint(
                InitializeMintParams(
                    decimals=p.decimals,
                    program_id=TOKEN_PROGRAM_ID,
                    mint=mint,
                    mint_authority=payer,
                    freeze_authority=None if p.revoke_freeze else p.recipient,
                )
            ),
            # 3. Metaplex metadata (requires current mint authority signature).
            create_metadata_account_v3(
                metadata=find_metadata_pda(mint),
                mint=mint,
                mint_authority=payer,
                payer=payer,
                update_authority=p.recipient,
                name=p.name,
                symbol=p.symbol,
                uri=p.metadata_uri,
                is_mutable=not p.revoke_update,
            ),
            # 4. Recipient's token account + full supply.
            create_associated_token_account(payer=payer, owner=p.recipient, mint=mint),
            mint_to(
                MintToParams(
                    program_id=TOKEN_PROGRAM_ID,
                    mint=mint,
                    dest=recipient_ata,
                    mint_authority=payer,
                    amount=p.raw_amount,
                )
            ),
            # 5. Revoke mint authority, or hand it to the recipient.
            set_authority(
                SetAuthorityParams(
                    program_id=TOKEN_PROGRAM_ID,
                    account=mint,
                    authority=AuthorityType.MINT_TOKENS,
                    current_authority=payer,
                    new_authority=None if p.revoke_mint else p.recipient,
                )
            ),
        ]
        return ixs

    async def _await_confirmation(
        self, sig: Signature, last_valid_block_height: int, raw_tx: bytes | None = None
    ) -> None:
        """Poll until confirmed, failed, or blockhash expired.

        Raises DeploymentError if the tx failed or provably expired. If the RPC
        is unreachable, the DeploymentError is chained from SolanaRpcException.
        """
        confirmed_rank = int(TransactionConfirmationStatus.Confirmed)
        while True:
            statuses = await self._with_retries(lambda: self._client.get_signature_statuses([sig]))
            status = statuses.value[0]
            if status is not None:
                if status.err is not None:
                    raise DeploymentError(f"Transaction failed on-chain: {status.err}")
                if status.confirmation_status is not None and int(status.confirmation_status) >= confirmed_rank:
                    return
            height = await self._with_retries(lambda: self._client.get_block_height(Confirmed))
            if height.value > last_valid_block_height:
                # One last look: the tx may have landed in the final valid block.
                final = (
                    await self._with_retries(
                        lambda: self._client.get_signature_statuses([sig], search_transaction_history=True)
                    )
                ).value[0]
                if final is not None and final.err is None:
                    return
                raise DeploymentError("Transaction expired before confirmation (congestion). Nothing was created.")
            if raw_tx is not None:
                # Re-broadcast until confirmed or expired: under congestion leaders drop txs.
                # Same signature, so a duplicate can never land twice.
                try:
                    await self._client.send_raw_transaction(raw_tx, opts=TxOpts(skip_preflight=True, max_retries=0))
                except (RPCException, SolanaRpcException):
                    pass
            await asyncio.sleep(2.0)

    async def _ensure_balance(self, required_lamports: int) -> None:
        balance = await self.payer_balance()
        if balance < required_lamports:
            raise DeploymentError(
                f"Payer wallet {self.payer_pubkey} has {balance / 1e9:.4f} SOL; "
                f"needs at least {required_lamports / 1e9:.4f} SOL."
            )

    async def _send_atomic(
        self,
        instructions: list[Instruction],
        mint_kp: Keypair | None = None,
        lookup_tables: list[AddressLookupTableAccount] | None = None,
    ) -> str:
        """Sign with payer (+ mint keypair), send, and wait for confirmation. Returns the signature."""
        signers = [self._payer] + ([mint_kp] if mint_kp else [])
        mint = mint_kp.pubkey() if mint_kp else Pubkey.default()
        blockhash = await self._with_retries(lambda: self._client.get_latest_blockhash(Confirmed))
        message = MessageV0.try_compile(
            payer=self.payer_pubkey,
            instructions=instructions,
            address_lookup_table_accounts=lookup_tables or [],
            recent_blockhash=blockhash.value.blockhash,
        )
        tx = VersionedTransaction(message, signers)
        size = len(bytes(tx))
        if size > PACKET_DATA_SIZE:
            raise DeploymentError(f"Transaction too large ({size} > {PACKET_DATA_SIZE} bytes); shorten name/URI")
        sig = tx.signatures[0]
        log.info("Sending tx %s (%d bytes) for mint %s", sig, size, mint)

        try:
            await self._client.send_transaction(
                tx,
                opts=TxOpts(skip_confirmation=True, preflight_commitment=Confirmed, max_retries=5),
            )
        except RPCException as exc:
            # Preflight simulation rejected the tx: it was never broadcast.
            raise DeploymentError(_describe_rpc_error(exc)) from exc
        except SolanaRpcException:
            # Transport error mid-send: tx may or may not have reached a leader.
            log.warning("Transport error while sending %s; checking status", sig, exc_info=True)

        try:
            await self._await_confirmation(sig, blockhash.value.last_valid_block_height, bytes(tx))
        except DeploymentError as exc:
            if isinstance(exc.__cause__, SolanaRpcException):
                raise DeploymentUnconfirmed(mint, sig) from exc
            raise
        return str(sig)

    async def deploy(self, p: TokenParams) -> DeployResult:
        """Standard SPL mint + Metaplex metadata."""
        await self._ensure_balance(self._min_balance)
        mint_kp = Keypair()
        rent = await self._with_retries(
            lambda: self._client.get_minimum_balance_for_rent_exemption(MINT_ACCOUNT_SIZE)
        )
        sig = await self._send_atomic(self._build_instructions(p, mint_kp.pubkey(), rent.value), mint_kp)
        log.info("Deployed mint %s (tx %s)", mint_kp.pubkey(), sig)
        return DeployResult(mint=mint_kp.pubkey(), signature=sig)

    # ------------------------------------------------------------------ pump.fun

    async def fetch_pump_global(self) -> pump.PumpGlobal:
        resp = await self._with_retries(lambda: self._client.get_account_info(pump.GLOBAL_PDA))
        if resp.value is None:
            raise DeploymentError("pump.fun program is not available on this cluster")
        return pump.PumpGlobal.decode(bytes(resp.value.data))

    async def fetch_bonding_curve(self, mint: Pubkey) -> pump.BondingCurveState | None:
        resp = await self._with_retries(lambda: self._client.get_account_info(pump.bonding_curve_pda(mint)))
        return pump.BondingCurveState.decode(bytes(resp.value.data)) if resp.value else None

    def _pump_static_accounts(self, g: pump.PumpGlobal) -> list[Pubkey]:
        """Accounts shared by every launch from this bot: candidates for the lookup table."""
        zero = Pubkey.default()
        fixed = [
            pump.GLOBAL_PDA,
            pump.MINT_AUTHORITY_PDA,
            pump.EVENT_AUTHORITY_PDA,
            pump.GLOBAL_VOLUME_ACCUMULATOR_PDA,
            pump.FEE_CONFIG_PDA,
            pump.PUMP_FEE_PROGRAM_ID,
            pump.MAYHEM_PROGRAM_ID,
            pump.MAYHEM_GLOBAL_PARAMS_PDA,
            pump.MAYHEM_SOL_VAULT_PDA,
            pump.user_volume_accumulator_pda(self.payer_pubkey),
            SYS_PROGRAM_ID,
            ASSOCIATED_TOKEN_PROGRAM_ID,
            TOKEN_2022_PROGRAM_ID,
            pump.PUMP_PROGRAM_ID,
        ]
        recipients = [g.fee_recipient, *g.fee_recipients, *g.buyback_fee_recipients]
        return list(dict.fromkeys(k for k in fixed + recipients if k != zero))

    async def _fetch_lookup_table(self, address: Pubkey) -> AddressLookupTableAccount | None:
        resp = await self._with_retries(lambda: self._client.get_account_info(address))
        if resp.value is None:
            return None
        table = AddressLookupTable.deserialize(bytes(resp.value.data))
        return AddressLookupTableAccount(address, list(table.addresses))

    async def _ensure_pump_lookup_table(self, g: pump.PumpGlobal) -> AddressLookupTableAccount:
        """Load (or create once) the bot's ALT and extend it if pump.fun added fee recipients."""
        try:
            return await self._ensure_pump_lookup_table_locked(g)
        except DeploymentUnconfirmed as exc:
            # No coin exists yet; retrying only risks a spare lookup table (small, reclaimable rent).
            raise DeploymentError(f"Lookup table setup not confirmed (tx {exc.signature}). Retry.") from exc

    async def _ensure_pump_lookup_table_locked(self, g: pump.PumpGlobal) -> AddressLookupTableAccount:
        async with self._lut_lock:
            required = self._pump_static_accounts(g)
            if self._pump_lut and set(required) <= set(self._pump_lut.addresses):
                return self._pump_lut

            payer = self.payer_pubkey
            table = await self._fetch_lookup_table(self._pump_lut_address) if self._pump_lut_address else None
            if table is None:
                slot = await self._with_retries(lambda: self._client.get_slot(Finalized))
                create_ix, address = lookup_table.create_lookup_table(payer, payer, slot.value)
                await self._send_atomic([create_ix])
                log.info("Created pump.fun lookup table %s", address)
                self._pump_lut_address = address
                if self._on_lut_created:
                    self._on_lut_created(address)
                table = AddressLookupTableAccount(address, [])

            missing = [k for k in required if k not in set(table.addresses)]
            for i in range(0, len(missing), lookup_table.MAX_ADDRESSES_PER_EXTEND):
                chunk = missing[i : i + lookup_table.MAX_ADDRESSES_PER_EXTEND]
                await self._send_atomic([lookup_table.extend_lookup_table(table.key, payer, payer, chunk)])
            if missing:
                # New entries are usable only from the slot after the extend.
                start = (await self._with_retries(lambda: self._client.get_slot(Confirmed))).value
                while (await self._with_retries(lambda: self._client.get_slot(Confirmed))).value <= start:
                    await asyncio.sleep(0.4)
                table = await self._fetch_lookup_table(table.key)
                if table is None:
                    raise DeploymentError("Lookup table disappeared after extending")

            self._pump_lut = table
            return table

    def _build_pump_instructions(
        self, p: PumpLaunchParams, g: pump.PumpGlobal, mint: Pubkey, token_amount: int
    ) -> list[Instruction]:
        payer = self.payer_pubkey
        ixs = [
            set_compute_unit_limit(PUMP_COMPUTE_UNIT_LIMIT),
            set_compute_unit_price(self._priority_fee),
            # Creator = recipient, so creator fees accrue to the user's wallet, not the bot's.
            pump.create_v2_instruction(
                mint=mint, user=payer, creator=p.recipient, name=p.name, symbol=p.symbol, uri=p.metadata_uri
            ),
        ]
        if token_amount == 0:
            return ixs

        ixs += [
            create_idempotent_associated_token_account(payer, payer, mint, TOKEN_2022_PROGRAM_ID),
            pump.buy_instruction(
                mint=mint,
                user=payer,
                creator=p.recipient,
                fee_recipient=g.pick_fee_recipient(),
                buyback_fee_recipient=g.pick_buyback_fee_recipient(),
                token_amount=token_amount,
                max_sol_cost=p.dev_buy_lamports,
            ),
        ]
        if p.recipient != payer:
            # Hand the bought tokens to the creator's wallet in the same transaction.
            ixs += [
                create_idempotent_associated_token_account(payer, p.recipient, mint, TOKEN_2022_PROGRAM_ID),
                transfer_checked(
                    TransferCheckedParams(
                        program_id=TOKEN_2022_PROGRAM_ID,
                        source=pump.ata_2022(payer, mint),
                        mint=mint,
                        dest=pump.ata_2022(p.recipient, mint),
                        owner=payer,
                        amount=token_amount,
                        decimals=pump.PUMP_DECIMALS,
                    )
                ),
            ]
        return ixs

    async def deploy_pump(self, p: PumpLaunchParams) -> PumpDeployResult:
        """pump.fun bonding-curve coin, with the creator's first buy in the same transaction.

        Atomic: if the buy fails, the coin is not created either.
        """
        g = await self.fetch_pump_global()
        if not g.create_v2_enabled:
            raise DeploymentError("pump.fun create_v2 is currently disabled")
        await self._ensure_balance(self._min_balance + p.dev_buy_lamports)

        token_amount = pump.quote_first_buy(g, p.dev_buy_lamports, self._pump_fee_bps) if p.dev_buy_lamports else 0
        mint_kp = Keypair()
        mint = mint_kp.pubkey()
        lut = await self._ensure_pump_lookup_table(g)
        sig = await self._send_atomic(self._build_pump_instructions(p, g, mint, token_amount), mint_kp, [lut])
        log.info("Launched pump.fun coin %s (tx %s), dev buy %d tokens", mint, sig, token_amount)

        curve = None
        try:
            curve = await self.fetch_bonding_curve(mint)
        except DeploymentError:
            log.warning("Could not read bonding curve for %s after launch", mint)
        return PumpDeployResult(
            mint=mint,
            signature=sig,
            dev_tokens=token_amount,
            initial_market_cap_lamports=g.initial_market_cap_lamports,
            market_cap_lamports=curve.market_cap_lamports if curve else None,
            curve_progress=curve.progress(g.initial_real_token_reserves) if curve else None,
        )

    # ------------------------------------------------------------------ pump.fun trading (bot wallet)

    async def _mint_token_program(self, mint: Pubkey) -> Pubkey:
        resp = await self._with_retries(lambda: self._client.get_account_info(mint))
        if resp.value is None:
            raise DeploymentError("Mint not found")
        return resp.value.owner

    async def token_balance(self, mint: Pubkey, token_program: Pubkey) -> int:
        account = pump.ata(self.payer_pubkey, mint, token_program)
        resp = await self._with_retries(lambda: self._client.get_account_info(account))
        if resp.value is None:
            return 0
        # SPL token account layout: amount is a u64 at offset 64 (same for Token-2022).
        return int.from_bytes(bytes(resp.value.data)[64:72], "little")

    async def _tradable_curve(self, mint: Pubkey) -> pump.BondingCurveState:
        curve = await self.fetch_bonding_curve(mint)
        if curve is None:
            raise DeploymentError("Not a pump.fun coin (no bonding curve)")
        if curve.complete:
            raise DeploymentError("Coin has graduated from the bonding curve; trade it on PumpSwap instead")
        return curve

    async def _trade_result(
        self, mint: Pubkey, token_program: Pubkey, g: pump.PumpGlobal, sig: str, side: str, tokens: int, sol: int
    ) -> PumpTradeResult:
        curve, left = None, 0
        try:
            curve = await self.fetch_bonding_curve(mint)
            left = await self.token_balance(mint, token_program)
        except DeploymentError:
            log.warning("Post-trade read failed for %s", mint)
        return PumpTradeResult(
            signature=sig,
            side=side,
            token_amount=tokens,
            sol_lamports=sol,
            tokens_left=left,
            market_cap_lamports=curve.market_cap_lamports if curve else None,
            curve_progress=curve.progress(g.initial_real_token_reserves) if curve else None,
        )

    async def pump_buy(self, mint: Pubkey, sol_lamports: int) -> PumpTradeResult:
        """Buy on the bonding curve with the bot wallet. Spends at most sol_lamports + slippage."""
        g = await self.fetch_pump_global()
        curve = await self._tradable_curve(mint)
        token_program = await self._mint_token_program(mint)
        await self._ensure_balance(self._min_balance + sol_lamports)

        tokens = pump.quote_buy(g, curve, sol_lamports, self._pump_fee_bps)
        if tokens == 0:
            raise DeploymentError("Amount too small")
        max_cost = sol_lamports * (10_000 + self._pump_slippage_bps) // 10_000
        payer = self.payer_pubkey
        ixs = [
            set_compute_unit_limit(COMPUTE_UNIT_LIMIT),
            set_compute_unit_price(self._priority_fee),
            create_idempotent_associated_token_account(payer, payer, mint, token_program),
            pump.buy_instruction(
                mint=mint,
                user=payer,
                creator=curve.creator,
                fee_recipient=g.pick_fee_recipient(),
                buyback_fee_recipient=g.pick_buyback_fee_recipient(),
                token_amount=tokens,
                max_sol_cost=max_cost,
                token_program=token_program,
            ),
        ]
        lut = await self._ensure_pump_lookup_table(g)
        sig = await self._send_trade(ixs, [lut], mint)
        return await self._trade_result(mint, token_program, g, sig, "buy", tokens, max_cost)

    async def pump_sell(self, mint: Pubkey, percent: int) -> PumpTradeResult:
        """Sell `percent` (1-100) of the bot wallet's balance back into the bonding curve."""
        if not 1 <= percent <= 100:
            raise DeploymentError("Percent must be 1-100")
        g = await self.fetch_pump_global()
        curve = await self._tradable_curve(mint)
        token_program = await self._mint_token_program(mint)
        await self._ensure_balance(10_000_000)  # fees only

        balance = await self.token_balance(mint, token_program)
        tokens = balance if percent == 100 else balance * percent // 100
        if tokens == 0:
            raise DeploymentError(f"Bot wallet {self.payer_pubkey} holds none of this token")
        expected = pump.quote_sell(g, curve, tokens, self._pump_fee_bps)
        min_out = expected * (10_000 - self._pump_slippage_bps) // 10_000
        ixs = [
            set_compute_unit_limit(COMPUTE_UNIT_LIMIT),
            set_compute_unit_price(self._priority_fee),
            pump.sell_instruction(
                mint=mint,
                user=self.payer_pubkey,
                creator=curve.creator,
                fee_recipient=g.pick_fee_recipient(),
                buyback_fee_recipient=g.pick_buyback_fee_recipient(),
                token_amount=tokens,
                min_sol_output=min_out,
                token_program=token_program,
            ),
        ]
        lut = await self._ensure_pump_lookup_table(g)
        sig = await self._send_trade(ixs, [lut], mint)
        return await self._trade_result(mint, token_program, g, sig, "sell", tokens, min_out)

    async def _send_trade(self, ixs: list[Instruction], luts: list[AddressLookupTableAccount], mint: Pubkey) -> str:
        try:
            return await self._send_atomic(ixs, lookup_tables=luts)
        except DeploymentUnconfirmed as exc:
            # Re-label with the real mint for the handler's message.
            raise DeploymentUnconfirmed(mint, exc.signature) from exc
