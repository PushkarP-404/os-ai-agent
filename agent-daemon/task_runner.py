#!/usr/bin/env python3
"""
task_runner.py — Elenchus: Dynamic Autonomous Task Execution Engine

Runs as a subprocess spawned by agent_daemon.py upon receiving an intent
from agent-cli via kernel syscall 548. Decoupled from the Netlink event loop.

Dynamically discovers the calling process's identity (UID, GID, home, cwd,
environment) from /proc/<caller_pid> so that actions are executed with the
actual caller's context rather than assuming hardcoded accounts or paths.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any
try:
    import pwd
except ImportError:
    pwd = None
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
import urllib.error

# ── OS Automation Engine & Intent Bus Bridge ─────────────────────────────────
from automation_engine import (
    ActionRegistry,
    AutomationEngine,
    IntentBus,
    IntentBusClient,
    IntentMessage,
    IntentResponse,
    LocalPolicyManager,
    get_automation_engine,
)

# ── Configuration ────────────────────────────────────────────────────────────
LLAMA_URL         = os.environ.get("LLAMA_URL",         "http://127.0.0.1:11434/completion")
AI_AGENT_LOG_DIR  = os.environ.get("AI_AGENT_LOG_DIR",  "/var/ai-agent")
TASKS_DIR         = os.path.join(AI_AGENT_LOG_DIR, "tasks")
CAPABILITIES_FILE = os.path.join(AI_AGENT_LOG_DIR, "capabilities.json")

LLM_TIMEOUT_S     = 60       # max seconds per LLM HTTP call
MAX_TASKS         = 5        # max sub-tasks Planner can generate
MAX_STEPS         = 3        # max Worker steps per sub-task
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


# ── Dynamic Caller Context Discovery ─────────────────────────────────────────
def get_caller_context(caller_pid: int | None = None) -> dict:
    """
    Dynamically discover caller's identity (user, uid, gid, home, cwd, env)
    from /proc/<caller_pid> without hardcoding any user or directory.
    """
    ctx = {
        "uid": os.getuid(),
        "gid": os.getgid(),
        "user": os.environ.get("USER", "root"),
        "home": os.environ.get("HOME", "/root"),
        "cwd": os.getcwd(),
        "env": dict(os.environ),
    }

    if caller_pid and os.path.exists(f"/proc/{caller_pid}"):
        try:
            stat_info = os.stat(f"/proc/{caller_pid}")
            ctx["uid"] = stat_info.st_uid
            ctx["gid"] = stat_info.st_gid
            try:
                user_record = pwd.getpwuid(ctx["uid"])
                ctx["user"] = user_record.pw_name
                ctx["home"] = user_record.pw_dir
            except Exception:
                pass

            # Caller current working directory
            try:
                caller_cwd = os.readlink(f"/proc/{caller_pid}/cwd")
                if os.path.isdir(caller_cwd):
                    ctx["cwd"] = caller_cwd
            except Exception:
                pass

            # Caller environment
            try:
                environ_path = f"/proc/{caller_pid}/environ"
                if os.path.exists(environ_path):
                    with open(environ_path, "rb") as f:
                        env_data = {}
                        for entry in f.read().split(b"\x00"):
                            if b"=" in entry:
                                k, v = entry.split(b"=", 1)
                                env_data[k.decode(errors="ignore")] = v.decode(errors="ignore")
                        if env_data:
                            ctx["env"].update(env_data)
                            if "HOME" in env_data:
                                ctx["home"] = env_data["HOME"]
                            if "USER" in env_data:
                                ctx["user"] = env_data["USER"]
            except Exception:
                pass
        except Exception:
            pass

    return ctx



# ── LLM helpers ──────────────────────────────────────────────────────────────
def extract_first_json_object(text: str) -> Any | None:
    """
    Extract the first well-formed, balanced JSON object ({...}) or array ([...]) from text.
    Properly ignores brackets inside string literals and escaped characters.
    Handles conversational prose, markdown code fences, and minor formatting imperfections.
    """
    if not text or not isinstance(text, str):
        return None

    cleaned = text.strip()
    if not cleaned:
        return None

    # Priority 1: Check for markdown code fences: ```json ... ``` or ``` ... ```
    fence_pattern = re.compile(r"```(?:json)?\s*([\s\S]*?)\s*```", re.IGNORECASE)
    for fence_match in fence_pattern.finditer(cleaned):
        block = fence_match.group(1).strip()
        try:
            return json.loads(block)
        except json.JSONDecodeError:
            res = _scan_balanced_json(block)
            if res is not None:
                return res

    # Priority 2: Direct parse if cleaned text is pure JSON container
    if (cleaned.startswith("{") and cleaned.endswith("}")) or (cleaned.startswith("[") and cleaned.endswith("]")):
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            pass

    # Priority 3: Scan text for balanced brackets aware of strings & escapes
    return _scan_balanced_json(cleaned)


def _scan_balanced_json(text: str) -> Any | None:
    """Scan string for the first balanced JSON container ({...} or [...]) respecting string escapes."""
    n = len(text)
    for i in range(n):
        ch = text[i]
        if ch not in ('{', '['):
            continue

        opening = ch
        closing = '}' if opening == '{' else ']'
        stack_depth = 0
        in_string = False
        escape = False

        for j in range(i, n):
            c = text[j]

            if in_string:
                if escape:
                    escape = False
                elif c == '\\':
                    escape = True
                elif c == '"':
                    in_string = False
                continue

            if c == '"':
                in_string = True
                escape = False
                continue

            if c == opening:
                stack_depth += 1
            elif c == closing:
                stack_depth -= 1
                if stack_depth == 0:
                    candidate = text[i:j + 1].strip()
                    try:
                        return json.loads(candidate)
                    except json.JSONDecodeError:
                        # Try removing trailing commas: , \s* [}\]]
                        fixed = re.sub(r",\s*([\]}])", r"\1", candidate)
                        try:
                            return json.loads(fixed)
                        except json.JSONDecodeError:
                            break
    return None


def sanitize_cmd(cmd: str) -> str:
    """Sanitize and normalize command string for safe execution."""
    if not cmd or not isinstance(cmd, str):
        return ""
    cmd = cmd.strip()
    # Strip surrounding quotes if whole command was wrapped in quotes
    if (cmd.startswith('"') and cmd.endswith('"')) or (cmd.startswith("'") and cmd.endswith("'")):
        cmd = cmd[1:-1].strip()
    # Remove dangerous unescaped control chars (e.g. null bytes)
    cmd = cmd.replace("\x00", "")
    # Normalize multiple whitespace characters
    cmd = " ".join(cmd.split())
    return cmd


def _llm_post(prompt: str, n_predict: int = 512) -> str:
    """
    Query local llama-server via HTTP POST.
    Hardened stop tokens: Removed \n\n and \n} which prematurely truncated JSON generation
    for small SLMs. Only explicit sentinel tokens are used.
    """
    payload = {
        "prompt": prompt,
        "n_predict": n_predict,
        "temperature": 0.1,
        "stop": ["<|im_end|>", "<|im_start|>"],
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
                return result.get("content", "").strip()
        except Exception as exc:
            if attempt < 1:
                time.sleep(1)
                continue
            return f"[LLM ERROR] {exc}"
    return "[LLM ERROR] Request failed"


def _extract_json_obj(text: str) -> dict | None:
    res = extract_first_json_object(text)
    return res if isinstance(res, dict) else None


def _extract_json_list(text: str) -> list | None:
    res = extract_first_json_object(text)
    return res if isinstance(res, list) else None


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


# ── Action extraction from LLM output ────────────────────────────────────────
def extract_action(raw: str) -> tuple[str, str]:
    """
    Extract shell command from model output or JSON.
    Returns (action, command_or_reason).
    """
    if "[LLM ERROR]" in raw or not raw.strip():
        return "unknown", raw

    # 1. Structured JSON using balanced extractor
    data = _extract_json_obj(raw)
    if data:
        if "cmd" in data:
            val = sanitize_cmd(str(data["cmd"]))
            if val and not val.startswith("[LLM"):
                return "execute", val
        action = data.get("action", "")
        if action in ("finish", "abort"):
            return action, data.get("reason", "Done")
        if action == "execute":
            args = data.get("args", [])
            if isinstance(args, list) and len(args) >= 2 and args[0] == "-c":
                return "execute", sanitize_cmd(args[1])
            raw_cmd = " ".join(str(a) for a in args) if isinstance(args, list) else str(args)
            return "execute", sanitize_cmd(raw_cmd)

    # 2. Regex for "cmd": "..."
    m = re.search(r'"cmd"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"?', raw)
    if m:
        candidate = sanitize_cmd(m.group(1))
        if candidate and not candidate.startswith("[LLM"):
            return "execute", candidate

    # 3. Markdown code block ```bash\n<cmd>\n```
    if "```" in raw:
        lines = [l.strip() for l in raw.split("```")[1].splitlines() if l.strip() and not l.strip().startswith("#")]
        if lines:
            return "execute", sanitize_cmd(lines[0])

    # 4. Command quoted in conversational text: `mkdir -p ...` or 'df -h'
    m = re.search(r'[`\'"]((?:mkdir|df|free|ps|ls|cat|rm|touch|apk|uptime|uname|whoami|hostname|ip|echo|cd)\s+[^`\'"]+)[`\'"]', raw)
    if m:
        return "execute", sanitize_cmd(m.group(1))

    # 5. Raw line starting with common shell command
    for line in raw.splitlines():
        line = line.strip().lstrip("-*123456789. ")
        if line.startswith(("#", "//", "/*")):
            continue
        parts = line.split()
        if parts and parts[0] in ("mkdir", "df", "free", "ps", "ip", "apk", "ls", "cat", "uptime", "uname", "whoami", "hostname", "touch", "rm", "echo", "curl", "wget", "grep", "find", "cd"):
            return "execute", line

    return "unknown", raw


# ── Worker ────────────────────────────────────────────────────────────────────
def run_worker_task(
    task_idx: int,
    task: str,
    caller_ctx: dict,
    log: TaskLog,
    job_start: float,
) -> tuple[bool, str]:
    """Execute a task via LLM reasoning using the dynamic caller context."""
    log.write("task_start", task_idx=task_idx, task=task)
    step = 0

    user_name = caller_ctx["user"]
    home_dir  = caller_ctx["home"]
    cwd       = caller_ctx["cwd"]
    history_ctx = ""

    while step < MAX_STEPS:
        elapsed = time.monotonic() - job_start
        if elapsed > TASK_MAX_TOTAL_S:
            msg = f"[ABORTED] Time limit exceeded."
            log.write("task_fail", task_idx=task_idx, reason="timeout", msg=msg)
            return False, msg

        worker_system = (
            "<|im_start|>system\n"
            f"You are a Linux command generator running commands for user '{user_name}' in '{cwd}'.\n"
            "Output ONLY JSON: {\"cmd\": \"<bash_command>\"}\n"
            "Rules:\n"
            "- Output a single bash command.\n"
            "- Do not use 'cd' alone if you want to create or inspect files; use relative or full paths.\n"
            "Examples:\n"
            "check disk -> {\"cmd\": \"df -h\"}\n"
            "check memory -> {\"cmd\": \"free -m\"}\n"
            "list files -> {\"cmd\": \"ls -la\"}\n"
            "<|im_end|>\n"
        )

        user_prompt = f"Task: {task}"
        if history_ctx:
            user_prompt += f"\nState: {history_ctx}"

        prompt = (
            worker_system
            + f"<|im_start|>user\n{user_prompt}<|im_end|>\n"
            + "<|im_start|>assistant\n{\"cmd\": \""
        )

        log.write("step", task_idx=task_idx, step=step, cwd=cwd)
        raw_output = _llm_post(prompt, n_predict=128)
        action, cmd_candidate = extract_action(raw_output)
        normalized_json = ""

        # If extraction failed, construct safe well-formed JSON object using json.dumps
        if action != "execute" or not cmd_candidate:
            cleaned_cmd = sanitize_cmd(raw_output)
            cleaned_cmd = re.sub(r'^(?:\{"cmd":\s*")|(?:^"cmd":\s*")', '', cleaned_cmd).strip()
            if cleaned_cmd and not cleaned_cmd.startswith("[LLM"):
                fallback_obj = {"cmd": cleaned_cmd}
                normalized_json = json.dumps(fallback_obj, ensure_ascii=False)
                action, cmd_candidate = extract_action(normalized_json)

        log.write("ai_raw", task_idx=task_idx, step=step, raw=raw_output,
                  normalized=normalized_json, cmd=cmd_candidate)

        if action == "finish":
            summary = cmd_candidate or "Task finished."
            log.write("task_finish", task_idx=task_idx, step=step, summary=summary)
            return True, summary

        if action == "abort":
            log.write("task_fail", task_idx=task_idx, step=step, reason=cmd_candidate)
            return False, f"[ABORTED] {cmd_candidate}"

        if action != "execute" or not cmd_candidate:
            log.write("json_error", task_idx=task_idx, step=step, raw=raw_output)
            step += 1
            continue

        shell_cmd = cmd_candidate.strip()
        safe, deny_reason = is_safe_command(shell_cmd)
        if not safe:
            log.write("security_block", task_idx=task_idx, step=step, cmd=shell_cmd, reason=deny_reason)
            return False, f"Blocked: {deny_reason}"

        # Handle 'cd' navigation dynamically: updates cwd for subsequent steps
        # instead of falsely terminating the task or losing directory in a subshell
        if shell_cmd.startswith("cd ") or shell_cmd == "cd":
            target = shell_cmd[3:].strip().strip("'\"") if shell_cmd.startswith("cd ") else home_dir
            if target.startswith("~"):
                target = os.path.join(home_dir, target[2:] if target.startswith("~/") else target[1:])
            elif not os.path.isabs(target):
                target = os.path.join(cwd, target)
            target = os.path.normpath(target)

            if os.path.isdir(target):
                cwd = target
                log.write("step_exec", task_idx=task_idx, step=step, cmd=shell_cmd)
                log.write("result", task_idx=task_idx, step=step, cmd=shell_cmd, returncode=0, stdout=f"Changed directory to {cwd}", stderr="")
                history_ctx = f"Current working directory changed to '{cwd}'."
                step += 1
                continue
            else:
                log.write("step_exec", task_idx=task_idx, step=step, cmd=shell_cmd)
                log.write("result", task_idx=task_idx, step=step, cmd=shell_cmd, returncode=1, stdout="", stderr=f"Directory '{target}' not found.")
                history_ctx = f"Failed to cd to '{target}': directory does not exist."
                step += 1
                continue

        log.write("step_exec", task_idx=task_idx, step=step, cmd=shell_cmd)

        # Prepare execution environment
        exec_env = dict(os.environ)
        exec_env.update(caller_ctx["env"])
        exec_env["HOME"] = home_dir
        exec_env["USER"] = user_name

        # If caller was non-root, run with caller's UID/GID for correct permissions
        preexec = None
        if caller_ctx["uid"] != 0:
            def _drop_privileges():
                os.setgid(caller_ctx["gid"])
                os.setuid(caller_ctx["uid"])
            preexec = _drop_privileges

        try:
            result = subprocess.run(
                ["sh", "-c", shell_cmd],
                capture_output=True,
                text=True,
                timeout=CMD_TIMEOUT_S,
                cwd=cwd if os.path.isdir(cwd) else home_dir,
                env=exec_env,
                preexec_fn=preexec,
            )
            stdout = result.stdout[:2000]
            stderr = result.stderr[:500]
            rc     = result.returncode

            log.write(
                "result",
                task_idx=task_idx,
                step=step,
                cmd=shell_cmd,
                returncode=rc,
                stdout=stdout,
                stderr=stderr,
            )

            if rc == 0:
                summary = stdout.strip() if stdout.strip() else f"Command '{shell_cmd}' completed successfully (exit 0)."
                log.write("task_finish", task_idx=task_idx, step=step, summary=summary[:300])
                return True, summary
            else:
                history_ctx = f"Command '{shell_cmd}' failed with exit {rc}: {stderr.strip() or stdout.strip()}"

        except subprocess.TimeoutExpired:
            log.write("result", task_idx=task_idx, step=step, cmd=shell_cmd,
                      returncode=-1, stdout="", stderr=f"[TIMEOUT] Exceeded {CMD_TIMEOUT_S}s.")
            history_ctx = f"Command '{shell_cmd}' timed out."
        except Exception as exc:
            log.write("result", task_idx=task_idx, step=step, cmd=shell_cmd,
                      returncode=-1, stdout="", stderr=str(exc))
            history_ctx = f"Command execution error: {exc}"

        step += 1

    msg = f"Task failed to complete within step limit on: {task}"
    log.write("task_fail", task_idx=task_idx, reason="step_limit_reached", msg=msg)
    return False, msg


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
def legacy_main():
    if len(sys.argv) < 3:
        print("Usage: task_runner.py <task_id> <intent> [mode] [caller_pid]", file=sys.stderr)
        sys.exit(1)

    task_id    = sys.argv[1]
    intent     = sys.argv[2]
    mode       = sys.argv[3] if len(sys.argv) > 3 else "ASSIST"
    caller_pid = int(sys.argv[4]) if len(sys.argv) > 4 and sys.argv[4].isdigit() else None
    if caller_pid is None:
        caller_pid_env = os.environ.get("CALLER_PID")
        if caller_pid_env and caller_pid_env.isdigit():
            caller_pid = int(caller_pid_env)

    signal.signal(signal.SIGTERM, lambda s, f: sys.exit(0))

    log = TaskLog(task_id)
    log.write("task_start", task_id=task_id, intent=intent, mode=mode)

    cleanup_old_tasks()
    caps = load_capabilities()

    # Discover caller context dynamically from /proc
    caller_ctx = get_caller_context(caller_pid)
    log.write("capabilities", caps=caps, caller_user=caller_ctx["user"], caller_home=caller_ctx["home"])

    job_start = time.monotonic()

    try:
        log.write("planner_start", intent=intent)

        # Single intent or sub-task execution directly driven by AI model
        task_list = [intent]
        log.write("plan", tasks=task_list, count=1)

        all_summaries: list[str] = []
        any_failed = False

        for task_idx, task in enumerate(task_list):
            if mode == "SUGGEST":
                log.write("suggest_skip", task_idx=task_idx, task=task,
                          note="SUGGEST mode: not executed")
                all_summaries.append(f"[WOULD DO] {task}")
                continue

            success, summary = run_worker_task(
                task_idx=task_idx,
                task=task,
                caller_ctx=caller_ctx,
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


def main():
    """Run the verified harness when explicitly enabled during rollout.

    Harness V2 is the default execution path. Set HARNESS_V2=0 only as a
    temporary rollback while investigating a deployment issue with the legacy
    executor.
    """
    if os.environ.get("HARNESS_V2", "1") != "1":
        legacy_main()
        return

    if len(sys.argv) < 3:
        print("Usage: task_runner.py <task_id> <intent> [mode] [caller_pid]", file=sys.stderr)
        sys.exit(1)

    from harness_v2 import Harness, JobStatus, JobStore

    task_id = sys.argv[1]
    intent = sys.argv[2]
    mode = sys.argv[3] if len(sys.argv) > 3 else "ASSIST"
    caller_pid = int(sys.argv[4]) if len(sys.argv) > 4 and sys.argv[4].isdigit() else None
    log = TaskLog(task_id)
    caller = get_caller_context(caller_pid)
    store = JobStore(AI_AGENT_LOG_DIR, task_id, log.write)

    try:
        store.create(intent, mode, caller)
        harness = Harness(store, caller, mode, lambda prompt: _llm_post(prompt, n_predict=600))
        harness.run(intent)
    except KeyboardInterrupt:
        store.set_status(JobStatus.CANCELLED, reason="Task cancelled")
        store.event("done", status="CANCELLED", task_count=0, elapsed_s=0, summaries=["Task cancelled"])
    except Exception as exc:
        store.event("task_fail", task_idx=0, reason="runner_exception", msg=str(exc))
        store.event("done", status="FAILED", task_count=0, elapsed_s=0, summaries=[str(exc)])
    finally:
        log.close()


if __name__ == "__main__":
    main()
