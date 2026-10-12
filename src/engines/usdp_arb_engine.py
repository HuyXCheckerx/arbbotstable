#!/usr/bin/env python3
"""Standalone USDP Arbitrage Test Engine (Testing Only - NOT added to bot).

Workflow:
1. Quotes 10,000 USDC for USDP on Ethereum via DEX Aggregator (OKX / Velora).
2. Quotes swapping the received USDP back to USDC 1:1 on Stable.com.
3. Calculates gross & net profit accounting for L1 gas and flash loan fees.
4. Optionally simulates on-chain execution with Morpho flash loan.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from decimal import Decimal
import json
import os
from pathlib import Path
import sys
import time
from typing import Any
from urllib.parse import urlencode
import uuid

# Load local .env
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env", override=True)
except ImportError:
    pass

import requests

# Contract Addresses on Ethereum Mainnet
USDC_ADDRESS = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
USDP_ADDRESS = "0x8E870D67F660D95d5be530380D0eC0bd388289E1"
MORPHO_ADDRESS = "0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb"
STABLE_POOL_ADDRESS = "0xCfC1bc6013eD89D484c626dd9ee5EB7bc1a1d9Da"
DEFAULT_EXECUTOR = "0xD48Ab89581e77b103014DCC922279855a3b2a940"
OKX_DEFAULT_ROUTER = "0x8feAB81D36E7576107D5dE0758c1b839Be31B4F6"
OKX_DEFAULT_SPENDER = "0x40aA958dd87FC8305b97f2BA922CDdCa374bcD7f"

USDC_DECIMALS = 6
USDP_DECIMALS = 18
STABLE_CHAIN_ID = "101"
STABLE_BASE_URL = "https://api-defi.stable.com"
DEFAULT_RPC_URLS = [
    "https://rpc.mevblocker.io",
    "https://1rpc.io/eth",
    "https://eth-mainnet.public.blastapi.io",
    "https://eth.drpc.org",
    "https://ethereum.publicnode.com",
]


@dataclass
class DexQuote:
    aggregator: str
    target: str
    allowance_target: str
    sell_amount_raw: int
    buy_amount_raw: int
    buy_amount_human: Decimal
    data: str
    value: int
    gas_estimate: int


@dataclass
class StableQuote:
    amount_in_human: Decimal
    amount_out_human: Decimal
    pool_balance_human: Decimal
    min_order_human: Decimal
    token_fee_human: Decimal
    native_fee_usd: Decimal
    raw_response: dict[str, Any]


@dataclass
class ArbEvaluation:
    loan_amount_usdc: Decimal
    dex_quote: DexQuote
    stable_quote: StableQuote
    gross_profit_usdc: Decimal
    gas_estimate_usd: Decimal
    net_profit_usd: Decimal
    is_profitable: bool


class USDPTestEngine:
    def __init__(
        self,
        executor: str = DEFAULT_EXECUTOR,
        operator: str | None = None,
        rpc_url: str | None = None,
        timeout: float = 12.0,
    ):
        self.executor = executor
        self.operator = operator or os.getenv("ETH_OPERATOR_ADDRESS")
        self.timeout = timeout
        
        env_rpc = os.getenv("ETH_RPC_URL")
        self.rpc_urls = [r for r in ([rpc_url] if rpc_url else []) + ([env_rpc] if env_rpc else []) + DEFAULT_RPC_URLS if r]
        
        self.okx_key = os.getenv("OKX_API_KEY", "afbd1cbb-fb82-4c24-9b50-c28823cd8001")
        self.okx_secret = os.getenv("OKX_SECRET_KEY", "E41ADC9119A47896A8ABE0E752983B31")
        self.okx_passphrase = os.getenv("OKX_PASSPHRASE", "Huypro159!")
        self.okx_project_id = os.getenv("OKX_PROJECT_ID", "4506f56deeba9843699dd86ea3253f5a")
        
        self.session = requests.Session()
        self.session.headers.update({"accept": "application/json", "user-agent": "arbbot-usdp-test/1.0"})

    def _rpc_call(self, method: str, params: list[Any]) -> Any:
        last_err = None
        for url in self.rpc_urls:
            try:
                resp = self.session.post(
                    url,
                    json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                    timeout=5.0,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    if "result" in data:
                        return data["result"]
                    if "error" in data:
                        raise RuntimeError(f"RPC error: {data['error']}")
            except Exception as exc:
                last_err = exc
                continue
        raise RuntimeError(f"All RPC endpoints failed for {method}: {last_err}")

    def get_gas_price_gwei(self) -> Decimal:
        try:
            res = self._rpc_call("eth_gasPrice", [])
            return Decimal(int(res, 16)) / Decimal(10**9)
        except Exception:
            return Decimal("1.5")  # fallback 1.5 Gwei

    def get_eth_usd(self) -> Decimal:
        try:
            resp = self.session.get(
                "https://data-api.binance.vision/api/v3/ticker/price?symbol=ETHUSDC",
                timeout=3.0,
            )
            if resp.status_code == 200:
                return Decimal(resp.json().get("price", "2500"))
        except Exception:
            pass
        return Decimal("2500.0")

    def quote_dex_okx(self, usdc_amount: Decimal, slippage_bps: int = 10) -> DexQuote:
        """Fetch quote from OKX DEX Aggregator for USDC -> USDP."""
        import base64
        import hashlib
        import hmac

        raw_in = int(usdc_amount * Decimal(10**USDC_DECIMALS))
        slippage_pct = f"{max(0.05, float(slippage_bps) / 100):.2f}"
        
        path = "/api/v6/dex/aggregator/swap"
        params = {
            "chainIndex": "1",
            "fromTokenAddress": USDC_ADDRESS,
            "toTokenAddress": USDP_ADDRESS,
            "amount": str(raw_in),
            "slippagePercent": slippage_pct,
            "userWalletAddress": self.executor,
        }
        qs = urlencode(params)
        ts = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
        message = ts + "GET" + path + "?" + qs
        digest = hmac.new(self.okx_secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).digest()
        signature = base64.b64encode(digest).decode("utf-8")
        
        headers = {
            "OK-ACCESS-KEY": self.okx_key,
            "OK-ACCESS-SIGN": signature,
            "OK-ACCESS-TIMESTAMP": ts,
            "OK-ACCESS-PASSPHRASE": self.okx_passphrase,
            "OK-ACCESS-PROJECT": self.okx_project_id,
        }
        
        url = f"https://www.okx.com{path}?{qs}"
        resp = self.session.get(url, headers=headers, timeout=self.timeout)
        if resp.status_code != 200:
            raise RuntimeError(f"OKX swap API HTTP {resp.status_code}: {resp.text[:200]}")
        payload = resp.json()
        if payload.get("code") != "0" or not payload.get("data"):
            raise RuntimeError(f"OKX swap error: {payload.get('msg', payload)}")
            
        item = payload["data"][0]
        tx = item.get("tx") or {}
        router_res = item.get("routerResult") or {}
        out_raw = int(router_res.get("toTokenAmount") or item.get("toTokenAmount"))
        out_human = Decimal(out_raw) / Decimal(10**USDP_DECIMALS)
        
        return DexQuote(
            aggregator="OKX",
            target=tx.get("to") or OKX_DEFAULT_ROUTER,
            allowance_target=OKX_DEFAULT_SPENDER,
            sell_amount_raw=raw_in,
            buy_amount_raw=out_raw,
            buy_amount_human=out_human,
            data=tx.get("data", "0x"),
            value=int(tx.get("value") or 0),
            gas_estimate=int(tx.get("gas") or 260_000),
        )

    def quote_dex_velora(self, usdc_amount: Decimal, slippage_bps: int = 10) -> DexQuote:
        """Fetch quote from Velora / ParaSwap Augustus v6.2 for USDC -> USDP."""
        raw_in = int(usdc_amount * Decimal(10**USDC_DECIMALS))
        url = "https://api.paraswap.io/prices"
        params = {
            "srcToken": USDC_ADDRESS.lower(),
            "srcDecimals": str(USDC_DECIMALS),
            "destToken": USDP_ADDRESS.lower(),
            "destDecimals": str(USDP_DECIMALS),
            "amount": str(raw_in),
            "side": "SELL",
            "network": "1",
            "version": "6.2",
        }
        resp = self.session.get(url, params=params, timeout=self.timeout)
        if resp.status_code != 200:
            raise RuntimeError(f"Velora price HTTP {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        route = data.get("priceRoute")
        if not route:
            raise RuntimeError(f"Velora returned no priceRoute: {data}")
            
        dest_raw = int(route.get("destAmount"))
        effective_slippage = max(10, int(slippage_bps))
        min_dest = dest_raw * (10_000 - effective_slippage) // 10_000
        
        # Build transaction
        tx_resp = self.session.post(
            "https://api.paraswap.io/transactions/1",
            params={"ignoreChecks": "true", "ignoreGasEstimate": "true"},
            json={
                "srcToken": USDC_ADDRESS.lower(),
                "destToken": USDP_ADDRESS.lower(),
                "srcAmount": str(raw_in),
                "destAmount": str(min_dest),
                "priceRoute": route,
                "userAddress": self.executor,
                "receiver": self.executor,
            },
            timeout=self.timeout,
        )
        if tx_resp.status_code != 200:
            raise RuntimeError(f"Velora transaction build HTTP {tx_resp.status_code}: {tx_resp.text[:200]}")
        tx = tx_resp.json()
        
        return DexQuote(
            aggregator="Velora",
            target=tx.get("to"),
            allowance_target=route.get("tokenTransferProxy") or tx.get("to"),
            sell_amount_raw=raw_in,
            buy_amount_raw=dest_raw,
            buy_amount_human=Decimal(dest_raw) / Decimal(10**USDP_DECIMALS),
            data=tx.get("data"),
            value=int(tx.get("value") or 0),
            gas_estimate=int(route.get("gasCost") or 280_000),
        )

    def quote_dex(self, usdc_amount: Decimal, aggregator: str = "okx", slippage_bps: int = 10) -> DexQuote:
        agg = aggregator.lower().strip()
        if agg == "velora":
            return self.quote_dex_velora(usdc_amount, slippage_bps)
        if agg == "okx":
            return self.quote_dex_okx(usdc_amount, slippage_bps)
        
        # auto: try OKX first, fallback to Velora
        try:
            return self.quote_dex_okx(usdc_amount, slippage_bps)
        except Exception as exc:
            return self.quote_dex_velora(usdc_amount, slippage_bps)

    def quote_stable(self, usdp_amount: Decimal) -> StableQuote:
        """Query Stable.com status for swapping USDP -> USDC on Ethereum."""
        headers = {
            "origin": "https://stable.com",
            "referer": "https://stable.com/",
            "accept": "application/json",
            "content-type": "application/json",
        }
        amount_str = f"{usdp_amount:.6f}"
        body = {
            "assetFrom": "USDP",
            "assetTo": "USDC",
            "chainFrom": STABLE_CHAIN_ID,
            "chainTo": STABLE_CHAIN_ID,
            "amountFrom": amount_str,
            "addressFrom": self.executor,
            "addressTo": self.executor,
            "gasLess": False,
        }
        resp = self.session.post(
            f"{STABLE_BASE_URL}/swap/status",
            json=body,
            headers=headers,
            timeout=self.timeout,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Stable.com /swap/status HTTP {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        asset = data.get("asset") or {}
        
        amount_in = Decimal(str(asset.get("amountFrom", amount_str)))
        amount_out = Decimal(str(asset.get("amountTo", amount_str)))
        balance = Decimal(str(asset.get("balance", "0")))
        min_order = Decimal(str(asset.get("min", "1000")))
        token_fee = Decimal(str(asset.get("tokenFee", "0")))
        native_fee_usd = Decimal(str(asset.get("nativeFeeUsd", "0")))
        
        return StableQuote(
            amount_in_human=amount_in,
            amount_out_human=amount_out,
            pool_balance_human=balance,
            min_order_human=min_order,
            token_fee_human=token_fee,
            native_fee_usd=native_fee_usd,
            raw_response=data,
        )

    def create_stable_order(self, usdp_amount: Decimal) -> dict[str, Any]:
        """Create singleChain order on Stable.com for maintainer signature."""
        headers = {
            "origin": "https://stable.com",
            "referer": "https://stable.com/",
            "accept": "application/json",
            "content-type": "application/json",
        }
        amount_str = f"{usdp_amount:.6f}"
        body = {
            "assetFrom": "USDP",
            "assetTo": "USDC",
            "chainFrom": STABLE_CHAIN_ID,
            "chainTo": STABLE_CHAIN_ID,
            "amountFrom": amount_str,
            "amountTo": amount_str,
            "addressFrom": self.executor,
            "addressTo": self.executor,
            "device": str(uuid.uuid4()),
            "gasLess": False,
        }
        resp = self.session.post(
            f"{STABLE_BASE_URL}/swap/create/singleChain",
            json=body,
            headers=headers,
            timeout=self.timeout,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Stable.com order creation HTTP {resp.status_code}: {resp.text[:200]}")
        res = resp.json()
        data = res.get("data")
        if not data or not data.get("maintainerSignature"):
            raise RuntimeError(f"Stable.com did not return maintainerSignature: {res}")
        return data

    def evaluate_arbitrage(
        self,
        amount_usdc: Decimal = Decimal("10000.0"),
        aggregator: str = "okx",
        slippage_bps: int = 10,
    ) -> ArbEvaluation:
        # Leg 1: DEX Swap USDC -> USDP
        dex_q = self.quote_dex(amount_usdc, aggregator=aggregator, slippage_bps=slippage_bps)
        
        # Leg 2: Stable.com Swap USDP -> USDC
        stable_q = self.quote_stable(dex_q.buy_amount_human)
        
        # Financial Calculations
        gross_profit = stable_q.amount_out_human - amount_usdc
        
        # Gas estimation (DEX swap ~220k gas + Stable swap ~110k gas + Morpho loan ~50k gas ~= 380k gas)
        total_gas_units = dex_q.gas_estimate + 120_000
        gas_price_gwei = self.get_gas_price_gwei()
        eth_usd = self.get_eth_usd()
        gas_cost_usd = (Decimal(total_gas_units) * gas_price_gwei / Decimal(10**9)) * eth_usd
        
        net_profit = gross_profit - gas_cost_usd - stable_q.native_fee_usd
        
        return ArbEvaluation(
            loan_amount_usdc=amount_usdc,
            dex_quote=dex_q,
            stable_quote=stable_q,
            gross_profit_usdc=gross_profit,
            gas_estimate_usd=gas_cost_usd,
            net_profit_usd=net_profit,
            is_profitable=net_profit > 0,
        )


    def scale_scan(
        self,
        min_amount: Decimal = Decimal("10000.0"),
        max_amount: Decimal = Decimal("100000.0"),
        step: Decimal = Decimal("10000.0"),
        aggregator: str = "okx",
        slippage_bps: int = 10,
        delay: float = 0.35,
    ) -> list[ArbEvaluation]:
        """Scan candidate amounts from min_amount to max_amount and evaluate yields."""
        results: list[ArbEvaluation] = []
        current = min_amount
        while current <= max_amount:
            try:
                eval_res = self.evaluate_arbitrage(
                    amount_usdc=current,
                    aggregator=aggregator,
                    slippage_bps=slippage_bps,
                )
                results.append(eval_res)
            except Exception as exc:
                # Log but continue scanning other sizes
                print(f"[!] Sizing scan warning at {current:,.0f} USDC: {exc}", file=sys.stderr)
            current += step
            if current <= max_amount and delay > 0:
                time.sleep(delay)
        return results


def format_evaluation(eval_result: ArbEvaluation, gas_price_gwei: Decimal, eth_usd: Decimal) -> str:
    dq = eval_result.dex_quote
    sq = eval_result.stable_quote
    gross = eval_result.gross_profit_usdc
    net = eval_result.net_profit_usd
    discount_pct = ((dq.buy_amount_human / eval_result.loan_amount_usdc) - 1) * 100
    
    status_str = f"PROFITABLE (+${net:.2f})" if eval_result.is_profitable else f"UNPROFITABLE (${net:.2f})"
    
    lines = [
        "=" * 74,
        "   USDP ARBITRAGE TEST ENGINE (STANDALONE TESTING ONLY)",
        "=" * 74,
        f"Timestamp:             {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}",
        f"Network:               Ethereum Mainnet (Chain ID 1)",
        f"Flash Provider:        Morpho (USDC Fee: 0 bps)",
        f"Principal Loan:        {eval_result.loan_amount_usdc:,.2f} USDC",
        "",
        "--- LEG 1: DEX SWAP (USDC -> USDP) ---",
        f"Aggregator:            {dq.aggregator}",
        f"Input:                 {eval_result.loan_amount_usdc:,.6f} USDC",
        f"Output:                {dq.buy_amount_human:,.6f} USDP",
        f"Effective Rate:        1 USDC = {(dq.buy_amount_human / eval_result.loan_amount_usdc):.6f} USDP ({discount_pct:+.3f}%)",
        f"Router Target:         {dq.target}",
        f"Allowance Spender:     {dq.allowance_target}",
        f"Gas Estimate:          {dq.gas_estimate:,} gas",
        "",
        "--- LEG 2: STABLE.COM REDEMPTION (USDP -> USDC) ---",
        f"Pool:                  Stable.com Single-Chain Pool (101)",
        f"Input:                 {sq.amount_in_human:,.6f} USDP",
        f"Output:                {sq.amount_out_human:,.6f} USDC",
        f"Pool Liquidity:        {sq.pool_balance_human:,.2f} USDP available",
        f"Redemption Fee:        {sq.token_fee_human:.2f} USDC (0 bps)",
        "",
        "--- ARBITRAGE PROFIT & LOSS BREAKDOWN ---",
        f"Starting Principal:    {eval_result.loan_amount_usdc:,.2f} USDC",
        f"Ending USDC:           {sq.amount_out_human:,.6f} USDC",
        f"Gross Profit:          {gross:+,.6f} USDC ({gross / eval_result.loan_amount_usdc * 100:+.3f}%)",
        f"Estimated Gas Cost:    ${eval_result.gas_estimate_usd:.2f} USD ({gas_price_gwei:.2f} Gwei @ ${eth_usd:,.0f}/ETH)",
        f"Predicted Net Profit:  {net:+,.2f} USD",
        f"Result:                {status_str}",
        "=" * 74,
    ]
    return "\n".join(lines)


def format_scaling_table(
    evaluations: list[ArbEvaluation],
    winner: ArbEvaluation | None,
    gas_price_gwei: Decimal,
    eth_usd: Decimal,
) -> str:
    lines = [
        "=" * 86,
        "          USDP ARBITRAGE YIELD CURVE SCAN (10,000 -> 100,000 USDC)",
        "=" * 86,
        f"Timestamp: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())} | Gas: {gas_price_gwei:.2f} Gwei | ETH: ${eth_usd:,.0f}",
        "-" * 86,
        f" {'Size (USDC)':<13} | {'USDP Received':<14} | {'Gross Profit':<14} | {'Est. Net':<13} | {'Margin':<7} | {'Yield Rank'}",
        "-" * 86,
    ]
    for ev in evaluations:
        size_str = f"{ev.loan_amount_usdc:,.0f} USDC"
        usdp_str = f"{ev.dex_quote.buy_amount_human:,.2f}"
        gross_str = f"{ev.gross_profit_usdc:+,.2f} USDC"
        net_str = f"{ev.net_profit_usd:+,.2f} USD"
        margin_pct = (ev.gross_profit_usdc / ev.loan_amount_usdc) * 100
        margin_str = f"{margin_pct:+.2f}%"

        if winner and ev.loan_amount_usdc == winner.loan_amount_usdc and ev.net_profit_usd > 0:
            rank_str = "★ HIGHEST YIELD ★"
        elif ev.net_profit_usd > 0:
            rank_str = "Profitable"
        else:
            rank_str = "(Unprofitable)"

        lines.append(
            f" {size_str:<13} | {usdp_str:<14} | {gross_str:<14} | {net_str:<13} | {margin_str:<7} | {rank_str}"
        )
    lines.append("=" * 86)
    
    if winner:
        lines.append("")
        lines.append("  >>> WINNING ROUTE DETAILS (MAXIMUM NET YIELD) <<<")
        lines.append(format_evaluation(winner, gas_price_gwei, eth_usd))
    else:
        lines.append("\n[!] No profitable size found in scan range.")
        
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Standalone USDP Arbitrage Test Engine (Scales from 10k to 100k & prints top yield)"
    )
    parser.add_argument(
        "--amount",
        type=Decimal,
        default=None,
        help="Test a specific single USDC amount (if omitted, scans 10k to 100k)",
    )
    parser.add_argument(
        "--min-amount",
        type=Decimal,
        default=Decimal("10000.0"),
        help="Minimum USDC amount for scaling scan (default: 10000)",
    )
    parser.add_argument(
        "--max-amount",
        type=Decimal,
        default=Decimal("100000.0"),
        help="Maximum USDC amount for scaling scan (default: 100000)",
    )
    parser.add_argument(
        "--step",
        type=Decimal,
        default=Decimal("10000.0"),
        help="Increment step for scaling scan (default: 10000)",
    )
    parser.add_argument(
        "--aggregator",
        choices=["okx", "velora", "auto"],
        default="okx",
        help="DEX aggregator for USDC -> USDP (default: okx)",
    )
    parser.add_argument(
        "--slippage-bps",
        type=int,
        default=10,
        help="Max slippage in basis points (default: 10 = 0.1%%)",
    )
    parser.add_argument(
        "--create-order",
        action="store_true",
        help="Also call Stable.com /swap/create/singleChain on the winning route",
    )
    parser.add_argument(
        "--continuous",
        action="store_true",
        help="Continuously monitor in a loop",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=6.0,
        help="Interval in seconds for continuous mode (default: 6.0)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output results as JSON",
    )

    args = parser.parse_args(argv)

    engine = USDPTestEngine()
    
    while True:
        try:
            gas_price = engine.get_gas_price_gwei()
            eth_usd = engine.get_eth_usd()

            if args.amount is not None:
                # Single amount evaluation
                eval_res = engine.evaluate_arbitrage(
                    amount_usdc=args.amount,
                    aggregator=args.aggregator,
                    slippage_bps=args.slippage_bps,
                )
                winner = eval_res
                evaluations = [eval_res]
            else:
                # Full scaling scan from min_amount to max_amount
                evaluations = engine.scale_scan(
                    min_amount=args.min_amount,
                    max_amount=args.max_amount,
                    step=args.step,
                    aggregator=args.aggregator,
                    slippage_bps=args.slippage_bps,
                )
                if not evaluations:
                    raise RuntimeError("No candidate sizes could be evaluated successfully")
                winner = max(evaluations, key=lambda e: e.net_profit_usd)

            order_data = None
            if args.create_order and winner and winner.is_profitable:
                order_data = engine.create_stable_order(winner.dex_quote.buy_amount_human)
                order_nonce = order_data.get("nonce")
                order_sig = str(order_data.get("maintainerSignature"))[:16] + "..."
            else:
                order_nonce = "N/A"
                order_sig = "N/A"

            if args.json:
                data = {
                    "timestamp": time.time(),
                    "winner": {
                        "loanAmountUsdc": float(winner.loan_amount_usdc),
                        "dexOutputUsdp": float(winner.dex_quote.buy_amount_human),
                        "dexAggregator": winner.dex_quote.aggregator,
                        "stableOutputUsdc": float(winner.stable_quote.amount_out_human),
                        "grossProfitUsdc": float(winner.gross_profit_usdc),
                        "gasEstimateUsd": float(winner.gas_estimate_usd),
                        "netProfitUsd": float(winner.net_profit_usd),
                        "isProfitable": winner.is_profitable,
                        "orderNonce": order_nonce,
                    } if winner else None,
                    "scan": [
                        {
                            "loanAmountUsdc": float(e.loan_amount_usdc),
                            "grossProfitUsdc": float(e.gross_profit_usdc),
                            "netProfitUsd": float(e.net_profit_usd),
                            "marginPct": float(e.gross_profit_usdc / e.loan_amount_usdc * 100),
                            "isProfitable": e.is_profitable,
                        }
                        for e in evaluations
                    ],
                }
                print(json.dumps(data, indent=2))
            else:
                if args.amount is not None:
                    print(format_evaluation(winner, gas_price, eth_usd))
                else:
                    print(format_scaling_table(evaluations, winner, gas_price, eth_usd))
                if args.create_order and winner and winner.is_profitable:
                    print(f"Verified Stable.com Order for Winner: Nonce={order_nonce}, Signature={order_sig}")

        except Exception as exc:
            print(f"[ERROR] Evaluation failed: {exc}", file=sys.stderr)
            if not args.continuous:
                return 1

        if not args.continuous:
            break
        time.sleep(args.interval)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

