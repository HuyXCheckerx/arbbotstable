"""Return a stranded USDG/PYUSD/USDT amount after a failed arbitrage exit.

This process never opens an arbitrage leg.  It consumes the exact amount in
``recovery_state.json`` that the main scanner recorded after a failed exit.
"""

from __future__ import annotations

import json
import os
import time

import requests

# swapstable installs the shared console/file logger at import time.
os.environ.setdefault("BOT_LOG_NAME", "recovery_worker")

from recovery_logic import (  # noqa: E402
    capacity_limited_recovery_amount_raw,
    raw_amount_to_human,
    recovery_quote_is_eligible,
    recovery_quote_metrics,
)
from recovery_store import RecoveryStore  # noqa: E402
from swapstable import (  # noqa: E402
    JUP_API_KEY,
    POSITION_TOLERANCE_RAW,
    PRIVATE_KEY,
    PYUSD_MINT,
    PYUSD_MINT_PK,
    RPC_URL,
    TOKEN_2022_PROGRAM,
    TOKEN_PROGRAM,
    USDC_MINT,
    USDC_MINT_PK,
    USDG_MINT,
    USDG_MINT_PK,
    USDT_MINT,
    USDT_MINT_PK,
    BalanceMonitor,
    Client,
    Confirmed,
    Keypair,
    confirm_transfer_ws_first,
    execute_stable_swap,
    execute_jup_swap,
    get_ata,
    get_jup_quote,
    get_submission_signature_status,
    get_token_balance,
    is_submission_blockhash_valid,
)


MIN_NET_PROFIT_USD = float(os.environ.get("RECOVERY_MIN_NET_PROFIT_USD", "0.10"))
JUP_SLIPPAGE_BPS = max(0, int(os.environ.get("RECOVERY_JUP_SLIPPAGE_BPS", "1")))
EXECUTION_COST_USD = float(os.environ.get("RECOVERY_EXECUTION_COST_USD", "0.01"))
QUOTE_INTERVAL_SECONDS = max(
    0.25, float(os.environ.get("RECOVERY_QUOTE_INTERVAL_SECONDS", "1"))
)
STABLE_FALLBACK_RETRY_SECONDS = max(
    1.0, float(os.environ.get("RECOVERY_STABLE_RETRY_SECONDS", "5"))
)
STABLE_MAX_RECOVERY_AMOUNT_USD = float(
    os.environ.get("RECOVERY_STABLE_MAX_AMOUNT_USD", "1000000")
)

ASSETS = {
    "USDG": (USDG_MINT, USDG_MINT_PK, TOKEN_2022_PROGRAM, "user_usdg"),
    "PYUSD": (PYUSD_MINT, PYUSD_MINT_PK, TOKEN_2022_PROGRAM, "user_pyusd"),
    "USDT": (USDT_MINT, USDT_MINT_PK, TOKEN_PROGRAM, "user_usdt"),
}


def load_keypair():
    try:
        return Keypair.from_base58_string(PRIVATE_KEY)
    except Exception:
        return Keypair.from_bytes(bytes(json.loads(PRIVATE_KEY)))


def reconcile_pending_plan(client, store, plan, token_ata):
    """Do not replace a possibly-broadcast recovery transaction after restart."""

    submission = plan.get("submission")
    if not isinstance(submission, dict):
        return False
    signature = submission.get("signature", "")
    blockhash = submission.get("blockhash", "")
    if not signature or not blockhash:
        store.mark_manual_review(plan["id"], "Recovery submission record is incomplete")
        return True

    state, detail = get_submission_signature_status(client, signature)
    if state == "confirmed":
        actual = get_token_balance(client, token_ata)
        if actual + POSITION_TOLERANCE_RAW < int(plan["amount_raw"]):
            print(f"[+] Recovery {signature} confirmed; exact planned token amount left wallet.")
            store.complete(plan["id"])
        else:
            store.mark_manual_review(
                plan["id"],
                "Recovery transaction confirmed but the planned token amount is still present",
            )
        return True
    if state == "failed":
        print(f"[!] Recovery {signature} failed on-chain: {detail}; resuming quote watch.")
        store.clear_pending_submission(signature)
        store.mark_watching(plan["id"], detail)
        return True
    if state == "not_found" and is_submission_blockhash_valid(client, blockhash) is False:
        # Repeat once before treating an unseen transaction as expired.
        repeat_state, _ = get_submission_signature_status(client, signature)
        if repeat_state == "not_found":
            print(f"[!] Recovery {signature} was not recorded before blockhash expiry.")
            store.clear_pending_submission(signature)
            store.mark_watching(plan["id"], "Submission expired without landing")
            return True
    print(f"[~] Recovery transaction {signature} is {state}; no replacement will be sent.")
    return True


def attempt_stable_recovery(
    session,
    client,
    keypair,
    monitor,
    store,
    plan,
    token,
    token_ata,
    balance_key,
    usdc_ata,
    actual_raw,
    reason,
):
    """Immediately return the planned amount through Stable.com after a bad Jup quote."""

    amount_raw = int(plan["amount_raw"])
    amount_human = raw_amount_to_human(amount_raw)
    if amount_human > STABLE_MAX_RECOVERY_AMOUNT_USD:
        store.mark_manual_review(
            plan["id"],
            f"Stable.com recovery amount {amount_human:.6f} exceeds its "
            f"${STABLE_MAX_RECOVERY_AMOUNT_USD:.0f} limit",
        )
        print(
            f"[halt] Not submitting {amount_human:.6f} {token} to Stable.com: "
            f"it exceeds the ${STABLE_MAX_RECOVERY_AMOUNT_USD:.0f} API limit."
        )
        return False
    print(
        f"[*] Recovery fallback: Jupiter is not profitable ({reason}); "
        f"sending exact {amount_human:.6f} {token}->USDC through Stable.com."
    )
    usdc_before = get_token_balance(client, usdc_ata)
    cursor = monitor.snapshot([balance_key, "user_usdc"])
    result = execute_stable_swap(
        session,
        client,
        keypair,
        token,
        "USDC",
        amount_human,
        pending_store=store,
        submission_label=f"Recovery Stable.com {token}->USDC",
    )
    if not result.may_have_landed:
        constraint = result.liquidity_constraint
        if token == "USDT" and constraint:
            partial_raw = capacity_limited_recovery_amount_raw(
                amount_raw,
                constraint["available_raw"],
            )
            if POSITION_TOLERANCE_RAW < partial_raw < amount_raw:
                amount_raw = partial_raw
                amount_human = raw_amount_to_human(amount_raw)
                print(
                    f"[recovery] Stable.com capacity is temporarily limited; "
                    f"returning {amount_human:.6f} USDT now and retaining the "
                    "confirmed remainder in the recovery plan."
                )
                usdc_before = get_token_balance(client, usdc_ata)
                cursor = monitor.snapshot([balance_key, "user_usdc"])
                result = execute_stable_swap(
                    session,
                    client,
                    keypair,
                    token,
                    "USDC",
                    amount_human,
                    pending_store=store,
                    submission_label=f"Partial recovery Stable.com {token}->USDC",
                )
        if not result.may_have_landed:
            store.mark_watching(
                plan["id"],
                "Stable.com recovery exit was not submitted; waiting for capacity",
            )
            return False
    confirmed, balances, _ = confirm_transfer_ws_first(
        client,
        monitor,
        {
            balance_key: (
                token_ata,
                lambda balance: balance <= actual_raw - amount_raw + POSITION_TOLERANCE_RAW,
            ),
            "user_usdc": (usdc_ata, lambda balance: balance > usdc_before),
        },
        cursor.revisions,
        f"Recovery Stable.com {token}->USDC",
        submission=result.submission,
        pending_store=store,
    )
    if confirmed:
        remaining_raw = int(balances.get(balance_key, max(0, actual_raw - amount_raw)))
        if remaining_raw <= POSITION_TOLERANCE_RAW:
            store.complete(plan["id"])
            print(f"[+] Stable.com recovery complete: {amount_human:.6f} {token} returned.")
        else:
            store.update_remaining_amount(
                plan["id"],
                remaining_raw,
                "Capacity-limited Stable.com recovery is continuing",
            )
            print(
                f"[+] Partial Stable.com recovery confirmed; "
                f"{raw_amount_to_human(remaining_raw):.6f} {token} remains."
            )
        return True
    store.mark_watching(plan["id"], "Stable.com recovery exit did not confirm")
    return False


def main():
    client = Client(RPC_URL, commitment=Confirmed)
    keypair = load_keypair()
    wallet = keypair.pubkey()
    usdc_ata = get_ata(wallet, USDC_MINT_PK, TOKEN_PROGRAM)
    asset_accounts = {
        name: get_ata(wallet, mint_pk, program)
        for name, (_, mint_pk, program, _) in ASSETS.items()
    }
    monitor = BalanceMonitor(
        RPC_URL,
        {
            "user_usdc": str(usdc_ata),
            **{ASSETS[name][3]: str(ata) for name, ata in asset_accounts.items()},
        },
    )
    monitor.seed("user_usdc", get_token_balance(client, usdc_ata))
    for name, ata in asset_accounts.items():
        monitor.seed(ASSETS[name][3], get_token_balance(client, ata))
    monitor.start()

    session = requests.Session()
    session.headers.update({"Content-Type": "application/json"})
    if JUP_API_KEY:
        session.headers.update({"x-api-key": JUP_API_KEY})
    store = RecoveryStore()
    print("[*] Recovery worker started; it will never open a first leg.")

    while True:
        try:
            plan = store.get_active()
            if plan is None:
                time.sleep(QUOTE_INTERVAL_SECONDS)
                continue
            if plan.get("status") == "manual_review":
                time.sleep(5)
                continue
            if float(plan.get("min_net_profit_usd", MIN_NET_PROFIT_USD)) != MIN_NET_PROFIT_USD:
                plan = store.set_min_net_profit(plan["id"], MIN_NET_PROFIT_USD)
                if plan is None:
                    continue
                print(
                    f"[*] Updated persisted recovery threshold to "
                    f"${MIN_NET_PROFIT_USD:.2f}."
                )
            token = plan.get("token")
            if token not in ASSETS:
                store.mark_manual_review(plan["id"], f"Unsupported recovery token: {token}")
                continue
            mint, _, _, balance_key = ASSETS[token]
            token_ata = asset_accounts[token]

            if plan.get("submission"):
                reconcile_pending_plan(client, store, plan, token_ata)
                time.sleep(QUOTE_INTERVAL_SECONDS)
                continue

            amount_raw = int(plan["amount_raw"])
            actual_raw = get_token_balance(client, token_ata)
            if actual_raw <= POSITION_TOLERANCE_RAW:
                print(f"[+] {token} recovery plan already cleared from wallet.")
                store.complete(plan["id"])
                continue
            if abs(actual_raw - amount_raw) > POSITION_TOLERANCE_RAW:
                store.mark_manual_review(
                    plan["id"],
                    f"Wallet has {actual_raw} raw {token}; plan requires exact {amount_raw}",
                )
                continue

            quote = get_jup_quote(
                session,
                mint,
                USDC_MINT,
                amount_raw,
                taker=str(wallet),
                slippage_bps=JUP_SLIPPAGE_BPS,
            )
            if not quote or "outAmount" not in quote:
                attempt_stable_recovery(
                    session, client, keypair, monitor, store, plan, token, token_ata,
                    balance_key, usdc_ata, actual_raw, "quote unavailable",
                )
                time.sleep(STABLE_FALLBACK_RETRY_SECONDS)
                continue
            metrics = recovery_quote_metrics(
                amount_raw, quote["outAmount"], EXECUTION_COST_USD, JUP_SLIPPAGE_BPS
            )
            store.update_quote(plan["id"], metrics["gross_profit_usd"], metrics["net_profit_usd"])
            threshold = MIN_NET_PROFIT_USD
            if not recovery_quote_is_eligible(metrics, threshold):
                attempt_stable_recovery(
                    session, client, keypair, monitor, store, plan, token, token_ata,
                    balance_key, usdc_ata, actual_raw,
                    f"Jupiter net ${metrics['net_profit_usd']:.6f} <= ${threshold:.2f}",
                )
                time.sleep(STABLE_FALLBACK_RETRY_SECONDS)
                continue

            print(
                f"[*] Recovery eligible: {raw_amount_to_human(amount_raw):.6f} {token}->USDC | "
                f"net ${metrics['net_profit_usd']:.6f} (threshold ${threshold:.2f})"
            )
            usdc_before = get_token_balance(client, usdc_ata)
            cursor = monitor.snapshot([balance_key, "user_usdc"])
            submission = execute_jup_swap(
                session, client, keypair, quote, pending_store=store,
                submission_label=f"Recovery {token}->USDC",
            )
            if not submission.may_have_landed:
                store.mark_watching(plan["id"], submission.error or "Jupiter did not accept recovery")
                time.sleep(QUOTE_INTERVAL_SECONDS)
                continue
            confirmed, _, _ = confirm_transfer_ws_first(
                client,
                monitor,
                {
                    balance_key: (
                        token_ata,
                        lambda balance: balance <= actual_raw - amount_raw + POSITION_TOLERANCE_RAW,
                    ),
                    "user_usdc": (usdc_ata, lambda balance: balance > usdc_before),
                },
                cursor.revisions,
                f"Recovery {token}->USDC",
                submission=submission,
                pending_store=store,
            )
            if confirmed:
                store.complete(plan["id"])
                print(
                    f"[+] Recovery complete: exact "
                    f"{raw_amount_to_human(amount_raw):.6f} {token} returned."
                )
            else:
                store.mark_watching(plan["id"], "Recovery exit did not confirm; watching fresh quotes")
        except Exception as exc:
            print(f"[!] Recovery worker error: {exc}")
            time.sleep(5)


if __name__ == "__main__":
    main()
