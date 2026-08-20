from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


SRC_DIR = Path(__file__).resolve().parent.parent
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))
for sub in ("core", "recovery", "engines", "web", "deployers"):
    subpath = str(SRC_DIR / sub)
    if subpath not in sys.path:
        sys.path.insert(0, subpath)

from state_store import DEFAULT_DB_PATH, read_dashboard_state  # noqa: E402


WEB_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = WEB_DIR.parents[1]
STATIC_DIR = WEB_DIR / "static"
TEMPLATE_PATH = WEB_DIR / "templates" / "dashboard.html"
FAVICON_PATH = WEB_DIR / "favicon.svg"

PORT = int(os.environ.get("PORT", "25284"))
HOST = os.environ.get("WEB_HOST", "0.0.0.0")
DB_PATH = os.environ.get("BOT_STATE_DB", str(DEFAULT_DB_PATH))
REQUEST_TIMEOUT_SECONDS = max(
    1.0,
    float(os.environ.get("WEB_REQUEST_TIMEOUT_SECONDS", "10")),
)
DB_TIMEOUT_SECONDS = max(
    0.05,
    float(os.environ.get("WEB_DB_TIMEOUT_SECONDS", "1")),
)
QUOTE_TIMEOUT_SECONDS = max(
    10.0,
    float(os.environ.get("WEB_QUOTE_TIMEOUT_SECONDS", "60")),
)
LIVE_TIMEOUT_SECONDS = max(
    QUOTE_TIMEOUT_SECONDS,
    float(os.environ.get("WEB_LIVE_TIMEOUT_SECONDS", "240")),
)
MAX_REQUEST_BYTES = 64 * 1024
LIVE_CONFIRMATION = "EXECUTE LIVE ARB"

SUPPORTED_PAIRS = {
    "ethereum": {"PYUSD/USDC", "USDT/USDC", "USDG/PYUSD", "PYUSD/USDG"},
    "solana": {"PYUSD/USDC", "USDT/USDC", "USDG/PYUSD", "PYUSD/USDG"},
    "polygon": {"PYUSD/USDC"},
    "bsc": {"USDT/USDC"},
}

STATIC_ASSETS = {
    "/static/dashboard.css": (STATIC_DIR / "dashboard.css", "text/css; charset=utf-8"),
    "/static/dashboard.js": (
        STATIC_DIR / "dashboard.js",
        "text/javascript; charset=utf-8",
    ),
}

# Kept as a module attribute for lightweight tests and embedders.
HTML_TEMPLATE = TEMPLATE_PATH.read_text(encoding="utf-8")

EXECUTION_LOGS: list[dict[str, str]] = []
LOG_LOCK = threading.Lock()
EXECUTION_LOCK = threading.Lock()


def add_log(entry: str, log_type: str = "info") -> None:
    timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
    with LOG_LOCK:
        EXECUTION_LOGS.append(
            {
                "timestamp": timestamp,
                "text": entry,
                "type": log_type,
            }
        )
        del EXECUTION_LOGS[:-300]


add_log("Arbitrage console online and listening for commands.", "system")


def _route_symbols(pair: str) -> tuple[str, str]:
    intermediate, loan = pair.split("/", 1)
    return loan, intermediate


def _route_flow(chain: str, pair: str) -> str:
    loan, intermediate = _route_symbols(pair)
    venue = "Jupiter" if chain == "solana" else "Matcha"
    return f"{loan} → {intermediate} ({venue}) → {loan} (Stable.com)"


def _json_plan(stdout: str) -> dict[str, Any] | None:
    try:
        value = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def parse_output_summary(
    output: str,
    plan: dict[str, Any] | None = None,
    *,
    chain: str = "ethereum",
    pair: str = "PYUSD/USDC",
) -> dict[str, str]:
    loan_symbol, intermediate_symbol = _route_symbols(pair)
    summary: dict[str, str] = {"flow": _route_flow(chain, pair)}

    if plan:
        loan_token = plan.get("loanToken")
        intermediate_token = plan.get("intermediateToken")
        if isinstance(loan_token, dict):
            loan_symbol = str(loan_token.get("symbol") or loan_symbol)
        if isinstance(intermediate_token, dict):
            intermediate_symbol = str(
                intermediate_token.get("symbol") or intermediate_symbol
            )

        loan_amount = plan.get("loanAmount")
        if loan_amount is not None:
            summary["loan"] = f"{loan_amount} {loan_symbol}"

        matcha = plan.get("matcha")
        if isinstance(matcha, dict):
            sold = matcha.get("sellLoanToken", matcha.get("sellUSDC"))
            bought = matcha.get("buyIntermediate", matcha.get("buyUSDT"))
            if sold is not None and bought is not None:
                summary["leg1"] = (
                    f"{sold} {loan_symbol} → {bought} {intermediate_symbol}"
                )

        stable = plan.get("stable")
        if isinstance(stable, dict):
            sold = stable.get("sellIntermediate", stable.get("sellUSDT"))
            bought = stable.get("buyLoanToken", stable.get("buyUSDC"))
            if sold is not None and bought is not None:
                summary["leg2"] = (
                    f"{sold} {intermediate_symbol} → {bought} {loan_symbol}"
                )

        gross = plan.get("grossProfit", plan.get("grossProfitAfterFlashFee"))
        if gross is not None:
            summary["gross"] = f"{gross} {loan_symbol}"
        net = plan.get("predictedNetProfit")
        if net is not None:
            summary["net"] = f"{net} {loan_symbol}"
        execution_cost = plan.get(
            "maximumExecutionCostUsd",
            plan.get("maximumExecutionCostUSDC"),
        )
        if execution_cost is not None:
            summary["cost"] = f"{execution_cost} {loan_symbol}"
        transaction_hash = plan.get("transactionHash")
        if transaction_hash:
            summary["transaction"] = str(transaction_hash)

    for line in output.splitlines():
        clean = line.strip()
        if "Leg 1" in clean:
            summary.setdefault("leg1", clean.split(":", 1)[-1].strip())
        elif "Leg 2" in clean:
            summary.setdefault("leg2", clean.split(":", 1)[-1].strip())
        elif "Guaranteed net result:" in clean:
            summary["net"] = clean.split(":", 1)[-1].strip()
        elif "Gross Profit:" in clean or "Guaranteed gross result:" in clean:
            summary.setdefault("gross", clean.split(":", 1)[-1].strip())
        elif "Loan Amount:" in clean or "Flash-loan principal:" in clean:
            summary.setdefault("loan", clean.split(":", 1)[-1].strip())

    return summary


def _error_message(output: str, returncode: int) -> str:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    for line in reversed(lines):
        if line.upper().startswith("ERROR:"):
            return line.split(":", 1)[1].strip()
    if lines:
        return lines[-1][:500]
    return f"Arbitrage process exited with status {returncode}"


def _log_process_output(output: str) -> None:
    for line in output.splitlines():
        clean = line.strip()
        if not clean:
            continue
        lowered = clean.lower()
        log_type = "info"
        if "error" in lowered or "failed" in lowered:
            log_type = "error"
        elif "quote" in lowered or "leg 1" in lowered or "leg 2" in lowered:
            log_type = "quote"
        elif "profit" in lowered or "gross" in lowered or "net result" in lowered:
            log_type = "success"
        add_log(clean, log_type)


def run_arb_command(
    chain: str,
    pair: str,
    mode: str,
    amount: str,
    slippage_bps: str,
) -> tuple[bool, str, dict[str, str]]:
    env = os.environ.copy()
    intermediate, loan = pair.split("/", 1)

    if chain == "solana":
        env["SOL_FLASH_ARB_AMOUNT_USDC"] = amount
        env[f"SOL_FLASH_ARB_AMOUNT_{loan}"] = amount
        env["SOL_FLASH_ARB_LOAN_TOKEN"] = loan
        env["SOL_FLASH_ARB_SLIPPAGE_BPS"] = slippage_bps
        env["SOL_FLASH_ARB_INTERMEDIATE_TOKEN"] = intermediate
        if loan != "USDC":
            env["SOL_FLASH_ARB_ONLY_DIRECT_ROUTES"] = "false"
        executable = "npx.cmd" if sys.platform == "win32" else "npx"
        cmd = [
            executable,
            "tsx",
            str(PROJECT_ROOT / "src" / "engines" / "solana_flash_arb.ts"),
        ]
        if mode == "quote":
            cmd.append("--quote-only")
        else:
            cmd.extend(
                ["--send", "--confirm-mainnet", "EXECUTE_SOLANA_FLASH_ARB"]
            )
    elif chain == "ethereum":
        generic_route = intermediate != "USDT"
        script_name = "eth_flash_arb_pyusd_usdc.py" if generic_route else "eth_flash_arb.py"
        cmd = [
            sys.executable,
            str(PROJECT_ROOT / "src" / "engines" / script_name),
            "--amount",
            amount,
            "--loan-token",
            loan,
            "--slippage-bps",
            slippage_bps,
        ]
        if generic_route:
            cmd.extend(["--intermediate-token", intermediate])
        if mode == "live":
            cmd.extend(["--send", "--confirm-mainnet", "EXECUTE_ATOMIC_ARB"])
    elif chain in ("polygon", "bsc"):
        cmd = [
            sys.executable,
            str(PROJECT_ROOT / "src" / "engines" / "multichain_flash_arb.py"),
            "--chain",
            chain,
            "--pair",
            pair,
            "--amount",
            amount,
            "--slippage-bps",
            slippage_bps,
        ]
        if mode == "live":
            cmd.extend(
                ["--send", "--confirm-mainnet", f"EXECUTE_{chain.upper()}_ARB"]
            )
    else:
        return False, f"Unsupported chain: {chain}", {}

    add_log(
        f"Starting {mode} · {chain.title()} · {pair} · {amount} · {slippage_bps} bps",
        "system",
    )
    timeout_seconds = LIVE_TIMEOUT_SECONDS if mode == "live" else QUOTE_TIMEOUT_SECONDS
    try:
        result = subprocess.run(
            cmd,
            cwd=str(PROJECT_ROOT),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        message = f"Command timed out after {timeout_seconds:g} seconds"
        add_log(message, "error")
        return False, message, {}
    except OSError as exc:
        message = f"Could not start arbitrage process: {exc}"
        add_log(message, "error")
        return False, message, {}

    stdout = result.stdout or ""
    stderr = result.stderr or ""
    output = "\n".join(part for part in (stdout, stderr) if part)
    # EVM stdout is a large JSON plan; stderr contains the human-readable trace.
    _log_process_output(stdout if chain == "solana" else stderr)
    plan = _json_plan(stdout)
    parsed = parse_output_summary(output, plan, chain=chain, pair=pair)
    if result.returncode != 0:
        return False, _error_message(output, result.returncode), parsed

    completion = "Live transaction submitted." if mode == "live" else "Quote ready."
    add_log(completion, "success")
    return True, output, parsed


def _validated_request(data: Any) -> tuple[str, str, str, str, str]:
    if not isinstance(data, dict):
        raise ValueError("request body must be an object")
    chain = str(data.get("chain", "")).lower()
    pair = str(data.get("pair", "")).upper()
    mode = str(data.get("mode", "")).lower()
    amount = str(data.get("amount", "")).strip()
    slippage_text = str(data.get("slippageBps", "")).strip()

    if chain not in SUPPORTED_PAIRS:
        raise ValueError("unsupported chain")
    if pair not in SUPPORTED_PAIRS[chain]:
        raise ValueError(f"{pair or 'selected pair'} is not supported on {chain.title()}")
    if mode not in ("quote", "live"):
        raise ValueError("mode must be quote or live")
    try:
        parsed_amount = Decimal(amount)
    except (InvalidOperation, ValueError):
        raise ValueError("amount must be a decimal number") from None
    if not parsed_amount.is_finite() or parsed_amount <= 0:
        raise ValueError("amount must be greater than zero")
    if len(amount) > 40:
        raise ValueError("amount is too long")
    try:
        slippage = int(slippage_text)
    except ValueError:
        raise ValueError("slippage must be a whole number") from None
    if not 0 <= slippage <= 100:
        raise ValueError("slippage must be between 0 and 100 bps")
    if mode == "live" and data.get("confirmation") != LIVE_CONFIRMATION:
        raise PermissionError(f'type "{LIVE_CONFIRMATION}" to authorize a live run')
    return chain, pair, mode, amount, str(slippage)


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "ArbitrageDashboard/2"

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(REQUEST_TIMEOUT_SECONDS)

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def _send(
        self,
        status: int,
        content_type: str,
        payload: bytes,
        *,
        cache_control: str = "no-store",
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", cache_control)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; connect-src 'self'; img-src 'self'; "
            "style-src 'self'; script-src 'self'; base-uri 'none'; "
            "form-action 'self'; frame-ancestors 'none'",
        )
        self.end_headers()
        self.wfile.write(payload)

    def _json(self, status: int, value: Any) -> None:
        payload = json.dumps(value, separators=(",", ":")).encode("utf-8")
        self._send(status, "application/json; charset=utf-8", payload)

    def _read_json_body(self) -> Any:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ValueError("invalid content length") from None
        if length <= 0:
            raise ValueError("request body is required")
        if length > MAX_REQUEST_BYTES:
            raise OverflowError("request body is too large")
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("invalid JSON") from None

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path in ("/favicon.svg", "/favicon.ico"):
            try:
                payload = FAVICON_PATH.read_bytes()
            except OSError:
                self._json(404, {"error": "not found"})
                return
            self._send(200, "image/svg+xml", payload, cache_control="public, max-age=3600")
            return

        asset = STATIC_ASSETS.get(path)
        if asset:
            asset_path, content_type = asset
            try:
                payload = asset_path.read_bytes()
            except OSError:
                self._json(404, {"error": "not found"})
                return
            self._send(200, content_type, payload, cache_control="public, max-age=300")
            return

        if path == "/api/logs":
            with LOG_LOCK:
                logs = list(EXECUTION_LOGS)
            self._json(200, {"logs": logs, "running": EXECUTION_LOCK.locked()})
            return

        if path == "/api/state":
            try:
                state = read_dashboard_state(DB_PATH, timeout_seconds=DB_TIMEOUT_SECONDS)
            except Exception:
                self._json(
                    503,
                    {"ok": False, "error": "dashboard state temporarily unavailable"},
                )
                return
            self._json(200, state)
            return

        if path == "/healthz":
            try:
                state = read_dashboard_state(DB_PATH, timeout_seconds=DB_TIMEOUT_SECONDS)
                bot_status = state.get("bot", {}).get("status", "offline")
                healthy = bot_status != "offline"
            except Exception:
                bot_status = "unavailable"
                healthy = False
            self._json(200 if healthy else 503, {"ok": healthy, "status": bot_status})
            return

        if path in ("/", "/index.html"):
            self._send(
                200,
                "text/html; charset=utf-8",
                HTML_TEMPLATE.encode("utf-8"),
            )
            return

        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        if path != "/api/run":
            self._json(404, {"error": "not found"})
            return

        try:
            data = self._read_json_body()
            chain, pair, mode, amount, slippage_bps = _validated_request(data)
        except OverflowError as exc:
            self._json(413, {"ok": False, "error": str(exc)})
            return
        except PermissionError as exc:
            self._json(403, {"ok": False, "error": str(exc)})
            return
        except ValueError as exc:
            self._json(400, {"ok": False, "error": str(exc)})
            return

        if not EXECUTION_LOCK.acquire(blocking=False):
            self._json(409, {"ok": False, "error": "another execution is already running"})
            return
        try:
            success, output, parsed = run_arb_command(
                chain,
                pair,
                mode,
                amount,
                slippage_bps,
            )
        finally:
            EXECUTION_LOCK.release()

        response = {
            "ok": success,
            "parsed": parsed,
            "output": output if success else "",
            "error": None if success else output,
        }
        self._json(200 if success else 422, response)


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def run(host: str = HOST, port: int = PORT) -> None:
    server_address = (host, port)
    httpd = DashboardServer(server_address, DashboardHandler)
    display_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    print(f"[*] Dashboard running on http://{display_host}:{port}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    run()
