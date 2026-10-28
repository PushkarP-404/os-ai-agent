#!/usr/bin/env python3
"""agent_intent.py — CLI interface to the AI-Agent OS Intent Bus.

Allows command-line users, shell scripts, and external applications to:
1. Dispatch standard OS intents to apps or services:
     agent-intent send --from code-editor --to git-service --type git.status
2. Send multi-app requests through the automation engine:
     agent-intent route --from code-editor --file workflow.json
3. Check bus health and registered apps:
     agent-intent ping
4. Resolve pending confirmations:
     agent-intent confirm <task_id> --approve / --deny
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any

from automation_engine import (
    IntentBusClient,
    IntentMessage,
    IntentResponse,
    get_automation_engine,
)


def main():
    parser = argparse.ArgumentParser(description="AI-Agent OS Intent Bus CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # 1. Send intent
    send_parser = subparsers.add_parser("send", help="Send an intent through the Intent Bus")
    send_parser.add_argument("--from", dest="source_app", default="agent-cli", help="Source application name")
    send_parser.add_argument("--to", dest="target_app", default="agent", help="Target application or service")
    send_parser.add_argument("--type", dest="intent_type", required=True, help="Intent type (e.g. git.status, file.read)")
    send_parser.add_argument("--payload", dest="payload", default="{}", help="JSON payload dictionary")
    send_parser.add_argument("--mode", dest="mode", default="ASSIST", choices=["SUGGEST", "ASSIST", "APPLY", "READ_ONLY"])
    send_parser.add_argument("--require-confirm", action="store_true", help="Require explicit confirmation")
    send_parser.add_argument("--tcp-port", type=int, default=None, help="TCP port if using TCP broker")

    # 2. Ping
    ping_parser = subparsers.add_parser("ping", help="Ping the Intent Bus server")
    ping_parser.add_argument("--tcp-port", type=int, default=None, help="TCP port if using TCP broker")

    # 3. Confirm
    confirm_parser = subparsers.add_parser("confirm", help="Approve or deny a pending confirmation")
    confirm_parser.add_argument("task_id", help="Task ID to resolve")
    confirm_parser.add_argument("--approve", action="store_true", default=True, help="Approve the action")
    confirm_parser.add_argument("--deny", action="store_false", dest="approve", help="Deny the action")
    confirm_parser.add_argument("--tcp-port", type=int, default=None, help="TCP port if using TCP broker")

    # 4. Route workflow
    route_parser = subparsers.add_parser("route", help="Execute an autonomous multi-app workflow")
    route_parser.add_argument("--from", dest="source_app", default="agent-cli", help="Source application name")
    route_parser.add_argument("--file", dest="workflow_file", default=None, help="JSON file defining workflow steps")
    route_parser.add_argument("--workflow", dest="workflow_json", default=None, help="JSON string defining workflow steps")
    route_parser.add_argument("--mode", dest="mode", default="APPLY", choices=["SUGGEST", "ASSIST", "APPLY"])

    args = parser.parse_args()

    if args.command == "ping":
        try:
            client = IntentBusClient("ping-tool", tcp_port=args.tcp_port)
            client.connect(timeout=2.0)
            ok = client.ping()
            client.disconnect()
            print(f"[IntentBus] Status: {'PONG (Operational)' if ok else 'FAIL'}")
            sys.exit(0 if ok else 1)
        except Exception as e:
            print(f"[IntentBus] Ping failed: {e}", file=sys.stderr)
            sys.exit(1)

    elif args.command == "send":
        try:
            payload = json.loads(args.payload)
        except json.JSONDecodeError as err:
            print(f"Error parsing --payload JSON: {err}", file=sys.stderr)
            sys.exit(1)

        intent = IntentMessage(
            task_id=f"intent-{int(time.time())}",
            source_app=args.source_app,
            target_app=args.target_app,
            intent_type=args.intent_type,
            payload=payload,
            required_confirmation=args.require_confirm,
            user=os.environ.get("USER", "root"),
            uid=os.getuid() if hasattr(os, "getuid") else 0,
            gid=os.getgid() if hasattr(os, "getgid") else 0,
        )

        try:
            client = IntentBusClient(args.source_app, tcp_port=args.tcp_port)
            client.connect(timeout=3.0)
            resp = client.send_intent(intent, mode=args.mode)
            client.disconnect()
            print(json.dumps(resp.to_dict(), indent=2))
            sys.exit(0 if resp.status in ("SUCCESS", "ROUTED", "SUGGEST") else 1)
        except Exception:
            # Fall back to direct in-process engine if bus socket is not running standalone
            engine = get_automation_engine()
            resp = engine.execute_intent(intent, mode=args.mode)
            print(json.dumps(resp.to_dict(), indent=2))
            sys.exit(0 if resp.status in ("SUCCESS", "ROUTED", "SUGGEST") else 1)

    elif args.command == "confirm":
        try:
            client = IntentBusClient("confirm-tool", tcp_port=args.tcp_port)
            client.connect(timeout=3.0)
            res = client.confirm(args.task_id, args.approve)
            client.disconnect()
            print(json.dumps(res, indent=2))
            sys.exit(0)
        except Exception as e:
            print(f"Error sending confirmation: {e}", file=sys.stderr)
            sys.exit(1)

    elif args.command == "route":
        if args.workflow_file:
            with open(args.workflow_file, "r", encoding="utf-8") as f:
                workflow_steps = json.load(f)
        elif args.workflow_json:
            workflow_steps = json.loads(args.workflow_json)
        else:
            print("Error: Either --file or --workflow must be specified.", file=sys.stderr)
            sys.exit(2)
        engine = get_automation_engine()
        results = engine.route_multi_app_workflow(
            source_app=args.source_app,
            steps=workflow_steps,
            mode=args.mode,
        )
        print(json.dumps([r.to_dict() for r in results], indent=2))
        all_ok = all(r.status == "SUCCESS" for r in results)
        sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()

