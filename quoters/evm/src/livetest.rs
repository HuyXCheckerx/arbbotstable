//! Live reproduction of the 2026-10-19 PYUSD/USDG misquote on Ethereum mainnet (PublicNode,
//! read-only). Ignored by default:
//!
//!     cargo test --release live_ -- --ignored --nocapture
//!
//! It picks a recent block `b` with one DODO PMM USDC/USDT swap and quotes PYUSD<->USDG at the
//! dry run's sizes in four states: everything at b-1; after a sync to b that never sees DODO's
//! log (the balance witness re-reads it); after a full re-read at b (the reference); and with
//! DODO's own storage put back to b-1 while the tokens stay at b. The last one is the state the
//! old mirror could reach, and it adds the swap's input to both directions.

use crate::chains::{Token, ETHEREUM};
use crate::guard::{Guard, Verdict};
use crate::optimizer::Params;
use crate::quote::{self, QuoteOut};
use crate::rpc::{hex_addr, hex_u64, tag};
use revm::primitives::{address, Address};
use serde_json::json;
use std::collections::BTreeMap;
use std::time::{Duration, Instant};

const DODO: Address = address!("04571c32a4e1c5f39bc3a238cb95b215058c432c");

fn params() -> Params {
    Params { steps: 20, max_splits: 4, precision_ppm: 10, max_sims: 4096, prune: true, gas_cost: 0.0, min_probe: 1, cutoff: 0.5, full_fill: false, via: None, max_hops: 3 }
}

fn show(tag: &str, q: &QuoteOut) {
    let legs: Vec<String> = q
        .legs
        .iter()
        .map(|l| format!("{}{} {} {}->{} {}%", if l.hop > 1 { "  " } else { "" }, l.label, &l.pool[..10], l.token_in, l.token_out, l.share_bps as f64 / 100.0))
        .collect();
    eprintln!("{tag:<28} block {} {} {} -> {} {}\n    {}", q.block, q.amount_in, q.input, q.amount_out, q.output, legs.join("\n    "));
}

fn out(q: &QuoteOut) -> u128 {
    q.out_atomic.parse().unwrap()
}

#[test]
#[ignore]
fn live_dodo_reserve_a_block_behind_its_balances_inflates_pyusd_usdg_both_ways() {
    let t0 = Instant::now();
    let s = quote::setup(&ETHEREUM, None, false).unwrap();
    let e = &s.engine;
    let m = &e.mirror;
    let (pyusd, usdg): (Token, Token) = (ETHEREUM.token("PYUSD").unwrap(), ETHEREUM.token("USDG").unwrap());

    // The largest recent block with exactly one DODO swap.
    let head = m.block();
    let logs = m.rpc.call("eth_getLogs", json!([{"fromBlock": tag(head - 40), "toBlock": tag(head - 1), "address": [DODO]}])).unwrap();
    let mut swaps: BTreeMap<u64, Vec<(Address, u128)>> = BTreeMap::new();
    for l in logs.as_array().unwrap() {
        // DODOSwap(fromToken, toToken, fromAmount, toAmount, trader, receiver); skip other events.
        let data = l["data"].as_str().unwrap().trim_start_matches("0x");
        if !l["topics"][0].as_str().unwrap_or("").starts_with("0xc2c0245e") || data.len() != 6 * 64 {
            continue;
        }
        let word = |i: usize| &data[i * 64..(i + 1) * 64];
        let from_token = hex_addr(&json!(format!("0x{}", &word(0)[24..]))).unwrap();
        let amount = u128::from_str_radix(word(2), 16).unwrap();
        swaps.entry(hex_u64(&l["blockNumber"]).unwrap()).or_default().push((from_token, amount));
    }
    let (b, (from_token, swapped)) = swaps
        .iter()
        .filter(|(_, v)| v.len() == 1)
        .map(|(b, v)| (*b, v[0]))
        .max_by_key(|(_, (_, a))| *a)
        .expect("no single-swap DODO block in the last 40 blocks");
    let sym = ETHEREUM.token_by_address(from_token).map(|t| t.symbol).unwrap_or("?");
    eprintln!("head {head}; block {b} has one DODO swap of {} {sym} in (setup {} ms)", quote::human(swapped, 6), t0.elapsed().as_millis());

    let p = params();
    let both = |tag: &str| -> (QuoteOut, QuoteOut) {
        let f = quote::quote(e, pyusd, usdg, 61_498_474_155, &p, &quote::market(e, usdg).unwrap()).unwrap();
        let r = quote::quote(e, usdg, pyusd, 39_999_008_000, &p, &quote::market(e, pyusd).unwrap()).unwrap();
        show(&format!("{tag} PYUSD->USDG"), &f);
        show(&format!("{tag} USDG->PYUSD"), &r);
        (f, r)
    };

    // 1. Everything pinned at b-1.
    m.reset_to(b - 1).unwrap();
    e.clear_cache();
    let (f0, r0) = both("b-1");
    let dodo_prev = m.state.read().storage.get(&DODO).cloned().expect("DODO was not simulated");

    let group = m.groups.read().iter().find(|g| g.accounts.contains(&DODO)).cloned().expect("DODO has no group");
    eprintln!("DODO's group: {} accounts, token slots {:?}", group.accounts.len(), group.token_slots);
    assert!(group.token_slots.iter().any(|(t, _)| *t == from_token));

    // 2. A sync to b that never sees DODO's log: the balance witness re-reads DODO at b. (If a
    // log query fails outright the sync re-reads everything instead; the test then retries.)
    m.ignore_logs.write().insert(DODO);
    let mut rep = m.sync_inner(Some(b), 64, false, || {}).unwrap();
    for _ in 0..3 {
        if !rep.full {
            break;
        }
        eprintln!("log scan incomplete, sync was a full re-read; again from b-1");
        m.reset_to(b - 1).unwrap();
        e.clear_cache();
        both("b-1 (again)");
        rep = m.sync_inner(Some(b), 64, false, || {}).unwrap();
    }
    eprintln!("sync to {b} without DODO's log: {rep:?}");
    assert!(!rep.full && rep.witness_accounts >= 1);
    let dodo_witness = m.state.read().storage.get(&DODO).cloned().unwrap();
    let (f1, r1) = both("b, witness");

    // 3. Reference: every cached account re-read at b.
    m.ignore_logs.write().clear();
    let rep = m.sync_inner(Some(b), 64, true, || {}).unwrap();
    eprintln!("full re-read at {b}: {} accounts, {} changed", rep.refreshed_accounts, rep.changed_accounts);
    assert_eq!(m.state.read().storage.get(&DODO), Some(&dodo_witness), "DODO after the witness re-read == full re-read");
    assert_ne!(Some(&dodo_witness), Some(&dodo_prev), "DODO's storage moved at b");
    let (f2, r2) = both("b, full re-read");
    assert_eq!(out(&f1), out(&f2));
    assert_eq!(out(&r1), out(&r2));

    // 4. The incident: DODO's reserves from b-1 next to token balances from b.
    {
        let mut st = m.state.write();
        st.storage.insert(DODO, dodo_prev);
        *st.versions.entry(DODO).or_default() += 1;
    }
    let (f3, r3) = both("b, DODO at b-1");
    let gain_f = out(&f3) as i128 - out(&f2) as i128;
    let gain_r = out(&r3) as i128 - out(&r2) as i128;
    eprintln!(
        "mixed state adds {} USDG and {} PYUSD (swap input {} {sym}); b-1 quotes were {} and {}",
        quote::human(gain_f.max(0) as u128, 6),
        quote::human(gain_r.max(0) as u128, 6),
        quote::human(swapped, 6),
        f0.amount_out,
        r0.amount_out
    );
    assert!(gain_f > swapped as i128 / 2 && gain_r > swapped as i128 / 2, "{gain_f} {gain_r}");
    assert!(f3.legs.iter().any(|l| l.pool.eq_ignore_ascii_case(&format!("{DODO:?}"))));

    // 5. The guard refuses the mixed-state quote once it deviates by more than 200 bp.
    let g = Guard::new(200.0, Duration::from_secs(600));
    let now = Instant::now();
    g.seed((pyusd.address, usdg.address, p.max_splits), 61_498_474_155, out(&f2), now);
    let v = g.check((pyusd.address, usdg.address, p.max_splits), 61_498_474_155, out(&f3), m.full_epoch(), now);
    eprintln!("guard on the mixed-state PYUSD->USDG quote: {v:?}");
    if gain_f as f64 / 61_498_474_155.0 * 1e4 > 200.0 {
        assert!(matches!(v, Verdict::Refuse { .. }));
    }
    eprintln!("done in {} s", t0.elapsed().as_secs());
}
