"""Durable hand-off between the arbitrage loop and recovery worker."""

from __future__ import annotations

import copy
from contextlib import contextmanager
import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_RECOVERY_PATH = BASE_DIR / "recovery_state.json"


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _atomic_write(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with open(temporary, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _default_state():
    return {"schema_version": 1, "recovery": None}


def _read(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            state = json.load(handle)
        if state.get("schema_version") != 1:
            raise ValueError("unsupported recovery state schema")
        if not isinstance(state.get("recovery"), (dict, type(None))):
            raise ValueError("invalid recovery state")
        return state
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return _default_state()


class RecoveryStore:
    """Store one exact token recovery plan outside the dashboard state file."""

    def __init__(self, path=None):
        self.path = Path(path or os.environ.get("RECOVERY_STATE_FILE", DEFAULT_RECOVERY_PATH))
        self._lock = threading.RLock()

    @contextmanager
    def _process_lock(self):
        """Serialize main/worker state changes across processes on Linux or Windows."""

        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "a+b") as handle:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                handle.write(b"0") if handle.tell() == 0 else None
                handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if os.name == "nt":
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _mutate(self, callback):
        with self._lock:
            with self._process_lock():
                state = _read(self.path)
                result = callback(state)
                _atomic_write(self.path, state)
                return copy.deepcopy(result)

    def get_active(self):
        with self._lock:
            with self._process_lock():
                recovery = _read(self.path).get("recovery")
                return copy.deepcopy(recovery) if isinstance(recovery, dict) else None

    def schedule(self, token, amount_raw, min_net_profit_usd, route, reason=""):
        """Create a recovery plan, preserving an existing active plan."""

        amount_raw = int(amount_raw)
        if amount_raw <= 0:
            raise ValueError("recovery amount must be positive")

        def apply(state):
            current = state.get("recovery")
            if isinstance(current, dict):
                return current
            plan = {
                "id": uuid.uuid4().hex,
                "status": "watching",
                "token": str(token),
                "amount_raw": amount_raw,
                "min_net_profit_usd": float(min_net_profit_usd),
                "route": str(route),
                "reason": str(reason),
                "created_at": utc_now(),
                "updated_at": utc_now(),
                "last_quote": None,
                "last_error": None,
                "submission": None,
            }
            state["recovery"] = plan
            return plan

        return self._mutate(apply)

    def update_quote(self, plan_id, gross_profit_usd, net_profit_usd):
        def apply(state):
            plan = state.get("recovery")
            if not isinstance(plan, dict) or plan.get("id") != plan_id:
                return None
            plan["last_quote"] = {
                "gross_profit_usd": float(gross_profit_usd),
                "net_profit_usd": float(net_profit_usd),
                "quoted_at": utc_now(),
            }
            plan["updated_at"] = utc_now()
            return plan

        return self._mutate(apply)

    def set_min_net_profit(self, plan_id, min_net_profit_usd):
        """Apply a changed recovery policy to an already-persisted plan."""

        def apply(state):
            plan = state.get("recovery")
            if not isinstance(plan, dict) or plan.get("id") != plan_id:
                return None
            plan["min_net_profit_usd"] = float(min_net_profit_usd)
            plan["updated_at"] = utc_now()
            return plan

        return self._mutate(apply)

    def sync_detected_position(self, token, amount_raw, min_net_profit_usd, route):
        """Adopt a startup-detected balance unless a transaction is pending.

        A prior process can leave a stale watching plan behind.  Its amount is
        not an authority over the wallet; the confirmed current balance is.
        """

        amount_raw = int(amount_raw)
        if amount_raw <= 0:
            raise ValueError("detected recovery amount must be positive")

        def apply(state):
            plan = state.get("recovery")
            if not isinstance(plan, dict):
                plan = {
                    "id": uuid.uuid4().hex,
                    "status": "watching",
                    "token": str(token),
                    "amount_raw": amount_raw,
                    "min_net_profit_usd": float(min_net_profit_usd),
                    "route": str(route),
                    "reason": "Detected intermediate position before a new first leg",
                    "created_at": utc_now(),
                    "updated_at": utc_now(),
                    "last_quote": None,
                    "last_error": None,
                    "submission": None,
                }
                state["recovery"] = plan
                return plan, True
            if plan.get("submission"):
                return plan, False
            if plan.get("token") == str(token):
                changed = int(plan.get("amount_raw", 0)) != amount_raw
                plan["amount_raw"] = amount_raw
                plan["min_net_profit_usd"] = float(min_net_profit_usd)
                plan["route"] = str(route)
                plan["status"] = "watching"
                plan["last_error"] = None
                plan["updated_at"] = utc_now()
                return plan, changed
            return plan, False

        return self._mutate(apply)

    def mark_watching(self, plan_id, error=None):
        def apply(state):
            plan = state.get("recovery")
            if not isinstance(plan, dict) or plan.get("id") != plan_id:
                return None
            plan["status"] = "watching"
            plan["submission"] = None
            plan["last_error"] = None if error is None else str(error)
            plan["updated_at"] = utc_now()
            return plan

        return self._mutate(apply)

    def mark_manual_review(self, plan_id, error):
        def apply(state):
            plan = state.get("recovery")
            if not isinstance(plan, dict) or plan.get("id") != plan_id:
                return None
            plan["status"] = "manual_review"
            plan["last_error"] = str(error)
            plan["updated_at"] = utc_now()
            return plan

        return self._mutate(apply)

    # These two methods intentionally match swapstable's pending-store API.
    def set_pending_submission(self, signature, blockhash, label):
        def apply(state):
            plan = state.get("recovery")
            if not isinstance(plan, dict):
                raise RuntimeError("cannot submit without an active recovery plan")
            plan["status"] = "pending"
            plan["submission"] = {
                "signature": str(signature),
                "blockhash": str(blockhash),
                "label": str(label),
                "saved_at": utc_now(),
            }
            plan["updated_at"] = utc_now()
            return plan

        return self._mutate(apply)

    def clear_pending_submission(self, expected_signature=None):
        def apply(state):
            plan = state.get("recovery")
            if not isinstance(plan, dict):
                return False
            submission = plan.get("submission")
            if not isinstance(submission, dict):
                return False
            if (
                expected_signature is not None
                and submission.get("signature") != str(expected_signature)
            ):
                return False
            plan["submission"] = None
            plan["updated_at"] = utc_now()
            return True

        return self._mutate(apply)

    def complete(self, plan_id):
        def apply(state):
            plan = state.get("recovery")
            if not isinstance(plan, dict) or plan.get("id") != plan_id:
                return False
            state["recovery"] = None
            return True

        return self._mutate(apply)


__all__ = ["DEFAULT_RECOVERY_PATH", "RecoveryStore"]
