"""Diff bot.chain.pump against the official @pump-fun/pump-sdk.

Setup once:  cd tests/pump_parity && npm install
Run:         python -m unittest tests.test_pump_parity
"""

from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

from solders.keypair import Keypair

from bot.chain import pump

PARITY_DIR = Path(__file__).parent / "pump_parity"
# Buyback recipient the SDK picks when Math.random() returns 0.
SDK_FIRST_BUYBACK_RECIPIENT = "5YxQFdt3Tr9zJLvkFccqXVUwhdTWJQc1fFg2YPbxvxeD"


def _as_json(ix) -> dict:
    return {
        "programId": str(ix.program_id),
        "data": bytes(ix.data).hex(),
        "keys": [[str(a.pubkey), a.is_signer, a.is_writable] for a in ix.accounts],
    }


@unittest.skipUnless(
    shutil.which("node") and (PARITY_DIR / "node_modules" / "@pump-fun").exists(),
    "run `npm install` in tests/pump_parity first",
)
class PumpParityTest(unittest.TestCase):
    def test_create_v2_and_buy_match_official_sdk(self) -> None:
        mint, user, creator, fee, buyback = (Keypair().pubkey() for _ in range(5))
        case = {
            "mint": str(mint),
            "user": str(user),
            "creator": str(creator),
            "feeRecipient": str(fee),
            "name": "Parity Tokén",
            "symbol": "PAR",
            "uri": "https://gateway.pinata.cloud/ipfs/QmTest",
            "tokenAmount": 12_345_678_901,
            "solAmount": 200_000_000,
            "minSolOut": 150_000_000,
            "buybackFeeRecipient": str(buyback),
        }
        out = subprocess.run(
            ["node", "reference.cjs"], cwd=PARITY_DIR, input=json.dumps(case),
            capture_output=True, text=True, check=True,
        )
        ref = json.loads(out.stdout)
        ref_create, _ref_ata, ref_buy = ref["createAndBuy"]

        create = pump.create_v2_instruction(
            mint=mint, user=user, creator=creator, name=case["name"], symbol=case["symbol"], uri=case["uri"]
        )
        buy = pump.buy_instruction(
            mint=mint,
            user=user,
            creator=creator,
            fee_recipient=fee,
            buyback_fee_recipient=pump.Pubkey.from_string(SDK_FIRST_BUYBACK_RECIPIENT),
            token_amount=case["tokenAmount"],
            max_sol_cost=case["solAmount"] * 101 // 100,  # SDK adds 1% slippage
        )
        self.assertEqual(_as_json(create), ref_create)
        self.assertEqual(_as_json(buy), ref_buy)

        trade = {"mint": mint, "user": user, "creator": creator, "fee_recipient": fee, "buyback_fee_recipient": buyback}
        self.assertEqual(
            _as_json(pump.buy_instruction(**trade, token_amount=case["tokenAmount"], max_sol_cost=case["solAmount"])),
            ref["buy"],
        )
        self.assertEqual(
            _as_json(
                pump.sell_instruction(**trade, token_amount=case["tokenAmount"], min_sol_output=case["minSolOut"])
            ),
            ref["sell"],
        )


if __name__ == "__main__":
    unittest.main()
