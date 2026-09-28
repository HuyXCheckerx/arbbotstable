"""Automated minimal proxy rotation and ban detection for MetaMatcha arbitrage.

Detects when the current executor contract taker address is flagged/blacklisted by
MetaMatcha / 0x risk filters, automatically deploys a fresh EIP-1167 proxy in 1 block (~86,000 gas),
updates .env, and restarts the sniper process.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from typing import Any

from src.config.contracts import (
    DUMMY_CLEAN_TAKER,
    MASTER_IMPLEMENTATION,
    OPERATOR_ADDRESS,
    PYUSD,
    USDG,
    get_current_executor,
)
from src.deployers.deploy_proxy_executor import deploy_minimal_proxy

logger = logging.getLogger("ProxyExecutor")


def probe_taker_status(taker_address: str, timeout: float = 12.0) -> bool:
    """Returns True if the taker address succeeds on MetaMatcha; False if blocked (Forbidden/403)."""
    try:
        from src.engines.matcha_browser_bridge import ensure_bridge_running, fetch_bridge_quotes
        ensure_bridge_running(timeout=20.0)

        payload = {
            "chainId": 1,
            "isAllowanceHolderFlow": True,
            "gasPrice": "1000000000",
            "sellTokenAddress": PYUSD.lower(),
            "sellTokenDecimals": 6,
            "buyTokenAddress": USDG.lower(),
            "buyTokenDecimals": 6,
            "sellAmount": "100000000000",
            "slippageBps": 50,
            "slippagePpm": 5000,
            "taker": taker_address.lower(),
        }
        res = fetch_bridge_quotes("ethereum", payload, ["0x"], timeout=timeout)
        if isinstance(res, dict) and any(v.get("ok") for v in res.values() if isinstance(v, dict)):
            return True
        return False
    except Exception as exc:
        err = str(exc).lower()
        if "forbidden" in err or "403" in err:
            return False
        # Other transient error (timeout, network)
        return False


def is_taker_blocked(taker_address: str, error_detail: str = "") -> bool:
    """Determines if a failure is specifically due to the taker address being blocked.

    Verifies by checking if the candidate taker fails with 'Forbidden' while a clean
    dummy taker succeeds under identical network conditions.
    """
    err_lower = error_detail.lower()
    suspicious = any(
        x in err_lower
        for x in (
            "forbidden",
            "403",
            "sendernotauthorized",
            "taker blocked",
            "access denied (http 403)",
        )
    )

    if not suspicious and not error_detail:
        # No initial suspicion; test probe directly
        current_ok = probe_taker_status(taker_address)
        if current_ok:
            return False
        suspicious = True

    if not suspicious:
        return False

    # Verify if a clean reference taker succeeds while the target taker fails
    logger.info("[ProxyExecutor] Investigating potential taker block on %s...", taker_address)
    clean_ok = probe_taker_status(DUMMY_CLEAN_TAKER)
    target_ok = probe_taker_status(taker_address)

    if not target_ok and clean_ok:
        logger.warning(
            "[ProxyExecutor] CONFIRMED: Taker %s is blacklisted (clean taker succeeded).",
            taker_address,
        )
        return True

    return False


def rotate_blocked_proxy_and_restart(
    log_instance: logging.Logger | None = None,
    current_address: str | None = None,
) -> None:
    """Stops services, deploys a fresh EIP-1167 proxy, updates .env, and restarts the running script."""
    log = log_instance or logger
    current = current_address or get_current_executor()

    print("\n" + "=" * 80)
    log.warning("================================================================================")
    log.warning("[ALERT] EXECUTOR CONTRACT TAKER BLOCKED BY METAMATCHA / 0X RISK FILTER!")
    log.warning("Current Blocked Address: %s", current)
    log.warning("Deploying fresh EIP-1167 minimal proxy (~86k gas) and restarting...")
    log.warning("================================================================================")
    print("=" * 80 + "\n")

    # Stop bridge and solver before deploying to prevent race conditions
    try:
        from src.engines.matcha_browser_bridge import stop_bridge_server
        stop_bridge_server()
    except Exception:
        pass

    try:
        new_proxy, tx_hash = deploy_minimal_proxy(
            owner_address=OPERATOR_ADDRESS,
            implementation=MASTER_IMPLEMENTATION,
        )
        log.info("[ProxyExecutor] Deployment confirmed! New Proxy: %s (Tx: %s)", new_proxy, tx_hash)
        os.environ["ETH_ARB_STABLECOIN_EXECUTOR"] = new_proxy
    except Exception as exc:
        log.error("[ProxyExecutor] Failed to deploy minimal proxy: %s", exc)
        sys.exit(1)

    print("\n" + "=" * 80)
    log.info("[ProxyExecutor] Restarting sniper script with new executor %s...", new_proxy)
    print("=" * 80 + "\n")
    time.sleep(1.0)

    # In-place process replacement with exact same arguments
    python_bin = sys.executable
    os.execv(python_bin, [python_bin] + sys.argv)
