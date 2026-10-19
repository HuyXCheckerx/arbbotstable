//! Route search over direct pools and multi-hop paths (up to three hops), with gas netted out.
//!
//! Follows the Solana engine (`src/quote/engine.ts`), generalized from two hops:
//! 1. Direct pools are pruned by dominance (a pool whose best rate is below another pool's
//!    marginal rate at the full amount never carries input).
//! 2. Multi-hop candidates: for each intermediate (or ordered pair of intermediates), each hop's
//!    pools are ranked by simulated output at the full input and at one grid unit, each hop on the
//!    best previous hop's output. The top pools per hop are paired and simulated end to end, and
//!    the best few at each size join the direct routes.
//! 3. A grid DP over routes keeps the best path per (grid units, routes used) and never combines
//!    routes that share a pool, so no pool's liquidity counts twice.
//! 4. A hop-level local search groups legs by the token they spend. It moves `step` of a token
//!    between the pools spending it (the shortlist), and a leg's output change passes to the legs
//!    spending that output (one leg changes, or one opens when none exist), down to the output
//!    token. Each group's inputs therefore always sum exactly to the outputs feeding it, and each
//!    pool is used once in the plan.
//!
//! The objective is the output-token amount minus every leg's gas times `gas_cost` (output
//! atomic units per gas); a leg that is not used costs nothing.

use crate::engine::Sim;
use crate::optimizer::Params;
use parking_lot::Mutex;
use rayon::prelude::*;
use revm::primitives::{Address, HashMap};

#[derive(Clone, Debug)]
pub struct LegQ {
    pub pool: usize,
    pub tin: Address,
    pub tout: Address,
    pub amount: u128,
    pub out: u128,
    pub gas: u64,
}

#[derive(Clone, Debug)]
pub struct Route {
    pub hops: Vec<(usize, Address, Address)>,
}

impl Route {
    fn pools(&self) -> impl Iterator<Item = usize> + '_ {
        self.hops.iter().map(|h| h.0)
    }
    fn overlaps(&self, other: &Route) -> bool {
        self.pools().any(|p| other.pools().any(|q| q == p))
    }
    fn same(&self, other: &Route) -> bool {
        self.hops.len() == other.hops.len() && self.hops.iter().zip(&other.hops).all(|(a, b)| a.0 == b.0)
    }
}

#[derive(Clone, Debug)]
pub struct RouteQ {
    pub out: u128,
    pub gas: u64,
    pub legs: Vec<LegQ>,
}

#[derive(Clone, Debug, Default)]
pub struct SearchInfo {
    pub sims: usize,
    pub direct_pools: usize,
    pub pruned: usize,
    pub over_capacity: usize,
    pub multi_hop_candidates: usize,
    pub multi_hop_routes: usize,
    pub hop_moves: usize,
    pub hop_gain: i128,
    pub truncated: bool,
}

#[derive(Clone, Debug)]
pub struct Plan {
    /// Legs ordered by hop; `hop` counts from the input (1) and `via` is the intermediate a
    /// leg spends or (for a first hop) buys.
    pub legs: Vec<(LegQ, u8, Option<Address>)>,
    pub out: u128,
    pub gas: u64,
    pub net: i128,
    /// Best single route (direct pool or multi-hop path) at the full amount.
    pub best_single: Option<(Route, RouteQ)>,
    pub direct_singles: Vec<(usize, Option<Sim>)>,
    pub search: SearchInfo,
}

pub struct Ctx<'a> {
    sim: &'a (dyn Fn(usize, Address, Address, u128) -> Option<Sim> + Sync),
    cache: Mutex<HashMap<(usize, Address, u128), Option<Sim>>>,
    count: std::sync::atomic::AtomicUsize,
    gas_cost: f64,
    max_sims: usize,
}

impl Ctx<'_> {
    fn get(&self, pool: usize, tin: Address, tout: Address, amount: u128) -> Option<Sim> {
        if amount == 0 {
            return Some(Sim::default());
        }
        if let Some(v) = self.cache.lock().get(&(pool, tin, amount)) {
            return v.clone();
        }
        if self.exhausted() {
            return None;
        }
        self.count.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        let v = (self.sim)(pool, tin, tout, amount);
        self.cache.lock().insert((pool, tin, amount), v.clone());
        v
    }

    fn leg(&self, pool: usize, tin: Address, tout: Address, amount: u128) -> Option<LegQ> {
        if amount == 0 {
            return None;
        }
        self.get(pool, tin, tout, amount).map(|s| LegQ { pool, tin, tout, amount, out: s.out, gas: s.gas })
    }

    fn cost(&self, gas: u64) -> i128 {
        (gas as f64 * self.gas_cost).round() as i128
    }

    fn eval(&self, r: &Route, amount: u128) -> Option<RouteQ> {
        let mut a = amount;
        let mut legs = Vec::with_capacity(r.hops.len());
        let mut gas = 0;
        for &(p, tin, tout) in &r.hops {
            let l = self.leg(p, tin, tout, a)?;
            if l.out == 0 {
                return None;
            }
            a = l.out;
            gas += l.gas;
            legs.push(l);
        }
        Some(RouteQ { out: a, gas, legs })
    }

    fn value(&self, q: &RouteQ) -> i128 {
        q.out as i128 - self.cost(q.gas)
    }

    fn many(&self, reqs: &[(&Route, u128)]) {
        reqs.par_iter().for_each(|(r, a)| {
            let _ = self.eval(r, *a);
        });
    }

    fn exhausted(&self) -> bool {
        self.count.load(std::sync::atomic::Ordering::Relaxed) >= self.max_sims
    }
}

pub struct Graph<'a> {
    /// The tokens each pool trades.
    pub pools: &'a [Vec<Address>],
    pub input: Address,
    pub output: Address,
    pub via: &'a [Address],
    /// 1 = direct pools only, 2 = up to two hops, 3 = up to three.
    pub max_hops: usize,
}

impl Graph<'_> {
    fn between(&self, a: Address, b: Address) -> Vec<usize> {
        (0..self.pools.len()).filter(|&i| self.pools[i].contains(&a) && self.pools[i].contains(&b)).collect()
    }
}

/// Pools kept per hop when pairing hops, and paths kept per size.
const TOP2: usize = 3;
const TOP3: usize = 2;
const MAX_TWO_HOP: usize = 4;
const MAX_THREE_HOP: usize = 3;

/// Ranks the pools of each hop of `tokens` (input, mids..., output) at `size`, each hop on the
/// best previous hop's output, keeping `top` per hop; returns every pool combination.
fn paths_at(ctx: &Ctx, g: &Graph, tokens: &[Address], size: u128, top: usize, shortlist: &mut Vec<Vec<usize>>) -> Vec<Route> {
    let mut per_hop: Vec<Vec<usize>> = Vec::new();
    let mut lead = size;
    for (h, w) in tokens.windows(2).enumerate() {
        let pools = g.between(w[0], w[1]);
        let rs: Vec<Route> = pools.iter().map(|&i| Route { hops: vec![(i, w[0], w[1])] }).collect();
        ctx.many(&rs.iter().map(|r| (r, lead)).collect::<Vec<_>>());
        let mut ranked: Vec<(usize, u128)> = rs.iter().filter_map(|r| ctx.eval(r, lead).map(|q| (r.hops[0].0, q.out))).collect();
        ranked.sort_by(|a, b| b.1.cmp(&a.1));
        ranked.truncate(top);
        let Some(best) = ranked.first().map(|x| x.1) else { return vec![] };
        for (p, _) in &ranked {
            if !shortlist[h].contains(p) {
                shortlist[h].push(*p);
            }
        }
        per_hop.push(ranked.into_iter().map(|x| x.0).collect());
        lead = best;
    }
    let mut out: Vec<Vec<usize>> = vec![vec![]];
    for hop in &per_hop {
        let mut next = Vec::new();
        for prefix in &out {
            for p in hop {
                if !prefix.contains(p) {
                    next.push([prefix.clone(), vec![*p]].concat());
                }
            }
        }
        out = next;
    }
    out.into_iter()
        .map(|ps| Route { hops: ps.iter().enumerate().map(|(h, p)| (*p, tokens[h], tokens[h + 1])).collect() })
        .collect()
}

pub fn route(g: &Graph, amount: u128, p: &Params, sim: &(dyn Fn(usize, Address, Address, u128) -> Option<Sim> + Sync)) -> Option<Plan> {
    let ctx = Ctx { sim, cache: Mutex::new(HashMap::default()), count: Default::default(), gas_cost: p.gas_cost, max_sims: p.max_sims };
    let mut info = SearchInfo::default();
    let (input, output) = (g.input, g.output);
    let n = if amount >= 50_000 * 10u128.pow(6) {
        (p.steps * 2).min(40)
    } else {
        p.steps.max(1)
    };
    let unit = (amount / n as u128).max(1);
    let probe = (amount / 10_000).max(p.min_probe).min(amount / 10).max(1);
    let at = |k: usize| -> u128 { (amount * k as u128) / n as u128 };
    let rate = |r: &Route, size: u128| ctx.eval(r, size).map(|q| q.out as f64 / size as f64);

    // 1. Direct routes, probes and pruning.
    let direct_pools = g.between(input, output);
    info.direct_pools = direct_pools.len();
    let direct: Vec<Route> = direct_pools.iter().map(|&i| Route { hops: vec![(i, input, output)] }).collect();
    let mut reqs: Vec<(&Route, u128)> = direct.iter().map(|r| (r, amount)).collect();
    reqs.extend(direct.iter().map(|r| (r, probe)));
    ctx.many(&reqs);
    let direct_singles: Vec<(usize, Option<Sim>)> = direct_pools.iter().map(|&i| (i, ctx.get(i, input, output, amount))).collect();
    let mut live: Vec<Route> = direct.iter().filter(|r| rate(r, probe).is_some()).cloned().collect();
    let mut threshold: Option<f64> = None;
    if p.prune && live.len() > 1 && probe as f64 * 0.5e-4 >= 2.0 {
        let mut by_rate = live.clone();
        by_rate.sort_by(|a, b| rate(b, probe).partial_cmp(&rate(a, probe)).unwrap());
        let mut tops: Vec<Route> = by_rate.iter().take(3).cloned().collect();
        if let Some(best_full) = live.iter().filter(|r| ctx.eval(r, amount).is_some()).max_by_key(|r| ctx.eval(r, amount).unwrap().out) {
            if !tops.iter().any(|t| t.same(best_full)) {
                tops.push(best_full.clone());
            }
        }
        let d = (amount / 1000).max(1);
        ctx.many(&tops.iter().map(|r| (r, amount - d)).collect::<Vec<_>>());
        let marginal = tops
            .iter()
            .filter_map(|r| Some((ctx.eval(r, amount)?.out as f64 - ctx.eval(r, amount - d)?.out as f64) / d as f64))
            .fold(0.0f64, f64::max);
        if marginal > 0.0 {
            threshold = Some(marginal * (1.0 - 0.5e-4));
            let before = live.len();
            live.retain(|r| rate(r, probe).unwrap() >= threshold.unwrap());
            info.pruned = before - live.len();
        }
    }

    // 2. Multi-hop candidates. `cands` collects, per token, the shortlisted pools spending it.
    let mids: Vec<Address> = g.via.iter().copied().filter(|m| *m != input && *m != output).collect();
    let mut token_paths: Vec<Vec<Address>> = Vec::new();
    if g.max_hops >= 2 {
        for &m in &mids {
            token_paths.push(vec![input, m, output]);
        }
    }
    if g.max_hops >= 3 {
        for &m1 in &mids {
            for &m2 in &mids {
                if m1 != m2 {
                    token_paths.push(vec![input, m1, m2, output]);
                }
            }
        }
    }
    let mut cands: HashMap<Address, Vec<(usize, Address)>> = HashMap::default();
    for r in &live {
        cands.entry(input).or_default().push((r.hops[0].0, output));
    }
    let mut two: Vec<Route> = Vec::new();
    let mut three: Vec<Route> = Vec::new();
    for tokens in &token_paths {
        if tokens.windows(2).any(|w| g.between(w[0], w[1]).is_empty()) {
            continue;
        }
        // Bound by the product of each hop's best rate at the probe: a path that cannot beat the
        // best direct route's marginal rate is skipped.
        if let Some(th) = threshold {
            let mut lead = probe;
            let mut prod = 1.0;
            for w in tokens.windows(2) {
                let rs: Vec<Route> = g.between(w[0], w[1]).iter().map(|&i| Route { hops: vec![(i, w[0], w[1])] }).collect();
                ctx.many(&rs.iter().map(|r| (r, lead)).collect::<Vec<_>>());
                let best = rs.iter().filter_map(|r| ctx.eval(r, lead)).max_by_key(|q| q.out);
                match best {
                    Some(q) => {
                        prod *= q.out as f64 / lead as f64;
                        lead = q.out;
                    }
                    None => {
                        prod = 0.0;
                        break;
                    }
                }
            }
            if prod < th {
                continue;
            }
        }
        let top = if tokens.len() == 3 { TOP2 } else { TOP3 };
        let mut shortlist: Vec<Vec<usize>> = vec![vec![]; tokens.len() - 1];
        let mut found: Vec<Route> = Vec::new();
        for size in [amount, unit] {
            for r in paths_at(&ctx, g, tokens, size, top, &mut shortlist) {
                if !found.iter().any(|f| f.same(&r)) {
                    found.push(r);
                }
            }
        }
        for (h, list) in shortlist.iter().enumerate() {
            let e = cands.entry(tokens[h]).or_default();
            for &pool in list {
                if !e.iter().any(|(q, t)| *q == pool && *t == tokens[h + 1]) {
                    e.push((pool, tokens[h + 1]));
                }
            }
        }
        ctx.many(&found.iter().flat_map(|r| [(r, amount), (r, unit)]).collect::<Vec<_>>());
        info.multi_hop_candidates += found.len();
        if tokens.len() == 3 {
            two.extend(found);
        } else {
            three.extend(found);
        }
    }
    let top_by = |list: &[Route], size: u128, k: usize| -> Vec<Route> {
        let mut v: Vec<(Route, i128)> = list.iter().filter_map(|r| ctx.eval(r, size).map(|q| (r.clone(), ctx.value(&q)))).collect();
        v.sort_by(|a, b| b.1.cmp(&a.1));
        v.into_iter().take(k).map(|x| x.0).collect()
    };
    let mut selected: Vec<Route> = Vec::new();
    for r in top_by(&two, amount, MAX_TWO_HOP)
        .into_iter()
        .chain(top_by(&two, unit, MAX_TWO_HOP))
        .chain(top_by(&three, amount, MAX_THREE_HOP))
        .chain(top_by(&three, unit, MAX_THREE_HOP))
    {
        if !selected.iter().any(|s| s.same(&r)) {
            selected.push(r);
        }
    }
    info.multi_hop_routes = selected.len();
    let routes: Vec<Route> = live.iter().cloned().chain(selected.iter().cloned()).collect();
    if routes.is_empty() {
        info.sims = ctx.count.load(std::sync::atomic::Ordering::Relaxed);
        return None;
    }
    let best_single = routes.iter().filter_map(|r| ctx.eval(r, amount).map(|q| (r.clone(), q))).max_by_key(|(_, q)| ctx.value(q));

    // 3. Grid DP over routes. Samples rise per route and stop at the first failure or once a
    // step's marginal rate falls below `cutoff` of the route's best rate.
    let samples: Vec<Vec<Option<RouteQ>>> = routes
        .par_iter()
        .map(|r| {
            let r0 = rate(r, probe).or_else(|| rate(r, unit)).unwrap_or(0.0);
            let mut v: Vec<Option<RouteQ>> = vec![None; n + 1];
            let mut prev = 0u128;
            for k in 1..=n {
                let Some(q) = ctx.eval(r, at(k)) else { break };
                let marg = q.out.saturating_sub(prev) as f64 / (at(k) - at(k - 1)) as f64;
                prev = q.out;
                v[k] = Some(q);
                if marg < p.cutoff * r0 {
                    break;
                }
            }
            v
        })
        .collect();
    info.over_capacity = samples.iter().filter(|v| v[n].is_none()).count();
    #[derive(Clone)]
    struct Path {
        value: i128,
        alloc: Vec<(usize, usize)>,
    }
    let s_max = p.max_splits.max(1);
    let mut paths: HashMap<(usize, usize), Path> = HashMap::default();
    paths.insert((0, 0), Path { value: 0, alloc: vec![] });
    for (ri, r) in routes.iter().enumerate() {
        let mut next = paths.clone();
        for ((m, s), path) in &paths {
            if *s >= s_max || path.alloc.iter().any(|(rj, _)| routes[*rj].overlaps(r)) {
                continue;
            }
            for k in 1..=(n - m) {
                let Some(q) = &samples[ri][k] else { break };
                let cand = Path { value: path.value + ctx.value(q), alloc: [path.alloc.clone(), vec![(ri, k)]].concat() };
                let key = (m + k, s + 1);
                if next.get(&key).map(|b| cand.value > b.value).unwrap_or(true) {
                    next.insert(key, cand);
                }
            }
        }
        paths = next;
    }
    let best = (1..=s_max).filter_map(|s| paths.get(&(n, s))).max_by_key(|p| p.value)?.clone();
    let mut alloc: Vec<(usize, u128)> = best.alloc.iter().map(|(ri, k)| (*ri, at(*k))).collect();
    let total: u128 = alloc.iter().map(|a| a.1).sum();
    if total < amount {
        let i = (0..alloc.len()).max_by_key(|&i| alloc[i].1).unwrap();
        alloc[i].1 += amount - total;
    }

    // Flow: legs grouped by the token they spend.
    let mut groups: Groups = HashMap::default();
    let add = |groups: &mut Groups, q: &RouteQ| {
        for l in &q.legs {
            groups.entry(l.tin).or_default().insert(l.pool, l.clone());
        }
    };
    let mut ok = true;
    for (ri, a) in &alloc {
        match ctx.eval(&routes[*ri], *a) {
            Some(q) => add(&mut groups, &q),
            None => {
                ok = false;
                break;
            }
        }
    }
    if !ok {
        // The remainder pushed a route past capacity: fall back to the best single route.
        groups.clear();
        add(&mut groups, &best_single.clone()?.1);
    }

    // 4. Hop-level local search.
    let flow = Flow { ctx: &ctx, output, cands: &cands, max_splits: p.max_splits };
    let start = flow.value(&groups);
    let floor = ((amount * p.precision_ppm as u128) / 1_000_000).max(1);
    let mut step = unit;
    let mut moves = 0usize;
    while step >= floor && moves < 1000 && !ctx.exhausted() && p.max_splits > 1 {
        let mut best_move: Option<(i128, Changes)> = None;
        let tokens: Vec<Address> = groups.iter().filter(|(_, g)| !g.is_empty()).map(|(t, _)| *t).collect();
        for t in tokens {
            // The amount of `t` that `step` of input currently buys.
            let in_t: u128 = groups[&t].values().map(|l| l.amount).sum();
            let size = if t == input { step } else { step * in_t / amount };
            if size == 0 {
                continue;
            }
            if let Some(m) = flow.shift(&groups, t, size) {
                if best_move.as_ref().map(|b| m.0 > b.0).unwrap_or(true) {
                    best_move = Some(m);
                }
            }
        }
        match best_move {
            Some((_, changes)) => {
                apply(&mut groups, changes);
                moves += 1;
            }
            None => step /= 2,
        }
    }
    info.hop_moves = moves;
    let end = flow.value(&groups);
    info.hop_gain = end - start;
    info.truncated = ctx.exhausted();
    info.sims = ctx.count.load(std::sync::atomic::Ordering::Relaxed);

    // Plan, ordered by hop depth from the input.
    let mut depth: HashMap<Address, u8> = HashMap::default();
    depth.insert(input, 1);
    let mut frontier = vec![input];
    while let Some(t) = frontier.pop() {
        let d = depth[&t];
        for l in groups.get(&t).map(|g| g.values().cloned().collect::<Vec<_>>()).unwrap_or_default() {
            if l.tout != output && depth.get(&l.tout).map(|x| *x > d + 1).unwrap_or(true) {
                depth.insert(l.tout, d + 1);
                frontier.push(l.tout);
            }
        }
    }
    let mut legs: Vec<(LegQ, u8, Option<Address>)> = groups
        .values()
        .flat_map(|g| g.values().cloned())
        .map(|l| {
            let hop = depth.get(&l.tin).copied().unwrap_or(1);
            let via = if hop == 1 { (l.tout != output).then_some(l.tout) } else { Some(l.tin) };
            (l, hop, via)
        })
        .collect();
    legs.sort_by(|a, b| a.1.cmp(&b.1).then(b.0.amount.cmp(&a.0.amount)));
    let out = legs.iter().filter(|(l, _, _)| l.tout == output).map(|(l, _, _)| l.out).sum();
    let gas = legs.iter().map(|(l, _, _)| l.gas).sum();
    Some(Plan { legs, out, gas, net: end, best_single, direct_singles, search: info })
}

type Groups = HashMap<Address, HashMap<usize, LegQ>>;
/// (token group, pool, new leg or None to close).
type Changes = Vec<(Address, usize, Option<LegQ>)>;

fn apply(groups: &mut Groups, changes: Changes) {
    for (t, pool, leg) in changes {
        let g = groups.entry(t).or_default();
        match leg {
            Some(l) => {
                g.insert(pool, l);
            }
            None => {
                g.remove(&pool);
            }
        }
    }
}

struct Flow<'a, 'b> {
    ctx: &'a Ctx<'b>,
    output: Address,
    cands: &'a HashMap<Address, Vec<(usize, Address)>>,
    max_splits: usize,
}

impl Flow<'_, '_> {
    fn value(&self, groups: &Groups) -> i128 {
        groups
            .values()
            .flat_map(|g| g.values())
            .map(|l| if l.tout == self.output { l.out as i128 } else { 0 } - self.ctx.cost(l.gas))
            .sum()
    }

    fn used(&self, groups: &Groups, pool: usize, except: Address) -> bool {
        groups.iter().any(|(t, g)| *t != except && g.contains_key(&pool))
    }

    /// Gain of replacing `before` by `after` in group `t`, with the output change settled
    /// downstream; None when downstream cannot absorb it.
    fn settle(&self, groups: &Groups, before: Option<&LegQ>, after: Option<&LegQ>, depth: usize) -> Option<(i128, Changes)> {
        let leg = after.or(before)?;
        let mut gain = self.ctx.cost(before.map(|l| l.gas).unwrap_or(0)) - self.ctx.cost(after.map(|l| l.gas).unwrap_or(0));
        let d_out = after.map(|l| l.out as i128).unwrap_or(0) - before.map(|l| l.out as i128).unwrap_or(0);
        let mut changes: Changes = vec![(leg.tin, leg.pool, after.cloned())];
        if leg.tout == self.output {
            gain += d_out;
        } else if d_out != 0 {
            let (g2, ch2) = self.absorb(groups, leg.tout, d_out, depth + 1, Some(leg.pool))?;
            gain += g2;
            changes.extend(ch2);
        }
        Some((gain, changes))
    }

    /// The best way for the legs spending `t` to take `delta` more (or less) of it: one existing
    /// leg changes, or with none a shortlisted pool opens; a total of zero closes them all.
    fn absorb(&self, groups: &Groups, t: Address, delta: i128, depth: usize, exclude: Option<usize>) -> Option<(i128, Changes)> {
        if depth > 4 {
            return None;
        }
        let empty = HashMap::default();
        let legs = groups.get(&t).unwrap_or(&empty);
        let total: i128 = legs.values().map(|l| l.amount as i128).sum::<i128>() + delta;
        if total < 0 {
            return None;
        }
        if total == 0 {
            let mut gain = 0;
            let mut changes = Changes::new();
            for l in legs.values() {
                let (g, ch) = self.settle(groups, Some(l), None, depth)?;
                gain += g;
                changes.extend(ch);
            }
            return Some((gain, changes));
        }
        let options: Vec<(usize, Address)> = if legs.is_empty() {
            self.cands.get(&t).cloned().unwrap_or_default()
        } else {
            legs.values().map(|l| (l.pool, l.tout)).collect()
        };
        let mut best: Option<(i128, Changes)> = None;
        for (pool, tout) in options {
            if Some(pool) == exclude || (legs.is_empty() && self.used(groups, pool, t)) {
                continue;
            }
            let cur = legs.get(&pool);
            let amt = cur.map(|l| l.amount as i128).unwrap_or(0) + delta;
            if amt < 0 {
                continue;
            }
            let next = if amt > 0 {
                match self.ctx.leg(pool, t, tout, amt as u128) {
                    Some(l) => Some(l),
                    None => continue,
                }
            } else {
                None
            };
            let Some(s) = self.settle(groups, cur, next.as_ref(), depth) else { continue };
            if best.as_ref().map(|b| s.0 > b.0).unwrap_or(true) {
                best = Some(s);
            }
        }
        best
    }

    /// The best move of up to `size` of `t` from one leg spending it to another shortlisted pool.
    fn shift(&self, groups: &Groups, t: Address, size: u128) -> Option<(i128, Changes)> {
        let legs = groups.get(&t)?;
        let cands = self.cands.get(&t)?;
        let mut best: Option<(i128, Changes)> = None;
        for from in legs.values() {
            let moved = from.amount.min(size);
            let rest = from.amount - moved;
            let rest_leg = if rest > 0 {
                match self.ctx.leg(from.pool, t, from.tout, rest) {
                    Some(l) => Some(l),
                    None => continue,
                }
            } else {
                None
            };
            for &(pool, tout) in cands {
                if pool == from.pool {
                    continue;
                }
                let to = legs.get(&pool);
                if let Some(to) = to {
                    if to.tout != tout {
                        continue;
                    }
                }
                if to.is_none() && (self.used(groups, pool, t) || (rest > 0 && legs.len() >= self.max_splits)) {
                    continue;
                }
                let Some(to_leg) = self.ctx.leg(pool, t, tout, to.map(|l| l.amount).unwrap_or(0) + moved) else { continue };
                // Settle `from` first; then settle `to` against the groups as `from` left them.
                let Some((g1, ch1)) = self.settle(groups, Some(from), rest_leg.as_ref(), 0) else { continue };
                let mut after = groups.clone();
                apply(&mut after, ch1.clone());
                let to_now = after.get(&t).and_then(|g| g.get(&pool)).cloned();
                if to_now.as_ref().map(|l| l.amount) != to.map(|l| l.amount) {
                    continue;
                }
                let Some((g2, ch2)) = self.settle(&after, to, Some(&to_leg), 0) else { continue };
                let gain = g1 + g2;
                if gain > 0 && best.as_ref().map(|b| gain > b.0).unwrap_or(true) {
                    best = Some((gain, [ch1, ch2].concat()));
                }
            }
        }
        best
    }
}
