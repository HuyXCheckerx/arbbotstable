import math


def calculate_refill_aware_min_size(
    pool_balance,
    min_trade_size,
    refill_threshold,
    refill_buffer=1,
):
    """Return the minimum trade needed to leave a pool below its refill trigger."""
    minimum = int(math.ceil(min_trade_size))
    threshold = float(refill_threshold)
    buffer = max(0, int(math.ceil(refill_buffer)))
    if threshold <= 0:
        return minimum

    required_to_trigger_refill = math.floor(float(pool_balance) - threshold) + buffer
    return max(minimum, required_to_trigger_refill)


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
