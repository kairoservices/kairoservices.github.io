"""Environment-driven configuration."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

from dotenv import load_dotenv
from solders.keypair import Keypair

LAMPORTS_PER_SOL = 1_000_000_000


class ConfigError(RuntimeError):
    pass


def _require(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigError(f"Missing required environment variable: {name}")
    return value


def _load_keypair(raw: str) -> Keypair:
    """Accept either a base58 secret key or a Solana CLI JSON byte array."""
    raw = raw.strip()
    try:
        if raw.startswith("["):
            return Keypair.from_bytes(bytes(json.loads(raw)))
        return Keypair.from_base58_string(raw)
    except Exception as exc:  # noqa: BLE001 - surface a clean config error
        raise ConfigError("PAYER_PRIVATE_KEY is not a valid Solana secret key") from exc


@dataclass(frozen=True, slots=True)
class Config:
    bot_token: str
    allowed_user_ids: frozenset[int]
    cluster: str
    rpc_url: str
    payer: Keypair
    priority_fee_micro_lamports: int
    min_payer_balance_lamports: int
    pinata_jwt: str
    pinata_gateway: str

    @property
    def is_mainnet(self) -> bool:
        return self.cluster == "mainnet-beta"


def load_config() -> Config:
    load_dotenv()

    allowed = frozenset(
        int(part) for part in _require("ALLOWED_USER_IDS").replace(" ", "").split(",") if part
    )
    if not allowed:
        raise ConfigError("ALLOWED_USER_IDS must list at least one Telegram user ID")

    cluster = os.getenv("SOLANA_CLUSTER", "devnet").strip()
    if cluster not in {"devnet", "mainnet-beta"}:
        raise ConfigError("SOLANA_CLUSTER must be 'devnet' or 'mainnet-beta'")

    gateway = os.getenv("PINATA_GATEWAY", "https://gateway.pinata.cloud/ipfs/").strip()
    if not gateway.endswith("/"):
        gateway += "/"

    return Config(
        bot_token=_require("BOT_TOKEN"),
        allowed_user_ids=allowed,
        cluster=cluster,
        rpc_url=_require("SOLANA_RPC_URL"),
        payer=_load_keypair(_require("PAYER_PRIVATE_KEY")),
        priority_fee_micro_lamports=int(os.getenv("PRIORITY_FEE_MICROLAMPORTS", "50000")),
        min_payer_balance_lamports=int(
            float(os.getenv("MIN_PAYER_BALANCE_SOL", "0.05")) * LAMPORTS_PER_SOL
        ),
        pinata_jwt=_require("PINATA_JWT"),
        pinata_gateway=gateway,
    )
