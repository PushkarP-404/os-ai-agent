#!/usr/bin/env python3
import urllib.request
import json
import websocket
import sys
import argparse
import time

def get_websocket():
    try:
        req = urllib.request.urlopen('http://127.0.0.1:9222/json')
        pages = json.loads(req.read().decode('utf-8'))
    except Exception as e:
        print(f"Error connecting to Chrome CDP: {e}")
        sys.exit(1)
        
    page = next((p for p in pages if p['type'] == 'page'), None)
    if not page:
        print("No normal page found.")
        sys.exit(1)
        
    ws_url = page.get('webSocketDebuggerUrl')
    if not ws_url:
        print("No webSocketDebuggerUrl found.")
        sys.exit(1)
        
    return websocket.create_connection(ws_url), page

def call_cdp(ws, method, params=None):
    req_id = int(time.time() * 1000) % 1000000
    req = {"id": req_id, "method": method}
    if params:
        req["params"] = params
    ws.send(json.dumps(req))
    
    while True:
        res = json.loads(ws.recv())
        if res.get('id') == req_id:
            return res.get('result', {})

def cmd_dump(ws, page):
    result = call_cdp(ws, "Accessibility.getFullAXTree")
    nodes = result.get('nodes', [])
    if not nodes:
        print("No Accessibility Nodes returned.")
        return
        
    print(f"Active Tab: {page.get('title', 'Unknown')}")
    print("Elements:")
    
    for node in nodes:
        role = node.get('role', {}).get('value', '')
        name = node.get('name', {}).get('value', '')
        
        if not role or role in ['genericContainer', 'LineBreak', 'StaticText']:
            if role == 'StaticText' and name:
                print(f"  [-] Text: \"{name}\"")
            continue
            
        if name:
            print(f"  [{node['nodeId']}] {role}: \"{name}\"")
        else:
            if role in ['button', 'link', 'textbox', 'searchbox', 'checkbox']:
                print(f"  [{node['nodeId']}] {role}")

def cmd_click(ws, backend_node_id):
    # Resolve backend node to DOM node
    dom_doc = call_cdp(ws, "DOM.getDocument")
    node_result = call_cdp(ws, "DOM.resolveNode", {"backendNodeId": int(backend_node_id)})
    
    obj_id = node_result.get('object', {}).get('objectId')
    if not obj_id:
        print(f"Failed to resolve node {backend_node_id}")
        return
        
    # Scroll into view
    call_cdp(ws, "Runtime.callFunctionOn", {
        "objectId": obj_id,
        "functionDeclaration": "function() { this.scrollIntoViewIfNeeded(); }",
    })
    
    # Click it
    call_cdp(ws, "Runtime.callFunctionOn", {
        "objectId": obj_id,
        "functionDeclaration": "function() { this.click(); }",
    })
    print(f"Clicked node {backend_node_id}")

def cmd_type(ws, backend_node_id, text):
    # Resolve backend node to DOM node
    dom_doc = call_cdp(ws, "DOM.getDocument")
    node_result = call_cdp(ws, "DOM.resolveNode", {"backendNodeId": int(backend_node_id)})
    
    obj_id = node_result.get('object', {}).get('objectId')
    if not obj_id:
        print(f"Failed to resolve node {backend_node_id}")
        return
        
    # Focus
    call_cdp(ws, "Runtime.callFunctionOn", {
        "objectId": obj_id,
        "functionDeclaration": "function() { this.focus(); }",
    })
    
    # Clear value
    call_cdp(ws, "Runtime.callFunctionOn", {
        "objectId": obj_id,
        "functionDeclaration": "function() { this.value = ''; }",
    })
    
    # Insert text
    for char in text:
        call_cdp(ws, "Input.insertText", {"text": char})
    print(f"Typed '{text}' into node {backend_node_id}")
    
def cmd_goto(ws, url):
    call_cdp(ws, "Page.navigate", {"url": url})
    print(f"Navigated to {url}")

def main():
    parser = argparse.ArgumentParser(description="CDP Controller for AI Agent")
    parser.add_argument('action', choices=['dump', 'click', 'type', 'goto'])
    parser.add_argument('--id', type=int, help="Backend Node ID")
    parser.add_argument('--text', type=str, help="Text to type")
    parser.add_argument('--url', type=str, help="URL to navigate to")
    
    args = parser.parse_args()
    
    ws, page = get_websocket()
    
    if args.action == 'dump':
        cmd_dump(ws, page)
    elif args.action == 'click':
        if args.id is None:
            print("--id is required for click")
            sys.exit(1)
        cmd_click(ws, args.id)
    elif args.action == 'type':
        if args.id is None or args.text is None:
            print("--id and --text are required for type")
            sys.exit(1)
        cmd_type(ws, args.id, args.text)
    elif args.action == 'goto':
        if args.url is None:
            print("--url is required for goto")
            sys.exit(1)
        cmd_goto(ws, args.url)
        
    ws.close()

if __name__ == '__main__':
    main()
