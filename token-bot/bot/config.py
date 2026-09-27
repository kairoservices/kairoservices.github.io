"""Environment-driven configuration."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv
from solders.keypair import Keypair
from solders.pubkey import Pubkey

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
    pump_enabled: bool
    pump_fee_bps: int
    pump_max_dev_buy_lamports: int
    pump_lookup_table: Pubkey | None
    pump_lookup_table_file: Path

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

    lut_file = Path(os.getenv("PUMP_LOOKUP_TABLE_FILE", "pump_lookup_table.txt"))
    lut_raw = os.getenv("PUMP_LOOKUP_TABLE", "").strip()
    if not lut_raw and lut_file.exists():
        lut_raw = lut_file.read_text().strip()
    try:
        lut = Pubkey.from_string(lut_raw) if lut_raw else None
    except ValueError as exc:
        raise ConfigError("PUMP_LOOKUP_TABLE is not a valid address") from exc

    return Config(
        bot_token=_require("BOT_TOKEN"),
        allowed_user_ids=allowed,
        cluster=cluster,
        rpc_url=_require("SOLANA_RPC_URL"),
        payer=_load_keypair(_require("PAYER_PRIVATE_KEY")),
        priority_fee_micro_lamports=int(os.getenv("PRIORITY_FEE_MICROLAMPORTS", "50000")),
        min_payer_balance_lamports=int(
            Decimal(os.getenv("MIN_PAYER_BALANCE_SOL", "0.05")) * LAMPORTS_PER_SOL
        ),
        pinata_jwt=_require("PINATA_JWT"),
        pinata_gateway=gateway,
        pump_enabled=os.getenv("PUMP_ENABLED", "true").strip().lower() in {"1", "true", "yes"},
        pump_fee_bps=int(os.getenv("PUMP_FEE_BPS", "300")),
        pump_max_dev_buy_lamports=int(Decimal(os.getenv("PUMP_MAX_DEV_BUY_SOL", "5")) * LAMPORTS_PER_SOL),
        pump_lookup_table=lut,
        pump_lookup_table_file=lut_file,
    )
