from curl_cffi import requests

s = requests.Session(impersonate='chrome124')
url = 'https://matcha.xyz/_next/static/chunks/1v9duoxm_9ard.js?dpl=dpl_7PVNSxBZmTtz4yKWiazfrZ23cQpo'
js = s.get(url).text
for target in ['INTENTS_QUOTE', 'SOLANA_SWAP_QUOTE']:
    idx = js.find(target)
    if idx != -1:
        print(f"--- {target} at {idx} ---")
        print(js[max(0, idx-200):min(len(js), idx+500)])
