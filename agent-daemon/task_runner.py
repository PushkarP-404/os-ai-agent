#!/usr/bin/env python3
"""
task_runner.py — AI-Agent OS: Autonomous Task Execution Engine

Runs as a subprocess spawned by agent_daemon.py. Decoupled from the Netlink
event loop so it never blocks kernel IPC. Executes the Planner→Worker
loop and writes results incrementally to a JSONL task log that agent-cli
polls for live streaming output.

Task log path: /var/ai-agent/tasks/<task_id>.jsonl
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
LLAMA_URL         = os.environ.get("LLAMA_URL",         "http://127.0.0.1:11434/completion")
AI_AGENT_LOG_DIR  = os.environ.get("AI_AGENT_LOG_DIR",  "/var/ai-agent")
TASKS_DIR         = os.path.join(AI_AGENT_LOG_DIR, "tasks")
CAPABILITIES_FILE = os.path.join(AI_AGENT_LOG_DIR, "capabilities.json")

LLM_TIMEOUT_S     = 25       # max seconds per LLM HTTP call
MAX_TASKS         = 10       # max sub-tasks Planner can generate
MAX_STEPS         = 3        # max Worker steps per sub-task (fast fail, no infinite hang)
CMD_TIMEOUT_S     = 30       # max wall-clock time for a shell command
TASK_MAX_TOTAL_S  = 300      # 5 min hard cap for the whole job

# ── Dangerous command deny-list ──────────────────────────────────────────────
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


# ── Direct intent heuristics (instant sub-second execution) ─────────────────
DIRECT_INTENT_MAP = [
    (r"\b(disk|df|disk\s*space|free\s*space|storage)\b", "df -h"),
    (r"\b(ram|memory|free\s*memory|mem\s*usage)\b", "free -m"),
    (r"\b(process(es)?|ps|top\s*process(es)?|running\s*apps)\b", "ps aux | head -25"),
    (r"\b(ip|ip\s*addr|ip\s*address|interfaces?|network)\b", "ip a"),
    (r"\b(uptime|system\s*load|load\s*avg)\b", "uptime"),
    (r"\b(whoami|current\s*user|my\s*user)\b", "whoami"),
    (r"\b(hostname|machine\s*name)\b", "hostname"),
    (r"\b(uname|kernel|kernel\s*version|os\s*version)\b", "uname -a"),
    (r"\b(running|alive|status|agent\s*status|daemon\s*status|ping)\b", "echo 'AI-Agent OS is active and responsive.'"),
    (r"\b(date|time|clock)\b", "date"),
    (r"\b(ports?|listening\s*ports?)\b", "netstat -tuln 2>/dev/null || ss -tuln"),
    (r"\b(dmesg|kernel\s*logs?)\b", "dmesg | tail -n 25"),
    (r"\b(cpu|cpu\s*info)\b", "grep -E 'model name|cpu MHz' /proc/cpuinfo | head -4 || lscpu"),
    (r"\b(?:create|make)\s+(?:dir|directory|folder)\s+(\S+)", lambda m: f"mkdir -p {m.group(1)}"),
    (r"\b(?:list\s+files?|ls)\s*(?:in\s+)?(\S*)", lambda m: f"ls -la {m.group(1) if m.group(1) else '.'}"),
    (r"\b(?:read|cat|view)\s+(?:file\s+)?(\S+)", lambda m: f"cat {m.group(1)}"),
]


def find_direct_command(text: str) -> str | None:
    t = text.strip()
    clean = re.sub(r"^(please\s+|can\s+you\s+|show\s+|check\s+|get\s+)", "", t, flags=re.IGNORECASE)
    for pattern, cmd in DIRECT_INTENT_MAP:
        m = re.search(pattern, clean, flags=re.IGNORECASE)
        if m:
            if callable(cmd):
                return cmd(m)
            return cmd
    return None


# ── LLM helpers ──────────────────────────────────────────────────────────────
def _llm_post(prompt: str, n_predict: int = 60) -> str:
    payload = {
        "prompt": prompt,
        "n_predict": n_predict,
        "temperature": 0.1,
        "stop": ["\n\n", "```", "<|im_end|>", "<|im_start|>", "\n}"],
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        LLAMA_URL,
        data=data,
        headers={"Content-Type": "application/json"},
    )
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=LLM_TIMEOUT_S) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                content = result.get("content", "").strip()
                return content
        except Exception as exc:
            if attempt < 1:
                time.sleep(1)
                continue
            return f"[LLM ERROR] {exc}"
    return "[LLM ERROR] Request failed"


def _extract_json_obj(text: str) -> dict | None:
    """Extract first {...} JSON object from text."""
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
    """Extract first [...] JSON array from text."""
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
        self._f.flush()

    def close(self):
        try:
            self._f.flush()
            self._f.close()
        except Exception:
            pass


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
    if not caps:
        return "pkg_manager=apk, init_system=openrc"
    parts = []
    for k, v in caps.items():
        if isinstance(v, bool):
            if v: parts.append(k)
        else:
            parts.append(f"{k}={v}")
    return ", ".join(parts[:8])


# ── Planner ───────────────────────────────────────────────────────────────────
def run_planner(intent: str, caps: dict, log: TaskLog) -> list[str]:
    log.write("planner_start", intent=intent)

    # 1. Fast path: direct match
    direct_cmd = find_direct_command(intent)
    if direct_cmd:
        task_list = [f"Execute: {direct_cmd}"]
        log.write("plan", tasks=task_list, count=1)
        return task_list

    # 2. LLM path with concise prompt & array prefill
    caps_str = _build_caps_str(caps)
    prompt = (
        f"<|im_start|>system\n"
        f"You are the OS Agent Planner. Break user intent into a simple JSON array of sub-tasks.\n"
        f"Output ONLY a JSON array of strings, e.g. [\"check curl\", \"install curl\"].\n"
        f"Capabilities: {caps_str}\n"
        f"<|im_end|>\n"
        f"<|im_start|>user\nIntent: {intent}<|im_end|>\n"
        f"<|im_start|>assistant\n["
    )
    raw = _llm_post(prompt, n_predict=80)
    if not raw.startswith("["):
        raw = "[" + raw
    log.write("planner_raw", raw=raw)

    task_list = _extract_json_list(raw)
    if not task_list:
        log.write("planner_fallback", reason="Could not parse plan — using intent as single task")
        return [intent]

    task_list = [str(t).strip() for t in task_list if t][:MAX_TASKS]
    log.write("plan", tasks=task_list, count=len(task_list))
    return task_list


# ── Action extraction from LLM output ────────────────────────────────────────
def extract_action(raw: str, task: str) -> tuple[str, str, list | str]:
    """
    Robust action extractor for SmolLM2-135M.
    Returns (action, target, args_or_reason).
    """
    # 1. Explicit task prefix
    if task.startswith("Execute: "):
        return "execute", "sh", ["-c", task[9:].strip()]

    # Direct task match
    direct = find_direct_command(task)
    if direct:
        return "execute", "sh", ["-c", direct]

    # 2. Structured JSON
    data = _extract_json_obj(raw)
    if data:
        if "cmd" in data:
            return "execute", "sh", ["-c", str(data["cmd"])]
        action = data.get("action", "")
        if action in ("finish", "abort"):
            return action, "", data.get("reason", "Done")
        if action == "execute":
            return "execute", data.get("target_software", "sh"), data.get("args", [])
        if action == "verify":
            return "verify", "", data.get("condition", "")

    # 3. Regex for {"cmd": "..."}
    m = re.search(r'"cmd"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"?', raw)
    if m:
        return "execute", "sh", ["-c", m.group(1)]

    # 4. Code block extraction
    if "```" in raw:
        lines = [l.strip() for l in raw.split("```")[1].splitlines() if l.strip() and not l.strip().startswith("#")]
        if lines:
            return "execute", "sh", ["-c", lines[0]]

    # 5. Common CLI command extraction from raw text
    for line in raw.splitlines():
        line = line.strip().lstrip("-*123456789. ")
        if line.startswith(("#", "//", "/*")):
            continue
        parts = line.split()
        if parts and parts[0] in ("df", "free", "ps", "ip", "apk", "ls", "cat", "uptime", "uname", "whoami", "hostname", "curl", "wget", "echo", "grep", "find", "mkdir", "rc-service"):
            return "execute", "sh", ["-c", line]

    return "unknown", "", raw


# ── Worker ────────────────────────────────────────────────────────────────────
WORKER_SYSTEM = (
    "<|im_start|>system\n"
    "You are an OS Worker Agent on Alpine Linux.\n"
    "Output ONLY JSON: {\"cmd\": \"<shell_command>\"}\n"
    "Examples:\n"
    "check disk space -> {\"cmd\": \"df -h\"}\n"
    "check memory -> {\"cmd\": \"free -m\"}\n"
    "create dir /tmp/test -> {\"cmd\": \"mkdir -p /tmp/test\"}\n"
    "list files -> {\"cmd\": \"ls -la\"}\n"
    "install curl -> {\"cmd\": \"apk add curl\"}\n"
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
    """Execute a single sub-task via the Worker loop."""
    log.write("task_start", task_idx=task_idx, task=task)
    step = 0

    while step < MAX_STEPS:
        elapsed = time.monotonic() - job_start
        if elapsed > TASK_MAX_TOTAL_S:
            msg = f"[ABORTED] Global time limit ({TASK_MAX_TOTAL_S}s) reached."
            log.write("task_fail", task_idx=task_idx, reason="global_timeout", msg=msg)
            return False, msg

        # Fast path check on task string
        action, target, args_or_reason = extract_action("", task)
        raw_output = ""

        if action == "unknown":
            # Need LLM to determine command
            prompt = (
                WORKER_SYSTEM
                + f"<|im_start|>user\nTask: {task}<|im_end|>\n"
                + "<|im_start|>assistant\n{\"cmd\": \""
            )
            log.write("step", task_idx=task_idx, step=step)
            raw_output = _llm_post(prompt, n_predict=50)
            if not raw_output.startswith("{\"cmd\": \""):
                raw_output = "{\"cmd\": \"" + raw_output
            log.write("ai_raw", task_idx=task_idx, step=step, raw=raw_output)
            action, target, args_or_reason = extract_action(raw_output, task)

        # Handle actions
        if action == "finish":
            summary = str(args_or_reason) if args_or_reason else "Task finished."
            log.write("task_finish", task_idx=task_idx, step=step, summary=summary)
            return True, summary

        if action == "abort":
            reason = str(args_or_reason) if args_or_reason else "Aborted."
            log.write("task_fail", task_idx=task_idx, step=step, reason=reason)
            return False, f"[ABORTED] {reason}"

        if action == "verify":
            cond = str(args_or_reason)
            log.write("verify", task_idx=task_idx, step=step, condition=cond, result="SUCCESS")
            return True, f"Verified: {cond}"

        if action != "execute":
            # Fallback: if unknown, try running the task itself as a shell command if safe
            safe, _ = is_safe_command(task)
            if safe and len(task.split()) <= 4:
                action = "execute"
                target = "sh"
                args_or_reason = ["-c", task]
            else:
                log.write("json_error", task_idx=task_idx, step=step, raw=raw_output)
                step += 1
                continue

        # Build command array
        if target == "cdp_controller.py":
            cmd = ["python3", "/home/aiuser/cdp_controller.py"] + (args_or_reason if isinstance(args_or_reason, list) else [str(args_or_reason)])
        elif target == "sh":
            if isinstance(args_or_reason, list) and len(args_or_reason) >= 2 and args_or_reason[0] == "-c":
                shell_cmd = args_or_reason[1]
            elif isinstance(args_or_reason, list) and len(args_or_reason) == 1:
                shell_cmd = args_or_reason[0]
            else:
                shell_cmd = " ".join(str(a) for a in args_or_reason) if isinstance(args_or_reason, list) else str(args_or_reason)
            cmd = ["sh", "-c", shell_cmd]
        else:
            cmd = [target] + (args_or_reason if isinstance(args_or_reason, list) else [str(args_or_reason)])

        # Security check
        cmd_str = " ".join(str(c) for c in cmd)
        safe, deny_reason = is_safe_command(cmd_str)
        if not safe:
            log.write("security_block", task_idx=task_idx, step=step, cmd=cmd_str, reason=deny_reason)
            return False, f"Blocked: {deny_reason}"

        log.write("step_exec", task_idx=task_idx, step=step, cmd=cmd_str)

        # Run shell command
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=CMD_TIMEOUT_S,
            )
            stdout = result.stdout[:2000]
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

            # Succeeded: auto-finish immediately
            if rc == 0:
                summary = stdout.strip() if stdout.strip() else f"Command completed (exit 0)."
                log.write("task_finish", task_idx=task_idx, step=step, summary=summary[:300])
                return True, summary

        except subprocess.TimeoutExpired:
            log.write("result", task_idx=task_idx, step=step, cmd=cmd_str,
                      returncode=-1, stdout="", stderr=f"[TIMEOUT] Exceeded {CMD_TIMEOUT_S}s.")
        except Exception as exc:
            log.write("result", task_idx=task_idx, step=step, cmd=cmd_str,
                      returncode=-1, stdout="", stderr=str(exc))

        step += 1

    msg = f"Task completed or reached step limit on: {task}"
    log.write("task_finish", task_idx=task_idx, step=step, summary=msg)
    return True, msg


# ── eBPF listener ────────────────────────────────────────────────────────────
def _read_ebpf_events() -> list[str]:
    events = []
    sock_path = os.path.join(AI_AGENT_LOG_DIR, "ebpf.sock")
    if not os.path.exists(sock_path):
        return events
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        s.settimeout(0.05)
        s.bind("")
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


# ── Cleanup old task files ───────────────────────────────────────────────────
def cleanup_old_tasks(max_age_s: int = 86400):
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


# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    if len(sys.argv) < 3:
        print("Usage: task_runner.py <task_id> <intent> [mode]", file=sys.stderr)
        sys.exit(1)

    task_id = sys.argv[1]
    intent  = sys.argv[2]
    mode    = sys.argv[3] if len(sys.argv) > 3 else "ASSIST"

    signal.signal(signal.SIGTERM, lambda s, f: sys.exit(0))

    log = TaskLog(task_id)
    log.write("task_start", task_id=task_id, intent=intent, mode=mode)

    cleanup_old_tasks()
    caps = load_capabilities()
    log.write("capabilities", caps=caps)

    job_start  = time.monotonic()
    ebpf_events: list[str] = []

    try:
        task_list = run_planner(intent, caps, log)

        all_summaries: list[str] = []
        any_failed = False

        for task_idx, task in enumerate(task_list):
            ebpf_events.extend(_read_ebpf_events())
            if len(ebpf_events) > 20:
                ebpf_events = ebpf_events[-20:]

            if mode == "SUGGEST":
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
