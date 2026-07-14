import copy
import json
import math
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_STATE_PATH = BASE_DIR / "bot_state.json"
DEFAULT_PNL_PATH = BASE_DIR / "pnl.txt"
STABLE_ASSETS = ("USDC", "USDG", "PYUSD", "USDT")
PNL_ASSET_VALUES_USD = {"USDC": 1.0, "USDG": 1.0, "PYUSD": 1.0, "USDT": 0.999}


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _atomic_write(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with open(temp_path, "w", encoding="utf-8") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_path, path)


def _empty_asset():
    return {"raw": 0, "amount": 0.0, "usd_value": 0.0}


def default_state():
    return {
        "schema_version": 1,
        "bot": {
            "status": "offline",
            "status_label": "Offline",
            "wallet": "",
            "session_started_at": None,
            "updated_at": utc_now(),
            "uptime_seconds": 0,
            "current_route": None,
            "last_error": None,
        },
        "balances": {
            "wallet": {
                "USDC": _empty_asset(),
                "USDG": _empty_asset(),
                "PYUSD": _empty_asset(),
                "USDT": _empty_asset(),
                "SOL": _empty_asset(),
            },
            "pools": {
                "USDC": _empty_asset(),
                "USDG": _empty_asset(),
                "PYUSD": _empty_asset(),
                "USDT": _empty_asset(),
            },
        },
        "market": {"sol_usd": 0.0, "stablecoin_valuation": "$1 estimate"},
        "pending_submission": None,
        "performance": {
            "total_realized_pnl_usd": 0.0,
            "session_realized_pnl_usd": 0.0,
            "legacy_balance_change_usd": 0.0,
            "legacy_arbs": 0,
            "total_arbs": 0,
            "session_arbs": 0,
            "total_attempts": 0,
            "session_attempts": 0,
            "total_sol_consumed": 0.0,
            "session_sol_consumed": 0.0,
            "total_sol_cost_usd": 0.0,
            "session_sol_cost_usd": 0.0,
            "current_portfolio_usd": 0.0,
            "current_stablecoin_value_usd": 0.0,
            "current_sol_value_usd": 0.0,
        },
        "recent_arbs": [],
    }


def read_state(path=DEFAULT_STATE_PATH):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            state = json.load(handle)
        if state.get("schema_version") != 1:
            raise ValueError("unsupported state schema")
        return state
    except (OSError, ValueError, json.JSONDecodeError, TypeError):
        return default_state()


class BotStateStore:
    def __init__(self, state_path=DEFAULT_STATE_PATH, pnl_path=DEFAULT_PNL_PATH):
        self.state_path = Path(state_path)
        self.pnl_path = Path(pnl_path)
        self.lock = threading.RLock()
        state_exists = self.state_path.exists()
        self.state = read_state(self.state_path)
        if not state_exists:
            self._migrate_legacy_pnl()
        self._session_started_monotonic = None

    def _migrate_legacy_pnl(self):
        try:
            with open(self.pnl_path, "r", encoding="utf-8") as handle:
                for line in handle:
                    key, _, value = line.partition(":")
                    value = value.strip()
                    if key == "Arbs Executed":
                        legacy_arbs = int(value)
                        self.state["performance"]["legacy_arbs"] = legacy_arbs
                        self.state["performance"]["total_arbs"] = legacy_arbs
                    elif key == "Total Balance Change":
                        self.state["performance"]["legacy_balance_change_usd"] = float(
                            value.replace("$", "").replace(",", "")
                        )
        except (OSError, ValueError):
            return

    def _uptime_seconds(self):
        if self._session_started_monotonic is None:
            return int(self.state["bot"].get("uptime_seconds", 0))
        return int(time.monotonic() - self._session_started_monotonic)

    def _write_locked(self):
        self.state["bot"]["updated_at"] = utc_now()
        self.state["bot"]["uptime_seconds"] = self._uptime_seconds()
        _atomic_write(self.state_path, json.dumps(self.state, indent=2, sort_keys=True) + "\n")
        self._write_legacy_pnl_locked()

    def _write_legacy_pnl_locked(self):
        uptime = self._uptime_seconds()
        hours, remainder = divmod(uptime, 3600)
        minutes, seconds = divmod(remainder, 60)
        performance = self.state["performance"]
        content = (
            f"Time Run: {hours:02d}:{minutes:02d}:{seconds:02d}\n"
            f"Arbs Executed: {performance['total_arbs']}\n"
            f"Total Balance Change: ${performance['total_realized_pnl_usd']:.4f}\n"
        )
        _atomic_write(self.pnl_path, content)

    def start_session(self, wallet):
        with self.lock:
            self._session_started_monotonic = time.monotonic()
            self.state["bot"].update(
                {
                    "status": "starting",
                    "status_label": "Starting",
                    "wallet": str(wallet),
                    "session_started_at": utc_now(),
                    "current_route": None,
                    "last_error": None,
                }
            )
            performance = self.state["performance"]
            for key in (
                "session_realized_pnl_usd",
                "session_arbs",
                "session_attempts",
                "session_sol_consumed",
                "session_sol_cost_usd",
            ):
                performance[key] = 0 if key in ("session_arbs", "session_attempts") else 0.0
            self._write_locked()

    def set_status(self, status, label=None, route=None, error=None):
        with self.lock:
            self.state["bot"]["status"] = status
            self.state["bot"]["status_label"] = label or status.replace("_", " ").title()
            self.state["bot"]["current_route"] = route
            if error is not None:
                self.state["bot"]["last_error"] = str(error)
            elif status not in ("error", "exposed"):
                self.state["bot"]["last_error"] = None
            self._write_locked()

    def get_pending_submission(self):
        with self.lock:
            pending = self.state.get("pending_submission")
            return copy.deepcopy(pending) if isinstance(pending, dict) else None

    def set_pending_submission(self, signature, blockhash, label):
        """Persist a signed transaction before it is handed to a broadcaster."""

        with self.lock:
            self.state["pending_submission"] = {
                "signature": str(signature),
                "blockhash": str(blockhash),
                "label": str(label),
                "saved_at": utc_now(),
            }
            self._write_locked()

    def clear_pending_submission(self, expected_signature=None):
        """Clear the pending lock, optionally only for the expected signature."""

        with self.lock:
            pending = self.state.get("pending_submission")
            if not isinstance(pending, dict):
                return False
            if (
                expected_signature is not None
                and pending.get("signature") != str(expected_signature)
            ):
                return False
            self.state["pending_submission"] = None
            self._write_locked()
            return True

    @staticmethod
    def _asset(raw, decimals, usd_price=1.0):
        amount = int(raw) / (10**decimals)
        return {
            "raw": int(raw),
            "amount": amount,
            "usd_value": amount * float(usd_price),
        }

    def update_snapshot(self, wallet_raw, pool_raw, sol_lamports, sol_usd):
        with self.lock:
            wallet_balances = self.state["balances"]["wallet"]
            pool_balances = self.state["balances"]["pools"]
            for asset in STABLE_ASSETS:
                wallet_balances[asset] = self._asset(wallet_raw.get(asset, 0), 6)
                pool_balances[asset] = self._asset(pool_raw.get(asset, 0), 6)
            wallet_balances["SOL"] = self._asset(sol_lamports, 9, sol_usd)
            self.state["market"]["sol_usd"] = float(sol_usd)

            stable_value = sum(wallet_balances[asset]["usd_value"] for asset in STABLE_ASSETS)
            sol_value = wallet_balances["SOL"]["usd_value"]
            performance = self.state["performance"]
            performance["current_stablecoin_value_usd"] = stable_value
            performance["current_sol_value_usd"] = sol_value
            performance["current_portfolio_usd"] = stable_value + sol_value
            self._write_locked()

    def record_attempt(
        self,
        route,
        amount,
        expected_gross_profit_usd,
        before,
        after,
        success,
        note="",
    ):
        with self.lock:
            required = tuple(asset.lower() for asset in STABLE_ASSETS) + ("sol_lamports", "sol_price")
            for snapshot_name, snapshot in (("before", before), ("after", after)):
                missing = [key for key in required if key not in snapshot or snapshot[key] is None]
                if missing:
                    raise ValueError(
                        f"Refusing to record P&L from incomplete {snapshot_name} snapshot: "
                        + ", ".join(missing)
                    )
                for key in required:
                    try:
                        value = float(snapshot[key])
                    except (TypeError, ValueError) as exc:
                        raise ValueError(
                            f"Refusing to record P&L with invalid {snapshot_name}.{key}"
                        ) from exc
                    if not math.isfinite(value) or value < 0:
                        raise ValueError(
                            f"Refusing to record P&L with invalid {snapshot_name}.{key}={value}"
                        )

            before_stables = sum(
                float(before[asset.lower()]) * PNL_ASSET_VALUES_USD[asset]
                for asset in STABLE_ASSETS
            )
            after_stables = sum(
                float(after[asset.lower()]) * PNL_ASSET_VALUES_USD[asset]
                for asset in STABLE_ASSETS
            )
            stablecoin_change = after_stables - before_stables
            before_lamports = int(before.get("sol_lamports", 0))
            after_lamports = int(after.get("sol_lamports", 0))
            consumed_lamports = max(0, before_lamports - after_lamports)
            sol_consumed = consumed_lamports / 10**9

            prices = [float(before.get("sol_price", 0.0)), float(after.get("sol_price", 0.0))]
            positive_prices = [price for price in prices if price > 0]
            sol_price_reference = sum(positive_prices) / len(positive_prices) if positive_prices else 0.0
            sol_cost_usd = sol_consumed * sol_price_reference
            realized_pnl = stablecoin_change - sol_cost_usd

            record = {
                "id": uuid.uuid4().hex[:12],
                "timestamp": utc_now(),
                "route": route,
                "amount": float(amount),
                "status": "success" if success else "failed",
                "expected_gross_profit_usd": float(expected_gross_profit_usd),
                "stablecoin_change_usd": stablecoin_change,
                "sol_consumed_lamports": consumed_lamports,
                "sol_consumed": sol_consumed,
                "sol_price_usd": sol_price_reference,
                "sol_cost_usd": sol_cost_usd,
                "realized_pnl_usd": realized_pnl,
                "note": str(note),
            }

            performance = self.state["performance"]
            performance["total_attempts"] += 1
            performance["session_attempts"] += 1
            performance["total_realized_pnl_usd"] += realized_pnl
            performance["session_realized_pnl_usd"] += realized_pnl
            performance["total_sol_consumed"] += sol_consumed
            performance["session_sol_consumed"] += sol_consumed
            performance["total_sol_cost_usd"] += sol_cost_usd
            performance["session_sol_cost_usd"] += sol_cost_usd
            if success:
                performance["total_arbs"] += 1
                performance["session_arbs"] += 1

            self.state["recent_arbs"].insert(0, record)
            self.state["recent_arbs"] = self.state["recent_arbs"][:50]
            self._write_locked()
            return copy.deepcopy(record)

    def snapshot(self):
        with self.lock:
            result = copy.deepcopy(self.state)
            result["bot"]["uptime_seconds"] = self._uptime_seconds()
            return result

    def estimated_execution_cost_usd(self, default_cost=0.005, percentile=0.90):
        """Estimate a conservative full-attempt SOL cost from recent records."""
        with self.lock:
            costs = sorted(
                float(record.get("sol_cost_usd", 0.0))
                for record in self.state.get("recent_arbs", [])
                if float(record.get("sol_cost_usd", 0.0)) > 0
            )
            if not costs:
                return float(default_cost)
            rank = max(0, min(len(costs) - 1, math.ceil(len(costs) * float(percentile)) - 1))
            return max(float(default_cost), costs[rank])
