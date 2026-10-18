//! Benchmark: local quotes against public aggregator quotes requested at the same time.

use crate::aggregators::{self, AggQuote};
use crate::chains::Chain;
use crate::optimizer::Params;
use crate::quote::{self, QuoteOut};
use anyhow::Result;
use serde::Serialize;
use std::fmt::Write as _;
use std::time::Instant;

#[derive(Serialize)]
pub struct Row {
    pub out_decimals: u8,
    pub chain: String,
    pub pair: String,
    pub size: u128,
    pub block: u64,
    pub sync_ms: u128,
    pub local: QuoteOut,
    pub aggs: Vec<AggQuote>,
}

#[derive(Serialize)]
pub struct Skipped {
    pub pair: String,
    pub size: u128,
    pub reason: String,
    pub aggs: Vec<AggQuote>,
}

#[derive(Serialize)]
pub struct ChainRun {
    pub chain: String,
    pub pools: usize,
    pub setup_ms: u128,
    pub cold_ms: Vec<u128>,
    pub rows: Vec<Row>,
    pub skipped: Vec<Skipped>,
    pub warnings: Vec<String>,
}

pub fn run(chain: &'static Chain, pair: (crate::chains::Token, crate::chains::Token), sizes: &[u128], rounds: usize, params: &Params, aggs: &[String]) -> Result<ChainRun> {
    let want = |n: &str| aggs.iter().any(|a| a == n);
    let (use_ps, use_ps_norfq, use_cow, lifi) = (want("paraswap"), want("paraswap-norfq"), want("cow"), want("lifi"));
    let s = quote::setup(chain, None, false)?;
    let e = &s.engine;
    let dirs = [(pair.0, pair.1), (pair.1, pair.0)];
    // Cold quotes load every pool's state lazily, so the mirror is warmed at every size first;
    // the measured quotes then run on the synced mirror, as a long-running service would.
    let mut cold_ms = Vec::new();
    for (tin, tout) in dirs {
        let m = quote::market(e, tout)?;
        for &size in sizes.iter().rev() {
            match quote::quote(e, tin, tout, size * 10u128.pow(tin.decimals as u32), params, &m) {
                Ok(q) => cold_ms.push(q.total_ms),
                Err(err) => eprintln!("warm-up {}->{} {size}: {err}", tin.symbol, tout.symbol),
            }
        }
    }
    let agent = crate::venues::agent();
    let mut rows = Vec::new();
    let mut skipped = Vec::new();
    for _ in 0..rounds {
        for (tin, tout) in dirs {
            for &size in sizes {
                let amount = size * 10u128.pow(tin.decimals as u32);
                let t = Instant::now();
                let rep = e.mirror.sync(64, false)?;
                let sync_ms = t.elapsed().as_millis();
                let m = quote::market(e, tout)?;
                let (local, aggs) = std::thread::scope(|sc| {
                    // ParaSwap's two variants run one after the other: its keyless tier rate-limits.
                    let a = agent.clone();
                    let h1 = sc.spawn(move || {
                        let mut v = Vec::new();
                        if use_ps {
                            v.push(aggregators::paraswap(&a, chain, tin, tout, amount, false));
                        }
                        if use_ps_norfq {
                            std::thread::sleep(std::time::Duration::from_secs(2));
                            v.push(aggregators::paraswap(&a, chain, tin, tout, amount, true));
                        }
                        v
                    });
                    let a = agent.clone();
                    let h3 = use_cow.then(|| sc.spawn(move || aggregators::cow(&a, chain, tin, tout, amount)));
                    let a = agent.clone();
                    let h4 = lifi.then(|| sc.spawn(move || aggregators::lifi(&a, chain, tin, tout, amount)));
                    let local = quote::quote(e, tin, tout, amount, params, &m);
                    let mut aggs = h1.join().unwrap();
                    if let Some(h) = h3 {
                        aggs.push(h.join().unwrap());
                    }
                    if let Some(h) = h4 {
                        aggs.push(h.join().unwrap());
                    }
                    (local, aggs)
                });
                let local = match local {
                    Ok(l) => l,
                    Err(err) => {
                        eprintln!("{} {}->{} {size}: {err}", chain.key, tin.symbol, tout.symbol);
                        skipped.push(Skipped { pair: format!("{}->{}", tin.symbol, tout.symbol), size, reason: err.to_string(), aggs });
                        continue;
                    }
                };
                eprintln!(
                    "  sync {} -> {}: {} ms (logs {} ms / {} calls{}, read {} accounts {} slots {} ms) rpc retries so far {}",
                    rep.from, rep.to, sync_ms, rep.logs_ms, rep.log_calls, if rep.full { ", full" } else { "" },
                    rep.refreshed_accounts, rep.refreshed_slots, rep.read_ms,
                    e.mirror.rpc.retries.load(std::sync::atomic::Ordering::Relaxed)
                );
                eprintln!(
                    "{} {}->{} {:>8}: local {} ({} ms){}",
                    chain.key,
                    tin.symbol,
                    tout.symbol,
                    size,
                    local.amount_out,
                    local.search_ms,
                    aggs.iter()
                        .map(|a| format!(" | {} {}", a.name, a.gross.map(|g| quote::human(g, tout.decimals)).unwrap_or_else(|| a.err.clone().unwrap_or_default())))
                        .collect::<String>()
                );
                rows.push(Row {
                    out_decimals: tout.decimals,
                    chain: chain.key.into(),
                    pair: format!("{}->{}", tin.symbol, tout.symbol),
                    size,
                    block: rep.to,
                    sync_ms,
                    local,
                    aggs,
                });
                std::thread::sleep(std::time::Duration::from_secs(4));
            }
        }
    }
    Ok(ChainRun { chain: chain.key.into(), pools: e.pools.len(), setup_ms: s.setup_ms, cold_ms, rows, skipped, warnings: s.warnings })
}

fn bp(local: f64, other: f64) -> f64 {
    (local - other) / other * 1e4
}

pub fn markdown(runs: &[ChainRun]) -> String {
    let names = ["ParaSwap", "ParaSwap (no RFQ)", "CoW", "LI.FI"];
    let mut s = String::new();
    let _ = writeln!(s, "Output before network fees. Δ = local − aggregator, in bp of the aggregator's output; positive means the local quote pays more.\n");
    let _ = writeln!(s, "| chain | pair | size | local out | local ms | sync ms | {} | aggregator ms |", names.map(|n| format!("Δ {n}")).join(" | "));
    let _ = writeln!(s, "|---|---|---:|---:|---:|---:|{}---:|", "---:|".repeat(names.len()));
    for r in runs.iter().flat_map(|c| &c.rows) {
        let out: f64 = r.local.amount_out.parse().unwrap_or(0.0);
        let dec = r.out_decimals as i32;
        let cells: Vec<String> = names
            .iter()
            .map(|n| match r.aggs.iter().find(|a| a.name == *n) {
                Some(a) => match a.gross {
                    Some(g) => format!("{:+.2}", bp(out, g as f64 / 10f64.powi(dec))),
                    None => "n/a".into(),
                },
                None => "–".into(),
            })
            .collect();
        let ms: Vec<String> = r.aggs.iter().map(|a| a.ms.to_string()).collect();
        let _ = writeln!(
            s,
            "| {} | {} | {} | {:.2} | {} | {} | {} | {} |",
            r.chain,
            r.pair,
            r.size,
            out,
            r.local.search_ms,
            r.sync_ms,
            cells.join(" | "),
            ms.join(" / ")
        );
    }
    let _ = writeln!(s, "\nNet of gas: local net (legs + {} base gas at the chain's gas price) vs each aggregator's output minus its own gas estimate (CoW: its fee-reduced buy amount).\n", 60_000);
    let _ = writeln!(s, "| chain | pair | size | local net | local gas | {} |", names.map(|n| format!("Δ {n}")).join(" | "));
    let _ = writeln!(s, "|---|---|---:|---:|---:|{}", "---:|".repeat(names.len()));
    for r in runs.iter().flat_map(|c| &c.rows) {
        let net: f64 = r.local.net_out.parse().unwrap_or(0.0);
        let cells: Vec<String> = names
            .iter()
            .map(|n| match r.aggs.iter().find(|a| a.name == *n).and_then(|a| a.net) {
                Some(an) => format!("{:+.2}", bp(net, an)),
                None => "n/a".into(),
            })
            .collect();
        let _ = writeln!(s, "| {} | {} | {} | {:.4} | {} | {} |", r.chain, r.pair, r.size, net, r.local.leg_gas + r.local.base_gas, cells.join(" | "));
    }
    for c in runs {
        for k in &c.skipped {
            let others: Vec<String> = k.aggs.iter().map(|a| format!("{} {}", a.name, a.raw_out.clone().unwrap_or_else(|| "n/a".into()))).collect();
            let _ = writeln!(s, "\nNot quoted: {} {} {}: {} (aggregators: {})", c.chain, k.pair, k.size, k.reason, others.join(", "));
        }
    }
    let _ = writeln!(s, "\nRoutes:\n");
    for r in runs.iter().flat_map(|c| &c.rows) {
        let legs: Vec<String> = r
            .local
            .legs
            .iter()
            .map(|l| match &l.via {
                None => format!("{} {:.1}%", l.label, l.share_bps as f64 / 100.0),
                Some(v) => format!("[via {v} hop {}] {} {:.1}%", l.hop, l.label, l.share_bps as f64 / 100.0),
            })
            .collect();
        let _ = writeln!(s, "- {} {} {}: local {}", r.chain, r.pair, r.size, legs.join(" + "));
        for a in &r.aggs {
            let _ = writeln!(s, "  - {}: {}", a.name, if a.err.is_some() { a.err.clone().unwrap() } else { a.route.clone() });
        }
    }
    s
}
