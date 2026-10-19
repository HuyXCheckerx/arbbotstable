//! Local swap simulation: the helper contract swaps against real pool code in revm.

use crate::chains::{Chain, Token};
use crate::evm::{exec, revert_reason, ChainEnv};
use crate::mirror::{account_from, Group, Mirror, NotCurrent};
use crate::venues::{IHelper, IERC20, Kind, Pool};
use alloy_sol_types::SolCall;
use anyhow::{anyhow, bail, Result};
use revm::primitives::{address, Address, Bytes, HashMap, U256};
use std::sync::Arc;

/// Synthetic addresses with no code on any supported chain.
pub const HELPER: Address = address!("00000000000000000000000000000000c0ffee01");
pub const CALLER: Address = address!("00000000000000000000000000000000c0ffee02");

const HELPER_HEX: &str = include_str!("helper.hex");

/// A simulation could not read the state it needed (an RPC failure, or the node no longer has
/// the mirror's block). Unlike a revert, it says nothing about the pool and is not cached; a
/// quote that hits one is refused rather than served from the pools that did load.
#[derive(Debug)]
pub struct ReadFailed {
    pub not_current: bool,
    pub msg: String,
}

impl std::fmt::Display for ReadFailed {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        let why = if self.not_current { " (the node no longer serves the mirrored block)" } else { "" };
        write!(f, "state read failed{why}: {}", self.msg)
    }
}

impl std::error::Error for ReadFailed {}

/// Cache version of a pool: (sum of its accounts' versions, hash of the token balances it reads).
type Version = (u64, u64);

#[derive(Clone, Debug, Default)]
pub struct Sim {
    pub out: u128,
    pub spent: u128,
    pub gas: u64,
}

pub struct Engine {
    pub chain: &'static Chain,
    pub env: ChainEnv,
    pub mirror: Arc<Mirror>,
    pub pools: Vec<Pool>,
    balance_slots: HashMap<Address, U256>,
    pub sims: std::sync::atomic::AtomicU64,
    /// Sqrt-price distance at which concentrated-liquidity swaps stop (0 = protocol bound).
    pub limit_bps: std::sync::atomic::AtomicU16,
    approved: parking_lot::Mutex<std::collections::HashSet<(Address, Address)>>,
    grouped: parking_lot::Mutex<std::collections::HashSet<String>>,
    /// Accounts each pool's simulations read (tokens, helper and caller excluded), and the token
    /// slots they read (balances; pinned helper slots excluded), sorted.
    groups: parking_lot::Mutex<HashMap<String, (Vec<Address>, Vec<(Address, U256)>)>>,
    /// Simulation cache across quotes: (pool, input token, amount, price limit) ->
    /// (pool state version, block, result). See `simulate_raw`.
    cache: parking_lot::Mutex<HashMap<(String, Address, u128, u16), (Version, u64, Result<Sim, String>)>>,
    pub cache_hits: std::sync::atomic::AtomicU64,
    pub cache_misses: std::sync::atomic::AtomicU64,
}

fn mint_amount(t: &Token) -> U256 {
    // 10^12 whole tokens: far above any quoted size, far below packed-balance flag bits.
    U256::from(10u64).pow(U256::from(12 + t.decimals as u64))
}

impl Engine {
    pub fn new(chain: &'static Chain, mirror: Arc<Mirror>, pools: Vec<Pool>) -> Result<Self> {
        let code = Bytes::from(revm::primitives::hex::decode(HELPER_HEX.trim())?);
        mirror.pin_account(HELPER, account_from(U256::ZERO, 1, code));
        mirror.pin_account(CALLER, account_from(U256::from(10u128.pow(30)), 0, Bytes::new()));
        let mut e = Engine {
            chain,
            env: chain.env(),
            mirror,
            pools,
            balance_slots: HashMap::default(),
            sims: Default::default(),
            limit_bps: std::sync::atomic::AtomicU16::new(100),
            approved: Default::default(),
            grouped: Default::default(),
            groups: Default::default(),
            cache: Default::default(),
            cache_hits: Default::default(),
            cache_misses: Default::default(),
        };
        let funded: Vec<Token> = chain.tokens.iter().copied().chain([chain.native]).collect();
        for t in &funded {
            e.fund(t)?;
        }
        {
            let mut hot = e.mirror.hot.write();
            for t in &funded {
                hot.insert(t.address);
            }
        }
        Ok(e)
    }

    pub fn call(&self, to: Address, data: Vec<u8>) -> Result<(bool, Bytes, u64)> {
        let head = self.mirror.state.read().header.clone();
        let data = Bytes::from(data);
        self.mirror.run(|view| {
            let r = exec(view, &self.env, &head, CALLER, to, data.clone());
            (r.success, r.output, r.gas_used)
        })
    }

    fn balance_of(&self, token: Address, owner: Address) -> Result<U256> {
        let (ok, out, _) = self.call(token, IERC20::balanceOfCall { owner }.abi_encode())?;
        if !ok || out.len() < 32 {
            bail!("balanceOf failed on {token}");
        }
        Ok(U256::from_be_slice(&out[..32]))
    }

    /// Finds the token's balance slot for the helper by reading `balanceOf(helper)` and
    /// testing each slot that call loaded, then pins a large balance there.
    fn fund(&mut self, t: &Token) -> Result<()> {
        let head = self.mirror.state.read().header.clone();
        let data = Bytes::from(IERC20::balanceOfCall { owner: HELPER }.abi_encode());
        let touched: Vec<(Address, U256)> = self.mirror.run(|view| {
            let r = exec(view, &self.env, &head, CALLER, t.address, data.clone());
            r.state
                .iter()
                .flat_map(|(a, acc)| acc.storage.keys().map(move |k| (*a, *k)))
                .collect::<Vec<_>>()
        })?;
        let magic = U256::from(0x5eed_1234_u64);
        for (a, k) in touched {
            self.mirror.pin_slot(a, k, magic);
            let b = self.balance_of(t.address, HELPER).unwrap_or_default();
            if b == magic {
                self.mirror.pin_slot(a, k, mint_amount(t));
                self.balance_slots.insert(t.address, k);
                return Ok(());
            }
            self.mirror.state.write().pinned_storage.get_mut(&a).map(|m| m.remove(&k));
        }
        bail!("could not find the balance slot of {} ({})", t.symbol, t.address)
    }

    /// Runs `helper.approve(token, spender)` and pins the storage it writes.
    pub fn approve(&self, token: Address, spender: Address) -> Result<()> {
        let head = self.mirror.state.read().header.clone();
        let data = Bytes::from(IHelper::approveCall { token, spender }.abi_encode());
        let writes = self.mirror.run(|view| {
            let r = exec(view, &self.env, &head, CALLER, HELPER, data.clone());
            let w: Vec<(Address, U256, U256)> = r
                .state
                .iter()
                .filter(|(a, _)| **a == token)
                .flat_map(|(a, acc)| {
                    acc.storage
                        .iter()
                        .filter(|(_, s)| s.present_value != s.original_value)
                        .map(move |(k, s)| (*a, *k, s.present_value))
                })
                .collect();
            (r.success, w, r.output)
        })?;
        if !writes.0 {
            bail!("approve {token} for {spender} failed: {}", revert_reason(&writes.2));
        }
        for (a, k, v) in writes.1 {
            self.mirror.pin_slot(a, k, v);
        }
        Ok(())
    }

    pub fn prepare(&self, pool: &Pool) -> Result<()> {
        let graph: Vec<Address> = self.chain.tokens.iter().map(|t| t.address).collect();
        for (t, s) in pool.approvals(&graph) {
            // USDT refuses to change a non-zero allowance, and pools often share a spender (a vault).
            if self.approved.lock().insert((t, s)) {
                self.approve(t, s)?;
            }
        }
        Ok(())
    }

    /// Simulates an exact-input swap of `amount` through `pool`.
    pub fn simulate(&self, pool: &Pool, tin: Address, tout: Address, amount: u128) -> Result<Sim> {
        let sim = self.simulate_raw(pool, tin, tout, amount)?;
        // The Sky PSM takes USDS in whole USDC units (1e12 wei); the dust stays unspent.
        let dust = if matches!(pool.kind, Kind::SkyPsm { usds, .. } if usds == tin) { 1_000_000_000_000u128 } else { 0 };
        if sim.spent > amount || amount - sim.spent > dust {
            bail!("pool took {} of {amount}", sim.spent);
        }
        Ok(sim)
    }

    /// Pools whose price moves with time or block context (interest accrual, oracles, hooks,
    /// adaptive fees, gas-price-dependent fees) are re-simulated every block; the rest only
    /// when an account they read changed.
    fn time_dependent(pool: &Pool) -> bool {
        match &pool.kind {
            Kind::V3 { .. } => pool.venue.contains("QuickSwap"),
            Kind::V4 { hooks, .. } => *hooks != Address::ZERO,
            Kind::Infinity { hooks, .. } => *hooks != Address::ZERO,
            Kind::Dodo { .. } => false,
            Kind::Maverick { .. } => true,
            Kind::SkyPsm { .. } => false,
            Kind::Curve { .. } | Kind::Fluid { .. } | Kind::Balancer { .. } => true,
        }
    }

    fn pool_version(&self, pool: &Pool) -> Option<Version> {
        use std::hash::{Hash, Hasher};
        let groups = self.groups.lock();
        let (accounts, token_slots) = groups.get(&pool.id)?;
        let st = self.mirror.state.read();
        // Token versions would change on every transfer of the token anywhere, so the pool's
        // own balance slots stand in for them: DODO's price moves with them.
        let mut h = std::hash::DefaultHasher::new();
        for (t, k) in token_slots {
            st.storage.get(t).and_then(|m| m.get(k)).hash(&mut h);
        }
        Some((accounts.iter().map(|a| st.versions.get(a).copied().unwrap_or(0)).sum(), h.finish()))
    }

    /// Like `simulate`, but also returns partial fills (a swap stopped at the price limit).
    pub fn simulate_raw(&self, pool: &Pool, tin: Address, tout: Address, amount: u128) -> Result<Sim> {
        let key = (pool.id.clone(), tin, amount, self.limit());
        let block = self.mirror.block();
        let version = self.pool_version(pool);
        if let Some(v) = version {
            if let Some((ver, blk, r)) = self.cache.lock().get(&key) {
                if *ver == v && (!Self::time_dependent(pool) || *blk == block) {
                    self.cache_hits.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
                    return r.clone().map_err(|e| anyhow!(e));
                }
            }
        }
        self.cache_misses.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        // Read failures propagate and are not cached; reverts are a property of the pool.
        let r = self.simulate_uncached(pool, tin, tout, amount)?;
        if let Some(v) = version.or_else(|| self.pool_version(pool)) {
            if self.mirror.block() == block {
                self.cache.lock().insert(key, (v, block, r.as_ref().map(|s| s.clone()).map_err(|e| e.to_string())));
            }
        }
        r
    }

    /// Outer error: the state could not be read (`ReadFailed`). Inner: the swap's outcome.
    fn simulate_uncached(&self, pool: &Pool, tin: Address, tout: Address, amount: u128) -> Result<Result<Sim>> {
        let data = pool.helper_call(HELPER, tin, tout, U256::from(amount), self.limit())?;
        self.sims.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        let head = self.mirror.state.read().header.clone();
        let skip: Vec<Address> = [HELPER, CALLER, self.chain.native.address]
            .into_iter()
            .chain(self.chain.tokens.iter().map(|t| t.address))
            .collect();
        let (ok, out, halt, touched) = self
            .mirror
            .run(|view| {
                let r = exec(view, &self.env, &head, CALLER, HELPER, data.clone());
                let mut g = Group::default();
                if r.success && !self.grouped.lock().contains(&pool.id) {
                    for (a, acc) in r.state.iter() {
                        if *a == HELPER || *a == CALLER || *a == head.beneficiary {
                            continue;
                        }
                        if skip.contains(a) {
                            let pinned = view.st.pinned_storage.get(a);
                            g.token_slots.extend(acc.storage.keys().filter(|k| !pinned.is_some_and(|m| m.contains_key(*k))).map(|k| (*a, *k)));
                        } else {
                            g.accounts.insert(*a);
                        }
                    }
                }
                (r.success, r.output, r.halt, g)
            })
            .map_err(|e| ReadFailed { not_current: e.downcast_ref::<NotCurrent>().is_some(), msg: e.to_string() })?;
        if !touched.accounts.is_empty() && self.grouped.lock().insert(pool.id.clone()) {
            let mut slots: Vec<(Address, U256)> = touched.token_slots.iter().copied().collect();
            slots.sort();
            self.groups.lock().insert(pool.id.clone(), (touched.accounts.iter().copied().collect(), slots));
            self.mirror.add_group(touched);
        }
        if !ok {
            return Ok(Err(anyhow!("{}", halt.unwrap_or_else(|| revert_reason(&out)))));
        }
        if out.len() < 96 {
            return Ok(Err(anyhow!("short helper output")));
        }
        let w = |i: usize| U256::from_be_slice(&out[i * 32..i * 32 + 32]);
        Ok(Ok(Sim { out: w(0).to::<u128>(), spent: w(1).to::<u128>(), gas: w(2).to::<u64>() }))
    }

    #[cfg(test)]
    pub fn clear_cache(&self) {
        self.cache.lock().clear();
    }

    pub fn limit(&self) -> u16 {
        self.limit_bps.load(std::sync::atomic::Ordering::Relaxed)
    }

    /// Accounts and storage keys one simulation reads or writes (for debugging).
    pub fn touched(&self, pool: &Pool, tin: Address, tout: Address, amount: u128) -> Result<Vec<(Address, Vec<U256>)>> {
        let data = pool.helper_call(HELPER, tin, tout, U256::from(amount), self.limit())?;
        let head = self.mirror.state.read().header.clone();
        self.mirror.run(|view| {
            let r = exec(view, &self.env, &head, CALLER, HELPER, data.clone());
            let mut v: Vec<(Address, Vec<U256>)> = r.state.iter().map(|(a, acc)| (*a, acc.storage.keys().copied().collect())).collect();
            v.sort();
            v
        })
    }

    /// Fluid's token order is not in the index: the order whose swap succeeds in the
    /// intended direction wins (the helper reverts when balances move the other way).
    pub fn resolve_fluid(&mut self) {
        let mut pools = std::mem::take(&mut self.pools);
        for p in pools.iter_mut() {
            if let Kind::Fluid { dex, token0 } = p.kind.clone() {
                let (a, b) = (p.tokens[0], p.tokens[1]);
                let dec = self.chain.token_by_address(a).map(|t| t.decimals).unwrap_or(18);
                let probe = 10u128.pow(dec as u32) * 10;
                if self.simulate(p, a, b, probe).is_err() {
                    let other = if token0 == a { b } else { a };
                    p.kind = Kind::Fluid { dex, token0: other };
                }
            }
        }
        self.pools = pools;
    }

    /// Price of one wrapped native token in `quote` atomic units, from a local swap.
    pub fn native_price(&self, quote: Token) -> Result<f64> {
        let (factory, fee, _) = self.chain.native_price;
        let n = self.chain.native.address;
        let stable = if self.chain.native_price.2 { self.chain.usdt } else { self.chain.usdc };
        let mut fees = vec![fee, 500, 3000, 100, 2500];
        fees.dedup();
        for fee in fees {
            let (ok, out, _) = self.call(
                factory,
                crate::venues::IV3Factory::getPoolCall { a: n, b: stable.address, fee: fee.try_into()? }.abi_encode(),
            )?;
            if !ok || out.len() < 32 {
                continue;
            }
            let pool = Address::from_slice(&out[12..32]);
            if pool == Address::ZERO {
                continue;
            }
            let token0 = if n < stable.address { n } else { stable.address };
            let p = Pool {
                venue: "price".into(),
                id: format!("{pool:?}"),
                label: "native price".into(),
                kind: Kind::V3 { pool, token0 },
                tvl_usd: None,
                tokens: vec![n, stable.address],
            };
            let one = 10u128.pow(self.chain.native.decimals as u32);
            if let Ok(s) = self.simulate(&p, n, stable.address, one / 10) {
                // Stablecoins are treated as equal here; this only prices gas.
                let scale = 10f64.powi(quote.decimals as i32 - stable.decimals as i32);
                return Ok(s.out as f64 * 10.0 * scale);
            }
        }
        bail!("no native price pool found")
    }

    /// The gas price read with the mirror's latest header, using EIP-1559 next-block prediction.
    pub fn gas_price(&self) -> Result<u128> {
        let h = self.mirror.state.read().header.clone();
        let basefee = h.basefee as u128;
        if basefee == 0 || h.gas_limit == 0 {
            return Ok(h.gas_price.max(basefee));
        }
        let target = (h.gas_limit / 2) as u128;
        let gas_used = h.gas_used as u128;
        let next_base_fee = if gas_used > target {
            let delta = basefee * (gas_used - target) / target / 8;
            basefee + delta.max(1)
        } else {
            let delta = basefee * (target - gas_used) / target / 8;
            basefee.saturating_sub(delta)
        };
        Ok(h.gas_price.max(next_base_fee))
    }
}
