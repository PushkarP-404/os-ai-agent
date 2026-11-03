#!/usr/bin/env python3
"""CDP controller for AI Agent — supports Chromium and Lightpanda.

BUG-5 FIX (2026-11-01): Added Lightpanda (port 9223) as primary engine with
Chromium (9222) fallback. Previously hardcoded to port 9222, making Lightpanda
completely unreachable.

BUG-6 FIX (2026-11-01): cmd_dump() now handles both browsers:
  - Chromium: uses Accessibility.getFullAXTree (rich accessibility tree)
  - Lightpanda: uses Runtime.evaluate to extract interactive elements via JS DOM
    queries. Elements are tagged with a data-elc-id attribute and their query
    selector is stored so that click/type can target them correctly.

IMPROVE (2026-11-03): Multiple robustness improvements:
  - P0: Lightpanda click/type now use CSS selector via data-elc-id rather than
        DOM.resolveNode(backendNodeId), which is incompatible with Lightpanda's
        JS-generated element IDs.
  - P1: cmd_goto() waits for Page.loadEventFired (or readyState complete) with
        a configurable timeout so the agent never acts on a half-loaded page.
  - P2: cmd_screenshot() falls back to scrot/import if Page.captureScreenshot
        is unsupported (Lightpanda does not implement it).
  - P3: call_cdp() now propagates CDP-level errors instead of silently returning
        empty dicts.
  - P4: _run_cdp() in harness_v2 passes --browser flag per call.
  - P5: get_websocket() retries up to 3× with 1 s sleep for slow starts.
"""
import urllib.request
import json
import base64
import subprocess
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

# Attribute injected into DOM nodes so Lightpanda click/type can re-find them
_LP_ATTR = "data-elc-id"


def get_websocket(prefer_lightpanda: bool = True):
    """Connect to a CDP-compatible browser.

    P5 FIX: Retries each port up to 3 times with 1-second backoff before
    moving on, to handle the case where the browser is still starting up.

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
        for attempt in range(3):  # P5: retry up to 3 times
            try:
                req = urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=3)
                pages = json.loads(req.read().decode("utf-8"))
                page = next((p for p in pages if p.get("type") == "page"), None)
                if not page:
                    break  # No page tab — don't retry, move to next browser
                ws_url = page.get("webSocketDebuggerUrl")
                if not ws_url:
                    break
                ws = websocket.create_connection(ws_url, timeout=10)
                return ws, page, browser
            except Exception as exc:
                last_err = exc
                if attempt < 2:
                    time.sleep(1)  # Wait before retrying
                continue

    print(
        f"Error: Could not connect to Chromium ({CHROMIUM_PORT}) or "
        f"Lightpanda ({LIGHTPANDA_PORT}): {last_err}"
    )
    sys.exit(1)


class CDPError(RuntimeError):
    """Raised when the browser returns a CDP-level error response."""
    pass


def call_cdp(ws, method, params=None, timeout=10.0):
    """Send a Chrome DevTools Protocol command and wait for its response.

    P3 FIX: Now raises CDPError if the browser returns {"error": ...} in the
    response, rather than silently returning an empty dict.

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
                # P3: propagate CDP-level errors
                if "error" in res:
                    err = res["error"]
                    raise CDPError(
                        f"CDP error for '{method}': [{err.get('code')}] {err.get('message')}"
                    )
                return res.get("result", {})
            # Ignore events/notifications (they have no 'id')
        except CDPError:
            raise  # Re-raise CDP errors as-is
        except websocket.WebSocketTimeoutException:
            break
        except Exception as e:
            print(f"CDP recv error: {e}")
            break
    raise TimeoutError(f"CDP call '{method}' timed out after {timeout}s")


def _wait_for_load(ws, timeout=10.0):
    """Wait until the page's readyState is 'complete'.

    P1 FIX: Polls document.readyState every 200 ms up to timeout seconds.
    Works for both Chromium and Lightpanda since both support Runtime.evaluate.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            result = call_cdp(ws, "Runtime.evaluate", {
                "expression": "document.readyState",
                "returnByValue": True,
                "timeout": 2000,
            }, timeout=3.0)
            state = result.get("result", {}).get("value", "")
            if state == "complete":
                return
        except Exception:
            pass
        time.sleep(0.2)
    # Don't crash if we time out — just proceed with whatever state we have


def cmd_dump(ws, page, browser: str = "chromium"):
    """Dump interactive elements from the active page.

    P0 FIX (Lightpanda): Elements are tagged with data-elc-id attributes in the
    DOM. The printed IDs correspond to those attributes, so cmd_click/cmd_type
    can re-locate elements by CSS [data-elc-id="N"] rather than trying to use
    DOM.resolveNode with Chromium backendNodeIds (incompatible with Lightpanda).

    BUG-6 FIX: Lightpanda does not implement Accessibility.getFullAXTree.
    For Lightpanda we fall back to a Runtime.evaluate that extracts all
    interactive elements and their text via JS DOM queries.
    """
    print(f"Active Tab: {page.get('title', 'Unknown')} [{browser}]")
    print("Elements:")

    if browser == "lightpanda":
        # Lightpanda: inject data-elc-id attributes then extract element info.
        # Storing the attribute in the DOM means click/type can re-find the
        # exact element with document.querySelector('[data-elc-id="N"]').
        js = f"""
(function() {{
  var sel = 'a, button, input, select, textarea, [role], h1, h2, h3, h4, label';
  var nodes = Array.from(document.querySelectorAll(sel));
  return nodes.slice(0, 200).map(function(el, i) {{
    el.setAttribute('{_LP_ATTR}', String(i));
    return {{
      id: i,
      tag: el.tagName.toLowerCase(),
      role: el.getAttribute('role') || el.tagName.toLowerCase(),
      name: (el.innerText || el.value || el.placeholder || el.getAttribute('aria-label') || '').trim().slice(0, 80),
      href: el.href || '',
      type: el.type || '',
    }};
  }});
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
                name_part = f': "{item["name"]}"' if item.get("name") else ""
                href_part = f' -> {item["href"]}' if item.get("href") else ""
                type_part = f' [{item["type"]}]' if item.get("type") else ""
                print(f"  [{item['id']}] {item['role']}{type_part}{name_part}{href_part}")
        except CDPError as e:
            print(f"  (Lightpanda DOM extract failed: {e})")
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


def _lp_resolve_element(ws, element_id) -> str:
    """Resolve a Lightpanda element ID to a JS objectId via data-elc-id attr.

    P0 FIX: Returns the Runtime objectId for use with Runtime.callFunctionOn,
    or raises if the element cannot be found.
    """
    js = f"document.querySelector('[{_LP_ATTR}=\"{int(element_id)}\"]')"
    try:
        result = call_cdp(ws, "Runtime.evaluate", {
            "expression": js,
            "returnByValue": False,  # Need objectId, not value
            "timeout": 3000,
        })
        obj = result.get("result", {})
        if obj.get("subtype") == "null" or not obj.get("objectId"):
            raise ValueError(
                f"Element with {_LP_ATTR}={element_id} not found. "
                "Run browser.dump first to tag elements."
            )
        return obj["objectId"]
    except CDPError:
        raise


def cmd_click(ws, element_id, browser: str = "chromium"):
    """Click an element by ID.

    P0 FIX: For Lightpanda, resolves element via data-elc-id CSS attribute
    instead of DOM.resolveNode (which requires Chromium backendNodeIds).
    """
    if browser == "lightpanda":
        obj_id = _lp_resolve_element(ws, element_id)
        call_cdp(ws, "Runtime.callFunctionOn", {
            "objectId": obj_id,
            "functionDeclaration": "function() { this.scrollIntoView({block:'center'}); this.click(); }",
        })
        print(f"Clicked element {element_id} [{browser}]")
        return

    # Chromium path: use DOM.resolveNode with backendNodeId
    # Bug fix (2026-09-27): Removed unused dom_doc call that wasted a CDP round-trip
    node_result = call_cdp(ws, "DOM.resolveNode", {"backendNodeId": int(element_id)})
    obj_id = node_result.get("object", {}).get("objectId")
    if not obj_id:
        print(f"Failed to resolve node {element_id}")
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
    print(f"Clicked node {element_id} [{browser}]")


def cmd_type(ws, element_id, text, browser: str = "chromium"):
    """Type text into an element.

    P0 FIX: For Lightpanda, resolves element via data-elc-id CSS attribute
    instead of DOM.resolveNode. Also dispatches an 'input' event so reactive
    frameworks (React, Vue) pick up the change.
    """
    if browser == "lightpanda":
        obj_id = _lp_resolve_element(ws, element_id)
        escaped = json.dumps(text)
        call_cdp(ws, "Runtime.callFunctionOn", {
            "objectId": obj_id,
            "functionDeclaration": (
                f"function() {{"
                f"  this.focus();"
                f"  this.value = {escaped};"
                f"  this.dispatchEvent(new Event('input', {{bubbles:true}}));"
                f"  this.dispatchEvent(new Event('change', {{bubbles:true}}));"
                f"}}"
            ),
        })
        print(f"Typed into element {element_id} [{browser}]")
        return

    # Chromium path
    # Bug fix (2026-09-27): Removed unused dom_doc call + now sends full text in 1 CDP call
    # Previously typed one character at a time (100 WebSocket calls for 100-char string).
    node_result = call_cdp(ws, "DOM.resolveNode", {"backendNodeId": int(element_id)})
    obj_id = node_result.get("object", {}).get("objectId")
    if not obj_id:
        print(f"Failed to resolve node {element_id}")
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
    print(f"Typed '{text}' into node {element_id} [{browser}]")


def cmd_goto(ws, url, browser: str = "chromium"):
    """Navigate to a URL and wait for the page to fully load.

    P1 FIX: After Page.navigate, polls document.readyState until 'complete'
    (or 10 s timeout). Previously returned immediately, so browser.dump called
    right after could see an empty/mid-load page.
    """
    call_cdp(ws, "Page.navigate", {"url": url})
    _wait_for_load(ws, timeout=10.0)
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
    except CDPError as e:
        print(f"Error extracting content: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"Error extracting content: {e}")
        sys.exit(1)


def cmd_screenshot(ws, output_path, browser: str = "chromium"):
    """Capture a screenshot and save it to output_path.

    P2 FIX: If Page.captureScreenshot fails (Lightpanda does not implement it),
    falls back to `scrot` (X11 screen capture), then `import -window root`
    (ImageMagick). If neither is available, prints a clear error.
    """
    # Try CDP Page.captureScreenshot first (works on Chromium; may fail on LP)
    try:
        result = call_cdp(ws, "Page.captureScreenshot", {"format": "png"})
        data_b64 = result.get("data", "")
        if data_b64:
            with open(output_path, "wb") as f:
                f.write(base64.b64decode(data_b64))
            print(f"Screenshot saved to {output_path}")
            return
    except CDPError as e:
        print(f"Page.captureScreenshot not supported by {browser}: {e}")
    except Exception as e:
        print(f"Page.captureScreenshot failed: {e}")

    # P2: Fallback — scrot (lightweight, Alpine-friendly)
    try:
        subprocess.run(["scrot", output_path], check=True, timeout=10)
        print(f"Screenshot saved to {output_path} [via scrot fallback]")
        return
    except (FileNotFoundError, subprocess.CalledProcessError):
        pass

    # P2: Fallback — ImageMagick import
    try:
        subprocess.run(
            ["import", "-window", "root", output_path],
            check=True, timeout=10,
            env={"DISPLAY": ":0"},
        )
        print(f"Screenshot saved to {output_path} [via ImageMagick fallback]")
        return
    except (FileNotFoundError, subprocess.CalledProcessError):
        pass

    print(
        f"Screenshot failed: Page.captureScreenshot unsupported by {browser} "
        "and neither scrot nor ImageMagick (import) are available."
    )
    sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="CDP Controller for AI Agent (Chromium + Lightpanda)"
    )
    parser.add_argument(
        "action",
        choices=["dump", "click", "type", "goto", "screenshot", "extract"],
    )
    parser.add_argument("--id", type=int, help="Element ID (backendNodeId for Chromium, data-elc-id for Lightpanda)")
    parser.add_argument("--text", type=str, help="Text to type")
    parser.add_argument("--url", type=str, help="URL to navigate to")
    parser.add_argument("--path", type=str, help="Output path for screenshot")
    parser.add_argument("--selector", type=str, default="body", help="CSS selector for extract action")
    parser.add_argument(
        "--browser",
        choices=["auto", "chromium", "lightpanda"],
        default="auto",
        help=(
            "Browser to target: auto (default, tries Lightpanda first), "
            "chromium, or lightpanda"
        ),
    )

    args = parser.parse_args()

    prefer_lightpanda = args.browser != "chromium"
    ws, page, browser = get_websocket(prefer_lightpanda=prefer_lightpanda)

    # If --browser=lightpanda was explicitly requested but we connected to chromium
    # (fallback), warn the user.
    if args.browser == "lightpanda" and browser == "chromium":
        print("Warning: Lightpanda unavailable, fell back to Chromium")
    elif args.browser == "chromium" and browser == "lightpanda":
        print("Warning: Chromium unavailable, fell back to Lightpanda")

    if args.action == "dump":
        cmd_dump(ws, page, browser)
    elif args.action == "extract":
        cmd_extract(ws, selector=args.selector or "body")
    elif args.action == "click":
        if args.id is None:
            print("--id is required for click")
            sys.exit(1)
        cmd_click(ws, args.id, browser=browser)
    elif args.action == "type":
        if args.id is None or args.text is None:
            print("--id and --text are required for type")
            sys.exit(1)
        cmd_type(ws, args.id, args.text, browser=browser)
    elif args.action == "goto":
        if args.url is None:
            print("--url is required for goto")
            sys.exit(1)
        cmd_goto(ws, args.url, browser=browser)
    elif args.action == "screenshot":
        if not args.path:
            print("--path is required for screenshot")
            sys.exit(1)
        cmd_screenshot(ws, args.path, browser=browser)

    ws.close()


if __name__ == "__main__":
    main()
