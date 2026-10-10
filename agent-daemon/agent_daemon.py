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
import uuid
import shutil
import urllib.request
import urllib.error
import signal
import subprocess
import concurrent.futures

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

CAPABILITIES_FILE = "/var/ai-agent/capabilities.json"
SYSTEM_CAPABILITIES = {}
EBPF_EVENTS = []

# ── Phase 11: task runner path ───────────────────────────────────────────────
TASK_RUNNER_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "task_runner.py",
)
# Fall back to system install path
if not os.path.exists(TASK_RUNNER_PATH):
    TASK_RUNNER_PATH = "/usr/local/lib/ai-agent/task_runner.py"

TASKS_DIR = os.path.join(os.environ.get("AI_AGENT_LOG_DIR", "/var/ai-agent"), "tasks")

def load_capabilities():
    global SYSTEM_CAPABILITIES
    if os.path.exists(CAPABILITIES_FILE):
        try:
            with open(CAPABILITIES_FILE, "r") as f:
                SYSTEM_CAPABILITIES = json.load(f)
        except Exception:
            pass

def build_live_capabilities():
    """Sample real OS state and merge into SYSTEM_CAPABILITIES at daemon startup.
    
    This gives the Planner accurate context about what's actually installed,
    avoiding hallucinated package manager names or missing tool assumptions.
    """
    global SYSTEM_CAPABILITIES
    live = {}
    # Package manager
    if shutil.which("apk"):   live["pkg_manager"] = "apk"
    elif shutil.which("apt"): live["pkg_manager"] = "apt"
    elif shutil.which("dnf"): live["pkg_manager"] = "dnf"
    elif shutil.which("yum"): live["pkg_manager"] = "yum"
    # Init system
    if os.path.exists("/sbin/openrc-run") or os.path.exists("/etc/init.d"):
        live["init_system"] = "openrc"
    elif shutil.which("systemctl"):
        live["init_system"] = "systemd"
    # Shell tools
    for tool in ["python3", "curl", "wget", "gcc", "git", "nginx", "ssh", "tar",
                 "grep", "awk", "sed", "find", "nc", "socat", "strace"]:
        if shutil.which(tool):
            live[f"has_{tool}"] = True
    # Basic system info
    live["user"]     = os.environ.get("USER", "root")
    try:
        live["hostname"] = socket.gethostname()
    except Exception:
        pass
    try:
        with open("/etc/os-release") as f:
            for line in f:
                if line.startswith("PRETTY_NAME="):
                    live["os"] = line.split("=", 1)[1].strip().strip('"')
                    break
    except Exception:
        pass
    # Merge live into persisted caps (live takes precedence for dynamic fields)
    SYSTEM_CAPABILITIES.update(live)
    save_capability("_live_sampled", True)
    print(f"[CAPS] OS capabilities sampled: {json.dumps(live)}")

def save_capability(key, value):
    SYSTEM_CAPABILITIES[key] = value
    try:
        os.makedirs(os.path.dirname(CAPABILITIES_FILE), exist_ok=True)
        with open(CAPABILITIES_FILE, "w") as f:
            json.dump(SYSTEM_CAPABILITIES, f)
        print(f"  [CACHE] Capability saved: {key}={value}")
    except Exception as e:
        print(f"Failed to save capability: {e}")

load_capabilities()


def query_ollama(prompt, min_tokens=8, n_predict=200):
    """POST a completion request to llama-server; return the response text.
    
    Bug fix (2026-09-27): Removed '.' from stop tokens — it caused the 135M model
    to terminate after a single token (e.g. the model outputting '1.' immediately
    stops). Also increased n_predict from 128 to 200 to allow full JSON responses.
    Added retry if response is too short (garbage detection).
    """
    # NOTE: Do NOT include '.' or single '\n' as stop tokens — the tiny SmolLM2
    # model outputs these almost immediately, resulting in single-character responses.
    payload = {
        "prompt": prompt,
        "n_predict": n_predict,
        "temperature": 0.1,
        "stop": ["\n\n", "<|im_end|>", "<|im_start|>"],
    }
    data = json.dumps(payload).encode("utf-8")
    req  = urllib.request.Request(
        LLAMA_URL,
        data=data,
        headers={"Content-Type": "application/json"},
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=LLM_TIMEOUT_S) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                # llama-server /completion returns {"content": "...", ...}
                content = result.get("content", "").strip()
                # Garbage detection: if response is shorter than min_tokens chars,
                # retry before falling back
                if len(content) < min_tokens:
                    print(f"  [WARN] LLM returned suspiciously short response ({len(content)} chars): {repr(content)}. Attempt {attempt+1}/3")
                    if attempt < 2:
                        continue
                    return "[AI AGENT FALLBACK] LLM response too short to be useful. Policy verdict: ALLOW with monitoring."
                return content
        except Exception as e:
            if attempt < 2:
                print(f"  [WARN] LLM request failed: {e}. Retrying... Attempt {attempt+1}/3")
                continue
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
    
    # Bug fix: Prevent prompt injection by removing special tokens from query
    query = query.replace("<|im_start|>", "").replace("<|im_end|>", "")

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

        # ── Phase 11: dispatch to task_runner.py subprocess ──────────────────
        # The kernel buffer (2048 bytes) is only used to return the task ID and
        # task log path. The actual results are written to the JSONL task file
        # and streamed by the updated agent-cli polling loop.
        task_id = str(uuid.uuid4())[:8]
        task_file = os.path.join(TASKS_DIR, f"{task_id}.jsonl")
        os.makedirs(TASKS_DIR, exist_ok=True)

        latency_ms = 0.0
        t_start = time.monotonic()

        if not os.path.exists(TASK_RUNNER_PATH):
            final_response = (
                f"[ERROR] task_runner.py not found at {TASK_RUNNER_PATH}. "
                f"Deploy Phase 11 files and retry."
            )
        else:
            try:
                env = os.environ.copy()
                env["LLAMA_URL"]        = LLAMA_URL
                env["AI_AGENT_LOG_DIR"] = os.environ.get("AI_AGENT_LOG_DIR", "/var/ai-agent")

                subprocess.Popen(
                    [sys.executable, TASK_RUNNER_PATH, task_id, query, mode],
                    stdout=open("/var/log/task_runner.log", "a"),
                    stderr=subprocess.STDOUT,
                    env=env,
                    close_fds=True,
                )
                latency_ms = (time.monotonic() - t_start) * 1000.0
                final_response = (
                    f"[TASK:{task_id}] Running in background.\n"
                    f"Log: {task_file}\n"
                    f"Mode: {mode}"
                )
                print(f"  [DISPATCH] Spawned task_runner for task {task_id}, mode={mode}")
                sys.stdout.flush()
            except Exception as exc:
                final_response = f"[ERROR] Failed to spawn task_runner: {exc}"
                latency_ms = (time.monotonic() - t_start) * 1000.0
                
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
    print(f"[AGENT DAEMON] Task runner: {TASK_RUNNER_PATH}")
    print(f"[AGENT DAEMON] Tasks dir: {TASKS_DIR}")

    # ── Phase 11: build live OS capability context ───────────────────────────
    build_live_capabilities()
    os.makedirs(TASKS_DIR, exist_ok=True)

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

    def ebpf_listener():
        sock_path = "/var/ai-agent/ebpf.sock"
        if os.path.exists(sock_path):
            os.remove(sock_path)
        usock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        usock.bind(sock_path)
        os.chmod(sock_path, 0o666)
        while True:
            try:
                data, _ = usock.recvfrom(4096)
                if data:
                    msg = data.decode('utf-8', errors='replace')
                    print(f"  [eBPF IPC] {msg}")
                    EBPF_EVENTS.append(msg)
                    if len(EBPF_EVENTS) > 10:
                        EBPF_EVENTS.pop(0)
            except Exception:
                pass
                
    import threading
    threading.Thread(target=ebpf_listener, daemon=True).start()

    max_events  = int(sys.argv[1]) if len(sys.argv) > 1 else None
    events_seen = 0

    def sig_handler(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, sig_handler)

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=4)

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
                executor.submit(handle_syscall_query, sock, payload[:QUERY_SIZE])
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
