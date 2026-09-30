import json
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.engines import solana_worker_pool as pool_module
from src.engines.solana_worker_pool import (
    RESPONSE_PREFIX,
    SolanaEngineWorker,
    SolanaWorkerPool,
    WorkerDied,
    WorkerUnavailable,
    worker_launcher,
)

# Speaks the worker protocol: "sleep" hangs, "die" exits mid-check, anything
# else echoes its argv and one environment value.
FAKE_WORKER = textwrap.dedent(
    f"""
    import json, os, sys, time
    PREFIX = {RESPONSE_PREFIX!r}
    if os.environ.get("FAKE_FAIL_STARTUP"):
        sys.exit(1)
    print(PREFIX + json.dumps({{"ready": True}}), flush=True)
    for line in sys.stdin:
        request = json.loads(line)
        argv = request["argv"]
        if argv == ["sleep"]:
            time.sleep(60)
        if argv == ["die"]:
            os._exit(3)
        print("noise without the prefix", flush=True)
        print(PREFIX + json.dumps({{
            "id": request["id"],
            "exitCode": 1 if argv == ["fail"] else 0,
            "stdout": "argv=" + " ".join(argv) + " pid=" + str(os.getpid()),
            "stderr": request["env"].get("ROUTE", ""),
        }}), flush=True)
    """
)


class SolanaWorkerPoolTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        script = Path(self.directory.name) / "fake_worker.py"
        script.write_text(FAKE_WORKER, encoding="utf-8")
        launcher = [sys.executable, str(script)]
        patcher = patch.object(pool_module, "worker_launcher", return_value=launcher)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.pool = SolanaWorkerPool(Path(self.directory.name))
        self.addCleanup(self.pool.close)

    def test_reuses_one_process_per_route_and_returns_engine_output(self):
        first = self.pool.run("route-a", ["--swap-order", "dex-first"], {"ROUTE": "a"}, 10)
        second = self.pool.run("route-a", ["--quote-only"], {"ROUTE": "a2"}, 10)
        other = self.pool.run("route-b", ["fail"], {"ROUTE": "b"}, 10)

        self.assertEqual(first.returncode, 0)
        self.assertIn("argv=--swap-order dex-first", first.stdout)
        self.assertEqual(first.stderr, "a")
        self.assertEqual(second.stderr, "a2")
        pid = lambda result: result.stdout.rsplit("pid=", 1)[1]
        self.assertEqual(pid(first), pid(second))
        self.assertNotEqual(pid(first), pid(other))
        self.assertEqual(other.returncode, 1)

    def test_timeout_stops_the_worker_and_next_check_gets_a_new_one(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            self.pool.run("route-a", ["sleep"], {}, 0.5)
        result = self.pool.run("route-a", ["ok"], {}, 10)
        self.assertEqual(result.returncode, 0)

    def test_worker_exit_during_a_check_is_reported_not_retried(self):
        with self.assertRaises(WorkerDied):
            self.pool.run("route-a", ["die"], {}, 10)
        self.assertEqual(self.pool.run("route-a", ["ok"], {}, 10).returncode, 0)

    def test_startup_failure_disables_pool_for_one_shot_fallback(self):
        with self.assertRaises(WorkerUnavailable):
            self.pool.run("route-a", ["ok"], {"FAKE_FAIL_STARTUP": "1"}, 10)
        self.assertFalse(self.pool.available())
        with self.assertRaises(WorkerUnavailable):
            self.pool.run("route-a", ["ok"], {}, 10)

    def test_node_startup_setting_change_restarts_worker(self):
        pid = lambda result: result.stdout.rsplit("pid=", 1)[1]
        first = self.pool.run("route-a", ["ok"], {}, 10)
        second = self.pool.run("route-a", ["ok"], {"NODE_OPTIONS": "--no-warnings"}, 10)
        self.assertNotEqual(pid(first), pid(second))

    def test_closed_pool_rejects_checks(self):
        self.pool.run("route-a", ["ok"], {}, 10)
        self.pool.close()
        self.assertFalse(self.pool.available())
        with self.assertRaises(WorkerUnavailable):
            self.pool.run("route-a", ["ok"], {}, 10)


@unittest.skipUnless(worker_launcher(ROOT), "node and a local tsx install are required")
class RealSolanaWorkerTests(unittest.TestCase):
    def test_engine_errors_match_one_shot_output(self):
        worker = SolanaEngineWorker(worker_launcher(ROOT), ROOT, {"PATH": ""})
        self.addCleanup(worker.close)
        result = worker.run(["--bogus"], {}, 60)
        self.assertEqual(result.returncode, 1)
        self.assertIn("ERROR: Unknown argument: --bogus", result.stderr)
        # The process survives an engine error and serves the next check.
        again = worker.run(["--also-bogus"], {}, 60)
        self.assertIn("Unknown argument: --also-bogus", again.stderr)
        self.assertTrue(worker.alive())


if __name__ == "__main__":
    unittest.main()
