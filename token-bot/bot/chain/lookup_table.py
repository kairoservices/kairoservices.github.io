"""Address Lookup Table (ALT) instructions.

A pump.fun create + dev buy touches ~28 accounts, which exceeds the
1232-byte transaction limit with plain 32-byte keys. Putting the static
pump.fun accounts in an ALT shrinks each one to a 1-byte index.

solders has no builders for these, so they are bincode-encoded here
(ProgramInstruction enum in solana-address-lookup-table-interface).
"""

from __future__ import annotations

import struct

from solders.address_lookup_table_account import ID as ALT_PROGRAM_ID
from solders.address_lookup_table_account import derive_lookup_table_address
from solders.instruction import AccountMeta, Instruction
from solders.pubkey import Pubkey
from solders.system_program import ID as SYS_PROGRAM_ID

_CREATE = 0
_EXTEND = 2
MAX_ADDRESSES_PER_EXTEND = 20  # keeps the extend tx well under the size limit


def create_lookup_table(authority: Pubkey, payer: Pubkey, recent_slot: int) -> tuple[Instruction, Pubkey]:
    table, bump = derive_lookup_table_address(authority, recent_slot)
    data = struct.pack("<IQB", _CREATE, recent_slot, bump)
    accounts = [
        AccountMeta(table, is_signer=False, is_writable=True),
        AccountMeta(authority, is_signer=True, is_writable=False),
        AccountMeta(payer, is_signer=True, is_writable=True),
        AccountMeta(SYS_PROGRAM_ID, is_signer=False, is_writable=False),
    ]
    return Instruction(ALT_PROGRAM_ID, data, accounts), table


def extend_lookup_table(table: Pubkey, authority: Pubkey, payer: Pubkey, addresses: list[Pubkey]) -> Instruction:
    data = struct.pack("<IQ", _EXTEND, len(addresses)) + b"".join(bytes(a) for a in addresses)
    accounts = [
        AccountMeta(table, is_signer=False, is_writable=True),
        AccountMeta(authority, is_signer=True, is_writable=False),
        AccountMeta(payer, is_signer=True, is_writable=True),
        AccountMeta(SYS_PROGRAM_ID, is_signer=False, is_writable=False),
    ]
    return Instruction(ALT_PROGRAM_ID, data, accounts)
