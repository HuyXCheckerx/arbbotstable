"""Pure calculations and safety checks for an intermediate-token recovery."""

from __future__ import annotations


DECIMALS = 1_000_000


def recovery_quote_metrics(amount_raw, out_raw, execution_cost_usd, slippage_bps):
    """Return conservative dollar P&L for returning one exact token amount."""

    amount_raw = int(amount_raw)
    out_raw = int(out_raw)
    amount_usd = amount_raw / DECIMALS
    gross_profit_usd = (out_raw - amount_raw) / DECIMALS
    slippage_reserve_usd = amount_usd * max(0, int(slippage_bps)) / 10_000
    net_profit_usd = gross_profit_usd - float(execution_cost_usd) - slippage_reserve_usd
    return {
        "gross_profit_usd": gross_profit_usd,
        "slippage_reserve_usd": slippage_reserve_usd,
        "net_profit_usd": net_profit_usd,
    }


def recovery_quote_is_eligible(metrics, minimum_net_profit_usd):
    return metrics["net_profit_usd"] >= float(minimum_net_profit_usd)


def planned_amount_is_available(wallet_raw, planned_raw, tolerance_raw):
    """Allow a tiny display/settlement tolerance, never silently resize a plan."""

    return int(wallet_raw) + int(tolerance_raw) >= int(planned_raw)
