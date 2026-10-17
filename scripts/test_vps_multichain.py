from src.engines.multichain_engine import MultichainEngine, CHAINS
import traceback

engine = MultichainEngine()
for key in ("bsc", "polygon"):
    config = CHAINS[key]
    print(f"Testing {config.name}...")
    try:
        dq = engine.dex.get_best_quote(config, config.usdc, config.usdt, 50000 * 10**config.decimals, 2, "0x50dA32E628b45AbB1335924086Ca0013b9d4eC1C")
        print("  DEX quote buyAmount:", dq.buy_amount if dq else None)
        sq = engine.stable.quote(config, "USDT", "USDC", dq.buy_amount if dq else 50000 * 10**config.decimals, "0x50dA32E628b45AbB1335924086Ca0013b9d4eC1C")
        print("  Stable quote amount_out:", sq.amount_out if sq else None)
        op = engine.scan_chain_opportunity(config, 50000, "USDT", 2)
        print("  Gross profit:", op["gross_profit"] if op else None)
    except Exception as e:
        traceback.print_exc()
