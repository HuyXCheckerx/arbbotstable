import requests
import re

content = requests.get("https://stable.com/assets/index-B7Mrqc6V.js").text

idx = content.find("function u_(")
print("--- u_ ---")
print(content[idx:idx+300])

idx2 = content.find("$Ve=")
if idx2 == -1:
    idx2 = content.find("function $Ve(")
print("--- $Ve ---")
print(content[idx2:idx2+300])
