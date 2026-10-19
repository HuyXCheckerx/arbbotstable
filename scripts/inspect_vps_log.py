import winrm
import base64

s = winrm.Session('http://103.77.208.166:5985/wsman', auth=('administrator', 'etvQ#5xU?dZN5JU'), transport='ntlm')

code = """
import os, time

now = time.time()
root = r"C:\\Users\\Administrator"
for dirpath, dirnames, filenames in os.walk(root):
    if any(x in dirpath for x in ["venv", ".git", "node_modules", "logs", "AppData", ".cache"]):
        continue
    for f in filenames:
        fp = os.path.join(dirpath, f)
        try:
            mtime = os.path.getmtime(fp)
            if now - mtime < 48 * 3600:
                print(f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(mtime))} | {fp}")
        except Exception:
            pass
"""

b64 = base64.b64encode(code.encode('utf-8')).decode('utf-8')
ps = f"""
$bytes = [System.Convert]::FromBase64String('{b64}')
[System.IO.File]::WriteAllBytes('C:\\Users\\Administrator\\arbbotstable\\inspect_sample.py', $bytes)
python C:\\Users\\Administrator\\arbbotstable\\inspect_sample.py
"""

r = s.run_ps(ps)
print(r.std_out.decode('utf-8', errors='replace'))
print(r.std_err.decode('utf-8', errors='replace'))
