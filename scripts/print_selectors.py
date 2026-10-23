import requests
import re
import json
import eth_utils

content = requests.get("https://stable.com/assets/index-B7Mrqc6V.js").text
m = re.search(r'Srn=JSON\.parse\(\'([^\']+)\'\)', content)
abi = json.loads(m.group(1).encode('utf-8').decode('unicode_escape'))

for item in abi:
    if item.get("type") == "error":
        types = ",".join(x["type"] for x in item.get("inputs", []))
        sig = f"{item['name']}({types})"
        selector = "0x" + eth_utils.keccak(text=sig)[:4].hex()
        if selector in ["0x8baa579f", "0xf525e320"]:
            print(f"MATCH: {selector} -> {sig}")
        else:
            print(f"{selector} -> {sig}")
