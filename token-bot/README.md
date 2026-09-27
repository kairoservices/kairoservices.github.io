# Token Deployment Assistant (Telegram bot)

An aiogram 3 bot that walks through launching a Solana token step by step and deploys it.
There are two launch types:

| | **pump.fun bonding curve** | **Standard SPL** |
|---|---|---|
| Price at launch | Curve base market cap (about 28 SOL of virtual reserves) | None until you add a liquidity pool |
| Supply / decimals | Fixed: 1,000,000,000 / 6 (Token-2022) | You choose (default 1B / 9) |
| Authorities | Mint and freeze revoked by pump.fun | Revoke or keep mint, freeze, and update authority |
| Dev buy | Optional, runs in the **same transaction** as the create | n/a |
| Dashboard | pump.fun, DexScreener, Solscan | Raydium create pool, Solana Explorer, Solscan |

## Flow

`/launch`, then:

1. Name, 2. Symbol, 3. Description (optional), 4. Logo (upload, URL, or skip)
5. Launch type
   - **pump.fun**: 6. Creator wallet, 7. Dev buy amount in SOL (buttons or typed; 0 = none)
   - **SPL**: tokenomics (recommended or custom), recipient wallet, authority toggles
8. Summary with estimates (pump: starting market cap, tokens received, market cap after the buy), then **Deploy**

`/cancel` aborts at any step. If a transaction provably did not land (preflight rejected, blockhash expired),
the summary comes back with **Retry**. If the RPC fails after sending, the bot shows the signature and does
**not** offer a retry, because retrying could create a duplicate token.

## How deployment works

**pump.fun** (`bot/chain/pump.py`): one v0 transaction with `create_v2` (the program creates the mint,
metadata and bonding curve), then the bot wallet's `buy`, then a transfer of the bought tokens to the creator
wallet. The creator wallet is registered as the coin's creator, so creator fees go to it. If the buy fails,
the whole launch reverts. The first buy is sized with a fee rate of `PUMP_FEE_BPS` (default 3%). This must be at
least pump.fun's real rate. If it is lower, preflight rejects the buy and nothing is spent.

The transaction touches ~28 accounts, which is more than fits in 1232 bytes. On the first pump launch the bot
creates an address lookup table (ALT) for the static pump.fun accounts (about 0.004 SOL rent). It saves the
table's address to `PUMP_LOOKUP_TABLE_FILE` and extends the table if pump.fun adds fee recipients.

**SPL** (`bot/chain/deployer.py`): one transaction with create account, init mint, Metaplex
`CreateMetadataAccountV3`, recipient ATA, mint the supply, then revoke the mint authority or give it to the
recipient. Freeze authority is never set if revoked. Update authority is "revoked" by making the metadata immutable.

Logo and metadata JSON go to IPFS through Pinata.

## Setup

```bash
cd token-bot
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in BOT_TOKEN, ALLOWED_USER_IDS, PAYER_PRIVATE_KEY, PINATA_JWT
python -m bot
```

Start on `SOLANA_CLUSTER=devnet` with a devnet RPC. Use a dedicated RPC provider on mainnet; the public
endpoint rate-limits.

## Security

- **The bot wallet pays for everything**: rent, fees, and dev buys. Only users in `ALLOWED_USER_IDS` can use
  the bot. Keep only the SOL you need in this wallet.
- `PAYER_PRIVATE_KEY` is a hot key in an env var. Never commit `.env`. Run the bot on a host you control.
- `PUMP_MAX_DEV_BUY_SOL` caps a single dev buy.
- Dev buys are ordinary on-chain purchases by the creator. pump.fun, DexScreener, and holder-analysis tools
  show them as the dev's holding.

## Tests

```bash
pip install -r requirements.txt
python -m unittest discover -s tests -t .
```

- `test_flow.py`: full conversations (both launch types, a retry, the allowlist) against a fake Telegram
  session and fake chain/IPFS services.
- `test_spl_litesvm.py`: runs the SPL mint, supply, and authority instructions in LiteSVM (a local Solana VM).
- `test_pump_parity.py`: compares `create_v2` and `buy` byte for byte with the official `@pump-fun/pump-sdk`.
  It needs Node: `cd tests/pump_parity && npm install`. Without Node it is skipped.

pump.fun changes its instruction accounts from time to time. When `@pump-fun/pump-sdk` has a new release, bump
it in `tests/pump_parity/package.json` and re-run the parity test before deploying.

## Layout

```
bot/
  __main__.py        entry point, dependency wiring
  config.py          env config
  handlers/          /launch conversation (launch.py), /start /help /cancel (common.py)
  chain/deployer.py  SPL launch, pump.fun launch, send/confirm, lookup table management
  chain/pump.py      pump.fun instructions, account decoding, curve math
  chain/metaplex.py  CreateMetadataAccountV3 encoding
  chain/lookup_table.py  ALT create/extend instructions
  services/          Pinata IPFS, SOL/USD price (display only)
  keyboards.py, states.py, middlewares.py, filters.py
```
