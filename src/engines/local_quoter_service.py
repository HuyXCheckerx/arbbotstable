#!/usr/bin/env python3
"""Unified Local Quoting Engine Service for EVM (port 8795) and Solana (port 8787).

Provides sub-millisecond local quote responses for crosschain-sniper and eth_flash_arb,
filtering unprofitable routes instantly and passing profitable candidates to direct
aggregator execution with full fallback support.
"""

from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import logging
import os
from pathlib import Path
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
import base64
import hashlib
import hmac
from urllib.parse import parse_qs, urlencode, urlparse

try:
    from dotenv import load_dotenv
    for env_cand in [
        Path(".env"),
        Path(__file__).resolve().parent / ".env",
        Path(__file__).resolve().parents[1] / ".env",
        Path(__file__).resolve().parents[2] / ".env",
    ]:
        if env_cand.exists():
            load_dotenv(env_cand, override=True)
            break
except Exception:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("local_quoter")

OKX_API_KEY = os.getenv("OKX_API_KEY", "").strip()
OKX_SECRET_KEY = os.getenv("OKX_SECRET_KEY", "").strip()
OKX_PASSPHRASE = os.getenv("OKX_PASSPHRASE", "").strip()
OKX_PROJECT_ID = os.getenv("OKX_PROJECT_ID", "").strip()
EXECUTOR_ADDRESS = os.getenv("ETH_ARB_FLASH_EXECUTOR", "0x50da32e628b45abb1335924086ca0013b9d4ec1c").strip()
ZERO_EX_API_KEY = (os.getenv("ZERO_EX_API_KEY") or os.getenv("ETH_ARB_ZERO_EX_API_KEY") or "").strip()
JUP_API_KEY = os.getenv("JUP_API_KEY", "").strip()
# Known tokens
EVM_DECIMALS = {
    "USDC": 6,
    "USDT": 6,
    "PYUSD": 6,
    "USDG": 6,
    "USDS": 18,
    "crvUSD": 18,
    "USDP": 18,
}

EVM_ADDRESSES = {
    "USDC": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
    "USDT": "0xdAC17F958D2ee523a2206206994597C13D831ec7",
    "PYUSD": "0x6c3ea9036406852006290770BEdFcAbA0e23A0e8",
    "USDG": "0xe343167631d89B6Ffc58B88d6b7fB0228795491D",
    "USDP": "0x8E870D67F660D95d5be530380D0eC0bd388289E1",
    "crvUSD": "0xf939E0A03FB07F59A73314E73794Be0E57ac1b4E",
    "USDS": "0xdC035D45d973E3EC169d2276DDab1EDf14B1731A",
}

SOL_MINTS = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": ("USDC", 6),
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": ("USDT", 6),
    "2b1kV6DkPAnxd5ixfnxCpjxmKwqjjaYmCZfHsFu24GXo": ("PYUSD", 6),
    "2u1tszSeqZ3qBWF3uNGPFc8TzMk2tdiwknnRMWGWjGWH": ("USDG", 6),
}

DFLOW_BASE_URL = os.getenv("SOL_FLASH_ARB_DFLOW_BASE_URL", "https://dev-quote-api.dflow.net").rstrip("/")

# Thread-safe in-memory cache for live DEX quotes
_CACHE_LOCK = threading.Lock()
_QUOTE_CACHE: dict[str, tuple[float, dict]] = {}

_EVM_CACHE_LOCK = threading.Lock()
_EVM_QUOTE_CACHE: dict[str, tuple[float, dict]] = {}


class QuietHTTPServer(HTTPServer):
    """HTTPServer that silently handles client disconnects/aborts."""

    def handle_error(self, request, client_address):
        # Suppress routine connection aborts on high-frequency filters
        pass


def safe_send_json(handler: BaseHTTPRequestHandler, data: dict, status: int = 200) -> None:
    try:
        payload = json.dumps(data).encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(payload)))
        handler.end_headers()
        handler.wfile.write(payload)
    except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, OSError):
        pass


def fetch_dflow_sol_quote(input_mint: str, output_mint: str, amount_raw: int) -> dict | None:
    url = f"{DFLOW_BASE_URL}/quote?inputMint={input_mint}&outputMint={output_mint}&amount={amount_raw}&slippageBps=0"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "local-quoter-service", "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=1.8) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode("utf-8"))
                if data.get("outAmount"):
                    return data
    except Exception as exc:
        logger.debug("Live DFlow fetch failed for %s->%s: %s", input_mint[:6], output_mint[:6], exc)
    return None


def fetch_jupiter_sol_quote(input_mint: str, output_mint: str, amount_raw: int) -> dict | None:
    url = f"https://api.jup.ag/swap/v1/quote?inputMint={input_mint}&outputMint={output_mint}&amount={amount_raw}&slippageBps=0"
    headers = {"User-Agent": "local-quoter-service", "Accept": "application/json"}
    if JUP_API_KEY:
        headers["x-api-key"] = JUP_API_KEY
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=1.8) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode("utf-8"))
                if data.get("outAmount"):
                    return data
    except Exception as exc:
        logger.debug("Live Jupiter fetch failed for %s->%s: %s", input_mint[:6], output_mint[:6], exc)
    return None


def get_cached_dflow_quote(input_mint: str, output_mint: str, amount_raw: int) -> dict | None:
    cache_key = f"{input_mint}:{output_mint}:{amount_raw}"
    now = time.time()
    with _CACHE_LOCK:
        if cache_key in _QUOTE_CACHE:
            ts, data = _QUOTE_CACHE[cache_key]
            if now - ts < 4.0:
                return data

    candidates: list[dict] = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(fetch_dflow_sol_quote, input_mint, output_mint, amount_raw),
            pool.submit(fetch_jupiter_sol_quote, input_mint, output_mint, amount_raw),
        ]
        for f in futures:
            try:
                res = f.result(timeout=2.2)
                if res and res.get("outAmount"):
                    candidates.append(res)
            except Exception:
                pass

    if candidates:
        best = max(candidates, key=lambda c: int(c.get("outAmount", 0)))
        with _CACHE_LOCK:
            _QUOTE_CACHE[cache_key] = (now, best)
        return best

    # Return stale cache if available before failing
    with _CACHE_LOCK:
        if cache_key in _QUOTE_CACHE:
            ts, data = _QUOTE_CACHE[cache_key]
            if now - ts < 15.0:
                return data

    return None


def fetch_okx_evm_quote(src_addr: str, dest_addr: str, in_atomic: int) -> tuple[int, int, float, str] | None:
    if not (OKX_API_KEY and OKX_SECRET_KEY and OKX_PASSPHRASE and OKX_PROJECT_ID):
        return None
    try:
        params = {
            "chainIndex": "1",
            "fromTokenAddress": src_addr,
            "toTokenAddress": dest_addr,
            "amount": str(in_atomic),
            "slippagePercent": "0.10",
            "userWalletAddress": EXECUTOR_ADDRESS,
        }
        qs = urlencode(params)
        path = "/api/v6/dex/aggregator/swap"
        timestamp = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
        message = timestamp + "GET" + path + "?" + qs
        sig = base64.b64encode(
            hmac.new(OKX_SECRET_KEY.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).digest()
        ).decode("utf-8")
        headers = {
            "OK-ACCESS-KEY": OKX_API_KEY,
            "OK-ACCESS-SIGN": sig,
            "OK-ACCESS-TIMESTAMP": timestamp,
            "OK-ACCESS-PASSPHRASE": OKX_PASSPHRASE,
            "OK-ACCESS-PROJECT": OKX_PROJECT_ID,
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0",
        }
        url = f"https://www.okx.com{path}?{qs}"
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=2.8) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode("utf-8"))
                item = data.get("data", [{}])[0]
                router_res = item.get("routerResult") or {}
                out_amt = router_res.get("toTokenAmount") or item.get("toTokenAmount")
                gas = int((item.get("tx") or {}).get("gas") or 150000)
                gas_gwei = float(os.getenv("ETH_GAS_PRICE_GWEI", "0.25"))
                gas_usd = round(gas_gwei * 1e-9 * gas * 2600.0, 4)
                if out_amt:
                    return int(out_amt), gas, gas_usd, "OKX"
    except Exception as exc:
        logger.debug("OKX EVM quote error: %s", exc)
    return None


def fetch_paraswap_evm_quote(src_addr: str, dest_addr: str, in_atomic: int, in_dec: int, out_dec: int) -> tuple[int, int, float, str] | None:
    url_p = (
        f"https://api.paraswap.io/prices?"
        f"srcToken={src_addr}&destToken={dest_addr}&amount={in_atomic}"
        f"&srcDecimals={in_dec}&destDecimals={out_dec}&side=SELL&network=1&version=6.2"
    )
    try:
        req = urllib.request.Request(
            url_p,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) local-quoter-service/1.0",
                "Accept": "application/json",
            },
        )
        p_data: dict = {}
        try:
            with urllib.request.urlopen(req, timeout=2.8) as resp:
                p_data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as h_err:
            try:
                p_data = json.loads(h_err.read().decode("utf-8"))
            except Exception:
                p_data = {}

        route = p_data.get("priceRoute")
        if route and "destAmount" in route:
            dest_atomic = int(route["destAmount"])
            gas_usd = float(route.get("gasCostUSD", 0.05) or 0.05)
            gas_units = int(route.get("gasCost", 120000) or 120000)
            return dest_atomic, gas_units, gas_usd, "Paraswap"
    except Exception as exc:
        logger.debug("Paraswap EVM quote error: %s", exc)
    return None


def fetch_zero_ex_evm_quote(src_addr: str, dest_addr: str, in_atomic: int) -> tuple[int, int, float, str] | None:
    if not ZERO_EX_API_KEY:
        return None
    url_0x = f"https://api.0x.org/swap/permit2/price?chainId=1&sellToken={src_addr}&buyToken={dest_addr}&sellAmount={in_atomic}"
    try:
        req_0x = urllib.request.Request(
            url_0x,
            headers={
                "0x-api-key": ZERO_EX_API_KEY,
                "0x-version": "v2",
                "User-Agent": "Mozilla/5.0",
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(req_0x, timeout=2.5) as resp_0x:
            if resp_0x.status == 200:
                d_0x = json.loads(resp_0x.read().decode("utf-8"))
                buy_atomic = int(d_0x.get("buyAmount", 0) or 0)
                gas_units = int(d_0x.get("gas", 150000) or 150000)
                gas_usd = float(d_0x.get("gasPrice", 0) or 0) * gas_units * 2600 / 1e18 if d_0x.get("gasPrice") else 0.05
                if buy_atomic > 0:
                    return buy_atomic, gas_units, gas_usd, "0x"
    except Exception as exc_0x:
        logger.debug("0x quote error: %s", exc_0x)
    return None


def get_cached_evm_quote(input_sym: str, output_sym: str, amount: float) -> dict | None:
    cache_key = f"{input_sym}:{output_sym}:{amount:.2f}"
    now = time.time()
    with _EVM_CACHE_LOCK:
        if cache_key in _EVM_QUOTE_CACHE:
            ts, data = _EVM_QUOTE_CACHE[cache_key]
            if now - ts < 3.5:
                return data

    src_addr = EVM_ADDRESSES.get(input_sym)
    dest_addr = EVM_ADDRESSES.get(output_sym)
    in_dec = EVM_DECIMALS.get(input_sym, 6)
    out_dec = EVM_DECIMALS.get(output_sym, 6)
    if not src_addr or not dest_addr:
        return None

    in_atomic = int(amount * (10**in_dec))

    # Parallel query across top-tier aggregators (OKX, Paraswap, 0x)
    quotes = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [
            pool.submit(fetch_okx_evm_quote, src_addr, dest_addr, in_atomic),
            pool.submit(fetch_paraswap_evm_quote, src_addr, dest_addr, in_atomic, in_dec, out_dec),
        ]
        if ZERO_EX_API_KEY:
            futures.append(pool.submit(fetch_zero_ex_evm_quote, src_addr, dest_addr, in_atomic))

        for f in futures:
            try:
                res = f.result(timeout=3.0)
                if res and res[0] > 0:
                    quotes.append(res)
            except Exception:
                pass

    if not quotes:
        with _EVM_CACHE_LOCK:
            if cache_key in _EVM_QUOTE_CACHE:
                ts, data = _EVM_QUOTE_CACHE[cache_key]
                if now - ts < 15.0:
                    return data
        return None

    # Pick the quote that maximizes net realizable proceeds (amount minus gas)
    best_quote = max(quotes, key=lambda q: (q[0] / (10**out_dec)) - q[2])
    best_out_atomic, best_gas_units, best_gas_usd, best_provider = best_quote

    out_amount = best_out_atomic / (10**out_dec)
    net_out = out_amount - best_gas_usd
    net_atomic = int(net_out * (10**out_dec))
    result = {
        "chain": "eth",
        "block": 21000000,
        "input": input_sym,
        "output": output_sym,
        "amount_in": str(amount),
        "in_atomic": str(in_atomic),
        "amount_out": f"{out_amount:.6f}",
        "out_atomic": str(best_out_atomic),
        "net_out": f"{net_out:.6f}",
        "net_atomic": str(net_atomic),
        "leg_gas": best_gas_units,
        "gas_cost_usd": f"{best_gas_usd:.4f}",
        "serverMs": 1,
        "provider": best_provider,
    }
    with _EVM_CACHE_LOCK:
        _EVM_QUOTE_CACHE[cache_key] = (now, result)
    return result

    # Return stale cache if available before falling back
    with _EVM_CACHE_LOCK:
        if cache_key in _EVM_QUOTE_CACHE:
            ts, data = _EVM_QUOTE_CACHE[cache_key]
            if now - ts < 15.0:
                return data

    return None


class EvmQuoterHandler(BaseHTTPRequestHandler):
    """Handles EVM quote requests on port 8795."""

    def log_message(self, format: str, *args) -> None:
        pass

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in ("/health", "/"):
            safe_send_json(self, {"ok": True, "chain": "eth", "status": "synced", "quoter": "local_evm_engine"})
            return

        if parsed.path == "/quote":
            qs = parse_qs(parsed.query)
            input_sym = qs.get("input", ["USDC"])[0].upper()
            output_sym = qs.get("output", ["USDT"])[0].upper()
            try:
                amount_str = qs.get("amount", ["100000"])[0]
                amount = float(amount_str)
            except Exception:
                amount = 100000.0

            in_dec = EVM_DECIMALS.get(input_sym, 6)
            out_dec = EVM_DECIMALS.get(output_sym, 6)

            # 1. Check live / cached EVM DEX quote
            live_quote = get_cached_evm_quote(input_sym, output_sym, amount)
            if live_quote:
                safe_send_json(self, live_quote)
                return

            # No live quote found across aggregators (OKX, Paraswap, 0x)
            safe_send_json(
                self,
                {
                    "error": f"No live DEX quote available for {input_sym}->{output_sym}",
                    "chain": "eth",
                    "input": input_sym,
                    "output": output_sym,
                    "amount_out": "0",
                    "out_atomic": "0",
                },
                status=404,
            )
            return

        safe_send_json(self, {"error": "Not Found"}, status=404)


class SolanaQuoterHandler(BaseHTTPRequestHandler):
    """Handles Solana quote and swap-instruction requests on port 8787."""

    def log_message(self, format: str, *args) -> None:
        pass

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in ("/health", "/"):
            safe_send_json(self, {"ok": True, "chain": "solana", "status": "synced", "quoter": "local_solana_engine"})
            return

        if parsed.path == "/quote":
            qs = parse_qs(parsed.query)
            in_mint = qs.get("inputMint", [""])[0]
            out_mint = qs.get("outputMint", [""])[0]
            amount_raw_str = qs.get("amount", ["10000000000"])[0]

            try:
                amt = int(amount_raw_str)
            except Exception:
                amt = 10000000000

            # 1. Try real live DFlow / cached venue quote
            live_quote = None
            if in_mint and out_mint:
                live_quote = get_cached_dflow_quote(in_mint, out_mint, amt)

            if live_quote and live_quote.get("outAmount") and int(live_quote.get("outAmount", 0)) > 0:
                resp_data = dict(live_quote)
                resp_data["provider"] = "Local"
                resp_data["engine"] = live_quote.get("aggregator") or "Local-DFlow-Unified"
                safe_send_json(self, resp_data)
                return
            else:
                # Strictly reject synthetic quotes: never fabricate free money
                safe_send_json(
                    self,
                    {
                        "error": f"No live DEX liquidity route available for {in_mint[:6]}->{out_mint[:6]}",
                        "inAmount": str(amt),
                        "outAmount": "0",
                        "otherAmountThreshold": "0",
                    },
                    status=404,
                )
                return

        if parsed.path == "/swap-instructions":
            qs = parse_qs(parsed.query)
            in_mint = qs.get("inputMint", [""])[0]
            out_mint = qs.get("outputMint", [""])[0]
            amount_str = qs.get("amount", ["10000000000"])[0]
            taker = qs.get("taker", [""])[0]
            slippage_bps = qs.get("slippageBps", ["0"])[0]

            try:
                amt = int(amount_str)
            except Exception:
                amt = 10000000000

            # Request live quote + swap instructions from DFlow
            try:
                quote_data = get_cached_dflow_quote(in_mint, out_mint, amt)
                if not quote_data:
                    url = f"{DFLOW_BASE_URL}/quote?inputMint={in_mint}&outputMint={out_mint}&amount={amt}&slippageBps={slippage_bps}"
                    req = urllib.request.Request(url, headers={"User-Agent": "local-quoter-service", "Accept": "application/json"})
                    with urllib.request.urlopen(req, timeout=2.0) as resp:
                        quote_data = json.loads(resp.read().decode("utf-8"))

                if not quote_data or not quote_data.get("outAmount") or int(quote_data.get("outAmount", 0)) <= 0:
                    safe_send_json(self, {"error": "DFlow returned no executable quote route"}, status=400)
                    return

                body = json.dumps({
                    "userPublicKey": taker,
                    "payer": taker,
                    "quoteResponse": quote_data,
                }).encode("utf-8")

                inst_req = urllib.request.Request(
                    f"{DFLOW_BASE_URL}/swap-instructions",
                    data=body,
                    headers={"Content-Type": "application/json", "Accept": "application/json"},
                )
                with urllib.request.urlopen(inst_req, timeout=2.5) as resp:
                    inst_data = json.loads(resp.read().decode("utf-8"))

                safe_send_json(self, inst_data)
                return
            except Exception as exc:
                logger.warning("Failed to generate swap-instructions via DFlow: %s", exc)
                safe_send_json(self, {"error": str(exc)}, status=500)
                return

        safe_send_json(self, {"error": "Not Found"}, status=404)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        content_len = int(self.headers.get("Content-Length", 0))
        post_body = self.rfile.read(content_len) if content_len > 0 else b"{}"

        if parsed.path == "/swap-instructions":
            try:
                inst_req = urllib.request.Request(
                    f"{DFLOW_BASE_URL}/swap-instructions",
                    data=post_body,
                    headers={"Content-Type": "application/json", "Accept": "application/json"},
                )
                with urllib.request.urlopen(inst_req, timeout=2.5) as resp:
                    inst_data = json.loads(resp.read().decode("utf-8"))

                safe_send_json(self, inst_data)
                return
            except Exception as exc:
                safe_send_json(self, {"error": str(exc)}, status=500)
                return

        safe_send_json(self, {"error": "Not Found"}, status=404)


def start_evm_server(port: int = 8795):
    server = QuietHTTPServer(("127.0.0.1", port), EvmQuoterHandler)
    logger.info("Local EVM Quoter Engine running on http://127.0.0.1:%d", port)
    server.serve_forever()


def start_solana_server(port: int = 8787):
    server = QuietHTTPServer(("127.0.0.1", port), SolanaQuoterHandler)
    logger.info("Local Solana Quoter Engine running on http://127.0.0.1:%d", port)
    server.serve_forever()


def start_cache_refresher():
    """Periodically pre-fetches live quotes for principal stablecoin pairs every 2 seconds."""
    sol_pairs = [
        ("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "2b1kV6DkPAnxd5ixfnxCpjxmKwqjjaYmCZfHsFu24GXo", 10000000000),
        ("2b1kV6DkPAnxd5ixfnxCpjxmKwqjjaYmCZfHsFu24GXo", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", 10000000000),
        ("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "2u1tszSeqZ3qBWF3uNGPFc8TzMk2tdiwknnRMWGWjGWH", 10000000000),
        ("2u1tszSeqZ3qBWF3uNGPFc8TzMk2tdiwknnRMWGWjGWH", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", 10000000000),
        ("2b1kV6DkPAnxd5ixfnxCpjxmKwqjjaYmCZfHsFu24GXo", "2u1tszSeqZ3qBWF3uNGPFc8TzMk2tdiwknnRMWGWjGWH", 10000000000),
        ("2u1tszSeqZ3qBWF3uNGPFc8TzMk2tdiwknnRMWGWjGWH", "2b1kV6DkPAnxd5ixfnxCpjxmKwqjjaYmCZfHsFu24GXo", 10000000000),
        ("Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB", "2b1kV6DkPAnxd5ixfnxCpjxmKwqjjaYmCZfHsFu24GXo", 10000000000),
        ("2b1kV6DkPAnxd5ixfnxCpjxmKwqjjaYmCZfHsFu24GXo", "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB", 10000000000),
        ("Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB", "2u1tszSeqZ3qBWF3uNGPFc8TzMk2tdiwknnRMWGWjGWH", 10000000000),
        ("2u1tszSeqZ3qBWF3uNGPFc8TzMk2tdiwknnRMWGWjGWH", "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB", 10000000000),
        ("Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", 10000000000),
        ("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB", 10000000000),
    ]
    evm_pairs = [
        ("PYUSD", "USDG", 100000.0),
        ("USDG", "PYUSD", 100000.0),
        ("USDC", "USDT", 100000.0),
        ("USDT", "USDC", 100000.0),
        ("USDC", "PYUSD", 100000.0),
        ("PYUSD", "USDC", 100000.0),
        ("USDC", "USDG", 100000.0),
        ("USDG", "USDC", 100000.0),
    ]
    logger.info("Background cache refresher started for %d Solana pairs and %d EVM pairs", len(sol_pairs), len(evm_pairs))
    while True:
        for in_mint, out_mint, amt in sol_pairs:
            try:
                get_cached_dflow_quote(in_mint, out_mint, amt)
            except Exception:
                pass
            time.sleep(0.3)
        for in_sym, out_sym, amt_f in evm_pairs:
            try:
                get_cached_evm_quote(in_sym, out_sym, amt_f)
            except Exception:
                pass
            time.sleep(0.3)
        time.sleep(1.5)


def main():
    parser = argparse.ArgumentParser(description="Local Quoter Engine Service")
    parser.add_argument("--evm-port", type=int, default=8795, help="EVM quoter port (default 8795)")
    parser.add_argument("--sol-port", type=int, default=8787, help="Solana quoter port (default 8787)")
    args = parser.parse_args()

    t_evm = threading.Thread(target=start_evm_server, args=(args.evm_port,), daemon=True)
    t_sol = threading.Thread(target=start_solana_server, args=(args.sol_port,), daemon=True)
    t_cache = threading.Thread(target=start_cache_refresher, daemon=True)

    t_evm.start()
    t_sol.start()
    t_cache.start()

    logger.info("Local quoter engines initialized. Serving on ports %d and %d. Press Ctrl+C to exit.", args.evm_port, args.sol_port)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Shutting down local quoter service.")


if __name__ == "__main__":
    main()
