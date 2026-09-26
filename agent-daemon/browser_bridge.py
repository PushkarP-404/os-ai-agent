import json
import urllib.request
import time
import sys

def get_dom_tree():
    try:
        # Ask Chromium for the active websocket debugger URL
        req = urllib.request.Request("http://127.0.0.1:9222/json")
        with urllib.request.urlopen(req) as response:
            data = json.loads(response.read().decode())
            
        if not data:
            return "Error: No active pages found."
            
        # Normally we would connect via websockets here
        # But for this simple bridge, we can just return the raw tabs as a proof-of-concept
        return json.dumps(data, indent=2)
    except Exception as e:
        return f"Error connecting to Chromium CDP: {e}"

if __name__ == "__main__":
    print(get_dom_tree())
