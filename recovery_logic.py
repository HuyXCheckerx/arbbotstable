"""Pure calculations and safety checks for an intermediate-token recovery."""

from __future__ import annotations


RAW_SCALE = 1_000_000


def raw_amount_to_human(amount_raw):
    return int(amount_raw) / RAW_SCALE


def recovery_quote_metrics(amount_raw, out_raw, execution_cost_usd, slippage_bps):
    """Return conservative dollar P&L for returning one exact token amount."""

    amount_raw = int(amount_raw)
    out_raw = int(out_raw)
    amount_usd = raw_amount_to_human(amount_raw)
    gross_profit_usd = (out_raw - amount_raw) / RAW_SCALE
    slippage_reserve_usd = amount_usd * max(0, int(slippage_bps)) / 10_000
    net_profit_usd = gross_profit_usd - float(execution_cost_usd) - slippage_reserve_usd
    return {
        "gross_profit_usd": gross_profit_usd,
        "slippage_reserve_usd": slippage_reserve_usd,
        "net_profit_usd": net_profit_usd,
    }


def recovery_quote_is_eligible(metrics, minimum_net_profit_usd):
    return metrics["net_profit_usd"] >= float(minimum_net_profit_usd)


def capacity_limited_recovery_amount_raw(
    planned_amount_raw,
    backend_available_raw,
    reserve_raw=1_000_000,
):
    """Return a safe partial input when Stable.com compares input to liquidity."""
    capacity = max(0, int(backend_available_raw) - max(0, int(reserve_raw)))
    return min(max(0, int(planned_amount_raw)), capacity)


def planned_amount_is_available(wallet_raw, planned_raw, tolerance_raw):
    """Allow a tiny display/settlement tolerance, never silently resize a plan."""

    return int(wallet_raw) + int(tolerance_raw) >= int(planned_raw)
