import json
import urllib.request
payload = {'prompt': 'hi', 'n_predict': 128}
data = json.dumps(payload).encode('utf-8')
req = urllib.request.Request('http://127.0.0.1:11434/completion', data=data, headers={'Content-Type': 'application/json'})
try:
    with urllib.request.urlopen(req, timeout=30) as resp:
        print(resp.read().decode())
except Exception as e:
    print(e)
