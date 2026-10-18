//! Mirrored chain state for local execution.
//!
//! Storage is loaded lazily, fork style: a simulation runs against whatever is
//! cached, every account or slot it touches that is not cached is recorded as a
//! miss (and read as empty), and the misses are fetched in one RPC batch at the
//! mirror's block before the simulation is run again. A simulation result is
//! only used once a run completes with no misses.
//!
//! `sync` keeps the cache current: it reads the logs of every mirrored contract
//! since the last block and re-reads all cached slots of the contracts that
//! emitted any, plus the configured tokens (their balances move on every
//! transfer). Synthetic state (the helper's code, balances and approvals) is
//! pinned and never overwritten by chain reads.

use crate::rpc::{hex_b256, hex_bytes, hex_u256, hex_u64, tag, Rpc};
use anyhow::{bail, Result};
use parking_lot::RwLock;
use revm::bytecode::Bytecode;
use revm::primitives::{keccak256, Address, HashMap, HashSet, B256, KECCAK_EMPTY, U256};
use revm::state::AccountInfo;
use revm::DatabaseRef;
use serde_json::{json, Value};
use std::cell::RefCell;
use std::convert::Infallible;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Instant;

#[derive(Clone, Debug, Default)]
pub struct BlockCtx {
    pub number: u64,
    pub timestamp: u64,
    pub basefee: u64,
    pub gas_limit: u64,
    pub prevrandao: B256,
    pub beneficiary: Address,
    /// `eth_gasPrice` when the header was read; local transactions pay this (some pools
    /// and hooks price by tx.gasprice, and BSC's base fee is 0).
    pub gas_price: u128,
}

#[derive(Default)]
pub struct State {
    /// Block every cached value corresponds to.
    pub block: u64,
    pub header: BlockCtx,
    pub accounts: HashMap<Address, AccountInfo>,
    pub storage: HashMap<Address, HashMap<U256, U256>>,
    pub pinned_accounts: HashMap<Address, AccountInfo>,
    pub pinned_storage: HashMap<Address, HashMap<U256, U256>>,
    pub block_hashes: HashMap<u64, B256>,
    /// Bumped whenever a sync finds an account's balance or a cached slot changed.
    pub versions: HashMap<Address, u64>,
}

#[derive(Default)]
pub struct Misses {
    pub accounts: HashSet<Address>,
    pub slots: HashSet<(Address, U256)>,
    pub hashes: HashSet<u64>,
}

impl Misses {
    pub fn is_empty(&self) -> bool {
        self.accounts.is_empty() && self.slots.is_empty() && self.hashes.is_empty()
    }
}

/// A read-only view of the mirror for one simulation; records misses.
#[derive(Clone, Copy)]
pub struct View<'a> {
    pub st: &'a State,
    pub misses: &'a RefCell<Misses>,
}

impl DatabaseRef for View<'_> {
    type Error = Infallible;

    fn basic_ref(&self, address: Address) -> Result<Option<AccountInfo>, Infallible> {
        if let Some(a) = self.st.pinned_accounts.get(&address) {
            return Ok(Some(a.clone()));
        }
        match self.st.accounts.get(&address) {
            Some(a) => Ok(Some(a.clone())),
            None => {
                self.misses.borrow_mut().accounts.insert(address);
                Ok(None)
            }
        }
    }

    fn code_by_hash_ref(&self, code_hash: B256) -> Result<Bytecode, Infallible> {
        for a in self.st.accounts.values().chain(self.st.pinned_accounts.values()) {
            if a.code_hash == code_hash {
                if let Some(c) = &a.code {
                    return Ok(c.clone());
                }
            }
        }
        Ok(Bytecode::default())
    }

    fn storage_ref(&self, address: Address, index: U256) -> Result<U256, Infallible> {
        if let Some(v) = self.st.pinned_storage.get(&address).and_then(|m| m.get(&index)) {
            return Ok(*v);
        }
        if self.st.pinned_accounts.contains_key(&address) {
            return Ok(U256::ZERO);
        }
        match self.st.storage.get(&address).and_then(|m| m.get(&index)) {
            Some(v) => Ok(*v),
            None => {
                self.misses.borrow_mut().slots.insert((address, index));
                Ok(U256::ZERO)
            }
        }
    }

    fn block_hash_ref(&self, number: u64) -> Result<B256, Infallible> {
        match self.st.block_hashes.get(&number) {
            Some(h) => Ok(*h),
            None => {
                self.misses.borrow_mut().hashes.insert(number);
                Ok(B256::ZERO)
            }
        }
    }
}

pub struct Mirror {
    pub rpc: Rpc,
    pub state: RwLock<State>,
    /// Token contracts whose cached slots are re-read every block.
    pub hot: RwLock<HashSet<Address>>,
    /// Accounts each pool's simulation reads. When any of them logs, all are re-read: a v4
    /// hook's storage changes on every swap, but only the PoolManager emits the event.
    pub groups: RwLock<Vec<HashSet<Address>>>,
    pub fetched_slots: AtomicU64,
    pub fetched_accounts: AtomicU64,
    pub fetch_rounds: AtomicU64,
    /// Cleared after a node refuses eth_getProof for the mirror's block (BSC serves proofs only
    /// for the newest blocks); reads then use eth_getStorageAt only.
    pub proofs: std::sync::atomic::AtomicBool,
    /// Cleared once a node refuses a proof even at the head block (BSC's PublicNode does).
    pub head_proofs: std::sync::atomic::AtomicBool,
    /// Set when the node's state window is shorter than our reads take (BSC: ~128 blocks at
    /// 0.45 s, i.e. under a minute, on PublicNode): reads then use the `latest` tag, so slots
    /// read in one pass may come from a block or two apart.
    pub latest_reads: std::sync::atomic::AtomicBool,
}

pub fn account_from(balance: U256, nonce: u64, code: revm::primitives::Bytes) -> AccountInfo {
    if code.is_empty() {
        AccountInfo { balance, nonce, code_hash: KECCAK_EMPTY, code: Some(Bytecode::default()), account_id: None }
    } else {
        let hash = keccak256(&code);
        AccountInfo { balance, nonce, code_hash: hash, code: Some(Bytecode::new_raw(code)), account_id: None }
    }
}

#[derive(Debug, Default, Clone)]
pub struct SyncReport {
    pub from: u64,
    pub to: u64,
    pub emitters: usize,
    pub refreshed_accounts: usize,
    pub refreshed_slots: usize,
    pub full: bool,
    pub ms: u128,
    pub logs_ms: u128,
    pub changed_accounts: usize,
    pub log_calls: usize,
    pub read_ms: u128,
}

impl Mirror {
    pub fn new(rpc: Rpc) -> Self {
        Mirror {
            rpc,
            state: RwLock::new(State::default()),
            hot: RwLock::new(HashSet::default()),
            groups: RwLock::new(Vec::new()),
            fetched_slots: AtomicU64::new(0),
            fetched_accounts: AtomicU64::new(0),
            fetch_rounds: AtomicU64::new(0),
            proofs: std::sync::atomic::AtomicBool::new(true),
            head_proofs: std::sync::atomic::AtomicBool::new(true),
            latest_reads: std::sync::atomic::AtomicBool::new(false),
        }
    }

    pub fn header(&self, block: Value) -> Result<BlockCtx> {
        let r = self.rpc.batch(&[("eth_getBlockByNumber", json!([block, false])), ("eth_gasPrice", json!([]))])?;
        let b = r[0].clone();
        let gas_price = hex_u256(&r[1]).map(|g| g.to::<u128>()).unwrap_or(0);
        if b.is_null() {
            bail!("block not found");
        }
        Ok(BlockCtx {
            number: hex_u64(&b["number"])?,
            timestamp: hex_u64(&b["timestamp"])?,
            basefee: b.get("baseFeePerGas").map(hex_u64).transpose()?.unwrap_or(0),
            gas_limit: hex_u64(&b["gasLimit"])?,
            prevrandao: b.get("mixHash").map(hex_b256).transpose()?.unwrap_or_default(),
            beneficiary: crate::rpc::hex_addr(&b["miner"])?,
            gas_price,
        })
    }

    /// Pins the mirror to the latest block. Call once before loading anything.
    pub fn init(&self) -> Result<()> {
        let h = self.header(json!("latest"))?;
        let mut st = self.state.write();
        st.block = h.number;
        st.header = h;
        Ok(())
    }

    pub fn block(&self) -> u64 {
        self.state.read().block
    }

    fn switch_to_latest(&self, why: &str) {
        if !self.latest_reads.swap(true, Ordering::Relaxed) {
            eprintln!("node state window too short for pinned reads ({}); reading at latest", &why[..why.len().min(160)]);
            self.proofs.store(true, Ordering::Relaxed);
            self.head_proofs.store(true, Ordering::Relaxed);
        }
    }

    fn at(&self, block: u64) -> Value {
        if self.latest_reads.load(Ordering::Relaxed) { json!("latest") } else { tag(block) }
    }

    fn window_error(msg: &str) -> bool {
        // A refused proof only turns proofs off (see `read_storage`); these mean the block's
        // state is gone from the node.
        msg.contains("Archive requests") || msg.contains("missing trie node")
    }

    /// Reads (balance, slot values) for each account at `block`. Accounts with several slots
    /// use one eth_getProof each (one item instead of one per slot: public endpoints rate-limit
    /// per batch item); the rest use eth_getBalance and eth_getStorageAt.
    /// `head` reads (sync, at the latest block) always try proofs; reads at the mirror's older
    /// block skip them once a node has refused one for its proof window.
    pub fn read_storage(&self, keys: &[(Address, Vec<U256>)], block: u64, head: bool) -> Result<Vec<(U256, Vec<U256>)>> {
        const PROOF_MIN: usize = 4;
        const PROOF_MAX: usize = 400;
        let mut proof_reqs: Vec<(&str, Value)> = Vec::new();
        let mut simple_reqs: Vec<(&str, Value)> = Vec::new();
        let mut plan: Vec<(bool, usize, usize)> = Vec::new();
        let use_proofs = if head { self.head_proofs.load(Ordering::Relaxed) } else { self.proofs.load(Ordering::Relaxed) };
        for (a, ks) in keys {
            if use_proofs && ks.len() >= PROOF_MIN {
                let start = proof_reqs.len();
                for part in ks.chunks(PROOF_MAX) {
                    let hex: Vec<B256> = part.iter().map(|k| B256::from(*k)).collect();
                    proof_reqs.push(("eth_getProof", json!([a, hex, self.at(block)])));
                }
                plan.push((true, start, proof_reqs.len() - start));
            } else {
                let start = simple_reqs.len();
                simple_reqs.push(("eth_getBalance", json!([a, self.at(block)])));
                for k in ks {
                    simple_reqs.push(("eth_getStorageAt", json!([a, B256::from(*k), self.at(block)])));
                }
                plan.push((false, start, ks.len() + 1));
            }
        }
        let proofs = if proof_reqs.is_empty() { Ok(vec![]) } else { self.rpc.batch_n(&proof_reqs, 8) };
        let simple = if simple_reqs.is_empty() { Ok(vec![]) } else { self.rpc.batch(&simple_reqs) };
        let latest = self.latest_reads.load(Ordering::Relaxed);
        for r in [&proofs, &simple] {
            if let Err(e) = r {
                if Self::window_error(&e.to_string()) && !latest {
                    self.switch_to_latest(&e.to_string());
                    return self.read_storage(keys, block, head);
                }
            }
        }
        let (proofs, simple) = (proofs?, simple?);
        let window = |v: &Value| v.get("__error").map(|e| Self::window_error(&e.to_string())).unwrap_or(false);
        if let Some(v) = simple.iter().find(|v| window(v)).or_else(|| proofs.iter().find(|v| window(v) && !v.to_string().contains("proof window"))).filter(|_| !latest) {
            self.switch_to_latest(&v.to_string());
            return self.read_storage(keys, block, head);
        }
        let mut out = Vec::with_capacity(keys.len());
        for ((_, ks), (is_proof, start, n)) in keys.iter().zip(plan) {
            if is_proof {
                let mut vals = Vec::with_capacity(ks.len());
                let mut bal = U256::ZERO;
                for r in &proofs[start..start + n] {
                    if r.get("__error").is_some() {
                        if Self::window_error(&r.to_string()) && !latest {
                            self.switch_to_latest(&r.to_string());
                            return self.read_storage(keys, block, head);
                        }
                        if r.to_string().contains("proof window") {
                            self.proofs.store(false, Ordering::Relaxed);
                            if head {
                                self.head_proofs.store(false, Ordering::Relaxed);
                            }
                            return self.read_storage(keys, block, head);
                        }
                        bail!("eth_getProof: {r}");
                    }
                    bal = hex_u256(&r["balance"])?;
                    for sp in r["storageProof"].as_array().into_iter().flatten() {
                        vals.push(hex_u256(&sp["value"])?);
                    }
                }
                if vals.len() != ks.len() {
                    bail!("eth_getProof returned {} of {} slots", vals.len(), ks.len());
                }
                out.push((bal, vals));
            } else {
                let r = &simple[start..start + n];
                let vals = r[1..].iter().map(hex_u256).collect::<Result<Vec<_>>>()?;
                out.push((hex_u256(&r[0])?, vals));
            }
        }
        Ok(out)
    }

    /// Fetches the recorded misses at the mirror's block and inserts them.
    pub fn load(&self, misses: Misses) -> Result<()> {
        let block = self.block();
        let accounts: Vec<Address> = misses.accounts.into_iter().collect();
        let slots: Vec<(Address, U256)> = misses.slots.into_iter().collect();
        let hashes: Vec<u64> = misses.hashes.into_iter().collect();
        let mut reqs: Vec<(&str, Value)> = Vec::new();
        for a in &accounts {
            reqs.push(("eth_getBalance", json!([a, self.at(block)])));
            reqs.push(("eth_getTransactionCount", json!([a, self.at(block)])));
            reqs.push(("eth_getCode", json!([a, self.at(block)])));
        }
        for n in &hashes {
            reqs.push(("eth_getBlockByNumber", json!([tag(*n), false])));
        }
        let mut by_addr: HashMap<Address, Vec<U256>> = HashMap::default();
        for (a, k) in &slots {
            by_addr.entry(*a).or_default().push(*k);
        }
        let grouped: Vec<(Address, Vec<U256>)> = by_addr.into_iter().collect();
        let (res, vals) = std::thread::scope(|sc| {
            let h = sc.spawn(|| self.read_storage(&grouped, block, false));
            let r = if reqs.is_empty() { Ok(vec![]) } else { self.rpc.batch(&reqs) };
            (r, h.join().unwrap())
        });
        if let Err(e) = &res {
            if Self::window_error(&e.to_string()) && !self.latest_reads.load(Ordering::Relaxed) {
                // The caller re-runs and loads again, now at latest.
                self.switch_to_latest(&e.to_string());
                return Ok(());
            }
        }
        let (res, vals) = (res?, vals?);
        if let Some(v) = res.iter().find(|v| v.get("__error").map(|e| Self::window_error(&e.to_string())).unwrap_or(false)) {
            self.switch_to_latest(&v.to_string());
            return Ok(());
        }
        self.fetch_rounds.fetch_add(1, Ordering::Relaxed);
        self.fetched_accounts.fetch_add(accounts.len() as u64, Ordering::Relaxed);
        self.fetched_slots.fetch_add(slots.len() as u64, Ordering::Relaxed);
        let mut st = self.state.write();
        if st.block != block {
            // A sync moved the mirror meanwhile; the caller re-runs and reloads what is still missing.
            return Ok(());
        }
        let mut i = 0;
        for a in &accounts {
            let info = account_from(hex_u256(&res[i])?, hex_u64(&res[i + 1])?, hex_bytes(&res[i + 2])?);
            st.accounts.insert(*a, info);
            i += 3;
        }
        for ((a, ks), (_, vs)) in grouped.iter().zip(vals) {
            let m = st.storage.entry(*a).or_default();
            for (k, v) in ks.iter().zip(vs) {
                m.insert(*k, v);
            }
        }
        for n in &hashes {
            st.block_hashes.insert(*n, hex_b256(&res[i]["hash"])?);
            i += 1;
        }
        Ok(())
    }

    /// Runs `f` against the mirror until it completes without touching uncached state.
    pub fn run<R>(&self, mut f: impl FnMut(View<'_>) -> R) -> Result<R> {
        for _ in 0..64 {
            let misses = RefCell::new(Misses::default());
            let r = {
                let st = self.state.read();
                let view = View { st: &st, misses: &misses };
                f(view)
            };
            let m = misses.into_inner();
            if m.is_empty() {
                return Ok(r);
            }
            self.load(m)?;
        }
        bail!("simulation kept touching new state after 64 load rounds")
    }

    pub fn pin_account(&self, addr: Address, info: AccountInfo) {
        self.state.write().pinned_accounts.insert(addr, info);
    }

    pub fn pin_slot(&self, addr: Address, key: U256, value: U256) {
        self.state.write().pinned_storage.entry(addr).or_default().insert(key, value);
    }

    /// Moves the mirror to the latest block. Re-reads cached slots of every contract that
    /// emitted a log since the last sync, and of the hot token contracts. A gap wider than
    /// `full_after` blocks re-reads everything.
    pub fn sync(&self, full_after: u64, force_full: bool) -> Result<SyncReport> {
        let t0 = Instant::now();
        let head = self.header(json!("latest"))?;
        let (from, watched) = {
            let st = self.state.read();
            let hot = self.hot.read();
            let watched: Vec<Address> = st
                .accounts
                .iter()
                .filter(|(a, info)| info.code_hash != KECCAK_EMPTY && !hot.contains(*a))
                .map(|(a, _)| *a)
                .collect();
            (st.block, watched)
        };
        let mut rep = SyncReport { from, to: head.number, ..Default::default() };
        if head.number <= from && !force_full {
            return Ok(rep);
        }
        let mut full = force_full || head.number.saturating_sub(from) > full_after;
        let mut refresh: HashSet<Address> = self.hot.read().clone();
        let tl = Instant::now();
        if !full && !watched.is_empty() {
            // PublicNode's Ethereum endpoint answers 403 to eth_getLogs with more than 8 addresses,
            // so the filter goes out in chunks of 8, four at a time.
            let chunks: Vec<&[Address]> = watched.chunks(8).collect();
            rep.log_calls = chunks.len();
            let results: Vec<Result<Value>> = std::thread::scope(|sc| {
                let mut out = Vec::new();
                for group in chunks.chunks(4) {
                    let hs: Vec<_> = group
                        .iter()
                        .map(|chunk| {
                            sc.spawn(move || {
                                self.rpc.call(
                                    "eth_getLogs",
                                    json!([{"fromBlock": tag(from + 1), "toBlock": tag(head.number), "address": chunk}]),
                                )
                            })
                        })
                        .collect();
                    out.extend(hs.into_iter().map(|h| h.join().unwrap()));
                }
                out
            });
            for r in results {
                match r {
                    Ok(logs) => {
                        for l in logs.as_array().into_iter().flatten() {
                            if let Ok(a) = crate::rpc::hex_addr(&l["address"]) {
                                refresh.insert(a);
                            }
                        }
                    }
                    Err(_) => full = true,
                }
            }
            rep.emitters = refresh.len();
            for g in self.groups.read().iter() {
                if g.iter().any(|a| refresh.contains(a)) {
                    refresh.extend(g.iter().copied());
                }
            }
        }
        rep.logs_ms = tl.elapsed().as_millis();
        if full {
            refresh.extend(watched.iter().copied());
        }
        let block = head.number;
        let keys: Vec<(Address, Vec<U256>)> = {
            let st = self.state.read();
            refresh
                .iter()
                .filter(|a| st.accounts.contains_key(*a))
                .map(|a| (*a, st.storage.get(a).map(|m| m.keys().copied().collect()).unwrap_or_default()))
                .collect()
        };
        let tr = Instant::now();
        let vals = self.read_storage(&keys, block, true)?;
        rep.read_ms = tr.elapsed().as_millis();
        let mut st = self.state.write();
        for ((a, ks), (bal, vs)) in keys.iter().zip(vals) {
            let mut changed = false;
            if let Some(info) = st.accounts.get_mut(a) {
                changed |= info.balance != bal;
                info.balance = bal;
            }
            let m = st.storage.entry(*a).or_default();
            for (k, v) in ks.iter().zip(vs) {
                changed |= m.insert(*k, v) != Some(v);
            }
            if changed {
                *st.versions.entry(*a).or_default() += 1;
                rep.changed_accounts += 1;
            }
            rep.refreshed_slots += ks.len();
        }
        rep.refreshed_accounts = keys.len();
        rep.full = full;
        st.block = block;
        st.header = head;
        rep.ms = t0.elapsed().as_millis();
        Ok(rep)
    }

    pub fn add_group(&self, accounts: HashSet<Address>) {
        let mut g = self.groups.write();
        if !g.contains(&accounts) {
            g.push(accounts);
        }
    }

    pub fn stats(&self) -> (usize, usize) {
        let st = self.state.read();
        (st.accounts.len(), st.storage.values().map(|m| m.len()).sum())
    }
}
