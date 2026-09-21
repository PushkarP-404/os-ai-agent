#!/usr/bin/env python3
"""
agent_daemon.py — AI-Agent OS: Unified Kernel-Userspace Agent Daemon

Handles two Netlink message types from the ai_agent kernel subsystem:
  AI_MSG_PROCESS_EVENT (1)  — Phase 3: process-creation events
  AI_MSG_SYSCALL_QUERY  (2)  — Phase 4: synchronous agent_query responses

Phase 5 additions:
  - Default Ollama target is now http://127.0.0.1:11434 (in-guest inference).
    Override with OLLAMA_URL env var to target the host (http://10.0.2.2:11434)
    during development.
  - LLM query timeout increased to 30s (kernel side-wait is 15s; daemon should
    not cut off mid-inference).
  - All interactions are logged via logger.py to /var/ai-agent/.
"""

import socket
import struct
import os
import sys
import time
import json
import urllib.request
import urllib.error
import signal

# ── Conditionally import Phase 5 logger (graceful fallback if not present) ──
try:
    import logger as agent_logger
    LOGGING_ENABLED = True
except ImportError:
    LOGGING_ENABLED = False

# ── Netlink constants ────────────────────────────────────────────────────────
NETLINK_AI_AGENT = 31

AI_MSG_REGISTER      = 0
AI_MSG_PROCESS_EVENT = 1
AI_MSG_SYSCALL_QUERY = 2
AI_MSG_SYSCALL_RESP  = 3

# struct nlmsghdr
NLMSG_HDR_FORMAT = "=IHHII"
NLMSG_HDR_SIZE   = struct.calcsize(NLMSG_HDR_FORMAT)

# struct process_event (Phase 3) — {parent_pid, child_pid, comm[16]}
EVENT_FORMAT = "=ii16s"
EVENT_SIZE   = struct.calcsize(EVENT_FORMAT)

# struct ai_agent_query_msg (Phase 4) — {query_id, caller_pid, target_pid, comm[16], query[1024]}
QUERY_FORMAT = "=iii16s1024s"
QUERY_SIZE   = struct.calcsize(QUERY_FORMAT)

# struct ai_agent_resp_msg (Phase 4) — {query_id, status, response[2048]}
RESP_FORMAT = "=ii2048s"
RESP_SIZE   = struct.calcsize(RESP_FORMAT)

# ── LLM backend configuration ────────────────────────────────────────────────
# Phase 5: default is in-guest local Ollama.
# For development against host Ollama: OLLAMA_URL=http://10.0.2.2:11434/api/generate
OLLAMA_URL   = os.environ.get("OLLAMA_URL",   "http://127.0.0.1:11434/api/generate")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.2:3b-instruct-q4_K_M")

# Timeout for LLM HTTP call. Keep above the kernel's 15s wait_event timeout.
LLM_TIMEOUT_S = 30


def query_ollama(prompt):
    """POST a generation request to Ollama; return the response text."""
    payload = {
        "model":  OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": 0.2,
            "num_predict": 200,
        },
    }
    data = json.dumps(payload).encode("utf-8")
    req  = urllib.request.Request(
        OLLAMA_URL,
        data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=LLM_TIMEOUT_S) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            return result.get("response", "").strip()
    except Exception as e:
        return (
            f"[AI AGENT FALLBACK] Local LLM unreachable ({e}). "
            f"Policy verdict: ALLOW with monitoring."
        )


def handle_syscall_query(sock, query_payload):
    """Process a Phase 4 AI_MSG_SYSCALL_QUERY message and reply to kernel."""
    query_id, caller_pid, target_pid, comm_raw, query_raw = struct.unpack(
        QUERY_FORMAT, query_payload
    )
    comm  = comm_raw.split(b"\x00")[0].decode("utf-8", errors="replace")
    query = query_raw.split(b"\x00")[0].decode("utf-8", errors="replace")

    print(f"\n[QUERY #{query_id}] PID {caller_pid} ({comm}) -> target {target_pid}")
    print(f"  Query: \"{query}\"")
    sys.stdout.flush()

    prompt = (
        f"You are the operating system's kernel AI agent. "
        f"A process is querying you via sys_agent_query:\n"
        f"- Process Comm: {comm}\n"
        f"- Caller PID: {caller_pid}\n"
        f"- Target PID: {target_pid}\n"
        f"- Query: {query}\n\n"
        f"Provide a concise, direct, 2-3 sentence system safety analysis "
        f"and actionable recommendation."
    )

    t_start = time.monotonic()
    ai_verdict = query_ollama(prompt)
    latency_ms = (time.monotonic() - t_start) * 1000.0

    print(f"  [AI] ({latency_ms:.0f}ms): {ai_verdict}")
    sys.stdout.flush()

    # ── Log to /var/ai-agent/ (Phase 5) ─────────────────────────────────────
    if LOGGING_ENABLED:
        try:
            agent_logger.log_interaction(
                query_id=query_id,
                caller_pid=caller_pid,
                comm=comm,
                target_pid=target_pid,
                query=query,
                response=ai_verdict,
                response_latency_ms=latency_ms,
                model=OLLAMA_MODEL,
            )
        except Exception as log_err:
            print(f"  [WARN] Logger error (non-fatal): {log_err}")

    # ── Send response back to kernel via Netlink ─────────────────────────────
    resp_bytes = ai_verdict.encode("utf-8", errors="replace")[:2047]
    payload    = struct.pack(RESP_FORMAT, query_id, 0, resp_bytes)
    total_len  = NLMSG_HDR_SIZE + len(payload)
    hdr        = struct.pack(NLMSG_HDR_FORMAT, total_len, AI_MSG_SYSCALL_RESP,
                             0, 0, os.getpid())
    try:
        sock.sendto(hdr + payload, (0, 0))
        print(f"  [REPLY] Sent response for query #{query_id}")
        sys.stdout.flush()
    except OSError as e:
        print(f"  [ERROR] Netlink send failed: {e}")


def main():
    print("==================================================")
    print("       AI-Agent OS: Unified Agent Daemon          ")
    print("==================================================")
    print(f"[AGENT DAEMON] PID: {os.getpid()}")
    print(f"[AGENT DAEMON] LLM: {OLLAMA_URL} (model: {OLLAMA_MODEL})")
    print(f"[AGENT DAEMON] Logging: {'ENABLED -> /var/ai-agent/' if LOGGING_ENABLED else 'DISABLED (logger.py not found)'}")

    # ── Open Netlink socket ──────────────────────────────────────────────────
    try:
        sock = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, NETLINK_AI_AGENT)
    except OSError as e:
        print(f"[ERROR] Cannot open Netlink socket proto {NETLINK_AI_AGENT}: {e}")
        print("Ensure the custom ai-agent kernel is booted.")
        sys.exit(1)

    try:
        sock.bind((os.getpid(), 0))
    except OSError as e:
        print(f"[ERROR] Netlink bind failed: {e}")
        sock.close()
        sys.exit(1)

    # ── Register with kernel ─────────────────────────────────────────────────
    reg_hdr = struct.pack(NLMSG_HDR_FORMAT, NLMSG_HDR_SIZE,
                          AI_MSG_REGISTER, 0, 1, os.getpid())
    try:
        sock.sendto(reg_hdr, (0, 0))
        print("[AGENT DAEMON] Registered with kernel ai_agent subsystem.")
    except OSError as e:
        print(f"[ERROR] Registration failed: {e}")
        sock.close()
        sys.exit(1)

    print("[AGENT DAEMON] Listening for process events and syscall queries...")
    sys.stdout.flush()

    max_events  = int(sys.argv[1]) if len(sys.argv) > 1 else None
    events_seen = 0

    def sig_handler(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, sig_handler)

    try:
        while True:
            data, _ = sock.recvfrom(4096)
            if len(data) < NLMSG_HDR_SIZE:
                continue

            nlmsg_len, nlmsg_type, nlmsg_flags, nlmsg_seq, nlmsg_pid = (
                struct.unpack_from(NLMSG_HDR_FORMAT, data, 0)
            )
            payload = data[NLMSG_HDR_SIZE:]

            if nlmsg_type == AI_MSG_SYSCALL_QUERY and len(payload) >= QUERY_SIZE:
                handle_syscall_query(sock, payload[:QUERY_SIZE])
                events_seen += 1

            elif nlmsg_type == AI_MSG_PROCESS_EVENT and len(payload) >= EVENT_SIZE:
                parent_pid, child_pid, comm_bytes = struct.unpack(
                    EVENT_FORMAT, payload[:EVENT_SIZE]
                )
                comm = comm_bytes.split(b"\x00")[0].decode("utf-8", errors="replace")
                events_seen += 1
                print(f"[PROC #{events_seen}] {parent_pid} ({comm}) -> child {child_pid}")
                sys.stdout.flush()
                if LOGGING_ENABLED:
                    try:
                        agent_logger.log_process_event(parent_pid, child_pid, comm)
                    except Exception:
                        pass

            elif len(payload) >= EVENT_SIZE:
                # Compatibility fallback for Phase 3 LKM on stock kernel
                parent_pid, child_pid, comm_bytes = struct.unpack(
                    EVENT_FORMAT, payload[:EVENT_SIZE]
                )
                comm = comm_bytes.split(b"\x00")[0].decode("utf-8", errors="replace")
                events_seen += 1
                print(f"[PROC #{events_seen}] {parent_pid} ({comm}) -> child {child_pid}")
                sys.stdout.flush()

            if max_events and events_seen >= max_events:
                print(f"[AGENT DAEMON] Reached target event count {max_events}. Exiting.")
                break

    except KeyboardInterrupt:
        print("\n[AGENT DAEMON] Stopped by signal.")
    finally:
        try:
            unreg_hdr = struct.pack(NLMSG_HDR_FORMAT, NLMSG_HDR_SIZE,
                                    AI_MSG_REGISTER, 0, 1, 0)
            sock.sendto(unreg_hdr, (0, 0))
            print("[AGENT DAEMON] Unregistered from kernel ai_agent subsystem.")
        except Exception:
            pass
        sock.close()

    if LOGGING_ENABLED:
        try:
            stats = agent_logger.get_stats()
            print(f"[AGENT DAEMON] Session summary: {stats}")
        except Exception:
            pass


if __name__ == "__main__":
    main()
