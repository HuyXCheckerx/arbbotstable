import json
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from websockets.sync.server import serve

from src.core import eth_head_stream
from src.core.eth_head_stream import EthHeadStream, websocket_url


def header(number: int, base_fee_wei: int) -> dict:
    return {"number": hex(number), "baseFeePerGas": hex(base_fee_wei)}


class WebsocketUrlTests(unittest.TestCase):
    def test_explicit_url_wins_and_can_disable(self):
        with patch.dict("os.environ", {"ETH_WS_URL": "wss://node.example/ws"}):
            self.assertEqual(websocket_url("https://rpc.example"), "wss://node.example/ws")
        with patch.dict("os.environ", {"ETH_WS_URL": "off"}):
            self.assertIsNone(websocket_url("https://rpc.example"))

    def test_derives_from_first_http_endpoint(self):
        with patch.dict("os.environ", {"ETH_WS_URL": ""}):
            self.assertEqual(
                websocket_url("https://eth-mainnet.g.alchemy.com/v2/key,https://b.example"),
                "wss://eth-mainnet.g.alchemy.com/v2/key",
            )
            self.assertEqual(
                websocket_url("https://mainnet.infura.io/v3/key"),
                "wss://mainnet.infura.io/ws/v3/key",
            )
            self.assertEqual(websocket_url("http://127.0.0.1:8545"), "ws://127.0.0.1:8545")
            self.assertIsNone(websocket_url("not a url"))


class EthHeadStreamTests(unittest.TestCase):
    def serve_heads(self, heads, reject=False):
        subscribed = threading.Event()

        def handler(connection):
            request = json.loads(connection.recv())
            self.assertEqual(request["method"], "eth_subscribe")
            self.assertEqual(request["params"], ["newHeads"])
            if reject:
                connection.send(json.dumps({"id": request["id"], "error": {"code": -32601}}))
                return
            connection.send(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": "0xsub"}))
            subscribed.set()
            for item in heads:
                connection.send(json.dumps({
                    "jsonrpc": "2.0",
                    "method": "eth_subscription",
                    "params": {"subscription": "0xsub", "result": item},
                }))
            try:
                connection.recv()
            except Exception:
                pass

        server = serve(handler, "127.0.0.1", 0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        port = server.socket.getsockname()[1]
        return f"ws://127.0.0.1:{port}", subscribed

    def wait_for(self, predicate, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        return False

    def test_tracks_newest_pushed_base_fee(self):
        url, _ = self.serve_heads([
            header(100, 2_000_000_000),
            header(101, 3_500_000_000),
            header(99, 9_000_000_000),  # older height from a lagging node
        ])
        stream = EthHeadStream(url).start()
        self.addCleanup(stream.stop)
        self.assertTrue(self.wait_for(lambda: stream.base_fee_wei() == 3_500_000_000))
        time.sleep(0.1)
        self.assertEqual(stream.base_fee_wei(), 3_500_000_000)
        self.assertEqual(str(stream.base_fee_gwei()), "3.5")

    def test_stale_or_missing_head_is_not_reported(self):
        url, _ = self.serve_heads([header(100, 2_000_000_000)])
        stream = EthHeadStream(url).start()
        self.addCleanup(stream.stop)
        self.assertTrue(self.wait_for(lambda: stream.base_fee_wei() is not None))
        self.assertIsNone(stream.base_fee_wei(max_age=-1))

    def test_rejected_subscription_reports_nothing(self):
        url, _ = self.serve_heads([], reject=True)
        stream = EthHeadStream(url).start()
        self.addCleanup(stream.stop)
        time.sleep(0.3)
        self.assertIsNone(stream.base_fee_wei())

    def test_unreachable_endpoint_reports_nothing(self):
        stream = EthHeadStream("ws://127.0.0.1:9").start()
        time.sleep(0.2)
        self.assertIsNone(stream.base_fee_wei())
        stream.stop()

    def test_shared_stream_starts_once(self):
        url, _ = self.serve_heads([header(5, 1)])
        self.addCleanup(eth_head_stream.stop)
        with patch.dict("os.environ", {"ETH_WS_URL": url}):
            first = eth_head_stream.start()
            self.assertIs(eth_head_stream.start(), first)
            self.assertIs(eth_head_stream.current(), first)
        eth_head_stream.stop()
        self.assertIsNone(eth_head_stream.current())


if __name__ == "__main__":
    unittest.main()
