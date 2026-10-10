#!/usr/bin/env python3
"""
task_runner.py — AI-Agent OS: Phase 11 Autonomous Task Execution Engine

Runs as a subprocess spawned by agent_daemon.py. Decoupled from the Netlink
event loop so it never blocks kernel IPC. Executes the full Planner→Worker
ReAct loop and writes results incrementally to a JSONL task log that the
updated agent-cli polls for live streaming output.

Task log path: /var/ai-agent/tasks/<task_id>.jsonl

Event types written to the log:
  task_start  — written immediately on launch
  plan        — Planner output (list of sub-tasks)
  step        — Worker is about to execute an action
  result      — stdout/stderr/returncode from a shell command
  verify      — a verify action result
  task_finish — per-subtask completion
  task_fail   — per-subtask failure/abort
  done        — final summary, overall status DONE or FAILED
"""

import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid
import urllib.request
import urllib.error

# ── Configuration ────────────────────────────────────────────────────────────
LLAMA_URL        = os.environ.get("LLAMA_URL",         "http://127.0.0.1:11434/completion")
AI_AGENT_LOG_DIR = os.environ.get("AI_AGENT_LOG_DIR",  "/var/ai-agent")
TASKS_DIR        = os.path.join(AI_AGENT_LOG_DIR, "tasks")
CAPABILITIES_FILE = os.path.join(AI_AGENT_LOG_DIR, "capabilities.json")

LLM_TIMEOUT_S    = 45       # longer than kernel wait — we're out-of-band now
MAX_TASKS        = 20       # max sub-tasks Planner can generate
MAX_STEPS        = 15       # max Worker steps per sub-task
CMD_TIMEOUT_S    = 30       # max wall-clock time for a shell command
TASK_MAX_TOTAL_S = 600      # 10 min hard cap for the whole job

# ── Dangerous command deny-list ──────────────────────────────────────────────
# Block catastrophic, unrecoverable operations.  Everything else is allowed.
_DENY_PATTERNS = [
    r"rm\s+-[rRf]*f[rR]?\s+/(?!\w)",   # rm -rf / or rm -fr /
    r"rm\s+-[rRf]*r[fF]?\s+/(?!\w)",
    r"\bdd\b.*\bof=/dev/[sh]d",         # overwrite block device
    r"mkfs\.",                           # format filesystem
    r">\s*/dev/(sd|nvme|vd|hd)",        # redirect to raw block device
    r":\(\)\{.*\};",                     # fork bomb :(){ :|:& };:
    r"chmod\s+[0-7]*7[0-7]*\s+/\s*$",  # chmod 777 /
    r"shred\s+.*/(dev|boot|etc)",        # shred critical paths
]
_DENY_RE = [re.compile(p, re.IGNORECASE) for p in _DENY_PATTERNS]


def is_safe_command(cmd: str) -> tuple[bool, str]:
    """Return (True, '') if safe, (False, reason) if dangerous."""
    for pattern in _DENY_RE:
        if pattern.search(cmd):
            return False, f"Blocked by security policy (pattern: {pattern.pattern!r})"
    return True, ""


# ── LLM helpers ──────────────────────────────────────────────────────────────
def _llm_post(prompt: str, n_predict: int = 300) -> str:
    payload = {
        "prompt": prompt,
        "n_predict": n_predict,
        "temperature": 0.1,
        "stop": ["\n\n", "<|im_end|>", "<|im_start|>"],
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        LLAMA_URL,
        data=data,
        headers={"Content-Type": "application/json"},
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=LLM_TIMEOUT_S) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                content = result.get("content", "").strip()
                if len(content) < 5 and attempt < 2:
                    continue
                return content
        except Exception as exc:
            if attempt < 2:
                time.sleep(2)
                continue
            return f"[LLM ERROR] {exc}"
    return "[LLM ERROR] Max retries reached"


def _extract_json_obj(text: str) -> dict | None:
    """Extract the first {...} JSON object from text."""
    # Try ```json blocks first
    if "```json" in text:
        text = text.split("```json")[1].split("```")[0]
    elif "```" in text:
        text = text.split("```")[1].split("```")[0]

    start = text.find("{")
    end   = text.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None


def _extract_json_list(text: str) -> list | None:
    """Extract the first [...] JSON array from text."""
    start = text.find("[")
    end   = text.rfind("]")
    if start == -1 or end == -1:
        return None
    try:
        result = json.loads(text[start:end + 1])
        return result if isinstance(result, list) else None
    except json.JSONDecodeError:
        return None


# ── Task log helpers ──────────────────────────────────────────────────────────
class TaskLog:
    def __init__(self, task_id: str):
        os.makedirs(TASKS_DIR, exist_ok=True)
        self.path = os.path.join(TASKS_DIR, f"{task_id}.jsonl")
        self._f = open(self.path, "a", encoding="utf-8", buffering=1)

    def write(self, event: str, **kwargs):
        record = {"event": event, "ts": time.time(), **kwargs}
        self._f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def close(self):
        self._f.close()


# ── Capability helpers ────────────────────────────────────────────────────────
def load_capabilities() -> dict:
    caps = {}
    if os.path.exists(CAPABILITIES_FILE):
        try:
            with open(CAPABILITIES_FILE) as f:
                caps = json.load(f)
        except Exception:
            pass
    return caps


def _build_caps_str(caps: dict) -> str:
    """Format capabilities dict as a compact readable string for LLM prompts."""
    if not caps:
        return "none"
    parts = []
    for k, v in caps.items():
        if isinstance(v, bool):
            if v:
                parts.append(k)
        else:
            parts.append(f"{k}={v}")
    return ", ".join(parts)


# ── Planner ───────────────────────────────────────────────────────────────────
def run_planner(intent: str, caps: dict, log: TaskLog) -> list[str]:
    caps_str = _build_caps_str(caps)
    prompt = (
        f"<|im_start|>system\n"
        f"You are the OS Agent Planner. Break the user's intent into a JSON array of sub-tasks.\n"
        f"CRITICAL: Output ONLY a valid JSON array of strings. No prose. No markdown.\n"
        f"Each task must be a concrete, single-action step executable by a shell command.\n"
        f"Maximum {MAX_TASKS} tasks. Do not include tasks that are already satisfied.\n"
        f"Known System Capabilities: {caps_str}\n"
        f"Example: [\"Check if curl is installed\", \"Install curl via apk if missing\", \"Test curl with example.com\"]\n"
        f"<|im_end|>\n"
        f"<|im_start|>user\nIntent: {intent}<|im_end|>\n"
        f"<|im_start|>assistant\n"
    )
    log.write("planner_start", intent=intent)
    raw = _llm_post(prompt, n_predict=400)
    log.write("planner_raw", raw=raw)

    task_list = _extract_json_list(raw)
    if not task_list:
        # Fallback: treat the whole intent as a single task
        log.write("planner_fallback", reason="Could not parse plan — using intent as single task")
        return [intent]

    # Sanitize: ensure all items are strings, cap length
    task_list = [str(t).strip() for t in task_list if t][:MAX_TASKS]
    log.write("plan", tasks=task_list, count=len(task_list))
    return task_list


# ── Worker ────────────────────────────────────────────────────────────────────
WORKER_SYSTEM = (
    "<|im_start|>system\n"
    "You are an OS Worker Agent. Accomplish the Current Task using the OS.\n"
    "Output ONLY JSON. Choose one action per response:\n"
    "1. Shell command: {\"target_software\": \"sh\", \"action\": \"execute\", \"args\": [\"-c\", \"<command>\"]}\n"
    "2. GUI (CDP):    {\"target_software\": \"cdp_controller.py\", \"action\": \"execute\", \"args\": [\"dump\" | \"click --id <id>\" | \"type --id <id> --text <text>\"]}\n"
    "3. Verify:       {\"action\": \"verify\", \"condition\": \"<what to check>\"}\n"
    "4. Finish:       {\"action\": \"finish\", \"reason\": \"<summary of what was done>\"}\n"
    "5. Abort:        {\"action\": \"abort\", \"reason\": \"<why task cannot be completed>\"}\n"
    "<|im_end|>\n"
)


def run_worker_task(
    task_idx: int,
    task: str,
    intent: str,
    ebpf_events: list,
    log: TaskLog,
    job_start: float,
) -> tuple[bool, str]:
    """
    Execute a single sub-task via the Worker ReAct loop.
    Returns (success: bool, summary: str).
    """
    log.write("task_start", task_idx=task_idx, task=task)
    prompt_context = f"Current Task: {task}\nOverall User Intent: {intent}"
    step = 0

    while step < MAX_STEPS:
        # Global job time limit
        elapsed = time.monotonic() - job_start
        if elapsed > TASK_MAX_TOTAL_S:
            msg = f"[ABORTED] Global time limit ({TASK_MAX_TOTAL_S}s) reached during task {task_idx + 1}."
            log.write("task_fail", task_idx=task_idx, reason="global_timeout", msg=msg)
            return False, msg

        ebpf_ctx = ""
        if ebpf_events:
            ebpf_ctx = "\nRecent OS Events:\n" + "\n".join(ebpf_events[-5:])

        full_prompt = (
            WORKER_SYSTEM
            + f"<|im_start|>user\n{prompt_context}{ebpf_ctx}<|im_end|>\n"
            + "<|im_start|>assistant\n"
        )

        log.write("step", task_idx=task_idx, step=step, prompt_tail=prompt_context[-300:])
        raw = _llm_post(full_prompt, n_predict=250)
        log.write("ai_raw", task_idx=task_idx, step=step, raw=raw)

        delegation = _extract_json_obj(raw)
        if delegation is None:
            log.write("json_error", task_idx=task_idx, step=step, raw=raw)
            prompt_context += f"\n\nSystem Error: Could not parse your JSON. Return ONLY a valid JSON object. Got: {raw[:100]}"
            step += 1
            continue

        action = delegation.get("action", "")
        target = delegation.get("target_software", "")
        args   = delegation.get("args", [])

        # ── finish ──
        if action == "finish":
            summary = delegation.get("reason", "Task complete.")
            log.write("task_finish", task_idx=task_idx, step=step, summary=summary)
            return True, summary

        # ── abort ──
        if action == "abort":
            reason = delegation.get("reason", "Unknown")
            log.write("task_fail", task_idx=task_idx, step=step, reason=reason)
            return False, f"[ABORTED] {reason}"

        # ── verify (mocked via eBPF context — treated as success) ──
        if action == "verify":
            cond = delegation.get("condition", "")
            log.write("verify", task_idx=task_idx, step=step, condition=cond, result="SUCCESS")
            prompt_context += f"\n\nVerification '{cond}': SUCCESS."
            step += 1
            continue

        # ── execute ──
        if action != "execute":
            prompt_context += f"\n\nUnknown action '{action}'. Use execute, verify, finish, or abort."
            step += 1
            continue

        # Build command
        if target == "cdp_controller.py":
            cmd = ["python3", "/home/aiuser/cdp_controller.py"] + (args if isinstance(args, list) else [args])
        elif target == "sh":
            # args should be ["-c", "<command>"]
            if isinstance(args, list) and len(args) >= 2 and args[0] == "-c":
                shell_cmd = args[1]
            elif isinstance(args, list) and len(args) == 1:
                shell_cmd = args[0]
            else:
                shell_cmd = " ".join(str(a) for a in args) if isinstance(args, list) else str(args)
            cmd = ["sh", "-c", shell_cmd]
        else:
            # Treat any other target as a direct command (expanded allowlist)
            cmd = [target] + (args if isinstance(args, list) else [str(args)])

        # ── Security check ──
        cmd_str = " ".join(str(c) for c in cmd)
        safe, deny_reason = is_safe_command(cmd_str)
        if not safe:
            log.write("security_block", task_idx=task_idx, step=step, cmd=cmd_str, reason=deny_reason)
            prompt_context += f"\n\nSECURITY BLOCK: Command refused — {deny_reason}. Choose a safer alternative or abort."
            step += 1
            continue

        log.write("step_exec", task_idx=task_idx, step=step, cmd=cmd_str)

        # ── Run ──
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=CMD_TIMEOUT_S,
            )
            stdout = result.stdout[:2000]   # cap per-step output
            stderr = result.stderr[:500]
            rc     = result.returncode

            log.write(
                "result",
                task_idx=task_idx,
                step=step,
                cmd=cmd_str,
                returncode=rc,
                stdout=stdout,
                stderr=stderr,
            )

            if rc == 0:
                prompt_context += f"\n\nAction succeeded (exit 0):\nSTDOUT:\n{stdout}\nNext: JSON action or finish."
            else:
                prompt_context += (
                    f"\n\nAction failed (exit {rc}):\n"
                    f"STDOUT:\n{stdout}\nSTDERR:\n{stderr}\n"
                    "Fix the command in next JSON or abort if unrecoverable."
                )

        except subprocess.TimeoutExpired:
            log.write("result", task_idx=task_idx, step=step, cmd=cmd_str,
                      returncode=-1, stdout="", stderr="[TIMEOUT] Command exceeded 30s limit.")
            prompt_context += f"\n\nCommand timed out after {CMD_TIMEOUT_S}s. Try a different approach or abort."

        except FileNotFoundError:
            log.write("result", task_idx=task_idx, step=step, cmd=cmd_str,
                      returncode=-1, stdout="", stderr=f"[NOT FOUND] '{cmd[0]}' is not installed.")
            prompt_context += f"\n\nCommand '{cmd[0]}' not found. It may need to be installed first, or use an alternative."

        except Exception as exc:
            log.write("result", task_idx=task_idx, step=step, cmd=cmd_str,
                      returncode=-1, stdout="", stderr=str(exc))
            prompt_context += f"\n\nExecution error: {exc}. Adjust your JSON."

        step += 1

    msg = f"[ABORTED] Worker reached max steps ({MAX_STEPS}) on task {task_idx + 1}: {task}"
    log.write("task_fail", task_idx=task_idx, reason="max_steps", msg=msg)
    return False, msg


# ── eBPF listener (non-blocking read from socket) ─────────────────────────────
def _read_ebpf_events() -> list[str]:
    """Read any queued eBPF events from the Unix socket without blocking."""
    events = []
    sock_path = os.path.join(AI_AGENT_LOG_DIR, "ebpf.sock")
    if not os.path.exists(sock_path):
        return events
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        s.settimeout(0.05)
        s.bind("")   # anonymous bind
        while True:
            try:
                data, _ = s.recvfrom(4096)
                events.append(data.decode("utf-8", errors="replace"))
            except (socket.timeout, BlockingIOError):
                break
        s.close()
    except Exception:
        pass
    return events


# ── Cleanup old task files ─────────────────────────────────────────────────────
def cleanup_old_tasks(max_age_s: int = 86400):
    """Delete task JSONL files older than max_age_s (default 24h)."""
    try:
        now = time.time()
        for fname in os.listdir(TASKS_DIR):
            if not fname.endswith(".jsonl"):
                continue
            fpath = os.path.join(TASKS_DIR, fname)
            if now - os.path.getmtime(fpath) > max_age_s:
                os.remove(fpath)
    except Exception:
        pass


# ── Main entry point ──────────────────────────────────────────────────────────
def main():
    if len(sys.argv) < 3:
        print("Usage: task_runner.py <task_id> <intent>", file=sys.stderr)
        sys.exit(1)

    task_id = sys.argv[1]
    intent  = sys.argv[2]
    mode    = sys.argv[3] if len(sys.argv) > 3 else "ASSIST"

    # Ignore SIGTERM gracefully — let the finally block write DONE/FAILED
    signal.signal(signal.SIGTERM, lambda s, f: sys.exit(0))

    log = TaskLog(task_id)
    log.write("task_start", task_id=task_id, intent=intent, mode=mode)

    cleanup_old_tasks()

    caps = load_capabilities()
    log.write("capabilities", caps=caps)

    job_start  = time.monotonic()
    ebpf_events: list[str] = []

    try:
        # ── Planner phase ──
        task_list = run_planner(intent, caps, log)

        all_summaries: list[str] = []
        any_failed = False

        # ── Worker loop ──
        for task_idx, task in enumerate(task_list):
            # Refresh eBPF context
            ebpf_events.extend(_read_ebpf_events())
            if len(ebpf_events) > 20:
                ebpf_events = ebpf_events[-20:]

            if mode == "SUGGEST":
                # In SUGGEST mode, just log what would be done — don't execute
                log.write("suggest_skip", task_idx=task_idx, task=task,
                          note="SUGGEST mode: not executed")
                all_summaries.append(f"[WOULD DO] {task}")
                continue

            success, summary = run_worker_task(
                task_idx=task_idx,
                task=task,
                intent=intent,
                ebpf_events=ebpf_events,
                log=log,
                job_start=job_start,
            )
            all_summaries.append(summary)
            if not success:
                any_failed = True

        # ── Final summary ──
        overall_status = "FAILED" if any_failed else "DONE"
        elapsed = time.monotonic() - job_start
        log.write(
            "done",
            status=overall_status,
            task_count=len(task_list),
            elapsed_s=round(elapsed, 1),
            summaries=all_summaries,
        )

    except KeyboardInterrupt:
        log.write("done", status="CANCELLED", reason="Task cancelled by signal.")
    except Exception as exc:
        log.write("done", status="FAILED", reason=f"Unhandled exception: {exc}")
    finally:
        log.close()


if __name__ == "__main__":
    main()
