import requests

content = requests.get('https://stable.com/assets/index-B7Mrqc6V.js').text
idx = content.find("const qOn=")
if idx == -1:
    idx = content.find("qOn=")
print(content[idx:idx+600])
