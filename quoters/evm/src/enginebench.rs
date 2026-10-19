//! Engine comparison: dumps the exact prestate of a few swaps, then replays them from that
//! dump in revm (no RPC) and times them. `bench-ts/run.mjs` replays the same dump in
//! @ethereumjs/evm.

use crate::engine::{Engine, CALLER, HELPER};
use crate::evm::exec;
use crate::mirror::{account_from, BlockCtx, Misses, State, View};
use crate::rpc::{hex_bytes, hex_u256};
use anyhow::{bail, Result};
use revm::primitives::{Address, Bytes, B256, U256};
use serde_json::{json, Map, Value};
use std::cell::RefCell;
use std::collections::BTreeMap;
use std::str::FromStr;
use std::time::Instant;

pub fn dump(engine: &Engine, venues: &[&str], amount_tokens: u128, path: &str) -> Result<()> {
    let c = engine.chain;
    let head = engine.mirror.state.read().header.clone();
    let mut accounts: BTreeMap<Address, (U256, u64, Bytes, BTreeMap<U256, U256>)> = BTreeMap::new();
    let mut cases = Vec::new();
    for v in venues {
        let Some(pool) = engine.pools.iter().find(|p| p.venue == *v || p.label.starts_with(*v)) else { continue };
        let amount = amount_tokens * 10u128.pow(c.usdc.decimals as u32);
        let sim = engine.simulate(pool, c.usdc.address, c.usdt.address, amount)?;
        let data = pool.helper_call(HELPER, c.usdc.address, c.usdt.address, U256::from(amount), engine.limit())?;
        let state = engine.mirror.run(|view| exec(view, &engine.env, &head, CALLER, HELPER, data.clone()).state)?;
        let st = engine.mirror.state.read();
        for (a, acc) in state {
            let info = st.pinned_accounts.get(&a).or_else(|| st.accounts.get(&a));
            let (bal, nonce, code) = match info {
                Some(i) => (i.balance, i.nonce, i.code.as_ref().map(|c| Bytes::from(c.original_bytes().to_vec())).unwrap_or_default()),
                None => (U256::ZERO, 0, Bytes::new()),
            };
            let e = accounts.entry(a).or_insert((bal, nonce, code, BTreeMap::new()));
            for (k, s) in acc.storage {
                e.3.insert(k, s.original_value);
            }
        }
        cases.push(json!({"label": pool.label, "pool": pool.id, "data": data, "out": sim.out.to_string(), "gas": sim.gas}));
    }
    let accts: Map<String, Value> = accounts
        .into_iter()
        .map(|(a, (b, n, code, st))| {
            let storage: Map<String, Value> = st.into_iter().map(|(k, v)| (format!("{}", B256::from(k)), json!(B256::from(v)))).collect();
            (format!("{a:?}"), json!({"balance": format!("0x{b:x}"), "nonce": n, "code": code, "storage": storage}))
        })
        .collect();
    let out = json!({
        "chainId": c.chain_id,
        "block": {"number": head.number + 1, "timestamp": head.timestamp + c.block_time_ms.div_ceil(1000).max(1), "basefee": head.basefee,
                  "gasLimit": head.gas_limit, "coinbase": head.beneficiary, "prevrandao": head.prevrandao, "gasPrice": head.gas_price.max(head.basefee as u128).to_string()},
        "caller": CALLER, "to": HELPER, "accounts": accts, "cases": cases,
    });
    std::fs::write(path, serde_json::to_string_pretty(&out)?)?;
    Ok(())
}

pub fn replay(chain: &'static crate::chains::Chain, path: &str, iters: usize) -> Result<()> {
    let d: Value = serde_json::from_str(&std::fs::read_to_string(path)?)?;
    let mut st = State::default();
    for (a, v) in d["accounts"].as_object().unwrap() {
        let a = Address::from_str(a)?;
        st.accounts.insert(a, account_from(hex_u256(&v["balance"])?, v["nonce"].as_u64().unwrap_or(0), hex_bytes(&v["code"])?));
        let m = st.storage.entry(a).or_default();
        for (k, val) in v["storage"].as_object().unwrap() {
            m.insert(U256::from_str(k)?, hex_u256(val)?);
        }
    }
    let b = &d["block"];
    let head = BlockCtx {
        number: b["number"].as_u64().unwrap() - 1,
        timestamp: b["timestamp"].as_u64().unwrap() - chain.block_time_ms.div_ceil(1000).max(1),
        basefee: b["basefee"].as_u64().unwrap(),
        gas_limit: b["gasLimit"].as_u64().unwrap(),
        prevrandao: B256::from_str(b["prevrandao"].as_str().unwrap())?,
        beneficiary: Address::from_str(b["coinbase"].as_str().unwrap())?,
        gas_price: b["gasPrice"].as_str().unwrap().parse()?,
        ..Default::default()
    };
    st.header = head.clone();
    let env = chain.env();
    for case in d["cases"].as_array().unwrap() {
        let data = hex_bytes(&case["data"])?;
        let misses = RefCell::new(Misses::default());
        let view = View { st: &st, misses: &misses };
        let r = exec(view, &env, &head, CALLER, HELPER, data.clone());
        if !misses.borrow().is_empty() || !r.success {
            bail!("{}: replay touched state outside the dump or failed", case["label"]);
        }
        let out = U256::from_be_slice(&r.output[..32]);
        let t = Instant::now();
        for _ in 0..iters {
            let r = exec(view, &env, &head, CALLER, HELPER, data.clone());
            std::hint::black_box(r.output);
        }
        let us = t.elapsed().as_secs_f64() * 1e6 / iters as f64;
        println!(
            "revm  {:<36} out {} (expected {})  gas {:>7}  {:>8.1} us/swap",
            case["label"].as_str().unwrap(),
            out,
            case["out"].as_str().unwrap(),
            r.gas_used,
            us
        );
    }
    Ok(())
}
