"""Correct Flashbots relay client (drop-in, additive).

Fixes the audit's HIGH finding: the relay authenticates requests with an
EIP-191 personal_sign over the *0x-prefixed lowercase hex string* of
keccak256(raw_request_body). The old code signed the raw 32-byte digest
(encode_defunct(body_hash)), which recovers to a different address, so the
relay rejected it.

Rules that matter (all enforced here):
  1. The bytes hashed MUST be the exact bytes sent. We serialize once with
     compact separators and send that same buffer via `data=` (never `json=`).
  2. Sign the hex STRING:  encode_defunct(text="0x" + keccak(body).hex()).
  3. Header:  X-Flashbots-Signature: <auth_address>:<0x-signature>
  4. The auth key is a throwaway *reputation* key, NOT the funded operator key.
"""
from __future__ import annotations

import json
import secrets
from typing import Iterable, Sequence

import requests
from eth_account import Account
from eth_account.messages import encode_defunct
from eth_utils import keccak

DEFAULT_RELAY = "https://relay.flashbots.net"


def _hex_prefixed(b: bytes) -> str:
    return "0x" + bytes(b).hex()


def serialize_body(payload: dict) -> bytes:
    """Single canonical serialization. Hash and send THIS buffer."""
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def flashbots_signature_header(body: bytes, auth_private_key: str) -> str:
    """Return the value for the X-Flashbots-Signature header."""
    if isinstance(body, str):
        raise TypeError("pass the exact bytes that will be sent, not str")
    digest_hex = _hex_prefixed(keccak(body))              # 0x + 64 hex chars
    msg = encode_defunct(text=digest_hex)                 # EIP-191 over the STRING
    signed = Account.sign_message(msg, private_key=auth_private_key)
    addr = Account.from_key(auth_private_key).address
    return f"{addr}:{_hex_prefixed(signed.signature)}"


def new_auth_key() -> str:
    """Generate a fresh reputation key (store in .env as FLASHBOTS_AUTH_KEY)."""
    return _hex_prefixed(secrets.token_bytes(32))


class FlashbotsError(RuntimeError):
    pass


class FlashbotsRelay:
    def __init__(self, auth_private_key: str, relay_url: str = DEFAULT_RELAY,
                 timeout: float = 5.0, session: requests.Session | None = None):
        if not auth_private_key:
            raise ValueError("auth_private_key required (use a throwaway key)")
        self.key = auth_private_key
        self.url = relay_url
        self.timeout = timeout
        self.s = session or requests.Session()
        self._id = 0

    def _rpc(self, method: str, params: list) -> dict:
        self._id += 1
        payload = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}
        body = serialize_body(payload)
        headers = {
            "Content-Type": "application/json",
            "X-Flashbots-Signature": flashbots_signature_header(body, self.key),
        }
        r = self.s.post(self.url, data=body, headers=headers, timeout=self.timeout)
        if r.status_code != 200:
            raise FlashbotsError(f"{self.url} HTTP {r.status_code}: {r.text[:300]}")
        j = r.json()
        if "error" in j:
            raise FlashbotsError(f"{self.url} RPC error: {j['error']}")
        return j["result"]

    # -- simulation (does not need to be the target block; use latest) --------
    def call_bundle(self, signed_txs: Sequence[str], block_number: int,
                    state_block: str = "latest") -> dict:
        res = self._rpc("eth_callBundle", [{
            "txs": list(signed_txs),
            "blockNumber": hex(block_number),
            "stateBlockNumber": state_block,
        }])
        return res

    # -- submission -----------------------------------------------------------
    def send_bundle(self, signed_txs: Sequence[str], target_block: int,
                    reverting_tx_hashes: Iterable[str] = ()) -> dict:
        """Submit for ONE block. Omitting revertingTxHashes means the bundle is
        dropped (no gas paid) if any tx reverts -- what we want for arbs."""
        params = {"txs": list(signed_txs), "blockNumber": hex(target_block)}
        rev = list(reverting_tx_hashes)
        if rev:
            params["revertingTxHashes"] = rev
        return self._rpc("eth_sendBundle", [params])

    def send_bundle_multi_block(self, signed_txs: Sequence[str], first_block: int,
                                n_blocks: int = 3) -> list[dict]:
        out = []
        for b in range(first_block, first_block + n_blocks):
            try:
                out.append({"block": b, "result": self.send_bundle(signed_txs, b)})
            except FlashbotsError as e:
                out.append({"block": b, "error": str(e)})
        return out


def send_to_builders(signed_txs: Sequence[str], first_block: int, auth_private_key: str,
                     endpoints: Sequence[str], n_blocks: int = 2) -> dict[str, list[dict]]:
    """Private multi-builder fan-out. NEVER falls back to the public mempool:
    if every endpoint fails, the caller should skip the trade."""
    results = {}
    for url in endpoints:
        relay = FlashbotsRelay(auth_private_key, relay_url=url)
        results[url] = relay.send_bundle_multi_block(signed_txs, first_block, n_blocks)
    return results


def any_accepted(results: dict[str, list[dict]]) -> bool:
    return any("result" in r for lst in results.values() for r in lst)
