"""Run the standard SPL launch instructions through LiteSVM (local SVM, no network).

The Metaplex program is not bundled with LiteSVM, so its instruction is left
out here; its encoding is checked against mpl-token-metadata separately.
"""

from __future__ import annotations

import unittest

from solders.keypair import Keypair
from solders.litesvm import LiteSVM
from solders.message import MessageV0
from solders.transaction import VersionedTransaction
from spl.token._layouts import ACCOUNT_LAYOUT, MINT_LAYOUT
from spl.token.instructions import get_associated_token_address

from bot.chain.deployer import MINT_ACCOUNT_SIZE, TokenDeployer
from bot.chain.metaplex import TOKEN_METADATA_PROGRAM_ID
from bot.models import TokenParams


class SplLaunchTest(unittest.TestCase):
    def launch(self, revoke: bool):
        svm = LiteSVM()
        payer, mint = Keypair(), Keypair()
        recipient = Keypair().pubkey()
        svm.airdrop(payer.pubkey(), 10**9)
        deployer = TokenDeployer("http://unused", payer, priority_fee_micro_lamports=1, min_payer_balance_lamports=0)
        params = TokenParams(
            name="T", symbol="T", metadata_uri="u", decimals=9, supply=10**9, recipient=recipient,
            revoke_mint=revoke, revoke_freeze=revoke, revoke_update=revoke,
        )
        ixs = [
            ix
            for ix in deployer._build_instructions(
                params, mint.pubkey(), svm.minimum_balance_for_rent_exemption(MINT_ACCOUNT_SIZE)
            )
            if ix.program_id != TOKEN_METADATA_PROGRAM_ID
        ]
        msg = MessageV0.try_compile(payer.pubkey(), ixs, [], svm.latest_blockhash())
        result = svm.send_transaction(VersionedTransaction(msg, [payer, mint]))
        self.assertEqual(type(result).__name__, "TransactionMetadata", result)
        mint_state = MINT_LAYOUT.parse(svm.get_account(mint.pubkey()).data)
        ata = ACCOUNT_LAYOUT.parse(svm.get_account(get_associated_token_address(recipient, mint.pubkey())).data)
        return mint_state, ata, recipient

    def test_revoke_all(self) -> None:
        mint, ata, _ = self.launch(revoke=True)
        self.assertEqual(mint.supply, 10**18)
        self.assertEqual(ata.amount, 10**18)
        self.assertEqual(mint.mint_authority_option, 0)
        self.assertEqual(mint.freeze_authority_option, 0)

    def test_keep_authorities_moves_them_to_recipient(self) -> None:
        mint, _, recipient = self.launch(revoke=False)
        self.assertEqual(bytes(mint.mint_authority), bytes(recipient))
        self.assertEqual(bytes(mint.freeze_authority), bytes(recipient))


if __name__ == "__main__":
    unittest.main()
