import winrm

s = winrm.Session('103.77.208.166', auth=('Administrator', 'etvQ#5xU?dZN5JU'), transport='ntlm')

test_py = '''
import urllib.request, json
try:
    with urllib.request.urlopen("http://127.0.0.1:8795/quote?input=PYUSD&output=USDG&amount=100000", timeout=5) as r:
        print("EVM:", r.read().decode())
except Exception as e:
    print("EVM ERR:", e)

try:
    with urllib.request.urlopen("http://127.0.0.1:8787/quote?inputMint=2b1kV6DkPAnxd5ixfnxCpjxmKwqjjaYmCZfHsFu24GXo&outputMint=2u1tszSeqZ3qBWF3uNGPFc8TzMk2tdiwknnRMWGWjGWH&amount=100000000000", timeout=5) as r:
        print("SOL:", r.read().decode()[:200])
except Exception as e:
    print("SOL ERR:", e)
'''

with open('/tmp/test_remote.py', 'w') as f:
    f.write(test_py)

import base64
b64 = base64.b64encode(test_py.encode()).decode()
s.run_ps(f"[System.IO.File]::WriteAllBytes('C:\\\\Users\\\\Administrator\\\\arbbotstable\\\\test_ports.py', [System.Convert]::FromBase64String('{b64}'))")

res = s.run_cmd(r'C:\Users\Administrator\arbbotstable\venv\Scripts\python.exe C:\Users\Administrator\arbbotstable\test_ports.py')
print(res.std_out.decode('utf-8', errors='replace'))
print(res.std_err.decode('utf-8', errors='replace'))
