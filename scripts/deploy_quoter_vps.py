import winrm
import base64

s = winrm.Session('103.77.208.166', auth=('Administrator', 'etvQ#5xU?dZN5JU'), transport='ntlm')

with open('src/engines/local_quoter_service.py', 'rb') as f:
    raw = f.read()

b64 = base64.b64encode(raw).decode('ascii')
chunk_size = 2500
chunks = [b64[i:i+chunk_size] for i in range(0, len(b64), chunk_size)]

print(f"Total b64 length: {len(b64)}, chunks: {len(chunks)}")

# First chunk with Set-Content
res0 = s.run_ps(f"[System.IO.File]::WriteAllText('C:/Users/Administrator/arbbotstable/temp_b64.txt', '{chunks[0]}')")
if res0.std_err:
    print("Err 0:", res0.std_err.decode())

# Remaining chunks with append
for idx, chunk in enumerate(chunks[1:], start=1):
    res = s.run_ps(f"[System.IO.File]::AppendAllText('C:/Users/Administrator/arbbotstable/temp_b64.txt', '{chunk}')")
    if res.std_err:
        print(f"Err {idx}:", res.std_err.decode())

# Decode to target file
finish_ps = '''
$b64 = [System.IO.File]::ReadAllText('C:/Users/Administrator/arbbotstable/temp_b64.txt')
$bytes = [System.Convert]::FromBase64String($b64.Trim())
[System.IO.File]::WriteAllBytes('C:/Users/Administrator/arbbotstable/src/engines/local_quoter_service.py', $bytes)
Remove-Item 'C:/Users/Administrator/arbbotstable/temp_b64.txt' -Force
Write-Output "DECODED_SIZE: $($bytes.Length)"
'''
res_f = s.run_ps(finish_ps)
print(res_f.std_out.decode('utf-8', errors='replace'))
if res_f.std_err:
    print("Finish Err:", res_f.std_err.decode('utf-8', errors='replace'))
