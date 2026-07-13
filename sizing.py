import math


def usdc_strategy_directions(tokens=("USDG", "PYUSD")):
    """Return the only supported arbitrage cycles: USDC base, two venue orders."""
    return [
        (token, venue_order)
        for token in tokens
        for venue_order in ("stable_first", "jupiter_first")
    ]


def stable_pool_can_settle(
    venue_order,
    usdc_principal,
    jupiter_output,
    stable_destination_pool,
    reserve=1,
):
    stable_amount = (
        float(usdc_principal)
        if venue_order == "stable_first"
        else float(jupiter_output)
    )
    capacity = max(0.0, float(stable_destination_pool) - float(reserve))
    return stable_amount <= capacity


def acquired_balance_delta(current_raw, baseline_raw):
    return max(0, int(current_raw) - int(baseline_raw))


def acquired_delta_is_cleared(current_raw, baseline_raw, tolerance_raw=100_000):
    return int(current_raw) <= int(baseline_raw) + int(tolerance_raw)


def drain_candidate_is_valid(
    pool_balance_raw,
    amount_raw,
    dust_raw=1,
    max_remainder_raw=1_000_000,
):
    """Return whether an exact-input swap safely leaves only drain dust."""
    pool = max(0, int(pool_balance_raw))
    amount = max(0, int(amount_raw))
    dust = max(0, int(dust_raw))
    max_remainder = max(dust, int(max_remainder_raw))
    remainder = pool - amount
    return amount > 0 and dust <= remainder <= max_remainder


def generate_drain_candidate_amounts_raw(
    pool_balance_raw,
    wallet_balance_raw,
    min_trade_size_raw,
    dust_raw=1,
    max_remainder_raw=1_000_000,
):
    """Return raw-unit sizes that leave a destination pool almost empty.

    The exact drain is preferred, with a short fallback ladder that leaves at
    most the configured remainder. If the wallet cannot bring the pool inside
    that remainder, no partial-drain candidate is returned.
    """
    pool = max(0, int(pool_balance_raw))
    wallet = max(0, int(wallet_balance_raw))
    minimum = max(1, int(min_trade_size_raw))
    dust = max(0, int(dust_raw))
    max_remainder = max(dust, int(max_remainder_raw))

    maximum_amount = min(wallet, pool - dust)
    if maximum_amount < minimum:
        return []
    if pool - maximum_amount > max_remainder:
        return []

    fallback_remainders = {dust, 10_000, 100_000, max_remainder}
    candidates = {maximum_amount}
    for remainder in fallback_remainders:
        if not dust <= remainder <= max_remainder:
            continue
        amount = pool - remainder
        if minimum <= amount <= wallet:
            candidates.add(amount)

    return sorted(
        amount
        for amount in candidates
        if drain_candidate_is_valid(
            pool,
            amount,
            dust_raw=dust,
            max_remainder_raw=max_remainder,
        )
    )


def generate_candidate_sizes(max_feasible, min_trade_size):
    """Return a bounded coarse grid of whole-token trade sizes."""
    maximum = int(math.floor(max_feasible))
    minimum = int(math.ceil(min_trade_size))
    if maximum < minimum or minimum <= 0:
        return []

    candidates = {
        minimum,
        minimum * 2,
        minimum * 5,
        maximum // 4,
        maximum // 2,
        (maximum * 3) // 4,
        maximum,
    }
    return sorted(size for size in candidates if minimum <= size <= maximum)


def generate_refinement_sizes(best_size, sampled_sizes, min_trade_size, max_feasible):
    """Return midpoint probes around the best coarse result.

    Jupiter routes and order-book levels are discontinuous, so midpoint probes
    are safer than assuming a smooth curve and applying binary/golden search.
    """
    minimum = int(math.ceil(min_trade_size))
    maximum = int(math.floor(max_feasible))
    sampled = sorted({int(size) for size in sampled_sizes if minimum <= int(size) <= maximum})
    best = int(best_size)
    if best not in sampled:
        sampled.append(best)
        sampled.sort()

    index = sampled.index(best)
    refinements = set()
    if index > 0:
        refinements.add((sampled[index - 1] + best) // 2)
    if index + 1 < len(sampled):
        refinements.add((best + sampled[index + 1]) // 2)
    if len(sampled) == 1:
        refinements.update({int(best * 0.8), int(best * 1.2)})

    return sorted(
        size
        for size in refinements
        if minimum <= size <= maximum and size not in sampled
    )


def calculate_quote_metrics(input_amount, output_amount, estimated_execution_cost_usd):
    input_value = float(input_amount)
    output_value = float(output_amount)
    cost = max(0.0, float(estimated_execution_cost_usd))
    gross_profit = output_value - input_value
    net_profit = gross_profit - cost
    net_return_bps = (net_profit / input_value * 10_000) if input_value > 0 else float("-inf")
    return {
        "input_amount": input_value,
        "output_amount": output_value,
        "gross_profit_usd": gross_profit,
        "estimated_execution_cost_usd": cost,
        "net_profit_usd": net_profit,
        "net_return_bps": net_return_bps,
    }


def is_profitable_candidate(metrics, min_net_profit_usd, min_net_return_bps=0.0):
    epsilon = 1e-9
    return (
        metrics["net_profit_usd"] + epsilon >= float(min_net_profit_usd)
        and metrics["net_return_bps"] + epsilon >= float(min_net_return_bps)
    )


def capital_efficiency_key(size, metrics):
    """Rank eligible candidates by net return, then dollars, then lower exposure."""
    return (
        metrics["net_return_bps"],
        metrics["net_profit_usd"],
        -float(size),
    )
