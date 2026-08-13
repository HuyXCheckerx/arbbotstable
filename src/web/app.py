import multiprocessing
import subprocess
import sys
import time
import os
import glob

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

def run_script(script_name, env):
    print(f"[*] Starting {script_name}...")
    while True:
        try:
            script_path = os.path.join(BASE_DIR, script_name)
            subprocess.run([sys.executable, script_path], cwd=BASE_DIR, env=env, check=True)
        except Exception as e:
            print(f"[!] {script_name} crashed: {e}. Restarting in 5s...")
            time.sleep(5)

if __name__ == "__main__":
    print("[*] Launching Stable.com Arbitrage Bots...")
    
    # Pterodactyl usually installs to ./.local/lib/pythonX.Y/site-packages
    # We must explicitly add this to the PYTHONPATH so child processes find it
    env = os.environ.copy()
    local_libs = glob.glob(os.path.join(BASE_DIR, ".local", "lib", "python*", "site-packages"))
    if local_libs:
        existing_path = env.get("PYTHONPATH", "")
        # Append all found local site-packages paths
        env["PYTHONPATH"] = os.pathsep.join(local_libs + ([existing_path] if existing_path else []))
        print(f"[*] Injected local package paths: {local_libs}")
    else:
        print("[*] No local site-packages found, relying on global Python path.")

    # Create process for script
    p1 = multiprocessing.Process(target=run_script, args=("swapstable.py", env))
    p2 = multiprocessing.Process(target=run_script, args=("web.py", env))
    recovery_env = env.copy()
    recovery_env["BOT_LOG_NAME"] = "recovery_worker"
    p3 = multiprocessing.Process(target=run_script, args=("recovery_worker.py", recovery_env))
    
    # Start process
    p1.start()
    p2.start()
    p3.start()
    
    try:
        # Keep the main process alive
        p1.join()
        p2.join()
        p3.join()
    except KeyboardInterrupt:
        print("\n[*] Shutting down...")
        p1.terminate()
        p2.terminate()
        p3.terminate()
        sys.exit(0)
