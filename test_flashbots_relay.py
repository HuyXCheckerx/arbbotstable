import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct
from eth_utils import keccak

from flashbots_relay import (FlashbotsRelay, FlashbotsError, any_accepted,
                             flashbots_signature_header, new_auth_key,
                             send_to_builders, serialize_body)

KEY = "0x" + "11" * 32


def relay_verify(body: bytes, header: str) -> bool:
    """Mirror of relay-side verification: recover signer from EIP-191 over the
    0x-hex string of keccak(body); must equal the claimed address."""
    addr, sig = header.split(":")
    digest_hex = "0x" + keccak(body).hex()
    rec = Account.recover_message(encode_defunct(text=digest_hex), signature=sig)
    return rec.lower() == addr.lower()


def test_header_format_and_recovery():
    body = serialize_body({"jsonrpc": "2.0", "id": 1, "method": "eth_callBundle", "params": []})
    h = flashbots_signature_header(body, KEY)
    addr, sig = h.split(":")
    assert addr == Account.from_key(KEY).address
    assert sig.startswith("0x") and len(sig) == 132
    assert relay_verify(body, h)


def test_old_scheme_is_rejected():
    body = serialize_body({"a": 1})
    old = Account.sign_message(encode_defunct(keccak(body)), private_key=KEY)  # raw 32 bytes (bug)
    hdr = f"{Account.from_key(KEY).address}:0x{old.signature.hex().removeprefix('0x')}"
    assert not relay_verify(body, hdr)


def test_tampered_body_fails():
    body = serialize_body({"a": 1})
    h = flashbots_signature_header(body, KEY)
    assert not relay_verify(body + b" ", h)


def test_str_body_rejected():
    with pytest.raises(TypeError):
        flashbots_signature_header('{"a":1}', KEY)


class _Handler(BaseHTTPRequestHandler):
    log = []
    mode = "ok"

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        sig = self.headers.get("X-Flashbots-Signature", "")
        req = json.loads(body)
        ok = bool(sig) and relay_verify(body, sig)   # body bytes exactly as received
        _Handler.log.append((req["method"], ok, req["params"][0]))
        if not ok:
            self.send_response(403); self.end_headers(); self.wfile.write(b"bad sig"); return
        if _Handler.mode == "rpcerr":
            out = {"jsonrpc": "2.0", "id": req["id"], "error": {"code": -32000, "message": "nope"}}
        else:
            out = {"jsonrpc": "2.0", "id": req["id"], "result": {"bundleHash": "0xabc"}}
        data = json.dumps(out).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.end_headers(); self.wfile.write(data)

    def log_message(self, *a): pass


@pytest.fixture()
def server():
    _Handler.log.clear(); _Handler.mode = "ok"
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def test_send_and_call_bundle_end_to_end(server):
    r = FlashbotsRelay(KEY, relay_url=server)
    assert r.call_bundle(["0xdead"], 100)["bundleHash"] == "0xabc"
    assert r.send_bundle(["0xdead"], 101)["bundleHash"] == "0xabc"
    assert all(ok for _, ok, _ in _Handler.log)
    assert _Handler.log[1][2]["blockNumber"] == hex(101)
    assert "revertingTxHashes" not in _Handler.log[1][2]   # revert => dropped, no gas


def test_multi_block_and_builders(server):
    res = send_to_builders(["0xdead"], 200, KEY, [server, server], n_blocks=3)
    assert any_accepted(res)
    blocks = [p["blockNumber"] for m, ok, p in _Handler.log if m == "eth_sendBundle"]
    assert blocks == [hex(200), hex(201), hex(202)] * 2


def test_rpc_error_surfaces_and_never_accepts(server):
    _Handler.mode = "rpcerr"
    with pytest.raises(FlashbotsError):
        FlashbotsRelay(KEY, relay_url=server).send_bundle(["0xdead"], 1)
    assert not any_accepted(send_to_builders(["0xdead"], 1, KEY, [server], 1))


def test_new_auth_key_valid():
    k = new_auth_key()
    assert Account.from_key(k).address.startswith("0x")
