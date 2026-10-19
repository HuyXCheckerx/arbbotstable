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
//!
//! Every read is pinned to a block number, never `latest`. Public endpoints are load balanced
//! across nodes at different heights: a `latest` read can come from a node a block or more
//! behind the header the mirror is labelled with, and that state is then never re-read (the
//! next log scan starts after the header's block). A pinned read at a block a node has not
//! imported fails instead, and is retried.

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
use std::time::{Duration, Instant};

#[derive(Clone, Debug, Default)]
pub struct BlockCtx {
    pub number: u64,
    pub hash: B256,
    pub timestamp: u64,
    pub basefee: u64,
    pub gas_limit: u64,
    pub gas_used: u64,
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
    /// Bumped by every sync that writes. Simulations that all ran in one epoch saw one block.
    pub epoch: u64,
    /// The epoch of the last sync that re-read every cached account.
    pub full_epoch: u64,
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

/// The accounts one pool's simulation reads, and the token balance slots it reads (tokens
/// themselves are not members: their slots change on every transfer). When a member logs, or
/// one of those balances changes without a member's log being seen, the members are re-read
/// at the same block as the balances. DODO prices from `balanceOf(pool) - reserve`, so its
/// balances and reserves must come from one block.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Group {
    pub accounts: HashSet<Address>,
    pub token_slots: HashSet<(Address, U256)>,
}

/// The node no longer serves the mirror's block (it fell out of the node's state window, for
/// example after the host slept). Quotes are refused until a sync moves the mirror forward.
#[derive(Debug)]
pub struct NotCurrent(pub String);

impl std::fmt::Display for NotCurrent {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "mirrored block is outside the node's state window ({})", self.0)
    }
}

impl std::error::Error for NotCurrent {}

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
    /// Each pool's group (see `Group`). A v4 hook's storage changes on every swap, but only the
    /// PoolManager emits the event.
    pub groups: RwLock<Vec<Group>>,
    pub fetched_slots: AtomicU64,
    pub fetched_accounts: AtomicU64,
    pub fetch_rounds: AtomicU64,
    /// Cleared after a node refuses eth_getProof for the mirror's block (BSC serves proofs only
    /// for the newest blocks); reads then use eth_getStorageAt only.
    pub proofs: std::sync::atomic::AtomicBool,
    /// Cleared once a node refuses a proof even at the head block (BSC's PublicNode does).
    pub head_proofs: std::sync::atomic::AtomicBool,
    /// Accounts a sync re-read only because a token balance they price from moved while no log
    /// of theirs was seen.
    pub witness_rereads: AtomicU64,
    /// Accounts and slots a `load` inserted while a sync was reading; they were read at the
    /// old block, so the sync dropped them for re-reading.
    pub dropped_during_sync: AtomicU64,
    /// Log queries retried because the answering node had not imported the head block.
    pub log_retries: AtomicU64,
    /// Logs of these contracts are ignored by `sync`, as if a node had dropped them.
    #[cfg(test)]
    pub ignore_logs: RwLock<HashSet<Address>>,
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
    /// Accounts re-read because a token balance of theirs moved with no log of theirs seen.
    pub witness_accounts: usize,
    /// Accounts and slots loaded at the old block while this sync read, then dropped.
    pub dropped: usize,
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
            witness_rereads: AtomicU64::new(0),
            dropped_during_sync: AtomicU64::new(0),
            log_retries: AtomicU64::new(0),
            #[cfg(test)]
            ignore_logs: RwLock::new(HashSet::default()),
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
            hash: b.get("hash").map(hex_b256).transpose()?.unwrap_or_default(),
            timestamp: hex_u64(&b["timestamp"])?,
            basefee: b.get("baseFeePerGas").map(hex_u64).transpose()?.unwrap_or(0),
            gas_limit: hex_u64(&b["gasLimit"])?,
            gas_used: b.get("gasUsed").map(hex_u64).transpose()?.unwrap_or(0),
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

    /// (epoch, block): see `State::epoch`.
    pub fn epoch(&self) -> (u64, u64) {
        let st = self.state.read();
        (st.epoch, st.block)
    }

    pub fn full_epoch(&self) -> u64 {
        self.state.read().full_epoch
    }

    fn window_error(msg: &str) -> bool {
        // A refused proof only turns proofs off (see `read_storage`); these mean the block's
        // state is gone from the node.
        msg.contains("Archive requests") || msg.contains("missing trie node")
    }

    fn not_current(block: u64, msg: &str) -> anyhow::Error {
        NotCurrent(format!("block {block}: {}", &msg[..msg.len().min(160)])).into()
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
                    proof_reqs.push(("eth_getProof", json!([a, hex, tag(block)])));
                }
                plan.push((true, start, proof_reqs.len() - start));
            } else {
                let start = simple_reqs.len();
                simple_reqs.push(("eth_getBalance", json!([a, tag(block)])));
                for k in ks {
                    simple_reqs.push(("eth_getStorageAt", json!([a, B256::from(*k), tag(block)])));
                }
                plan.push((false, start, ks.len() + 1));
            }
        }
        let proofs = if proof_reqs.is_empty() { Ok(vec![]) } else { self.rpc.batch_n(&proof_reqs, 8) };
        let simple = if simple_reqs.is_empty() { Ok(vec![]) } else { self.rpc.batch(&simple_reqs) };
        for r in [&proofs, &simple] {
            if let Err(e) = r {
                if Self::window_error(&e.to_string()) {
                    return Err(Self::not_current(block, &e.to_string()));
                }
            }
        }
        let (proofs, simple) = (proofs?, simple?);
        let window = |v: &Value| v.get("__error").map(|e| Self::window_error(&e.to_string())).unwrap_or(false);
        if let Some(v) = simple.iter().chain(proofs.iter()).find(|v| window(v)) {
            return Err(Self::not_current(block, &v.to_string()));
        }
        let mut out = Vec::with_capacity(keys.len());
        for ((_, ks), (is_proof, start, n)) in keys.iter().zip(plan) {
            if is_proof {
                let mut vals = Vec::with_capacity(ks.len());
                let mut bal = U256::ZERO;
                for r in &proofs[start..start + n] {
                    if r.get("__error").is_some() {
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
                if let Some(e) = r.iter().find(|v| v.get("__error").is_some()) {
                    bail!("state read at block {block}: {e}");
                }
                let vals = r[1..].iter().map(hex_u256).collect::<Result<Vec<_>>>()?;
                out.push((hex_u256(&r[0])?, vals));
            }
        }
        Ok(out)
    }

    /// Fetches the recorded misses at the mirror's block and inserts them. Fails with
    /// `NotCurrent` when the node no longer has that block.
    pub fn load(&self, misses: Misses) -> Result<()> {
        let block = self.block();
        let accounts: Vec<Address> = misses.accounts.into_iter().collect();
        let slots: Vec<(Address, U256)> = misses.slots.into_iter().collect();
        let hashes: Vec<u64> = misses.hashes.into_iter().collect();
        let mut reqs: Vec<(&str, Value)> = Vec::new();
        for a in &accounts {
            reqs.push(("eth_getBalance", json!([a, tag(block)])));
            reqs.push(("eth_getTransactionCount", json!([a, tag(block)])));
            reqs.push(("eth_getCode", json!([a, tag(block)])));
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
            if Self::window_error(&e.to_string()) {
                return Err(Self::not_current(block, &e.to_string()));
            }
        }
        let (res, vals) = (res?, vals?);
        if let Some(v) = res.iter().find(|v| v.get("__error").is_some()) {
            if Self::window_error(&v.to_string()) {
                return Err(Self::not_current(block, &v.to_string()));
            }
            bail!("account read at block {block}: {v}");
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

    /// Contracts in `watched` that logged in (from, head], or None when a chunk's logs could not
    /// be read completely (the caller then re-reads everything). Each query is batched with a
    /// header read of `head` and only accepted when the hash matches: on a load-balanced
    /// endpoint the query can reach a node that has not imported `head` yet.
    fn emitters(&self, from: u64, head: &BlockCtx, watched: &[Address]) -> (Option<HashSet<Address>>, usize) {
        // PublicNode's Ethereum endpoint answers 403 to eth_getLogs with more than 8 addresses,
        // so the filter goes out in chunks of 8, four at a time.
        let chunks: Vec<&[Address]> = watched.chunks(8).collect();
        let query = |chunk: &[Address]| -> Option<Vec<Address>> {
            let reqs = [
                ("eth_getLogs", json!([{"fromBlock": tag(from + 1), "toBlock": tag(head.number), "address": chunk}])),
                ("eth_getBlockByNumber", json!([tag(head.number), false])),
            ];
            for attempt in 0..4u64 {
                if attempt > 0 {
                    self.log_retries.fetch_add(1, Ordering::Relaxed);
                    std::thread::sleep(Duration::from_millis(300 * attempt));
                }
                let Ok(r) = self.rpc.batch_n(&reqs, 2) else { continue };
                let seen = hex_b256(&r[1]["hash"]).ok();
                if r[0].get("__error").is_some() || seen != Some(head.hash) {
                    continue;
                }
                let Some(logs) = r[0].as_array() else { continue };
                return Some(logs.iter().filter_map(|l| crate::rpc::hex_addr(&l["address"]).ok()).collect());
            }
            None
        };
        let results: Vec<Option<Vec<Address>>> = std::thread::scope(|sc| {
            let mut out = Vec::new();
            for group in chunks.chunks(4) {
                let hs: Vec<_> = group.iter().map(|chunk| sc.spawn(move || query(chunk))).collect();
                out.extend(hs.into_iter().map(|h| h.join().unwrap()));
            }
            out
        });
        let mut found = HashSet::default();
        for r in results {
            match r {
                Some(v) => found.extend(v),
                None => return (None, chunks.len()),
            }
        }
        #[cfg(test)]
        found.retain(|a| !self.ignore_logs.read().contains(a));
        (Some(found), chunks.len())
    }

    /// Moves the mirror to the latest block. Re-reads cached slots of every contract that
    /// emitted a log since the last sync, of the hot token contracts, and of every group whose
    /// token balances moved. A gap wider than `full_after` blocks re-reads everything.
    pub fn sync(&self, full_after: u64, force_full: bool) -> Result<SyncReport> {
        self.sync_inner(None, full_after, force_full, || {})
    }

    /// `sync` to `target` (None: the latest block). `before_commit` runs after the reads and
    /// before the state is written (tests use it to interleave a `load`).
    pub fn sync_inner(&self, target: Option<u64>, full_after: u64, force_full: bool, before_commit: impl FnOnce()) -> Result<SyncReport> {
        let t0 = Instant::now();
        let head = self.header(target.map(tag).unwrap_or_else(|| json!("latest")))?;
        // What this sync covers. A `load` running concurrently inserts values read at the old
        // block; whatever it adds beyond this snapshot is dropped at commit and re-read later.
        let (from, watched, known) = {
            let st = self.state.read();
            let hot = self.hot.read();
            let watched: Vec<Address> = st
                .accounts
                .iter()
                .filter(|(a, info)| info.code_hash != KECCAK_EMPTY && !hot.contains(*a))
                .map(|(a, _)| *a)
                .collect();
            let known: HashSet<Address> = st.accounts.keys().copied().collect();
            (st.block, watched, known)
        };
        let mut rep = SyncReport { from, to: head.number, ..Default::default() };
        // Never step back: the header can come from a node behind the one the last sync used.
        if head.number < from || (head.number == from && !force_full) {
            return Ok(rep);
        }
        let mut full = force_full || head.number.saturating_sub(from) > full_after;
        let mut refresh: HashSet<Address> = self.hot.read().clone();
        let tl = Instant::now();
        if !full && !watched.is_empty() {
            let (found, calls) = self.emitters(from, &head, &watched);
            rep.log_calls = calls;
            match found {
                Some(f) => refresh.extend(f),
                None => full = true,
            }
            rep.emitters = refresh.len();
            // Only accounts that logged pull in their groups. Matching against the growing set
            // made it transitive and order dependent: groups share non-logging accounts (USDC's
            // implementation, read by every USDC pool), so one USDC pool's log re-read others.
            let emitted = refresh.clone();
            for g in self.groups.read().iter() {
                if g.accounts.iter().any(|a| emitted.contains(a)) {
                    refresh.extend(g.accounts.iter().copied());
                }
            }
        }
        rep.logs_ms = tl.elapsed().as_millis();
        if full {
            refresh.extend(watched.iter().copied());
        }
        let block = head.number;
        let keys_of = |accounts: &mut dyn Iterator<Item = Address>| -> Vec<(Address, Vec<U256>)> {
            let st = self.state.read();
            accounts
                .filter(|a| known.contains(a) && st.accounts.contains_key(a))
                .map(|a| (a, st.storage.get(&a).map(|m| m.keys().copied().collect()).unwrap_or_default()))
                .collect()
        };
        let mut keys = keys_of(&mut refresh.iter().copied());
        let tr = Instant::now();
        let mut vals = self.read_storage(&keys, block, true)?;
        // Token balances are re-read every block, a pool's own state only when it logs. A pool
        // whose balance moved while none of its logs was seen (a node dropped them, or a direct
        // transfer) is re-read now, at the same block.
        let moved: HashSet<(Address, U256)> = {
            let st = self.state.read();
            let mut c = HashSet::default();
            for ((a, ks), (_, vs)) in keys.iter().zip(&vals) {
                let Some(m) = st.storage.get(a) else { continue };
                for (k, v) in ks.iter().zip(vs) {
                    if m.get(k) != Some(v) {
                        c.insert((*a, *k));
                    }
                }
            }
            c
        };
        let extra: HashSet<Address> = self
            .groups
            .read()
            .iter()
            .filter(|g| g.token_slots.iter().any(|s| moved.contains(s)))
            .flat_map(|g| g.accounts.iter().copied())
            .filter(|a| !refresh.contains(a))
            .collect();
        if !extra.is_empty() {
            let more = keys_of(&mut extra.iter().copied());
            vals.extend(self.read_storage(&more, block, true)?);
            rep.witness_accounts = more.len();
            self.witness_rereads.fetch_add(more.len() as u64, Ordering::Relaxed);
            keys.extend(more);
        }
        rep.read_ms = tr.elapsed().as_millis();
        before_commit();
        let mut st = self.state.write();
        if st.block > block {
            return Ok(rep);
        }
        // Accounts first loaded after the snapshot were not covered by the log scan, and slots
        // added to a re-read account were not re-read: both hold old-block values.
        let late: Vec<Address> = st.accounts.keys().filter(|a| !known.contains(*a)).copied().collect();
        for a in &late {
            st.accounts.remove(a);
            rep.dropped += 1 + st.storage.remove(a).map(|m| m.len()).unwrap_or(0);
        }
        for ((a, ks), (bal, vs)) in keys.iter().zip(vals) {
            let mut changed = false;
            if let Some(info) = st.accounts.get_mut(a) {
                changed |= info.balance != bal;
                info.balance = bal;
            }
            let m = st.storage.entry(*a).or_default();
            if m.len() > ks.len() {
                let read: HashSet<&U256> = ks.iter().collect();
                let before = m.len();
                m.retain(|k, _| read.contains(k));
                rep.dropped += before - m.len();
            }
            for (k, v) in ks.iter().zip(vs) {
                changed |= m.insert(*k, v) != Some(v);
            }
            if changed {
                *st.versions.entry(*a).or_default() += 1;
                rep.changed_accounts += 1;
            }
            rep.refreshed_slots += ks.len();
        }
        self.dropped_during_sync.fetch_add(rep.dropped as u64, Ordering::Relaxed);
        rep.refreshed_accounts = keys.len();
        rep.full = full;
        st.block = block;
        st.header = head;
        st.epoch += 1;
        if full {
            st.full_epoch = st.epoch;
        }
        rep.ms = t0.elapsed().as_millis();
        Ok(rep)
    }

    pub fn add_group(&self, group: Group) {
        let mut g = self.groups.write();
        if !g.contains(&group) {
            g.push(group);
        }
    }

    pub fn stats(&self) -> (usize, usize) {
        let st = self.state.read();
        (st.accounts.len(), st.storage.values().map(|m| m.len()).sum())
    }
}

#[cfg(test)]
impl Mirror {
    /// Drops all chain state and pins the mirror to `block` (pinned synthetic state stays).
    pub fn reset_to(&self, block: u64) -> Result<()> {
        let h = self.header(tag(block))?;
        let mut st = self.state.write();
        st.accounts.clear();
        st.storage.clear();
        st.block_hashes.clear();
        st.block = h.number;
        st.header = h;
        st.epoch += 1;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::testnode::{Acct, Block, Chain, Node};
    use revm::primitives::Bytes;

    const TOKEN: Address = Address::repeat_byte(0x70);
    const POOL: Address = Address::repeat_byte(0x50);
    const OTHER: Address = Address::repeat_byte(0x0e);
    /// The token's slot holding the pool's balance, and the pool's reserve slot.
    const BAL: U256 = U256::from_limbs([7, 0, 0, 0]);
    const RES: U256 = U256::ZERO;
    const AUX: U256 = U256::from_limbs([1, 0, 0, 0]);

    fn contract(slots: &[(U256, u64)]) -> Acct {
        Acct { code: Bytes::from_static(&[0x60, 0x00]), storage: slots.iter().map(|(k, v)| (*k, U256::from(*v))).collect(), ..Default::default() }
    }

    /// Block 100: the pool holds 1000 tokens and its reserve says so.
    fn node() -> Node {
        let mut b = Block::default();
        b.accounts.insert(TOKEN, contract(&[(BAL, 1000)]));
        b.accounts.insert(POOL, contract(&[(RES, 1000), (AUX, 5)]));
        b.accounts.insert(OTHER, contract(&[]));
        let mut c = Chain { window: 128, ..Default::default() };
        c.blocks.insert(100, b);
        Node::start(c)
    }

    /// A mirror at the node's head with the token hot and the pool's group registered. With
    /// `witness`, the group records the token balance slot the pool prices from.
    fn mirror(node: &Node, witness: bool) -> Mirror {
        let m = Mirror::new(Rpc::new(&node.url));
        m.init().unwrap();
        m.hot.write().insert(TOKEN);
        let mut misses = Misses::default();
        misses.accounts.extend([TOKEN, POOL]);
        misses.slots.extend([(TOKEN, BAL), (POOL, RES)]);
        m.load(misses).unwrap();
        let token_slots = if witness { [(TOKEN, BAL)].into_iter().collect() } else { HashSet::default() };
        m.add_group(Group { accounts: [POOL].into_iter().collect(), token_slots });
        m
    }

    /// A swap of `amount` into the pool: its balance and its reserve move together.
    fn swap(node: &Node, amount: u64) -> u64 {
        node.chain.lock().unwrap().mine(&[POOL, TOKEN], |acc| {
            let t = acc.get_mut(&TOKEN).unwrap();
            let v = t.storage[&BAL] + U256::from(amount);
            t.storage.insert(BAL, v);
            let p = acc.get_mut(&POOL).unwrap();
            let r = p.storage[&RES] + U256::from(amount);
            p.storage.insert(RES, r);
            p.storage.insert(AUX, U256::from(6));
        })
    }

    fn slot(m: &Mirror, a: Address, k: U256) -> Option<u64> {
        m.state.read().storage.get(&a).and_then(|s| s.get(&k)).map(|v| v.to::<u64>())
    }

    /// The 2026-10-19 state: the pool's log for the swap block is not seen, the hot token is
    /// re-read anyway, and the pool's balance runs ahead of its reserve by the swap. DODO
    /// credits that difference as free input. Without the balance witness nothing re-reads the
    /// pool until it logs again.
    #[test]
    fn without_the_witness_a_missed_log_leaves_balance_and_reserve_a_block_apart() {
        let node = node();
        let m = mirror(&node, false);
        node.chain.lock().unwrap().dropped_logs.insert(POOL);
        swap(&node, 8505);
        let rep = m.sync(64, false).unwrap();
        assert_eq!(rep.to, 101);
        assert_eq!(slot(&m, TOKEN, BAL), Some(9505));
        assert_eq!(slot(&m, POOL, RES), Some(1000), "reserve stays at block 100");
    }

    #[test]
    fn a_pool_whose_balance_moved_without_a_seen_log_is_reread_at_the_same_block() {
        let node = node();
        let m = mirror(&node, true);
        node.chain.lock().unwrap().dropped_logs.insert(POOL);
        swap(&node, 8505);
        let rep = m.sync(64, false).unwrap();
        assert_eq!(rep.witness_accounts, 1);
        assert_eq!(slot(&m, TOKEN, BAL), Some(9505));
        assert_eq!(slot(&m, POOL, RES), Some(9505));
        assert_eq!(m.witness_rereads.load(Ordering::Relaxed), 1);
    }

    #[test]
    fn a_log_query_answered_by_a_lagging_node_is_retried() {
        let node = node();
        let m = mirror(&node, false);
        swap(&node, 8505);
        // That node has not imported block 101: it returns no logs for it, and no header.
        node.chain.lock().unwrap().lagging_log_requests = 1;
        let rep = m.sync(64, false).unwrap();
        assert!(!rep.full);
        assert_eq!(m.log_retries.load(Ordering::Relaxed), 1);
        assert_eq!(slot(&m, POOL, RES), Some(9505));
    }

    #[test]
    fn state_reads_are_pinned_and_a_lagging_node_is_retried_not_trusted() {
        let node = node();
        let m = mirror(&node, false);
        swap(&node, 8505);
        // At `latest` this node would answer with block 100's reserve.
        node.chain.lock().unwrap().lagging_state_requests = 2;
        let rep = m.sync(64, false).unwrap();
        assert_eq!(rep.to, 101);
        assert_eq!(slot(&m, POOL, RES), Some(9505));
        assert_eq!(slot(&m, TOKEN, BAL), Some(9505));
        assert_eq!(node.chain.lock().unwrap().latest_reads, 0);
    }

    /// After the host sleeps, the mirror's block is older than the node's state window. The old
    /// mirror switched every later read to `latest` for good; now the read fails as
    /// `NotCurrent` (a 503) and the next sync re-reads everything at a pinned head.
    #[test]
    fn an_out_of_window_block_fails_the_load_and_does_not_unpin_reads() {
        let node = node();
        let m = mirror(&node, true);
        for _ in 0..200 {
            node.chain.lock().unwrap().mine(&[], |_| {});
        }
        let mut misses = Misses::default();
        misses.slots.insert((POOL, AUX));
        let e = m.load(misses).unwrap_err();
        assert!(e.downcast_ref::<NotCurrent>().is_some(), "{e}");
        let rep = m.sync(64, false).unwrap();
        assert!(rep.full);
        assert_eq!(m.full_epoch(), m.epoch().0);
        let mut misses = Misses::default();
        misses.slots.insert((POOL, AUX));
        m.load(misses).unwrap();
        assert_eq!(slot(&m, POOL, AUX), Some(5));
        assert_eq!(node.chain.lock().unwrap().latest_reads, 0);
    }

    /// A `load` that runs while a sync is reading inserts values from the old block. The sync
    /// drops them at commit, so they are re-read at the new block instead of going stale.
    #[test]
    fn values_loaded_while_a_sync_reads_are_dropped() {
        let node = node();
        let m = mirror(&node, true);
        swap(&node, 8505);
        let rep = m
            .sync_inner(None, 64, false, || {
                let mut misses = Misses::default();
                misses.slots.insert((POOL, AUX));
                misses.accounts.insert(OTHER);
                m.load(misses).unwrap();
                assert_eq!(slot(&m, POOL, AUX), Some(5), "block 100's value");
            })
            .unwrap();
        assert_eq!(rep.dropped, 2);
        assert_eq!(slot(&m, POOL, AUX), None);
        assert!(!m.state.read().accounts.contains_key(&OTHER));
        let mut misses = Misses::default();
        misses.slots.insert((POOL, AUX));
        m.load(misses).unwrap();
        assert_eq!(slot(&m, POOL, AUX), Some(6));
    }

    /// Groups that share an account that never logs (a token implementation) are not re-read
    /// because the other group's pool logged.
    #[test]
    fn a_log_rereads_its_own_group_only() {
        let node = node();
        let m = mirror(&node, true);
        let mut misses = Misses::default();
        misses.accounts.insert(OTHER);
        misses.slots.insert((OTHER, AUX));
        m.load(misses).unwrap();
        // POOL's group (registered first) and OTHER's group both read SHARED.
        const SHARED: Address = Address::repeat_byte(0x5a);
        node.chain.lock().unwrap().mine(&[], |acc| {
            acc.insert(SHARED, contract(&[]));
        });
        m.sync(64, false).unwrap();
        let mut misses = Misses::default();
        misses.accounts.insert(SHARED);
        m.load(misses).unwrap();
        m.groups.write().clear();
        m.add_group(Group { accounts: [POOL, SHARED].into_iter().collect(), token_slots: HashSet::default() });
        m.add_group(Group { accounts: [OTHER, SHARED].into_iter().collect(), token_slots: HashSet::default() });
        node.chain.lock().unwrap().mine(&[POOL], |acc| {
            acc.get_mut(&POOL).unwrap().storage.insert(AUX, U256::from(9));
            acc.get_mut(&OTHER).unwrap().storage.insert(AUX, U256::from(9));
        });
        let rep = m.sync(64, false).unwrap();
        // TOKEN, POOL and SHARED; OTHER only joined before through SHARED.
        assert_eq!(rep.refreshed_accounts, 3);
        assert_eq!(slot(&m, OTHER, AUX), Some(0));
    }

    #[test]
    fn every_write_starts_a_new_epoch() {
        let node = node();
        let m = mirror(&node, true);
        let (e0, b0) = m.epoch();
        assert_eq!(m.sync(64, false).unwrap().to, b0, "no new block, no write");
        assert_eq!(m.epoch().0, e0);
        swap(&node, 1);
        m.sync(64, false).unwrap();
        assert_eq!(m.epoch(), (e0 + 1, b0 + 1));
        m.sync(64, true).unwrap();
        assert_eq!(m.full_epoch(), e0 + 2);
    }
}
