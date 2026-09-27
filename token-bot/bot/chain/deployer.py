"""SPL token + Metaplex metadata deployment in a single atomic transaction."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TypeVar

from solana.exceptions import SolanaRpcException
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solana.rpc.core import RPCException
from solana.rpc.models import TxOpts
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.instruction import Instruction
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.system_program import CreateAccountParams, create_account
from solders.transaction import VersionedTransaction
from solders.transaction_status import TransactionConfirmationStatus
from spl.token.constants import TOKEN_PROGRAM_ID
from spl.token.instructions import (
    AuthorityType,
    create_associated_token_account,
    get_associated_token_address,
    initialize_mint,
    mint_to,
    set_authority,
)
from spl.token.models import InitializeMintParams, MintToParams, SetAuthorityParams

from bot.chain.metaplex import create_metadata_account_v3, find_metadata_pda
from bot.models import DeployResult, TokenParams

log = logging.getLogger(__name__)

MINT_ACCOUNT_SIZE = 82
COMPUTE_UNIT_LIMIT = 300_000
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
    ) -> None:
        self._client = AsyncClient(rpc_url, commitment=Confirmed)
        self._payer = payer
        self._priority_fee = priority_fee_micro_lamports
        self._min_balance = min_payer_balance_lamports

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

    async def _await_confirmation(self, sig: Signature, last_valid_block_height: int) -> None:
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
            await asyncio.sleep(1.0)

    async def deploy(self, p: TokenParams) -> DeployResult:
        balance = await self.payer_balance()
        if balance < self._min_balance:
            raise DeploymentError(
                f"Payer wallet {self.payer_pubkey} has {balance / 1e9:.4f} SOL; "
                f"needs at least {self._min_balance / 1e9:.4f} SOL."
            )

        mint_kp = Keypair()
        mint = mint_kp.pubkey()

        rent = await self._with_retries(
            lambda: self._client.get_minimum_balance_for_rent_exemption(MINT_ACCOUNT_SIZE)
        )
        blockhash = await self._with_retries(lambda: self._client.get_latest_blockhash(Confirmed))

        message = MessageV0.try_compile(
            payer=self.payer_pubkey,
            instructions=self._build_instructions(p, mint, rent.value),
            address_lookup_table_accounts=[],
            recent_blockhash=blockhash.value.blockhash,
        )
        tx = VersionedTransaction(message, [self._payer, mint_kp])
        sig = tx.signatures[0]
        log.info("Sending deploy tx %s for mint %s", sig, mint)

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
            await self._await_confirmation(sig, blockhash.value.last_valid_block_height)
        except DeploymentError as exc:
            if isinstance(exc.__cause__, SolanaRpcException):
                raise DeploymentUnconfirmed(mint, sig) from exc
            raise

        log.info("Deployed mint %s (tx %s)", mint, sig)
        return DeployResult(mint=mint, signature=str(sig))
