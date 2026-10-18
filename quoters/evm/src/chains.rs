//! Per-chain configuration: tokens, venue contracts and indexes.

use crate::evm::ChainEnv;
use revm::primitives::hardfork::SpecId;
use revm::primitives::{address, Address};

#[derive(Clone, Copy, Debug)]
pub struct Token {
    pub symbol: &'static str,
    pub address: Address,
    pub decimals: u8,
}

#[derive(Clone, Copy, Debug)]
pub enum V3Lookup {
    /// `getPool(a, b, fee)` for each fee tier.
    Fees(&'static [u32]),
    /// Algebra `poolByPair(a, b)`: one pool per pair, dynamic fee.
    Algebra,
}

#[derive(Clone, Copy, Debug)]
pub struct V3Factory {
    pub venue: &'static str,
    pub factory: Address,
    pub lookup: V3Lookup,
}

pub struct Chain {
    pub key: &'static str,
    pub name: &'static str,
    pub chain_id: u64,
    pub spec: SpecId,
    pub block_time_ms: u64,
    pub rpc_http: &'static str,
    pub rpc_ws: &'static str,
    pub usdc: Token,
    pub usdt: Token,
    pub native: Token,
    /// Tokens whose pairwise pools are discovered and mirrored (USDC and USDT included).
    pub tokens: &'static [Token],
    /// Default intermediates for two-hop routes.
    pub via: &'static [&'static str],
    /// The native price comes from simulating a swap of 1 wrapped native token in this
    /// v3 factory's (native, stablecoin, fee) pool; the bool picks USDT over USDC.
    pub native_price: (Address, u32, bool),
    pub v3: &'static [V3Factory],
    /// Uniswap v4 (PoolManager, PositionManager).
    pub v4: Option<(Address, Address)>,
    /// PancakeSwap Infinity (Vault, CLPoolManager).
    pub infinity: Option<(Address, Address)>,
    pub curve_api: Option<&'static str>,
    /// Balancer v2 Vault and the Balancer API chain enum.
    pub balancer: Option<(Address, &'static str)>,
    /// Sky LitePSM wrapper (USDC <-> USDS at 1:1 minus tin/tout).
    pub sky_psm: Option<Address>,
    pub gecko: &'static str,
    /// Gas a route spends outside its swap legs (21k intrinsic, calldata, executor overhead).
    pub base_gas: u64,
    pub paraswap: u64,
    pub cow: Option<&'static str>,
}

impl Chain {
    pub fn env(&self) -> ChainEnv {
        ChainEnv { chain_id: self.chain_id, spec: self.spec, block_time_ms: self.block_time_ms }
    }

    pub fn token(&self, sym: &str) -> Option<Token> {
        self.tokens.iter().copied().find(|t| t.symbol.eq_ignore_ascii_case(sym) || format!("{:?}", t.address).eq_ignore_ascii_case(sym))
    }

    pub fn token_by_address(&self, a: Address) -> Option<Token> {
        self.tokens.iter().copied().find(|t| t.address == a).or_else(|| (self.native.address == a).then_some(self.native))
    }
}

const UNI_FEES: &[u32] = &[100, 500, 3000, 10000];
const PANCAKE_FEES: &[u32] = &[100, 500, 2500, 10000];

pub static ETHEREUM: Chain = Chain {
    key: "eth",
    name: "Ethereum",
    chain_id: 1,
    spec: SpecId::OSAKA,
    block_time_ms: 12_000,
    rpc_http: "https://ethereum-rpc.publicnode.com",
    rpc_ws: "wss://ethereum-rpc.publicnode.com",
    usdc: Token { symbol: "USDC", address: address!("A0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"), decimals: 6 },
    usdt: Token { symbol: "USDT", address: address!("dAC17F958D2ee523a2206206994597C13D831ec7"), decimals: 6 },
    native: Token { symbol: "WETH", address: address!("C02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"), decimals: 18 },
    tokens: &[
        Token { symbol: "USDC", address: address!("A0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"), decimals: 6 },
        Token { symbol: "USDT", address: address!("dAC17F958D2ee523a2206206994597C13D831ec7"), decimals: 6 },
        Token { symbol: "PYUSD", address: address!("6c3ea9036406852006290770BEdFcAbA0e23A0e8"), decimals: 6 },
        Token { symbol: "USDG", address: address!("e343167631d89B6Ffc58B88d6b7fB0228795491D"), decimals: 6 },
        Token { symbol: "USDS", address: address!("dC035D45d973E3EC169d2276DDab16f1e407384F"), decimals: 18 },
        Token { symbol: "crvUSD", address: address!("f939E0A03FB07F59A73314E73794Be0E57ac1b4E"), decimals: 18 },
    ],
    via: &["USDC", "USDT", "USDS", "crvUSD"],
    native_price: (address!("1F98431c8aD98523631AE4a59f267346ea31F984"), 500, false),
    v3: &[
        V3Factory { venue: "Uniswap v3", factory: address!("1F98431c8aD98523631AE4a59f267346ea31F984"), lookup: V3Lookup::Fees(UNI_FEES) },
        V3Factory { venue: "PancakeSwap v3", factory: address!("0BFbCF9fa4f9C56B0F40a671Ad40E0805A091865"), lookup: V3Lookup::Fees(PANCAKE_FEES) },
        V3Factory { venue: "SushiSwap v3", factory: address!("bACEB8eC6b9355Dfc0269C18bac9d6E2Bdc29C4F"), lookup: V3Lookup::Fees(UNI_FEES) },
    ],
    v4: Some((address!("000000000004444c5dc75cB358380D2e3dE08A90"), address!("bd216513d74c8cf14cf4747e6aaa6420ff64ee9e"))),
    infinity: None,
    curve_api: Some("ethereum"),
    balancer: Some((address!("BA12222222228d8Ba445958a75a0704d566BF2C8"), "MAINNET")),
    sky_psm: Some(address!("A188EEC8F81263234dA3622A406892F3D630f98c")),
    gecko: "eth",
    base_gas: 60_000,
    paraswap: 1,
    cow: Some("mainnet"),
};

pub static BSC: Chain = Chain {
    key: "bsc",
    name: "BNB Chain",
    chain_id: 56,
    spec: SpecId::PRAGUE,
    block_time_ms: 450,
    rpc_http: "https://bsc-rpc.publicnode.com",
    rpc_ws: "wss://bsc-rpc.publicnode.com",
    usdc: Token { symbol: "USDC", address: address!("8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d"), decimals: 18 },
    usdt: Token { symbol: "USDT", address: address!("55d398326f99059fF775485246999027B3197955"), decimals: 18 },
    native: Token { symbol: "WBNB", address: address!("bb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c"), decimals: 18 },
    tokens: &[
        Token { symbol: "USDC", address: address!("8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d"), decimals: 18 },
        Token { symbol: "USDT", address: address!("55d398326f99059fF775485246999027B3197955"), decimals: 18 },
    ],
    via: &[],
    native_price: (address!("0BFbCF9fa4f9C56B0F40a671Ad40E0805A091865"), 100, true),
    v3: &[
        V3Factory { venue: "PancakeSwap v3", factory: address!("0BFbCF9fa4f9C56B0F40a671Ad40E0805A091865"), lookup: V3Lookup::Fees(PANCAKE_FEES) },
        V3Factory { venue: "Uniswap v3", factory: address!("dB1d10011AD0Ff90774D0C6Bb92e5C5c8b4461F7"), lookup: V3Lookup::Fees(UNI_FEES) },
    ],
    v4: Some((address!("28e2ea090877bf75740558f6bfb36a5ffee9e9df"), address!("7a4a5c919ae2541aed11041a1aeee68f1287f95b"))),
    infinity: Some((address!("238a358808379702088667322f80aC48bAd5e6c4"), address!("a0FfB9c1CE1Fe56963B0321B32E7A0302114058b"))),
    curve_api: Some("bsc"),
    balancer: None,
    sky_psm: None,
    gecko: "bsc",
    base_gas: 60_000,
    paraswap: 56,
    cow: Some("bnb"),
};

pub static POLYGON: Chain = Chain {
    key: "polygon",
    name: "Polygon PoS",
    chain_id: 137,
    spec: SpecId::PRAGUE,
    block_time_ms: 2_000,
    rpc_http: "https://polygon-bor-rpc.publicnode.com",
    rpc_ws: "wss://polygon-bor-rpc.publicnode.com",
    usdc: Token { symbol: "USDC", address: address!("3c499c542cEF5E3811e1192ce70d8cC03d5c3359"), decimals: 6 },
    usdt: Token { symbol: "USDT", address: address!("c2132D05D31c914a87C6611C10748AEb04B58e8F"), decimals: 6 },
    native: Token { symbol: "WPOL", address: address!("0d500B1d8E8eF31E21C99d1Db9A6444d3ADf1270"), decimals: 18 },
    tokens: &[
        Token { symbol: "USDC", address: address!("3c499c542cEF5E3811e1192ce70d8cC03d5c3359"), decimals: 6 },
        Token { symbol: "USDT", address: address!("c2132D05D31c914a87C6611C10748AEb04B58e8F"), decimals: 6 },
    ],
    via: &[],
    native_price: (address!("1F98431c8aD98523631AE4a59f267346ea31F984"), 500, false),
    v3: &[
        V3Factory { venue: "Uniswap v3", factory: address!("1F98431c8aD98523631AE4a59f267346ea31F984"), lookup: V3Lookup::Fees(UNI_FEES) },
        V3Factory { venue: "QuickSwap v3", factory: address!("411b0fAcC3489691f28ad58c47006AF5E3Ab3A28"), lookup: V3Lookup::Algebra },
        V3Factory { venue: "SushiSwap v3", factory: address!("917933899c6a5F8E37F31E19f92CdBFF7e8FF0e2"), lookup: V3Lookup::Fees(UNI_FEES) },
    ],
    v4: Some((address!("67366782805870060151383f4bbff9dab53e5cd6"), address!("1ec2ebf4f37e7363fdfe3551602425af0b3ceef9"))),
    infinity: None,
    curve_api: Some("polygon"),
    balancer: Some((address!("BA12222222228d8Ba445958a75a0704d566BF2C8"), "POLYGON")),
    sky_psm: None,
    gecko: "polygon_pos",
    base_gas: 60_000,
    paraswap: 137,
    cow: Some("polygon"),
};

pub fn by_key(k: &str) -> Option<&'static Chain> {
    match k.to_ascii_lowercase().as_str() {
        "eth" | "ethereum" | "mainnet" | "1" => Some(&ETHEREUM),
        "bsc" | "bnb" | "56" => Some(&BSC),
        "polygon" | "pol" | "matic" | "137" => Some(&POLYGON),
        _ => None,
    }
}
