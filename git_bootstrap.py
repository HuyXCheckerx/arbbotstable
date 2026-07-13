from pathlib import Path
import subprocess
import time

ROOT = Path("/home/container")
REPOSITORY = "https://github.com/HuyXCheckerx/arbbotstable.git"
BRANCH = "main"


def git(*arguments):
    print(">", "git", *arguments, flush=True)
    subprocess.run(
        ["git", *arguments],
        cwd=ROOT,
        check=True,
        text=True,
    )


if not (ROOT / ".git").is_dir():
    git("init", "-b", BRANCH)

remote = subprocess.run(
    ["git", "remote", "get-url", "origin"],
    cwd=ROOT,
    capture_output=True,
    text=True,
)

if remote.returncode == 0:
    git("remote", "set-url", "origin", REPOSITORY)
else:
    git("remote", "add", "origin", REPOSITORY)

git("fetch", "origin", BRANCH)
git("reset", "--hard", f"origin/{BRANCH}")
git("branch", "--set-upstream-to", f"origin/{BRANCH}", BRANCH)
git("config", "--local", "pull.ff", "only")
git("status", "--short", "--branch")

print()
print("GIT BOOTSTRAP COMPLETE")
print("Stop the server, restore .env, set App Py File to app.py, then start again.")
print("The bootstrap will remain alive until you press Stop.", flush=True)

try:
    while True:
        time.sleep(3600)
except KeyboardInterrupt:
    pass