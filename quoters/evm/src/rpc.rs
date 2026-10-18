//! Minimal blocking JSON-RPC client with batching and retry on rate limits.

use anyhow::{anyhow, bail, Context, Result};
use revm::primitives::{Address, Bytes, B256, U256};
use serde_json::{json, Value};
use std::str::FromStr;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Duration;

pub struct Rpc {
    pub url: String,
    agent: ureq::Agent,
    pub requests: AtomicU64,
    pub batch_items: AtomicU64,
    /// Requests retried after HTTP 429/5xx or a transport error.
    pub retries: AtomicU64,
    /// In-flight HTTP requests, capped at `MAX_INFLIGHT`: free endpoints throttle bursts,
    /// and a throttled sync stalls the whole mirror.
    inflight: (std::sync::Mutex<usize>, std::sync::Condvar),
}

const MAX_INFLIGHT: usize = 2;

const BATCH: usize = 100;

impl Rpc {
    pub fn new(url: &str) -> Self {
        let agent = ureq::Agent::config_builder()
            .timeout_global(Some(Duration::from_secs(30)))
            .http_status_as_error(false)
            .build()
            .into();
        Rpc { url: url.to_string(), agent, requests: AtomicU64::new(0), batch_items: AtomicU64::new(0), retries: AtomicU64::new(0), inflight: (std::sync::Mutex::new(0), std::sync::Condvar::new()) }
    }

    fn post(&self, body: &Value) -> Result<Value> {
        {
            let (m, cv) = &self.inflight;
            let mut n = cv.wait_while(m.lock().unwrap(), |n| *n >= MAX_INFLIGHT).unwrap();
            *n += 1;
        }
        let r = self.post_inner(body);
        let (m, cv) = &self.inflight;
        *m.lock().unwrap() -= 1;
        cv.notify_one();
        r
    }

    fn post_inner(&self, body: &Value) -> Result<Value> {
        let mut delay = 300;
        let mut last = String::new();
        for _ in 0..7 {
            self.requests.fetch_add(1, Ordering::Relaxed);
            match self.agent.post(&self.url).header("content-type", "application/json").send_json(body) {
                Ok(mut resp) => {
                    let status = resp.status().as_u16();
                    if status == 429 || status >= 500 {
                        last = format!("HTTP {status}");
                    } else {
                        let text = resp.body_mut().with_config().limit(256 << 20).read_to_string()?;
                        return serde_json::from_str(&text)
                            .with_context(|| format!("HTTP {status}: {}", &text[..text.len().min(300)]));
                    }
                }
                Err(e) => last = e.to_string(),
            }
            self.retries.fetch_add(1, Ordering::Relaxed);
            if std::env::var("RPC_LOG").is_ok() {
                let what = body.as_array().map(|a| format!("batch of {} {}", a.len(), a[0]["method"])).unwrap_or_else(|| body["method"].to_string());
                eprintln!("rpc retry ({last}) {what}");
            }
            std::thread::sleep(Duration::from_millis(delay));
            delay = (delay * 2).min(5000);
        }
        bail!("rpc {}: {last}", self.url)
    }

    pub fn call(&self, method: &str, params: Value) -> Result<Value> {
        let v = self.post(&json!({"jsonrpc":"2.0","id":1,"method":method,"params":params}))?;
        if let Some(e) = v.get("error") {
            bail!("{method}: {e}");
        }
        Ok(v.get("result").cloned().unwrap_or(Value::Null))
    }

    /// Sends requests in batches of 100. Items that fail with a rate-limit style error are retried.
    pub fn batch(&self, reqs: &[(&str, Value)]) -> Result<Vec<Value>> {
        self.batch_n(reqs, BATCH)
    }

    pub fn batch_n(&self, reqs: &[(&str, Value)], size: usize) -> Result<Vec<Value>> {
        let mut out: Vec<Option<Value>> = vec![None; reqs.len()];
        let mut pending: Vec<usize> = (0..reqs.len()).collect();
        for round in 0..6 {
            if pending.is_empty() {
                break;
            }
            let mut failed = Vec::new();
            let mut last_err = String::new();
            // Up to 4 HTTP requests in flight.
            let chunks: Vec<&[usize]> = pending.chunks(size).collect();
            let results: Vec<(Vec<(usize, Value)>, Vec<usize>, String)> = std::thread::scope(|sc| {
                let mut out = Vec::new();
                for group in chunks.chunks(4) {
                    let hs: Vec<_> = group.iter().map(|chunk| sc.spawn(move || self.send_chunk(reqs, chunk))).collect();
                    out.extend(hs.into_iter().map(|h| h.join().unwrap()));
                }
                out
            });
            for (ok, bad, err) in results {
                for (id, v) in ok {
                    out[id] = Some(v);
                }
                if !bad.is_empty() {
                    last_err = err;
                }
                failed.extend(bad);
            }
            if !failed.is_empty() && round < 5 {
                std::thread::sleep(Duration::from_millis(400 * (round as u64 + 1)));
            } else if !failed.is_empty() {
                bail!("rpc batch: {} of {} items failed: {last_err}", failed.len(), reqs.len());
            }
            pending = failed;
        }
        out.into_iter().map(|v| v.ok_or_else(|| anyhow!("rpc batch: missing result"))).collect()
    }
}

impl Rpc {
    /// One HTTP batch; returns (answered items, items to retry, last error).
    fn send_chunk(&self, reqs: &[(&str, Value)], chunk: &[usize]) -> (Vec<(usize, Value)>, Vec<usize>, String) {
        let body: Vec<Value> =
            chunk.iter().map(|&i| json!({"jsonrpc":"2.0","id":i,"method":reqs[i].0,"params":reqs[i].1})).collect();
        self.batch_items.fetch_add(chunk.len() as u64, Ordering::Relaxed);
        let resp = match self.post(&Value::Array(body)) {
            Ok(Value::Array(a)) => a,
            Ok(other) => return (vec![], chunk.to_vec(), other.to_string()),
            Err(e) => return (vec![], chunk.to_vec(), e.to_string()),
        };
        let mut ok = Vec::new();
        let mut err = String::new();
        let mut seen = vec![false; chunk.len()];
        for item in resp {
            let Some(id) = item.get("id").and_then(|v| v.as_u64()) else { continue };
            let id = id as usize;
            let Some(pos) = chunk.iter().position(|&c| c == id) else { continue };
            if let Some(e) = item.get("error") {
                let msg = e.to_string();
                err = msg.clone();
                // Reverts and invalid params are final; everything else is retried.
                if msg.contains("revert") || msg.contains("invalid") || msg.contains("proof window") || msg.contains("Archive requests") {
                    ok.push((id, json!({"__error": msg})));
                    seen[pos] = true;
                }
            } else {
                ok.push((id, item.get("result").cloned().unwrap_or(Value::Null)));
                seen[pos] = true;
            }
        }
        let bad = chunk.iter().zip(seen).filter(|(_, s)| !s).map(|(c, _)| *c).collect();
        (ok, bad, err)
    }
}

pub fn hex_u64(v: &Value) -> Result<u64> {
    let s = v.as_str().ok_or_else(|| anyhow!("expected hex string, got {v}"))?;
    Ok(u64::from_str_radix(s.trim_start_matches("0x"), 16)?)
}

pub fn hex_u256(v: &Value) -> Result<U256> {
    let s = v.as_str().ok_or_else(|| anyhow!("expected hex string, got {v}"))?;
    let s = s.trim_start_matches("0x");
    if s.is_empty() {
        return Ok(U256::ZERO);
    }
    Ok(U256::from_str_radix(s, 16)?)
}

pub fn hex_bytes(v: &Value) -> Result<Bytes> {
    let s = v.as_str().ok_or_else(|| anyhow!("expected hex string, got {v}"))?;
    Ok(Bytes::from_str(s)?)
}

pub fn hex_b256(v: &Value) -> Result<B256> {
    let s = v.as_str().ok_or_else(|| anyhow!("expected hex string, got {v}"))?;
    Ok(B256::from_str(s)?)
}

pub fn hex_addr(v: &Value) -> Result<Address> {
    let s = v.as_str().ok_or_else(|| anyhow!("expected address, got {v}"))?;
    Ok(Address::from_str(s)?)
}

pub fn tag(block: u64) -> Value {
    Value::String(format!("0x{block:x}"))
}
