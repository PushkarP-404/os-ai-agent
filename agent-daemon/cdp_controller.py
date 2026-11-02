#!/usr/bin/env python3
"""CDP controller for AI Agent — supports Chromium and Lightpanda.

BUG-5 FIX (2026-11-01): Added Lightpanda (port 9223) as primary engine with
Chromium (9222) fallback. Previously hardcoded to port 9222, making Lightpanda
completely unreachable.

BUG-6 FIX (2026-11-01): cmd_dump() now handles both browsers:
  - Chromium: uses Accessibility.getFullAXTree (rich accessibility tree)
  - Lightpanda: uses Runtime.evaluate to extract innerText (Lightpanda does
    not implement the Accessibility domain but fully supports Runtime/DOM).
"""
import urllib.request
import json
import base64
import websocket
import sys
import argparse
import time
import threading

# Bug fix (2026-09-27): Use a monotonic counter for request IDs.
_cdp_id_counter = 0

# ── Browser port configuration ────────────────────────────────────────────────
CHROMIUM_PORT = 9222
LIGHTPANDA_PORT = 9223


def get_websocket(prefer_lightpanda: bool = True):
    """Connect to a CDP-compatible browser.

    Args:
        prefer_lightpanda: When True, try Lightpanda first (port 9223), then
                           fall back to Chromium (port 9222). When False,
                           always use Chromium.

    Returns:
        (websocket_connection, page_info_dict, browser_type_str)
    """
    ports_to_try: list[tuple[int, str]] = []
    if prefer_lightpanda:
        ports_to_try = [(LIGHTPANDA_PORT, "lightpanda"), (CHROMIUM_PORT, "chromium")]
    else:
        ports_to_try = [(CHROMIUM_PORT, "chromium"), (LIGHTPANDA_PORT, "lightpanda")]

    last_err = None
    for port, browser in ports_to_try:
        try:
            req = urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=3)
            pages = json.loads(req.read().decode("utf-8"))
            page = next((p for p in pages if p.get("type") == "page"), None)
            if not page:
                continue
            ws_url = page.get("webSocketDebuggerUrl")
            if not ws_url:
                continue
            ws = websocket.create_connection(ws_url, timeout=10)
            return ws, page, browser
        except Exception as exc:
            last_err = exc
            continue

    print(f"Error: Could not connect to Chromium ({CHROMIUM_PORT}) or Lightpanda ({LIGHTPANDA_PORT}): {last_err}")
    sys.exit(1)


def call_cdp(ws, method, params=None, timeout=10.0):
    """Send a Chrome DevTools Protocol command and wait for its response.

    Bug fix (2026-09-27): Previously used an infinite while True loop with no
    timeout, causing the daemon to hang forever if Chrome disconnects. Now uses
    a deadline-based loop with ws.settimeout().
    """
    global _cdp_id_counter
    _cdp_id_counter += 1
    req_id = _cdp_id_counter

    req = {"id": req_id, "method": method}
    if params:
        req["params"] = params
    ws.settimeout(timeout)
    ws.send(json.dumps(req))

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            res = json.loads(ws.recv())
            if res.get("id") == req_id:
                return res.get("result", {})
            # Ignore events/notifications (they have no 'id')
        except websocket.WebSocketTimeoutException:
            break
        except Exception as e:
            print(f"CDP recv error: {e}")
            break
    raise TimeoutError(f"CDP call '{method}' timed out after {timeout}s")


def cmd_dump(ws, page, browser: str = "chromium"):
    """Dump interactive elements from the active page.

    BUG-6 FIX: Lightpanda does not implement Accessibility.getFullAXTree.
    For Lightpanda we fall back to a Runtime.evaluate that extracts all
    interactive elements and their text via JS DOM queries.
    """
    print(f"Active Tab: {page.get('title', 'Unknown')} [{browser}]")
    print("Elements:")

    if browser == "lightpanda":
        # Lightpanda: use Runtime.evaluate to extract interactive elements via JS
        js = """
(function() {
  var sel = 'a, button, input, select, textarea, [role], h1, h2, h3, h4, label';
  var nodes = Array.from(document.querySelectorAll(sel));
  return nodes.slice(0, 200).map(function(el, i) {
    return {
      id: i,
      tag: el.tagName.toLowerCase(),
      role: el.getAttribute('role') || el.tagName.toLowerCase(),
      name: (el.innerText || el.value || el.placeholder || el.getAttribute('aria-label') || '').trim().slice(0, 80),
      href: el.href || '',
    };
  });
})();
"""
        try:
            result = call_cdp(ws, "Runtime.evaluate", {
                "expression": js,
                "returnByValue": True,
                "timeout": 5000,
            })
            items = result.get("result", {}).get("value", []) or []
            for item in items:
                if item.get("name"):
                    print(f"  [{item['id']}] {item['role']}: \"{item['name']}\"")
                else:
                    print(f"  [{item['id']}] {item['role']}")
        except Exception as e:
            print(f"  (Lightpanda DOM extract failed: {e})")
        return

    # Chromium: use Accessibility domain for a rich AX tree
    try:
        call_cdp(ws, "Accessibility.enable", timeout=3.0)
    except Exception:
        pass
    result = call_cdp(ws, "Accessibility.getFullAXTree")
    nodes = result.get("nodes", [])
    if not nodes:
        print("No Accessibility Nodes returned.")
        return

    for node in nodes:
        role = node.get("role", {}).get("value", "")
        name = node.get("name", {}).get("value", "")

        if not role or role in ["genericContainer", "LineBreak", "StaticText"]:
            if role == "StaticText" and name:
                print(f"  [-] Text: \"{name}\"")
            continue

        if name:
            print(f"  [{node['nodeId']}] {role}: \"{name}\"")
        else:
            if role in ["button", "link", "textbox", "searchbox", "checkbox"]:
                print(f"  [{node['nodeId']}] {role}")


def cmd_click(ws, backend_node_id):
    # Bug fix (2026-09-27): Removed unused dom_doc call that wasted a CDP round-trip
    node_result = call_cdp(ws, "DOM.resolveNode", {"backendNodeId": int(backend_node_id)})

    obj_id = node_result.get("object", {}).get("objectId")
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
    # Bug fix (2026-09-27): Removed unused dom_doc call + now sends full text in 1 CDP call
    # Previously typed one character at a time (100 WebSocket calls for 100-char string).
    node_result = call_cdp(ws, "DOM.resolveNode", {"backendNodeId": int(backend_node_id)})

    obj_id = node_result.get("object", {}).get("objectId")
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

    # Insert entire text in a single CDP call (was: one call per character)
    call_cdp(ws, "Input.insertText", {"text": text})
    print(f"Typed '{text}' into node {backend_node_id}")


def cmd_goto(ws, url):
    call_cdp(ws, "Page.navigate", {"url": url})
    print(f"Navigated to {url}")


def cmd_extract(ws, selector="body"):
    """Extract text content from elements matching selector via JS DOM evaluate."""
    js = f"""
(function() {{
  var sel = {json.dumps(selector)};
  var elements = Array.from(document.querySelectorAll(sel));
  if (elements.length === 0) {{
    elements = [document.body];
  }}
  return elements.slice(0, 50).map(function(el) {{
    return (el.innerText || el.textContent || '').trim();
  }}).filter(Boolean);
}})();
"""
    try:
        result = call_cdp(ws, "Runtime.evaluate", {
            "expression": js,
            "returnByValue": True,
            "timeout": 5000,
        })
        items = result.get("result", {}).get("value", []) or []
        for item in items:
            print(item)
    except Exception as e:
        print(f"Error extracting content: {e}")
        sys.exit(1)


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
    parser = argparse.ArgumentParser(description="CDP Controller for AI Agent (Chromium + Lightpanda)")
    parser.add_argument("action", choices=["dump", "click", "type", "goto", "screenshot", "extract"])
    parser.add_argument("--id", type=int, help="Backend Node ID")
    parser.add_argument("--text", type=str, help="Text to type")
    parser.add_argument("--url", type=str, help="URL to navigate to")
    parser.add_argument("--path", type=str, help="Output path for screenshot")
    parser.add_argument("--selector", type=str, default="body", help="CSS selector for extract action")
    parser.add_argument("--browser", choices=["auto", "chromium", "lightpanda"], default="auto",
                        help="Browser to target: auto (default, tries Lightpanda first), chromium, or lightpanda")

    args = parser.parse_args()

    prefer_lightpanda = args.browser != "chromium"
    ws, page, browser = get_websocket(prefer_lightpanda=prefer_lightpanda)

    if args.action == "dump":
        cmd_dump(ws, page, browser)
    elif args.action == "extract":
        cmd_extract(ws, selector=args.selector or "body")
    elif args.action == "click":
        if args.id is None:
            print("--id is required for click")
            sys.exit(1)
        cmd_click(ws, args.id)
    elif args.action == "type":
        if args.id is None or args.text is None:
            print("--id and --text are required for type")
            sys.exit(1)
        cmd_type(ws, args.id, args.text)
    elif args.action == "goto":
        if args.url is None:
            print("--url is required for goto")
            sys.exit(1)
        cmd_goto(ws, args.url)
    elif args.action == "screenshot":
        if not args.path:
            print("--path is required for screenshot")
            sys.exit(1)
        cmd_screenshot(ws, args.path)

    ws.close()


if __name__ == "__main__":
    main()
