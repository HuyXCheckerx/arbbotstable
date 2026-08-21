#!/usr/bin/env python3
"""
Solana Triangular Flash Arbitrage Route:
PYUSD Flash Loan -> Matcha (PYUSD -> USDC, 0 bps) -> USDC -> USDG -> USDG -> PYUSD -> Repay
"""

import os
import sys
import json
import time
import requests
from dataclasses import dataclass
from typing import Optional, Dict, Any, Tuple

from dotenv import load_dotenv
from solders.pubkey import Pubkey
from solana.rpc.api import Client

load_dotenv()

# Tokens on Solana Mainnet
PYUSD_MINT = Pubkey.from_string("2b1kV6DkPAnxd5ixfnxCpjxmKwqjjaYmCZfHsFu24GXo")
USDC_MINT = Pubkey.from_string("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")
USDG_MINT = Pubkey.from_string("2u1tszSeqZ3qBWF3uNGPFc8TzMk2tdiwknnRMWGWjGWH")

# Programs
STABLE_PROGRAM_ID = Pubkey.from_string("2zz7bEA4TzSJFvvGBgdVAdFBpAfkZHK3fCFBQk63MiBG")
TOKEN_PROGRAM_ID = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
TOKEN_2022_PROGRAM_ID = Pubkey.from_string("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
ASSOCIATED_TOKEN_PROGRAM_ID = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")

# API Configuration
MATCHA_BASE_URL = os.environ.get("SOLANA_ARB_MATCHA_BASE_URL", "https://meta.matcha.xyz")
STABLE_API_BASE = os.environ.get("SOL_FLASH_ARB_STABLE_API_BASE", "https://api-defi.stable.com")
SOLANA_RPC_URL = os.environ.get("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
SOLANA_CHAIN_ID_MATCHA = 1399811149

MATCHA_HEADERS = {
    "origin": "https://meta.matcha.xyz",
    "referer": "https://meta.matcha.xyz/",
    "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
}

STABLE_HEADERS = {
    "accept": "application/json",
    "content-type": "application/json",
    "origin": "https://stable.com",
    "referer": "https://stable.com/"
}

DECIMALS = 6


def get_token_program(mint: Pubkey) -> Pubkey:
    if mint in (PYUSD_MINT, USDG_MINT):
        return TOKEN_2022_PROGRAM_ID
    return TOKEN_PROGRAM_ID


def get_stable_pool_pda_and_ata(mint: Pubkey) -> Tuple[Pubkey, Pubkey]:
    token_program = get_token_program(mint)
    pool_pda, _ = Pubkey.find_program_address([b"pool", bytes(mint)], STABLE_PROGRAM_ID)
    pool_ata, _ = Pubkey.find_program_address(
        [bytes(pool_pda), bytes(token_program), bytes(mint)],
        ASSOCIATED_TOKEN_PROGRAM_ID
    )
    return pool_pda, pool_ata


def get_stable_pool_capacity(client: Client, mint: Pubkey) -> float:
    _, pool_ata = get_stable_pool_pda_and_ata(mint)
    try:
        res = client.get_token_account_balance(pool_ata)
        if res.value and res.value.ui_amount is not None:
            return float(res.value.ui_amount)
    except Exception as e:
        print(f"[!] Warning: Failed to query on-chain balance for {mint}: {e}")
    return 0.0


def query_matcha_quotes(
    sell_mint: Pubkey,
    buy_mint: Pubkey,
    sell_amount_raw: int,
    slippage_bps: int = 0,
    timeout: int = 12
) -> Dict[str, Any]:
    """Query meta.matcha.xyz for all available Solana aggregators."""
    payload = {
        "chainId": SOLANA_CHAIN_ID_MATCHA,
        "sellTokenAddress": str(sell_mint),
        "buyTokenAddress": str(buy_mint),
        "sellAmount": str(sell_amount_raw),
        "sellTokenDecimals": DECIMALS,
        "buyTokenDecimals": DECIMALS,
        "slippageBps": slippage_bps,
    }
    
    resp = requests.post(f"{MATCHA_BASE_URL}/api/competitions", json=payload, headers=MATCHA_HEADERS, timeout=timeout)
    if resp.status_code != 200:
        raise RuntimeError(f"Matcha competition init failed ({resp.status_code}): {resp.text}")
    
    comp = resp.json()
    comp_id = comp.get("id") or comp.get("competitionId")
    if not comp_id:
        raise RuntimeError("Matcha returned no competition ID")
    
    aggregators = ["0x", "Jupiter", "OKX"]
    quotes = {}
    
    for agg in aggregators:
        try:
            q_resp = requests.post(
                f"{MATCHA_BASE_URL}/api/quotes?aggregator={agg}",
                json={"competitionId": comp_id, "aggregator": agg},
                headers=MATCHA_HEADERS,
                timeout=timeout
            )
            if q_resp.status_code == 200:
                data = q_resp.json()
                quotes[agg] = data
        except Exception:
            pass
            
    return quotes


def select_best_matcha_quote(quotes: Dict[str, Any]) -> Tuple[str, int, Dict[str, Any]]:
    """Select the best quote that passed on-chain simulation or has highest buyAmount."""
    best_agg = None
    best_amount = 0
    best_quote_data = None
    
    # Priority: simulations that succeeded
    for agg, data in quotes.items():
        direct = data.get("direct", {})
        quote = direct.get("quote", {})
        simulation = direct.get("simulation", {})
        buy_amount = int(quote.get("buyAmount", "0"))
        sim_result = simulation.get("result")
        
        if sim_result == "success" and buy_amount > best_amount:
            best_agg = agg
            best_amount = buy_amount
            best_quote_data = data
            
    if best_agg is None:
        # Fallback to highest quote without simulation filter
        for agg, data in quotes.items():
            direct = data.get("direct", {})
            quote = direct.get("quote", {})
            buy_amount = int(quote.get("buyAmount", "0"))
            if buy_amount > best_amount:
                best_agg = agg
                best_amount = buy_amount
                best_quote_data = data
                
    if not best_agg or not best_quote_data:
        raise RuntimeError("No valid quotes returned from Matcha")
        
    return best_agg, best_amount, best_quote_data


def run_solana_matcha_pyusd_pipeline():
    print("=" * 80)
    print("SOLANA FLASH ARBITRAGE ROUTE SIMULATOR & QUOTER (meta.matcha.xyz + Stable.com)")
    print("=" * 80)
    
    client = Client(SOLANA_RPC_URL)
    
    # -------------------------------------------------------------
    # 0. QUERY STABLE.COM CAPACITY (ON-CHAIN + POOL RESERVES)
    # -------------------------------------------------------------
    print("\n[STEP 0] Checking Stable.com Capacity on Solana...")
    pyusd_capacity = get_stable_pool_capacity(client, PYUSD_MINT)
    usdg_capacity = get_stable_pool_capacity(client, USDG_MINT)
    usdc_capacity = get_stable_pool_capacity(client, USDC_MINT)
    
    print(f"  * Stable.com PYUSD Vault: {pyusd_capacity:,.2f} PYUSD")
    print(f"  * Stable.com USDG Vault:  {usdg_capacity:,.2f} USDG")
    print(f"  * Stable.com USDC Vault:  {usdc_capacity:,.2f} USDC")
    
    # Max capacity sizing
    max_capacity_raw = int(pyusd_capacity * 10**DECIMALS)
    if max_capacity_raw <= 0:
        max_capacity_raw = 70_000 * 10**DECIMALS
        print(f"  [!] Fallback to default capacity: 70,000.00 PYUSD")
    else:
        print(f"  [+] Maximum Sized Capacity: {pyusd_capacity:,.2f} PYUSD")

    loan_amount_raw = max_capacity_raw
    loan_amount = loan_amount_raw / 10**DECIMALS
    
    # -------------------------------------------------------------
    # 1. FLASH LOAN PYUSD
    # -------------------------------------------------------------
    print("\n" + "-" * 80)
    print(f"[STEP 1] FLASH LOAN PRINCIPAL (Borrow PYUSD)")
    print(f"  * Asset:         PYUSD ({PYUSD_MINT})")
    print(f"  * Borrow Amount: {loan_amount:,.6f} PYUSD ({loan_amount_raw:,} raw units)")
    print(f"  * Sizing Source: Stable.com maximum liquidity capacity")
    print(f"  * Flash Fee:     0.00% (MarginFi / Zero-fee flash loan facility)")

    # -------------------------------------------------------------
    # 2. SWAP PYUSD -> USDC ON MATCHA (0 BPS SLIPPAGE)
    # -------------------------------------------------------------
    print("\n" + "-" * 80)
    print(f"[STEP 2] SWAP PYUSD -> USDC ON MATCHA (meta.matcha.xyz, 0 BPS SLIPPAGE)")
    print(f"  * Input Amount:  {loan_amount:,.6f} PYUSD")
    print(f"  * Slippage:      0 bps (exact minimum threshold enforced)")
    print(f"  * Querying Matcha Meta Aggregators...")
    
    matcha_quotes_leg1 = query_matcha_quotes(PYUSD_MINT, USDC_MINT, loan_amount_raw, slippage_bps=0)
    
    for agg, data in matcha_quotes_leg1.items():
        direct = data.get("direct", {})
        q = direct.get("quote", {})
        sim = direct.get("simulation", {})
        out_raw = int(q.get("buyAmount", "0"))
        out_ui = out_raw / 10**DECIMALS
        sources = ", ".join(q.get("sources", []))
        sim_res = sim.get("result", "unknown")
        cu = sim.get("details", {}).get("computeUnitsConsumed", "N/A")
        print(f"    - [{agg:8s}] Output: {out_ui:,.6f} USDC | Sources: {sources} | Sim: {sim_res} (CU: {cu})")
        
    best_agg_1, out_usdc_raw, best_data_1 = select_best_matcha_quote(matcha_quotes_leg1)
    out_usdc = out_usdc_raw / 10**DECIMALS
    rate_1 = out_usdc / loan_amount
    print(f"  [+] Selected Best Leg 1 Route: {best_agg_1}")
    print(f"      -> Received:   {out_usdc:,.6f} USDC")
    print(f"      -> Quote Rate: 1 PYUSD = {rate_1:.6f} USDC")

    # -------------------------------------------------------------
    # 3. SWAP USDC -> USDG (0 BPS SLIPPAGE)
    # -------------------------------------------------------------
    print("\n" + "-" * 80)
    print(f"[STEP 3] SWAP USDC -> USDG ON MATCHA / DEX (0 BPS SLIPPAGE)")
    print(f"  * Input Amount:  {out_usdc:,.6f} USDC")
    print(f"  * Slippage:      0 bps")
    print(f"  * Querying Matcha Meta Aggregators...")
    
    matcha_quotes_leg2 = query_matcha_quotes(USDC_MINT, USDG_MINT, out_usdc_raw, slippage_bps=0)
    
    for agg, data in matcha_quotes_leg2.items():
        direct = data.get("direct", {})
        q = direct.get("quote", {})
        sim = direct.get("simulation", {})
        out_raw = int(q.get("buyAmount", "0"))
        out_ui = out_raw / 10**DECIMALS
        sources = ", ".join(q.get("sources", []))
        sim_res = sim.get("result", "unknown")
        cu = sim.get("details", {}).get("computeUnitsConsumed", "N/A")
        print(f"    - [{agg:8s}] Output: {out_ui:,.6f} USDG | Sources: {sources} | Sim: {sim_res} (CU: {cu})")

    best_agg_2, out_usdg_raw, best_data_2 = select_best_matcha_quote(matcha_quotes_leg2)
    out_usdg = out_usdg_raw / 10**DECIMALS
    rate_2 = out_usdg / out_usdc
    print(f"  [+] Selected Best Leg 2 Route: {best_agg_2}")
    print(f"      -> Received:   {out_usdg:,.6f} USDG")
    print(f"      -> Quote Rate: 1 USDC = {rate_2:.6f} USDG")

    # -------------------------------------------------------------
    # 4. SWAP USDG -> PYUSD (REPAY ROUTE VIA STABLE.COM / DEX)
    # -------------------------------------------------------------
    print("\n" + "-" * 80)
    print(f"[STEP 4] SWAP USDG -> PYUSD (STABLE.COM & DEX SETTLEMENT)")
    print(f"  * Input Amount:  {out_usdg:,.6f} USDG")
    
    # 4A. Matcha DEX Route
    matcha_quotes_leg3 = query_matcha_quotes(USDG_MINT, PYUSD_MINT, out_usdg_raw, slippage_bps=0)
    best_agg_3, out_pyusd_dex_raw, best_data_3 = select_best_matcha_quote(matcha_quotes_leg3)
    out_pyusd_dex = out_pyusd_dex_raw / 10**DECIMALS
    print(f"  * Matcha DEX Best ({best_agg_3}): {out_pyusd_dex:,.6f} PYUSD (Rate: {out_pyusd_dex/out_usdg:.6f})")
    
    # 4B. Stable.com SingleChain Settlement
    stable_output_raw = out_usdg_raw  # 1:1 on protocol par settlement
    stable_output_ui = stable_output_raw / 10**DECIMALS
    print(f"  * Stable.com Par Settle:       {stable_output_ui:,.6f} PYUSD (Rate: 1.000000)")

    # -------------------------------------------------------------
    # 5. REPAY FLASH LOAN & PNL CALCULATION
    # -------------------------------------------------------------
    print("\n" + "=" * 80)
    print("ROUTE SUMMARY & EXECUTION METRICS")
    print("=" * 80)
    
    final_pyusd_received = max(out_pyusd_dex, stable_output_ui)
    final_pyusd_raw = int(final_pyusd_received * 10**DECIMALS)
    gross_profit_raw = final_pyusd_raw - loan_amount_raw
    gross_profit = gross_profit_raw / 10**DECIMALS
    gross_return_bps = (gross_profit / loan_amount) * 10_000
    
    print(f"  1. Borrowed Principal:        {loan_amount:,.6f} PYUSD")
    print(f"  2. Leg 1 (PYUSD -> USDC):     {out_usdc:,.6f} USDC (via Matcha {best_agg_1})")
    print(f"  3. Leg 2 (USDC -> USDG):     {out_usdg:,.6f} USDG (via Matcha {best_agg_2})")
    print(f"  4. Leg 3 (USDG -> PYUSD):     {final_pyusd_received:,.6f} PYUSD (via {'Stable.com' if final_pyusd_received == stable_output_ui else 'Matcha ' + best_agg_3})")
    print(f"  5. Repay Flash Loan:         -{loan_amount:,.6f} PYUSD")
    print("  " + "-" * 50)
    
    if gross_profit >= 0:
        print(f"  [+] Gross Profit:             +{gross_profit:,.6f} PYUSD (+{gross_return_bps:.2f} bps)")
    else:
        print(f"  [-] Gross Profit (Spread):    {gross_profit:,.6f} PYUSD ({gross_return_bps:.2f} bps)")

    # Execution network cost
    est_tx_fee_sol = 0.000105
    sol_price_usd = 190.0
    est_fee_usd = est_tx_fee_sol * sol_price_usd
    net_profit = gross_profit - est_fee_usd
    
    print(f"  * Estimated Network Fee:     ~{est_tx_fee_sol:.6f} SOL (~${est_fee_usd:.4f})")
    print(f"  * Projected Net PnL:          {'+' if net_profit >= 0 else ''}{net_profit:,.6f} USD")
    print("=" * 80)


if __name__ == "__main__":
    run_solana_matcha_pyusd_pipeline()
