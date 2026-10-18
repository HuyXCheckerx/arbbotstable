//! Venue definitions: how each pool type is discovered and how the helper swaps through it.

use crate::chains::{Chain, V3Lookup};
use crate::rpc::{hex_bytes, Rpc};
use alloy_sol_types::{sol, SolCall, SolValue};
use anyhow::{anyhow, Result};
use revm::primitives::{keccak256, Address, Bytes, B256, U256};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::str::FromStr;

sol! {
    struct V4Key { address currency0; address currency1; uint24 fee; int24 tickSpacing; address hooks; }
    struct InfKey { address currency0; address currency1; address hooks; address poolManager; uint24 fee; bytes32 parameters; }
    struct SingleSwap { bytes32 poolId; uint8 kind; address assetIn; address assetOut; uint256 amount; bytes userData; }
    struct FundManagement { address sender; bool fromInternalBalance; address recipient; bool toInternalBalance; }

    interface IHelper {
        function approve(address token, address spender) external;
        function callSwap(address tokenIn, address tokenOut, uint256 amountIn, address target, bytes data, uint8 mode) external returns (uint256, uint256, uint256);
        function v3Swap(address pool, bool zeroForOne, uint256 amountIn, address tokenIn, address tokenOut, uint16 limitBps) external returns (uint256, uint256, uint256);
        function v4Swap(address manager, V4Key key, bool zeroForOne, uint256 amountIn, uint16 limitBps) external returns (uint256, uint256, uint256);
        function infSwap(address vault, InfKey key, bool zeroForOne, uint256 amountIn, uint16 limitBps) external returns (uint256, uint256, uint256);
        function mavSwap(address pool, bool tokenAIn, uint256 amountIn, address tokenIn, address tokenOut, uint16 limitBps) external returns (uint256, uint256, uint256);
    }
    interface IMaverick { function tokenA() external view returns (address); function tokenB() external view returns (address); }
    interface ISkyPsm {
        function sellGem(address usr, uint256 gemAmt) external returns (uint256);
        function buyGem(address usr, uint256 gemAmt) external returns (uint256);
        function tout() external view returns (uint256);
        function gem() external view returns (address);
        function usds() external view returns (address);
    }
    interface IERC20 { function balanceOf(address owner) external view returns (uint256); }
    interface IV3Factory { function getPool(address a, address b, uint24 fee) external view returns (address); }
    interface IAlgebraFactory { function poolByPair(address a, address b) external view returns (address); }
    interface IV3Pool { function liquidity() external view returns (uint128); }
    interface IPosM { function poolKeys(bytes25 id) external view returns (address currency0, address currency1, uint24 fee, int24 tickSpacing, address hooks); }
    interface IInfCL { function poolIdToPoolKey(bytes32 id) external view returns (address currency0, address currency1, address hooks, address poolManager, uint24 fee, bytes32 parameters); }
    interface ICurve { function exchange(int128 i, int128 j, uint256 dx, uint256 min_dy) external; }
    interface ICurveU { function exchange(uint256 i, uint256 j, uint256 dx, uint256 min_dy) external; }
    interface IFluid { function swapIn(bool swap0to1, uint256 amountIn, uint256 amountOutMin, address to) external payable returns (uint256); }
    interface IDodo { function sellBase(address to) external returns (uint256); function sellQuote(address to) external returns (uint256); function _BASE_TOKEN_() external view returns (address); }
    interface IBalVault { function swap(SingleSwap singleSwap, FundManagement funds, uint256 limit, uint256 deadline) external returns (uint256); }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(tag = "type")]
pub enum Kind {
    V3 { pool: Address, token0: Address },
    V4 { manager: Address, currency0: Address, currency1: Address, fee: u32, tick_spacing: i32, hooks: Address },
    Infinity { vault: Address, manager: Address, currency0: Address, currency1: Address, hooks: Address, fee: u32, parameters: B256 },
    Curve { pool: Address, coins: Vec<Address>, uint_indices: bool },
    Fluid { dex: Address, token0: Address },
    Dodo { pool: Address, base: Address },
    Balancer { vault: Address, pool_id: B256 },
    Maverick { pool: Address, token_a: Address },
    /// Sky LitePSM wrapper: USDC (gem) <-> USDS. `tout` is read at discovery.
    SkyPsm { wrapper: Address, gem: Address, usds: Address, tout: U256 },
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Pool {
    pub venue: String,
    /// Pool address, or v4/Infinity pool id.
    pub id: String,
    pub label: String,
    pub kind: Kind,
    #[serde(default)]
    pub tvl_usd: Option<f64>,
    /// Every token the pool trades (two for most venues, more for Curve and Balancer).
    #[serde(default)]
    pub tokens: Vec<Address>,
}

impl Pool {
    pub fn short(&self) -> String {
        format!("{} {}", self.label, &self.id[..10.min(self.id.len())])
    }

    /// (token, spender) approvals the helper needs before swapping, for the tokens in `graph`.
    pub fn approvals(&self, graph: &[Address]) -> Vec<(Address, Address)> {
        let spender = match &self.kind {
            Kind::Curve { pool, .. } => *pool,
            Kind::Fluid { dex, .. } => *dex,
            Kind::Balancer { vault, .. } => *vault,
            Kind::SkyPsm { wrapper, .. } => *wrapper,
            _ => return vec![],
        };
        self.tokens.iter().filter(|t| graph.contains(t)).map(|t| (*t, spender)).collect()
    }

    /// Calldata for the helper entry point that swaps `amount` of `tin` for `tout` here.
    /// Concentrated-liquidity swaps stop `limit_bps` (sqrt-price) from the current price.
    pub fn helper_call(&self, helper: Address, tin: Address, tout: Address, amount: U256, limit_bps: u16) -> Result<Bytes> {
        let data = match &self.kind {
            Kind::V3 { pool, token0 } => IHelper::v3SwapCall {
                pool: *pool,
                zeroForOne: tin == *token0,
                amountIn: amount,
                tokenIn: tin,
                tokenOut: tout,
                limitBps: limit_bps,
            }
            .abi_encode(),
            Kind::V4 { manager, currency0, currency1, fee, tick_spacing, hooks } => IHelper::v4SwapCall {
                manager: *manager,
                key: V4Key {
                    currency0: *currency0,
                    currency1: *currency1,
                    fee: (*fee).try_into()?,
                    tickSpacing: (*tick_spacing).try_into()?,
                    hooks: *hooks,
                },
                zeroForOne: tin == *currency0,
                amountIn: amount,
                limitBps: limit_bps,
            }
            .abi_encode(),
            Kind::Infinity { vault, manager, currency0, currency1, hooks, fee, parameters } => IHelper::infSwapCall {
                vault: *vault,
                key: InfKey {
                    currency0: *currency0,
                    currency1: *currency1,
                    hooks: *hooks,
                    poolManager: *manager,
                    fee: (*fee).try_into()?,
                    parameters: *parameters,
                },
                zeroForOne: tin == *currency0,
                amountIn: amount,
                limitBps: limit_bps,
            }
            .abi_encode(),
            Kind::Curve { pool, coins, uint_indices } => {
                let i = coins.iter().position(|c| *c == tin).ok_or_else(|| anyhow!("token not in pool"))?;
                let j = coins.iter().position(|c| *c == tout).ok_or_else(|| anyhow!("token not in pool"))?;
                let inner = if *uint_indices {
                    ICurveU::exchangeCall { i: U256::from(i), j: U256::from(j), dx: amount, min_dy: U256::ZERO }.abi_encode()
                } else {
                    ICurve::exchangeCall { i: i as i128, j: j as i128, dx: amount, min_dy: U256::ZERO }.abi_encode()
                };
                call_swap(tin, tout, amount, *pool, inner, 0)
            }
            Kind::Fluid { dex, token0 } => {
                let inner = IFluid::swapInCall { swap0to1: tin == *token0, amountIn: amount, amountOutMin: U256::ZERO, to: helper }
                    .abi_encode();
                call_swap(tin, tout, amount, *dex, inner, 0)
            }
            Kind::Dodo { pool, base } => {
                let inner = if tin == *base {
                    IDodo::sellBaseCall { to: helper }.abi_encode()
                } else {
                    IDodo::sellQuoteCall { to: helper }.abi_encode()
                };
                call_swap(tin, tout, amount, *pool, inner, 1)
            }
            Kind::SkyPsm { wrapper, gem, usds, tout: fee } => {
                let inner = if tin == *gem {
                    ISkyPsm::sellGemCall { usr: helper, gemAmt: amount }.abi_encode()
                } else if tin == *usds {
                    // buyGem takes the USDC amount out: the USDS in is gemAmt * 1e12 * (1 + tout).
                    let wad = U256::from(10u64).pow(U256::from(18));
                    let gem_amt = amount * wad / (U256::from(10u64).pow(U256::from(12)) * (wad + *fee));
                    ISkyPsm::buyGemCall { usr: helper, gemAmt: gem_amt }.abi_encode()
                } else {
                    return Err(anyhow!("token not in pool"));
                };
                call_swap(tin, tout, amount, *wrapper, inner, 0)
            }
            Kind::Maverick { pool, token_a } => IHelper::mavSwapCall {
                pool: *pool,
                tokenAIn: tin == *token_a,
                amountIn: amount,
                tokenIn: tin,
                tokenOut: tout,
                limitBps: limit_bps,
            }
            .abi_encode(),
            Kind::Balancer { vault, pool_id } => {
                let inner = IBalVault::swapCall {
                    singleSwap: SingleSwap {
                        poolId: *pool_id,
                        kind: 0,
                        assetIn: tin,
                        assetOut: tout,
                        amount,
                        userData: Bytes::new(),
                    },
                    funds: FundManagement { sender: helper, fromInternalBalance: false, recipient: helper, toInternalBalance: false },
                    limit: U256::ZERO,
                    deadline: U256::MAX,
                }
                .abi_encode();
                call_swap(tin, tout, amount, *vault, inner, 0)
            }
        };
        Ok(data.into())
    }
}

fn call_swap(tin: Address, tout: Address, amount: U256, target: Address, inner: Vec<u8>, mode: u8) -> Vec<u8> {
    IHelper::callSwapCall { tokenIn: tin, tokenOut: tout, amountIn: amount, target, data: inner.into(), mode }.abi_encode()
}

// ---------------------------------------------------------------------------------------------
// Discovery
// ---------------------------------------------------------------------------------------------

#[derive(Default, Serialize, Deserialize)]
pub struct Discovery {
    pub chain: String,
    pub block: u64,
    /// The graph tokens the pools were discovered for.
    #[serde(default)]
    pub tokens: Vec<Address>,
    pub pools: Vec<Pool>,
    /// Indexed USDC/USDT pools on venues this engine cannot simulate yet.
    pub unsupported: Vec<(String, String, f64)>,
    pub warnings: Vec<String>,
}

fn eth_calls(rpc: &Rpc, calls: &[(Address, Vec<u8>)]) -> Result<Vec<Option<Bytes>>> {
    let reqs: Vec<(&str, Value)> = calls
        .iter()
        .map(|(to, data)| ("eth_call", json!([{"to": to, "data": Bytes::from(data.clone())}, "latest"])))
        .collect();
    let res = rpc.batch(&reqs)?;
    Ok(res.into_iter().map(|v| if v.get("__error").is_some() { None } else { hex_bytes(&v).ok() }).collect())
}

fn word_addr(b: &Option<Bytes>) -> Option<Address> {
    let b = b.as_ref()?;
    if b.len() < 32 {
        return None;
    }
    let a = Address::from_slice(&b[12..32]);
    (a != Address::ZERO).then_some(a)
}

struct Indexed {
    dex: String,
    address: String,
    tvl: f64,
    a: Address,
    b: Address,
}

fn http_json(agent: &ureq::Agent, url: &str, body: Option<Value>) -> Result<Value> {
    let resp = match body {
        Some(b) => agent.post(url).header("content-type", "application/json").send_json(&b),
        None => agent.get(url).header("accept", "application/json").call(),
    };
    let mut resp = resp?;
    let status = resp.status().as_u16();
    let text = resp.body_mut().with_config().limit(256 << 20).read_to_string()?;
    if status != 200 {
        return Err(anyhow!("HTTP {status}: {}", &text[..text.len().min(200)]));
    }
    Ok(serde_json::from_str(&text)?)
}

pub fn agent() -> ureq::Agent {
    ureq::Agent::config_builder()
        .timeout_global(Some(std::time::Duration::from_secs(30)))
        .http_status_as_error(false)
        .build()
        .into()
}

/// GeckoTerminal's pools for each graph token, kept when both of a pool's tokens are graph tokens.
fn gecko_pools(chain: &Chain, agent: &ureq::Agent, graph: &[Address], warnings: &mut Vec<String>) -> Vec<Indexed> {
    let mut out: Vec<Indexed> = Vec::new();
    for t in graph {
        for page in 1..=3 {
            let url = format!(
                "https://api.geckoterminal.com/api/v2/networks/{}/tokens/{t:?}/pools?page={page}&sort=h24_volume_usd_liquidity_desc",
                chain.gecko
            );
            let mut tries = 0;
            let j = loop {
                match http_json(agent, &url, None) {
                    Ok(j) => break Some(j),
                    Err(e) if tries < 4 && e.to_string().contains("429") => {
                        tries += 1;
                        std::thread::sleep(std::time::Duration::from_secs(15));
                    }
                    Err(e) => {
                        warnings.push(format!("geckoterminal {t:?} page {page}: {e}"));
                        break None;
                    }
                }
            };
            std::thread::sleep(std::time::Duration::from_millis(2500));
            let Some(j) = j else { break };
            let data = j["data"].as_array().cloned().unwrap_or_default();
            if data.is_empty() {
                break;
            }
            for p in data {
                let tok = |k: &str| {
                    p["relationships"][k]["data"]["id"]
                        .as_str()
                        .and_then(|s| s.split('_').next_back())
                        .and_then(|s| Address::from_str(s).ok())
                };
                let (Some(x), Some(y)) = (tok("base_token"), tok("quote_token")) else { continue };
                if x == y || !graph.contains(&x) || !graph.contains(&y) {
                    continue;
                }
                let address = p["attributes"]["address"].as_str().unwrap_or("").to_string();
                if out.iter().any(|o| o.address.eq_ignore_ascii_case(&address)) {
                    continue;
                }
                out.push(Indexed {
                    dex: p["relationships"]["dex"]["data"]["id"].as_str().unwrap_or("").to_string(),
                    address,
                    tvl: p["attributes"]["reserve_in_usd"].as_str().and_then(|s| s.parse().ok()).unwrap_or(0.0),
                    a: x,
                    b: y,
                });
            }
        }
    }
    out
}

pub fn v4_pool_id(c0: Address, c1: Address, fee: u32, ts: i32, hooks: Address) -> B256 {
    use alloy_sol_types::private::primitives::aliases::{I24, U24};
    let enc = (c0, c1, U24::from(fee), I24::try_from(ts).unwrap(), hooks).abi_encode();
    keccak256(enc)
}


fn sorted(a: Address, b: Address) -> (Address, Address) {
    if a < b { (a, b) } else { (b, a) }
}

fn sym(chain: &Chain, a: Address) -> String {
    chain.token_by_address(a).map(|t| t.symbol.to_string()).unwrap_or_else(|| format!("{a:?}")[..8].to_string())
}

/// Discovers pools between every pair of the chain's graph tokens.
pub fn discover(chain: &Chain, rpc: &Rpc) -> Result<Discovery> {
    let mut d = Discovery { chain: chain.key.into(), ..Default::default() };
    d.block = crate::rpc::hex_u64(&rpc.call("eth_blockNumber", json!([]))?)?;
    let graph: Vec<Address> = chain.tokens.iter().map(|t| t.address).collect();
    d.tokens = graph.clone();
    let mut pairs = Vec::new();
    for i in 0..graph.len() {
        for j in i + 1..graph.len() {
            pairs.push(sorted(graph[i], graph[j]));
        }
    }
    let pair_name = |a: Address, b: Address| format!("{}/{}", sym(chain, a), sym(chain, b));
    let agent = agent();

    // Uniswap v3 style factories, every pair and fee tier.
    let mut calls = Vec::new();
    let mut meta = Vec::new();
    for &(t0, t1) in &pairs {
        for f in chain.v3 {
            match f.lookup {
                V3Lookup::Fees(fees) => {
                    for &fee in fees {
                        calls.push((f.factory, IV3Factory::getPoolCall { a: t0, b: t1, fee: fee.try_into()? }.abi_encode()));
                        meta.push((f.venue, format!("{} {}%", f.venue, fee as f64 / 10000.0), t0, t1));
                    }
                }
                V3Lookup::Algebra => {
                    calls.push((f.factory, IAlgebraFactory::poolByPairCall { a: t0, b: t1 }.abi_encode()));
                    meta.push((f.venue, format!("{} dyn", f.venue), t0, t1));
                }
            }
        }
    }
    let res = eth_calls(rpc, &calls)?;
    let mut v3_pools = Vec::new();
    for ((venue, label, t0, t1), r) in meta.into_iter().zip(res.iter()) {
        if let Some(pool) = word_addr(r) {
            v3_pools.push((venue, label, pool, t0, t1));
        }
    }
    let liq = eth_calls(rpc, &v3_pools.iter().map(|(_, _, p, _, _)| (*p, IV3Pool::liquidityCall {}.abi_encode())).collect::<Vec<_>>())?;
    for ((venue, label, pool, t0, t1), l) in v3_pools.into_iter().zip(liq) {
        let l = l.map(|b| U256::from_be_slice(&b[..32.min(b.len())])).unwrap_or_default();
        if l.is_zero() {
            continue;
        }
        d.pools.push(Pool {
            venue: venue.into(),
            id: format!("{pool:?}"),
            label: format!("{label} {}", pair_name(t0, t1)),
            kind: Kind::V3 { pool, token0: t0 },
            tvl_usd: None,
            tokens: vec![t0, t1],
        });
    }

    let indexed = gecko_pools(chain, &agent, &graph, &mut d.warnings);

    // Uniswap v4: indexed ids resolved through PositionManager.poolKeys, plus hookless fee tiers.
    if let Some((pm, posm)) = chain.v4 {
        let ids: Vec<(B256, f64)> = indexed
            .iter()
            .filter(|p| p.dex.starts_with("uniswap-v4") || p.dex.starts_with("uniswap_v4"))
            .filter_map(|p| B256::from_str(&p.address).ok().map(|id| (id, p.tvl)))
            .collect();
        let mut keys: Vec<(B256, Address, Address, u32, i32, Address, f64)> = Vec::new();
        let res = eth_calls(
            rpc,
            &ids.iter()
                .map(|(id, _)| (posm, IPosM::poolKeysCall { id: alloy_sol_types::private::FixedBytes::<25>::from_slice(&id[..25]) }.abi_encode()))
                .collect::<Vec<_>>(),
        )?;
        for ((id, tvl), r) in ids.into_iter().zip(res) {
            let Some(r) = r else { continue };
            let Ok(k) = IPosM::poolKeysCall::abi_decode_returns(&r) else { continue };
            let (fee, ts): (u32, i32) = (k.fee.to(), k.tickSpacing.as_i32());
            if v4_pool_id(k.currency0, k.currency1, fee, ts, k.hooks) != id {
                d.warnings.push(format!("v4 pool {id}: PositionManager key does not hash to the id"));
                continue;
            }
            keys.push((id, k.currency0, k.currency1, fee, ts, k.hooks, tvl));
        }
        let mut cands = Vec::new();
        for &(t0, t1) in &pairs {
            for fee in [1u32, 2, 3, 4, 5, 6, 7, 8, 9, 10, 20, 25, 30, 50, 80, 100, 500] {
                for ts in [1i32, 10] {
                    let id = v4_pool_id(t0, t1, fee, ts, Address::ZERO);
                    if !keys.iter().any(|k| k.0 == id) {
                        cands.push((id, t0, t1, fee, ts));
                    }
                }
            }
        }
        // Initialized pools have a non-zero slot0; pools without in-range liquidity are skipped.
        let reqs: Vec<(&str, Value)> = cands
            .iter()
            .flat_map(|(id, ..)| {
                let slot = U256::from_be_bytes(keccak256((*id, U256::from(6)).abi_encode()).0);
                [
                    ("eth_getStorageAt", json!([pm, B256::from(slot), "latest"])),
                    ("eth_getStorageAt", json!([pm, B256::from(slot + U256::from(3)), "latest"])),
                ]
            })
            .collect();
        let vals = rpc.batch(&reqs)?;
        for (i, (id, t0, t1, fee, ts)) in cands.into_iter().enumerate() {
            let nz = |v: &Value| crate::rpc::hex_u256(v).map(|x| !x.is_zero()).unwrap_or(false);
            if nz(&vals[2 * i]) && nz(&vals[2 * i + 1]) {
                keys.push((id, t0, t1, fee, ts, Address::ZERO, 0.0));
            }
        }
        for (id, c0, c1, fee, ts, hooks, tvl) in keys {
            let fee_s = if fee == 0x800000 { "dyn".to_string() } else { format!("{}%", fee as f64 / 10000.0) };
            let hook_s = if hooks == Address::ZERO { String::new() } else { " hooked".into() };
            d.pools.push(Pool {
                venue: "Uniswap v4".into(),
                id: format!("{id}"),
                label: format!("Uniswap v4 {fee_s}{hook_s} {}", pair_name(c0, c1)),
                kind: Kind::V4 { manager: pm, currency0: c0, currency1: c1, fee, tick_spacing: ts, hooks },
                tvl_usd: (tvl > 0.0).then_some(tvl),
                tokens: vec![c0, c1],
            });
        }
    }

    // PancakeSwap Infinity CL: indexed ids resolved through CLPoolManager.poolIdToPoolKey.
    if let Some((vault, clpm)) = chain.infinity {
        let ids: Vec<(B256, f64)> = indexed
            .iter()
            .filter(|p| p.dex.starts_with("pancakeswap-infinity-cl"))
            .filter_map(|p| B256::from_str(&p.address).ok().map(|id| (id, p.tvl)))
            .collect();
        let res = eth_calls(rpc, &ids.iter().map(|(id, _)| (clpm, IInfCL::poolIdToPoolKeyCall { id: *id }.abi_encode())).collect::<Vec<_>>())?;
        for ((id, tvl), r) in ids.into_iter().zip(res) {
            let Some(r) = r else { continue };
            let Ok(k) = IInfCL::poolIdToPoolKeyCall::abi_decode_returns(&r) else { continue };
            if k.poolManager != clpm {
                continue;
            }
            let fee: u32 = k.fee.to();
            let fee_s = if fee == 0x800000 { "dyn".to_string() } else { format!("{}%", fee as f64 / 10000.0) };
            let hook_s = if k.hooks == Address::ZERO { String::new() } else { " hooked".into() };
            d.pools.push(Pool {
                venue: "PancakeSwap Infinity".into(),
                id: format!("{id}"),
                label: format!("Pancake Infinity CL {fee_s}{hook_s} {}", pair_name(k.currency0, k.currency1)),
                kind: Kind::Infinity {
                    vault,
                    manager: clpm,
                    currency0: k.currency0,
                    currency1: k.currency1,
                    hooks: k.hooks,
                    fee,
                    parameters: k.parameters,
                },
                tvl_usd: Some(tvl),
                tokens: vec![k.currency0, k.currency1],
            });
        }
    }

    // Fluid DEX, DODO and Maverick v2 from the index. Fluid's token order is resolved by simulation.
    for p in &indexed {
        let Ok(addr) = Address::from_str(&p.address) else { continue };
        let (t0, t1) = sorted(p.a, p.b);
        if p.dex.starts_with("fluid") {
            d.pools.push(Pool {
                venue: "Fluid DEX".into(),
                id: format!("{addr:?}"),
                label: format!("Fluid DEX {}", pair_name(t0, t1)),
                kind: Kind::Fluid { dex: addr, token0: t0 },
                tvl_usd: Some(p.tvl),
                tokens: vec![t0, t1],
            });
        } else if p.dex.starts_with("dodo") && p.tvl > 20_000.0 {
            let base = eth_calls(rpc, &[(addr, IDodo::_BASE_TOKEN_Call {}.abi_encode())])?.pop().flatten();
            if let Some(base) = word_addr(&base) {
                d.pools.push(Pool {
                    venue: "DODO".into(),
                    id: format!("{addr:?}"),
                    label: format!("DODO PMM {}", pair_name(t0, t1)),
                    kind: Kind::Dodo { pool: addr, base },
                    tvl_usd: Some(p.tvl),
                    tokens: vec![t0, t1],
                });
            }
        } else if p.dex.starts_with("maverick-v2") && p.tvl > 20_000.0 {
            let r = eth_calls(rpc, &[(addr, IMaverick::tokenACall {}.abi_encode()), (addr, IMaverick::tokenBCall {}.abi_encode())])?;
            if let (Some(a), Some(b)) = (word_addr(&r[0]), word_addr(&r[1])) {
                d.pools.push(Pool {
                    venue: "Maverick v2".into(),
                    id: format!("{addr:?}"),
                    label: format!("Maverick v2 {}", pair_name(a, b)),
                    kind: Kind::Maverick { pool: addr, token_a: a },
                    tvl_usd: Some(p.tvl),
                    tokens: vec![a, b],
                });
            }
        }
    }

    // Sky LitePSM wrapper (USDC <-> USDS), when both tokens are graph tokens.
    if let Some(w) = chain.sky_psm {
        let r = eth_calls(rpc, &[(w, ISkyPsm::gemCall {}.abi_encode()), (w, ISkyPsm::usdsCall {}.abi_encode()), (w, ISkyPsm::toutCall {}.abi_encode())])?;
        if let (Some(gem), Some(usds), Some(t)) = (word_addr(&r[0]), word_addr(&r[1]), r[2].as_ref()) {
            if graph.contains(&gem) && graph.contains(&usds) && t.len() >= 32 {
                d.pools.push(Pool {
                    venue: "Sky PSM".into(),
                    id: format!("{w:?}"),
                    label: format!("Sky LitePSM {}", pair_name(gem, usds)),
                    kind: Kind::SkyPsm { wrapper: w, gem, usds, tout: U256::from_be_slice(&t[..32]) },
                    tvl_usd: None,
                    tokens: vec![gem, usds],
                });
            }
        }
    }

    // Curve: the Curve API lists every pool; keep those holding at least two graph tokens directly.
    if let Some(name) = chain.curve_api {
        match http_json(&agent, &format!("https://api.curve.finance/v1/getPools/all/{name}"), None) {
            Ok(j) => {
                for p in j["data"]["poolData"].as_array().into_iter().flatten() {
                    let coins: Vec<Address> = p["coinsAddresses"]
                        .as_array()
                        .into_iter()
                        .flatten()
                        .filter_map(|c| c.as_str().and_then(|s| Address::from_str(s).ok()))
                        .collect();
                    let tvl = p["usdTotal"].as_f64().unwrap_or(0.0);
                    let in_graph: Vec<Address> = coins.iter().copied().filter(|c| graph.contains(c)).collect();
                    if in_graph.len() < 2 || tvl < 20_000.0 {
                        continue;
                    }
                    let Some(addr) = p["address"].as_str().and_then(|s| Address::from_str(s).ok()) else { continue };
                    let crypto = p["isCryptoPool"].as_bool().unwrap_or(false)
                        || p["id"].as_str().map(|s| s.contains("crypto") || s.contains("twocrypto") || s.contains("tricrypto")).unwrap_or(false);
                    d.pools.push(Pool {
                        venue: "Curve".into(),
                        id: format!("{addr:?}"),
                        label: format!("Curve {}", p["name"].as_str().unwrap_or("").replace("Curve.fi ", "")),
                        kind: Kind::Curve { pool: addr, coins: coins.clone(), uint_indices: crypto },
                        tvl_usd: Some(tvl),
                        tokens: in_graph,
                    });
                }
            }
            Err(e) => d.warnings.push(format!("curve api: {e}")),
        }
    }

    // Balancer v2 pools holding at least two graph tokens, from the Balancer API.
    if let Some((vault, api_chain)) = chain.balancer {
        let list = graph.iter().map(|a| format!("\"{a:?}\"")).collect::<Vec<_>>().join(", ");
        let q = format!(
            r#"{{ poolGetPools(first: 100, orderBy: totalLiquidity, orderDirection: desc, where: {{chainIn: [{api_chain}], tokensIn: [{list}], minTvl: 20000}}) {{ id address name protocolVersion type dynamicData {{ totalLiquidity }} poolTokens {{ address }} }} }}"#
        );
        match http_json(&agent, "https://api-v3.balancer.fi/", Some(json!({"query": q}))) {
            Ok(j) => {
                for p in j["data"]["poolGetPools"].as_array().into_iter().flatten() {
                    let toks: Vec<Address> = p["poolTokens"]
                        .as_array()
                        .into_iter()
                        .flatten()
                        .filter_map(|t| t["address"].as_str().and_then(|s| Address::from_str(s).ok()))
                        .filter(|a| graph.contains(a))
                        .collect();
                    if toks.len() < 2 {
                        continue;
                    }
                    let tvl: f64 = p["dynamicData"]["totalLiquidity"].as_str().and_then(|s| s.parse().ok()).unwrap_or(0.0);
                    if p["protocolVersion"].as_i64() != Some(2) {
                        d.unsupported.push((format!("balancer-v{}", p["protocolVersion"]), p["address"].as_str().unwrap_or("").into(), tvl));
                        continue;
                    }
                    let Some(id) = p["id"].as_str().and_then(|s| B256::from_str(s).ok()) else { continue };
                    d.pools.push(Pool {
                        venue: "Balancer v2".into(),
                        id: format!("{id}"),
                        label: format!("Balancer v2 {}", p["name"].as_str().unwrap_or("")),
                        kind: Kind::Balancer { vault, pool_id: id },
                        tvl_usd: Some(tvl),
                        tokens: toks,
                    });
                }
            }
            Err(e) => d.warnings.push(format!("balancer api: {e}")),
        }
    }

    // Index TVL onto pools found elsewhere; report the indexed pools no adapter covers.
    for pool in d.pools.iter_mut() {
        if pool.tvl_usd.is_none() {
            pool.tvl_usd = indexed.iter().find(|p| p.address.eq_ignore_ascii_case(&pool.id)).map(|p| p.tvl);
        }
    }
    let have: std::collections::HashSet<String> = d.pools.iter().map(|p| p.id.to_lowercase()).collect();
    for p in indexed {
        if have.contains(&p.address.to_lowercase()) || p.tvl < 10_000.0 {
            continue;
        }
        let v3_family = (p.dex.starts_with("uniswap") || p.dex.starts_with("pancakeswap-v3") || p.dex.starts_with("sushi")) && p.dex.contains("v3");
        if v3_family {
            continue;
        }
        d.unsupported.push((format!("{} {}", p.dex, pair_name(p.a, p.b)), p.address, p.tvl));
    }
    d.unsupported.sort_by(|a, b| b.2.partial_cmp(&a.2).unwrap());
    Ok(d)
}
