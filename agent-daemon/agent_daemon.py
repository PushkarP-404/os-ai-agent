#!/usr/bin/env python3
"""
agent_daemon.py — Elenchus: Unified Kernel-Userspace Agent Daemon

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
import importlib.util
import pathlib
import threading

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
AGENT_MODE  = os.environ.get("AGENT_MODE", "SUGGEST") # "SUGGEST", "ASSIST", or "APPLY" (safe default)

# For logger.py compatibility we keep OLLAMA_MODEL pointing at LLAMA_MODEL
OLLAMA_URL   = LLAMA_URL
OLLAMA_MODEL = LLAMA_MODEL

# Timeout for LLM HTTP call. Keep above the kernel's 15s wait_event timeout.
LLM_TIMEOUT_S = 30

CAPABILITIES_FILE = "/var/ai-agent/capabilities.json"
SYSTEM_CAPABILITIES = {}
EBPF_EVENTS = []

# ── Task runner + Harness V2 paths ──────────────────────────────────────────
TASK_RUNNER_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "task_runner.py",
)
if not os.path.exists(TASK_RUNNER_PATH):
    TASK_RUNNER_PATH = "/usr/local/lib/ai-agent/task_runner.py"

TASKS_DIR    = os.path.join(os.environ.get("AI_AGENT_LOG_DIR", "/var/ai-agent"), "tasks")
HARNESS_V2   = os.environ.get("HARNESS_V2", "1") == "1"

# ── Load harness_v2 in-process (avoids subprocess startup cost) ───────────────
_harness_mod = None
if HARNESS_V2:
    _hv2_path = pathlib.Path(__file__).parent / "harness_v2.py"
    if not _hv2_path.exists():
        _hv2_path = pathlib.Path("/usr/local/lib/ai-agent/harness_v2.py")
    if _hv2_path.exists():
        try:
            _spec = importlib.util.spec_from_file_location("harness_v2", str(_hv2_path))
            _harness_mod = importlib.util.module_from_spec(_spec)
            sys.modules["harness_v2"] = _harness_mod
            _spec.loader.exec_module(_harness_mod)
            print(f"[DAEMON] harness_v2 loaded from {_hv2_path}")
        except Exception as _hv2_err:
            print(f"[WARN] harness_v2 load failed ({_hv2_err}); falling back to task_runner subprocess")
            _harness_mod = None

# Per-job cancellation tokens keyed by task_id
_cancel_tokens: dict[str, threading.Event] = {}
_cancel_lock   = threading.Lock()
_worker_pool   = concurrent.futures.ThreadPoolExecutor(max_workers=8)

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
    # Hardware compute & accelerator detection
    live["cpu_cores"] = os.cpu_count() or 1
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    live["total_mem_kb"] = int(line.split()[1])
                    break
    except Exception:
        pass

    live["has_gpu"] = False
    if os.path.exists("/dev/dri") and any(pathlib.Path("/dev/dri").glob("renderD*")):
        live["has_gpu"] = True
        live["gpu_backend"] = "drm_vulkan"
    elif any(pathlib.Path("/dev").glob("nvidia*")):
        live["has_gpu"] = True
        live["gpu_backend"] = "nvidia"

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


# ── Plan grammar for grammar-constrained decoding ─────────────────────────────
_GRAMMAR_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "plan_grammar.gbnf")
_PLAN_GRAMMAR_STR: str = ""
try:
    with open(_GRAMMAR_PATH, "r", encoding="utf-8") as _gf:
        _PLAN_GRAMMAR_STR = _gf.read()
    print(f"[DAEMON] Grammar-constrained decoding enabled ({_GRAMMAR_PATH})")
except OSError:
    print(f"[DAEMON] plan_grammar.gbnf not found — free-form JSON parsing active")


def query_ollama(prompt: str, min_tokens: int = 8, n_predict: int = 200,
                 use_grammar: bool = True) -> str:
    """POST a completion request to llama-server; return the response text.

    Grammar-constrained decoding:
        When plan_grammar.gbnf is present and use_grammar=True, the grammar
        field is included in the request so llama-server constrains the output
        to valid Plan JSON automatically.  This eliminates F1 (invalid JSON)
        and F2 (wrong tool name) failures without any model fine-tuning.

    Bug fix (2026-09-27): Removed '.' from stop tokens — it caused the 135M model
    to terminate after a single token (e.g. the model outputting '1.' immediately
    stops). Also increased n_predict from 128 to 200 to allow full JSON responses.
    Added retry if response is too short (garbage detection).
    """
    # NOTE: Do NOT include '.' or single '\n' as stop tokens — the tiny SmolLM2
    # model outputs these almost immediately, resulting in single-character responses.
    payload: dict = {
        "prompt":      prompt,
        "n_predict":   n_predict,
        "temperature": 0.1,
        "stop": ["<|im_end|>", "<|im_start|>"],
    }
    # Inject grammar only for planning prompts (identified by the plan schema marker)
    # Repair prompts and simple queries skip grammar to allow more flexible output.
    if use_grammar and _PLAN_GRAMMAR_STR and '"goal"' in prompt:
        payload["grammar"] = _PLAN_GRAMMAR_STR
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
        except urllib.error.HTTPError as e:
            if "grammar" in payload:
                print(f"  [WARN] LLM rejected grammar ({e}). Retrying without grammar...")
                del payload["grammar"]
                data = json.dumps(payload).encode("utf-8")
                req = urllib.request.Request(
                    LLAMA_URL,
                    data=data,
                    headers={"Content-Type": "application/json"},
                )
                continue
            if attempt < 2:
                print(f"  [WARN] LLM request failed: {e}. Retrying... Attempt {attempt+1}/3")
                continue
            return (
                f"[AI AGENT FALLBACK] Local LLM unreachable ({e}). "
                f"Policy verdict: ALLOW with monitoring."
            )
        except Exception as e:
            if attempt < 2:
                print(f"  [WARN] LLM request failed: {e}. Retrying... Attempt {attempt+1}/3")
                continue
            return (
                f"[AI AGENT FALLBACK] Local LLM unreachable ({e}). "
                f"Policy verdict: ALLOW with monitoring."
            )


def send_desktop_notification(title, message, urgency="normal"):
    """Dispatches a desktop notification to active X11 desktop user if present."""
    if not shutil.which("notify-send"):
        return
    env = os.environ.copy()
    if "DISPLAY" not in env:
        env["DISPLAY"] = ":0.0"
    if "XAUTHORITY" not in env:
        for xauth in ["/home/aiuser/.Xauthority", "/root/.Xauthority"]:
            if os.path.exists(xauth):
                env["XAUTHORITY"] = xauth
                break
    try:
        subprocess.run(
            ["notify-send", "-u", urgency, "-a", "Elenchus", "-i", "ai-agent", title, str(message)[:150]],
            env=env,
            timeout=2,
            capture_output=True
        )
    except Exception:
        pass


def handle_syscall_query(sock, query_payload):
    """Process a Phase 4 AI_MSG_SYSCALL_QUERY message and reply to kernel."""
    query_id = 0
    try:
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
            is_resume = False
            resume_task_id = None

            if query.startswith("/suggest "):
                mode = "SUGGEST"
                query = query[9:].strip()
            elif query.startswith("/assist "):
                mode = "ASSIST"
                query = query[8:].strip()
            elif query.startswith("/apply "):
                mode = "APPLY"
                query = query[7:].strip()

            if query.startswith("/resume "):
                is_resume = True
                resume_task_id = query[8:].strip()
            elif query.startswith("resume "):
                parts = query.split(None, 1)
                if len(parts) > 1:
                    is_resume = True
                    resume_task_id = parts[1].strip()

            # ── Dispatch to harness_v2 (in-process) or task_runner subprocess ───────
            if is_resume and resume_task_id:
                task_id = resume_task_id
            else:
                task_id = str(uuid.uuid4())[:8]

            task_file = os.path.join(TASKS_DIR, f"{task_id}.jsonl")
            os.makedirs(TASKS_DIR, exist_ok=True)

            latency_ms = 0.0
            t_start    = time.monotonic()

            if HARNESS_V2 and _harness_mod is not None:
                # ── In-process V2 harness (preferred) ────────────────────────────
                try:
                    from task_runner import TaskLog, get_caller_context
                    _log = TaskLog(task_id)
                    _caller = get_caller_context(caller_pid)
                except Exception:
                    _log    = type('_NullLog', (), {'write': lambda *a, **k: None, 'close': lambda s: None})()
                    _caller = {"uid": os.getuid(), "gid": os.getgid(),
                               "user": os.environ.get("USER", "root"),
                               "home": os.environ.get("HOME", "/root"),
                               "cwd":  "/"}

                cancel_event = threading.Event()
                with _cancel_lock:
                    _cancel_tokens[task_id] = cancel_event

                base_dir = os.environ.get("AI_AGENT_LOG_DIR", "/var/ai-agent")
                store    = _harness_mod.JobStore(base_dir, task_id, _log.write)

                if is_resume:
                    store.load_state()
                    if mode == AGENT_MODE and "mode" in store.state:
                        mode = store.state["mode"]
                    if "caller" in store.state and store.state["caller"]:
                        _caller = store.state["caller"]
                    _log.write("resume", task_id=task_id)
                    print(f"  [DISPATCH-V2] Resuming job {task_id}, mode={mode}")
                else:
                    try:
                        store.create(query, mode, _caller)
                        _log.write("start", task_id=task_id, query=query, mode=mode)
                    except Exception as _ce:
                        print(f"  [WARN] Failed to write initial job start: {_ce}")

                def _run_v2_job():
                    """Run the verified harness in a thread-pool worker."""
                    # Adaptive n_predict: give more tokens to complex multi-step tasks.
                    # Simple tasks (~1-3 steps) need ~700 tokens for plan JSON.
                    # Complex multi-app tasks (~8+ steps) need up to 2000 tokens.
                    import re as _re
                    _action_count = sum(
                        1 for v in [
                            "open", "read", "write", "create", "send", "save",
                            "export", "move", "find", "update", "compile", "run",
                            "check", "generate", "summarize", "compare", "deploy",
                        ] if v in query.lower()
                    )
                    _n_predict = min(2000, max(700, _action_count * 180))

                    harness = _harness_mod.Harness(
                        store, _caller, mode,
                        lambda prompt: query_ollama(prompt, n_predict=_n_predict),
                        cancelled=cancel_event.is_set,
                    )
                    try:
                        if is_resume:
                            status = harness.resume()
                            print(f"  [V2-RESUME] Job {task_id} finished: {status}")
                        else:
                            status = harness.run(query)
                            print(f"  [V2] Job {task_id} finished: {status}")
                        sys.stdout.flush()
                        send_desktop_notification(f"AI Task {status}", f"{query[:60]} ({status})")
                    except Exception as exc:
                        send_desktop_notification("AI Task Failed", str(exc)[:80], urgency="critical")
                        import traceback
                        store.event("task_fail", task_idx=0, reason="harness_exception",
                                    msg=str(exc))
                        store.event("done", status="FAILED", task_count=0,
                                    elapsed_s=0, summaries=[str(exc)])
                        _log.write("done", status="FAILED", reason=str(exc))
                        print(f"  [V2] Job {task_id} EXCEPTION: {exc}")
                        traceback.print_exc()
                        sys.stdout.flush()
                    finally:
                        _log.close()
                        with _cancel_lock:
                            _cancel_tokens.pop(task_id, None)

                _worker_pool.submit(_run_v2_job)
                latency_ms    = (time.monotonic() - t_start) * 1000.0
                action_str    = "Resuming" if is_resume else "Running"
                final_response = (
                    f"[TASK:{task_id}] {action_str} in background (harness_v2).\n"
                    f"Log: {task_file}\n"
                    f"Mode: {mode}"
                )
                print(f"  [DISPATCH-V2] Submitted job {task_id}, mode={mode}, resume={is_resume}")
                sys.stdout.flush()

            elif os.path.exists(TASK_RUNNER_PATH):
                # ── Subprocess fallback (HARNESS_V2=0 or harness_v2 failed to load) ─
                try:
                    env = os.environ.copy()
                    env["LLAMA_URL"]        = LLAMA_URL
                    env["AI_AGENT_LOG_DIR"] = os.environ.get("AI_AGENT_LOG_DIR", "/var/ai-agent")
                    env["CALLER_PID"]       = str(caller_pid)
                    env["HARNESS_V2"]       = "0"   # legacy path in task_runner

                    subprocess.Popen(
                        [sys.executable, TASK_RUNNER_PATH, task_id, query, mode, str(caller_pid)],
                        stdout=open("/var/log/task_runner.log", "a"),
                        stderr=subprocess.STDOUT,
                        env=env,
                        close_fds=True,
                    )
                    latency_ms    = (time.monotonic() - t_start) * 1000.0
                    final_response = (
                        f"[TASK:{task_id}] Running in background (legacy).\n"
                        f"Log: {task_file}\n"
                        f"Mode: {mode}"
                    )
                    print(f"  [DISPATCH-LEGACY] Spawned task_runner for task {task_id}")
                    sys.stdout.flush()
                except Exception as exc:
                    final_response = f"[ERROR] Failed to spawn task_runner: {exc}"
                    latency_ms     = (time.monotonic() - t_start) * 1000.0
            else:
                final_response = (
                    f"[ERROR] Neither harness_v2 nor task_runner.py is available. "
                    f"Deploy the agent files and retry."
                )
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

    except Exception as e:
        import traceback
        print(f"  [ERROR] handle_syscall_query unhandled exception: {e}")
        traceback.print_exc()
        sys.stdout.flush()
        if query_id:
            try:
                err_resp = f"[ERROR] Daemon internal error: {e}".encode("utf-8")[:2047]
                payload  = struct.pack(RESP_FORMAT, query_id, 0, err_resp)
                hdr      = struct.pack(NLMSG_HDR_FORMAT, NLMSG_HDR_SIZE + len(payload),
                                       AI_MSG_SYSCALL_RESP, 0, 0, os.getpid())
                sock.sendto(hdr + payload, (0, 0))
            except Exception:
                pass


def reconcile_orphaned_jobs():
    """Scan /var/ai-agent/jobs/ on daemon boot and mark interrupted jobs."""
    base_dir = os.environ.get("AI_AGENT_LOG_DIR", "/var/ai-agent")
    jobs_dir = pathlib.Path(base_dir) / "jobs"
    if not jobs_dir.exists():
        return
    reconciled = 0
    for job_file in jobs_dir.glob("*/job.json"):
        try:
            with open(job_file, "r", encoding="utf-8") as f:
                state = json.load(f)
            if state.get("status") in ("RUNNING", "VERIFYING", "PLANNING"):
                task_id = state.get("job_id")
                state["status"] = "INTERRUPTED"
                state["interrupted_at"] = time.time()
                with open(job_file, "w", encoding="utf-8") as f:
                    json.dump(state, f, indent=2, sort_keys=True)
                reconciled += 1
                print(f"[RECOVERY] Interrupted job {task_id} marked as INTERRUPTED")
        except Exception as err:
            print(f"[RECOVERY] Error inspecting {job_file}: {err}")
    if reconciled > 0:
        print(f"[RECOVERY] Reconciled {reconciled} orphaned job(s) from previous daemon run.")


def main():
    print("==================================================")
    print("           Elenchus: Unified Agent Daemon         ")
    print("==================================================")
    print(f"[AGENT DAEMON] PID: {os.getpid()}")
    print(f"[AGENT DAEMON] LLM: {OLLAMA_URL} (model: {OLLAMA_MODEL})")
    print(f"[AGENT DAEMON] Logging: {'ENABLED -> /var/ai-agent/' if LOGGING_ENABLED else 'DISABLED (logger.py not found)'}")
    print(f"[AGENT DAEMON] Task runner: {TASK_RUNNER_PATH}")
    print(f"[AGENT DAEMON] Tasks dir: {TASKS_DIR}")

    # ── Phase 11: build live OS capability context ───────────────────────────
    build_live_capabilities()
    reconcile_orphaned_jobs()
    os.makedirs(TASKS_DIR, exist_ok=True)

    # ── Phase 12: OS Automation Engine & Intent Bus ───────────────────────────
    try:
        from automation_engine import get_automation_engine
        _automation_engine = get_automation_engine()
        print("[AGENT DAEMON] OS Automation Engine & Intent Bus active (/var/run/ai-agent/intent.sock)")
        sys.stdout.flush()
    except Exception as _ae_err:
        print(f"[WARN] Could not initialize OS Automation Engine: {_ae_err}")
        sys.stdout.flush()

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
                _worker_pool.submit(handle_syscall_query, sock, payload[:QUERY_SIZE])
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
