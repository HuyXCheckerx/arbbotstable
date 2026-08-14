"""Supervise the scanner, dashboard, and recovery worker processes."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROCESS_SCRIPTS = {
    "scanner": PROJECT_ROOT / "src" / "engines" / "swapstable.py",
    "dashboard": PROJECT_ROOT / "src" / "web" / "web.py",
    "recovery": PROJECT_ROOT / "src" / "recovery" / "recovery_worker.py",
}


def start_script(
    label: str,
    script_path: Path,
    env: dict[str, str],
) -> subprocess.Popen[bytes]:
    print(f"[*] Starting {label}: {script_path.relative_to(PROJECT_ROOT)}", flush=True)
    return subprocess.Popen(
        [sys.executable, str(script_path)],
        cwd=PROJECT_ROOT,
        env=env,
    )


def child_environment() -> dict[str, str]:
    env = os.environ.copy()
    local_libraries = sorted(
        str(path)
        for path in (PROJECT_ROOT / ".local" / "lib").glob("python*/site-packages")
    )
    if local_libraries:
        existing = env.get("PYTHONPATH")
        env["PYTHONPATH"] = os.pathsep.join(
            local_libraries + ([existing] if existing else [])
        )
        print(f"[*] Added local package paths: {local_libraries}", flush=True)
    return env


def main() -> int:
    missing = [str(path) for path in PROCESS_SCRIPTS.values() if not path.is_file()]
    if missing:
        raise RuntimeError("Missing supervised scripts: " + ", ".join(missing))

    print("[*] Launching Stable.com arbitrage services...", flush=True)
    base_env = child_environment()
    process_envs: dict[str, dict[str, str]] = {}
    processes: dict[str, subprocess.Popen[bytes]] = {}
    for label, script_path in PROCESS_SCRIPTS.items():
        env = base_env.copy()
        if label == "recovery":
            env["BOT_LOG_NAME"] = "recovery_worker"
        process_envs[label] = env
        processes[label] = start_script(label, script_path, env)

    try:
        while True:
            time.sleep(1)
            for label, process in list(processes.items()):
                returncode = process.poll()
                if returncode is None:
                    continue
                print(
                    f"[!] {label} exited with status {returncode}; restarting in 5s...",
                    flush=True,
                )
                time.sleep(5)
                processes[label] = start_script(
                    label,
                    PROCESS_SCRIPTS[label],
                    process_envs[label],
                )
    except KeyboardInterrupt:
        print("\n[*] Shutting down...", flush=True)
        for process in processes.values():
            process.terminate()
        for process in processes.values():
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
