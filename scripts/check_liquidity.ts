import { MARGINFI_IDL, Project0Client, getConfig, AssetTag } from "@0dotxyz/p0-ts-sdk";
import { Connection, PublicKey } from "@solana/web3.js";
// @ts-ignore
import * as bufferLayout from "buffer-layout";

if ((bufferLayout as any)?.Union?.prototype?.decode) {
  const origUnionDecode = (bufferLayout as any).Union.prototype.decode;
  (bufferLayout as any).Union.prototype.decode = function (b: any, offset: any) {
    const dlo = this.discriminator;
    if (!dlo) return origUnionDecode.call(this, b, offset);
    const discr = dlo.decode(b, offset ?? 0);
    if (this.registry[discr] === undefined && !this.defaultLayout) {
      const dest = this.makeDestinationObject();
      if (dlo.property) dest[dlo.property] = discr;
      return dest;
    }
    return origUnionDecode.call(this, b, offset);
  };
}

for (const t of (MARGINFI_IDL as any)?.types ?? []) {
  if (t.type && t.type.kind === "enum") {
    while (t.type.variants.length < 64) {
      t.type.variants.push({ name: `Unknown${t.name}${t.type.variants.length}` });
    }
  }
}

const PYUSD_MINT = new PublicKey("2b1kV6DkPAnxd5ixfnxCpjxmKwqjjaYmCZfHsFu24GXo");
const USDG_MINT = new PublicKey("2u1tszSeqZ3qBWF3uNGPFc8TzMk2tdiwknnRMWGWjGWH");
const USDC_MINT = new PublicKey("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v");

async function main() {
  const connection = new Connection("https://api.mainnet-beta.solana.com", "confirmed");
  const mfiConfig = getConfig("production");
  const client = await Project0Client.initialize(connection, mfiConfig);

  console.log("=== MARGINFI / PROJECT0 ON-CHAIN STATUS ===");
  const tokens = [
    { name: "USDG", mint: USDG_MINT },
    { name: "PYUSD", mint: PYUSD_MINT },
    { name: "USDC", mint: USDC_MINT },
  ];

  for (const t of tokens) {
    const banks = client.getBanksByMint(t.mint, AssetTag.DEFAULT);
    console.log(`\n========================================`);
    console.log(`Token: ${t.name} (${t.mint.toBase58()}) | Found: ${banks.length} bank(s)`);
    for (const b of banks) {
      const vaultBalance = await connection.getTokenAccountBalance(b.liquidityVault).catch(() => ({ value: { uiAmount: 0 } }));
      const totalLiabilityShares = b.totalLiabilityShares;
      const totalAssetShares = b.totalAssetShares;
      console.log(`  Bank Address: ${b.address.toBase58()}`);
      console.log(`  Liquidity Vault: ${b.liquidityVault.toBase58()}`);
      console.log(`  Free Available Liquidity: ${vaultBalance.value.uiAmount} ${t.name}`);
      try {
        const { assets, liabilities } = b.computeQuantityComponents(totalAssetShares, totalLiabilityShares);
        console.log(`  Total Deposited: ${assets.toString()} ${t.name}`);
        console.log(`  Total Borrowed: ${liabilities.toString()} ${t.name}`);
        const util = (liabilities.toNumber() / (assets.toNumber() || 1)) * 100;
        console.log(`  Utilization: ${util.toFixed(4)}%`);
      } catch (err: any) {
        console.log(`  Quantity compute: ${err.message}`);
      }
      try {
        console.log(`  Borrow Limit: ${b.config.borrowLimit.toString()} | Deposit Limit: ${b.config.depositLimit.toString()}`);
      } catch (err: any) {}
      console.log(`  Operational State: ${JSON.stringify(b.config.operationalState)}`);
    }
  }
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
