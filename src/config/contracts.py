"""Centralized contract, protocol, and token addresses for Ethereum arbitrage.

Hardcoded parameters: all contract addresses, token addresses, and protocol targets.
Only private keys remain secret in .env.
"""

from __future__ import annotations

import os

# ==============================================================================
# OPERATOR & MASTER CONTRACT IMPLEMENTATIONS
# ==============================================================================
OPERATOR_ADDRESS = "0x50dA32E628b45AbB1335924086Ca0013b9d4eC1C"

# Master MorphoMatchaStableArbUsdc implementation with 'blacked' method
MASTER_IMPLEMENTATION = "0xD48Ab89581e77b103014DCC922279855a3b2a940"

# Fallback known takers for testing/probing risk blocks
DUMMY_CLEAN_TAKER = "0xd8da6bf26964af9d7eed9e03e53415d37aa96045"

# ==============================================================================
# RPC CONFIGURATION
# ==============================================================================
CHAIN_ID = 1
DEFAULT_RPC_URL = "https://ethereum.publicnode.com"
RPC_FALLBACKS = [
    "https://rpc.mevblocker.io",
    "https://eth-mainnet.public.blastapi.io",
    "https://1rpc.io/eth",
]

# ==============================================================================
# CORE STABLECOIN TOKENS
# ==============================================================================
PYUSD = "0x6c3ea9036406852006290770BEdFcAbA0e23A0e8"
USDG = "0xe343167631d89B6Ffc58B88d6b7fB0228795491D"
USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
USDT = "0xdAC17F958D2ee523a2206206994597C13D831ec7"

TOKEN_DECIMALS = {
    PYUSD.lower(): 6,
    USDG.lower(): 6,
    USDC.lower(): 6,
    USDT.lower(): 6,
}

# ==============================================================================
# DEFI PROTOCOLS & SPENDERS
# ==============================================================================
MORPHO = "0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb"
ALLOWANCE_HOLDER = "0x0000000000001fF3684f28c67538d4D072C22734"
STABLE_POOL = "0x4879Fe4B72b4c10658a5c3702A3e970221E72754"
UNISWAP_V4_POOL_MANAGER = "0x000000000004444c5dc75cB358380D2e3dE08A90"
AAVE_V3_POOL = "0x87870Bca3F3fD6335C3F4ce8392D69350B4fA4E2"


def get_current_executor() -> str:
    """Returns the current active executor contract address from .env or fallback."""
    return os.getenv("ETH_ARB_STABLECOIN_EXECUTOR", MASTER_IMPLEMENTATION).strip()
