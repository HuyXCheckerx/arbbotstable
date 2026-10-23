import winrm
import time

host = "103.77.208.166"
auth = ("Administrator", "etvQ#5xU?dZN5JU")

print("Waiting for Windows VPS to complete reboot...")
start_time = time.time()
connected = False

for attempt in range(1, 40):
    time.sleep(6)
    elapsed = int(time.time() - start_time)
    try:
        s = winrm.Session(host, auth=auth, transport="ntlm")
        res = s.run_cmd("powershell -Command \"(Get-CimInstance Win32_OperatingSystem).LastBootUpTime\"")
        if res.status_code == 0 and res.std_out:
            boot_time = res.std_out.decode('utf-8', errors='replace').strip()
            if "September 29" in boot_time:
                print(f"[{elapsed}s] Server shutting down... (still showing old boot time)")
            else:
                print(f"[{elapsed}s] VPS is FRESHLY REBOOTED! LastBootUpTime: {boot_time}")
                connected = True
                break
        else:
            print(f"[{elapsed}s] WinRM returned status {res.status_code} (attempt {attempt})")
    except Exception as e:
        print(f"[{elapsed}s] Reconnect attempt {attempt}: machine is down / restarting...")

if not connected:
    print("Timeout waiting for fresh reboot.")
    exit(1)
