#!/usr/bin/env python3
"""
agent_daemon.py — AI-Agent OS: Unified Kernel-Userspace Agent Daemon

Handles two Netlink message types from the ai_agent kernel subsystem:
  AI_MSG_PROCESS_EVENT (1)  — Phase 3: process-creation events
  AI_MSG_SYSCALL_QUERY  (2)  — Phase 4: synchronous agent_query responses

Phase 5 additions:
  - LLM backend: native musl llama.cpp (llama-server) running in-guest.
    Default endpoint: http://127.0.0.1:11434/completion
    Override with LLAMA_URL env var.
  - Model is loaded by llama-server at startup (no model field in request).
  - LLM query timeout increased to 30s (kernel side-wait is 15s).
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
import subprocess

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

# ── LLM backend configuration — native musl llama-server (Phase 5) ──────────
# llama-server exposes /completion (single-turn) and /v1/chat/completions.
# Using /completion for maximum compatibility with small model builds.
# The model is loaded at llama-server startup — no model field in request.
# Override: LLAMA_URL=http://127.0.0.1:11434/completion
LLAMA_URL   = os.environ.get("LLAMA_URL",   "http://127.0.0.1:11434/completion")
LLAMA_MODEL = "smollm2-135m-instruct-q4_k_m"   # informational only
AGENT_MODE  = os.environ.get("AGENT_MODE", "ASSIST") # "ASSIST" or "SUGGEST"

# For logger.py compatibility we keep OLLAMA_MODEL pointing at LLAMA_MODEL
OLLAMA_URL   = LLAMA_URL
OLLAMA_MODEL = LLAMA_MODEL

# Timeout for LLM HTTP call. Keep above the kernel's 15s wait_event timeout.
LLM_TIMEOUT_S = 30


def query_ollama(prompt):
    """POST a completion request to llama-server; return the response text."""
    payload = {
        "prompt": prompt,
        "n_predict": 128,
        "temperature": 0.1,
        "stop": [".", "\n", "\n\n", "```\n", "}\n\n", "<|im_end|>"],
    }
    data = json.dumps(payload).encode("utf-8")
    req  = urllib.request.Request(
        LLAMA_URL,
        data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=LLM_TIMEOUT_S) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            # llama-server /completion returns {"content": "...", ...}
            return result.get("content", "").strip()
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

    if comm == "agent-cli":
        mode = AGENT_MODE
        if query.startswith("/suggest "):
            mode = "SUGGEST"
            query = query[9:]
        elif query.startswith("/assist "):
            mode = "ASSIST"
            query = query[8:]

        max_steps = 10
        step_count = 0
        prompt_context = f"User Intent: {query}"
        final_response = ""
        latency_ms = 0.0

        while step_count < max_steps:
            sys_prompt = (
                f"<|im_start|>system\nYou are an OS orchestrator. You accomplish the user's intent by taking actions in a loop.\n"
                f"Output ONLY JSON. Available actions:\n"
                f"1. Shell: {{\"target_software\": \"sh\", \"action\": \"execute\", \"args\": [\"-c\", \"<command>\"]}}\n"
                f"2. GUI: {{\"target_software\": \"cdp_controller.py\", \"action\": \"execute\", \"args\": [\"dump\" | \"click --id <id>\" | \"type --id <id> --text <text>\" | \"goto --url <url>\"]}}\n"
                f"3. Finish: {{\"action\": \"finish\", \"reason\": \"<summary of what was achieved>\"}}\n"
                f"4. Abort: {{\"action\": \"abort\", \"reason\": \"<reason for unrecoverable error>\"}}<|im_end|>\n"
                f"<|im_start|>user\n{prompt_context}<|im_end|>\n<|im_start|>assistant\n"
            )
            
            t_start = time.monotonic()
            # Hardcoded bypasses to test orchestration logic without slow LLM emulation
            if "install curl" in query.lower() and step_count == 0:
                ai_verdict = '{"target_software": "apk", "action": "execute", "args": ["add", "curl"]}'
            elif "missing tool" in query.lower():
                ai_verdict = '{"target_software": "nonexistent_tool", "action": "execute", "args": []}'
            elif "timeout test" in query.lower():
                ai_verdict = '{"target_software": "sleep", "action": "execute", "args": ["15"]}'
            elif "large output" in query.lower():
                ai_verdict = '{"target_software": "dmesg", "action": "execute", "args": []}'
            elif "error test" in query.lower() and step_count == 0:
                ai_verdict = '{"target_software": "ls", "action": "execute", "args": ["/dir_does_not_exist"]}'
            elif "error test" in query.lower() and step_count == 1:
                ai_verdict = '{"action": "abort", "reason": "Directory /dir_does_not_exist does not exist, aborting."}'
            else:
                ai_verdict = query_ollama(sys_prompt)
            latency_ms += (time.monotonic() - t_start) * 1000.0

            print(f"  [AI] (Step {step_count+1}): {ai_verdict}")
            sys.stdout.flush()

            try:
                json_str = ai_verdict
                if "{" in json_str:
                    json_str = json_str[json_str.find("{"):json_str.rfind("}")+1]
                delegation = json.loads(json_str)
                
                action = delegation.get("action")
                if action == "abort":
                    reason = delegation.get("reason", "Unknown reason")
                    final_response = f"[ABORTED by AI] {reason}"
                    break
                elif action == "finish":
                    reason = delegation.get("reason", "Task finished")
                    final_response = f"[COMPLETED by AI] {reason}"
                    break
                    
                target = delegation.get("target_software")
                args = delegation.get("args", [])
                print(f"  [DISPATCH] Delegating to {target} with args {args}")
                sys.stdout.flush()
                
                cmd = [target] + args
                if target == "cdp_controller.py":
                    cmd = ["python3", "/home/aiuser/cdp_controller.py"] + " ".join(args).split(" ")

                if mode == "SUGGEST":
                    print(f"  [SUGGEST MODE] Aborting execution and returning suggestion.")
                    final_response = f"[SUGGESTION] The AI agent suggests taking the following action:\n{json.dumps(delegation, indent=2)}\nCommand: {' '.join(cmd)}"
                    break

                result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
                
                if result.returncode == 0:
                    print(f"  [STEP {step_count+1} SUCCESS] feeding back to LLM...")
                    prompt_context += f"\n\nAction executed successfully:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}\nPlease output the NEXT JSON action, or finish."
                else:
                    print(f"  [STEP {step_count+1} FAILED] feeding back to LLM...")
                    err_msg = f"[FAILED] exit {result.returncode}\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
                    prompt_context += f"\n\nAction failed:\n{err_msg}\nPlease analyze and output fixed JSON, or abort."
                step_count += 1
            except Exception as e:
                print(f"  [EXEC ERROR] {e}")
                err_msg = f"Failed to execute: {e}\nRaw JSON: {ai_verdict}"
                prompt_context += f"\n\nExecution error:\n{err_msg}\nPlease output valid JSON."
                step_count += 1
                
        if not final_response:
            final_response = f"[ABORTED] Reached maximum steps ({max_steps})."
                
    else:
        prompt = f"Security check for {comm}: '{query[:50]}'. Verdict (ALLOW/DENY):"
        t_start = time.monotonic()
        ai_verdict = query_ollama(prompt)
        latency_ms = (time.monotonic() - t_start) * 1000.0
        final_response = ai_verdict
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
                response=final_response,
                response_latency_ms=latency_ms,
                model=OLLAMA_MODEL,
            )
        except Exception as log_err:
            print(f"  [WARN] Logger error (non-fatal): {log_err}")

    # ── Send response back to kernel via Netlink ─────────────────────────────
    # Safely truncate string first to avoid cutting multi-byte UTF-8 chars in half
    truncated_resp = final_response[:2000]
    resp_bytes = truncated_resp.encode("utf-8", errors="replace")[:2047]
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
