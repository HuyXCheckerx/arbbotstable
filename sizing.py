import math
import re


def usdc_strategy_directions(tokens=("USDG", "PYUSD"), jupiter_first_only=("USDT",)):
    """Return enabled USDC cycles, including one-way Jupiter-first tokens."""
    directions = [
        (token, venue_order)
        for venue_order in ("stable_first", "jupiter_first")
        for token in tokens
    ]
    directions.extend((token, "jupiter_first") for token in jupiter_first_only)
    return directions


def stable_swap_output_amount(asset_from, asset_to, input_amount, usdt_fee_bps=10):
    """Return Stable.com's fee-adjusted output; USDT/USDC costs 10 bps."""
    amount = float(input_amount)
    if {str(asset_from).upper(), str(asset_to).upper()} == {"USDC", "USDT"}:
        input_raw = int(round(amount * 1_000_000))
        return stable_swap_output_raw(
            asset_from, asset_to, input_raw, usdt_fee_bps
        ) / 1_000_000
    return amount


def stable_swap_output_raw(asset_from, asset_to, input_raw, usdt_fee_bps=10):
    """Integer version of ``stable_swap_output_amount``, rounded down."""
    raw = int(input_raw)
    if {str(asset_from).upper(), str(asset_to).upper()} == {"USDC", "USDT"}:
        return raw * (10_000 - int(usdt_fee_bps)) // 10_000
    return raw


def reserve_adjusted_min_profit(
    token,
    reserve_amount,
    normal_min_profit=0.10,
    low_reserve_min_profit=0.05,
    usdg_threshold=7_500.0,
    pyusd_threshold=0.10,
):
    """Lower the route floor while its Stable.com token reserve is scarce."""
    thresholds = {"USDG": float(usdg_threshold), "PYUSD": float(pyusd_threshold)}
    threshold = thresholds.get(str(token).upper())
    if threshold is not None and float(reserve_amount) < threshold:
        return min(float(normal_min_profit), float(low_reserve_min_profit))
    return float(normal_min_profit)


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

    fallback_remainders = {dust, max_remainder}
    if max_remainder > dust:
        fallback_remainders.add((dust + max_remainder) // 2)
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


def maximum_safe_stable_input_raw(wallet_balance_raw, pool_balance_raw, reserve_raw):
    """Maximum exact input bounded by wallet funds and protected pool reserve."""

    wallet = max(0, int(wallet_balance_raw))
    pool_capacity = max(0, int(pool_balance_raw) - max(0, int(reserve_raw)))
    return min(wallet, pool_capacity)


def normalize_drain_window_raw(
    requested_min_raw,
    requested_max_raw,
    protocol_floor_raw=1_800_000,
    safety_buffer_raw=100_000,
    refill_trigger_raw=2_000_000,
):
    """Return a safe reserve window bounded by protocol and refill limits."""
    protocol_floor = max(0, int(protocol_floor_raw))
    safety_buffer = max(0, int(safety_buffer_raw))
    refill_trigger = max(0, int(refill_trigger_raw))
    maximum_below_refill = refill_trigger - 1
    safe_floor = protocol_floor + safety_buffer

    if safe_floor > maximum_below_refill:
        raise ValueError("reserve floor and safety buffer leave no drain window")

    minimum = max(int(requested_min_raw), safe_floor)
    if minimum > maximum_below_refill:
        raise ValueError("minimum drain remainder must stay below the refill trigger")

    maximum = min(
        maximum_below_refill,
        max(minimum, int(requested_max_raw)),
    )
    return minimum, maximum


def parse_stable_reserve_constraint(payload):
    """Extract Stable.com's raw pool-reserve values from an error payload."""
    if not isinstance(payload, dict):
        return None
    details = payload.get("details")
    if not isinstance(details, dict):
        return None
    reserve_error = details.get("insufficient_pool_balance")
    if not reserve_error:
        return None

    values = {
        name: int(value)
        for name, value in re.findall(
            r"(remainingAfterOperation|thresholdYMinusZ|thresholdY|thresholdZ)=(\d+)",
            str(reserve_error),
        )
    }
    required_raw = values.get("thresholdYMinusZ")
    if required_raw is None:
        threshold_y = values.get("thresholdY")
        threshold_z = values.get("thresholdZ")
        if threshold_y is not None and threshold_z is not None:
            required_raw = threshold_y - threshold_z

    if required_raw is None or required_raw < 0:
        return None
    return {
        "remaining_raw": values.get("remainingAfterOperation"),
        "required_raw": required_raw,
        "threshold_y_raw": values.get("thresholdY"),
        "threshold_z_raw": values.get("thresholdZ"),
    }


def parse_stable_liquidity_constraint(payload):
    """Extract Stable.com's backend view of requested and available liquidity."""
    if not isinstance(payload, dict):
        return None
    details = payload.get("details")
    if not isinstance(details, dict):
        return None
    liquidity_error = details.get("insufficient_pool_balance")
    if not liquidity_error:
        return None

    values = {
        name: int(value)
        for name, value in re.findall(
            r"\b(amount|available)=(\d+)",
            str(liquidity_error),
        )
    }
    amount_raw = values.get("amount")
    available_raw = values.get("available")
    if amount_raw is None or available_raw is None or amount_raw <= available_raw:
        return None
    return {
        "amount_raw": amount_raw,
        "available_raw": available_raw,
    }


def adjusted_drain_minimum_raw(
    current_min_raw,
    max_remainder_raw,
    reserve_constraint,
    safety_buffer_raw=100_000,
    checked_remainder_raw=None,
):
    """Raise a drain floor after a reserve rejection, or return None if impossible."""
    if not isinstance(reserve_constraint, dict):
        return int(current_min_raw)

    required_raw = reserve_constraint.get("required_raw")
    if required_raw is None:
        return int(current_min_raw)

    current_min = max(0, int(current_min_raw))
    maximum = max(0, int(max_remainder_raw))
    required = max(0, int(required_raw))
    safety_buffer = max(0, int(safety_buffer_raw))
    adjusted_min = max(current_min, required + safety_buffer)

    remaining_raw = reserve_constraint.get("remaining_raw")
    if remaining_raw is not None and checked_remainder_raw is not None:
        observed_depletion = max(
            0,
            int(checked_remainder_raw) - int(remaining_raw),
        )
        adjusted_min = max(
            adjusted_min,
            required + max(safety_buffer, observed_depletion),
        )

    return adjusted_min if adjusted_min <= maximum else None


def generate_candidate_sizes(max_feasible, min_trade_size):
    """Return a low-request coarse grid with small anchors and exact maximum."""
    maximum = int(math.floor(max_feasible))
    minimum = int(math.ceil(min_trade_size))
    if maximum < minimum or minimum <= 0:
        return []

    candidates = {
        minimum,
        minimum * 2,
        minimum * 5,
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


def calculate_quote_metrics(
    input_amount,
    output_amount,
    estimated_execution_cost_usd,
    output_value_amount=None,
):
    input_value = float(input_amount)
    output_value = float(
        output_amount if output_value_amount is None else output_value_amount
    )
    cost = max(0.0, float(estimated_execution_cost_usd))
    gross_profit = output_value - input_value
    net_profit = gross_profit - cost
    net_return_bps = (net_profit / input_value * 10_000) if input_value > 0 else float("-inf")
    return {
        "input_amount": input_value,
        "output_amount": float(output_amount),
        "output_value_amount": output_value,
        "gross_profit_usd": gross_profit,
        "estimated_execution_cost_usd": cost,
        "net_profit_usd": net_profit,
        "net_return_bps": net_return_bps,
    }


def calculate_route_metrics(input_amount, jupiter_output, execution_cost, token, venue_order):
    """Value a Jupiter quote at the amount the Stable.com exit will return."""
    output_value = float(jupiter_output)
    if venue_order == "jupiter_first":
        output_value = stable_swap_output_amount(token, "USDC", jupiter_output)
    return calculate_quote_metrics(
        input_amount,
        jupiter_output,
        execution_cost,
        output_value_amount=output_value,
    )


def is_profitable_candidate(metrics, min_net_profit_usd, min_net_return_bps=0.0):
    epsilon = 1e-9
    return (
        metrics["net_profit_usd"] + epsilon >= float(min_net_profit_usd)
        and metrics["net_return_bps"] + epsilon >= float(min_net_return_bps)
    )


def absolute_profit_key(size, metrics):
    """Rank by six-decimal net dollars, then lower exposure on a tie."""
    return (
        round(float(metrics["net_profit_usd"]), 6),
        -float(size),
    )
