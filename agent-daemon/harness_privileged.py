#!/usr/bin/env python3
"""Root-owned, local-only broker for the small V2 harness tool set.

The normal agent daemon stays unprivileged. This process accepts only
registered system-changing tools from the local ai-agent service user over a
Unix-domain socket and returns a bounded structured result.
"""

from __future__ import annotations

import argparse
import grp
import json
import os
import pwd
import socket
import struct
from dataclasses import asdict

from harness_v2 import PolicyEngine, ToolExecutor, ToolResult


SOCKET_PATH = "/run/ai-agent/privileged.sock"
RUNTIME_DIR = "/run/ai-agent"
MAX_REQUEST_BYTES = 16384


def _error(summary: str, detail: str = "", exit_code: int = 1) -> bytes:
    return (json.dumps(asdict(ToolResult(False, summary, stderr=detail, exit_code=exit_code))) + "\n").encode("utf-8")


def _peer_uid(conn: socket.socket) -> int:
    raw = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    _pid, uid, _gid = struct.unpack("3i", raw)
    return uid


def _prepare_socket(path: str) -> socket.socket:
    os.makedirs(RUNTIME_DIR, mode=0o750, exist_ok=True)
    try:
        group_id = grp.getgrnam("ai-agent").gr_gid
    except KeyError as exc:
        raise RuntimeError("ai-agent group does not exist") from exc
    os.chown(RUNTIME_DIR, 0, group_id)
    os.chmod(RUNTIME_DIR, 0o750)
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(path)
    os.chown(path, 0, group_id)
    os.chmod(path, 0o660)
    listener.listen(8)
    return listener


def serve(path: str = SOCKET_PATH) -> None:
    if os.geteuid() != 0:
        raise RuntimeError("harness_privileged.py must run as root")
    agent_uid = pwd.getpwnam("ai-agent").pw_uid
    executor = ToolExecutor({"uid": 0, "gid": 0, "user": "root", "home": "/root", "cwd": "/"},
                            allow_privilege_proxy=False)
    listener = _prepare_socket(path)
    try:
        while True:
            conn, _addr = listener.accept()
            with conn:
                try:
                    if _peer_uid(conn) != agent_uid:
                        conn.sendall(_error("Privileged request denied", "socket peer is not ai-agent", 126))
                        continue
                    data = bytearray()
                    while len(data) <= MAX_REQUEST_BYTES:
                        chunk = conn.recv(4096)
                        if not chunk:
                            break
                        data.extend(chunk)
                        if b"\n" in chunk:
                            break
                    if len(data) > MAX_REQUEST_BYTES:
                        conn.sendall(_error("Privileged request denied", "request is too large", 2))
                        continue
                    request = json.loads(bytes(data).split(b"\n", 1)[0].decode("utf-8"))
                    tool, args = request.get("tool"), request.get("args")
                    if tool not in PolicyEngine.CHANGE or not isinstance(args, dict):
                        conn.sendall(_error("Privileged request denied", "tool is not permitted", 126))
                        continue
                    caller = request.get("caller")
                    if isinstance(caller, dict):
                        exec_inst = ToolExecutor(caller, allow_privilege_proxy=False)
                    else:
                        exec_inst = executor
                    result = exec_inst.run(tool, args)
                    conn.sendall((json.dumps(asdict(result), ensure_ascii=False) + "\n").encode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError, OSError, ValueError) as exc:
                    conn.sendall(_error("Privileged request failed", str(exc), 2))
    finally:
        listener.close()
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


def main() -> None:
    parser = argparse.ArgumentParser(description="AI-Agent OS privileged tool broker")
    parser.add_argument("--socket", default=SOCKET_PATH)
    args = parser.parse_args()
    serve(args.socket)


if __name__ == "__main__":
    main()
