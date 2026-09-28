import { VersionedTransaction, TransactionMessage, Connection, PublicKey } from '@solana/web3.js';

// Test transaction from earlier Jupiter / 0x quote:
const txBase64 = "AQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAACAAQAFCgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAHCULeULQew8fXCGjVgDIo5bVn9Cw7ZGzMciq7MyQPEk9t1pWG6qWtPmFmciR1xbrt4IW+b1nNcz2N5abYbCcsNuRl9sKBN/6x1hLohVfbcYYK5RqbPNFzjYNw1mo5L3b8TX/YklbzA9eunBLliZBt5Bs8Avo3x1c24xwUkF9nRJRPixdukLDvYMw2r2DTumX5VA1ifoAVfXgkOTnLswDEoyXJY9OJInxuz0QKRSODYMLWhOZ2v8QhASOe9jb6fhZAwZGb+UhFzL/7K26csOb57yM5bvF9xJrLEObOkAAAAC0P/on9df2SnTAmx8pWHneSwmrNt/J3VFLMhqns4zl6AR51VvyMcBu7nTFbs5oFQf9sbLeo/SOUQKxzaJWvBOPzLRZZN60qIeBaoyduwRCEOuOgf6Lrr8cRk1SIcKsL0UEBwAFAsBcFQAHAAkDBBcBAAAAAAAGBgAEAB8AIwEBCTsFAAMDAQQeHyMjCAkkIyIhABceIAMaAhwYFhsZHQAFCwABAgoOIx8iIAwNJCMjIQAUHh8DEgEVDxAREzfRmFOTfP7Y6QMA6HZIFwAAAJpmmUgXAAAAAAAAAAAAAwAAAC8BAOACAAF0ABAnAQMvAQAwJAADAzfZ1CdLbMUuVyHLWSz+ET2mZYPXsiOVaZYzsF/o6Cr5BTo3OFg7ATmTvHYT6Igfy1CnEqzsDFHkLCXHjTPjG1B3Mqv/NZa61gfu5ejx5OrjB+ntUVYCUL/o6LhArZLcLXohevPK4ns//WrZymMPFmxT1uPC3AMmTAdOVlX0UVD1AA==";

async function main() {
    const conn = new Connection('https://solana-rpc.publicnode.com');
    const tx = VersionedTransaction.deserialize(Buffer.from(txBase64, 'base64'));
    const luts = await Promise.all(
        tx.message.addressTableLookups.map(async (l) => (await conn.getAddressLookupTable(l.accountKey)).value)
    );
    const validLuts = luts.filter(Boolean);
    const decompiled = TransactionMessage.decompile(tx.message, { addressLookupTableAccounts: validLuts });
    
    console.log('Payer:', decompiled.payerKey.toBase58());
    console.log('Num instructions:', decompiled.instructions.length);
    decompiled.instructions.forEach((ix, idx) => {
        console.log(`\nInstruction #${idx}: Program: ${ix.programId.toBase58()}`);
        console.log('Keys count:', ix.keys.length);
        ix.keys.forEach((k, ki) => {
            console.log(`   Key[${ki}]: ${k.pubkey.toBase58()} (isSigner: ${k.isSigner}, isWritable: ${k.isWritable})`);
        });
    });
}
main().catch(console.error);
