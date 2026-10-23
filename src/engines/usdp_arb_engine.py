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
import re
import sys
import time
from typing import Any, Iterable
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

try:
    from web3 import Web3
except ImportError:
    Web3 = None

import requests

# Contract Addresses on Ethereum Mainnet
USDC_ADDRESS = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
USDP_ADDRESS = "0x8E870D67F660D95d5be530380D0eC0bd388289E1"
MORPHO_ADDRESS = "0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb"
STABLE_POOL_ADDRESS = "0xCfC1bc6013eD89D484c626dd9ee5EB7bc1a1d9Da"
DEFAULT_EXECUTOR = "0x20f5Cc012bBb5323EfBFcC91A7d0C960175D3b2c"
OKX_DEFAULT_ROUTER = "0x8feAB81D36E7576107D5dE0758c1b839Be31B4F6"
OKX_DEFAULT_SPENDER = "0x40aA958dd87FC8305b97f2BA922CDdCa374bcD7f"

USDC_DECIMALS = 6
USDP_DECIMALS = 18
STABLE_CHAIN_ID = "101"
STABLE_BASE_URL = "https://api-defi.stable.com"
MATCHA_BASE_URL = "https://meta.matcha.xyz"
MATCHA_AGGREGATORS = (
    "0x",
    "1inch",
    "OKX",
    "KyberSwap",
    "Barter",
    "Bebop",
    "Bitget",
    "ParaSwap",
    "Enso",
)
ENSO_BASE_URLS = [
    "https://api.enso.finance",
    "https://api.enso.build",
]
DEFAULT_RPC_URLS = [
    "https://rpc.mevblocker.io",
    "https://1rpc.io/eth",
    "https://eth-mainnet.public.blastapi.io",
    "https://eth.drpc.org",
    "https://ethereum.publicnode.com",
]

EXECUTOR_ABI = [
    {
        "inputs": [{"internalType": "address", "name": "token", "type": "address"}],
        "name": "supportsLoanToken",
        "outputs": [{"internalType": "bool", "name": "", "type": "bool"}],
        "stateMutability": "pure",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "owner",
        "outputs": [{"internalType": "address", "name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"internalType": "uint256", "name": "loanAmount", "type": "uint256"},
            {"internalType": "address", "name": "loanToken", "type": "address"},
            {"internalType": "address", "name": "intermediateToken", "type": "address"},
            {
                "components": [
                    {"internalType": "address", "name": "target", "type": "address"},
                    {"internalType": "address", "name": "allowanceTarget", "type": "address"},
                    {"internalType": "uint256", "name": "sellAmount", "type": "uint256"},
                    {"internalType": "uint256", "name": "value", "type": "uint256"},
                    {"internalType": "bytes", "name": "data", "type": "bytes"},
                ],
                "internalType": "struct MorphoMatchaStableArbUsdc.MatchaRoute",
                "name": "matcha",
                "type": "tuple",
            },
            {
                "components": [
                    {"internalType": "uint256", "name": "amountIn", "type": "uint256"},
                    {"internalType": "uint64", "name": "deadline", "type": "uint64"},
                    {"internalType": "uint256", "name": "nonce", "type": "uint256"},
                    {"internalType": "bytes", "name": "maintainerSignature", "type": "bytes"},
                    {"internalType": "uint256", "name": "executionFeeNative", "type": "uint256"},
                ],
                "internalType": "struct MorphoMatchaStableArbUsdc.StableOrder",
                "name": "stable",
                "type": "tuple",
            },
            {"internalType": "uint256", "name": "minProfit", "type": "uint256"},
        ],
        "name": "executeArbitrageWithTokens",
        "outputs": [],
        "stateMutability": "payable",
        "type": "function",
    },
]

ERROR_SELECTORS: dict[str, str] = {
    "0x30cd7471": "NotOwner(): Caller is not the executor owner",
    "0x10dfc033": "WrongChain(): Chain ID is not 1 (Ethereum Mainnet)",
    "0xe6c4247b": "InvalidAddress(): Zero address passed",
    "0x2c5211c6": "InvalidAmount(): Loan or swap amount is zero",
    "0x84e505d2": "InvalidRoute(): Route parameter mismatch or expired order",
    "0xfcf3a30b": "InvalidLoanToken(): Token is not in the contract's supported whitelist",
    "0x2723e9c2": "InvalidNativeValue(): Native ETH msg.value mismatch",
    "0xf7a632f5": "InvalidCallback(): Unauthorized Morpho/Aave callback",
    "0x3e0aa470": "InsufficientMatchaOutput(uint256,uint256): DEX swap output was less than required by Stable.com order",
    "0x4e88422a": "InsufficientProfit(uint256,uint256): Ending USDC was less than required loan repayment + minProfit",
    "0xfecb69b4": "TokenCallFailed(address): ERC20 transfer or approve failed",
    "0xe1caab11": "ExternalCallFailed(address,bytes): External DEX or Stable call failed",
}


@dataclass
class SimulationResult:
    success: bool
    estimated_gas: int
    native_value_wei: int
    min_profit_raw: int
    order_nonce: int | None
    order_deadline: int | None
    maintainer_signature: str | None
    stable_amount_in_raw: int = 0
    error_selector: str | None = None
    error_message: str | None = None
    remedy: str | None = None
    raw_error: str | None = None


@dataclass
class ExecutionResult:
    mode: str  # "dry-run" | "live"
    success: bool
    simulation: SimulationResult
    tx_hash: str | None = None
    block_number: int | None = None
    gas_used: int | None = None
    effective_gas_price_gwei: Decimal | None = None
    actual_gas_cost_usd: Decimal | None = None
    net_realized_profit_usd: Decimal | None = None
    etherscan_url: str | None = None
    broadcast_method: str | None = None
    error: str | None = None


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
        executor: str | None = None,
        operator: str | None = None,
        rpc_url: str | None = None,
        timeout: float = 12.0,
    ):
        self.executor = (
            executor
            or os.getenv("ETH_ARB_STABLECOIN_EXECUTOR")
            or os.getenv("ETH_EXECUTOR_ADDRESS")
            or DEFAULT_EXECUTOR
        )
        self.operator = operator or os.getenv("ETH_OPERATOR_ADDRESS")
        self.timeout = timeout
        
        env_rpc = os.getenv("ETH_RPC_URL")
        fallbacks = [f.strip() for f in os.getenv("ETH_RPC_FALLBACKS", "").split(",") if f.strip()]
        self.rpc_urls = [r for r in ([rpc_url] if rpc_url else []) + ([env_rpc] if env_rpc else []) + fallbacks + DEFAULT_RPC_URLS if r]
        self.rpc_url = self.rpc_urls[0] if self.rpc_urls else "https://1rpc.io/eth"
        self.okx_key = os.getenv("OKX_API_KEY", "").strip()
        self.okx_secret = os.getenv("OKX_SECRET_KEY", "").strip()
        self.okx_passphrase = os.getenv("OKX_PASSPHRASE", "").strip()
        self.okx_project_id = os.getenv("OKX_PROJECT_ID", "").strip()
        self.enso_key = os.getenv("ENSO_API_KEY", "").strip()
        
        self.session = requests.Session()
        self.session.headers.update({"accept": "application/json", "user-agent": "arbbot-usdp-test/1.0"})
        self._cookies_injected = False
        self._bridge_disabled_until = 0.0

    def get_working_web3(self) -> Any:
        if Web3 is None:
            raise RuntimeError("web3 library is required for live simulation and execution. Run: pip install web3")
        for url in self.rpc_urls:
            try:
                cand = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": self.timeout}))
                if cand.is_connected():
                    return cand
            except Exception:
                continue
        raise RuntimeError(f"All RPC endpoints failed to connect for Web3: {self.rpc_urls}")

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

    def missing_okx_credentials(self) -> list[str]:
        """Names of the OKX credential env vars that are not set (never their values)."""
        configured = {
            "OKX_API_KEY": self.okx_key,
            "OKX_SECRET_KEY": self.okx_secret,
            "OKX_PASSPHRASE": self.okx_passphrase,
            "OKX_PROJECT_ID": self.okx_project_id,
        }
        return [name for name, value in configured.items() if not value]

    def quote_dex_okx(self, usdc_amount: Decimal, slippage_bps: int = 10) -> DexQuote:
        """Fetch quote from OKX DEX Aggregator for USDC -> USDP."""
        missing = self.missing_okx_credentials()
        if missing:
            raise RuntimeError(f"OKX quote unavailable: set {', '.join(missing)} in .env")

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

    def quote_dex_matcha(
        self,
        usdc_amount: Decimal,
        slippage_bps: int = 10,
        aggregators: Iterable[str] | None = None,
    ) -> DexQuote:
        """Fetch quote from meta.matcha.xyz across DEX aggregators for USDC -> USDP."""
        raw_in = int(usdc_amount * Decimal(10**USDC_DECIMALS))
        gas_price_wei = int(self.get_gas_price_gwei() * Decimal(10**9))
        effective_slippage = max(10, int(slippage_bps))

        payload = {
            "chainId": 1,
            "isAllowanceHolderFlow": True,
            "gasPrice": str(gas_price_wei),
            "sellTokenAddress": USDC_ADDRESS.lower(),
            "sellTokenDecimals": USDC_DECIMALS,
            "buyTokenAddress": USDP_ADDRESS.lower(),
            "buyTokenDecimals": USDP_DECIMALS,
            "sellAmount": str(raw_in),
            "slippageBps": effective_slippage,
            "slippagePpm": effective_slippage * 100,
            "taker": self.executor.lower(),
        }

        target_aggs = list(aggregators) if aggregators else ["0x", "1inch", "OKX", "KyberSwap"]
        quotes_map: dict[str, Any] = {}

        # 1. Try local Matcha browser bridge if active and not in backoff
        now = time.monotonic()
        if now >= self._bridge_disabled_until:
            try:
                from src.engines.matcha_browser_bridge import fetch_bridge_quotes
                bridge_res = fetch_bridge_quotes("ethereum", payload, target_aggs, timeout=min(2.0, self.timeout))
                if isinstance(bridge_res, dict):
                    quotes_map = bridge_res
            except Exception:
                # Back off bridge for 45s if inactive or unreachable to keep scan blazing fast
                self._bridge_disabled_until = now + 45.0

        # 2. Try direct HTTP to meta.matcha.xyz if bridge didn't provide quotes
        if not quotes_map:
            matcha_headers = {
                "accept": "application/json, text/plain, */*",
                "content-type": "application/json",
                "origin": "https://meta.matcha.xyz",
                "referer": "https://meta.matcha.xyz/ethereum",
                "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            }
            if not self._cookies_injected:
                try:
                    from src.engines.matcha_cookie_manager import inject_matcha_cookies
                    inject_matcha_cookies(self.session, target_url="https://meta.matcha.xyz/ethereum")
                except Exception:
                    pass
                self._cookies_injected = True

            try:
                comp_resp = self.session.post(
                    f"{MATCHA_BASE_URL}/api/competitions",
                    json=payload,
                    headers=matcha_headers,
                    timeout=min(4.0, self.timeout),
                )
                if comp_resp.status_code == 200:
                    comp_data = comp_resp.json()
                    comp_id = comp_data.get("competitionId") or comp_data.get("id")
                    if comp_id:
                        for agg in target_aggs:
                            try:
                                q_resp = self.session.post(
                                    f"{MATCHA_BASE_URL}/api/quotes?aggregator={agg}",
                                    json={"competitionId": comp_id, "aggregator": agg},
                                    headers=matcha_headers,
                                    timeout=2.0,
                                )
                                if q_resp.status_code == 200:
                                    quotes_map[agg] = q_resp.json()
                            except Exception:
                                continue
            except Exception:
                pass

        # 3. Parse and select best simulated quote across MetaMatcha aggregators
        best_quote: DexQuote | None = None
        best_buy_amount: int = 0

        for agg, q_data in quotes_map.items():
            if not isinstance(q_data, dict):
                continue
            holder = q_data.get("allowanceHolder")
            if not isinstance(holder, dict):
                holder = q_data.get("data", {}).get("allowanceHolder") if isinstance(q_data.get("data"), dict) else None
            if not isinstance(holder, dict):
                continue
            quote = holder.get("quote") or {}
            tx = quote.get("transaction") if isinstance(quote.get("transaction"), dict) else quote
            target = tx.get("to") or quote.get("to")
            allowance_target = quote.get("allowanceTarget") or holder.get("allowanceTarget")
            data = tx.get("data") or quote.get("data")
            buy_amt = int(quote.get("buyAmount") or 0)
            gas_est = int(quote.get("gas") or tx.get("gas") or 260_000)
            val = int(tx.get("value") or quote.get("value") or 0)

            if buy_amt > best_buy_amount and target and allowance_target and data:
                best_buy_amount = buy_amt
                best_quote = DexQuote(
                    aggregator=f"meta.matcha ({agg})",
                    target=target,
                    allowance_target=allowance_target,
                    sell_amount_raw=raw_in,
                    buy_amount_raw=buy_amt,
                    buy_amount_human=Decimal(buy_amt) / Decimal(10**USDP_DECIMALS),
                    data=data,
                    value=val,
                    gas_estimate=gas_est,
                )

        if best_quote:
            return best_quote

        # 4. If MetaMatcha endpoint is challenged/blocked by WAF or returns no liquidity,
        # fall back to Enso or OKX, selecting whichever yields the higher USDP output
        candidates: list[DexQuote] = []
        try:
            candidates.append(self.quote_dex_enso(usdc_amount, slippage_bps))
        except Exception:
            pass

        try:
            candidates.append(self.quote_dex_okx(usdc_amount, slippage_bps))
        except Exception:
            pass

        if candidates:
            best_fallback = max(candidates, key=lambda q: q.buy_amount_raw)
            return DexQuote(
                aggregator=f"meta.matcha ({best_fallback.aggregator})",
                target=best_fallback.target,
                allowance_target=best_fallback.allowance_target,
                sell_amount_raw=best_fallback.sell_amount_raw,
                buy_amount_raw=best_fallback.buy_amount_raw,
                buy_amount_human=best_fallback.buy_amount_human,
                data=best_fallback.data,
                value=best_fallback.value,
                gas_estimate=best_fallback.gas_estimate,
            )

        # 5. Last resort fallback to Velora
        return self.quote_dex_velora(usdc_amount, slippage_bps)

    def quote_dex_enso(self, usdc_amount: Decimal, slippage_bps: int = 10) -> DexQuote:
        """Fetch quote from Enso Router API for USDC -> USDP on Ethereum."""
        if not self.enso_key:
            raise RuntimeError("Enso quote unavailable: set ENSO_API_KEY in .env")
        raw_in = int(usdc_amount * Decimal(10**USDC_DECIMALS))
        headers = {
            "Authorization": f"Bearer {self.enso_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        payload = {
            "chainId": 1,
            "fromAddress": self.executor,
            "tokenIn": [USDC_ADDRESS],
            "tokenOut": [USDP_ADDRESS],
            "amountIn": [str(raw_in)],
            "routingStrategy": "router",
            "slippage": str(max(10, slippage_bps)),
        }
        
        last_exc: Exception | None = None
        for base_url in ENSO_BASE_URLS:
            try:
                resp = self.session.post(
                    f"{base_url}/api/v1/shortcuts/route",
                    headers=headers,
                    json=payload,
                    timeout=min(6.0, self.timeout),
                )
                if resp.status_code == 200:
                    data = resp.json()
                    out_raw = int(data.get("amountOut") or 0)
                    if out_raw <= 0:
                        raise RuntimeError(f"Enso returned invalid amountOut: {data}")
                    out_human = Decimal(out_raw) / Decimal(10**USDP_DECIMALS)
                    
                    tx = data.get("tx") or {}
                    target = tx.get("to")
                    calldata = tx.get("data", "0x")
                    value = int(tx.get("value") or 0)
                    gas_est = int(data.get("gas") or 320_000)
                    
                    pre_txs = data.get("preTransactions") or []
                    allowance_target = None
                    for ptx in pre_txs:
                        if isinstance(ptx, dict) and ptx.get("spender"):
                            allowance_target = ptx.get("spender")
                            break
                    if not allowance_target:
                        allowance_target = target
                        
                    if not target or not calldata:
                        raise RuntimeError(f"Enso response missing target or calldata: {data}")
                        
                    return DexQuote(
                        aggregator="Enso",
                        target=target,
                        allowance_target=allowance_target,
                        sell_amount_raw=raw_in,
                        buy_amount_raw=out_raw,
                        buy_amount_human=out_human,
                        data=calldata,
                        value=value,
                        gas_estimate=gas_est,
                    )
                else:
                    last_exc = RuntimeError(f"Enso HTTP {resp.status_code}: {resp.text[:200]}")
            except Exception as exc:
                last_exc = exc
                continue
                
        raise RuntimeError(f"Enso quote failed across endpoints: {last_exc}")

    def quote_dex(self, usdc_amount: Decimal, aggregator: str = "matcha", slippage_bps: int = 10) -> DexQuote:
        agg = aggregator.lower().strip()
        if agg in ("matcha", "meta.matcha", "metamatcha"):
            return self.quote_dex_matcha(usdc_amount, slippage_bps)
        if agg == "enso":
            try:
                return self.quote_dex_enso(usdc_amount, slippage_bps)
            except Exception as e:
                # Fallback to OKX then Velora if Enso is out of credits or down
                for fn in (self.quote_dex_okx, self.quote_dex_velora):
                    try:
                        return fn(usdc_amount, slippage_bps)
                    except Exception:
                        continue
                raise
        if agg == "okx":
            return self.quote_dex_okx(usdc_amount, slippage_bps)
        if agg == "velora":
            return self.quote_dex_velora(usdc_amount, slippage_bps)
        
        # auto: try matcha, enso, okx, velora and return best quote
        quotes: list[DexQuote] = []
        for fn in (self.quote_dex_matcha, self.quote_dex_enso, self.quote_dex_okx, self.quote_dex_velora):
            try:
                quotes.append(fn(usdc_amount, slippage_bps))
            except Exception:
                continue
        if quotes:
            return max(quotes, key=lambda q: q.buy_amount_raw)
        raise RuntimeError("All DEX aggregators (Matcha, Enso, OKX, Velora) failed to return a quote")

    def quote_stable(self, usdp_amount: Decimal, attempts: int = 3) -> StableQuote:
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
        last_exc: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
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
            except Exception as exc:
                last_exc = exc
                if attempt < attempts:
                    time.sleep(0.4 * attempt)
        raise RuntimeError(f"Failed to quote Stable.com after {attempts} attempts: {last_exc}")

    def create_stable_order(self, usdp_amount: Decimal, attempts: int = 3) -> dict[str, Any]:
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
        last_exc: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
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
                data["amountInHuman"] = usdp_amount
                data["amountInRaw"] = int(usdp_amount * Decimal(10**USDP_DECIMALS))
                return data
            except Exception as exc:
                last_exc = exc
                if attempt < attempts:
                    time.sleep(0.5 * attempt)
        raise RuntimeError(f"Failed to create Stable.com order after {attempts} attempts: {last_exc}")

    def evaluate_arbitrage(
        self,
        amount_usdc: Decimal = Decimal("10000.0"),
        aggregator: str = "matcha",
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
        aggregator: str = "matcha",
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

    def simulate_live_arbitrage(
        self,
        evaluation: ArbEvaluation,
        order_data: dict[str, Any] | None = None,
        min_profit_usdc: Decimal = Decimal("1.0"),
        leg1_buffer_bps: int = 0,
    ) -> SimulationResult:
        """Run pre-flight on-chain simulation (eth_call) against Ethereum mainnet."""
        w3 = self.get_working_web3()
        executor_cs = w3.to_checksum_address(self.executor)

        # Determine operator address
        operator_addr = self.operator
        if not operator_addr:
            priv_key = os.getenv("ETH_OPERATOR_PRIVATE_KEY")
            if priv_key:
                try:
                    operator_addr = w3.eth.account.from_key(priv_key).address
                except Exception:
                    pass
        if not operator_addr:
            operator_addr = "0x50dA32E628b45AbB1335924086Ca0013b9d4eC1C"
        operator_cs = w3.to_checksum_address(operator_addr)

        # Check executor bytecode
        try:
            code = w3.eth.get_code(executor_cs)
            if len(code) == 0:
                return SimulationResult(
                    success=False,
                    estimated_gas=0,
                    native_value_wei=0,
                    min_profit_raw=0,
                    order_nonce=None,
                    order_deadline=None,
                    maintainer_signature=None,
                    error_message=f"Executor contract {executor_cs} has no deployed bytecode on Ethereum mainnet",
                    remedy="Deploy the contract using python deploy_usdp_executor.py --send --confirm-mainnet DEPLOY_EXECUTOR",
                )
        except Exception as exc:
            return SimulationResult(
                success=False,
                estimated_gas=0,
                native_value_wei=0,
                min_profit_raw=0,
                order_nonce=None,
                order_deadline=None,
                maintainer_signature=None,
                error_message=f"Failed to check executor code: {exc}",
                remedy="Check RPC connection and executor address configuration",
            )

        # Check contract owner
        contract = w3.eth.contract(address=executor_cs, abi=EXECUTOR_ABI)
        try:
            owner_cs = w3.to_checksum_address(contract.functions.owner().call())
            if owner_cs.lower() != operator_cs.lower():
                return SimulationResult(
                    success=False,
                    estimated_gas=0,
                    native_value_wei=0,
                    min_profit_raw=0,
                    order_nonce=None,
                    order_deadline=None,
                    maintainer_signature=None,
                    error_selector="0x30cd7471",
                    error_message=f"Operator {operator_cs} is not the owner of executor {executor_cs} (owner is {owner_cs})",
                    remedy="Configure ETH_OPERATOR_PRIVATE_KEY for the executor owner wallet",
                )
        except Exception:
            pass

        # Check if contract supports USDP loan token
        try:
            is_supported = contract.functions.supportsLoanToken(w3.to_checksum_address(USDP_ADDRESS)).call()
            if not is_supported:
                return SimulationResult(
                    success=False,
                    estimated_gas=0,
                    native_value_wei=0,
                    min_profit_raw=0,
                    order_nonce=None,
                    order_deadline=None,
                    maintainer_signature=None,
                    error_selector="0xfcf3a30b",
                    error_message=f"InvalidLoanToken(): Executor contract {executor_cs} whitelists [USDC, PYUSD, USDG, USDT], but does not yet whitelist USDP ({USDP_ADDRESS})",
                    remedy="Deploy updated USDP executor: python deploy_usdp_executor.py --send --confirm-mainnet DEPLOY_EXECUTOR",
                )
        except Exception:
            pass

        # Ensure order exists
        if order_data is None:
            buffer_factor = Decimal(1) - (Decimal(leg1_buffer_bps) / Decimal(10000))
            safe_amount = (evaluation.dex_quote.buy_amount_human * buffer_factor).quantize(Decimal("0.000001"))
            order_data = self.create_stable_order(safe_amount)

        if "amountInRaw" in order_data:
            stable_amount_in_raw = int(order_data["amountInRaw"])
        elif "amountInHuman" in order_data:
            stable_amount_in_raw = int(Decimal(str(order_data["amountInHuman"])) * Decimal(10**USDP_DECIMALS))
        else:
            buffer_factor = Decimal(1) - (Decimal(leg1_buffer_bps) / Decimal(10000))
            safe_amount = (evaluation.dex_quote.buy_amount_human * buffer_factor).quantize(Decimal("0.000001"))
            stable_amount_in_raw = int(safe_amount * Decimal(10**USDP_DECIMALS))

        raw_sig = order_data["maintainerSignature"]
        sig_bytes = bytes.fromhex(raw_sig[2:] if raw_sig.startswith("0x") else raw_sig)
        data_hex = evaluation.dex_quote.data
        data_bytes = bytes.fromhex(data_hex[2:] if data_hex.startswith("0x") else data_hex)
        native_fee = int(order_data.get("executionFeeNative") or order_data.get("nativeFee") or 0)
        native_val = int(evaluation.dex_quote.value) + native_fee

        matcha_tuple = (
            w3.to_checksum_address(evaluation.dex_quote.target),
            w3.to_checksum_address(evaluation.dex_quote.allowance_target),
            int(evaluation.dex_quote.sell_amount_raw),
            int(evaluation.dex_quote.value),
            data_bytes,
        )

        stable_tuple = (
            stable_amount_in_raw,
            int(order_data["deadline"]),
            int(order_data["nonce"]),
            sig_bytes,
            native_fee,
        )

        min_profit_raw = int(min_profit_usdc * Decimal(10**USDC_DECIMALS))

        fn = contract.functions.executeArbitrageWithTokens(
            int(evaluation.dex_quote.sell_amount_raw),
            w3.to_checksum_address(USDC_ADDRESS),
            w3.to_checksum_address(USDP_ADDRESS),
            matcha_tuple,
            stable_tuple,
            min_profit_raw,
        )

        call_params = {"from": operator_cs, "value": native_val}
        err_str = ""
        sim_success = False
        for curr_url in self.rpc_urls:
            try:
                if curr_url != self.rpc_url:
                    alt_w3 = Web3(Web3.HTTPProvider(curr_url, request_kwargs={"timeout": self.timeout}))
                    alt_contract = alt_w3.eth.contract(address=executor_cs, abi=EXECUTOR_ABI)
                    curr_fn = alt_contract.functions.executeArbitrageWithTokens(
                        int(evaluation.dex_quote.sell_amount_raw),
                        alt_w3.to_checksum_address(USDC_ADDRESS),
                        alt_w3.to_checksum_address(USDP_ADDRESS),
                        matcha_tuple,
                        stable_tuple,
                        min_profit_raw,
                    )
                else:
                    curr_fn = fn
                curr_fn.call(call_params)
                estimated_gas = curr_fn.estimate_gas(call_params)
                return SimulationResult(
                    success=True,
                    estimated_gas=int(estimated_gas),
                    native_value_wei=native_val,
                    min_profit_raw=min_profit_raw,
                    order_nonce=int(order_data["nonce"]),
                    order_deadline=int(order_data["deadline"]),
                    maintainer_signature=str(order_data["maintainerSignature"]),
                    stable_amount_in_raw=stable_amount_in_raw,
                )
            except Exception as exc:
                err_str = str(exc)
                if any(k in err_str for k in ("429", "Too Many Requests", "timeout", "Connection reset", "Client Error")):
                    continue
                break
        try:
            pass
        except Exception:
            pass
            matched_sel = None
            for sel in ERROR_SELECTORS:
                if sel in err_str:
                    matched_sel = sel
                    break

            remedy = None
            error_msg = None

            match_matcha = re.search(r"(?:0x)?3e0aa470([0-9a-fA-F]{64})([0-9a-fA-F]{64})", err_str)
            if match_matcha:
                matched_sel = "0x3e0aa470"
                rec_wei = int(match_matcha.group(1), 16)
                req_wei = int(match_matcha.group(2), 16)
                rec_human = Decimal(rec_wei) / Decimal(10**18)
                req_human = Decimal(req_wei) / Decimal(10**18)
                diff_human = req_human - rec_human
                error_msg = (
                    f"InsufficientMatchaOutput: DEX swap delivered {rec_human:.6f} USDP, "
                    f"which was {diff_human:.6f} USDP short of Stable.com order requirement ({req_human:.6f} USDP)."
                )
                remedy = f"Apply a buffer via --leg1-buffer-bps (e.g. 5 bps = 0.05%%; shortfall was only {diff_human:.4f} USDP)"

            match_profit = re.search(r"(?:0x)?4e88422a([0-9a-fA-F]{64})([0-9a-fA-F]{64})", err_str)
            if match_profit:
                matched_sel = "0x4e88422a"
                avail_wei = int(match_profit.group(1), 16)
                req_wei = int(match_profit.group(2), 16)
                avail_human = Decimal(avail_wei) / Decimal(10**6)
                req_human = Decimal(req_wei) / Decimal(10**6)
                error_msg = (
                    f"InsufficientProfit: Net available USDC was {avail_human:.6f}, "
                    f"below required repayment + minProfit ({req_human:.6f} USDC)."
                )
                remedy = "Lower --min-profit-usd or choose a higher yield size."

            if not error_msg:
                if matched_sel == "0xfcf3a30b":
                    remedy = f"Deploy/use updated USDP executor: python deploy_usdp_executor.py --send --confirm-mainnet DEPLOY_EXECUTOR"
                elif matched_sel in ("0x30cd7471", "0x82b42900"):
                    remedy = f"Configure operator {operator_cs} as owner of executor {executor_cs}"
                elif matched_sel == "0x3e0aa470":
                    remedy = "DEX output slipped below minimum required. Try slightly higher slippage or pass --leg1-buffer-bps."

                error_msg = ERROR_SELECTORS.get(matched_sel, f"eth_call reverted: {err_str}") if matched_sel else f"eth_call reverted: {err_str}"

            return SimulationResult(
                success=False,
                estimated_gas=0,
                native_value_wei=native_val,
                min_profit_raw=min_profit_raw,
                order_nonce=int(order_data.get("nonce", 0)),
                order_deadline=int(order_data.get("deadline", 0)),
                maintainer_signature=str(order_data.get("maintainerSignature", "")),
                stable_amount_in_raw=stable_amount_in_raw,
                error_selector=matched_sel,
                error_message=error_msg,
                remedy=remedy,
                raw_error=err_str,
            )

    def execute_live_arbitrage(
        self,
        evaluation: ArbEvaluation,
        order_data: dict[str, Any] | None = None,
        min_profit_usdc: Decimal = Decimal("1.0"),
        gas_multiplier: Decimal = Decimal("1.15"),
        max_fee_gwei: Decimal | None = None,
        priority_fee_gwei: Decimal = Decimal("0.05"),
        receipt_timeout: int = 60,
        leg1_buffer_bps: int = 0,
        dry_run: bool = False,
    ) -> ExecutionResult:
        """Execute or dry-run the USDP arbitrage transaction on Ethereum mainnet."""
        # 1. Run Pre-flight On-Chain Simulation
        sim = self.simulate_live_arbitrage(
            evaluation=evaluation,
            order_data=order_data,
            min_profit_usdc=min_profit_usdc,
            leg1_buffer_bps=leg1_buffer_bps,
        )

        if not sim.success:
            return ExecutionResult(
                mode="dry-run" if dry_run else "live",
                success=False,
                simulation=sim,
                error=sim.error_message,
            )

        w3 = self.get_working_web3()
        private_key = os.getenv("ETH_OPERATOR_PRIVATE_KEY", "").strip()
        if not private_key and not dry_run:
            return ExecutionResult(
                mode="dry-run",
                success=False,
                simulation=sim,
                error="ETH_OPERATOR_PRIVATE_KEY is missing from environment; cannot sign transaction",
            )

        if private_key:
            account = w3.eth.account.from_key(private_key)
            operator_cs = account.address
        else:
            operator_cs = w3.to_checksum_address(self.operator or "0x50dA32E628b45AbB1335924086Ca0013b9d4eC1C")

        executor_cs = w3.to_checksum_address(self.executor)
        contract = w3.eth.contract(address=executor_cs, abi=EXECUTOR_ABI)

        raw_sig = sim.maintainer_signature or ""
        sig_bytes = bytes.fromhex(raw_sig[2:] if raw_sig.startswith("0x") else raw_sig)
        data_hex = evaluation.dex_quote.data
        data_bytes = bytes.fromhex(data_hex[2:] if data_hex.startswith("0x") else data_hex)

        native_val = sim.native_value_wei
        native_fee = native_val - int(evaluation.dex_quote.value)

        matcha_tuple = (
            w3.to_checksum_address(evaluation.dex_quote.target),
            w3.to_checksum_address(evaluation.dex_quote.allowance_target),
            int(evaluation.dex_quote.sell_amount_raw),
            int(evaluation.dex_quote.value),
            data_bytes,
        )

        stable_tuple = (
            int(sim.stable_amount_in_raw or evaluation.dex_quote.buy_amount_raw),
            int(sim.order_deadline or 0),
            int(sim.order_nonce or 0),
            sig_bytes,
            native_fee,
        )

        fn = contract.functions.executeArbitrageWithTokens(
            int(evaluation.dex_quote.sell_amount_raw),
            w3.to_checksum_address(USDC_ADDRESS),
            w3.to_checksum_address(USDP_ADDRESS),
            matcha_tuple,
            stable_tuple,
            sim.min_profit_raw,
        )

        # 2. Gas settings (EIP-1559)
        gas_limit = int((Decimal(sim.estimated_gas) * gas_multiplier).to_integral_value())
        latest = w3.eth.get_block("latest")
        base_fee = int(latest.get("baseFeePerGas", w3.eth.gas_price))
        priority_fee_wei = int(w3.to_wei(priority_fee_gwei, "gwei"))

        if max_fee_gwei is not None:
            max_fee_wei = int(w3.to_wei(max_fee_gwei, "gwei"))
        else:
            max_fee_wei = int(Decimal(base_fee) * Decimal("1.35")) + priority_fee_wei

        if max_fee_wei < priority_fee_wei:
            max_fee_wei = base_fee + priority_fee_wei

        nonce = w3.eth.get_transaction_count(operator_cs, "pending")

        tx_dict = {
            "chainId": 1,
            "from": operator_cs,
            "to": executor_cs,
            "value": native_val,
            "data": fn._encode_transaction_data(),
            "nonce": nonce,
            "gas": gas_limit,
            "maxFeePerGas": max_fee_wei,
            "maxPriorityFeePerGas": priority_fee_wei,
            "type": 2,
        }

        # 3. Check operator balance
        bal = w3.eth.get_balance(operator_cs)
        max_cost = gas_limit * max_fee_wei + native_val
        if bal < max_cost and not dry_run:
            return ExecutionResult(
                mode="live",
                success=False,
                simulation=sim,
                error=f"Operator balance {w3.from_wei(bal, 'ether')} ETH is insufficient for maximum tx cost {w3.from_wei(max_cost, 'ether')} ETH",
            )

        if dry_run:
            return ExecutionResult(
                mode="dry-run",
                success=True,
                simulation=sim,
                gas_used=sim.estimated_gas,
                effective_gas_price_gwei=Decimal(w3.from_wei(base_fee + priority_fee_wei, "gwei")),
            )

        # 4. Live Broadcast & Receipt Wait
        signed = account.sign_transaction(tx_dict)
        broadcast_method = "standard-rpc"
        enable_flashbots = os.getenv("ETH_ENABLE_FLASHBOTS", "true").strip().lower() not in (
            "false", "0", "no",
        )

        try:
            from src.engines.eth_flash_arb_pyusd_usdc import broadcast_flashbots_or_fallback
        except ImportError:
            try:
                from eth_flash_arb_pyusd_usdc import broadcast_flashbots_or_fallback
            except ImportError:
                broadcast_flashbots_or_fallback = None

        if broadcast_flashbots_or_fallback is not None and enable_flashbots:
            try:
                tx_hash_val, broadcast_method = broadcast_flashbots_or_fallback(
                    web3=w3,
                    signed_tx=signed,
                    private_key=private_key,
                    rpc_url=self.rpc_url,
                    receipt_timeout=receipt_timeout,
                )
                tx_hex = tx_hash_val.hex() if hasattr(tx_hash_val, "hex") else str(tx_hash_val)
                if not tx_hex.startswith("0x"):
                    tx_hex = f"0x{tx_hex}"
            except Exception as exc:
                return ExecutionResult(
                    mode="live",
                    success=False,
                    simulation=sim,
                    broadcast_method="flashbots-failed",
                    error=f"Flashbots submission rejected or not acknowledged: {exc}",
                )
        else:
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
            tx_hex = tx_hash.hex()
            if not tx_hex.startswith("0x"):
                tx_hex = f"0x{tx_hex}"
            broadcast_method = "standard-rpc"

        etherscan_url = f"https://etherscan.io/tx/{tx_hex}"

        try:
            receipt = w3.eth.wait_for_transaction_receipt(tx_hex, timeout=receipt_timeout)
            success = (receipt.status == 1)
            gas_used = int(receipt.gasUsed)
            effective_gas_price = Decimal(receipt.effectiveGasPrice) / Decimal(10**9)
            eth_usd = self.get_eth_usd()
            actual_gas_cost_usd = (Decimal(gas_used) * effective_gas_price / Decimal(10**9)) * eth_usd
            block_number = int(receipt.blockNumber)
            error_msg = None if success else "Transaction reverted on-chain during execution"
        except Exception as exc:
            success = False
            gas_used = 0
            effective_gas_price = Decimal("0")
            actual_gas_cost_usd = Decimal("0")
            block_number = None
            error_msg = f"Transaction not mined within {receipt_timeout}s: {exc}"

        return ExecutionResult(
            mode="live",
            success=success,
            simulation=sim,
            tx_hash=tx_hex,
            block_number=block_number,
            gas_used=gas_used,
            effective_gas_price_gwei=effective_gas_price,
            actual_gas_cost_usd=actual_gas_cost_usd,
            net_realized_profit_usd=(evaluation.gross_profit_usdc - actual_gas_cost_usd) if success else Decimal("0"),
            etherscan_url=etherscan_url,
            broadcast_method=broadcast_method,
            error=error_msg,
        )


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


def format_simulation_report(sim: SimulationResult, executor: str, operator: str | None) -> str:
    lines = [
        "=" * 86,
        "          LIVE RUN ENGINE: PRE-FLIGHT ON-CHAIN SIMULATION (eth_call)",
        "=" * 86,
        f"Executor:        {executor}",
        f"Operator:        {operator or 'Auto-resolved'}",
        f"Stable Nonce:    {sim.order_nonce if sim.order_nonce is not None else 'N/A'}",
        f"Deadline:        {sim.order_deadline if sim.order_deadline is not None else 'N/A'}",
        f"Native Value:    {sim.native_value_wei} wei ({Decimal(sim.native_value_wei) / Decimal(10**18):.6f} ETH)",
    ]
    if sim.success:
        lines.extend([
            "Simulation:      PASSED (0 Reverts)",
            f"Estimated Gas:   {sim.estimated_gas:,} gas units",
            "Pre-Flight:      READY FOR BROADCAST",
        ])
    else:
        lines.extend([
            "Simulation:      FAILED (Reverted)",
            f"Error Selector:  {sim.error_selector or 'Unknown'}",
            f"Diagnostics:     {sim.error_message}",
        ])
        if sim.remedy:
            lines.append(f"Remedy:          {sim.remedy}")
    lines.append("=" * 86)
    return "\n".join(lines)


def format_execution_report(result: ExecutionResult) -> str:
    lines = [
        "=" * 86,
        f"          LIVE RUN ENGINE: EXECUTION SUMMARY ({result.mode.upper()})",
        "=" * 86,
    ]
    if result.mode == "dry-run":
        if result.success:
            lines.extend([
                "Mode:            DRY-RUN (Simulated on-chain; no transaction broadcast)",
                "Pre-Flight:      PASSED",
                f"Estimated Gas:   {result.simulation.estimated_gas:,} units",
                "Action:          To broadcast live transaction, run with: --live --confirm-live LIVE",
            ])
        else:
            lines.extend([
                "Mode:            DRY-RUN",
                "Pre-Flight:      FAILED",
                f"Error:           {result.error}",
            ])
    else:
        if result.success:
            lines.extend([
                "Status:          TRANSACTION MINED & CONFIRMED (SUCCESS)",
                f"Tx Hash:         {result.tx_hash}",
                f"Block Number:    {result.block_number}",
                f"Gas Used:        {result.gas_used:,}",
                f"Gas Price:       {result.effective_gas_price_gwei:.3f} Gwei",
                f"Actual Gas Cost: ${result.actual_gas_cost_usd:.2f} USD",
                f"Net Profit:      ${result.net_realized_profit_usd:.2f} USD",
                f"Etherscan:       {result.etherscan_url}",
            ])
        else:
            lines.extend([
                "Status:          TRANSACTION FAILED",
                f"Tx Hash:         {result.tx_hash or 'None'}",
                f"Error:           {result.error}",
            ])
    lines.append("=" * 86)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Standalone USDP Arbitrage Test & Live Run Engine"
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
        choices=["matcha", "meta.matcha", "enso", "okx", "velora", "auto"],
        default="matcha",
        help="DEX aggregator for USDC -> USDP (default: matcha [meta.matcha.xyz] with Enso/OKX fallback)",
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
        "--live",
        action="store_true",
        help="Enable live run engine (runs pre-flight simulation and executes if confirmed)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run full on-chain pre-flight simulation and order creation without broadcasting",
    )
    parser.add_argument(
        "--send",
        action="store_true",
        help="Broadcast live transaction without requiring safety confirmation token",
    )
    parser.add_argument(
        "--confirm-live",
        type=str,
        default="",
        help="Confirmation token for live execution (e.g. --confirm-live LIVE)",
    )
    parser.add_argument(
        "--min-profit-usd",
        type=Decimal,
        default=Decimal("1.0"),
        help="Minimum net profit in USD required to trigger execution (default: 1.0)",
    )
    parser.add_argument(
        "--leg1-buffer-bps",
        type=int,
        default=0,
        help="Slippage buffer in basis points for Leg 2 Stable.com redemption (default: 0 = 0 bps, 100% of USDP output is redeemed)",
    )
    parser.add_argument(
        "--executor",
        type=str,
        default=None,
        help="Executor contract address override (defaults to ETH_ARB_STABLECOIN_EXECUTOR or DEFAULT_EXECUTOR)",
    )
    parser.add_argument(
        "--gas-multiplier",
        type=Decimal,
        default=Decimal("1.15"),
        help="Gas multiplier on estimated gas (default: 1.15)",
    )
    parser.add_argument(
        "--max-fee-gwei",
        type=Decimal,
        default=None,
        help="Cap on max fee per gas in Gwei",
    )
    parser.add_argument(
        "--priority-fee-gwei",
        type=Decimal,
        default=Decimal("0.05"),
        help="Priority fee in Gwei (default: 0.05)",
    )
    parser.add_argument(
        "--receipt-timeout",
        type=int,
        default=60,
        help="Receipt timeout in seconds (default: 60)",
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
        "--loan-token",
        type=str,
        default="USDC",
        help="Loan token (default: USDC)",
    )
    parser.add_argument(
        "--intermediate-token",
        type=str,
        default="USDP",
        help="Intermediate token (default: USDP)",
    )
    parser.add_argument(
        "--swap-order",
        choices=["dex-first", "stable-first"],
        default="dex-first",
        help="Swap venue order (default: dex-first)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output results as JSON",
    )

    args = parser.parse_args(argv)

    if args.swap_order != "dex-first":
        err_msg = "USDP stable-first arbitrage is structurally unexecutable (USDP trades at a discount on DEX)"
        if args.json:
            print(json.dumps({
                "error": err_msg,
                "grossProfit": "-999999",
                "predictedNetProfit": "-999999",
                "isProfitable": False,
            }))
        else:
            print(f"[ERROR] {err_msg}", file=sys.stderr)
        return 1

    if args.loan_token and args.loan_token.strip().upper() != "USDC":
        err_msg = f"{args.loan_token} flash loan is unsupported on Ethereum; use USDC loan"
        if args.json:
            print(json.dumps({
                "error": err_msg,
                "grossProfit": "0",
                "predictedNetProfit": "0",
                "isProfitable": False,
            }))
        else:
            print(f"[ERROR] {err_msg}", file=sys.stderr)
        return 1

    engine = USDPTestEngine(executor=args.executor)
    
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
            if (args.create_order or args.live or args.dry_run or args.send) and winner and winner.is_profitable:
                try:
                    buffer_factor = Decimal(1) - (Decimal(args.leg1_buffer_bps) / Decimal(10000))
                    safe_order_amount = (winner.dex_quote.buy_amount_human * buffer_factor).quantize(Decimal("0.000001"))
                    order_data = engine.create_stable_order(safe_order_amount)
                    order_nonce = order_data.get("nonce")
                    order_sig = str(order_data.get("maintainerSignature"))[:16] + "..."
                except Exception as exc:
                    order_nonce = f"Failed: {exc}"
                    order_sig = "N/A"
            else:
                order_nonce = "N/A"
                order_sig = "N/A"

            exec_result: ExecutionResult | None = None
            if (args.live or args.dry_run or args.send) and winner:
                if not winner.is_profitable or winner.net_profit_usd < args.min_profit_usd:
                    if not args.json:
                        print(
                            f"\n[INFO] Winning route net profit (${winner.net_profit_usd:.2f}) "
                            f"is below execution threshold (${args.min_profit_usd:.2f}). Skipping execution."
                        )
                else:
                    is_live_broadcast = False
                    if args.send:
                        is_live_broadcast = True
                    elif args.live:
                        if args.confirm_live.strip().upper() == "LIVE":
                            is_live_broadcast = True
                        else:
                            if not args.json:
                                print(
                                    "\n[NOTICE] --live specified without '--confirm-live LIVE'. "
                                    "Performing pre-flight on-chain simulation (DRY-RUN)."
                                )

                    exec_result = engine.execute_live_arbitrage(
                        evaluation=winner,
                        order_data=order_data,
                        min_profit_usdc=args.min_profit_usd,
                        gas_multiplier=args.gas_multiplier,
                        max_fee_gwei=args.max_fee_gwei,
                        priority_fee_gwei=args.priority_fee_gwei,
                        receipt_timeout=args.receipt_timeout,
                        leg1_buffer_bps=args.leg1_buffer_bps,
                        dry_run=not is_live_broadcast,
                    )

            if args.json:
                data = {
                    "timestamp": time.time(),
                    "grossProfit": str(winner.gross_profit_usdc) if winner else "0",
                    "predictedNetProfit": str(winner.net_profit_usd) if winner else "0",
                    "transactionHash": exec_result.tx_hash if exec_result else None,
                    "transactionStatus": (
                        "confirmed" if exec_result and exec_result.success
                        else "reverted" if exec_result and exec_result.error and "revert" in exec_result.error.lower()
                        else "submitted" if exec_result and exec_result.tx_hash
                        else "dry-run"
                    ),
                    "winner": {
                        "loanAmountUsdc": float(winner.loan_amount_usdc),
                        "dexOutputUsdp": float(winner.dex_quote.buy_amount_human),
                        "dexAggregator": winner.dex_quote.aggregator,
                        "stableOutputUsdc": float(winner.stable_quote.amount_out_human),
                        "grossProfitUsdc": float(winner.gross_profit_usdc),
                        "grossProfit": str(winner.gross_profit_usdc),
                        "gasEstimateUsd": float(winner.gas_estimate_usd),
                        "netProfitUsd": float(winner.net_profit_usd),
                        "predictedNetProfit": str(winner.net_profit_usd),
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
                    "execution": {
                        "mode": exec_result.mode,
                        "success": exec_result.success,
                        "txHash": exec_result.tx_hash,
                        "blockNumber": exec_result.block_number,
                        "gasUsed": exec_result.gas_used,
                        "etherscanUrl": exec_result.etherscan_url,
                        "error": exec_result.error,
                        "simulation": {
                            "success": exec_result.simulation.success,
                            "estimatedGas": exec_result.simulation.estimated_gas,
                            "errorSelector": exec_result.simulation.error_selector,
                            "errorMessage": exec_result.simulation.error_message,
                            "remedy": exec_result.simulation.remedy,
                        } if exec_result and exec_result.simulation else None,
                    } if exec_result else None,
                }
                print(json.dumps(data, indent=2))
            else:
                if args.amount is not None:
                    print(format_evaluation(winner, gas_price, eth_usd))
                else:
                    print(format_scaling_table(evaluations, winner, gas_price, eth_usd))

                if (args.create_order or args.live or args.dry_run or args.send) and winner and winner.is_profitable:
                    print(f"Verified Stable.com Order for Winner: Nonce={order_nonce}, Signature={order_sig}")

                if exec_result and exec_result.simulation:
                    print("\n" + format_simulation_report(exec_result.simulation, engine.executor, engine.operator))
                    print("\n" + format_execution_report(exec_result))

        except Exception as exc:
            print(f"[ERROR] Engine run failed: {exc}", file=sys.stderr)
            if not args.continuous:
                return 1

        if not args.continuous:
            break
        time.sleep(args.interval)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

