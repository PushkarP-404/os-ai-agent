#!/usr/bin/env python3
import urllib.request
import json
import websocket
import sys

def main():
    # 1. Fetch available pages
    try:
        req = urllib.request.urlopen('http://127.0.0.1:9222/json')
        pages = json.loads(req.read().decode('utf-8'))
    except Exception as e:
        print(f"Error connecting to Chrome CDP: {e}")
        sys.exit(1)
        
    if not pages:
        print("No pages found in Chrome.")
        sys.exit(1)
        
    # Find a page that is a normal tab
    page = next((p for p in pages if p['type'] == 'page'), None)
    if not page:
        print("No normal page found.")
        sys.exit(1)
        
    ws_url = page.get('webSocketDebuggerUrl')
    if not ws_url:
        print("No webSocketDebuggerUrl found.")
        sys.exit(1)
        
    # 2. Connect via WebSocket
    ws = websocket.create_connection(ws_url)
    
    # 3. Request Accessibility Tree
    req_id = 1
    req = {
        "id": req_id,
        "method": "Accessibility.getFullAXTree"
    }
    ws.send(json.dumps(req))
    
    # Wait for response
    result = None
    while True:
        res = json.loads(ws.recv())
        if res.get('id') == req_id:
            result = res
            break
            
    ws.close()
    
    # 4. Print structured text
    nodes = result.get('result', {}).get('nodes', [])
    if not nodes:
        print("No Accessibility Nodes returned.")
        sys.exit(1)
        
    print(f"Active Tab: {page.get('title', 'Unknown')}")
    print("Elements:")
    
    # Simple formatting
    for node in nodes:
        # Ignore nodes that aren't very useful to a text LLM
        role = node.get('role', {}).get('value', '')
        name = node.get('name', {}).get('value', '')
        
        # Only print nodes with a role and either a name or being interactive
        if not role or role in ['genericContainer', 'LineBreak', 'StaticText']:
            # static text is useful if we want to read, but let's see
            if role == 'StaticText' and name:
                print(f"  [-] Text: \"{name}\"")
            continue
            
        if name:
            print(f"  [{node['nodeId']}] {role}: \"{name}\"")
        else:
            # Print interactive roles even without name
            if role in ['button', 'link', 'textbox', 'searchbox', 'checkbox']:
                print(f"  [{node['nodeId']}] {role}")

if __name__ == '__main__':
    main()
