import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from eth_account import Account
from eth_account.messages import encode_defunct
from eth_utils import keccak

from flashbots_relay import (
    FlashbotsRelay,
    FlashbotsError,
    any_accepted,
    flashbots_signature_header,
    new_auth_key,
    send_to_builders,
    serialize_body,
)
from src.engines import eth_flash_arb_pyusd_usdc as engine

KEY = "0x" + "11" * 32


def relay_verify(body: bytes, header: str) -> bool:
    """Mirror of relay-side verification: recover signer from EIP-191 over the
    0x-hex string of keccak(body); must equal the claimed address."""
    addr, sig = header.split(":")
    digest_hex = "0x" + keccak(body).hex()
    rec = Account.recover_message(encode_defunct(text=digest_hex), signature=sig)
    return rec.lower() == addr.lower()


class _Handler(BaseHTTPRequestHandler):
    log: list = []
    mode = "ok"

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        sig = self.headers.get("X-Flashbots-Signature", "")
        req = json.loads(body)
        ok = bool(sig) and relay_verify(body, sig)  # body bytes exactly as received
        _Handler.log.append((req["method"], ok, req["params"][0]))
        if not ok:
            self.send_response(403)
            self.end_headers()
            self.wfile.write(b"bad sig")
            return
        if _Handler.mode == "rpcerr":
            out = {
                "jsonrpc": "2.0",
                "id": req["id"],
                "error": {"code": -32000, "message": "nope"},
            }
        else:
            out = {
                "jsonrpc": "2.0",
                "id": req["id"],
                "result": {"bundleHash": "0xabc"},
            }
        data = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


class TestFlashbotsRelay(unittest.TestCase):
    def setUp(self):
        _Handler.log.clear()
        _Handler.mode = "ok"
        self.server = HTTPServer(("127.0.0.1", 0), _Handler)
        self.server_thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )
        self.server_thread.start()
        self.server_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_header_format_and_recovery(self):
        body = serialize_body(
            {"jsonrpc": "2.0", "id": 1, "method": "eth_callBundle", "params": []}
        )
        h = flashbots_signature_header(body, KEY)
        addr, sig = h.split(":")
        self.assertEqual(addr, Account.from_key(KEY).address)
        self.assertTrue(sig.startswith("0x") and len(sig) == 132)
        self.assertTrue(relay_verify(body, h))

    def test_old_scheme_is_rejected(self):
        body = serialize_body({"a": 1})
        old = Account.sign_message(
            encode_defunct(keccak(body)), private_key=KEY
        )  # raw 32 bytes (bug)
        hdr = f"{Account.from_key(KEY).address}:0x{old.signature.hex().removeprefix('0x')}"
        self.assertFalse(relay_verify(body, hdr))

    def test_tampered_body_fails(self):
        body = serialize_body({"a": 1})
        h = flashbots_signature_header(body, KEY)
        self.assertFalse(relay_verify(body + b" ", h))

    def test_str_body_rejected(self):
        with self.assertRaises(TypeError):
            flashbots_signature_header('{"a":1}', KEY)

    def test_send_and_call_bundle_end_to_end(self):
        r = FlashbotsRelay(KEY, relay_url=self.server_url)
        self.assertEqual(r.call_bundle(["0xdead"], 100)["bundleHash"], "0xabc")
        self.assertEqual(r.send_bundle(["0xdead"], 101)["bundleHash"], "0xabc")
        self.assertTrue(all(ok for _, ok, _ in _Handler.log))
        self.assertEqual(_Handler.log[1][2]["blockNumber"], hex(101))
        self.assertNotIn(
            "revertingTxHashes", _Handler.log[1][2]
        )  # revert => dropped, no gas

    def test_multi_block_and_builders(self):
        res = send_to_builders(
            ["0xdead"], 200, KEY, [self.server_url, self.server_url], n_blocks=3
        )
        self.assertTrue(any_accepted(res))
        blocks = [
            p["blockNumber"] for m, ok, p in _Handler.log if m == "eth_sendBundle"
        ]
        self.assertEqual(blocks, [hex(200), hex(201), hex(202)] * 2)

    def test_rpc_error_surfaces_and_never_accepts(self):
        _Handler.mode = "rpcerr"
        with self.assertRaises(FlashbotsError):
            FlashbotsRelay(KEY, relay_url=self.server_url).send_bundle(["0xdead"], 1)
        self.assertFalse(
            any_accepted(send_to_builders(["0xdead"], 1, KEY, [self.server_url], 1))
        )

    def test_new_auth_key_valid(self):
        k = new_auth_key()
        self.assertTrue(Account.from_key(k).address.startswith("0x"))

    def test_production_broadcaster_authenticates_exact_body_at_relay(self):
        web3 = Mock()
        web3.eth.block_number = 123
        signed = SimpleNamespace(raw_transaction=b"\x01\x02", hash=b"\x11" * 32)
        settings = {
            "ETH_ENABLE_FLASHBOTS": "true",
            "ETH_FLASHBOTS_RPC": "",
            "ETH_FLASHBOTS_RELAY": self.server_url,
            "ETH_ALLOW_PUBLIC_FALLBACK": "false",
            "FLASHBOTS_AUTH_KEY": KEY,
        }
        with patch.dict(os.environ, settings):
            tx_hash, method = engine.broadcast_flashbots_or_fallback(
                web3, signed, "unused-operator-key", "https://unused.invalid"
            )
        self.assertEqual((tx_hash, method), (signed.hash, "flashbots-relay"))
        self.assertEqual(_Handler.log, [("eth_sendBundle", True, {"txs": ["0x0102"], "blockNumber": "0x7c"})])
        web3.eth.send_raw_transaction.assert_not_called()

    def test_private_failure_does_not_send_to_public_rpc_by_default(self):
        web3 = Mock()
        web3.eth.block_number = 123
        signed = SimpleNamespace(raw_transaction=b"\x01", hash=b"\x11" * 32)
        _Handler.mode = "rpcerr"
        settings = {
            "ETH_ENABLE_FLASHBOTS": "true",
            "ETH_FLASHBOTS_RPC": "",
            "ETH_FLASHBOTS_RELAY": self.server_url,
            "FLASHBOTS_AUTH_KEY": KEY,
        }
        with patch.dict(os.environ, settings):
            os.environ.pop("ETH_ALLOW_PUBLIC_FALLBACK", None)
            with self.assertRaisesRegex(engine.ArbError, "public fallback is disabled"):
                engine.broadcast_flashbots_or_fallback(web3, signed, "unused", "https://unused.invalid")
        web3.eth.send_raw_transaction.assert_not_called()

    def test_invalid_protect_acknowledgement_is_not_accepted(self):
        signed = SimpleNamespace(raw_transaction=b"\x01", hash=b"\x11" * 32)
        for payload in ({"result": None}, {"result": "0x" + "22" * 32}):
            with self.subTest(payload=payload):
                response = Mock()
                response.__enter__ = Mock(return_value=response)
                response.__exit__ = Mock(return_value=None)
                response.read.return_value = json.dumps(payload).encode()
                settings = {"ETH_ENABLE_FLASHBOTS": "true", "ETH_FLASHBOTS_RPC": "https://unused.invalid", "ETH_FLASHBOTS_RELAY": "", "ETH_ALLOW_PUBLIC_FALLBACK": "false"}
                with patch.dict(os.environ, settings), patch("urllib.request.urlopen", return_value=response):
                    with self.assertRaises(engine.ArbError):
                        engine.broadcast_flashbots_or_fallback(Mock(), signed, "unused", "https://unused.invalid")

    def test_private_transaction_is_not_dropped_based_on_public_rpc_absence(self):
        web3 = Mock()
        web3.eth.wait_for_transaction_receipt.side_effect = TimeoutError("not visible")
        plan = {"broadcastMethod": "flashbots-protect"}
        with patch.object(engine, "transaction_is_absent_and_nonce_unused") as absent:
            engine.record_transaction_receipt(plan, web3, b"\x11" * 32, 1)
        absent.assert_not_called()
        self.assertEqual(plan["transactionStatus"], "submitted")

    def test_lost_broadcast_response_preserves_hash_before_and_after_send(self):
        signed = SimpleNamespace(raw_transaction=b"\x01", hash=b"\x11" * 32)
        web3 = Mock()
        web3.eth.account.sign_transaction.return_value = signed
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "route.json"
            plan = {}

            def lost_response(**kwargs):
                stored = json.loads(output.read_text())
                self.assertEqual(stored["transactionHash"], "0x" + "11" * 32)
                raise TimeoutError("response lost")

            with patch.dict(os.environ, {"ETH_ENABLE_FLASHBOTS": "true"}), patch.object(engine, "broadcast_flashbots_or_fallback", side_effect=lost_response):
                engine.submit_transaction_plan(plan, web3, {}, "unused", "https://unused.invalid", 1, str(output))
            stored = json.loads(output.read_text())
            self.assertEqual(stored["transactionStatus"], "submitted")
            self.assertEqual(stored["broadcastMethod"], "flashbots-unacknowledged")
            self.assertEqual(stored["broadcastError"], "response lost")
            archive = output.parent / "transactions" / f"{stored['transactionHash']}.json"
            self.assertEqual(json.loads(archive.read_text()), stored)
            engine.write_plan(str(output), {"mode": "dry-run"})
            self.assertEqual(json.loads(archive.read_text()), stored)

    def test_confirmed_submission_archives_transport_and_receipt(self):
        signed = SimpleNamespace(raw_transaction=b"\x01", hash=b"\x11" * 32)
        web3 = Mock()
        web3.eth.account.sign_transaction.return_value = signed
        web3.eth.wait_for_transaction_receipt.return_value = {"status": 1, "blockNumber": 123, "gasUsed": 21000, "effectiveGasPrice": 1}
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "route.json"
            plan = {}
            with patch.object(engine, "broadcast_flashbots_or_fallback", return_value=(signed.hash, "flashbots-protect")):
                engine.submit_transaction_plan(plan, web3, {}, "unused", "https://unused.invalid", 1, str(output))
            archive = output.parent / "transactions" / f"{plan['transactionHash']}.json"
            stored = json.loads(archive.read_text())
            self.assertEqual(stored["transactionStatus"], "confirmed")
            self.assertEqual(stored["broadcastMethod"], "flashbots-protect")


if __name__ == "__main__":
    unittest.main()
