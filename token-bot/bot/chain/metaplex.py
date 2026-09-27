"""Minimal Metaplex Token Metadata instruction builder.

There is no maintained Python SDK for mpl-token-metadata, so the
CreateMetadataAccountV3 instruction is Borsh-encoded by hand here.
Layout reference: mpl-token-metadata `CreateMetadataAccountArgsV3`.
"""

from __future__ import annotations

import struct

from solders.instruction import AccountMeta, Instruction
from solders.pubkey import Pubkey
from solders.system_program import ID as SYS_PROGRAM_ID
from solders.sysvar import RENT

TOKEN_METADATA_PROGRAM_ID = Pubkey.from_string("metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s")

_CREATE_METADATA_ACCOUNT_V3 = 33
_NONE = b"\x00"


def find_metadata_pda(mint: Pubkey) -> Pubkey:
    pda, _bump = Pubkey.find_program_address(
        [b"metadata", bytes(TOKEN_METADATA_PROGRAM_ID), bytes(mint)],
        TOKEN_METADATA_PROGRAM_ID,
    )
    return pda


def _borsh_string(value: str) -> bytes:
    encoded = value.encode("utf-8")
    return struct.pack("<I", len(encoded)) + encoded


def create_metadata_account_v3(
    *,
    metadata: Pubkey,
    mint: Pubkey,
    mint_authority: Pubkey,
    payer: Pubkey,
    update_authority: Pubkey,
    name: str,
    symbol: str,
    uri: str,
    is_mutable: bool,
    seller_fee_basis_points: int = 0,
) -> Instruction:
    """Build CreateMetadataAccountV3.

    `is_mutable=False` permanently locks metadata, which is how the
    "revoke update authority" option is implemented.
    """
    data = b"".join(
        [
            bytes([_CREATE_METADATA_ACCOUNT_V3]),
            # DataV2
            _borsh_string(name),
            _borsh_string(symbol),
            _borsh_string(uri),
            struct.pack("<H", seller_fee_basis_points),
            _NONE,  # creators: Option<Vec<Creator>>
            _NONE,  # collection: Option<Collection>
            _NONE,  # uses: Option<Uses>
            # CreateMetadataAccountArgsV3
            struct.pack("<?", is_mutable),
            _NONE,  # collection_details: Option<CollectionDetails>
        ]
    )
    accounts = [
        AccountMeta(metadata, is_signer=False, is_writable=True),
        AccountMeta(mint, is_signer=False, is_writable=False),
        AccountMeta(mint_authority, is_signer=True, is_writable=False),
        AccountMeta(payer, is_signer=True, is_writable=True),
        # Update authority need not sign; it may be the end user's wallet.
        AccountMeta(update_authority, is_signer=False, is_writable=False),
        AccountMeta(SYS_PROGRAM_ID, is_signer=False, is_writable=False),
        AccountMeta(RENT, is_signer=False, is_writable=False),
    ]
    return Instruction(TOKEN_METADATA_PROGRAM_ID, data, accounts)
