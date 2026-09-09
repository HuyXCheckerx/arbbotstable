import re
from curl_cffi import requests

s = requests.Session(impersonate='chrome124')
html = s.get('https://matcha.xyz').text
scripts = re.findall(r'src=["\']([^"\']+static/chunks/[^"\']+)["\']', html)
print(f"Found {len(scripts)} scripts")
for src in scripts:
    url = 'https://matcha.xyz' + src if src.startswith('/') else src
    js = s.get(url).text
    if 'swap/quote' in js:
        print('FOUND in', url)
        for m in re.finditer(r'swap/quote', js):
            idx = m.start()
            print('--- SNIPPET ---')
            print(js[max(0, idx-200):idx+300])
