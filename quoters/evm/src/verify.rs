//! Replays local simulations on the RPC node: the same helper call, as `eth_call` at the
//! mirror's block with the helper's code and every pinned slot passed as state overrides,
//! and the block number and timestamp overridden to the ones the local EVM uses.

use crate::chains::Token;
use crate::engine::{Engine, CALLER, HELPER};
use crate::evm::GAS_LIMIT;
use crate::quote::human;
use crate::rpc::{hex_bytes, tag};
use anyhow::Result;
use revm::primitives::{Bytes, B256, U256};
use serde_json::{json, Map, Value};

pub struct Row {
    pub out_decimals: u8,
    pub label: String,
    pub dir: String,
    pub amount: u128,
    pub local: Option<(u128, u64)>,
    pub node: Option<(u128, u64)>,
    pub err: Option<String>,
}

fn overrides(engine: &Engine) -> Value {
    let st = engine.mirror.state.read();
    let mut m = Map::new();
    for (a, info) in &st.pinned_accounts {
        let code = info.code.as_ref().map(|c| Bytes::from(c.original_bytes().to_vec())).unwrap_or_default();
        let mut o = json!({"balance": format!("0x{:x}", info.balance), "nonce": format!("0x{:x}", info.nonce)});
        if !code.is_empty() {
            o["code"] = json!(code);
        }
        m.insert(format!("{a:?}"), o);
    }
    for (a, slots) in &st.pinned_storage {
        let diff: Map<String, Value> =
            slots.iter().map(|(k, v)| (format!("{}", B256::from(*k)), json!(B256::from(*v)))).collect();
        let e = m.entry(format!("{a:?}")).or_insert_with(|| json!({}));
        e["stateDiff"] = Value::Object(diff);
    }
    Value::Object(m)
}

fn decode(out: &Bytes) -> Option<(u128, u64)> {
    if out.len() < 96 {
        return None;
    }
    let w = |i: usize| U256::from_be_slice(&out[i * 32..i * 32 + 32]);
    Some((w(0).to::<u128>(), w(2).to::<u64>()))
}

/// With `wait_secs`, every case is simulated once first, then the chain moves on for that
/// long and the mirror syncs, so the comparison also tests that sync kept the state current.
/// Every ordered token pair of every pool (graph tokens only), at each size. `only` keeps pools
/// trading at least one of those tokens.
fn cases<'a>(engine: &'a Engine, sizes: &[u128], only: &[Token]) -> Vec<(&'a crate::venues::Pool, Token, Token, u128)> {
    let c = engine.chain;
    let mut out = Vec::new();
    for p in &engine.pools {
        if !only.is_empty() && !p.tokens.iter().any(|t| only.iter().any(|o| o.address == *t)) {
            continue;
        }
        let toks: Vec<Token> = p.tokens.iter().filter_map(|a| c.token_by_address(*a)).collect();
        for tin in &toks {
            for tout in &toks {
                if tin.address == tout.address {
                    continue;
                }
                for &s in sizes {
                    out.push((p, *tin, *tout, s * 10u128.pow(tin.decimals as u32)));
                }
            }
        }
    }
    out
}

pub fn run(engine: &Engine, sizes: &[u128], wait_secs: u64, only: &[Token]) -> Result<Vec<Row>> {
    if wait_secs > 0 {
        for (p, tin, tout, a) in cases(engine, sizes, only) {
            let _ = engine.simulate_raw(p, tin.address, tout.address, a);
        }
        std::thread::sleep(std::time::Duration::from_secs(wait_secs));
    }
    let rep = engine.mirror.sync(64, false)?;
    eprintln!("synced {} -> {} ({} accounts re-read)", rep.from, rep.to, rep.refreshed_accounts);
    let c = engine.chain;
    let cases = cases(engine, sizes, only);
    // Local first, so any lazily loaded state is in place; then one batch at the same block.
    let locals: Vec<Result<(u128, u64), String>> = cases
        .iter()
        .map(|(p, tin, tout, a)| engine.simulate_raw(p, tin.address, tout.address, *a).map(|s| (s.out, s.gas)).map_err(|e| e.to_string()))
        .collect();
    let (block, head) = {
        let st = engine.mirror.state.read();
        (st.block, st.header.clone())
    };
    let ov = overrides(engine);
    let bo = json!({
        "number": format!("0x{:x}", head.number + 1),
        "time": format!("0x{:x}", head.timestamp + c.block_time_ms.div_ceil(1000).max(1)),
    });
    let reqs: Vec<(&str, Value)> = cases
        .iter()
        .map(|(p, tin, tout, a)| {
            let data = p.helper_call(HELPER, tin.address, tout.address, U256::from(*a), engine.limit()).unwrap();
            // The local EVM pays the chain's gas price; some pools (two Curve pools on Ethereum,
            // a hooked Pancake Infinity pool on BSC) price differently when tx.gasprice is 0,
            // eth_call's default.
            let call = json!({"from": CALLER, "to": HELPER, "data": data, "gas": format!("0x{GAS_LIMIT:x}"),
                              "gasPrice": format!("0x{:x}", head.gas_price.max(head.basefee as u128))});
            ("eth_call", json!([call, tag(block), ov, bo]))
        })
        .collect();
    if let Ok(path) = std::env::var("VERIFY_DUMP") {
        std::fs::write(&path, serde_json::to_string(&reqs.iter().map(|r| &r.1).collect::<Vec<_>>())?)?;
        let touched: Vec<Value> = cases
            .iter()
            .map(|(p, tin, tout, a)| {
                let t = engine.touched(p, tin.address, tout.address, *a).unwrap_or_default();
                json!(t.iter().map(|(a, ks)| json!({"address": a, "keys": ks.iter().map(|k| B256::from(*k)).collect::<Vec<_>>()})).collect::<Vec<_>>())
            })
            .collect();
        std::fs::write(format!("{path}.local"), serde_json::to_string(&touched)?)?;
    }
    let mut nodes = engine.mirror.rpc.batch_n(&reqs, 8);
    if let Ok(v) = &nodes {
        // Nodes without block overrides reject the fourth parameter; retry without it.
        if v.iter().all(|x| x.get("__error").map(|e| e.to_string().contains("invalid")).unwrap_or(false)) {
            let reqs3: Vec<(&str, Value)> =
                reqs.iter().map(|(m, p)| (*m, json!([p[0], p[1], p[2]]))).collect();
            eprintln!("node rejected block overrides; comparing at the node's block context");
            nodes = engine.mirror.rpc.batch_n(&reqs3, 8);
        }
    }
    let nodes = nodes?;
    let mut rows = Vec::new();
    for (((p, tin, tout, a), l), n) in cases.iter().zip(locals).zip(nodes) {
        let node = if n.get("__error").is_some() { None } else { hex_bytes(&n).ok().as_ref().and_then(decode) };
        let err = match (&l, &node) {
            (Err(e), None) => Some(format!("both failed: {e} / {}", n.get("__error").map(|e| e.to_string()).unwrap_or_default())),
            (Err(e), Some(_)) => Some(format!("local failed: {e}")),
            (Ok(_), None) => Some(format!("node failed: {}", n.get("__error").map(|e| e.to_string()).unwrap_or_else(|| n.to_string()))),
            _ => None,
        };
        rows.push(Row {
            out_decimals: tout.decimals,
            label: p.short(),
            dir: format!("{}->{}", tin.symbol, tout.symbol),
            amount: *a / 10u128.pow(tin.decimals as u32),
            local: l.ok(),
            node,
            err,
        });
    }
    Ok(rows)
}

pub fn print(_engine: &Engine, rows: &[Row]) {
    let mut exact = 0;
    let mut compared = 0;
    let mut worst: f64 = 0.0;
    for r in rows {
        let dec = r.out_decimals;
        match (r.local, r.node) {
            (Some(l), Some(n)) => {
                compared += 1;
                let bp = if n.0 > 0 { (l.0 as f64 - n.0 as f64) / n.0 as f64 * 1e4 } else { 0.0 };
                worst = worst.max(bp.abs());
                if l.0 == n.0 {
                    exact += 1;
                }
                println!(
                    "{:<44} {:<10} {:>8}  local {:>18} gas {:>7}  node {:>18} gas {:>7}  {}",
                    r.label,
                    r.dir,
                    r.amount,
                    human(l.0, dec),
                    l.1,
                    human(n.0, dec),
                    n.1,
                    if l.0 == n.0 { "exact".to_string() } else { format!("{bp:+.4} bp") }
                );
            }
            _ => println!("{:<44} {:<10} {:>8}  {}", r.label, r.dir, r.amount, r.err.clone().unwrap_or_default()),
        }
    }
    println!("{exact} of {compared} compared simulations match the node exactly; largest difference {worst:.4} bp");
}
