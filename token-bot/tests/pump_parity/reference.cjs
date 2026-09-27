// Reads a JSON case on stdin, prints the official SDK's create_v2 + buy instructions as JSON.
const { PUMP_SDK } = require("@pump-fun/pump-sdk");
const { PublicKey } = require("@solana/web3.js");
const BN = require("bn.js");

async function main() {
  const input = JSON.parse(require("fs").readFileSync(0, "utf8"));

  Math.random = () => 0; // pick the first fee recipient / buyback recipient deterministically
  const pk = (s) => new PublicKey(s);
  const zero = PublicKey.default;
  const global = {
    initialized: true,
    authority: zero,
    feeRecipient: pk(input.feeRecipient),
    initialVirtualTokenReserves: new BN(0),
    initialVirtualSolReserves: new BN(0),
    initialRealTokenReserves: new BN(0),
    tokenTotalSupply: new BN(0),
    feeBasisPoints: new BN(0),
    withdrawAuthority: zero,
    enableMigrate: false,
    poolMigrationFee: new BN(0),
    creatorFeeBasisPoints: new BN(0),
    feeRecipients: Array(7).fill(zero),
    setCreatorAuthority: zero,
    adminSetCreatorAuthority: zero,
    createV2Enabled: true,
    whitelistPda: zero,
    reservedFeeRecipient: zero,
    mayhemModeEnabled: false,
    reservedFeeRecipients: Array(7).fill(zero),
    isCashbackEnabled: false,
    buybackFeeRecipients: Array(8).fill(zero),
    buybackBasisPoints: new BN(0),
    initialVirtualQuoteReserves: new BN(0),
    whitelistedQuoteMints: [zero],
    creatorFeeConfigurable: false,
    maxConfigurableCreatorFeeBps: new BN(0),
    holderRewardClaimAuthority: zero,
    isHolderRewardEnabled: false,
  };

  const ixs = await PUMP_SDK.createV2AndBuyInstructions({
    global,
    mint: pk(input.mint),
    name: input.name,
    symbol: input.symbol,
    uri: input.uri,
    creator: pk(input.creator),
    user: pk(input.user),
    amount: new BN(input.tokenAmount),
    solAmount: new BN(input.solAmount),
    mayhemMode: false,
  });

  const { TOKEN_2022_PROGRAM_ID } = require("@solana/spl-token");
  const trade = {
    user: pk(input.user),
    mint: pk(input.mint),
    creator: pk(input.creator),
    feeRecipient: pk(input.feeRecipient),
    buybackFeeRecipient: pk(input.buybackFeeRecipient),
    tokenProgram: TOKEN_2022_PROGRAM_ID,
  };
  const buy = await PUMP_SDK.getBuyInstructionRaw({
    ...trade, amount: new BN(input.tokenAmount), solAmount: new BN(input.solAmount),
  });
  const sell = await PUMP_SDK.getSellInstructionRaw({
    ...trade, amount: new BN(input.tokenAmount), solAmount: new BN(input.minSolOut),
  });

  const asJson = (ix) => ({
    programId: ix.programId.toBase58(),
    data: Buffer.from(ix.data).toString("hex"),
    keys: ix.keys.map((k) => [k.pubkey.toBase58(), k.isSigner, k.isWritable]),
  });
  console.log(JSON.stringify({ createAndBuy: ixs.map(asJson), buy: asJson(buy), sell: asJson(sell) }));
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
