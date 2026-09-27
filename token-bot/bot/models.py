"""Domain objects shared between handlers and the chain layer."""

from __future__ import annotations

from dataclasses import dataclass

from solders.pubkey import Pubkey

# On-chain limits enforced by the Token Metadata program (bytes, UTF-8).
MAX_NAME_LEN = 32
MAX_SYMBOL_LEN = 10
MAX_URI_LEN = 200
MAX_DECIMALS = 9
U64_MAX = 2**64 - 1

DEFAULT_DECIMALS = 9
DEFAULT_SUPPLY = 1_000_000_000


@dataclass(frozen=True, slots=True)
class TokenParams:
    name: str
    symbol: str
    metadata_uri: str
    decimals: int
    supply: int  # whole tokens, before applying decimals
    recipient: Pubkey  # receives the supply and any authority that is kept
    revoke_mint: bool
    revoke_freeze: bool
    revoke_update: bool

    @property
    def raw_amount(self) -> int:
        return self.supply * 10**self.decimals


@dataclass(frozen=True, slots=True)
class DeployResult:
    mint: Pubkey
    signature: str
