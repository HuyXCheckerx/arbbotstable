The USER performed the following action:
Show the contents of file d:\Stuff\Stable.com Contracts\swapstable.py from lines 89 to 141
File Path: `file:///d:/Stuff/Stable.com%20Contracts/swapstable.py`
Total Lines: 357
Total Bytes: 13439
Showing lines 89 to 141
The following code has been modified to include a line number before every line, in the format: <line_number>: <original_line>. Please note that any changes targeting the original code should remove the line number, colon, and leading space.
89:     resp = session.post(f"{JUP_API}/swap", json={
90:         "quoteResponse": quote,
91:         "userPublicKey": str(keypair.pubkey()),
92:         "dynamicComputeUnitLimit": True,
93:         "dynamicSlippage": False,
94:         "wrapAndUnwrapSol": False,
95:         "prioritizationFeeLamports": {
96:             "priorityLevelWithMaxLamports": {
97:                 "maxLamports": PRIORITY_FEE,
98:                 "priorityLevel": "medium",
99:             }
100:         },
101:         "jitoTipLamports": JITO_TIP,
102:     }, timeout=15)
103:     
104:     if resp.status_code != 200:
105:         print(f"[!] Jup swap error: {resp.text}")
106:         return False
107:         
108:     tx_b64 = resp.json().get("swapTransaction", "")
109:     if not tx_b64:
110:         return False
111:         
112:     tx_bytes = base64.b64decode(tx_b64)
113:     tx = VersionedTransaction.from_bytes(tx_bytes)
114:     
115:     recent_blockhash = client.get_latest_blockhash().value.blockhash
116:     signed_tx = VersionedTransaction(tx.message, [keypair])
117:     
118:     result = client.send_transaction(signed_tx)
119:     if result.value:
120:         print(f"[+] Jup Swap Sent: {result.value}")
121:         client.confirm_transaction(result.value, commitment=Confirmed)
122:         return True
123:     return False
124: 
125: def get_stable_pool_info(session, wallet, asset_from, asset_to):
126:     resp = session.post(f"{STABLE_API}/swap/status", json={
127:         "chainFrom": str(STABLE_CHAIN_ID),
128:         "assetFrom": asset_from,
129:         "chainTo": str(STABLE_CHAIN_ID),
130:         "assetTo": asset_to,
131:         "gasLess": False,
132:         "amountFrom": "1000",
133:         "addressFrom": str(wallet),
134:         "addressTo": str(wallet),
135:     }, timeout=10)
136:     if resp.status_code == 200:
137:         data = resp.json()
138:         return data.get("asset", data)
139:     return None
140: 
141: def execute_stable_swap(session, client, keypair, asset_from, asset_to, amount_human):

