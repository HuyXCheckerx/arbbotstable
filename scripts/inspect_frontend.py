import requests
import re

headers = {"User-Agent": "Mozilla/5.0"}
html = requests.get("https://stable.com/swap", headers=headers).text
scripts = re.findall(r'src="([^"]+\.js[^"]*)"', html) + re.findall(r'href="([^"]+\.js[^"]*)"', html)
print(f"Found {len(scripts)} scripts:", scripts)

for s in scripts:
    url = f"https://stable.com{s}" if s.startswith("/") else s
    if "cookieyes" in url:
        continue
    js = requests.get(url, headers=headers).text
    print(f"\nFetched {url} (len: {len(js)})")
    for term in ["crossChain", "userLockSourceTransaction", "destinationLockTransactionId", "claim_cross_chain_dest", "ClaimCrossChainDest", "secretHash", "sessionAddress"]:
        pos = js.find(term)
        if pos != -1:
            print(f"  --> Match for '{term}' at pos {pos}:")
            print("     ", js[max(0, pos-100):pos+250])
