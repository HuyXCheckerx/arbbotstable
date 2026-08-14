"""Bootstrap/update the repository and launch its process supervisor.

This file intentionally lives at the repository root so it can also be used as
the single uploaded entrypoint on Python-file-only hosting panels.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parent
REPOSITORY = os.environ.get(
    "GIT_REPOSITORY_URL",
    "https://github.com/HuyXCheckerx/arbbotstable.git",
)
BRANCH = os.environ.get("GIT_BRANCH", "main")


def git(*arguments: str) -> None:
    print("> git", *arguments, flush=True)
    subprocess.run(["git", *arguments], cwd=ROOT, check=True)


def configure_remote() -> None:
    remote = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if remote.returncode == 0:
        git("remote", "set-url", "origin", REPOSITORY)
    else:
        git("remote", "add", "origin", REPOSITORY)


def update_code() -> None:
    if shutil.which("git") is None:
        raise RuntimeError("Git is required for server_start.py but is not installed.")

    first_run = not (ROOT / ".git").is_dir()
    if first_run:
        print("[*] Creating .git and downloading the application...", flush=True)
        git("init", "-b", BRANCH)

    configure_remote()
    if first_run:
        git("fetch", "origin", BRANCH)
        git("reset", "--hard", f"origin/{BRANCH}")
        git("branch", "--set-upstream-to", f"origin/{BRANCH}", BRANCH)
    elif os.environ.get("SKIP_GIT_PULL", "0") != "1":
        print("[*] Updating code from GitHub...", flush=True)
        git("pull", "--ff-only", "origin", BRANCH)


def install_requirements() -> None:
    requirements = ROOT / "requirements.txt"
    if not requirements.is_file():
        raise RuntimeError(f"Missing {requirements}; Git bootstrap did not complete.")

    command = [sys.executable, "-m", "pip", "install"]
    if not os.environ.get("VIRTUAL_ENV"):
        command.append("--user")
    command.extend(["-r", str(requirements)])
    print(">", *command, flush=True)
    subprocess.run(command, cwd=ROOT, check=True)


def main() -> None:
    update_code()
    install_requirements()

    app = ROOT / "app.py"
    if not app.is_file():
        raise RuntimeError(f"Missing {app}; cannot start the bot.")
    print("[*] Starting app.py...", flush=True)
    os.execv(sys.executable, [sys.executable, str(app)])


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"[!] Startup failed: {error}", flush=True)
        raise
