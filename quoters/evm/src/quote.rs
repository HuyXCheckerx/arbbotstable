//! Setup (discovery, mirror, helper) and one exact-input quote.

use crate::chains::{Chain, Token};
use crate::engine::{Engine, ReadFailed};
use crate::mirror::Mirror;
use crate::optimizer::Params;
use crate::router::{route, Graph};
use revm::primitives::Address;
use crate::rpc::Rpc;
use crate::venues::{discover, Discovery};
use anyhow::{bail, Result};
use serde::Serialize;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::{Instant, SystemTime};

pub struct Setup {
    pub engine: Engine,
    pub discovery: Discovery,
    pub warnings: Vec<String>,
    pub setup_ms: u128,
}

fn cache_path(chain: &Chain) -> PathBuf {
    PathBuf::from(format!("artifacts/pools-{}.json", chain.key))
}

/// Loads the pool list (cached for a day unless `rediscover`), pins the mirror to the latest
/// block, funds the helper and runs each pool's approvals.
pub fn setup(chain: &'static Chain, rpc_url: Option<&str>, rediscover: bool) -> Result<Setup> {
    let t0 = Instant::now();
    let rpc = Rpc::new(rpc_url.unwrap_or(chain.rpc_http));
    let path = cache_path(chain);
    let fresh = std::fs::metadata(&path)
        .and_then(|m| m.modified())
        .map(|t| SystemTime::now().duration_since(t).unwrap_or_default().as_secs() < 86_400)
        .unwrap_or(false);
    // A cache written before pools carried their token lists (or for other tokens) is stale.
    let cached: Option<Discovery> = if fresh && !rediscover {
        serde_json::from_str::<Discovery>(&std::fs::read_to_string(&path)?).ok().filter(|d| {
            !d.pools.is_empty()
                && d.pools.iter().all(|p| !p.tokens.is_empty())
                && chain.tokens.iter().all(|t| d.tokens.contains(&t.address))
        })
    } else {
        None
    };
    let discovery: Discovery = if let Some(d) = cached {
        d
    } else {
        let d = discover(chain, &rpc)?;
        std::fs::create_dir_all("artifacts")?;
        std::fs::write(&path, serde_json::to_string_pretty(&d)?)?;
        d
    };
    let mirror = Arc::new(Mirror::new(rpc));
    mirror.init()?;
    let mut warnings = discovery.warnings.clone();
    let mut engine = Engine::new(chain, mirror, Vec::new())?;
    let mut pools = Vec::new();
    for p in &discovery.pools {
        match engine.prepare(p) {
            Ok(()) => pools.push(p.clone()),
            Err(e) => warnings.push(format!("{}: {e}", p.short())),
        }
    }
    engine.pools = pools;
    engine.resolve_fluid();
    Ok(Setup { engine, discovery, warnings, setup_ms: t0.elapsed().as_millis() })
}

#[derive(Serialize, Clone, Debug)]
pub struct LegOut {
    pub venue: String,
    pub label: String,
    pub pool: String,
    pub share_bps: u64,
    pub amount_in: String,
    pub amount_out: String,
    pub gas: u64,
    pub token_in: String,
    pub token_out: String,
    /// 1 for direct legs and first hops, 2 for second hops.
    pub hop: u8,
    /// The intermediate of a two-hop leg.
    pub via: Option<String>,
}

#[derive(Serialize, Clone, Debug)]
pub struct SingleOut {
    pub label: String,
    pub pool: String,
    pub amount_out: Option<String>,
    pub gas: Option<u64>,
}

#[derive(Serialize, Clone, Debug)]
pub struct QuoteOut {
    pub chain: String,
    pub block: u64,
    pub input: String,
    pub output: String,
    pub amount_in: String,
    pub in_atomic: String,
    pub out_atomic: String,
    pub amount_out: String,
    /// Output minus the gas of the legs and of the route's base cost, in output tokens.
    pub net_out: String,
    pub net_atomic: String,
    pub leg_gas: u64,
    pub base_gas: u64,
    pub gas_price_gwei: f64,
    pub native_price: f64,
    pub gas_cost_out: String,
    pub price_impact_bps: f64,
    pub legs: Vec<LegOut>,
    pub best_single: Option<SingleOut>,
    pub singles: Vec<SingleOut>,
    pub via: Vec<String>,
    /// Direct pools for the pair.
    pub pools_considered: usize,
    pub multi_hop_candidates: usize,
    pub multi_hop_routes: usize,
    pub hop_moves: usize,
    pub pruned: usize,
    pub over_capacity: usize,
    pub simulations: usize,
    pub truncated: bool,
    pub search_ms: u128,
    pub total_ms: u128,
}

/// Parses a decimal amount ("100000", "2.5") into atomic units exactly.
pub fn parse_amount(s: &str, dec: u8) -> Result<u128> {
    let (i, f) = s.split_once('.').unwrap_or((s, ""));
    if f.len() > dec as usize {
        bail!("{s} has more than {dec} decimals");
    }
    let i: u128 = if i.is_empty() { 0 } else { i.replace('_', "").parse()? };
    let f: u128 = if f.is_empty() { 0 } else { format!("{f:0<width$}", width = dec as usize).parse()? };
    Ok(i * 10u128.pow(dec as u32) + f)
}

pub fn human(atomic: u128, dec: u8) -> String {
    let s = format!("{:0>width$}", atomic, width = dec as usize + 1);
    let (i, f) = s.split_at(s.len() - dec as usize);
    let f = &f[..f.len().min(6)];
    format!("{i}.{f}")
}

pub fn to_f64(atomic: i128, dec: u8) -> f64 {
    atomic as f64 / 10f64.powi(dec as i32)
}

pub struct Market {
    pub gas_price: u128,
    pub native_in_out: f64,
}

/// Gas price and native price, in output-token atomic units per native token.
pub fn market(engine: &Engine, out: Token) -> Result<Market> {
    Ok(Market { gas_price: engine.gas_price()?, native_in_out: engine.native_price(out)? })
}

pub fn quote(engine: &Engine, tin: Token, tout: Token, amount: u128, params: &Params, m: &Market) -> Result<QuoteOut> {
    let t0 = Instant::now();
    let gas_cost = m.gas_price as f64 * m.native_in_out / 1e18;
    let mut p = params.clone();
    p.gas_cost = if p.gas_cost >= 0.0 { gas_cost } else { 0.0 };
    p.min_probe = 10u128.pow(tin.decimals as u32) / 10;
    let pools = &engine.pools;
    let chain = engine.chain;
    let via_syms: Vec<String> = p.via.clone().unwrap_or_else(|| chain.via.iter().map(|s| s.to_string()).collect());
    let via: Vec<Address> = via_syms
        .iter()
        .filter_map(|s| chain.token(s).map(|t| t.address))
        .filter(|a| *a != tin.address && *a != tout.address)
        .collect();
    let token_lists: Vec<Vec<Address>> = pools.iter().map(|p| p.tokens.clone()).collect();
    let graph = Graph { pools: &token_lists, input: tin.address, output: tout.address, via: &via, max_hops: if via.is_empty() { 1 } else { p.max_hops } };
    // A pool whose state could not be read is not a pool that cannot swap: the quote fails
    // instead of routing around it.
    let read_failed: parking_lot::Mutex<Option<anyhow::Error>> = parking_lot::Mutex::new(None);
    let sim = |i: usize, a: Address, b: Address, amt: u128| match engine.simulate(&pools[i], a, b, amt) {
        Ok(s) => Some(s),
        Err(e) => {
            if e.downcast_ref::<ReadFailed>().is_some() {
                read_failed.lock().get_or_insert(e);
            }
            None
        }
    };
    let ts = Instant::now();
    // Concentrated-liquidity swaps stop 1% (sqrt price) from market. If the pools cannot absorb
    // the amount within that, retry with 10% (no marginal-rate cutoff), and with `full_fill`
    // with no limit at all, which walks every initialized tick.
    let base = engine.limit();
    let mut plan = None;
    let mut tries = vec![(base, p.cutoff), (1000, 0.0)];
    if p.full_fill {
        tries.push((0, 0.0));
    }
    for (lim, cutoff) in tries {
        engine.limit_bps.store(lim, std::sync::atomic::Ordering::Relaxed);
        let mut pp = p.clone();
        pp.cutoff = cutoff;
        plan = route(&graph, amount, &pp, &sim);
        if plan.is_some() {
            break;
        }
    }
    engine.limit_bps.store(base, std::sync::atomic::Ordering::Relaxed);
    if let Some(e) = read_failed.into_inner() {
        return Err(e);
    }
    let Some(plan) = plan else {
        bail!(
            "no route absorbs {} {} within a ~20% price move (--full-fill quotes it anyway)",
            human(amount, tin.decimals),
            tin.symbol
        );
    };
    let search_ms = ts.elapsed().as_millis();
    let base_gas = chain.base_gas;
    let gas_out = ((plan.gas + base_gas) as f64 * gas_cost).round() as i128;
    let net = plan.out as i128 - gas_out;
    let dec = |a: Address| chain.token_by_address(a).map(|t| t.decimals).unwrap_or(18);
    let symb = |a: Address| chain.token_by_address(a).map(|t| t.symbol.to_string()).unwrap_or_else(|| format!("{a:?}"));
    // A second-hop leg's share is of its intermediate's amount; others' of the input.
    // A leg's share is of everything spent of its input token (the input, or an intermediate).
    let group_total = |t: Address| -> u128 { plan.legs.iter().filter(|(l, _, _)| l.tin == t).map(|(l, _, _)| l.amount).sum() };
    let legs: Vec<LegOut> = plan
        .legs
        .iter()
        .map(|(l, hop, mid)| {
            let pool = &pools[l.pool];
            let base_amt = if *hop >= 2 { group_total(l.tin).max(1) } else { amount };
            LegOut {
                venue: pool.venue.clone(),
                label: pool.label.clone(),
                pool: pool.id.clone(),
                share_bps: ((l.amount * 10_000) / base_amt) as u64,
                amount_in: human(l.amount, dec(l.tin)),
                amount_out: human(l.out, dec(l.tout)),
                gas: l.gas,
                token_in: symb(l.tin),
                token_out: symb(l.tout),
                hop: *hop,
                via: mid.map(symb),
            }
        })
        .collect();
    let singles: Vec<SingleOut> = plan
        .direct_singles
        .iter()
        .map(|(i, s)| SingleOut {
            label: pools[*i].label.clone(),
            pool: pools[*i].id.clone(),
            amount_out: s.as_ref().map(|s| human(s.out, tout.decimals)),
            gas: s.as_ref().map(|s| s.gas),
        })
        .collect();
    let best_single = plan.best_single.as_ref().map(|(r, q)| SingleOut {
        label: r.hops.iter().map(|h| pools[h.0].label.clone()).collect::<Vec<_>>().join(" -> "),
        pool: r.hops.iter().map(|h| pools[h.0].id[..10.min(pools[h.0].id.len())].to_string()).collect::<Vec<_>>().join(" -> "),
        amount_out: Some(human(q.out, tout.decimals)),
        gas: Some(q.gas),
    });
    // Price impact against the best rate of the best single route for a small probe.
    let probe = (amount / 10_000).max(p.min_probe).min(amount / 10).max(1);
    let best_rate = plan
        .best_single
        .as_ref()
        .and_then(|(r, _)| {
            let mut a = probe;
            for h in &r.hops {
                a = engine.simulate(&pools[h.0], h.1, h.2, a).ok()?.out;
            }
            Some(a as f64 / probe as f64)
        })
        .unwrap_or(0.0)
        .max(
            plan.direct_singles
                .iter()
                .filter_map(|(i, _)| engine.simulate(&pools[*i], tin.address, tout.address, probe).ok().map(|s| s.out as f64 / probe as f64))
                .fold(0.0, f64::max),
        );
    let rate = plan.out as f64 / amount as f64;
    let impact = if best_rate > 0.0 { (1.0 - rate / best_rate) * 1e4 } else { 0.0 };
    Ok(QuoteOut {
        chain: chain.key.into(),
        block: engine.mirror.block(),
        input: tin.symbol.into(),
        output: tout.symbol.into(),
        amount_in: human(amount, tin.decimals),
        in_atomic: amount.to_string(),
        out_atomic: plan.out.to_string(),
        amount_out: human(plan.out, tout.decimals),
        net_out: format!("{:.6}", to_f64(net, tout.decimals)),
        net_atomic: net.to_string(),
        leg_gas: plan.gas,
        base_gas,
        gas_price_gwei: m.gas_price as f64 / 1e9,
        native_price: m.native_in_out / 10f64.powi(tout.decimals as i32),
        gas_cost_out: format!("{:.6}", to_f64(gas_out, tout.decimals)),
        price_impact_bps: impact,
        legs,
        best_single,
        singles,
        via: via.iter().map(|a| symb(*a)).collect(),
        pools_considered: plan.search.direct_pools,
        pruned: plan.search.pruned,
        over_capacity: plan.search.over_capacity,
        multi_hop_candidates: plan.search.multi_hop_candidates,
        multi_hop_routes: plan.search.multi_hop_routes,
        hop_moves: plan.search.hop_moves,
        simulations: plan.search.sims,
        truncated: plan.search.truncated,
        search_ms,
        total_ms: t0.elapsed().as_millis(),
    })
}

pub fn print_quote(q: &QuoteOut) {
    println!(
        "{} block {}  gas {:.3} gwei  native ${:.2}",
        q.chain, q.block, q.gas_price_gwei, q.native_price
    );
    println!("{} {} -> {} {}", q.amount_in, q.input, q.amount_out, q.output);
    println!(
        "  net of gas: {} {} (gas {} legs + {} base = {} {})  impact {:.2} bp",
        q.net_out,
        q.output,
        q.leg_gas,
        q.base_gas,
        q.gas_cost_out,
        q.output,
        q.price_impact_bps
    );
    for l in &q.legs {
        let path = match (&l.via, l.hop) {
            (None, _) => "direct  ".to_string(),
            (Some(v), h) => format!("{v:>6} {h}"),
        };
        println!(
            "  {path} {:>6.2}%  {:<40} {}  {:>16} {:<6} -> {:>16} {:<6} gas {}",
            l.share_bps as f64 / 100.0,
            l.label,
            &l.pool[..10.min(l.pool.len())],
            l.amount_in,
            l.token_in,
            l.amount_out,
            l.token_out,
            l.gas
        );
    }
    if let Some(b) = &q.best_single {
        println!("  best single route: {} ({}) -> {}", b.label, b.pool, b.amount_out.clone().unwrap_or_default());
    }
    println!(
        "  search: {} direct pools ({} pruned), via [{}]: {} multi-hop candidates, {} routes, {} over capacity, {} hop moves, {} simulations{} in {} ms",
        q.pools_considered,
        q.pruned,
        q.via.join(", "),
        q.multi_hop_candidates,
        q.multi_hop_routes,
        q.over_capacity,
        q.hop_moves,
        q.simulations,
        if q.truncated { " (budget hit)" } else { "" },
        q.search_ms
    );
}
