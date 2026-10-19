import winrm

s = winrm.Session('http://103.77.208.166:5985/wsman', auth=('administrator', 'etvQ#5xU?dZN5JU'), transport='ntlm')

ps_script = r'''
$code = @'
path = r"C:\Users\Administrator\arbbotstable\.env"
with open(path, "r", encoding="utf-8") as f:
    text = f.read()

import re
text = re.sub(r"ETH_ARB_QUOTE_PROVIDER=.*", "ETH_ARB_QUOTE_PROVIDER=direct", text)
text = re.sub(r"ETH_ARB_DIRECT_AGGREGATORS=.*", "ETH_ARB_DIRECT_AGGREGATORS=velora", text)
text = re.sub(r"ETH_MIN_PRIORITY_FEE_GWEI=.*", "ETH_MIN_PRIORITY_FEE_GWEI=0.1", text)

with open(path, "w", encoding="utf-8") as f:
    f.write(text)

print("SUCCESS")
'@

Set-Content -Path "C:\Users\Administrator\arbbotstable\patch_env.py" -Value $code
python "C:\Users\Administrator\arbbotstable\patch_env.py"
'''

r = s.run_ps(ps_script)
print('STDOUT:', r.std_out.decode('utf-8', errors='replace'))
print('STDERR:', r.std_err.decode('utf-8', errors='replace'))


