//! Long-running quote service for one chain.
//!
//! A sync thread follows the chain: every websocket `newHeads` notification triggers
//! `Mirror::sync`, which re-reads the state of contracts that logged (and of their pool
//! groups). If the socket fails, it polls once per block time and retries the socket. Every
//! `RESYNC_SECS` it re-reads everything, since notifications and log filters can miss updates.
//! Quotes run against the mirror and reuse cached simulations of pools whose state did not change.

use crate::engine::Engine;
use crate::optimizer::Params;
use crate::quote::{self, parse_amount};
use anyhow::Result;
use serde_json::json;
use std::net::TcpStream;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Mutex;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use tungstenite::stream::MaybeTlsStream;
use tungstenite::{Message, WebSocket};

const RESYNC_SECS: u64 = 300;

pub struct Status {
    pub head: AtomicU64,
    pub synced_at_ms: AtomicU64,
    pub syncs: AtomicU64,
    pub sync_errors: AtomicU64,
    pub ws_connected: AtomicU64,
    /// Reads that timed out (no head within two block times) and fell back to a polled sync.
    pub ws_stalls: AtomicU64,
    pub latencies: Mutex<Vec<u128>>,
    pub served: AtomicU64,
}

fn now_ms() -> u64 {
    SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_millis() as u64
}

fn set_timeout(ws: &mut WebSocket<MaybeTlsStream<TcpStream>>, d: Duration) {
    match ws.get_mut() {
        MaybeTlsStream::Plain(s) => {
            let _ = s.set_read_timeout(Some(d));
        }
        MaybeTlsStream::Rustls(s) => {
            let _ = s.get_mut().set_read_timeout(Some(d));
        }
        _ => {}
    }
}

fn sync_once(engine: &Engine, st: &Status, force_full: bool) {
    match engine.mirror.sync(64, force_full) {
        Ok(rep) => {
            if std::env::var("SYNC_LOG").is_ok() {
                eprintln!(
                    "sync {}->{} {} ms (logs {} ms, read {} ms, {} accounts, {} changed{}) rpc retries {}",
                    rep.from, rep.to, rep.ms, rep.logs_ms, rep.read_ms, rep.refreshed_accounts, rep.changed_accounts,
                    if rep.full { ", full" } else { "" }, engine.mirror.rpc.retries.load(Ordering::Relaxed)
                );
            }
            st.syncs.fetch_add(1, Ordering::Relaxed);
            st.synced_at_ms.store(now_ms(), Ordering::Relaxed);
            st.head.fetch_max(rep.to, Ordering::Relaxed);
        }
        Err(e) => {
            st.sync_errors.fetch_add(1, Ordering::Relaxed);
            eprintln!("sync: {e}");
        }
    }
}

pub fn sync_loop(engine: &'static Engine, ws_url: String, st: &'static Status) {
    let block_ms = engine.chain.block_time_ms.max(400);
    let mut last_full = Instant::now();
    loop {
        match tungstenite::connect(ws_url.as_str()) {
            Ok((mut ws, _)) => {
                // PublicNode's websocket sometimes holds newHeads back for a minute or more, so a
                // read that waits longer than two block times triggers a poll-style sync.
                set_timeout(&mut ws, Duration::from_millis((block_ms * 2).max(2000)));
                let sub = json!({"jsonrpc":"2.0","id":1,"method":"eth_subscribe","params":["newHeads"]});
                if ws.send(Message::Text(sub.to_string().into())).is_err() {
                    continue;
                }
                st.ws_connected.store(1, Ordering::Relaxed);
                loop {
                    match ws.read() {
                        Ok(Message::Text(t)) => {
                            let v: serde_json::Value = serde_json::from_str(&t).unwrap_or_default();
                            if let Some(n) = v["params"]["result"]["number"].as_str() {
                                if let Ok(n) = u64::from_str_radix(n.trim_start_matches("0x"), 16) {
                                    st.head.fetch_max(n, Ordering::Relaxed);
                                }
                                let full = last_full.elapsed().as_secs() >= RESYNC_SECS;
                                // Heads that arrive during a sync queue in the socket; the next
                                // sync jumps straight to the latest block.
                                sync_once(engine, st, full);
                                if full {
                                    last_full = Instant::now();
                                }
                            }
                        }
                        Ok(Message::Ping(p)) => {
                            let _ = ws.send(Message::Pong(p));
                        }
                        Ok(_) => {}
                        Err(tungstenite::Error::Io(e))
                            if matches!(e.kind(), std::io::ErrorKind::WouldBlock | std::io::ErrorKind::TimedOut) =>
                        {
                            st.ws_stalls.fetch_add(1, Ordering::Relaxed);
                            let full = last_full.elapsed().as_secs() >= RESYNC_SECS;
                            sync_once(engine, st, full);
                            if full {
                                last_full = Instant::now();
                            }
                        }
                        Err(e) => {
                            eprintln!("websocket: {e}; polling until it reconnects");
                            break;
                        }
                    }
                }
                st.ws_connected.store(0, Ordering::Relaxed);
            }
            Err(e) => eprintln!("websocket connect: {e}; polling"),
        }
        // Poll for ~30 s, then try the socket again.
        let t = Instant::now();
        while t.elapsed().as_secs() < 30 {
            let full = last_full.elapsed().as_secs() >= RESYNC_SECS;
            sync_once(engine, st, full);
            if full {
                last_full = Instant::now();
            }
            std::thread::sleep(Duration::from_millis(block_ms));
        }
    }
}

fn query(url: &str) -> std::collections::HashMap<String, String> {
    url.split_once('?')
        .map(|(_, q)| q)
        .unwrap_or("")
        .split('&')
        .filter_map(|kv| kv.split_once('='))
        .map(|(k, v)| (k.to_string(), v.to_string()))
        .collect()
}

pub fn serve(engine: &'static Engine, host: &str, port: u16, params: Params, max_lag: u64, max_age_ms: u64) -> Result<()> {
    let st: &'static Status = Box::leak(Box::new(Status {
        head: AtomicU64::new(engine.mirror.block()),
        synced_at_ms: AtomicU64::new(now_ms()),
        syncs: AtomicU64::new(0),
        sync_errors: AtomicU64::new(0),
        ws_connected: AtomicU64::new(0),
        ws_stalls: AtomicU64::new(0),
        latencies: Mutex::new(Vec::new()),
        served: AtomicU64::new(0),
    }));
    let ws = engine.chain.rpc_ws.to_string();
    std::thread::spawn(move || sync_loop(engine, ws, st));
    let server = tiny_http::Server::http((host, port)).map_err(|e| anyhow::anyhow!("{e}"))?;
    eprintln!("listening on http://{host}:{port} ({}; GET /quote?input=USDC&output=USDT&amount=100000, GET /health)", engine.chain.name);
    let params = &*Box::leak(Box::new(params));
    for req in server.incoming_requests() {
        std::thread::spawn(move || {
            let url = req.url().to_string();
            let lag = st.head.load(Ordering::Relaxed).saturating_sub(engine.mirror.block());
            let age = now_ms().saturating_sub(st.synced_at_ms.load(Ordering::Relaxed));
            let healthy = lag <= max_lag && age <= max_age_ms;
            let (code, body) = if url.starts_with("/health") {
                let mut l = st.latencies.lock().unwrap().clone();
                l.sort();
                let pct = |p: f64| l.get(((l.len() as f64 * p) as usize).min(l.len().saturating_sub(1))).copied();
                let (accounts, slots) = engine.mirror.stats();
                let mut reasons = Vec::new();
                if lag > max_lag {
                    reasons.push(format!("mirror {lag} blocks behind head"));
                }
                if age > max_age_ms {
                    reasons.push(format!("last sync {age} ms ago"));
                }
                (
                    if healthy { 200 } else { 503 },
                    json!({
                        "ok": healthy, "reasons": reasons, "chain": engine.chain.key, "block": engine.mirror.block(),
                        "head": st.head.load(Ordering::Relaxed), "lagBlocks": lag, "syncAgeMs": age,
                        "syncs": st.syncs.load(Ordering::Relaxed), "syncErrors": st.sync_errors.load(Ordering::Relaxed),
                        "websocket": st.ws_connected.load(Ordering::Relaxed) == 1, "wsStalls": st.ws_stalls.load(Ordering::Relaxed),
                        "pools": engine.pools.len(), "accounts": accounts, "slots": slots,
                        "cacheHits": engine.cache_hits.load(Ordering::Relaxed), "cacheMisses": engine.cache_misses.load(Ordering::Relaxed),
                        "served": st.served.load(Ordering::Relaxed), "p50Ms": pct(0.5), "p90Ms": pct(0.9), "p99Ms": pct(0.99),
                        "rpcRequests": engine.mirror.rpc.requests.load(Ordering::Relaxed), "rpcRetries": engine.mirror.rpc.retries.load(Ordering::Relaxed),
                    }),
                )
            } else if url.starts_with("/quote") {
                let q = query(&url);
                let c = engine.chain;
                let tin = q.get("input").and_then(|s| c.token(s));
                let tout = q.get("output").and_then(|s| c.token(s));
                match (tin, tout, q.get("amount")) {
                    _ if !healthy => (503, json!({"error": "mirrored state is not current", "lagBlocks": lag, "syncAgeMs": age})),
                    (Some(tin), Some(tout), Some(a)) if tin.address != tout.address => match parse_amount(a, tin.decimals) {
                        Ok(amount) if amount > 0 => {
                            let mut p = params.clone();
                            if let Some(ms) = q.get("maxSplits").and_then(|s| s.parse().ok()) {
                                p.max_splits = ms;
                            }
                            let t = Instant::now();
                            let r = quote::market(engine, tout).and_then(|m| quote::quote(engine, tin, tout, amount, &p, &m));
                            let ms = t.elapsed().as_millis();
                            match r {
                                Ok(out) => {
                                    st.served.fetch_add(1, Ordering::Relaxed);
                                    st.latencies.lock().unwrap().push(ms);
                                    let mut v = serde_json::to_value(&out).unwrap();
                                    v["serverMs"] = json!(ms);
                                    (200, v)
                                }
                                Err(e) => (422, json!({"error": e.to_string()})),
                            }
                        }
                        _ => (400, json!({"error": "bad amount"})),
                    },
                    _ => (400, json!({"error": "need input, output (USDC or USDT) and amount (in tokens)"})),
                }
            } else {
                (404, json!({"error": "not found"}))
            };
            let resp = tiny_http::Response::from_string(body.to_string())
                .with_status_code(code)
                .with_header("content-type: application/json".parse::<tiny_http::Header>().unwrap());
            let _ = req.respond(resp);
        });
    }
    Ok(())
}
