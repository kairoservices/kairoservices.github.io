"""pump.fun bonding-curve launch: `create_v2` + creator's first `buy`.

Ported from the official `@pump-fun/pump-sdk` (v2.0.0, IDL in
`src/idl/pump.json`). pump.fun changes its account lists from time to time;
`tests/pump_parity` rebuilds the same instructions with the official SDK and
diffs them against this module. Re-run it after every SDK release.

Coins created by `create_v2` are Token-2022 mints with a fixed supply
(1B, 6 decimals). The program owns the mint and revokes mint/freeze
authority itself, so those launch options do not apply here.
"""

from __future__ import annotations

import random
import struct
from dataclasses import dataclass

from solders.instruction import AccountMeta, Instruction
from solders.pubkey import Pubkey
from solders.system_program import ID as SYS_PROGRAM_ID
from spl.token.constants import ASSOCIATED_TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID
from spl.token.instructions import get_associated_token_address

PUMP_PROGRAM_ID = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
PUMP_FEE_PROGRAM_ID = Pubkey.from_string("pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ")
MAYHEM_PROGRAM_ID = Pubkey.from_string("MAyhSmzXzV1pTf7LsNkrNwkWKTo4ougAJ1PPg47MD4e")

PUMP_DECIMALS = 6
PUMP_TOTAL_SUPPLY = 1_000_000_000  # whole tokens; fixed by the program
_ZERO = Pubkey.default()

_CREATE_V2_DISCRIMINATOR = bytes([214, 144, 76, 236, 95, 139, 49, 180])
_BUY_DISCRIMINATOR = bytes([102, 6, 61, 18, 1, 218, 235, 234])


def _pda(seeds: list[bytes], program: Pubkey = PUMP_PROGRAM_ID) -> Pubkey:
    return Pubkey.find_program_address(seeds, program)[0]


GLOBAL_PDA = _pda([b"global"])
MINT_AUTHORITY_PDA = _pda([b"mint-authority"])
EVENT_AUTHORITY_PDA = _pda([b"__event_authority"])
GLOBAL_VOLUME_ACCUMULATOR_PDA = _pda([b"global_volume_accumulator"])
FEE_CONFIG_PDA = _pda([b"fee_config", bytes(PUMP_PROGRAM_ID)], PUMP_FEE_PROGRAM_ID)
MAYHEM_GLOBAL_PARAMS_PDA = _pda([b"global-params"], MAYHEM_PROGRAM_ID)
MAYHEM_SOL_VAULT_PDA = _pda([b"sol-vault"], MAYHEM_PROGRAM_ID)


def bonding_curve_pda(mint: Pubkey) -> Pubkey:
    return _pda([b"bonding-curve", bytes(mint)])


def bonding_curve_v2_pda(mint: Pubkey) -> Pubkey:
    return _pda([b"bonding-curve-v2", bytes(mint)])


def creator_vault_pda(creator: Pubkey) -> Pubkey:
    return _pda([b"creator-vault", bytes(creator)])


def user_volume_accumulator_pda(user: Pubkey) -> Pubkey:
    return _pda([b"user_volume_accumulator", bytes(user)])


def ata_2022(owner: Pubkey, mint: Pubkey) -> Pubkey:
    return get_associated_token_address(owner, mint, TOKEN_2022_PROGRAM_ID)


# --------------------------------------------------------------------------- account decoding


class _Reader:
    def __init__(self, data: bytes, offset: int = 8) -> None:  # skip Anchor discriminator
        self._data = data
        self._pos = offset

    def u64(self) -> int:
        (value,) = struct.unpack_from("<Q", self._data, self._pos)
        self._pos += 8
        return value

    def bool(self) -> bool:
        value = self._data[self._pos] != 0
        self._pos += 1
        return value

    def pubkey(self) -> Pubkey:
        value = Pubkey.from_bytes(self._data[self._pos : self._pos + 32])
        self._pos += 32
        return value

    def pubkeys(self, n: int) -> list[Pubkey]:
        return [self.pubkey() for _ in range(n)]


@dataclass(frozen=True, slots=True)
class PumpGlobal:
    fee_recipient: Pubkey
    fee_recipients: list[Pubkey]
    initial_virtual_token_reserves: int
    initial_virtual_sol_reserves: int
    initial_real_token_reserves: int
    token_total_supply: int
    fee_basis_points: int
    creator_fee_basis_points: int
    create_v2_enabled: bool
    buyback_fee_recipients: list[Pubkey]
    buyback_basis_points: int

    @classmethod
    def decode(cls, data: bytes) -> PumpGlobal:
        r = _Reader(data)
        r.bool()  # initialized
        r.pubkey()  # authority
        fee_recipient = r.pubkey()
        init_vt, init_vs, init_rt, total_supply, fee_bps = (r.u64() for _ in range(5))
        r.pubkey()  # withdraw_authority
        r.bool()  # enable_migrate
        r.u64()  # pool_migration_fee
        creator_fee_bps = r.u64()
        fee_recipients = r.pubkeys(7)
        r.pubkey()  # set_creator_authority
        r.pubkey()  # admin_set_creator_authority
        create_v2_enabled = r.bool()
        r.pubkey()  # whitelist_pda
        r.pubkey()  # reserved_fee_recipient
        r.bool()  # mayhem_mode_enabled
        r.pubkeys(7)  # reserved_fee_recipients
        r.bool()  # is_cashback_enabled
        buyback_fee_recipients = r.pubkeys(8)
        buyback_bps = r.u64()
        return cls(
            fee_recipient=fee_recipient,
            fee_recipients=fee_recipients,
            initial_virtual_token_reserves=init_vt,
            initial_virtual_sol_reserves=init_vs,
            initial_real_token_reserves=init_rt,
            token_total_supply=total_supply,
            fee_basis_points=fee_bps,
            creator_fee_basis_points=creator_fee_bps,
            create_v2_enabled=create_v2_enabled,
            buyback_fee_recipients=buyback_fee_recipients,
            buyback_basis_points=buyback_bps,
        )

    def pick_fee_recipient(self) -> Pubkey:
        # Same as the SDK: any listed recipient is accepted; spread load randomly.
        return random.choice([k for k in [self.fee_recipient, *self.fee_recipients] if k != _ZERO])

    def pick_buyback_fee_recipient(self) -> Pubkey:
        return random.choice([k for k in self.buyback_fee_recipients if k != _ZERO])

    @property
    def initial_market_cap_lamports(self) -> int:
        return market_cap_lamports(
            self.initial_virtual_sol_reserves, self.initial_virtual_token_reserves, self.token_total_supply
        )


@dataclass(frozen=True, slots=True)
class BondingCurveState:
    virtual_token_reserves: int
    virtual_sol_reserves: int
    real_token_reserves: int
    real_sol_reserves: int
    token_total_supply: int
    complete: bool

    @classmethod
    def decode(cls, data: bytes) -> BondingCurveState:
        r = _Reader(data)
        vt, vs, rt, rs, supply = (r.u64() for _ in range(5))
        return cls(vt, vs, rt, rs, supply, r.bool())

    @property
    def market_cap_lamports(self) -> int:
        return market_cap_lamports(self.virtual_sol_reserves, self.virtual_token_reserves, self.token_total_supply)

    def progress(self, initial_real_token_reserves: int) -> float:
        """Share of the curve's sellable tokens already bought (graduation at 100%)."""
        if initial_real_token_reserves == 0:
            return 0.0
        return 1 - self.real_token_reserves / initial_real_token_reserves


# --------------------------------------------------------------------------- math


def market_cap_lamports(virtual_sol: int, virtual_token: int, supply: int) -> int:
    return virtual_sol * supply // virtual_token


def quote_first_buy(g: PumpGlobal, sol_lamports: int, assumed_fee_bps: int) -> int:
    """Tokens a first buy of `sol_lamports` (fees included) receives on a fresh curve.

    The exact fee comes from pump-fees' market-cap tiers. We divide by a
    fee rate that is at least as high as the real one, so the program's
    actual cost stays under `max_sol_cost = sol_lamports`. If the real rate
    is higher, preflight fails with a slippage error and nothing is spent.
    """
    fee_bps = max(
        assumed_fee_bps,
        g.fee_basis_points + g.creator_fee_basis_points + g.buyback_basis_points,
    )
    net_in = (sol_lamports - 1) * 10_000 // (10_000 + fee_bps)
    vt, vs = g.initial_virtual_token_reserves, g.initial_virtual_sol_reserves
    tokens = vt - (vt * vs) // (vs + net_in) - 1  # constant product, rounded down
    return max(0, min(tokens, g.initial_real_token_reserves))


def market_cap_after_first_buy(g: PumpGlobal, token_amount: int) -> int:
    """Curve market cap (lamports) once the first buy of `token_amount` has executed."""
    vt, vs = g.initial_virtual_token_reserves, g.initial_virtual_sol_reserves
    new_vt = vt - token_amount
    new_vs = vt * vs // new_vt  # constant product k = vt * vs
    return market_cap_lamports(new_vs, new_vt, g.token_total_supply)


# --------------------------------------------------------------------------- instructions


def _borsh_string(value: str) -> bytes:
    encoded = value.encode("utf-8")
    return struct.pack("<I", len(encoded)) + encoded


def create_v2_instruction(
    *, mint: Pubkey, user: Pubkey, creator: Pubkey, name: str, symbol: str, uri: str
) -> Instruction:
    data = b"".join(
        [
            _CREATE_V2_DISCRIMINATOR,
            _borsh_string(name),
            _borsh_string(symbol),
            _borsh_string(uri),
            bytes(creator),
            b"\x00",  # is_mayhem_mode: false
            b"\x00",  # is_cashback_enabled: OptionBool(false) (cashback is deprecated)
            struct.pack("<Q", 0),  # creator_fee_bps: OptionU64(0) = schedule rate
            b"\x00",  # is_holder_reward: OptionBool(false)
        ]
    )
    curve = bonding_curve_pda(mint)
    accounts = [
        AccountMeta(mint, is_signer=True, is_writable=True),
        AccountMeta(MINT_AUTHORITY_PDA, is_signer=False, is_writable=False),
        AccountMeta(curve, is_signer=False, is_writable=True),
        AccountMeta(ata_2022(curve, mint), is_signer=False, is_writable=True),
        AccountMeta(GLOBAL_PDA, is_signer=False, is_writable=False),
        AccountMeta(user, is_signer=True, is_writable=True),
        AccountMeta(SYS_PROGRAM_ID, is_signer=False, is_writable=False),
        AccountMeta(TOKEN_2022_PROGRAM_ID, is_signer=False, is_writable=False),
        AccountMeta(ASSOCIATED_TOKEN_PROGRAM_ID, is_signer=False, is_writable=False),
        AccountMeta(MAYHEM_PROGRAM_ID, is_signer=False, is_writable=True),
        AccountMeta(MAYHEM_GLOBAL_PARAMS_PDA, is_signer=False, is_writable=False),
        AccountMeta(MAYHEM_SOL_VAULT_PDA, is_signer=False, is_writable=True),
        AccountMeta(_pda([b"mayhem-state", bytes(mint)], MAYHEM_PROGRAM_ID), is_signer=False, is_writable=True),
        AccountMeta(ata_2022(MAYHEM_SOL_VAULT_PDA, mint), is_signer=False, is_writable=True),
        AccountMeta(EVENT_AUTHORITY_PDA, is_signer=False, is_writable=False),
        AccountMeta(PUMP_PROGRAM_ID, is_signer=False, is_writable=False),
    ]
    return Instruction(PUMP_PROGRAM_ID, data, accounts)


def buy_instruction(
    *,
    mint: Pubkey,
    user: Pubkey,
    creator: Pubkey,
    fee_recipient: Pubkey,
    buyback_fee_recipient: Pubkey,
    token_amount: int,
    max_sol_cost: int,
) -> Instruction:
    data = _BUY_DISCRIMINATOR + struct.pack("<QQ", token_amount, max_sol_cost) + b"\x01"  # track_volume: true
    curve = bonding_curve_pda(mint)
    accounts = [
        AccountMeta(GLOBAL_PDA, is_signer=False, is_writable=False),
        AccountMeta(fee_recipient, is_signer=False, is_writable=True),
        AccountMeta(mint, is_signer=False, is_writable=False),
        AccountMeta(curve, is_signer=False, is_writable=True),
        AccountMeta(ata_2022(curve, mint), is_signer=False, is_writable=True),
        AccountMeta(ata_2022(user, mint), is_signer=False, is_writable=True),
        AccountMeta(user, is_signer=True, is_writable=True),
        AccountMeta(SYS_PROGRAM_ID, is_signer=False, is_writable=False),
        AccountMeta(TOKEN_2022_PROGRAM_ID, is_signer=False, is_writable=False),
        AccountMeta(creator_vault_pda(creator), is_signer=False, is_writable=True),
        AccountMeta(EVENT_AUTHORITY_PDA, is_signer=False, is_writable=False),
        AccountMeta(PUMP_PROGRAM_ID, is_signer=False, is_writable=False),
        AccountMeta(GLOBAL_VOLUME_ACCUMULATOR_PDA, is_signer=False, is_writable=False),
        AccountMeta(user_volume_accumulator_pda(user), is_signer=False, is_writable=True),
        AccountMeta(FEE_CONFIG_PDA, is_signer=False, is_writable=False),
        AccountMeta(PUMP_FEE_PROGRAM_ID, is_signer=False, is_writable=False),
        # Remaining accounts the program expects after the IDL list (see SDK getBuyInstructionInternal).
        AccountMeta(bonding_curve_v2_pda(mint), is_signer=False, is_writable=False),
        AccountMeta(buyback_fee_recipient, is_signer=False, is_writable=True),
    ]
    return Instruction(PUMP_PROGRAM_ID, data, accounts)
