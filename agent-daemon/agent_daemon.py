#!/usr/bin/env python3
import socket
import struct
import os
import sys
import time
import json
import urllib.request
import urllib.error

NETLINK_AI_AGENT = 31

# Netlink message types
AI_MSG_REGISTER = 0
AI_MSG_PROCESS_EVENT = 1
AI_MSG_SYSCALL_QUERY = 2
AI_MSG_SYSCALL_RESP = 3

# struct nlmsghdr {
#   __u32 nlmsg_len;
#   __u16 nlmsg_type;
#   __u16 nlmsg_flags;
#   __u32 nlmsg_seq;
#   __u32 nlmsg_pid;
# };
NLMSG_HDR_FORMAT = "=IHHII"
NLMSG_HDR_SIZE = struct.calcsize(NLMSG_HDR_FORMAT)

# struct process_event (Phase 3)
EVENT_FORMAT = "=ii16s"
EVENT_SIZE = struct.calcsize(EVENT_FORMAT)

# struct ai_agent_query_msg (Phase 4)
QUERY_FORMAT = "=iii16s1024s"
QUERY_SIZE = struct.calcsize(QUERY_FORMAT)

# struct ai_agent_resp_msg (Phase 4)
RESP_FORMAT = "=ii2048s"
RESP_SIZE = struct.calcsize(RESP_FORMAT)

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://10.0.2.2:11434/api/generate")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "mistral:latest")

def query_ollama(prompt, timeout=10):
    """Query the local Ollama LLM backend."""
    payload = {
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": 0.2,
            "num_predict": 128
        }
    }
    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(
        OLLAMA_URL,
        data=data,
        headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            res_json = json.loads(response.read().decode('utf-8'))
            return res_json.get("response", "").strip()
    except Exception as e:
        return f"[AI AGENT FALLBACK] Local LLM unreachable ({e}). Policy verdict: ALLOW with monitoring."

def handle_syscall_query(sock, query_payload):
    query_id, caller_pid, target_pid, comm_raw, query_raw = struct.unpack(QUERY_FORMAT, query_payload)
    comm = comm_raw.split(b'\x00')[0].decode('utf-8', errors='replace')
    query = query_raw.split(b'\x00')[0].decode('utf-8', errors='replace')

    print(f"\n[QUERY RECEIVED] ID: {query_id} | Caller: {caller_pid} ({comm}) | Target PID: {target_pid}")
    print(f"Query text: \"{query}\"")
    sys.stdout.flush()

    prompt = (
        f"You are the operating system's kernel AI agent. A process is querying you via sys_agent_query:\n"
        f"- Process Comm: {comm}\n"
        f"- Caller PID: {caller_pid}\n"
        f"- Target PID: {target_pid}\n"
        f"- Query: {query}\n\n"
        f"Provide a concise, direct, 2-3 sentence system safety analysis and actionable recommendation."
    )

    print("[QUERY PROCESSING] Consulting AI agent backend...")
    sys.stdout.flush()
    start_time = time.time()
    ai_verdict = query_ollama(prompt)
    elapsed = time.time() - start_time
    print(f"[AI RESPONSE] ({elapsed:.2f}s): {ai_verdict}")
    sys.stdout.flush()

    # Construct Netlink response packet to kernel
    resp_bytes = ai_verdict.encode('utf-8', errors='replace')[:2047]
    payload = struct.pack(RESP_FORMAT, query_id, 0, resp_bytes)
    total_len = NLMSG_HDR_SIZE + len(payload)
    hdr = struct.pack(NLMSG_HDR_FORMAT, total_len, AI_MSG_SYSCALL_RESP, 0, 0, os.getpid())

    try:
        sock.sendto(hdr + payload, (0, 0))
        print(f"[REPLY SENT] Dispatched response for query ID {query_id} back to kernel.")
        sys.stdout.flush()
    except OSError as e:
        print(f"[ERROR] Failed to send Netlink response to kernel: {e}")

def main():
    print(f"==================================================")
    print(f"       AI-Agent OS: Unified Agent Daemon          ")
    print(f"==================================================")
    print(f"[AGENT DAEMON] Starting userspace listener (PID: {os.getpid()})...")
    print(f"[AGENT DAEMON] LLM Backend: {OLLAMA_URL} ({OLLAMA_MODEL})")
    
    try:
        sock = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, NETLINK_AI_AGENT)
    except OSError as e:
        print(f"[ERROR] Failed to open Netlink socket (protocol {NETLINK_AI_AGENT}): {e}")
        print("Ensure custom kernel with ai_agent subsystem is booted.")
        sys.exit(1)

    try:
        sock.bind((os.getpid(), 0))
    except OSError as e:
        print(f"[ERROR] Failed to bind Netlink socket: {e}")
        sock.close()
        sys.exit(1)

    # Send registration packet (type AI_MSG_REGISTER = 0)
    reg_hdr = struct.pack(NLMSG_HDR_FORMAT, NLMSG_HDR_SIZE, AI_MSG_REGISTER, 0, 1, os.getpid())
    try:
        sock.sendto(reg_hdr, (0, 0))
        print("[AGENT DAEMON] Registered with kernel AI subsystem.")
    except OSError as e:
        print(f"[ERROR] Failed to send registration message to kernel: {e}")
        sock.close()
        sys.exit(1)

    print("[AGENT DAEMON] Actively monitoring process events and interactive syscall queries...")
    sys.stdout.flush()

    # Optional max_events parameter
    max_events = int(sys.argv[1]) if len(sys.argv) > 1 else None
    events_count = 0

    try:
        while True:
            data, addr = sock.recvfrom(4096)
            if len(data) < NLMSG_HDR_SIZE:
                continue

            nlmsg_len, nlmsg_type, nlmsg_flags, nlmsg_seq, nlmsg_pid = struct.unpack_from(
                NLMSG_HDR_FORMAT, data, 0
            )

            payload = data[NLMSG_HDR_SIZE:]

            if nlmsg_type == AI_MSG_SYSCALL_QUERY and len(payload) >= QUERY_SIZE:
                handle_syscall_query(sock, payload[:QUERY_SIZE])
                events_count += 1
            elif nlmsg_type == AI_MSG_PROCESS_EVENT and len(payload) >= EVENT_SIZE:
                parent_pid, child_pid, comm_bytes = struct.unpack(EVENT_FORMAT, payload[:EVENT_SIZE])
                comm = comm_bytes.split(b'\x00')[0].decode('utf-8', errors='replace')
                events_count += 1
                print(f"[EVENT #{events_count}] Process Created: Parent {parent_pid} ({comm}) ---> Child {child_pid}")
                sys.stdout.flush()
            elif len(payload) >= EVENT_SIZE:
                # Compatibility fallback for Phase 3 LKM
                parent_pid, child_pid, comm_bytes = struct.unpack(EVENT_FORMAT, payload[:EVENT_SIZE])
                comm = comm_bytes.split(b'\x00')[0].decode('utf-8', errors='replace')
                events_count += 1
                print(f"[EVENT #{events_count}] Process Created: Parent {parent_pid} ({comm}) ---> Child {child_pid}")
                sys.stdout.flush()

            if max_events and events_count >= max_events:
                print(f"[AGENT DAEMON] Reached target event count of {max_events}. Exiting.")
                break

    except KeyboardInterrupt:
        print("\n[AGENT DAEMON] Stopped by user.")
    finally:
        sock.close()

if __name__ == "__main__":
    main()
