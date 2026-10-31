#!/usr/bin/env python3
"""cdp_controller.py — Elenchus Hybrid Browser CDP Controller

Supports dual-engine architecture:
- Lightpanda (port 9223): Prioritized for fast, low-memory, headless background agent tasks.
- Chromium (port 9222): Used for visual interactive tasks and screenshots.
"""
import urllib.request
import json
import base64
import websocket
import sys
import argparse
import time

_cdp_id_counter = 0

LIGHTPANDA_PORT = 9223
CHROMIUM_PORT = 9222

def _is_port_available(port: int, timeout: float = 0.8) -> bool:
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=timeout)
        return True
    except Exception:
        return False

def _call_cdp_raw(ws, method: str, params: dict | None = None, session_id: str | None = None, timeout: float = 5.0) -> dict:
    global _cdp_id_counter
    _cdp_id_counter += 1
    req_id = _cdp_id_counter
    req = {"id": req_id, "method": method}
    if params:
        req["params"] = params
    if session_id:
        req["sessionId"] = session_id
    ws.send(json.dumps(req))
    
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            msg = json.loads(ws.recv())
            if msg.get("id") == req_id:
                return msg.get("result", {})
        except Exception:
            break
    return {}

def get_websocket(action: str = "dump", engine: str = "auto", port: int | None = None):
    # Screenshots strictly require Chromium because Lightpanda has no visual rasterizer
    if action == "screenshot":
        engine = "chromium"
        port = port or CHROMIUM_PORT

    if engine == "auto":
        if port == LIGHTPANDA_PORT:
            engine = "lightpanda"
        elif port == CHROMIUM_PORT:
            engine = "chromium"
        elif _is_port_available(LIGHTPANDA_PORT):
            engine = "lightpanda"
            port = LIGHTPANDA_PORT
        elif _is_port_available(CHROMIUM_PORT):
            engine = "chromium"
            port = CHROMIUM_PORT
        else:
            # Fallback to Lightpanda default
            engine = "lightpanda"
            port = LIGHTPANDA_PORT

    if engine == "lightpanda":
        target_port = port or LIGHTPANDA_PORT
        ws_url = f"ws://127.0.0.1:{target_port}/"
        try:
            ws = websocket.create_connection(ws_url, timeout=5.0)
        except Exception as e:
            print(f"Error connecting to Lightpanda CDP on port {target_port}: {e}")
            sys.exit(1)

        # In Lightpanda, attach to target session
        t_res = _call_cdp_raw(ws, "Target.createTarget", {"url": "about:blank"})
        tid = t_res.get("targetId", "FID-0000000001")
        a_res = _call_cdp_raw(ws, "Target.attachToTarget", {"targetId": tid, "flatten": True})
        session_id = a_res.get("sessionId", "SID-1")
        return ws, {"title": "Lightpanda Headless Session", "engine": "lightpanda"}, session_id, "lightpanda"

    else:
        target_port = port or CHROMIUM_PORT
        try:
            req = urllib.request.urlopen(f"http://127.0.0.1:{target_port}/json", timeout=5.0)
            pages = json.loads(req.read().decode('utf-8'))
        except Exception as e:
            print(f"Error connecting to Chromium CDP on port {target_port}: {e}")
            sys.exit(1)

        page = next((p for p in pages if p.get('type') == 'page'), None)
        if not page:
            print("No normal page found in Chromium.")
            sys.exit(1)

        ws_url = page.get('webSocketDebuggerUrl')
        if not ws_url:
            print("No webSocketDebuggerUrl found in Chromium.")
            sys.exit(1)

        return websocket.create_connection(ws_url), page, None, "chromium"

def call_cdp(ws, method: str, params: dict | None = None, timeout: float = 10.0, session_id: str | None = None) -> dict:
    global _cdp_id_counter
    _cdp_id_counter += 1
    req_id = _cdp_id_counter

    req = {"id": req_id, "method": method}
    if params:
        req["params"] = params
    if session_id:
        req["sessionId"] = session_id
    ws.settimeout(timeout)
    ws.send(json.dumps(req))

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            res = json.loads(ws.recv())
            if res.get('id') == req_id:
                if 'error' in res:
                    return res
                return res.get('result', {})
        except websocket.WebSocketTimeoutException:
            break
        except Exception as e:
            print(f"CDP recv error: {e}")
            break
    raise TimeoutError(f"CDP call '{method}' timed out after {timeout}s")

def cmd_dump(ws, page, session_id=None, engine="chromium"):
    if engine == "lightpanda":
        # Extract title and markdown/text via Runtime.evaluate
        title_res = call_cdp(ws, "Runtime.evaluate", {"expression": "document.title"}, session_id=session_id)
        title = title_res.get("result", {}).get("value", "Untitled")
        print(f"Active Tab (Lightpanda): {title}")

        text_res = call_cdp(ws, "Runtime.evaluate", {"expression": "document.body ? document.body.innerText : ''"}, session_id=session_id)
        body_text = text_res.get("result", {}).get("value", "")
        print("Page Content:")
        for line in body_text.splitlines()[:50]:
            if line.strip():
                print(f"  {line.strip()}")
        return

    # Chromium Accessibility dump
    try:
        call_cdp(ws, "Accessibility.enable", timeout=3.0)
    except Exception:
        pass
    result = call_cdp(ws, "Accessibility.getFullAXTree")
    nodes = result.get('nodes', [])
    if not nodes:
        print("No Accessibility Nodes returned.")
        return

    print(f"Active Tab (Chromium): {page.get('title', 'Unknown')}")
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

def cmd_click(ws, backend_node_id, session_id=None):
    node_result = call_cdp(ws, "DOM.resolveNode", {"backendNodeId": int(backend_node_id)}, session_id=session_id)
    obj_id = node_result.get('object', {}).get('objectId')
    if not obj_id:
        print(f"Failed to resolve node {backend_node_id}")
        return

    call_cdp(ws, "Runtime.callFunctionOn", {
        "objectId": obj_id,
        "functionDeclaration": "function() { this.scrollIntoViewIfNeeded(); }",
    }, session_id=session_id)

    call_cdp(ws, "Runtime.callFunctionOn", {
        "objectId": obj_id,
        "functionDeclaration": "function() { this.click(); }",
    }, session_id=session_id)
    print(f"Clicked node {backend_node_id}")

def cmd_type(ws, backend_node_id, text, session_id=None):
    node_result = call_cdp(ws, "DOM.resolveNode", {"backendNodeId": int(backend_node_id)}, session_id=session_id)
    obj_id = node_result.get('object', {}).get('objectId')
    if not obj_id:
        print(f"Failed to resolve node {backend_node_id}")
        return

    call_cdp(ws, "Runtime.callFunctionOn", {
        "objectId": obj_id,
        "functionDeclaration": "function() { this.focus(); }",
    }, session_id=session_id)

    call_cdp(ws, "Runtime.callFunctionOn", {
        "objectId": obj_id,
        "functionDeclaration": "function() { this.value = ''; }",
    }, session_id=session_id)

    call_cdp(ws, "Input.insertText", {"text": text}, session_id=session_id)
    print(f"Typed '{text}' into node {backend_node_id}")

def cmd_goto(ws, url, session_id=None):
    call_cdp(ws, "Page.navigate", {"url": url}, session_id=session_id)
    print(f"Navigated to {url}")

def cmd_screenshot(ws, output_path):
    result = call_cdp(ws, "Page.captureScreenshot", {"format": "png"})
    data_b64 = result.get("data", "")
    if not data_b64:
        print("Failed to capture screenshot: empty data")
        sys.exit(1)
    with open(output_path, "wb") as f:
        f.write(base64.b64decode(data_b64))
    print(f"Screenshot saved to {output_path}")

def main():
    parser = argparse.ArgumentParser(description="Hybrid CDP Controller for Elenchus (Lightpanda & Chromium)")
    parser.add_argument('action', choices=['dump', 'click', 'type', 'goto', 'screenshot'])
    parser.add_argument('--id', type=int, help="Backend Node ID")
    parser.add_argument('--text', type=str, help="Text to type")
    parser.add_argument('--url', type=str, help="URL to navigate to")
    parser.add_argument('--path', type=str, help="Output path for screenshot")
    parser.add_argument('--engine', choices=['auto', 'lightpanda', 'chromium'], default='auto',
                        help="Browser engine to target (default: auto)")
    parser.add_argument('--port', type=int, default=None, help="CDP port override (9223=Lightpanda, 9222=Chromium)")

    args = parser.parse_args()

    ws, page, session_id, engine = get_websocket(action=args.action, engine=args.engine, port=args.port)

    if args.action == 'dump':
        cmd_dump(ws, page, session_id=session_id, engine=engine)
    elif args.action == 'click':
        if args.id is None:
            print("--id is required for click")
            sys.exit(1)
        cmd_click(ws, args.id, session_id=session_id)
    elif args.action == 'type':
        if args.id is None or args.text is None:
            print("--id and --text are required for type")
            sys.exit(1)
        cmd_type(ws, args.id, args.text, session_id=session_id)
    elif args.action == 'goto':
        if args.url is None:
            print("--url is required for goto")
            sys.exit(1)
        cmd_goto(ws, args.url, session_id=session_id)
    elif args.action == 'screenshot':
        if not args.path:
            print("--path is required for screenshot")
            sys.exit(1)
        cmd_screenshot(ws, args.path)

    ws.close()

if __name__ == '__main__':
    main()
