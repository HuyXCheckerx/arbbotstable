mod aggregators;
mod bench;
mod chains;
mod engine;
mod enginebench;
mod evm;
mod guard;
#[cfg(test)]
mod livetest;
mod mirror;
mod optimizer;
mod quote;
mod router;
mod rpc;
mod server;
#[cfg(test)]
mod testnode;
mod venues;
mod verify;

use anyhow::{anyhow, Result};
use clap::{Parser, Subcommand};

#[derive(Parser)]
#[command(about = "Local USDC/USDT quote engine for EVM chains (revm against mirrored chain state)")]
struct Cli {
    #[command(subcommand)]
    cmd: Cmd,
}

#[derive(Subcommand)]
enum Cmd {
    /// Long-running quote service for one chain (websocket-synced mirror, simulation cache).
    Serve {
        #[arg(long, default_value = "eth")]
        chain: String,
        #[arg(long)]
        rpc: Option<String>,
        #[arg(long, default_value = "127.0.0.1")]
        host: String,
        #[arg(long, default_value_t = 8795)]
        port: u16,
        /// Refuse quotes while the mirror is more than this many blocks behind the head.
        #[arg(long, default_value_t = 8)]
        max_lag: u64,
        /// Refuse quotes when the last successful sync is older than this.
        #[arg(long, default_value_t = 30_000)]
        max_sync_age_ms: u64,
        /// Pairs to warm before serving, besides USDC/USDT (e.g. "PYUSD,USDG;USDC,USDS").
        #[arg(long)]
        warm_pairs: Option<String>,
        /// Refuse a quote whose rate is this far from the pair's recent quotes of a similar size
        /// until a full re-read of the mirror confirms it (0 = off).
        #[arg(long, default_value_t = 200.0)]
        max_jump_bps: f64,
    },
    /// Local quotes vs ParaSwap, CoW and LI.FI at the same time; writes artifacts/bench.{md,json}.
    Bench {
        #[arg(long, default_value = "eth,bsc,polygon")]
        chains: String,
        #[arg(long, default_value = "1000,10000,100000,1000000")]
        sizes: String,
        /// The token pair, both directions (e.g. PYUSD,USDG).
        #[arg(long, default_value = "USDC,USDT")]
        pair: String,
        #[arg(long, default_value_t = 1)]
        rounds: usize,
        /// Aggregators to ask: paraswap, paraswap-norfq (excludeRFQ=true), cow, lifi.
        /// LI.FI allows 75 unauthenticated requests per 2 hours; ParaSwap's keyless tier rate-limits too.
        #[arg(long, default_value = "paraswap,cow,lifi")]
        aggs: String,
    },
    /// Dump the prestate of one swap per venue (needs RPC), for engine timing.
    DumpState {
        #[arg(long, default_value = "eth")]
        chain: String,
        #[arg(long, default_value = "Uniswap v3 0.01%,Uniswap v4 0.001%,Fluid DEX,Curve DAI/USDC/USDT,Balancer v2")]
        venues: String,
        #[arg(long, default_value_t = 10_000)]
        amount: u128,
        #[arg(long, default_value = "artifacts/prestate-eth.json")]
        out: String,
    },
    /// Time revm on a prestate dump, with no RPC.
    EngineBench {
        #[arg(long, default_value = "eth")]
        chain: String,
        #[arg(long, default_value = "artifacts/prestate-eth.json")]
        state: String,
        #[arg(long, default_value_t = 2000)]
        iters: usize,
    },
    /// Replay every pool's local simulation on the node (eth_call with state overrides) and compare.
    Verify {
        #[arg(long, default_value = "eth")]
        chain: String,
        #[arg(long)]
        rpc: Option<String>,
        /// Whole-token sizes, comma separated.
        #[arg(long, default_value = "1000,100000")]
        sizes: String,
        /// Simulate once, wait this many seconds, sync, then compare (tests log-driven sync).
        #[arg(long, default_value_t = 0)]
        wait: u64,
        /// Only pools trading one of these tokens (comma separated symbols).
        #[arg(long)]
        tokens: Option<String>,
    },
    /// Discover USDC/USDT pools and cache them in artifacts/pools-<chain>.json.
    Discover {
        #[arg(long, default_value = "eth")]
        chain: String,
        #[arg(long)]
        rpc: Option<String>,
    },
    /// Quote an exact-input swap by simulating every pool locally.
    Quote {
        #[arg(long, default_value = "eth")]
        chain: String,
        #[arg(long)]
        rpc: Option<String>,
        #[arg(long, default_value = "100000")]
        amount: String,
        #[arg(long, default_value = "USDC")]
        input: String,
        #[arg(long, default_value = "USDT")]
        output: String,
        #[arg(long, default_value_t = 20)]
        steps: usize,
        #[arg(long, default_value_t = 4)]
        max_splits: usize,
        #[arg(long, default_value_t = 10)]
        precision_ppm: u64,
        #[arg(long, default_value_t = 4096)]
        max_sims: usize,
        /// Ignore gas when choosing the split.
        #[arg(long)]
        no_gas: bool,
        /// Spend the whole input even past a ~20% price move (walks every tick; slow).
        #[arg(long)]
        full_fill: bool,
        /// Intermediates, comma separated (default: the chain's list; "none" = direct only).
        #[arg(long)]
        via: Option<String>,
        /// Longest route: 1 = direct, 2 = two hops, 3 = three hops.
        #[arg(long, default_value_t = 3)]
        max_hops: usize,
        #[arg(long)]
        rediscover: bool,
        #[arg(long)]
        json: bool,
        /// Quote again from warm state this many times and print latency.
        #[arg(long, default_value_t = 1)]
        repeat: usize,
    },
}

fn main() -> Result<()> {
    match Cli::parse().cmd {
        Cmd::Serve { chain, rpc, host, port, max_lag, max_sync_age_ms, warm_pairs, max_jump_bps } => {
            let c = chains::by_key(&chain).ok_or_else(|| anyhow!("unknown chain {chain}"))?;
            if host != "127.0.0.1" && host != "localhost" {
                eprintln!("warning: listening beyond loopback with no authentication");
            }
            let s = quote::setup(c, rpc.as_deref(), false)?;
            for w in &s.warnings {
                eprintln!("warning: {w}");
            }
            let params = optimizer::Params { steps: 20, max_splits: 4, precision_ppm: 10, max_sims: 4096, prune: true, gas_cost: 0.0, min_probe: 1, cutoff: 0.5, full_fill: false, via: None, max_hops: 3 };
            // Load every pool's state at the largest sizes before serving: lazy loads during
            // requests are slow and draw rate limits on free endpoints.
            let t = std::time::Instant::now();
            let guard: &'static guard::Guard = Box::leak(Box::new(guard::Guard::new(max_jump_bps, std::time::Duration::from_secs(600))));
            let mut warm = vec![(c.usdc, c.usdt), (c.usdt, c.usdc)];
            for pair in warm_pairs.unwrap_or_default().split(';').filter(|x| !x.is_empty()) {
                let t: Vec<_> = pair.split(',').filter_map(|x| c.token(x.trim())).collect();
                if t.len() == 2 {
                    warm.push((t[0], t[1]));
                    warm.push((t[1], t[0]));
                }
            }
            for (tin, tout) in warm {
                if let Ok(m) = quote::market(&s.engine, tout) {
                    for size in [1_000_000u128, 100_000, 1_000] {
                        let amount = size * 10u128.pow(tin.decimals as u32);
                        match quote::quote(&s.engine, tin, tout, amount, &params, &m) {
                            Ok(q) => guard.seed((tin.address, tout.address, params.max_splits), amount, q.out_atomic.parse().unwrap_or(0), std::time::Instant::now()),
                            Err(e) => eprintln!("warm-up {}->{} {size}: {e}", tin.symbol, tout.symbol),
                        }
                    }
                }
            }
            eprintln!("warm-up {} ms", t.elapsed().as_millis());
            let engine: &'static engine::Engine = Box::leak(Box::new(s.engine));
            server::serve(engine, &host, port, params, max_lag, max_sync_age_ms, guard)?;
        }
        Cmd::Bench { chains: cs, sizes, pair, rounds, aggs } => {
            let sizes: Vec<u128> = sizes.split(',').map(|x| x.trim().parse()).collect::<Result<_, _>>()?;
            let params = optimizer::Params { steps: 20, max_splits: 4, precision_ppm: 10, max_sims: 4096, prune: true, gas_cost: 0.0, min_probe: 1, cutoff: 0.5, full_fill: false, via: None, max_hops: 3 };
            let mut runs = Vec::new();
            for k in cs.split(',') {
                let c = chains::by_key(k.trim()).ok_or_else(|| anyhow!("unknown chain {k}"))?;
                let pp: Vec<&str> = pair.split(',').map(|x| x.trim()).collect();
                let (Some(a), Some(b)) = (pp.first().and_then(|x| c.token(x)), pp.get(1).and_then(|x| c.token(x))) else {
                    eprintln!("{k}: unknown pair {pair}");
                    continue;
                };
                match bench::run(c, (a, b), &sizes, rounds, &params, &aggs.split(',').map(|x| x.trim().to_string()).collect::<Vec<_>>()) {
                    Ok(r) => runs.push(r),
                    Err(e) => eprintln!("{k}: {e}"),
                }
            }
            std::fs::create_dir_all("artifacts")?;
            let stem = if pair.eq_ignore_ascii_case("USDC,USDT") { "bench".to_string() } else { format!("bench-{}", pair.replace(',', "-").to_lowercase()) };
            std::fs::write(format!("artifacts/{stem}.json"), serde_json::to_string_pretty(&runs)?)?;
            let md = bench::markdown(&runs);
            std::fs::write(format!("artifacts/{stem}.md"), &md)?;
            println!("{md}");
        }
        Cmd::DumpState { chain, venues, amount, out } => {
            let c = chains::by_key(&chain).ok_or_else(|| anyhow!("unknown chain {chain}"))?;
            let s = quote::setup(c, None, false)?;
            let v: Vec<&str> = venues.split(',').map(|x| x.trim()).collect();
            enginebench::dump(&s.engine, &v, amount, &out)?;
            println!("wrote {out}");
        }
        Cmd::EngineBench { chain, state, iters } => {
            let c = chains::by_key(&chain).ok_or_else(|| anyhow!("unknown chain {chain}"))?;
            enginebench::replay(c, &state, iters)?;
        }
        Cmd::Verify { chain, rpc, sizes, wait, tokens } => {
            let c = chains::by_key(&chain).ok_or_else(|| anyhow!("unknown chain {chain}"))?;
            let s = quote::setup(c, rpc.as_deref(), false)?;
            for w in &s.warnings {
                eprintln!("warning: {w}");
            }
            let sizes: Vec<u128> = sizes.split(',').map(|x| x.trim().parse()).collect::<Result<_, _>>()?;
            let only: Vec<chains::Token> = tokens.map(|t| t.split(',').filter_map(|x| c.token(x.trim())).collect()).unwrap_or_default();
            let rows = verify::run(&s.engine, &sizes, wait, &only)?;
            verify::print(&s.engine, &rows);
        }
        Cmd::Discover { chain, rpc } => {
            let c = chains::by_key(&chain).ok_or_else(|| anyhow!("unknown chain {chain}"))?;
            let r = rpc::Rpc::new(rpc.as_deref().unwrap_or(c.rpc_http));
            let d = venues::discover(c, &r)?;
            std::fs::create_dir_all("artifacts")?;
            std::fs::write(format!("artifacts/pools-{}.json", c.key), serde_json::to_string_pretty(&d)?)?;
            for p in &d.pools {
                println!("{:<22} {:<40} {}", p.venue, p.label, p.id);
            }
            for (dex, addr, tvl) in &d.unsupported {
                println!("unsupported {dex:<28} {addr} ${tvl:.0}");
            }
            for w in &d.warnings {
                eprintln!("warning: {w}");
            }
        }
        Cmd::Quote { chain, rpc, amount, input, output, steps, max_splits, precision_ppm, max_sims, no_gas, full_fill, via, max_hops, rediscover, json, repeat } => {
            let c = chains::by_key(&chain).ok_or_else(|| anyhow!("unknown chain {chain}"))?;
            let tin = c.token(&input).ok_or_else(|| anyhow!("unknown token {input}"))?;
            let tout = c.token(&output).ok_or_else(|| anyhow!("unknown token {output}"))?;
            let s = quote::setup(c, rpc.as_deref(), rediscover)?;
            for w in &s.warnings {
                eprintln!("warning: {w}");
            }
            let atomic = quote::parse_amount(&amount, tin.decimals)?;
            let params = optimizer::Params {
                steps,
                max_splits,
                precision_ppm,
                max_sims,
                prune: true,
                gas_cost: if no_gas { -1.0 } else { 0.0 },
                min_probe: 1,
                cutoff: 0.5,
                full_fill,
                via: via.map(|v| if v.eq_ignore_ascii_case("none") { vec![] } else { v.split(',').map(|x| x.trim().to_string()).collect() }),
                max_hops,
            };
            let m = quote::market(&s.engine, tout)?;
            let mut times = Vec::new();
            for i in 0..repeat.max(1) {
                let q = quote::quote(&s.engine, tin, tout, atomic, &params, &m)?;
                times.push(q.total_ms);
                if i == 0 {
                    if json {
                        println!("{}", serde_json::to_string_pretty(&q)?);
                    } else {
                        quote::print_quote(&q);
                        let (accts, slots) = s.engine.mirror.stats();
                        println!(
                            "  setup {} ms, first quote {} ms (cold), {} accounts / {} slots mirrored, {} RPC requests",
                            s.setup_ms,
                            q.total_ms,
                            accts,
                            slots,
                            s.engine.mirror.rpc.requests.load(std::sync::atomic::Ordering::Relaxed)
                        );
                    }
                }
            }
            if times.len() > 1 {
                let mut warm = times[1..].to_vec();
                warm.sort();
                eprintln!("warm quote latency: p50 {} ms, max {} ms over {} runs", warm[warm.len() / 2], warm[warm.len() - 1], warm.len());
            }
        }
    }
    Ok(())
}
