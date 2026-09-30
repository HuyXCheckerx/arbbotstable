"""Push-based Ethereum block headers over a JSON-RPC WebSocket (eth_subscribe newHeads).

The sniper's gas gate and transaction builder only need the latest block's
base fee. Polling eth_getBlockByNumber over HTTP costs a round trip (and up to
several endpoint timeouts) per check; a newHeads subscription delivers each
header as soon as the node sees it. Readers get a value only while it is
fresh, and otherwise fall back to their existing HTTP request, so a missing or
broken WebSocket never changes behavior, it only removes the speedup.
"""

from __future__ import annotations

from decimal import Decimal
import json
import logging
import os
import threading
import time
from urllib.parse import urlsplit, urlunsplit

logger = logging.getLogger("crosschain-sniper")

# Mainnet produces a block every 12 s. A header older than this may have been
# superseded without the subscription noticing.
FRESH_HEAD_SECONDS = 14.0
RECONNECT_MIN_SECONDS = 1.0
RECONNECT_MAX_SECONDS = 60.0


def websocket_url(rpc_url: str | None = None) -> str | None:
    """ETH_WS_URL, or the ws(s) form of the first ETH_RPC_URL endpoint."""
    explicit = os.getenv("ETH_WS_URL", "").strip()
    if explicit:
        return explicit if explicit.lower() not in ("0", "false", "off", "none") else None
    raw = (rpc_url or os.getenv("ETH_RPC_URL", "")).split(",")[0].strip()
    try:
        parts = urlsplit(raw)
    except ValueError:
        return None
    if parts.scheme in ("ws", "wss"):
        return raw
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    path = parts.path
    # Infura serves WebSockets on a separate path.
    if parts.hostname.endswith("infura.io") and path.startswith("/v3/"):
        path = "/ws" + path
    scheme = "wss" if parts.scheme == "https" else "ws"
    return urlunsplit((scheme, parts.netloc, path, parts.query, ""))


def safe_host(url: str) -> str:
    try:
        return urlsplit(url).hostname or "<invalid endpoint>"
    except ValueError:
        return "<invalid endpoint>"


class EthHeadStream:
    def __init__(self, url: str) -> None:
        self.url = url
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._base_fee_wei: int | None = None
        self._block_number: int | None = None
        self._received_at = 0.0
        self._thread: threading.Thread | None = None
        self._connection = None

    def start(self) -> "EthHeadStream":
        if self._thread is None:
            self._thread = threading.Thread(
                target=self._run, name="eth-head-stream", daemon=True
            )
            self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        connection = self._connection
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _record(self, header: dict) -> None:
        base_fee = header.get("baseFeePerGas")
        number = header.get("number")
        if not base_fee or not number:
            return
        with self._lock:
            number = int(number, 16)
            # Reorgs and multiple nodes can repeat heights; keep the newest.
            if self._block_number is not None and number < self._block_number:
                return
            self._base_fee_wei = int(base_fee, 16)
            self._block_number = number
            self._received_at = time.monotonic()

    def _run(self) -> None:
        from websockets.sync.client import connect

        delay = RECONNECT_MIN_SECONDS
        warned = False
        while not self._stop.is_set():
            try:
                with connect(
                    self.url,
                    open_timeout=10,
                    ping_interval=20,
                    ping_timeout=20,
                    max_size=2**20,
                    user_agent_header="arbbot-sniper/1.0",
                ) as connection:
                    self._connection = connection
                    connection.send(json.dumps({
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "eth_subscribe",
                        "params": ["newHeads"],
                    }))
                    subscribed = False
                    while not self._stop.is_set():
                        message = json.loads(connection.recv(timeout=60))
                        if message.get("id") == 1:
                            if "error" in message:
                                raise RuntimeError(f"eth_subscribe rejected: {message['error']}")
                            subscribed = True
                            logger.info(
                                "STREAM  | Ethereum | newHeads subscription active on %s",
                                safe_host(self.url),
                            )
                            delay = RECONNECT_MIN_SECONDS
                            warned = False
                            continue
                        header = (message.get("params") or {}).get("result")
                        if subscribed and isinstance(header, dict):
                            self._record(header)
            except Exception as exc:
                if self._stop.is_set():
                    break
                if not warned:
                    logger.warning(
                        "STREAM  | Ethereum | WebSocket %s unavailable (%s); "
                        "using HTTP block polling until it reconnects",
                        safe_host(self.url),
                        type(exc).__name__,
                    )
                    warned = True
            finally:
                self._connection = None
            if self._stop.wait(delay):
                break
            delay = min(delay * 2, RECONNECT_MAX_SECONDS)

    def base_fee_wei(self, max_age: float = FRESH_HEAD_SECONDS) -> int | None:
        with self._lock:
            if self._base_fee_wei is None or time.monotonic() - self._received_at > max_age:
                return None
            return self._base_fee_wei

    def base_fee_gwei(self, max_age: float = FRESH_HEAD_SECONDS) -> Decimal | None:
        wei = self.base_fee_wei(max_age)
        return None if wei is None else Decimal(wei) / Decimal(10**9)


_active: EthHeadStream | None = None
_active_lock = threading.Lock()


def start(rpc_url: str | None = None) -> EthHeadStream | None:
    """Start the shared stream once; None when no WebSocket URL is available."""
    global _active
    with _active_lock:
        if _active is not None:
            return _active
        url = websocket_url(rpc_url)
        if not url:
            return None
        _active = EthHeadStream(url).start()
        return _active


def current() -> EthHeadStream | None:
    return _active


def stop() -> None:
    global _active
    with _active_lock:
        stream, _active = _active, None
    if stream is not None:
        stream.stop()
