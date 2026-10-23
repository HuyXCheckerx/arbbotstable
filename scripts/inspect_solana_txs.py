import urllib.request, json, time

rpc = "https://solana-rpc.publicnode.com"
pubkey = "G3yfNkUaTvr1QvAPThRuNL9H5oogVDrzSVopCsY1f1he"

req = urllib.request.Request(rpc, data=json.dumps({
    "jsonrpc": "2.0", "id": 1, "method": "getSignaturesForAddress",
    "params": [pubkey, {"limit": 25}]
}).encode(), headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"})

with urllib.request.urlopen(req) as r:
    sigs = json.loads(r.read())["result"]

tokens = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": "USDC",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": "USDT",
    "2b1kV6DkPAnxd5ixfnxCpjxmKwqjjaYmCZfHsFu24GXo": "PYUSD",
    "2u1tszSeqZ3qBWF3uNGPFc8TzMk2tdiwknnRMWGWjGWH": "USDG",
}

print(f"Total signatures found: {len(sigs)}")

for idx, s in enumerate(sigs):
    sig = s["signature"]
    err = s["err"]
    block_time = s.get("blockTime")
    time_str = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(block_time)) if block_time else "unknown"
    if err is not None:
        print(f"[{idx}] {time_str} | Sig: {sig[:16]}... FAILED: {err}")
        continue
    try:
        req = urllib.request.Request(rpc, data=json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "getTransaction",
            "params": [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}]
        }).encode(), headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=5) as r:
            tx = json.loads(r.read()).get("result")
            if not tx:
                print(f"[{idx}] {time_str} | Sig: {sig[:16]}... (null tx)")
                continue
            meta = tx["meta"]
            fee = meta["fee"] / 1e9
            
            pre_bals = {}
            for b in meta.get("preTokenBalances", []):
                if b.get("owner") == pubkey:
                    m = b.get("mint")
                    sym = tokens.get(m, m[:6])
                    pre_bals[sym] = b.get("uiTokenAmount", {}).get("uiAmount") or 0.0
            
            post_bals = {}
            for b in meta.get("postTokenBalances", []):
                if b.get("owner") == pubkey:
                    m = b.get("mint")
                    sym = tokens.get(m, m[:6])
                    post_bals[sym] = b.get("uiTokenAmount", {}).get("uiAmount") or 0.0
            
            diffs = []
            for sym in set(list(pre_bals.keys()) + list(post_bals.keys())):
                diff = post_bals.get(sym, 0.0) - pre_bals.get(sym, 0.0)
                if abs(diff) > 1e-6:
                    diffs.append(f"{sym}: {diff:+.6f}")
            
            diff_str = ", ".join(diffs) if diffs else "NO SPL CHANGE"
            print(f"[{idx:2d}] {time_str} | {sig[:16]}... | Fee: {fee:.5f} SOL | {diff_str}")
    except Exception as e:
        print(f"[{idx:2d}] {time_str} | Error: {e}")
    time.sleep(0.2)
