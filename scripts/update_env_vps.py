import os

import winrm

# The VPS login comes from the environment (VPS_WINRM_HOST, VPS_WINRM_USER, VPS_WINRM_PASSWORD):
# this repository is public.

s = winrm.Session(
    os.environ["VPS_WINRM_HOST"],
    auth=(os.environ["VPS_WINRM_USER"], os.environ["VPS_WINRM_PASSWORD"]),
    transport="ntlm",
)

ps_script = r'''
$code = @'
path = r"C:\Users\Administrator\arbbotstable\.env"
with open(path, "r", encoding="utf-8") as f:
    text = f.read()

import re
if "ETH_ARB_MIN_PROFIT=" in text:
    text = re.sub(r"ETH_ARB_MIN_PROFIT=.*", "ETH_ARB_MIN_PROFIT=2", text)
else:
    text += "\nETH_ARB_MIN_PROFIT=2\n"

if "ETH_ARB_MIN_PROFIT_USD=" in text:
    text = re.sub(r"ETH_ARB_MIN_PROFIT_USD=.*", "ETH_ARB_MIN_PROFIT_USD=2", text)
else:
    text += "ETH_ARB_MIN_PROFIT_USD=2\n"

if "ETH_ARB_DIRECT_AGGREGATORS=" in text:
    text = re.sub(r"ETH_ARB_DIRECT_AGGREGATORS=.*", "ETH_ARB_DIRECT_AGGREGATORS=okx,velora", text)
else:
    text += "ETH_ARB_DIRECT_AGGREGATORS=okx,velora\n"

if "ETH_GAS_PRICE_GWEI=" in text:
    text = re.sub(r"ETH_GAS_PRICE_GWEI=.*", "ETH_GAS_PRICE_GWEI=0.25", text)
else:
    text += "ETH_GAS_PRICE_GWEI=0.25\n"

with open(path, "w", encoding="utf-8") as f:
    f.write(text)

print("SUCCESS: Set ETH_ARB_DIRECT_AGGREGATORS=okx,velora")
'@

Set-Content -Path "C:\Users\Administrator\arbbotstable\patch_env.py" -Value $code
python "C:\Users\Administrator\arbbotstable\patch_env.py"
'''

r = s.run_ps(ps_script)
print('STDOUT:', r.std_out.decode('utf-8', errors='replace'))
print('STDERR:', r.std_err.decode('utf-8', errors='replace'))


