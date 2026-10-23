import requests
import re

content = requests.get('https://stable.com/assets/index-B7Mrqc6V.js').text
for m in re.finditer(r'createCrossChainSwap\(\{', content):
    idx = m.start()
    print("--- CALL SITE ---")
    print(content[idx:idx+400])
