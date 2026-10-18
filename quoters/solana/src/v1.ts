import { getBase58Encoder } from "@solana/kit";
import { MessageV1, PublicKey, TransactionInstruction, VersionedTransaction } from "@solana/web3.js";

export const V1_PACKET_LIMIT = 4096;
export const V0_PACKET_LIMIT = 1232;

export interface V1Config {
  computeUnitLimit?: number | null;
  heapSize?: number | null;
  loadedAccountsDataSizeLimit?: number | null;
  priorityFee?: number | null;
}

export function serializeV1Message(message: MessageV1): Uint8Array {
  const parts: Buffer[] = [];
  parts.push(Buffer.from([0x81])); // VERSION_1_MESSAGE_PREFIX
  parts.push(Buffer.from([
    message.header.numRequiredSignatures,
    message.header.numReadonlySignedAccounts,
    message.header.numReadonlyUnsignedAccounts,
  ]));

  let configMask = 0;
  const configValues: Buffer[] = [];
  const cfg = message.transactionConfig || {};
  if (cfg.priorityFee != null) {
    configMask |= 0b00011; // bits 0 and 1
    const b = Buffer.alloc(8);
    b.writeBigUInt64LE(BigInt(cfg.priorityFee));
    configValues.push(b);
  }
  if (cfg.computeUnitLimit != null) {
    configMask |= 0b00100; // bit 2
    const b = Buffer.alloc(4);
    b.writeUInt32LE(cfg.computeUnitLimit);
    configValues.push(b);
  }
  if (cfg.loadedAccountsDataSizeLimit != null) {
    configMask |= 0b01000; // bit 3
    const b = Buffer.alloc(4);
    b.writeUInt32LE(cfg.loadedAccountsDataSizeLimit);
    configValues.push(b);
  }
  if (cfg.heapSize != null) {
    configMask |= 0b10000; // bit 4
    const b = Buffer.alloc(4);
    b.writeUInt32LE(cfg.heapSize);
    configValues.push(b);
  }

  const maskBuf = Buffer.alloc(4);
  maskBuf.writeUInt32LE(configMask);
  parts.push(maskBuf);

  parts.push(Buffer.from(getBase58Encoder().encode(message.recentBlockhash) as Uint8Array));
  parts.push(Buffer.from([message.compiledInstructions.length, message.staticAccountKeys.length]));

  for (const key of message.staticAccountKeys) {
    parts.push(key.toBuffer());
  }

  for (const val of configValues) {
    parts.push(val);
  }

  for (const ix of message.compiledInstructions) {
    const h = Buffer.alloc(4);
    h.writeUInt8(ix.programIdIndex, 0);
    h.writeUInt8(ix.accountKeyIndexes.length, 1);
    h.writeUInt16LE(ix.data.length, 2);
    parts.push(h);
  }

  for (const ix of message.compiledInstructions) {
    parts.push(Buffer.from(ix.accountKeyIndexes));
    parts.push(Buffer.from(ix.data));
  }

  return Buffer.concat(parts);
}

export function serializeV1Transaction(tx: VersionedTransaction): Uint8Array {
  const msgBuf = serializeV1Message(tx.message as MessageV1);
  const sigParts = tx.signatures.map((s) => Buffer.from(s));
  return Buffer.concat([msgBuf, ...sigParts]);
}

// Ensure web3.js methods work seamlessly with Version 1 transactions
let installed = false;
export function installV1Serialization(): void {
  if (installed) return;
  installed = true;
  MessageV1.prototype.serialize = function (this: MessageV1) {
    return serializeV1Message(this);
  };
  const originalTxSerialize = VersionedTransaction.prototype.serialize;
  VersionedTransaction.prototype.serialize = function (this: VersionedTransaction) {
    if (this.version === 1) {
      return serializeV1Transaction(this);
    }
    return originalTxSerialize.call(this);
  };
}
installV1Serialization();

export interface CompileToV1Args {
  payerKey: PublicKey;
  recentBlockhash: string;
  instructions: TransactionInstruction[];
  config?: V1Config;
}

export function compileToV1Message(args: CompileToV1Args): MessageV1 {
  const { payerKey, recentBlockhash, instructions, config = {} } = args;
  const keyMap = new Map<string, { key: PublicKey; isSigner: boolean; isWritable: boolean }>();

  function getAccount(pk: PublicKey) {
    const s = pk.toBase58();
    let acc = keyMap.get(s);
    if (!acc) {
      acc = { key: pk, isSigner: false, isWritable: false };
      keyMap.set(s, acc);
    }
    return acc;
  }

  // Payer is always a writable signer
  const payerAcc = getAccount(payerKey);
  payerAcc.isSigner = true;
  payerAcc.isWritable = true;

  for (const ix of instructions) {
    getAccount(ix.programId); // Program IDs are unsigned read-only
    for (const meta of ix.keys) {
      const acc = getAccount(meta.pubkey);
      if (meta.isSigner) acc.isSigner = true;
      if (meta.isWritable) acc.isWritable = true;
    }
  }

  const accounts = Array.from(keyMap.values());
  const writableSigners = [payerAcc, ...accounts.filter((a) => a !== payerAcc && a.isSigner && a.isWritable)];
  const readonlySigners = accounts.filter((a) => a.isSigner && !a.isWritable);
  const writableUnsigned = accounts.filter((a) => !a.isSigner && a.isWritable);
  const readonlyUnsigned = accounts.filter((a) => !a.isSigner && !a.isWritable);

  const ordered = [...writableSigners, ...readonlySigners, ...writableUnsigned, ...readonlyUnsigned];
  const indexMap = new Map<string, number>(ordered.map((a, i) => [a.key.toBase58(), i]));

  const header = {
    numRequiredSignatures: writableSigners.length + readonlySigners.length,
    numReadonlySignedAccounts: readonlySigners.length,
    numReadonlyUnsignedAccounts: readonlyUnsigned.length,
  };

  const compiledInstructions = instructions.map((ix) => ({
    programIdIndex: indexMap.get(ix.programId.toBase58())!,
    accountKeyIndexes: ix.keys.map((k) => indexMap.get(k.pubkey.toBase58())!),
    data: Buffer.from(ix.data),
  }));

  const transactionConfig = {
    computeUnitLimit: config.computeUnitLimit ?? null,
    heapSize: config.heapSize ?? null,
    loadedAccountsDataSizeLimit: config.loadedAccountsDataSizeLimit ?? null,
    priorityFee: config.priorityFee ?? null,
  };

  return new MessageV1({
    header,
    staticAccountKeys: ordered.map((a) => a.key),
    recentBlockhash,
    compiledInstructions,
    transactionConfig,
  });
}
