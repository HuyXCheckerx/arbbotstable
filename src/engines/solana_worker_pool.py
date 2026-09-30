"""Warm, long-lived Solana engine processes for the sniper's route checks.

Each Solana route check used to start a new Node/tsx process: ~1.5 s of
startup and SDK imports plus fresh TLS handshakes to every RPC and quote
endpoint. The pool keeps one worker (src/engines/solana_engine_worker.ts) per
route. A worker runs one check at a time with the same argv and environment a
one-shot process would get, and returns the same exit code and output, so the
sniper's outcome parsing is unchanged. Any worker problem falls back to the
one-shot process; a worker that dies or times out mid-check is never retried.
"""

from __future__ import annotations

from dataclasses import dataclass
import itertools
import json
import logging
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import threading
import time
from typing import IO

RESPONSE_PREFIX = "@@ARBBOT_SOLANA_WORKER@@"
STARTUP_TIMEOUT_SECONDS = 90.0
IDLE_SECONDS = 600.0
MAX_JOBS_PER_WORKER = 500
DISABLE_AFTER_STARTUP_FAILURE_SECONDS = 600.0

logger = logging.getLogger("crosschain-sniper")


class WorkerUnavailable(RuntimeError):
    """The check was not delivered; running it one-shot is safe."""


class WorkerDied(RuntimeError):
    """The worker exited after receiving the check; its outcome is unknown."""


def _startup_environment(environment: dict[str, str]) -> tuple[tuple[str, str], ...]:
    # Node reads these once at startup; a change needs a new process.
    return tuple(sorted(
        (key, value) for key, value in environment.items() if key.startswith("NODE_")
    ))


def worker_launcher(project_root: Path) -> list[str] | None:
    """node --import tsx <worker>, or None when a local tsx install is missing."""
    node = shutil.which("node")
    worker = project_root / "src" / "engines" / "solana_engine_worker.ts"
    if not node or not worker.exists():
        return None
    if not (project_root / "node_modules" / "tsx" / "package.json").exists():
        return None
    return [node, "--import", "tsx", str(worker)]


class SolanaEngineWorker:
    def __init__(
        self,
        launcher: list[str],
        cwd: Path,
        environment: dict[str, str],
        log_path: Path | None = None,
    ) -> None:
        self.launcher = launcher
        self.startup_environment = _startup_environment(environment)
        self.jobs = 0
        self.last_used = time.monotonic()
        self._ids = itertools.count(1)
        self._responses: queue.Queue[dict | None] = queue.Queue()
        self._log: IO[bytes] | int = subprocess.DEVNULL
        if log_path is not None:
            try:
                log_path.parent.mkdir(parents=True, exist_ok=True)
                self._log = open(log_path, "ab")
            except OSError:
                self._log = subprocess.DEVNULL
        creationflags = 0
        if sys.platform == "win32":
            creationflags = subprocess.CREATE_NO_WINDOW
        try:
            self.process = subprocess.Popen(
                launcher,
                cwd=cwd,
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=self._log,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=creationflags,
            )
        except OSError as exc:
            self._close_log()
            raise WorkerUnavailable(f"could not start Solana worker: {exc}") from exc
        threading.Thread(
            target=self._read_responses,
            name=f"solana-worker-{self.process.pid}",
            daemon=True,
        ).start()
        ready = self._next_response(STARTUP_TIMEOUT_SECONDS)
        if not ready or not ready.get("ready"):
            self.close()
            raise WorkerUnavailable("Solana worker did not become ready")

    def _read_responses(self) -> None:
        stdout = self.process.stdout
        try:
            for line in stdout:
                if not line.startswith(RESPONSE_PREFIX):
                    continue
                try:
                    self._responses.put(json.loads(line[len(RESPONSE_PREFIX):]))
                except json.JSONDecodeError:
                    continue
        except (OSError, ValueError):
            pass
        self._responses.put(None)

    def _next_response(self, timeout: float) -> dict | None:
        try:
            return self._responses.get(timeout=max(0.0, timeout))
        except queue.Empty as exc:
            raise TimeoutError from exc

    def alive(self) -> bool:
        return self.process.poll() is None

    def run(
        self,
        argv: list[str],
        environment: dict[str, str],
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        request_id = next(self._ids)
        request = json.dumps({"id": request_id, "argv": argv, "env": environment})
        self.last_used = time.monotonic()
        try:
            self.process.stdin.write(request + "\n")
            self.process.stdin.flush()
        except (OSError, ValueError) as exc:
            self.close()
            raise WorkerUnavailable(f"Solana worker is not accepting checks: {exc}") from exc
        self.jobs += 1
        deadline = time.monotonic() + timeout
        while True:
            try:
                response = self._next_response(deadline - time.monotonic())
            except TimeoutError:
                # Same as subprocess.run: kill the engine that overran.
                self.close(force=True)
                raise subprocess.TimeoutExpired(self.launcher, timeout) from None
            if response is None:
                self.close()
                raise WorkerDied("Solana engine worker exited during the check")
            if response.get("id") == request_id:
                break
        self.last_used = time.monotonic()
        return subprocess.CompletedProcess(
            self.launcher,
            int(response.get("exitCode") or 0),
            stdout=str(response.get("stdout") or ""),
            stderr=str(response.get("stderr") or ""),
        )

    def _close_log(self) -> None:
        if self._log is not subprocess.DEVNULL:
            try:
                self._log.close()
            except OSError:
                pass
            self._log = subprocess.DEVNULL

    def close(self, force: bool = False) -> None:
        process = self.process
        if process.poll() is None:
            try:
                process.stdin.close()
            except (OSError, ValueError):
                pass
            try:
                # An idle worker exits by itself once its input closes.
                process.wait(timeout=0 if force else 2)
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
        self._close_log()


@dataclass
class _Slot:
    lock: threading.Lock
    worker: SolanaEngineWorker | None = None


class SolanaWorkerPool:
    """One warm worker per route key; each route runs one check at a time."""

    def __init__(
        self,
        project_root: Path,
        log_path: Path | None = None,
        *,
        idle_seconds: float = IDLE_SECONDS,
        max_jobs: int = MAX_JOBS_PER_WORKER,
    ) -> None:
        self.project_root = project_root
        self.log_path = log_path
        self.idle_seconds = idle_seconds
        self.max_jobs = max_jobs
        self._lock = threading.Lock()
        self._slots: dict[str, _Slot] = {}
        self._disabled_until = 0.0
        self._closed = False

    def available(self) -> bool:
        return not self._closed and time.monotonic() >= self._disabled_until

    def _slot(self, key: str) -> _Slot:
        with self._lock:
            slot = self._slots.get(key)
            if slot is None:
                slot = self._slots[key] = _Slot(threading.Lock())
            return slot

    def _worker(self, slot: _Slot, environment: dict[str, str]) -> SolanaEngineWorker:
        worker = slot.worker
        if worker is not None and (
            not worker.alive()
            or worker.jobs >= self.max_jobs
            or worker.startup_environment != _startup_environment(environment)
        ):
            worker.close()
            worker = slot.worker = None
        if worker is None:
            launcher = worker_launcher(self.project_root)
            if launcher is None:
                self._disable("local node/tsx install not found")
                raise WorkerUnavailable("local node/tsx install not found")
            try:
                worker = SolanaEngineWorker(
                    launcher, self.project_root, environment, self.log_path
                )
            except WorkerUnavailable as exc:
                self._disable(str(exc))
                raise
            slot.worker = worker
        return worker

    def _disable(self, reason: str) -> None:
        self._disabled_until = time.monotonic() + DISABLE_AFTER_STARTUP_FAILURE_SECONDS
        logger.warning(
            "WORKER  | Solana   | %s; using one-shot engine processes for %.0fs",
            reason,
            DISABLE_AFTER_STARTUP_FAILURE_SECONDS,
        )

    def run(
        self,
        key: str,
        argv: list[str],
        environment: dict[str, str],
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        if not self.available():
            raise WorkerUnavailable("Solana worker pool is disabled")
        self.reap_idle()
        slot = self._slot(key)
        with slot.lock:
            if self._closed:
                raise WorkerUnavailable("Solana worker pool is closed")
            worker = self._worker(slot, environment)
            try:
                return worker.run(argv, environment, timeout)
            finally:
                if not worker.alive():
                    slot.worker = None

    def prewarm(self, keys: list[str], environment: dict[str, str]) -> None:
        """Start workers in the background so the first checks skip startup."""
        def start(key: str) -> None:
            slot = self._slot(key)
            if not slot.lock.acquire(blocking=False):
                return
            try:
                if self.available() and not self._closed:
                    self._worker(slot, environment)
            except WorkerUnavailable:
                pass
            finally:
                slot.lock.release()

        for key in keys:
            threading.Thread(target=start, args=(key,), name="solana-worker-prewarm",
                             daemon=True).start()

    def reap_idle(self) -> None:
        cutoff = time.monotonic() - self.idle_seconds
        with self._lock:
            slots = list(self._slots.values())
        for slot in slots:
            if not slot.lock.acquire(blocking=False):
                continue
            try:
                if slot.worker is not None and slot.worker.last_used < cutoff:
                    slot.worker.close()
                    slot.worker = None
            finally:
                slot.lock.release()

    def close(self) -> None:
        self._closed = True
        with self._lock:
            slots = list(self._slots.values())
        for slot in slots:
            # A check still running keeps its worker until it finishes; the
            # sniper joins its chain threads before closing the pool.
            with slot.lock:
                if slot.worker is not None:
                    slot.worker.close()
                    slot.worker = None


def persistent_workers_enabled() -> bool:
    return os.getenv("SNIPER_SOLANA_PERSISTENT_WORKERS", "true").strip().lower() in (
        "1", "true", "yes", "on",
    )
