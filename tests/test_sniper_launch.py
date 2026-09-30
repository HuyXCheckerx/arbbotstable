"""Launcher regressions run in fresh interpreters, without test sys.path changes."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class SniperLaunchTests(unittest.TestCase):
    def test_real_launchers_import_monitor_outside_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            for script in ('sniper.py', 'src/engines/crosschain_sniper.py'):
                with self.subTest(script=script):
                    result = subprocess.run([sys.executable, '-I', str(ROOT / script), '--help'],
                        cwd=directory, capture_output=True, text=True, timeout=20)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn('--interval-seconds', result.stdout)

    def test_both_workers_initialize_under_runpy_without_engine_search_path(self):
        code = r'''
import runpy, sys, threading
from decimal import Decimal
from unittest.mock import Mock
ns = runpy.run_path(sys.argv[1], run_name='launch_test')
stop = threading.Event()
stop.set()  # No network calls, subprocesses, or trading.
for chain in ('ethereum', 'solana'):
    ns['worker'](chain, [], live=False, base_threshold=Decimal('4'),
        interval_seconds=2, cooldown_seconds=15, timeout_seconds=30,
        cooldown_policy=ns['CooldownPolicy'](30, 300, 3600, 10, 300, 30, 60),
        backoff=ns['AdaptiveBackoff'](), once=True, stop=stop, logger=Mock())
'''
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, '-I', '-c', code,
                                     str(ROOT / 'src/engines/crosschain_sniper.py')],
                cwd=directory, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
