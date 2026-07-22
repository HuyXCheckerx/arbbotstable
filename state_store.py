import copy
import json
import math
import os
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = Path(os.environ.get("BOT_STATE_DB", BASE_DIR / "bot_state.db"))
DEFAULT_STATE_PATH = Path(os.environ.get("BOT_STATE_FILE", BASE_DIR / "bot_state.json"))
DEFAULT_PNL_PATH = BASE_DIR / "pnl.txt"
STABLE_ASSETS = ("USDC", "USDG", "PYUSD", "USDT")
PNL_ASSET_VALUES_USD = {"USDC": 1.0, "USDG": 1.0, "PYUSD": 1.0, "USDT": 0.999}
DATABASE_SCHEMA_VERSION = 1


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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


def _merge_missing(target, defaults):
    for key, value in defaults.items():
        if key not in target:
            target[key] = copy.deepcopy(value)
        elif isinstance(value, dict) and isinstance(target[key], dict):
            _merge_missing(target[key], value)
    return target


def _read_legacy_state(path):
    if path is None:
        return None
    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            state = json.load(handle)
        if state.get("schema_version") != 1:
            raise ValueError("unsupported state schema")
        return _merge_missing(state, default_state())
    except (OSError, ValueError, json.JSONDecodeError, TypeError, AttributeError):
        return None


def _migrate_legacy_pnl(state, path):
    if path is None:
        return
    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            for line in handle:
                key, _, value = line.partition(":")
                value = value.strip()
                if key == "Arbs Executed":
                    legacy_arbs = int(value)
                    state["performance"]["legacy_arbs"] = legacy_arbs
                    state["performance"]["total_arbs"] = legacy_arbs
                elif key == "Total Balance Change":
                    state["performance"]["legacy_balance_change_usd"] = float(
                        value.replace("$", "").replace(",", "")
                    )
    except (OSError, ValueError):
        return


def _connect(db_path):
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path, timeout=10, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 10000")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    return connection


def _create_schema(connection):
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS dashboard_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            schema_version INTEGER NOT NULL,
            state_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS attempts (
            id TEXT PRIMARY KEY,
            timestamp TEXT NOT NULL,
            route TEXT NOT NULL,
            amount REAL NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('success', 'failed')),
            expected_gross_profit_usd REAL NOT NULL,
            stablecoin_change_usd REAL NOT NULL,
            sol_consumed_lamports INTEGER NOT NULL,
            sol_consumed REAL NOT NULL,
            sol_price_usd REAL NOT NULL,
            sol_cost_usd REAL NOT NULL,
            realized_pnl_usd REAL NOT NULL,
            note TEXT NOT NULL DEFAULT ''
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS attempts_timestamp_idx ON attempts (timestamp DESC)"
    )
    connection.execute(f"PRAGMA user_version = {DATABASE_SCHEMA_VERSION}")


def _attempt_values(record):
    return (
        str(record["id"]),
        str(record["timestamp"]),
        str(record.get("route", "")),
        float(record.get("amount", 0.0)),
        "success" if record.get("status") == "success" else "failed",
        float(record.get("expected_gross_profit_usd", 0.0)),
        float(record.get("stablecoin_change_usd", 0.0)),
        int(record.get("sol_consumed_lamports", 0)),
        float(record.get("sol_consumed", 0.0)),
        float(record.get("sol_price_usd", 0.0)),
        float(record.get("sol_cost_usd", 0.0)),
        float(record.get("realized_pnl_usd", 0.0)),
        str(record.get("note", "")),
    )


ATTEMPT_INSERT_SQL = """
    INSERT INTO attempts (
        id, timestamp, route, amount, status, expected_gross_profit_usd,
        stablecoin_change_usd, sol_consumed_lamports, sol_consumed,
        sol_price_usd, sol_cost_usd, realized_pnl_usd, note
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def _initialize_database(connection, legacy_state_path=None, legacy_pnl_path=None):
    with connection:
        schema_version = connection.execute("PRAGMA user_version").fetchone()[0]
        if schema_version == 0:
            _create_schema(connection)
        elif schema_version != DATABASE_SCHEMA_VERSION:
            raise RuntimeError(
                f"Unsupported bot database schema version {schema_version}; "
                f"expected {DATABASE_SCHEMA_VERSION}"
            )
        existing = connection.execute(
            "SELECT 1 FROM dashboard_state WHERE id = 1"
        ).fetchone()
        if existing:
            return

        state = _read_legacy_state(legacy_state_path)
        if state is None:
            state = default_state()
            _migrate_legacy_pnl(state, legacy_pnl_path)

        for record in state.get("recent_arbs", []):
            try:
                connection.execute(ATTEMPT_INSERT_SQL, _attempt_values(record))
            except (KeyError, TypeError, ValueError, sqlite3.IntegrityError):
                continue

        connection.execute(
            """
            INSERT INTO dashboard_state (id, schema_version, state_json, updated_at)
            VALUES (1, ?, ?, ?)
            """,
            (
                DATABASE_SCHEMA_VERSION,
                json.dumps(state, separators=(",", ":"), sort_keys=True),
                state["bot"].get("updated_at") or utc_now(),
            ),
        )


def _default_legacy_paths(db_path, legacy_state_path, legacy_pnl_path):
    try:
        is_default_database = Path(db_path).resolve() == DEFAULT_DB_PATH.resolve()
    except OSError:
        is_default_database = False
    if is_default_database:
        if legacy_state_path is None:
            legacy_state_path = DEFAULT_STATE_PATH
        if legacy_pnl_path is None:
            legacy_pnl_path = DEFAULT_PNL_PATH
    return legacy_state_path, legacy_pnl_path


def _row_to_attempt(row):
    return {
        "id": row["id"],
        "timestamp": row["timestamp"],
        "route": row["route"],
        "amount": row["amount"],
        "status": row["status"],
        "expected_gross_profit_usd": row["expected_gross_profit_usd"],
        "stablecoin_change_usd": row["stablecoin_change_usd"],
        "sol_consumed_lamports": row["sol_consumed_lamports"],
        "sol_consumed": row["sol_consumed"],
        "sol_price_usd": row["sol_price_usd"],
        "sol_cost_usd": row["sol_cost_usd"],
        "realized_pnl_usd": row["realized_pnl_usd"],
        "note": row["note"],
    }


def _read_state_from_connection(connection):
    row = connection.execute(
        "SELECT state_json FROM dashboard_state WHERE id = 1"
    ).fetchone()
    if row is None:
        return default_state()
    try:
        state = json.loads(row["state_json"])
        if state.get("schema_version") != 1:
            raise ValueError("unsupported state schema")
        _merge_missing(state, default_state())
    except (ValueError, json.JSONDecodeError, TypeError, AttributeError):
        state = default_state()

    rows = connection.execute(
        "SELECT * FROM attempts ORDER BY timestamp DESC, rowid DESC LIMIT 50"
    ).fetchall()
    state["recent_arbs"] = [_row_to_attempt(attempt) for attempt in rows]
    return state


def read_state(db_path=DEFAULT_DB_PATH, legacy_state_path=None, legacy_pnl_path=None):
    legacy_state_path, legacy_pnl_path = _default_legacy_paths(
        db_path, legacy_state_path, legacy_pnl_path
    )
    connection = _connect(db_path)
    try:
        _initialize_database(connection, legacy_state_path, legacy_pnl_path)
        return _read_state_from_connection(connection)
    finally:
        connection.close()


def read_daily_profit(db_path=DEFAULT_DB_PATH, days=371, now=None):
    days = max(1, min(int(days), 3660))
    now = now or datetime.now(timezone.utc)
    start_date = (now.astimezone(timezone.utc).date() - timedelta(days=days - 1)).isoformat()
    connection = _connect(db_path)
    try:
        legacy_state_path, legacy_pnl_path = _default_legacy_paths(db_path, None, None)
        _initialize_database(connection, legacy_state_path, legacy_pnl_path)
        rows = connection.execute(
            """
            SELECT
                substr(timestamp, 1, 10) AS profit_date,
                SUM(realized_pnl_usd) AS profit_usdc,
                COUNT(*) AS attempts,
                SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) AS successful_arbs
            FROM attempts
            WHERE substr(timestamp, 1, 10) >= ?
            GROUP BY substr(timestamp, 1, 10)
            ORDER BY profit_date
            """,
            (start_date,),
        ).fetchall()
        return [
            {
                "date": row["profit_date"],
                "profit_usdc": row["profit_usdc"],
                "attempts": row["attempts"],
                "successful_arbs": row["successful_arbs"],
            }
            for row in rows
        ]
    finally:
        connection.close()


def read_dashboard_state(db_path=DEFAULT_DB_PATH, days=371):
    state = read_state(db_path)
    state["daily_profit"] = {
        "currency": "USDC",
        "timezone": "UTC",
        "days": read_daily_profit(db_path, days),
    }
    return state


class BotStateStore:
    def __init__(
        self,
        db_path=DEFAULT_DB_PATH,
        legacy_state_path=None,
        legacy_pnl_path=None,
    ):
        self.db_path = Path(db_path)
        legacy_state_path, legacy_pnl_path = _default_legacy_paths(
            self.db_path, legacy_state_path, legacy_pnl_path
        )
        self.lock = threading.RLock()
        self.connection = _connect(self.db_path)
        _initialize_database(self.connection, legacy_state_path, legacy_pnl_path)
        self.state = _read_state_from_connection(self.connection)
        self._session_started_monotonic = None

    def close(self):
        with self.lock:
            self.connection.close()

    def _uptime_seconds(self):
        if self._session_started_monotonic is None:
            return int(self.state["bot"].get("uptime_seconds", 0))
        return int(time.monotonic() - self._session_started_monotonic)

    def _persist_state_locked(self):
        self.state["bot"]["updated_at"] = utc_now()
        self.state["bot"]["uptime_seconds"] = self._uptime_seconds()
        self.connection.execute(
            """
            INSERT INTO dashboard_state (id, schema_version, state_json, updated_at)
            VALUES (1, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                schema_version = excluded.schema_version,
                state_json = excluded.state_json,
                updated_at = excluded.updated_at
            """,
            (
                DATABASE_SCHEMA_VERSION,
                json.dumps(self.state, separators=(",", ":"), sort_keys=True),
                self.state["bot"]["updated_at"],
            ),
        )

    def _write_locked(self):
        with self.connection:
            self._persist_state_locked()

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
            required = tuple(asset.lower() for asset in STABLE_ASSETS) + (
                "sol_lamports",
                "sol_price",
            )
            for snapshot_name, snapshot in (("before", before), ("after", after)):
                missing = [
                    key for key in required if key not in snapshot or snapshot[key] is None
                ]
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

            prices = [
                float(before.get("sol_price", 0.0)),
                float(after.get("sol_price", 0.0)),
            ]
            positive_prices = [price for price in prices if price > 0]
            sol_price_reference = (
                sum(positive_prices) / len(positive_prices) if positive_prices else 0.0
            )
            sol_cost_usd = sol_consumed * sol_price_reference
            realized_pnl = stablecoin_change - sol_cost_usd

            record = {
                "id": uuid.uuid4().hex,
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
            with self.connection:
                self.connection.execute(ATTEMPT_INSERT_SQL, _attempt_values(record))
                self._persist_state_locked()
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
            rank = max(
                0,
                min(
                    len(costs) - 1,
                    math.ceil(len(costs) * float(percentile)) - 1,
                ),
            )
            return max(float(default_cost), costs[rank])
