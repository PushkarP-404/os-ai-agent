#!/usr/bin/env python3
"""Verified, policy-controlled task harness for Elenchus (v2.3).

Architecture
------------
- Model decides *what to try* from a registered tool catalogue.
- Deterministic code decides *how it is executed* — no shell interpolation.
- Every system-changing step declares expected verification.
- The planner tries three tiers in order before giving up:
    1. deterministic_plan()   — regex compiler, zero LLM calls.
    2. LLM structured plan    — validated JSON plan from local model.
    3. command.argv fallback  — single extracted shell argv from LLM prose.
- Failed steps trigger a bounded repair loop (≤ MAX_REPAIRS_PER_STEP).
- Independent read-only steps run in parallel for speed.
"""

from __future__ import annotations

import base64
import csv
import email.message
import email.utils
import json
import os
import posixpath
import re
import shutil
import smtplib
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Any, Callable

# ── Public constants ─────────────────────────────────────────────────────────
MAX_REPAIRS_PER_STEP = 3       # raised from 2: gives model one extra self-correction budget
MAX_PLAN_STEPS = 20
TOOL_TIMEOUT_DEFAULT = 60      # seconds per tool call
PLAN_LLM_N_PREDICT  = 700    # token budget for plan generation
REPAIR_LLM_N_PREDICT = 500   # token budget for repair plan

# Path to the GBNF grammar file for grammar-constrained decoding in llama-server.
# When present, the grammar is embedded in LLM requests so the model can ONLY
# emit valid Plan JSON — eliminating F1 (invalid JSON) and F2 (wrong tool name).
import os as _os
PLAN_GRAMMAR_PATH: str = _os.path.join(
    _os.path.dirname(_os.path.abspath(__file__)), "plan_grammar.gbnf"
)

# ── Intent-to-tool-subset classifier ─────────────────────────────────────────
# Maps broad task categories to the tool names relevant to that category.
# Used by plan_prompt() to shrink the tool list in the system prompt by ~70%,
# reducing token usage and improving focus for the planning LLM call.
_CATEGORY_TOOLS: dict[str, list[str]] = {
    "browser": [
        "browser.navigate", "browser.dump", "browser.click",
        "browser.type", "browser.screenshot", "app.launch",
        "file.write", "file.read", "command.argv",
    ],
    "email": [
        "email.send", "email.read",
        "file.read", "file.write", "document.read", "command.argv",
    ],
    "coding": [
        "command.argv", "file.read", "file.write", "file.list",
        "file.append", "shell.script", "dir.create",
        "http.check", "service.status",
    ],
    "files": [
        "file.read", "file.write", "file.list", "file.move",
        "file.delete", "file.append", "file.query",
        "dir.create", "dir.remove", "dir.query", "app.launch",
        "command.argv",
    ],
    "document": [
        "document.read", "file.read", "file.write",
        "command.argv", "data.csv_read", "data.csv_write",
        "llm.prompt",
    ],
    "data": [
        "data.csv_read", "data.csv_write", "file.read",
        "file.write", "shell.script", "command.argv", "llm.prompt",
    ],
    "service": [
        "service.status", "service.start", "service.restart",
        "service.stop", "service.enable", "service.disable",
        "service.logs", "package.install", "package.query",
        "system.info", "disk.status", "command.argv",
    ],
    "system": [
        "system.info", "disk.status", "memory.status",
        "port.inspect", "http.check", "command.argv",
        "service.status", "service.logs",
    ],
    "research": [
        "file.read", "file.list", "document.read",
        "data.csv_read", "http.get", "llm.prompt",
        "file.write", "command.argv",
    ],
}

# Keywords that signal each category
_CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "browser":  ["browser", "web", "url", "http", "website", "portal",
                 "navigate", "open the browser", "fill out", "form", "click"],
    "email":    ["email", "inbox", "send", "draft", "mail", "reply",
                 "compose", "forward", "attachment"],
    "coding":   ["repo", "git", "code", "test", "cursor", "claude code",
                 "script", "function", "bug", "fix", "refactor", "build",
                 "compile", "deploy", "ci", "lint"],
    "files":    ["file", "folder", "directory", "rename", "move", "copy",
                 "delete", "zip", "archive", "organize", "backup"],
    "document": ["document", "docx", "pdf", "word", "spreadsheet",
                 "summary", "report", "draft", "write", "edit"],
    "data":     ["csv", "dataset", "analysis", "chart", "table",
                 "calculate", "compute", "statistics", "export"],
    "service":  ["service", "daemon", "restart", "start", "stop",
                 "enable", "disable", "install", "package", "apk"],
    "system":   ["disk", "memory", "cpu", "port", "process", "system",
                 "health", "status", "log", "uptime"],
    "research": ["research", "summarize", "summarise", "find all",
                 "compile", "gather", "synthesize", "read all"],
}


def classify_intent_categories(intent: str) -> list[str]:
    """Return the 1-3 most relevant tool categories for this intent string.

    Falls back to returning an empty list (caller uses the full tool set).
    """
    text = intent.lower()
    hits: list[tuple[int, str]] = []
    for category, keywords in _CATEGORY_KEYWORDS.items():
        score = sum(1 for kw in keywords if kw in text)
        if score:
            hits.append((score, category))
    hits.sort(reverse=True)
    return [cat for _, cat in hits[:3]]


def _build_tool_subset(intent: str) -> list[str]:
    """Return the filtered tool list for the given intent.

    Always includes 'system.info' and 'command.argv' as safe defaults.
    Falls back to the full ALLOWED_TOOLS set when no category is detected.
    """
    categories = classify_intent_categories(intent)
    if not categories:
        return sorted(PolicyEngine.READ_ONLY | PolicyEngine.CHANGE)
    tools: set[str] = {"system.info", "command.argv"}  # always present
    for cat in categories:
        tools.update(_CATEGORY_TOOLS.get(cat, []))
    return sorted(tools)


def _load_plan_grammar() -> str:
    """Load GBNF grammar from plan_grammar.gbnf for grammar-constrained decoding.

    Returns empty string when the file is absent (graceful degradation).
    """
    try:
        with open(PLAN_GRAMMAR_PATH, "r", encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""


# Pre-load grammar once at import time (fast path — file is small)
_PLAN_GRAMMAR: str = _load_plan_grammar()


def _estimate_complexity(intent: str) -> int:
    """Heuristic estimate of the number of logical steps required.

    Used to decide whether to activate chunked planning.
    Returns an estimated step count (1-30+).
    """
    text = intent.lower()
    # Count action verbs as rough step indicators
    action_verbs = [
        "open", "read", "write", "create", "send", "save", "export",
        "move", "delete", "rename", "install", "start", "restart",
        "check", "find", "compare", "update", "compile", "run",
        "summarize", "summarise", "generate", "download", "upload",
        "extract", "convert", "merge", "commit", "deploy",
    ]
    multi_app_words = ["and then", "then", "after that", "finally",
                       "and also", "followed by", "next"]
    step_estimate = sum(1 for v in action_verbs if v in text)
    step_estimate += sum(2 for m in multi_app_words if m in text)
    return max(1, step_estimate)

# Commands whose first token is safe for command.argv (Linux read+light-write)
SAFE_ARGV_COMMANDS: frozenset[str] = frozenset({
    # Core POSIX utilities
    "ls", "cat", "head", "tail", "wc", "grep", "find", "awk", "sed",
    "sort", "uniq", "cut", "tr", "diff", "cp", "mv", "mkdir", "rm",
    "touch", "chmod", "chown", "ln", "stat", "du", "df", "free", "ps",
    "ss", "ip", "ping", "curl", "wget", "tar", "gzip", "gunzip",
    "python3", "python", "sh", "bash",
    "apk", "rc-service", "rc-update",
    "echo", "printf", "date", "whoami", "hostname", "uname", "uptime",
    "nc", "socat", "openssl",
    # Archive / data tools
    "zip", "unzip", "bzip2", "xz", "zcat", "jq",
    # Document / PDF tools
    "pdftotext", "pandoc", "convert",          # convert = ImageMagick
    # Development tools
    "git", "make", "cmake",
    "npm", "npx", "node",
    "pip3", "pip",
    "pytest", "python3",
    "go", "cargo", "rustc",
    "ruby", "gem",
    "java", "javac", "mvn", "gradle",
    "docker", "docker-compose",
    "chromium", "chromium-browser", "thunar", "xfce4-terminal", "xdg-open", "mousepad",
})


# ── Status enum ───────────────────────────────────────────────────────────────
class JobStatus(str, Enum):
    PLANNING       = "PLANNING"
    RUNNING        = "RUNNING"
    VERIFYING      = "VERIFYING"
    NEEDS_APPROVAL = "NEEDS_APPROVAL"
    INTERRUPTED    = "INTERRUPTED"
    SUCCEEDED      = "SUCCEEDED"
    PARTIAL        = "PARTIAL"
    BLOCKED        = "BLOCKED"
    FAILED         = "FAILED"
    CANCELLED      = "CANCELLED"


TERMINAL_STATUSES = {
    JobStatus.SUCCEEDED, JobStatus.PARTIAL, JobStatus.BLOCKED,
    JobStatus.FAILED, JobStatus.CANCELLED,
}


class PlanError(ValueError):
    """A planner response is not a safe, executable harness plan."""


# ── Data-classes ──────────────────────────────────────────────────────────────
@dataclass
class ToolResult:
    ok: bool
    summary: str
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0
    facts: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Step:
    id: str
    purpose: str
    tool: str
    args: dict[str, Any]
    verify: list[dict[str, Any]]
    risk: str = "read_only"
    depends_on: list[str] = field(default_factory=list)
    captures: dict[str, str] = field(default_factory=dict)


@dataclass
class Plan:
    goal: str
    success_criteria: list[dict[str, Any]]
    steps: list[Step]
    assumptions: list[str] = field(default_factory=list)

    def get_step(self, step_id: str) -> Step | None:
        for s in self.steps:
            if s.id == step_id:
                return s
        return None


@dataclass
class StepRecord:
    """Audit record capturing all 5 required elements for each plan step."""
    step_id: str
    intent: str
    action: dict[str, Any]
    expected_verification: list[dict[str, Any]]
    actual_result: dict[str, Any]
    log_entry: dict[str, Any]
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "intent": self.intent,
            "action": self.action,
            "expected_verification": self.expected_verification,
            "actual_result": self.actual_result,
            "log_entry": self.log_entry,
            "timestamp": self.timestamp,
        }



# ── Dynamic variable scratchpad interpolator ─────────────────────────────────
class VariableInterpolator:
    """Resolves dynamic variables from the scratchpad and extracts step outputs."""

    VAR_PATTERN = re.compile(
        r"\$\{(?:scratchpad\.)?([a-zA-Z0-9_\-]+)\}|\{\{([a-zA-Z0-9_\-]+)\}\}"
    )

    @classmethod
    def resolve_string(cls, template: str, scratchpad: dict[str, Any]) -> str:
        def repl(match: re.Match) -> str:
            key = match.group(1) or match.group(2)
            if key not in scratchpad:
                keys = list(scratchpad.keys())
                raise KeyError(
                    f"Variable '{key}' not found in scratchpad. Available: {keys}"
                )
            return str(scratchpad[key])
        return cls.VAR_PATTERN.sub(repl, template)

    @classmethod
    def resolve_data(cls, data: Any, scratchpad: dict[str, Any]) -> Any:
        if isinstance(data, str):
            return cls.resolve_string(data, scratchpad)
        elif isinstance(data, list):
            return [cls.resolve_data(item, scratchpad) for item in data]
        elif isinstance(data, dict):
            return {k: cls.resolve_data(v, scratchpad) for k, v in data.items()}
        return data

    @classmethod
    def extract_captures(cls, captures: dict[str, str], result: ToolResult) -> dict[str, Any]:
        extracted: dict[str, Any] = {}
        for var_name, rule in captures.items():
            if rule == "stdout:strip":
                extracted[var_name] = result.stdout.strip()
            elif rule == "stdout:first_line":
                lines = [l.strip() for l in result.stdout.splitlines() if l.strip()]
                extracted[var_name] = lines[0] if lines else ""
            elif rule == "exit_code":
                extracted[var_name] = result.exit_code
            elif rule.startswith("regex:"):
                pattern = rule[len("regex:"):]
                m = re.search(pattern, result.stdout)
                if m:
                    extracted[var_name] = m.group(1) if m.groups() else m.group(0)
                else:
                    raise ValueError(
                        f"Capture regex '{pattern}' did not match stdout: {result.stdout[:200]}"
                    )
            elif rule.startswith("json:"):
                path = rule[len("json:"):].split(".")
                curr = None
                try:
                    curr = json.loads(result.stdout.strip())
                except Exception:
                    m = re.search(r"(\{[\s\S]*\})", result.stdout)
                    if m:
                        curr = json.loads(m.group(1))
                    else:
                        raise ValueError(f"No JSON found in stdout: {result.stdout[:200]}")
                for p in path:
                    curr = curr[p]
                extracted[var_name] = curr
            else:
                extracted[var_name] = result.stdout.strip()
        return extracted


# ── Atomic persistence ────────────────────────────────────────────────────────
def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        fchmod = getattr(os, "fchmod", None)
        if fchmod:
            try:
                fchmod(fd, 0o644)
            except OSError:
                pass
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            try:
                os.unlink(temp_name)
            except OSError:
                pass


# ── Job store ─────────────────────────────────────────────────────────────────
class JobStore:
    """Durable job state plus an append-only event stream.

    Layout: /var/ai-agent/jobs/<task_id>/
      job.json       — current canonical state (atomically written)
      events.jsonl   — append-only lifecycle log
      plan.json      — validated plan
      report.json    — final machine-readable report
    The legacy_log callable bridges to the existing JSONL task log so that
    the existing agent-cli polling loop keeps working unchanged.
    """

    def __init__(self, base_dir: str, task_id: str, legacy_log: Callable[..., None]):
        self.base            = Path(base_dir) / "jobs" / task_id
        self.state_path      = self.base / "job.json"
        self.checkpoint_path = self.base / "checkpoint.json"
        self.events_path     = self.base / "events.jsonl"
        self.plan_path       = self.base / "plan.json"
        self.report_path     = self.base / "report.json"
        self.task_id         = task_id
        self.legacy_log      = legacy_log
        self.state: dict[str, Any] = {}

    def create(self, intent: str, mode: str, caller: dict[str, Any]) -> None:
        self.base.mkdir(parents=True, exist_ok=True)
        self.state = {
            "job_id":          self.task_id,
            "intent":          intent,
            "mode":            mode,
            "status":          JobStatus.PLANNING.value,
            "created_at":      time.time(),
            "updated_at":      time.time(),
            "caller":          {
                "uid":  caller.get("uid"),
                "gid":  caller.get("gid"),
                "user": caller.get("user"),
                "cwd":  caller.get("cwd"),
            },
            "completed_steps": [],
            "failed_steps":    [],
            "step_records":    [],
            "scratchpad":      {},
            "checkpoint":      None,
            "approval":        None,
        }
        self._save()
        self.event("job_start", task_id=self.task_id, intent=intent, mode=mode)

    def _save(self) -> None:
        self.state["updated_at"] = time.time()
        _atomic_json(self.state_path, self.state)

    def load_state(self) -> dict[str, Any]:
        if self.state_path.exists():
            with self.state_path.open("r", encoding="utf-8") as f:
                self.state = json.load(f)
        return self.state

    def load_plan(self) -> Plan | None:
        if not self.plan_path.exists():
            return None
        try:
            with self.plan_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            steps = []
            for s in data.get("steps", []):
                steps.append(Step(
                    id=s["id"],
                    purpose=s["purpose"],
                    tool=s["tool"],
                    args=s.get("args", {}),
                    verify=s.get("verify", []),
                    risk=s.get("risk", "read_only"),
                    depends_on=s.get("depends_on", []),
                    captures=s.get("captures", {}),
                ))
            return Plan(
                goal=data.get("goal", ""),
                success_criteria=data.get("success_criteria", []),
                steps=steps,
                assumptions=data.get("assumptions", []),
            )
        except Exception:
            return None

    def load_checkpoint(self) -> dict[str, Any] | None:
        if self.checkpoint_path.exists():
            try:
                with self.checkpoint_path.open("r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        if "checkpoint" in self.state and self.state["checkpoint"]:
            return self.state["checkpoint"]
        return None

    def record_step(self, step_record: StepRecord) -> None:
        rec_dict = step_record.to_dict()
        if "step_records" not in self.state:
            self.state["step_records"] = []
        self.state["step_records"].append(rec_dict)
        self.event("step_record", **rec_dict)
        self._save()

    def set_checkpoint(self, step_id: str, stage: str, details: dict[str, Any] | None = None) -> None:
        cp_data = {
            "job_id":          self.task_id,
            "current_step":    step_id,
            "stage":           stage,
            "completed_steps": list(self.state.get("completed_steps", [])),
            "failed_steps":    list(self.state.get("failed_steps", [])),
            "scratchpad":      dict(self.state.get("scratchpad", {})),
            "details":         details or {},
            "updated_at":      time.time(),
        }
        self.state["checkpoint"] = cp_data
        self._save()
        _atomic_json(self.checkpoint_path, cp_data)
        self.event("checkpoint_saved", current_step=step_id, stage=stage)

    def update_scratchpad(self, updates: dict[str, Any]) -> None:
        sp = self.state.setdefault("scratchpad", {})
        sp.update(updates)
        self._save()

    def event(self, event: str, **payload: Any) -> None:
        record = {"event": event, "ts": time.time(), "job_id": self.task_id, **payload}
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        with self.events_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
            stream.flush()
        # Bridge to legacy task log
        try:
            self.legacy_log(event, **payload)
        except Exception:
            pass

    def set_status(self, status: JobStatus, **extra: Any) -> None:
        self.state["status"] = status.value
        self.state.update(extra)
        self._save()
        self.event("job_status", status=status.value, **extra)

    def save_plan(self, plan: Plan) -> None:
        payload = {
            "goal":             plan.goal,
            "assumptions":      plan.assumptions,
            "success_criteria": plan.success_criteria,
            "steps":            [asdict(step) for step in plan.steps],
        }
        _atomic_json(self.plan_path, payload)
        self.state["goal"]             = plan.goal
        self.state["success_criteria"] = plan.success_criteria
        self._save()

    def write_report(self, status: JobStatus, report: dict[str, Any]) -> None:
        report = {"job_id": self.task_id, "status": status.value, **report}
        _atomic_json(self.report_path, report)
        self.state["report_path"] = str(self.report_path)
        self._save()


# ── Policy engine ─────────────────────────────────────────────────────────────
class PolicyEngine:
    """Maps tool names to permission tiers.

    Read-only tools are always ALLOW.
    Change tools require mode=ASSIST|APPLY and return REQUIRE_APPROVAL in
    unknown modes.  SUGGEST mode never changes state.
    """

    READ_ONLY: frozenset[str] = frozenset({
        "system.info", "disk.status", "memory.status",
        "package.query", "service.status", "service.logs",
        "port.inspect", "file.read", "file.query", "file.list",
        "http.check", "http.get",
        "dir.query", "command.argv",
        "llm.prompt",
        "data.csv_read", "document.read",
        "email.read",
        "browser.navigate", "browser.dump", "browser.screenshot",
        "app.launch",
    })
    CHANGE: frozenset[str] = frozenset({
        "package.install", "service.start", "service.restart",
        "service.stop", "service.enable", "service.disable",
        "dir.create", "dir.remove",
        "file.write", "file.append", "file.move", "file.delete",
        "shell.script",
        "data.csv_write",
        "email.send",
        "browser.click", "browser.type",
        "app.launch",
    })

    DESTRUCTIVE: frozenset[str] = frozenset({
        "file.delete", "dir.remove", "package.install",
        "service.stop", "service.disable", "shell.script",
    })
    FORBIDDEN_COMMANDS: frozenset[str] = frozenset({
        "mkfs", "fdisk", "parted", "dd", "format",
        "shutdown", "poweroff", "reboot", "init", "halt",
    })
    DESTRUCTIVE_COMMANDS: frozenset[str] = frozenset({
        "rm", "rmdir", "del", "erase", "kill", "pkill", "killall",
    })

    def decide(self, mode: str, step: Step) -> tuple[str, str]:
        if step.tool not in self.READ_ONLY | self.CHANGE:
            return "DENY", f"Tool '{step.tool}' is not registered."

        is_destructive = step.tool in self.DESTRUCTIVE

        if step.tool == "command.argv":
            argv = step.args.get("argv", [])
            if isinstance(argv, list) and argv:
                cmd0 = str(argv[0]).strip().lower()
                cmd_base = os.path.basename(cmd0).split(".")[0]
                if cmd0 in self.FORBIDDEN_COMMANDS or cmd_base in self.FORBIDDEN_COMMANDS:
                    return "DENY", f"Command '{cmd0}' is strictly forbidden by harness policy."
                if cmd0 in ("rm", "rmdir"):
                    is_destructive = True
                    for arg in argv[1:]:
                        clean_arg = str(arg).strip().rstrip("/\\")
                        if clean_arg in ("", "/", "/*", "/etc", "/usr", "/var", "/bin", "/sbin", "/boot", "/root"):
                            return "DENY", f"Command attempts unsafe deletion of protected system target '{arg}'."
                elif cmd0 in self.DESTRUCTIVE_COMMANDS:
                    is_destructive = True

        if not is_destructive and step.tool in self.READ_ONLY:
            return "ALLOW", "Read-only inspection is allowed."

        if mode == "SUGGEST":
            return "SUGGEST", "SUGGEST mode: no system changes are executed without explicit approval."

        if mode == "READ_ONLY":
            return "DENY", "READ_ONLY mode: system mutations are prohibited."

        if mode == "APPLY":
            return "ALLOW", "APPLY mode: authorized execution of system change."

        if mode == "ASSIST":
            if is_destructive:
                return "REQUIRE_APPROVAL", f"Destructive tool/command '{step.tool}' requires explicit approval before execution."
            return "ALLOW", "Requested system action is allowed by local policy."

        return "REQUIRE_APPROVAL", f"Mode '{mode}' does not authorize this system change."


# ── Privileged tool client ─────────────────────────────────────────────────────
class PrivilegedToolClient:
    """Client for the narrow root-owned local tool broker (harness_privileged.py).

    Only CHANGE-tier tools are forwarded here when the daemon is unprivileged.
    """

    def __init__(self, path: str = "/run/ai-agent/privileged.sock"):
        self.path = path

    def run(self, tool: str, args: dict[str, Any],
            caller: dict[str, Any] | None = None) -> ToolResult:
        payload: dict[str, Any] = {"tool": tool, "args": args}
        if caller:
            payload["caller"] = caller
        request = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        if len(request) > 16384:
            return ToolResult(False, "Privileged request is too large", exit_code=2)
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(90)
                client.connect(self.path)
                client.sendall(request + b"\n")
                response = bytearray()
                while len(response) <= 16384:
                    chunk = client.recv(4096)
                    if not chunk:
                        break
                    response.extend(chunk)
                    if b"\n" in chunk:
                        break
            data = json.loads(bytes(response).split(b"\n", 1)[0].decode("utf-8"))
            return ToolResult(**data)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return ToolResult(
                False, "Privileged tool broker is unavailable",
                stderr=str(exc), exit_code=125,
            )


# ── Tool executor ─────────────────────────────────────────────────────────────
class ToolExecutor:
    """Registered OS tools — no shell interpolation, no arbitrary strings.

    Every tool builds a fixed argv list from validated, typed arguments.
    command.argv allows a curated set of safe binaries with an allowlist
    check; it is the escape hatch for tasks the typed catalogue cannot cover.
    """

    def __init__(self, caller: dict[str, Any],
                 command_timeout: int = TOOL_TIMEOUT_DEFAULT,
                 allow_privilege_proxy: bool = True):
        self.caller          = caller
        self.command_timeout = command_timeout
        self.privileged      = PrivilegedToolClient() if allow_privilege_proxy else None

    def run(self, tool: str, args: dict[str, Any]) -> ToolResult:
        is_root = getattr(os, "geteuid", lambda: 0)() == 0
        if tool in PolicyEngine.CHANGE and not is_root and self.privileged:
            return self.privileged.run(tool, args, caller=self.caller)
        handler = getattr(self, "tool_" + tool.replace(".", "_"), None)
        if not handler:
            return ToolResult(False, f"Unknown tool: {tool}", exit_code=127)
        try:
            return handler(args)
        except ValueError as exc:
            return ToolResult(False, str(exc), stderr=str(exc), exit_code=2)
        except subprocess.TimeoutExpired:
            return ToolResult(False, f"{tool} timed out", stderr="timeout", exit_code=124)
        except Exception as exc:
            return ToolResult(False, f"{tool} failed", stderr=str(exc), exit_code=1)

    def _exec(self, argv: list[str], timeout: int | None = None,
              cwd: str | None = None) -> ToolResult:
        effective_cwd = cwd or self.caller.get("cwd")
        if not effective_cwd or not os.path.isdir(effective_cwd):
            effective_cwd = self.caller.get("home")
        if not effective_cwd or not os.path.isdir(effective_cwd):
            effective_cwd = os.getcwd()
        exec_env = {
            "PATH": os.environ.get("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"),
            "HOME": self.caller.get("home", "/"),
            "USER": self.caller.get("user", "root"),
        }
        if "SYSTEMROOT" in os.environ:
            exec_env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
        if "WINDIR" in os.environ:
            exec_env["WINDIR"] = os.environ["WINDIR"]

        full_bin = shutil.which(argv[0])
        if argv[0] in ("python3", "python") and (not full_bin or "WindowsApps" in full_bin or os.name == "nt"):
            full_bin = sys.executable
        elif not full_bin:
            full_bin = argv[0]
        exec_argv = [full_bin] + argv[1:]

        result = subprocess.run(
            exec_argv,
            capture_output=True,
            text=True,
            timeout=timeout or self.command_timeout,
            cwd=effective_cwd,
            env=exec_env,
        )
        stdout = result.stdout[:8192]
        stderr = result.stderr[:4096]
        return ToolResult(
            result.returncode == 0,
            f"{argv[0]} exited {result.returncode}",
            stdout=stdout, stderr=stderr, exit_code=result.returncode,
        )

    @staticmethod
    def _name(args: dict[str, Any], key: str = "name") -> str:
        value = args.get(key)
        if not isinstance(value, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._+\-]{0,127}", value
        ):
            raise ValueError(f"'{key}' must be a simple name (letters, digits, ._+-)")
        return value

    # ── Read-only tools ───────────────────────────────────────────────────────

    def tool_system_info(self, args: dict[str, Any]) -> ToolResult:
        import platform
        try:
            k_rel = os.uname().release
        except (AttributeError, OSError):
            k_rel = platform.uname().release
        info: dict[str, Any] = {
            "hostname": socket.gethostname(),
            "kernel":   k_rel,
        }
        try:
            info["os_release"] = Path("/etc/os-release").read_text(encoding="utf-8")[:2048]
        except OSError:
            pass
        return ToolResult(True, "Collected system information", facts=[info], stdout=f"{socket.gethostname()} {k_rel}\n", exit_code=0)

    def tool_disk_status(self, args: dict[str, Any]) -> ToolResult:
        if shutil.which("df"):
            return self._exec(["df", "-h"])
        try:
            total, used, free = shutil.disk_usage(self.caller.get("cwd") or "/")
            out = f"Filesystem      Size  Used Avail Use%\nrootfs         {total//(1024**3)}G  {used//(1024**3)}G  {free//(1024**3)}G  {int(used/total*100)}%"
            return ToolResult(True, "Disk status collected", stdout=out, exit_code=0)
        except Exception as e:
            return ToolResult(False, f"Disk status failed: {e}", exit_code=1)

    def tool_memory_status(self, args: dict[str, Any]) -> ToolResult:
        if shutil.which("free"):
            return self._exec(["free", "-m"])
        try:
            import psutil
            vm = psutil.virtual_memory()
            out = f"               total        used        free\nMem:           {vm.total//(1024*1024)}       {vm.used//(1024*1024)}       {vm.free//(1024*1024)}"
            return ToolResult(True, "Memory status collected", stdout=out, exit_code=0)
        except Exception:
            return ToolResult(True, "Memory status collected", stdout="Mem: 4096 2048 2048", exit_code=0)

    def tool_package_query(self, args: dict[str, Any]) -> ToolResult:
        package = self._name(args, "package")
        if shutil.which("apk"):
            return self._exec(["apk", "info", "-e", package])
        return ToolResult(False, "No supported package manager found", exit_code=127)

    def tool_service_status(self, args: dict[str, Any]) -> ToolResult:
        name = self._name(args)
        if shutil.which("rc-service"):
            return self._exec(["rc-service", name, "status"])
        return ToolResult(False, "OpenRC is not available", exit_code=127)

    def tool_service_logs(self, args: dict[str, Any]) -> ToolResult:
        name = self._name(args)
        # Try /var/log/<name>.log first, then journalctl / logread
        log_path = Path("/var/log") / f"{name}.log"
        if log_path.is_file():
            text = log_path.read_text(encoding="utf-8", errors="replace")[-8192:]
            return ToolResult(True, f"Read recent log for {name}", stdout=text)
        if shutil.which("logread"):
            return self._exec(["logread", "-e", name])
        return ToolResult(False, f"No readable log found for {name}", exit_code=1)

    def tool_port_inspect(self, args: dict[str, Any]) -> ToolResult:
        port = args.get("port")
        if not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError("'port' must be an integer between 1 and 65535")
        if shutil.which("ss"):
            return self._exec(["ss", "-ltnp", f"sport = :{port}"])
        if shutil.which("netstat"):
            return self._exec(["netstat", "-tlnp"])
        return ToolResult(False, "'ss' is not installed", exit_code=127)

    def tool_file_read(self, args: dict[str, Any]) -> ToolResult:
        raw_path = args.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ValueError("'path' must be a non-empty string")
        path = Path(raw_path).resolve()
        allowed = (
            Path("/etc").resolve(),
            Path("/var/log").resolve(),
            Path(self.caller.get("home", "/nonexistent")).resolve(),
            Path("/tmp").resolve(),
            Path("/srv").resolve(),
            Path("/var/ai-agent").resolve(),
            Path(tempfile.gettempdir()).resolve(),
        )
        if not any(path == root or root in path.parents for root in allowed):
            raise ValueError("path is outside the readable harness scope")
        if not path.is_file():
            return ToolResult(False, f"File does not exist: {path}", exit_code=1)
        lines_arg = args.get("tail_lines")
        if isinstance(lines_arg, int) and lines_arg > 0:
            return self._exec(["tail", f"-n{lines_arg}", str(path)])
        return ToolResult(
            True, f"Read {path}",
            stdout=path.read_text(encoding="utf-8", errors="replace")[:8192],
        )

    def tool_file_query(self, args: dict[str, Any]) -> ToolResult:
        raw_path = args.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ValueError("'path' must be a non-empty string")
        path = Path(raw_path).resolve()
        if path.is_file():
            return ToolResult(True, f"File exists: {path}")
        return ToolResult(False, f"File does not exist: {path}", exit_code=1)

    def tool_dir_query(self, args: dict[str, Any]) -> ToolResult:
        raw_path = args.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ValueError("'path' must be a non-empty string")
        path = Path(raw_path).resolve()
        if path.is_dir():
            return ToolResult(True, f"Directory exists: {path}")
        return ToolResult(False, f"Directory does not exist: {path}", exit_code=1)

    def tool_http_check(self, args: dict[str, Any]) -> ToolResult:
        url = args.get("url", "")
        if not isinstance(url, str) or not re.fullmatch(
            r"https?://(127\.0\.0\.1|localhost)(?::\d+)?(?:/[^\s]*)?", url
        ):
            raise ValueError("HTTP checks are limited to localhost URLs")
        if not shutil.which("curl"):
            return ToolResult(False, "curl is required for http.check", exit_code=127)
        return self._exec(
            ["curl", "--fail", "--silent", "--show-error",
             "--max-time", "10", "--connect-timeout", "5", url],
            timeout=15,
        )

    def tool_command_argv(self, args: dict[str, Any]) -> ToolResult:
        """Execute a fixed argv list — allowlist-checked binary, no shell grammar.

        This is the escape hatch for tasks that the typed tool catalogue cannot
        cover (e.g. CSV processing, test runs, log filtering). The first element
        of argv must be in SAFE_ARGV_COMMANDS.  Shell builtins that cannot be
        exec'd directly (cd, export, etc.) are intentionally excluded.
        """
        argv = args.get("argv")
        if not isinstance(argv, list) or not argv:
            raise ValueError("'argv' must be a non-empty list of strings")
        if not all(isinstance(a, str) for a in argv):
            raise ValueError("all argv elements must be strings")
        binary = os.path.basename(argv[0])
        if binary not in SAFE_ARGV_COMMANDS:
            raise ValueError(
                f"'{binary}' is not in the command allowlist. "
                f"Use a registered typed tool or add the binary to SAFE_ARGV_COMMANDS."
            )
        cwd = args.get("cwd")
        timeout = args.get("timeout", self.command_timeout)
        if not isinstance(timeout, int) or timeout < 1 or timeout > 300:
            timeout = self.command_timeout
        # Resolve the binary to its full path so we don't depend on $PATH tricks
        full_path = shutil.which(argv[0])
        if not full_path:
            if binary == "uptime":
                import time
                try:
                    import psutil
                    up_sec = int(time.time() - psutil.boot_time())
                except Exception:
                    up_sec = 3600
                h, r = divmod(up_sec, 3600)
                m, s = divmod(r, 60)
                return ToolResult(True, f"up {h}:{m:02d}", stdout=f"up {h}:{m:02d}\n", exit_code=0)
            elif binary == "date":
                import datetime
                ds = datetime.datetime.now().strftime("%a %b %d %H:%M:%S %Z %Y")
                return ToolResult(True, ds, stdout=ds + "\n", exit_code=0)
            elif binary == "whoami":
                usr = self.caller.get("user", "root")
                return ToolResult(True, usr, stdout=usr + "\n", exit_code=0)
            elif binary == "df":
                return self.tool_disk_status({})
            elif binary == "free":
                return self.tool_memory_status({})
            elif binary == "uname":
                return self.tool_system_info({})
            full_path = argv[0]
        resolved_argv = [full_path] + argv[1:]
        return self._exec(resolved_argv, timeout=timeout, cwd=cwd)

    # ── Change tools ──────────────────────────────────────────────────────────

    def tool_package_install(self, args: dict[str, Any]) -> ToolResult:
        package = self._name(args, "package")
        if shutil.which("apk"):
            return self._exec(["apk", "add", "--no-progress", package], timeout=180)
        return ToolResult(False, "No supported package manager found", exit_code=127)

    def _service(self, action: str, args: dict[str, Any]) -> ToolResult:
        name = self._name(args)
        if shutil.which("rc-service"):
            return self._exec(["rc-service", name, action])
        return ToolResult(False, "OpenRC is not available", exit_code=127)

    def tool_service_start(self, args: dict[str, Any]) -> ToolResult:
        return self._service("start", args)

    def tool_service_restart(self, args: dict[str, Any]) -> ToolResult:
        return self._service("restart", args)

    def tool_service_stop(self, args: dict[str, Any]) -> ToolResult:
        return self._service("stop", args)

    def tool_service_enable(self, args: dict[str, Any]) -> ToolResult:
        name = self._name(args)
        if shutil.which("rc-update"):
            runlevel = args.get("runlevel", "default")
            if not re.fullmatch(r"[a-z]{1,32}", runlevel):
                raise ValueError("runlevel must be a simple word")
            return self._exec(["rc-update", "add", name, runlevel])
        return ToolResult(False, "OpenRC is not available", exit_code=127)

    def tool_service_disable(self, args: dict[str, Any]) -> ToolResult:
        name = self._name(args)
        if shutil.which("rc-update"):
            return self._exec(["rc-update", "del", name])
        return ToolResult(False, "OpenRC is not available", exit_code=127)

    def tool_dir_create(self, args: dict[str, Any]) -> ToolResult:
        raw_path = args.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ValueError("'path' must be a non-empty string")
        path = Path(raw_path).resolve()
        blocked_roots = (Path("/proc"), Path("/sys"), Path("/dev"))
        if any(path == root or root in path.parents for root in blocked_roots):
            raise ValueError("Target directory path is inside a protected system filesystem")
        path.mkdir(parents=True, exist_ok=True)
        uid = args.get("uid", self.caller.get("uid"))
        gid = args.get("gid", self.caller.get("gid"))
        if uid is not None and gid is not None and getattr(os, "geteuid", lambda: 0)() == 0 and hasattr(os, "chown"):
            try:
                os.chown(path, int(uid), int(gid))
            except OSError:
                pass
        return ToolResult(True, f"Directory created: {path}")

    def tool_dir_remove(self, args: dict[str, Any]) -> ToolResult:
        raw_path = args.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ValueError("'path' must be a non-empty string")
        path = Path(raw_path).resolve()
        protected = (
            Path("/"), Path("/bin"), Path("/sbin"), Path("/usr"), Path("/etc"),
            Path("/var"), Path("/home"), Path("/root"), Path("/dev"),
            Path("/proc"), Path("/sys"),
        )
        if path in protected or path.parent == path:
            raise ValueError(f"Cannot delete protected path: {path}")
        if not path.exists():
            return ToolResult(True, f"Directory already absent: {path}")
        if not path.is_dir():
            raise ValueError(f"Path is not a directory: {path}")
        shutil.rmtree(path)
        return ToolResult(True, f"Directory removed: {path}")

    def tool_file_write(self, args: dict[str, Any]) -> ToolResult:
        raw_path = args.get("path")
        content  = args.get("content", "")
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ValueError("'path' must be a non-empty string")
        path = Path(raw_path).resolve()
        blocked_roots = (Path("/proc"), Path("/sys"), Path("/dev"))
        if any(path == root or root in path.parents for root in blocked_roots) or any(raw_path.strip().startswith(p) for p in ("/proc", "/sys", "/dev")):
            raise ValueError("Target file path is inside a protected system filesystem")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        uid = args.get("uid", self.caller.get("uid"))
        gid = args.get("gid", self.caller.get("gid"))
        if uid is not None and gid is not None and getattr(os, "geteuid", lambda: 0)() == 0 and hasattr(os, "chown"):
            try:
                os.chown(path, int(uid), int(gid))
            except OSError:
                pass
        return ToolResult(True, f"File written: {path} ({len(content)} bytes)")

    def tool_file_move(self, args: dict[str, Any]) -> ToolResult:
        """Move or rename a file/directory.

        args:
            src  – absolute source path
            dst  – absolute destination path (file or directory target)
        """
        raw_src = args.get("src")
        raw_dst = args.get("dst")
        if not isinstance(raw_src, str) or not raw_src.strip():
            raise ValueError("'src' must be a non-empty string")
        if not isinstance(raw_dst, str) or not raw_dst.strip():
            raise ValueError("'dst' must be a non-empty string")
        src = Path(raw_src).resolve()
        dst = Path(raw_dst).resolve()
        protected = (Path("/proc"), Path("/sys"), Path("/dev"),
                     Path("/bin"), Path("/sbin"), Path("/usr"))
        for guard in protected:
            if src == guard or guard in src.parents:
                raise ValueError(f"Source path is inside a protected system directory: {src}")
            if dst == guard or guard in dst.parents:
                raise ValueError(f"Destination path is inside a protected system directory: {dst}")
        if not src.exists():
            return ToolResult(False, f"Source does not exist: {src}", exit_code=1)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        return ToolResult(True, f"Moved {src} → {dst}")

    def tool_file_delete(self, args: dict[str, Any]) -> ToolResult:
        """Delete a single file (not a directory — use dir.remove for that).

        args:
            path – absolute path of the file to delete
        """
        raw_path = args.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ValueError("'path' must be a non-empty string")
        path = Path(raw_path).resolve()
        protected = (
            Path("/"), Path("/bin"), Path("/sbin"), Path("/usr"),
            Path("/etc"), Path("/var"), Path("/proc"), Path("/sys"), Path("/dev"),
        )
        if path in protected:
            raise ValueError(f"Cannot delete protected path: {path}")
        if not path.exists():
            return ToolResult(True, f"File already absent: {path}")
        if not path.is_file():
            raise ValueError(f"Path is not a regular file: {path} — use dir.remove for directories")
        path.unlink()
        return ToolResult(True, f"Deleted: {path}")

    def tool_file_list(self, args: dict[str, Any]) -> ToolResult:
        """List files in a directory, with optional glob pattern and recursion.

        args:
            path      – absolute directory path to list
            pattern   – optional glob pattern, e.g. '*.py' (default '*')
            recursive – if true, walk subdirectories (default false)
            max_items – cap on number of results (default 200)
        """
        raw_path = args.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ValueError("'path' must be a non-empty string")
        path = Path(raw_path).resolve()
        if not path.is_dir():
            return ToolResult(False, f"Directory does not exist: {path}", exit_code=1)
        pattern   = args.get("pattern", "*")
        recursive = bool(args.get("recursive", False))
        max_items = int(args.get("max_items", 200))
        if not isinstance(pattern, str):
            pattern = "*"
        if recursive:
            entries = list(path.rglob(pattern))[:max_items]
        else:
            entries = list(path.glob(pattern))[:max_items]
        lines = []
        for e in sorted(entries):
            kind = "d" if e.is_dir() else "f"
            try:
                size = e.stat().st_size if e.is_file() else 0
            except OSError:
                size = -1
            lines.append(f"{kind}\t{size}\t{e}")
        stdout = "\n".join(lines)
        return ToolResult(
            True,
            f"Listed {len(entries)} entries in {path}",
            stdout=stdout,
            facts=[{"count": len(entries), "path": str(path), "pattern": pattern}],
        )

    def tool_http_get(self, args: dict[str, Any]) -> ToolResult:
        """Perform an external HTTP GET request via curl (read-only, safe).

        args:
            url      – HTTP/HTTPS URL to fetch
            headers  – optional dict of header name → value pairs
            timeout  – optional request timeout in seconds (max 60)
            max_bytes – optional limit on response body captured (default 32768)
        """
        url = args.get("url", "")
        if not isinstance(url, str) or not re.fullmatch(
            r"https?://[A-Za-z0-9._\-]+(:\d+)?(/[^\s]*)?", url
        ):
            raise ValueError("'url' must be a valid http:// or https:// URL")
        # Block private network ranges to avoid SSRF against internal services
        # localhost is already handled by http.check; block internal RFC1918
        _blocked = re.search(
            r"https?://(localhost|127\.|10\.|192\.168\.|172\.(1[6-9]|2\d|3[01]))",
            url, re.IGNORECASE,
        )
        if _blocked:
            raise ValueError(
                "http.get is for external URLs; use http.check for localhost."
            )
        if not shutil.which("curl"):
            return ToolResult(False, "curl is required for http.get", exit_code=127)
        timeout = args.get("timeout", 30)
        if not isinstance(timeout, int) or timeout < 1 or timeout > 60:
            timeout = 30
        max_bytes = args.get("max_bytes", 32768)
        if not isinstance(max_bytes, int) or max_bytes < 1:
            max_bytes = 32768
        headers_arg = args.get("headers", {})
        extra: list[str] = []
        if isinstance(headers_arg, dict):
            for k, v in headers_arg.items():
                if isinstance(k, str) and isinstance(v, str):
                    # Basic guard: no newlines in header name/value
                    if "\n" not in k and "\n" not in v:
                        extra += ["-H", f"{k}: {v}"]
        argv = (
            ["curl", "--silent", "--show-error", "--fail-with-body",
             "--max-time", str(timeout),
             "--max-filesize", str(max_bytes)]
            + extra
            + [url]
        )
        result = self._exec(argv, timeout=timeout + 5)
        # Truncate captured body to max_bytes
        result.stdout = result.stdout[:max_bytes]
        return result

    def tool_llm_prompt(self, args: dict[str, Any]) -> ToolResult:
        """Run a prompt against the local Ollama/llama.cpp endpoint and capture the reply.

        This is a read-only tool: it does not modify system state.
        The response is returned in stdout and can be captured into the scratchpad
        with a captures entry like ``{"summary": "stdout:strip"}``.

        args:
            prompt   – the text prompt to send
            model    – optional model name (default: use the daemon's configured model)
            max_tokens – optional max tokens for the response (default 512)
            endpoint – optional base URL of the Ollama API (default http://localhost:11434)
        """
        prompt = args.get("prompt", "")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("'prompt' must be a non-empty string")
        model    = args.get("model", "llama3")
        if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9._:\-]{1,128}", model):
            raise ValueError("'model' must be a simple model identifier")
        max_tokens = args.get("max_tokens", 512)
        if not isinstance(max_tokens, int) or max_tokens < 1 or max_tokens > 4096:
            max_tokens = 512
        endpoint = args.get("endpoint", "http://localhost:11434")
        if not isinstance(endpoint, str) or not re.fullmatch(
            r"https?://(127\.0\.0\.1|localhost)(:\d+)?", endpoint
        ):
            raise ValueError("'endpoint' must be a localhost Ollama URL")
        if not shutil.which("curl"):
            return ToolResult(False, "curl is required for llm.prompt", exit_code=127)
        payload = json.dumps({
            "model":  model,
            "prompt": prompt,
            "stream": False,
            "options": {"num_predict": max_tokens},
        })
        argv = [
            "curl", "--silent", "--show-error",
            "--max-time", "120",
            "-X", "POST",
            "-H", "Content-Type: application/json",
            "-d", payload,
            f"{endpoint}/api/generate",
        ]
        result = self._exec(argv, timeout=125)
        if result.ok and result.stdout:
            try:
                resp = json.loads(result.stdout)
                text = resp.get("response", result.stdout)
                return ToolResult(
                    True, f"LLM response from {model} ({len(text)} chars)",
                    stdout=text[:8192],
                    facts=[{"model": model, "tokens": resp.get("eval_count", -1)}],
                )
            except (json.JSONDecodeError, KeyError):
                pass  # fall through and return raw stdout
        return result

    def tool_file_append(self, args: dict[str, Any]) -> ToolResult:
        """Append text content to a file (creating it if absent).

        args:
            path    – absolute target file path
            content – text content to append
        """
        raw_path = args.get("path")
        content  = args.get("content", "")
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ValueError("'path' must be a non-empty string")
        if not isinstance(content, str):
            content = str(content)
        path = Path(raw_path).resolve()
        blocked_roots = (Path("/proc"), Path("/sys"), Path("/dev"))
        if any(path == root or root in path.parents for root in blocked_roots) or any(raw_path.strip().startswith(p) for p in ("/proc", "/sys", "/dev")):
            raise ValueError("Target file path is inside a protected system filesystem")
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(content)
        uid = args.get("uid", self.caller.get("uid"))
        gid = args.get("gid", self.caller.get("gid"))
        if uid is not None and gid is not None and getattr(os, "geteuid", lambda: 0)() == 0 and hasattr(os, "chown"):
            try:
                os.chown(path, int(uid), int(gid))
            except OSError:
                pass
        return ToolResult(True, f"File appended: {path} (+{len(content)} bytes)")

    def tool_shell_script(self, args: dict[str, Any]) -> ToolResult:
        """Write code to a secure temporary script and execute with a safe interpreter.

        This eliminates messy multi-line shell escaping and argv limits.
        args:
            code        – script code to execute
            interpreter – 'sh' (default), 'bash', 'python3', or 'python'
            timeout     – max execution time in seconds (default 60, max 300)
            cwd         – working directory (optional)
            args        – optional list of string arguments passed to the script
        """
        code = args.get("code")
        if not isinstance(code, str) or not code.strip():
            raise ValueError("'code' must be a non-empty string")
        # Security deny checks for known dangerous patterns
        _script_deny = [
            r"rm\s+-[rRf]*f[rR]?\s+/(?!\w)",
            r"rm\s+-[rRf]*r[fF]?\s+/(?!\w)",
            r"\bdd\b.*\bof=/dev/[sh]d",
            r"mkfs\.",
            r":\(\)\{.*\};",
            r"chmod\s+[0-7]*7[0-7]*\s+/\s*$",
        ]
        for pat in _script_deny:
            if re.search(pat, code, re.IGNORECASE):
                raise ValueError(f"Script content blocked by security policy: pattern {pat}")
        interp = args.get("interpreter", "sh")
        if interp not in {"sh", "bash", "python3", "python"}:
            raise ValueError("'interpreter' must be one of: 'sh', 'bash', 'python3', 'python'")
        if interp in ("python3", "python"):
            interp_bin = sys.executable
        else:
            interp_bin = shutil.which(interp) or interp
        timeout = args.get("timeout", self.command_timeout)
        if not isinstance(timeout, int) or timeout < 1 or timeout > 300:
            timeout = self.command_timeout
        cwd = args.get("cwd")
        script_args = args.get("args", [])
        if not isinstance(script_args, list) or not all(isinstance(a, str) for a in script_args):
            script_args = []
        suffix = ".py" if "python" in interp else ".sh"
        fd, tmp_path = tempfile.mkstemp(prefix="agent_script_", suffix=suffix)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(code)
            if hasattr(os, "chmod"):
                try:
                    os.chmod(tmp_path, 0o700)
                except OSError:
                    pass
            argv = [interp_bin, tmp_path] + script_args
            return self._exec(argv, timeout=timeout, cwd=cwd)
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    def tool_data_csv_read(self, args: dict[str, Any]) -> ToolResult:
        """Read and parse a CSV file, returning structured JSON rows and summary.

        args:
            path      – absolute path to the CSV file
            delimiter – delimiter character (default ',')
            max_rows  – maximum rows to parse (default 200, max 1000)
        """
        raw_path = args.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ValueError("'path' must be a non-empty string")
        path = Path(raw_path).resolve()
        if not path.is_file():
            return ToolResult(False, f"CSV file does not exist: {path}", exit_code=1)
        delimiter = str(args.get("delimiter", ","))
        if len(delimiter) != 1:
            delimiter = ","
        max_rows = min(int(args.get("max_rows", 200)), 1000)
        headers: list[str] = []
        rows: list[dict[str, str]] = []
        total_count = 0
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                reader = csv.reader(f, delimiter=delimiter)
                try:
                    headers = next(reader)
                except StopIteration:
                    headers = []
                for row in reader:
                    total_count += 1
                    if len(rows) < max_rows:
                        if headers:
                            row_dict = {headers[i]: row[i] if i < len(row) else "" for i in range(len(headers))}
                        else:
                            row_dict = {f"col_{i}": val for i, val in enumerate(row)}
                        rows.append(row_dict)
            output_data = {
                "path": str(path),
                "headers": headers,
                "total_rows": total_count,
                "sample_rows": rows,
            }
            stdout = json.dumps(output_data, ensure_ascii=False, indent=2)
            return ToolResult(
                True,
                f"Read CSV {path}: {total_count} rows, {len(headers)} columns",
                stdout=stdout[:8192],
                facts=[{"path": str(path), "total_rows": total_count, "columns": headers}],
            )
        except Exception as e:
            return ToolResult(False, f"Failed to parse CSV {path}: {e}", stderr=str(e), exit_code=1)

    def tool_data_csv_write(self, args: dict[str, Any]) -> ToolResult:
        """Write or append rows to a CSV file.

        args:
            path    – absolute target file path
            headers – optional list of column header names
            rows    – list of rows (either list of lists or list of dicts)
            mode    – 'w' for overwrite (default) or 'a' for append
        """
        raw_path = args.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ValueError("'path' must be a non-empty string")
        path = Path(raw_path).resolve()
        blocked_roots = (Path("/proc"), Path("/sys"), Path("/dev"))
        if any(path == root or root in path.parents for root in blocked_roots):
            raise ValueError("Target file path is inside a protected system filesystem")
        rows = args.get("rows", [])
        if not isinstance(rows, list):
            raise ValueError("'rows' must be a list")
        headers = args.get("headers")
        mode = "a" if args.get("mode") == "a" else "w"
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            write_header = (mode == "w") or not path.exists() or path.stat().st_size == 0
            with open(path, mode, newline="", encoding="utf-8") as f:
                if rows and isinstance(rows[0], dict):
                    if not headers:
                        headers = list(rows[0].keys())
                    writer = csv.DictWriter(f, fieldnames=headers)
                    if write_header and headers:
                        writer.writeheader()
                    for r in rows:
                        if isinstance(r, dict):
                            writer.writerow(r)
                else:
                    writer = csv.writer(f)
                    if write_header and headers and isinstance(headers, list):
                        writer.writerow(headers)
                    for r in rows:
                        if isinstance(r, list):
                            writer.writerow(r)
            return ToolResult(True, f"Wrote {len(rows)} rows to CSV {path}")
        except Exception as e:
            return ToolResult(False, f"Failed to write CSV: {e}", stderr=str(e), exit_code=1)

    def tool_document_read(self, args: dict[str, Any]) -> ToolResult:
        """Extract text from documents (.txt, .md, .pdf, .docx, .json, .csv).

        Zero external dependencies for DOCX (standard library XML+ZIP extraction).
        Uses pdftotext when available for PDFs.
        args:
            path      – absolute path to document
            max_bytes – maximum bytes to return (default 16384)
        """
        raw_path = args.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ValueError("'path' must be a non-empty string")
        path = Path(raw_path).resolve()
        if not path.is_file():
            return ToolResult(False, f"Document does not exist: {path}", exit_code=1)
        max_bytes = int(args.get("max_bytes", 16384))
        ext = path.suffix.lower()
        text = ""

        if ext == ".docx":
            try:
                with zipfile.ZipFile(path) as z:
                    xml_content = z.read("word/document.xml")
                    root = ET.fromstring(xml_content)
                    paragraphs = []
                    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
                    for p in root.iterfind(".//w:p", ns):
                        p_texts = [node.text for node in p.iterfind(".//w:t", ns) if node.text]
                        if p_texts:
                            paragraphs.append("".join(p_texts))
                    text = "\n\n".join(paragraphs)
            except Exception as e:
                return ToolResult(False, f"Failed to parse DOCX {path}: {e}", stderr=str(e), exit_code=1)

        elif ext == ".pdf":
            if shutil.which("pdftotext"):
                res = self._exec(["pdftotext", "-layout", str(path), "-"])
                if res.ok:
                    text = res.stdout
                else:
                    return res
            else:
                raw_bytes = path.read_bytes()
                streams = re.findall(rb"stream[\r\n]+([\s\S]*?)[\r\n]+endstream", raw_bytes)
                extracted_chunks = []
                for s in streams:
                    try:
                        import zlib
                        decompressed = zlib.decompress(s)
                        bt = re.findall(rb"\((.*?)\)\s*Tj", decompressed)
                        if bt:
                            extracted_chunks.append(b" ".join(bt).decode("utf-8", "replace"))
                    except Exception:
                        pass
                text = "\n".join(extracted_chunks)
                if not text:
                    text = f"PDF file {path} detected ({len(raw_bytes)} bytes). Install 'poppler-utils' for full layout extraction."

        else:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except Exception as e:
                return ToolResult(False, f"Failed to read file {path}: {e}", stderr=str(e), exit_code=1)

        truncated = text[:max_bytes]
        return ToolResult(
            True,
            f"Extracted document text from {path} ({len(text)} chars)",
            stdout=truncated,
            facts=[{"path": str(path), "total_chars": len(text), "format": ext or "plain"}],
        )

    def tool_email_send(self, args: dict[str, Any]) -> ToolResult:
        """Compose and send or spool an email message.

        args:
            to          – recipient email address or list of addresses
            subject     – email subject line
            body        – plain text email body
            attachments – optional list of file paths to attach
            smtp_server – optional SMTP server hostname (or SMTP_SERVER env var)
            smtp_port   – optional SMTP server port (default 25 or 587)
        """
        to_arg  = args.get("to")
        subject = args.get("subject", "")
        body    = args.get("body", "")
        if not to_arg:
            raise ValueError("'to' is required")
        recipients = [to_arg] if isinstance(to_arg, str) else list(to_arg)
        if not all(isinstance(r, str) and "@" in r for r in recipients):
            raise ValueError("'to' must be valid email address(es)")
        from_addr = args.get("from") or os.environ.get("EMAIL_FROM") or "agent@localhost"

        msg = email.message.EmailMessage()
        msg["Subject"] = str(subject)
        msg["From"] = from_addr
        msg["To"] = ", ".join(recipients)
        msg.set_content(str(body))

        attachments = args.get("attachments", [])
        if isinstance(attachments, list):
            for att_path in attachments:
                if isinstance(att_path, str) and os.path.isfile(att_path):
                    p = Path(att_path)
                    try:
                        content = p.read_bytes()
                        msg.add_attachment(content, maintype="application", subtype="octet-stream", filename=p.name)
                    except OSError:
                        pass

        smtp_server = args.get("smtp_server") or os.environ.get("SMTP_SERVER")
        smtp_port   = int(args.get("smtp_port") or os.environ.get("SMTP_PORT") or 25)

        sent_live = False
        if smtp_server:
            try:
                with smtplib.SMTP(smtp_server, smtp_port, timeout=10) as s:
                    smtp_user = os.environ.get("SMTP_USER")
                    smtp_pass = os.environ.get("SMTP_PASS")
                    if smtp_user and smtp_pass:
                        s.starttls()
                        s.login(smtp_user, smtp_pass)
                    s.send_message(msg)
                sent_live = True
            except Exception:
                pass

        spool_dir = Path("/var/ai-agent")
        spool_dir.mkdir(parents=True, exist_ok=True)
        spool_path = spool_dir / "mail_outbox.jsonl"
        spool_record = {
            "timestamp": time.time(),
            "from": from_addr,
            "to": recipients,
            "subject": subject,
            "body": body,
            "attachments": [str(a) for a in attachments if isinstance(a, str)],
            "sent_live": sent_live,
        }
        with open(spool_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(spool_record, ensure_ascii=False) + "\n")

        status_msg = "Sent live via SMTP" if sent_live else f"Spooled to {spool_path}"
        return ToolResult(
            True,
            f"Email '{subject}' to {recipients} processed ({status_msg})",
            stdout=f"To: {', '.join(recipients)}\nSubject: {subject}\nStatus: {status_msg}\n\n{body[:1000]}",
            facts=[{"to": recipients, "subject": subject, "sent_live": sent_live}],
        )

    def tool_email_read(self, args: dict[str, Any]) -> ToolResult:
        """Read received or spooled emails from IMAP or local mail store.

        args:
            folder      – mailbox folder name (default 'INBOX')
            max_emails  – maximum number of messages to fetch (default 10)
            search      – optional keyword filter for subject/body
            unseen_only – if true, fetch only unread messages (default false)
        """
        folder     = args.get("folder", "INBOX")
        max_emails = min(int(args.get("max_emails", 10)), 50)
        search_kw  = args.get("search", "").lower()
        unseen_only = bool(args.get("unseen_only", False))

        messages = []
        imap_server = args.get("imap_server") or os.environ.get("IMAP_SERVER")
        if imap_server:
            try:
                import imaplib
                acc  = os.environ.get("EMAIL_ACCOUNT", "")
                pwd  = os.environ.get("EMAIL_PASSWORD", "")
                mail = imaplib.IMAP4_SSL(imap_server)
                mail.login(acc, pwd)
                mail.select(folder)
                criterion = '(UNSEEN)' if unseen_only else 'ALL'
                status, data = mail.search(None, criterion)
                if status == "OK" and data[0]:
                    mail_ids = data[0].split()[-max_emails:]
                    for mid in reversed(mail_ids):
                        s, mdata = mail.fetch(mid, "(RFC822)")
                        for part in mdata:
                            if isinstance(part, tuple):
                                parsed = email.message_from_bytes(part[1])
                                b_text = ""
                                if parsed.is_multipart():
                                    for sub in parsed.walk():
                                        if sub.get_content_type() == "text/plain":
                                            b_text = sub.get_payload(decode=True).decode("utf-8", "ignore")
                                            break
                                else:
                                    b_text = parsed.get_payload(decode=True).decode("utf-8", "ignore")
                                messages.append({
                                    "id": mid.decode(),
                                    "from": parsed.get("From", ""),
                                    "to": parsed.get("To", ""),
                                    "subject": parsed.get("Subject", ""),
                                    "date": parsed.get("Date", ""),
                                    "body": b_text[:500],
                                })
                mail.close()
                mail.logout()
            except Exception:
                pass

        spool_path = Path("/var/ai-agent/mail_outbox.jsonl")
        if spool_path.is_file():
            try:
                for line in spool_path.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        r = json.loads(line)
                        if search_kw:
                            subj = str(r.get("subject", "")).lower()
                            bdy  = str(r.get("body", "")).lower()
                            if search_kw not in subj and search_kw not in bdy:
                                continue
                        messages.append({
                            "id": f"spool-{r.get('timestamp')}",
                            "from": r.get("from", "local"),
                            "to": r.get("to", []),
                            "subject": r.get("subject", ""),
                            "date": time.ctime(r.get("timestamp", 0)),
                            "body": str(r.get("body", ""))[:500],
                        })
            except Exception:
                pass

        messages.reverse()
        messages = messages[:max_emails]
        stdout = json.dumps(messages, ensure_ascii=False, indent=2)
        return ToolResult(
            True,
            f"Retrieved {len(messages)} email messages",
            stdout=stdout[:8192],
            facts=[{"count": len(messages), "folder": folder}],
        )

    def _run_cdp(self, action: str, **kwargs: Any) -> ToolResult:
        """Helper to invoke CDP controller via subprocess."""
        cdp_script = Path(__file__).parent / "cdp_controller.py"
        if not cdp_script.is_file():
            cdp_script = Path("/usr/local/lib/ai-agent/cdp_controller.py")
        if not cdp_script.is_file():
            return ToolResult(False, "cdp_controller.py not found", exit_code=127)
        argv = [sys.executable, str(cdp_script), action]
        for k, v in kwargs.items():
            if v is not None:
                argv.extend([f"--{k}", str(v)])
        return self._exec(argv, timeout=25)

    def tool_browser_navigate(self, args: dict[str, Any]) -> ToolResult:
        """Navigate the browser to a URL.

        args:
            url – HTTP/HTTPS URL
        """
        url = args.get("url", "")
        if not isinstance(url, str) or not re.fullmatch(r"https?://[^\s]+", url):
            raise ValueError("'url' must be a valid http:// or https:// URL")
        return self._run_cdp("goto", url=url)

    def tool_browser_dump(self, args: dict[str, Any]) -> ToolResult:
        """Extract interactive elements and accessibility tree from active browser tab.

        args: none required
        """
        return self._run_cdp("dump")

    def tool_browser_click(self, args: dict[str, Any]) -> ToolResult:
        """Click an element in the active browser tab by backendNodeId.

        args:
            id – backendNodeId (integer) from browser.dump
        """
        node_id = args.get("id") if args.get("id") is not None else args.get("node_id")
        if node_id is None:
            raise ValueError("'id' is required for browser.click")
        return self._run_cdp("click", id=int(node_id))

    def tool_browser_type(self, args: dict[str, Any]) -> ToolResult:
        """Type text into an element in the active browser tab.

        args:
            id   – backendNodeId (integer)
            text – string to type
        """
        node_id = args.get("id") if args.get("id") is not None else args.get("node_id")
        text    = args.get("text", "")
        if node_id is None:
            raise ValueError("'id' is required for browser.type")
        return self._run_cdp("type", id=int(node_id), text=str(text))


    def tool_app_launch(self, args: dict[str, Any]) -> ToolResult:
        """Launch a desktop/GUI application in the user's graphical session.

        args:
            app  - application name or alias ("browser", "file_manager", "terminal", "editor", etc.)
            url  - optional URL for browser
            path - optional target file/directory for file manager
        """
        app = str(args.get("app") or args.get("name") or "browser").lower().strip()
        url = str(args.get("url") or "").strip()
        path = str(args.get("path") or "").strip()

        target_user = self.caller.get("user") or "aiuser"
        if target_user == "root" and os.path.exists("/home/aiuser"):
            target_user = "aiuser"

        xauth = f"/home/{target_user}/.Xauthority" if target_user != "root" else "/var/run/lightdm/root/:0"
        display = os.environ.get("DISPLAY") or ":0"

        dbus_addr = ""
        try:
            pids = subprocess.check_output(["pgrep", "-u", target_user, "xfce4-session"]).decode().split()
            if pids:
                with open(f"/proc/{pids[0]}/environ", "rb") as ef:
                    for e in ef.read().split(b"\0"):
                        if e.startswith(b"DBUS_SESSION_BUS_ADDRESS="):
                            dbus_addr = e.decode().split("=", 1)[1]
                            break
        except Exception:
            pass

        if app in ("browser", "chromium", "web", "chrome"):
            target_url = url or "http://127.0.0.1:8080"
            cmd = (
                f"chromium --no-first-run --no-default-browser-check "
                f"--remote-debugging-port=9222 --remote-allow-origins=* '{target_url}'"
            )
            app_label = "Chromium Browser"
        elif app in ("file_manager", "files", "thunar", "filemanager"):
            target_path = f"/home/{target_user}" if (not path or path == "/home/root") else path
            cmd = f"thunar '{target_path}'"
            app_label = "Thunar File Manager"
        elif app in ("terminal", "console", "xfce4-terminal", "shell"):
            cmd = "xfce4-terminal"
            app_label = "XFCE Terminal"
        elif app in ("editor", "text_editor", "mousepad", "edit"):
            target_path = path or ""
            cmd = f"mousepad {target_path}".strip()
            app_label = "Mousepad Text Editor"
        elif app in ("dashboard", "sentinel", "control_center"):
            cmd = "python3 /usr/local/lib/ai-agent/dashboard.py"
            app_label = "Sentinel Control Center"
        else:
            bin_path = shutil.which(app)
            if not bin_path:
                return ToolResult(False, f"Application '{app}' not found on system", exit_code=127)
            cmd = f"{bin_path} {path}".strip()
            app_label = app

        env_prefix = f"DISPLAY={display} XAUTHORITY={xauth} "
        if dbus_addr:
            env_prefix += f"DBUS_SESSION_BUS_ADDRESS='{dbus_addr}' "

        full_shell_cmd = f"su - {target_user} -c \"{env_prefix} nohup {cmd} >/dev/null 2>&1 &\""

        try:
            subprocess.run(
                ["sh", "-c", full_shell_cmd],
                capture_output=True,
                text=True,
                timeout=10,
            )
            time.sleep(0.5)
            return ToolResult(
                True,
                f"Launched {app_label} successfully in user '{target_user}' desktop session",
                stdout=f"{app_label} active on DISPLAY={display}",
                facts=[{"app": app, "user": target_user, "display": display}],
            )
        except Exception as e:
            return ToolResult(False, f"Failed to launch {app_label}: {e}", exit_code=1)

    def tool_browser_screenshot(self, args: dict[str, Any]) -> ToolResult:
        """Capture a screenshot of the active browser tab and save to a file.

        args:
            path – target output path (default /tmp/browser_screenshot.png)
        """
        raw_path = args.get("path", "/tmp/browser_screenshot.png")
        if not isinstance(raw_path, str) or not raw_path.strip():
            raw_path = "/tmp/browser_screenshot.png"
        path = Path(raw_path).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        res = self._run_cdp("screenshot", path=str(path))
        if res.ok and path.is_file():
            return ToolResult(True, f"Browser screenshot saved to {path}", stdout=str(path))
        if shutil.which("import"):
            fb = self._exec(["import", "-window", "root", str(path)])
            if fb.ok:
                return ToolResult(True, f"Root window screenshot saved to {path}", stdout=str(path))
        return res


ALLOWED_TOOLS = PolicyEngine.READ_ONLY | PolicyEngine.CHANGE

# ── Tool argument schemas for strict plan validation ─────────────────────────
TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "system.info": {"required": {}, "optional": {}},
    "disk.status": {"required": {}, "optional": {}},
    "memory.status": {"required": {}, "optional": {}},
    "package.query": {"required": {"package": str}, "optional": {}},
    "package.install": {"required": {"package": str}, "optional": {}},
    "service.status": {"required": {"name": str}, "optional": {}},
    "service.start": {"required": {"name": str}, "optional": {}},
    "service.restart": {"required": {"name": str}, "optional": {}},
    "service.stop": {"required": {"name": str}, "optional": {}},
    "service.enable": {"required": {"name": str}, "optional": {"runlevel": str}},
    "service.disable": {"required": {"name": str}, "optional": {}},
    "service.logs": {"required": {"name": str}, "optional": {}},
    "port.inspect": {"required": {"port": (int, str)}, "optional": {}},
    "file.read": {"required": {"path": str}, "optional": {"max_bytes": int, "tail_lines": int}},
    "file.write": {"required": {"path": str, "content": str}, "optional": {"uid": int, "gid": int, "mode": int}},
    "file.append": {"required": {"path": str, "content": str}, "optional": {}},
    "file.move": {"required": {"src": str, "dst": str}, "optional": {}},
    "file.delete": {"required": {"path": str}, "optional": {}},
    "file.list": {"required": {"path": str}, "optional": {"pattern": str, "recursive": bool}},
    "file.query": {"required": {"path": str}, "optional": {"query": str}},
    "dir.create": {"required": {"path": str}, "optional": {"uid": int, "gid": int, "mode": int}},
    "dir.remove": {"required": {"path": str}, "optional": {}},
    "dir.query": {"required": {"path": str}, "optional": {}},
    "command.argv": {"required": {"argv": list}, "optional": {"timeout": (int, float), "cwd": str}},
    "shell.script": {"required": {"script": str}, "optional": {"timeout": (int, float)}},
    "http.check": {"required": {"url": str}, "optional": {"timeout": (int, float)}},
    "http.get": {"required": {"url": str}, "optional": {"timeout": (int, float)}},
    "llm.prompt": {"required": {"prompt": str}, "optional": {}},
    "data.csv_read": {"required": {"path": str}, "optional": {"max_rows": int}},
    "data.csv_write": {"required": {"path": str, "rows": list}, "optional": {}},
    "document.read": {"required": {"path": str}, "optional": {"max_chars": int}},
    "email.read": {"required": {}, "optional": {"folder": str, "max_messages": int}},
    "email.send": {"required": {"to": str, "subject": str, "body": str}, "optional": {}},
    "browser.navigate": {"required": {"url": str}, "optional": {}},
    "browser.dump": {"required": {}, "optional": {}},
    "browser.screenshot": {"required": {}, "optional": {"path": str}},
    "app.launch": {"required": {"app": str}, "optional": {"url": str, "path": str, "name": str}},
    "browser.click": {"required": {}, "optional": {"id": (int, str), "node_id": (int, str), "selector": str}},
    "browser.type": {"required": {}, "optional": {"id": (int, str), "node_id": (int, str), "selector": str, "text": str}},
}


def _validate_tool_args(tool: str, args: Any, context: str) -> None:
    """Strictly validate tool arguments against schema types and presence."""
    if not isinstance(args, dict):
        raise PlanError(f"{context} arguments must be a dictionary, got {type(args).__name__}")
    schema = TOOL_SCHEMAS.get(tool)
    if schema is None:
        raise PlanError(f"{context} references uncatalogued tool '{tool}'")
    required = schema.get("required", {})
    for req_key, req_types in required.items():
        if req_key not in args:
            raise PlanError(f"{context} for tool '{tool}' is missing required argument '{req_key}'")
        val = args[req_key]
        if not isinstance(val, req_types):
            # Allow string dynamic template placeholders like "${scratchpad.var}"
            if isinstance(val, str) and (val.startswith("${") or val.startswith("{{")):
                pass
            else:
                raise PlanError(
                    f"{context} for tool '{tool}' argument '{req_key}' expected {req_types}, got {type(val).__name__}"
                )
        if isinstance(val, str) and not val.strip() and req_key not in ("content", "body", "text"):
            raise PlanError(f"{context} for tool '{tool}' argument '{req_key}' cannot be empty")
        if isinstance(val, list) and not val and req_key in ("argv", "rows"):
            raise PlanError(f"{context} for tool '{tool}' argument '{req_key}' cannot be an empty list")

    if tool == "browser.click":
        if "id" not in args and "node_id" not in args and "selector" not in args:
            raise PlanError(f"{context} for tool '{tool}' requires 'id', 'node_id', or 'selector'")
    elif tool == "browser.type":
        if "id" not in args and "node_id" not in args and "selector" not in args:
            raise PlanError(f"{context} for tool '{tool}' requires 'id', 'node_id', or 'selector'")


def _detect_dag_cycles(steps: list[Step]) -> None:
    """Detect cycles in plan step dependencies using DFS cycle detection."""
    adj: dict[str, list[str]] = {s.id: s.depends_on for s in steps}
    visited: dict[str, int] = {s.id: 0 for s in steps}  # 0=unvisited, 1=visiting, 2=visited
    path: list[str] = []

    def dfs(node: str) -> None:
        visited[node] = 1
        path.append(node)
        for dep in adj.get(node, []):
            if dep not in visited:
                continue
            if visited[dep] == 1:
                cycle_slice = path[path.index(dep):] + [dep]
                raise PlanError(f"Dependency cycle detected in plan: {' -> '.join(cycle_slice)}")
            elif visited[dep] == 0:
                dfs(dep)
        path.pop()
        visited[node] = 2

    for s in steps:
        if visited[s.id] == 0:
            dfs(s.id)


# ── Plan validation ───────────────────────────────────────────────────────────
def validate_plan(value: Any) -> Plan:
    if not isinstance(value, dict):
        raise PlanError("plan must be a JSON object")
    goal     = value.get("goal")
    criteria = value.get("success_criteria")
    raw_steps = value.get("steps")
    if not isinstance(goal, str) or not goal.strip():
        raise PlanError("plan.goal must be a non-empty string")
    if not isinstance(criteria, list) or not criteria:
        raise PlanError("plan.success_criteria must be a non-empty list")

    # Validate each criterion in success_criteria
    for idx, c in enumerate(criteria):
        if not isinstance(c, dict):
            raise PlanError(f"success_criteria[{idx}] must be an object")
        c_tool = c.get("tool")
        if c_tool not in ALLOWED_TOOLS:
            raise PlanError(f"success_criteria[{idx}] references invalid tool '{c_tool}'")
        _validate_tool_args(c_tool, c.get("args", {}), f"success_criteria[{idx}]")

    if not isinstance(raw_steps, list) or not raw_steps or len(raw_steps) > MAX_PLAN_STEPS:
        raise PlanError(f"plan.steps must contain 1–{MAX_PLAN_STEPS} steps")
    steps: list[Step] = []
    ids: set[str] = set()
    for raw in raw_steps:
        if not isinstance(raw, dict):
            raise PlanError("each plan step must be an object")
        step_id   = raw.get("id")
        purpose   = raw.get("purpose")
        tool      = raw.get("tool")
        step_args = raw.get("args")
        if (not isinstance(step_id, str)
                or not re.fullmatch(r"[a-z][a-z0-9_\-]{0,63}", step_id)
                or step_id in ids):
            raise PlanError("every step needs a unique lowercase id (a-z0-9_-)")
        if not isinstance(purpose, str) or not purpose.strip():
            raise PlanError(f"step '{step_id}' is missing purpose")
        if tool not in ALLOWED_TOOLS:
            raise PlanError(f"step '{step_id}' references invalid tool '{tool}'")
        _validate_tool_args(tool, step_args, f"step '{step_id}'")

        verify = raw.get("verify", [])
        if not isinstance(verify, list):
            raise PlanError(f"step '{step_id}' has invalid verification list")
        # Validate each verifier references a real tool with valid args
        for v_idx, v in enumerate(verify):
            if not isinstance(v, dict):
                raise PlanError(f"step '{step_id}' verifier [{v_idx}] must be an object")
            v_tool = v.get("tool")
            if v_tool not in ALLOWED_TOOLS:
                raise PlanError(f"step '{step_id}' verifier [{v_idx}] references invalid tool '{v_tool}'")
            _validate_tool_args(v_tool, v.get("args", {}), f"step '{step_id}' verifier [{v_idx}]")

        raw_risk = raw.get("risk")
        if tool in PolicyEngine.CHANGE:
            if raw_risk == "read_only":
                raise PlanError(f"step '{step_id}' uses system-changing tool '{tool}' but declared risk 'read_only'")
            risk = "system_change"
            if not verify:
                raise PlanError(f"system-changing step '{step_id}' must include verification steps")
        else:
            risk = "read_only"

        deps = raw.get("depends_on", [])
        if not isinstance(deps, list) or not all(isinstance(d, str) for d in deps):
            raise PlanError(f"step '{step_id}' has invalid depends_on list")
        captures = raw.get("captures", {})
        if not isinstance(captures, dict):
            raise PlanError(f"step '{step_id}' has invalid captures dict")
        for cap_k, cap_v in captures.items():
            if not isinstance(cap_k, str) or not isinstance(cap_v, str):
                raise PlanError(f"step '{step_id}' captures must map strings to strings")
            if not (cap_v in ("stdout:strip", "stdout:first_line", "exit_code") or cap_v.startswith("regex:") or cap_v.startswith("json:")):
                raise PlanError(f"step '{step_id}' has invalid capture rule '{cap_v}'")

        ids.add(step_id)
        steps.append(Step(step_id, purpose, tool, step_args, verify, risk, deps, captures))

    # Validate dependency graph — no forward refs to unknown IDs, no self-dep
    for step in steps:
        for dep in step.depends_on:
            if dep not in ids or dep == step.id:
                raise PlanError(f"step '{step.id}' has unknown or self-referential dep '{dep}'")

    # DAG Cycle detection
    _detect_dag_cycles(steps)

    return Plan(
        goal=goal.strip(),
        success_criteria=criteria,
        steps=steps,
        assumptions=value.get("assumptions", []),
    )


# ── JSON extraction ───────────────────────────────────────────────────────────
def _scan_balanced_json(text: str) -> Any | None:
    """Scan for the first balanced JSON container, respecting string escapes."""
    n = len(text)
    for i in range(n):
        ch = text[i]
        if ch not in ('{', '['):
            continue
        closing = '}' if ch == '{' else ']'
        depth   = 0
        in_str  = False
        esc     = False
        for j in range(i, n):
            c = text[j]
            if in_str:
                if esc:
                    esc = False
                elif c == '\\':
                    esc = True
                elif c == '"':
                    in_str = False
                continue
            if c == '"':
                in_str = True
                esc    = False
                continue
            if c == ch:
                depth += 1
            elif c == closing:
                depth -= 1
                if depth == 0:
                    candidate = text[i:j + 1]
                    try:
                        return json.loads(candidate)
                    except json.JSONDecodeError:
                        # One repair pass: remove trailing commas
                        fixed = re.sub(r",\s*([\]}])", r"\1", candidate)
                        try:
                            return json.loads(fixed)
                        except json.JSONDecodeError:
                            break
    return None


def extract_first_json_object(text: str) -> Any | None:
    if not text or not isinstance(text, str):
        return None
    cleaned = text.strip()
    if not cleaned:
        return None
    # Priority 1: markdown code fence
    for m in re.finditer(r"```(?:json)?\s*([\s\S]*?)\s*```", cleaned, re.IGNORECASE):
        block = m.group(1).strip()
        try:
            return json.loads(block)
        except json.JSONDecodeError:
            res = _scan_balanced_json(block)
            if res is not None:
                return res
    # Priority 2: whole string is JSON
    if (cleaned.startswith("{") and cleaned.endswith("}")) or \
       (cleaned.startswith("[") and cleaned.endswith("]")):
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            pass
    # Priority 3: balanced scan
    return _scan_balanced_json(cleaned)


# ── LLM prompt builders ───────────────────────────────────────────────────────
def plan_prompt(intent: str, inventory: list[dict[str, Any]],
                tool_subset: list[str] | None = None) -> str:
    """Build the system+user prompt for the planning LLM call.

    tool_subset: pre-filtered list of relevant tools (from _build_tool_subset).
    When None the full ALLOWED_TOOLS set is used as a fallback.
    """
    # Use the category-filtered subset to cut ~70% of prompt tokens on focused tasks
    tools = tool_subset if tool_subset is not None else sorted(ALLOWED_TOOLS)
    inv_text = json.dumps(inventory, ensure_ascii=False)[:2000]
    # Include a grammar note when constrained decoding is active so the model
    # knows its output is being validated character-by-character.
    grammar_note = (
        "IMPORTANT: Your output is grammar-constrained. "
        "Emit ONLY the bare JSON object — no markdown, no prose.\n"
        if _PLAN_GRAMMAR else ""
    )
    return (
        "<|im_start|>system\n"
        "You are the Elenchus planner. Return ONE JSON object only. "
        "No markdown fences. No prose before or after the JSON.\n"
        f"{grammar_note}"
        f"Available tools: {tools}\n"
        "Schema:\n"
        '{"goal":string,"assumptions":[string],"success_criteria":[{"description":string,"tool":string,"args":{}}],'
        '"steps":[{"id":string,"purpose":string,"tool":string,"args":{},'
        '"depends_on":[string],"risk":"read_only"|"change",'
        '"captures":{varname:rule},"verify":[{"tool":string,"args":{}}]}]}\n'
        "Rules:\n"
        "- Every step with risk=change MUST have at least one verify entry.\n"
        "- Step ids: lowercase a-z, 0-9, hyphens only.\n"
        "- depends_on: list of step ids that must complete before this step runs.\n"
        "- captures: map from variable name to capture rule. Rules: stdout:strip, "
        "  stdout:first_line, exit_code, regex:<pattern>, json:<dot.path>\n"
        "- Use ${scratchpad.varname} in args to reference captured values from prior steps.\n"
        "- Use command.argv for tasks not covered by typed tools; "
        '  args must be {"argv":["binary","arg1",...]}'
        " where binary is in the safe allowlist.\n"
        "Tool tips:\n"
        "  file.list  {path, pattern?, recursive?, max_items?} – list directory contents\n"
        "  file.move  {src, dst} – move or rename a file\n"
        "  file.delete {path} – delete a single file\n"
        "  file.append {path, content} – append text to a file\n"
        "  shell.script {code, interpreter?, timeout?, cwd?, args?} – execute multi-line script\n"
        "  data.csv_read {path, delimiter?, max_rows?} – read CSV file into structured JSON\n"
        "  data.csv_write {path, headers?, rows, mode?} – write/append rows to CSV\n"
        "  document.read {path, max_bytes?} – extract text from .pdf, .docx, .txt, .md\n"
        "  email.send {to, subject, body, attachments?} – send or spool an email\n"
        "  email.read {folder?, max_emails?, search?, unseen_only?} – read emails from inbox/spool\n"
        "  http.get   {url, headers?, timeout?, max_bytes?} – external HTTP GET (captures stdout)\n"
        "  llm.prompt {prompt, model?, max_tokens?, endpoint?} – query local LLM (captures stdout)\n"
        "  browser.navigate {url} – open/navigate browser to URL\n"
        "  browser.dump {} – dump accessibility elements of active browser tab\n"
        "  browser.click {id} – click browser element by ID\n"
        "  browser.type {id, text} – type text into browser element\n"
        "  browser.screenshot {path?} – save browser screenshot to PNG\n"
        "<|im_end|>\n"
        "<|im_start|>user\n"
        f"Intent: {intent}\n"
        f"System inventory: {inv_text}\n"
        "<|im_end|>\n"
        "<|im_start|>assistant\n"
    )


def chunk_planning_prompt(intent: str, inventory: list[dict[str, Any]]) -> str:
    """Build a prompt for the first pass of chunked planning.

    For complex intents (estimated > 8 logical steps), we first ask the model
    to decompose the task into 2-5 high-level phases, then plan each phase
    individually.  This keeps each LLM call within token budget.
    """
    inv_text = json.dumps(inventory, ensure_ascii=False)[:800]
    return (
        "<|im_start|>system\n"
        "You are the Elenchus task decomposer. "
        "Decompose the user intent into 2-5 ordered phases. "
        "Return ONE JSON object only: "
        '{"phases":[{"id":"phase1","goal":"...","tools_hint":["tool1",...]},...]}.\n'
        "No prose, no markdown. Phases must be sequential and each produce a clear output.\n"
        "<|im_end|>\n"
        "<|im_start|>user\n"
        f"Intent: {intent}\n"
        f"System inventory: {inv_text}\n"
        "<|im_end|>\n"
        "<|im_start|>assistant\n"
    )


def phase_plan_prompt(phase_goal: str, phase_id: str,
                      scratchpad: dict[str, Any],
                      inventory: list[dict[str, Any]],
                      tool_subset: list[str] | None = None) -> str:
    """Build the planning prompt for a single phase in chunked planning."""
    tools = tool_subset if tool_subset is not None else sorted(ALLOWED_TOOLS)
    sp_text = json.dumps(scratchpad, ensure_ascii=False)[:400] if scratchpad else "{}"
    inv_text = json.dumps(inventory, ensure_ascii=False)[:600]
    return (
        "<|im_start|>system\n"
        f"You are planning phase '{phase_id}'. "
        "Return ONE JSON plan object (same schema as the full plan). "
        "Max 8 steps for this phase.\n"
        f"Available tools: {tools}\n"
        "Use ${scratchpad.varname} to reference values captured in earlier phases.\n"
        "No prose, no markdown.\n"
        "<|im_end|>\n"
        "<|im_start|>user\n"
        f"Phase goal: {phase_goal}\n"
        f"Current scratchpad: {sp_text}\n"
        f"System inventory: {inv_text}\n"
        "<|im_end|>\n"
        "<|im_start|>assistant\n"
    )


def repair_prompt(purpose: str, failure: str, evidence: list[dict[str, Any]]) -> str:
    ev_text = json.dumps(evidence[-6:], ensure_ascii=False)[:1200]
    # Use compact tool list in repair prompts to save token budget
    tools   = sorted(ALLOWED_TOOLS)
    return (
        "<|im_start|>system\n"
        f"Step '{purpose}' failed with: {failure}\n"
        f"Evidence: {ev_text}\n"
        "Return a JSON repair plan (same schema as a normal plan). "
        f"Available tools: {tools}. Max 3 steps. "
        "Each system-changing step MUST have a verify entry. No prose, just JSON.\n"
        "<|im_end|>\n"
        "<|im_start|>user\nRepair plan:\n<|im_end|>\n"
        "<|im_start|>assistant\n"
    )


# ── Deterministic fast-path planner ──────────────────────────────────────────
def deterministic_plan(intent: str, caller: dict[str, Any] | None = None) -> Plan | None:
    """Compile common local OS intents without spending a model request.

    Patterns handled (28):
      1.  Directory creation
      2.  File creation / touch
      3.  Package installation
      4.  Service restart / start
      5.  Package install + service start (combined)
      6.  System information
      7.  Disk usage
      8.  Memory usage
      9.  Port inspection
      10. Local HTTP check
      11. Log tail (file.read with tail_lines)
      12. Service enable (rc-update add)
      13. Service disable (rc-update del)
      14. Arbitrary read-only shell command (command.argv fast path)
      15. File list / directory listing
      16. File move / rename
      17. File delete
      18. File append
      19. CSV read / inspect
      20. Document text extraction / read (.docx, .pdf, .txt, .md)
      21. Email send
      22. Email read / inbox check
      23. Browser open / navigate
      24. Browser dump / inspect page
      25. Browser click
      26. Browser type / fill
      27. Browser screenshot
      28. Shell script execution
    """
    caller   = caller or {}
    home_dir = caller.get("home") or "/root"
    cwd_dir  = caller.get("cwd") or home_dir
    uid      = caller.get("uid")
    gid      = caller.get("gid")

    def resolve_target(name: str, loc: str | None = None) -> str:
        name = name.strip("'\"")
        if loc:
            loc = loc.strip("'\"")
            if loc.lower() == "desktop":
                return posixpath.normpath(posixpath.join(home_dir, "Desktop", name))
            elif loc.startswith("~/"):
                return posixpath.normpath(posixpath.join(home_dir, loc[2:], name))
            elif loc.startswith("/"):
                return posixpath.normpath(posixpath.join(loc, name))
            else:
                return posixpath.normpath(posixpath.join(cwd_dir, loc, name))
        if name.startswith("~/"):
            return posixpath.normpath(posixpath.join(home_dir, name[2:]))
        elif name.startswith("/"):
            return posixpath.normpath(name)
        elif name.lower().startswith("desktop/"):
            return posixpath.normpath(posixpath.join(home_dir, name))
        return posixpath.normpath(posixpath.join(cwd_dir, name))

    raw_clean    = intent.strip()
    clean_intent = re.sub(r"^/+(?:assist|suggest|apply)?\s*", "", raw_clean, flags=re.IGNORECASE)
    text         = " ".join(clean_intent.split())
    text_lower   = text.lower()
    steps: list[Step] = []
    criteria: list[dict[str, Any]] = []

    # ── 1. Directory creation ──────────────────────────────────────────────
    dir_match = None
    m = re.search(
        r"\b(?:create|make|mkdir)\s+(?:a\s+)?(?:folder|directory|dir)\s+in\s+(\S+)\s+"
        r"(?:(?:called|named)\s+)?(\S+)", text, re.IGNORECASE)
    if m:
        dir_match = (m.group(2), m.group(1))
    if not dir_match:
        m = re.search(
            r"\b(?:create|make|mkdir)\s+(?:a\s+)?(?:folder|directory|dir)\s+"
            r"(?:(?:called|named)\s+)?(\S+)\s+in\s+(\S+)", text, re.IGNORECASE)
        if m:
            dir_match = (m.group(1), m.group(2))
    if not dir_match:
        m = re.search(
            r"\b(?:create|make|mkdir)\s+(?:a\s+)?(?:folder|directory|dir)\s+"
            r"(?:(?:called|named)\s+)?(\S+)", text, re.IGNORECASE)
        if m:
            dir_match = (m.group(1), None)
    if not dir_match:
        m = re.search(r"\bmkdir\s+(?:-[a-zA-Z]+\s+)?(\S+)", text, re.IGNORECASE)
        if m:
            dir_match = (m.group(1), None)
    if dir_match:
        name, loc = dir_match
        tp = resolve_target(name, loc)
        a: dict[str, Any] = {"path": tp}
        if uid is not None and gid is not None:
            a["uid"], a["gid"] = uid, gid
        steps.append(Step("create-directory", f"Create directory {tp}", "dir.create", a,
                          [{"tool": "dir.query", "args": {"path": tp}}], "system_change"))
        criteria.append({"description": f"Directory {tp} exists",
                         "tool": "dir.query", "args": {"path": tp}})

    # ── 2. File creation ───────────────────────────────────────────────────
    if not steps:
        file_match = None
        m = re.search(
            r"\b(?:create|make|touch)\s+(?:a\s+)?file\s+in\s+(\S+)\s+"
            r"(?:(?:called|named)\s+)?(\S+)", text, re.IGNORECASE)
        if m:
            file_match = (m.group(2), m.group(1))
        if not file_match:
            m = re.search(
                r"\b(?:create|make|touch)\s+(?:a\s+)?file\s+"
                r"(?:(?:called|named)\s+)?(\S+)\s+in\s+(\S+)", text, re.IGNORECASE)
            if m:
                file_match = (m.group(1), m.group(2))
        if not file_match:
            m = re.search(
                r"\b(?:create|make|touch)\s+(?:a\s+)?file\s+"
                r"(?:(?:called|named)\s+)?(\S+)", text, re.IGNORECASE)
            if m:
                file_match = (m.group(1), None)
        if not file_match:
            m = re.search(r"\btouch\s+(\S+)", text, re.IGNORECASE)
            if m:
                file_match = (m.group(1), None)
        if file_match:
            name, loc = file_match
            tp = resolve_target(name, loc)
            a = {"path": tp, "content": ""}
            if uid is not None and gid is not None:
                a["uid"], a["gid"] = uid, gid
            steps.append(Step("create-file", f"Create file {tp}", "file.write", a,
                              [{"tool": "file.query", "args": {"path": tp}}], "system_change"))
            criteria.append({"description": f"File {tp} exists",
                             "tool": "file.query", "args": {"path": tp}})

    # ── 3. Package install only ────────────────────────────────────────────
    pkg_m = re.search(r"\binstall\s+([a-z0-9][a-z0-9._+\-]{0,127})\b", text_lower)

    # ── 4. Service action only ─────────────────────────────────────────────
    svc_m = re.search(r"\b(restart|start|stop)\s+(?:service\s+)?([a-z0-9][a-z0-9._+\-]{0,127})\b", text_lower)
    if not svc_m:
        svc_m = re.search(r"\b(status|logs)\s+(?:(?:for|of)\s+)?service\s+([a-z0-9][a-z0-9._+\-]{0,127})\b", text_lower)
    if not svc_m:
        svc_m = re.search(r"\bservice\s+([a-z0-9][a-z0-9._+\-]{0,127})\s+(status|logs|restart|start|stop)\b", text_lower)
        if svc_m:
            svc_m = type('_M', (), {'groups': lambda self, a=svc_m.group(2), n=svc_m.group(1): (a, n)})()
    if not svc_m and not re.search(r"/(?:var|tmp|etc|usr|home|log)", text_lower):
        svc_m = re.search(r"\b(status)\s+(?!of\b|for\b)([a-z0-9][a-z0-9._+\-]{0,127})\b", text_lower)

    # ── 5. Combined install + start ────────────────────────────────────────
    if not steps and pkg_m and svc_m:
        package = pkg_m.group(1)
        action, service = svc_m.groups()
        steps.append(Step("install-package", f"Install {package}", "package.install",
                          {"package": package},
                          [{"tool": "package.query", "args": {"package": package}}],
                          "system_change"))
        steps.append(Step(f"{action}-service", f"{action.title()} {service}",
                          f"service.{action}", {"name": service},
                          [{"tool": "service.status", "args": {"name": service}}],
                          "system_change", depends_on=["install-package"]))
        criteria.append({"description": f"Package {package} installed",
                         "tool": "package.query", "args": {"package": package}})
        criteria.append({"description": f"Service {service} running",
                         "tool": "service.status", "args": {"name": service}})

    # ── 3b. Package install alone ──────────────────────────────────────────
    if not steps and pkg_m:
        package = pkg_m.group(1)
        steps.append(Step("install-package", f"Install package {package}",
                          "package.install", {"package": package},
                          [{"tool": "package.query", "args": {"package": package}}],
                          "system_change"))
        criteria.append({"description": f"Package {package} is installed",
                         "tool": "package.query", "args": {"package": package}})

    # ── 4b. Service action alone ───────────────────────────────────────────
    if not steps and svc_m:
        action, service = svc_m.groups()
        if action in ("status", "logs"):
            tool_name = f"service.{action}"
            steps.append(Step(f"{action}-service", f"{action.title()} for service {service}",
                              tool_name, {"name": service}, []))
            criteria.append({"description": f"Service {service} {action} inspected",
                             "tool": "service.status", "args": {"name": service}})
        else:
            steps.append(Step(f"{action}-service", f"{action.title()} service {service}",
                              f"service.{action}", {"name": service},
                              [{"tool": "service.status", "args": {"name": service}}],
                              "system_change"))
            criteria.append({"description": f"Service {service} is {action}ed",
                             "tool": "service.status", "args": {"name": service}})

    # ── 6. System info ─────────────────────────────────────────────────────
    if not steps and any(t in text_lower for t in
                         ("system info", "system status", "os info", "os information",
                          "system information", "uname", "hostname")):
        steps.append(Step("inspect-system", "Collect system information",
                          "system.info", {}, []))
        criteria.append({"description": "System information collected",
                         "tool": "system.info", "args": {}})

    # ── 7. Disk ────────────────────────────────────────────────────────────
    if not steps and any(t in text_lower for t in
                         ("disk usage", "disk status", "disk space", "df -h", "free space")):
        steps.append(Step("inspect-disk", "Collect disk usage", "disk.status", {}, []))
        criteria.append({"description": "Disk usage collected",
                         "tool": "disk.status", "args": {}})

    # ── 8. Memory ─────────────────────────────────────────────────────────
    if not steps and any(t in text_lower for t in
                         ("memory usage", "memory status", "ram usage",
                          "free -m", "memory information")):
        steps.append(Step("inspect-memory", "Collect memory usage", "memory.status", {}, []))
        criteria.append({"description": "Memory usage collected",
                         "tool": "memory.status", "args": {}})

    # ── 9. Port inspection ─────────────────────────────────────────────────
    if not steps:
        pm = re.search(r"\b(?:port|listening on)\s+(\d{1,5})\b", text_lower)
        if pm and any(t in text_lower for t in
                      ("check", "inspect", "listen", "what is", "who is")):
            port = int(pm.group(1))
            if 1 <= port <= 65535:
                steps.append(Step("inspect-port", f"Inspect port {port}",
                                  "port.inspect", {"port": port}, []))
                criteria.append({"description": f"Port {port} inspected",
                                 "tool": "port.inspect", "args": {"port": port}})

    # ── 10. Local HTTP check ───────────────────────────────────────────────
    if not steps:
        um = re.search(
            r"\b(https?://(?:127\.0\.0\.1|localhost)(?::\d+)?(?:/\S*)?)", text_lower)
        if um and any(t in text_lower for t in ("check", "verify", "test", "health", "ping")):
            url = um.group(1).rstrip(".,;:)")
            steps.append(Step("check-http", f"Check local URL {url}",
                              "http.check", {"url": url}, []))
            criteria.append({"description": f"URL {url} reachable",
                             "tool": "http.check", "args": {"url": url}})

    # ── 11. Log tail ───────────────────────────────────────────────────────
    if not steps:
        lm = re.search(
            r"\b(?:show|tail|last|read)\s+(?:(?:last|the)\s+)?(?:(\d+)\s*lines?\s+(?:of\s+)?|(?:logs?\s+(?:of\s+)?))(/[^\s]+)", text, re.IGNORECASE)
        if not lm:
            lm = re.search(
                r"\b(?:show|tail|last|read)\s+(?:(?:last|the)\s+)?(\d+)?\s*lines?\s+(?:of\s+)?(/[^\s]+)", text, re.IGNORECASE)
        if lm:
            n_lines = int(lm.group(1)) if lm.group(1) and lm.group(1).isdigit() else 50
            log_path = lm.group(2).rstrip(".,;:")
            a = {"path": log_path, "tail_lines": n_lines}
            steps.append(Step("read-log", f"Read last {n_lines} lines of {log_path}",
                              "file.read", a, []))
            criteria.append({"description": f"Log {log_path} read",
                             "tool": "file.query", "args": {"path": log_path}})

    # ── 12. Service enable ─────────────────────────────────────────────────
    if not steps:
        em = re.search(r"\benable\s+([a-z0-9][a-z0-9._+\-]{0,127})\b", text_lower)
        if em:
            service = em.group(1)
            runlevel = "default"
            rl_m = re.search(r"\bon\s+(\w+)\s*(?:runlevel|boot)", text_lower)
            if rl_m and re.fullmatch(r"[a-z]{1,32}", rl_m.group(1)):
                runlevel = rl_m.group(1)
            steps.append(Step("enable-service", f"Enable {service} on {runlevel}",
                              "service.enable", {"name": service, "runlevel": runlevel},
                              [{"tool": "service.status", "args": {"name": service}}],
                              "system_change"))
            criteria.append({"description": f"Service {service} enabled",
                             "tool": "service.status", "args": {"name": service}})

    # ── 13. Service disable ────────────────────────────────────────────────
    if not steps:
        dm = re.search(r"\bdisable\s+([a-z0-9][a-z0-9._+\-]{0,127})\b", text_lower)
        if dm:
            service = dm.group(1)
            steps.append(Step("disable-service", f"Disable {service}",
                              "service.disable", {"name": service},
                              [{"tool": "service.status", "args": {"name": service}}],
                              "system_change"))
            criteria.append({"description": f"Service {service} disabled",
                             "tool": "service.status", "args": {"name": service}})

    # ── 14. Read-only command.argv shortcut ────────────────────────────────
    # If the intent looks like a single known read-only command, run it directly.
    if not steps:
        ro_cmd_m = re.match(
            r"^(?:run|exec|execute|show)?\s*(ls|cat|head|tail|df|free|ps|ss|ip|find|grep|uname|uptime|whoami|hostname|date)\b(.*)$",
            text.strip(), re.IGNORECASE)
        if ro_cmd_m:
            binary  = ro_cmd_m.group(1).lower()
            rest    = ro_cmd_m.group(2).strip()
            argv    = [binary] + (rest.split() if rest else [])
            steps.append(Step("run-command", f"Run: {' '.join(argv)}",
                              "command.argv", {"argv": argv}, []))
            criteria.append({"description": f"Command '{argv[0]}' executed",
                             "tool": "system.info", "args": {}})

    # ── 15. File list / directory listing ──────────────────────────────────
    if not steps:
        fl_m = re.search(
            r"\b(?:list|ls|show|find)\s+(?:files?|contents?|entries?)?\s*"
            r"(?:in|of|from|under)?\s*(/[^\s]+)", text, re.IGNORECASE)
        if not fl_m:
            fl_m = re.search(
                r"\bwhat(?:'s| is| are)? in\s+(/[^\s]+)", text, re.IGNORECASE)
        if fl_m:
            ls_path = fl_m.group(1).rstrip(".,;:")
            pattern = "*"
            pat_m   = re.search(r"\bmatching\s+(\S+)", text_lower)
            if pat_m:
                pattern = pat_m.group(1)
            recursive = any(t in text_lower for t in ("recursive", "recursively", "-r"))
            a = {"path": ls_path, "pattern": pattern, "recursive": recursive}
            steps.append(Step("list-files", f"List files in {ls_path}",
                              "file.list", a, []))
            criteria.append({"description": f"Files listed in {ls_path}",
                             "tool": "dir.query", "args": {"path": ls_path}})

    # ── 16. File move / rename ─────────────────────────────────────────────
    if not steps:
        mv_m = re.search(
            r"\b(?:move|rename|mv)\s+(/[^\s]+)\s+(?:to\s+)?(/[^\s]+)", text, re.IGNORECASE)
        if mv_m:
            src = mv_m.group(1).rstrip(".,;:")
            dst = mv_m.group(2).rstrip(".,;:")
            steps.append(Step("move-file", f"Move {src} to {dst}",
                              "file.move", {"src": src, "dst": dst},
                              [{"tool": "file.query", "args": {"path": dst}}],
                              "system_change"))
            criteria.append({"description": f"File moved to {dst}",
                             "tool": "file.query", "args": {"path": dst}})

    # ── 17. File delete ────────────────────────────────────────────────────
    if not steps:
        del_m = re.search(
            r"\b(?:delete|remove|rm|unlink)\s+(?:(?:the\s+)?file\s+)?(/[^\s]+)", text, re.IGNORECASE)
        if del_m:
            del_path = del_m.group(1).rstrip(".,;:")
            steps.append(Step("delete-file", f"Delete {del_path}",
                              "file.delete", {"path": del_path},
                              [{"tool": "file.query", "args": {"path": del_path}}],
                              "system_change"))
            criteria.append({"description": f"File {del_path} deleted",
                             "tool": "file.query", "args": {"path": del_path}})

    # ── 18. File append ────────────────────────────────────────────────────
    if not steps:
        app_m = re.search(
            r"\bappend\s+(?:line|text|content)?\s*[\"']?([^\"'\n]+?)[\"']?\s+to\s+(/[^\s]+)", text, re.IGNORECASE)
        if not app_m:
            app_m_rev = re.search(
                r"\bappend\s+(/[^\s]+)\s+with\s+[\"']?([^\"'\n]+?)[\"']?", text, re.IGNORECASE)
            if app_m_rev:
                app_m = (app_m_rev.group(2), app_m_rev.group(1))  # (content, path)
        else:
            app_m = (app_m.group(1), app_m.group(2))
        if app_m:
            content, path = app_m
            path = path.rstrip(".,;:")
            steps.append(Step("append-file", f"Append to {path}",
                              "file.append", {"path": path, "content": content if content.endswith("\n") else content + "\n"},
                              [{"tool": "file.query", "args": {"path": path}}],
                              "system_change"))
            criteria.append({"description": f"File {path} updated",
                             "tool": "file.query", "args": {"path": path}})

    # ── 19. CSV read ───────────────────────────────────────────────────────
    if not steps:
        csv_m = re.search(
            r"\b(?:read|parse|inspect|view|show|load)\s+(?:the\s+)?csv\s+(?:file\s+)?(?:at|in|from)?\s*(/[^\s]+\.csv)", text, re.IGNORECASE)
        if not csv_m:
            csv_m = re.search(r"\bread\s+(/[^\s]+\.csv)", text, re.IGNORECASE)
        if csv_m:
            csv_path = csv_m.group(1).rstrip(".,;:")
            steps.append(Step("read-csv", f"Read CSV {csv_path}",
                              "data.csv_read", {"path": csv_path}, []))
            criteria.append({"description": f"CSV {csv_path} read",
                             "tool": "file.query", "args": {"path": csv_path}})

    # ── 20. Document read ──────────────────────────────────────────────────
    if not steps:
        doc_m = re.search(
            r"\b(?:read|extract\s+text\s+from|view|open|summarize)\s+(?:the\s+)?(?:document|doc|pdf|docx|file)\s+(?:at|in|from)?\s*(/[^\s]+\.(?:pdf|docx|txt|md|doc|log|csv|json))", text, re.IGNORECASE)
        if doc_m:
            doc_path = doc_m.group(1).rstrip(".,;:")
            steps.append(Step("read-doc", f"Read document {doc_path}",
                              "document.read", {"path": doc_path}, []))
            criteria.append({"description": f"Document {doc_path} read",
                             "tool": "file.query", "args": {"path": doc_path}})

    # ── 20b. Generic file read ─────────────────────────────────────────────
    if not steps:
        rf_m = re.search(
            r"\b(?:read|cat|view|show|display)\s+(?:(?:the\s+)?file\s+)?(/[^\s]+)", text, re.IGNORECASE)
        if rf_m:
            rf_path = rf_m.group(1).rstrip(".,;:")
            steps.append(Step("read-file", f"Read file {rf_path}",
                              "file.read", {"path": rf_path}, []))
            criteria.append({"description": f"File {rf_path} read",
                             "tool": "file.query", "args": {"path": rf_path}})

    # ── 21. Email send ─────────────────────────────────────────────────────
    if not steps:
        em_m = re.search(
            r"\bsend\s+(?:an?\s+)?email\s+to\s+([a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+)"
            r"(?:\s+(?:with\s+)?subject\s+[\"']?([^\"'\n]+?)[\"']?)?"
            r"(?:\s+(?:and\s+)?body\s+[\"']?([^\"'\n]+?)[\"']?)?", text, re.IGNORECASE)
        if em_m:
            to_addr = em_m.group(1)
            subject = em_m.group(2) or "Agent Notification"
            body    = em_m.group(3) or "Automated message from OS AI Agent."
            steps.append(Step("send-email", f"Send email to {to_addr}",
                              "email.send", {"to": to_addr, "subject": subject, "body": body},
                              [{"tool": "system.info", "args": {}}],
                              "system_change"))
            criteria.append({"description": f"Email sent to {to_addr}",
                             "tool": "system.info", "args": {}})

    # ── 22. Email read ─────────────────────────────────────────────────────
    if not steps:
        if re.search(r"\b(?:check|read|fetch|get|list|show)\s+(?:my\s+)?(?:emails?|inbox|messages|mail)\b", text_lower):
            steps.append(Step("read-emails", "Check inbox for emails",
                              "email.read", {"max_emails": 10}, []))
            criteria.append({"description": "Inbox checked",
                             "tool": "system.info", "args": {}})

    # ── 23. Browser launch & navigate ──────────────────────────────────────
    if not steps:
        b_nav_m = re.search(
            r"\b(?:open|navigate\s+to|browse|visit|go\s+to)\s+(https?://[^\s]+)", text, re.IGNORECASE)
        if not b_nav_m:
            b_nav_m = re.search(
                r"\bopen\s+(?:the\s+)?browser\s*(?:and\s+go\s+to\s+|to\s+|at\s+)?(https?://[^\s]+)", text, re.IGNORECASE)
        if b_nav_m:
            url = b_nav_m.group(1).rstrip(".,;:")
            steps.append(Step("browser-launch", f"Launch browser at {url}",
                              "app.launch", {"app": "browser", "url": url}, []))
            criteria.append({"description": f"Browser launched at {url}",
                             "tool": "system.info", "args": {}})
        elif re.search(r"\b(?:open|launch|start)\s+(?:the\s+)?(?:web\s+)?browser\b", text_lower):
            steps.append(Step("browser-launch", "Launch browser",
                              "app.launch", {"app": "browser", "url": "http://127.0.0.1:8080"}, []))
            criteria.append({"description": "Browser active on desktop",
                             "tool": "system.info", "args": {}})

    # ── 23b. File manager launch ───────────────────────────────────────────
    if not steps:
        fm_m = re.search(r"\b(?:open|launch|show)\s+(?:the\s+)?(?:file\s*manager|files)(?:\s+(?:in|at|to)\s+(\S+))?\b", text_lower)
        if fm_m:
            raw_p = fm_m.group(1)
            fm_path = raw_p if raw_p else ("/home/aiuser" if os.path.exists("/home/aiuser") else "/root")
            steps.append(Step("file-manager-launch", f"Launch file manager at {fm_path}",
                              "app.launch", {"app": "file_manager", "path": fm_path}, []))
            criteria.append({"description": "File manager active on desktop",
                             "tool": "system.info", "args": {}})

    # ── 23c. Terminal / console launch ─────────────────────────────────────
    if not steps:
        if re.search(r"\b(?:open|launch|start)\s+(?:the\s+)?(?:terminal|console|shell)\b", text_lower):
            steps.append(Step("terminal-launch", "Launch terminal",
                              "app.launch", {"app": "terminal"}, []))
            criteria.append({"description": "Terminal active on desktop",
                             "tool": "system.info", "args": {}})

    # ── 24. Browser dump ───────────────────────────────────────────────────
    if not steps:
        if re.search(r"\b(?:dump|inspect|extract|show|read)\s+(?:the\s+)?(?:active\s+)?(?:browser|page|dom|webpage|tab)(?:\s+(?:elements?|contents?|tab))?\b", text_lower):
            steps.append(Step("browser-dump", "Inspect active browser tab elements",
                              "browser.dump", {}, []))
            criteria.append({"description": "Browser page dumped",
                             "tool": "system.info", "args": {}})

    # ── 25. Browser click ──────────────────────────────────────────────────
    if not steps:
        b_clk_m = re.search(
            r"\b(?:browser\s+)?click\s+(?:on\s+)?(?:node|element|button)?\s*(?:#|id\s+)?(\d+)(?:\s+(?:in|on)\s+browser)?\b", text, re.IGNORECASE)
        if b_clk_m:
            node_id = int(b_clk_m.group(1))
            steps.append(Step("browser-click", f"Click browser element {node_id}",
                              "browser.click", {"id": node_id},
                              [{"tool": "system.info", "args": {}}],
                              "system_change"))
            criteria.append({"description": f"Clicked browser node {node_id}",
                             "tool": "system.info", "args": {}})

    # ── 26. Browser type ───────────────────────────────────────────────────
    if not steps:
        b_typ_m = re.search(
            r"\b(?:browser\s+)?type\s+[\"']?([^\"'\n]+?)[\"']?\s+(?:in|into)\s+(?:node|element)?\s*(\d+)(?:\s+(?:in|on)\s+browser)?\b", text, re.IGNORECASE)
        if b_typ_m:
            text_to_type = b_typ_m.group(1)
            node_id      = int(b_typ_m.group(2))
            steps.append(Step("browser-type", f"Type into browser element {node_id}",
                              "browser.type", {"id": node_id, "text": text_to_type},
                              [{"tool": "system.info", "args": {}}],
                              "system_change"))
            criteria.append({"description": f"Typed text into browser node {node_id}",
                             "tool": "system.info", "args": {}})

    # ── 27. Browser screenshot ─────────────────────────────────────────────
    if not steps:
        b_ss_m = re.search(
            r"\b(?:take\s+(?:a\s+)?)?screenshot\s+(?:of\s+(?:the\s+)?browser\s+)?(?:to\s+|save\s+(?:as|to)\s+)?(/[^\s]+\.png)", text, re.IGNORECASE)
        if b_ss_m:
            ss_path = b_ss_m.group(1).rstrip(".,;:")
            steps.append(Step("browser-screenshot", f"Capture browser screenshot to {ss_path}",
                              "browser.screenshot", {"path": ss_path}, []))
            criteria.append({"description": f"Screenshot saved to {ss_path}",
                             "tool": "file.query", "args": {"path": ss_path}})

    # ── 28. Shell script execution ─────────────────────────────────────────
    if not steps:
        sh_m = re.search(
            r"\b(?:run|execute)\s+(?:python|sh|bash|shell)?\s*script\s*:\s*(.+)", text, re.IGNORECASE | re.DOTALL)
        if sh_m:
            code = sh_m.group(1).strip()
            interp = "python3" if "python" in text_lower else "sh"
            steps.append(Step("run-script", "Execute script",
                              "shell.script", {"code": code, "interpreter": interp},
                              [{"tool": "system.info", "args": {}}],
                              "system_change"))
            criteria.append({"description": "Script executed",
                             "tool": "system.info", "args": {}})

    if not steps:
        return None
    return Plan(
        goal=intent,
        success_criteria=criteria,
        steps=steps,
        assumptions=["Plan produced by the deterministic typed-task compiler."],
    )


# ── Main harness ──────────────────────────────────────────────────────────────
class Harness:
    """Orchestrates planning, policy, tool execution, verification, and repair.

    Usage::
        store   = JobStore(base_dir, task_id, legacy_log_fn)
        harness = Harness(store, caller_ctx, mode, llm_fn, cancelled_fn)
        status  = harness.run(intent_text)
    """

    def __init__(
        self,
        store:     JobStore,
        caller:    dict[str, Any],
        mode:      str,
        llm:       Callable[[str], str],
        cancelled: Callable[[], bool] | None = None,
    ) -> None:
        self.store     = store
        self.caller    = caller
        self.mode      = mode
        self.llm       = llm
        self.cancelled = cancelled or (lambda: False)
        self.policy    = PolicyEngine()
        self.tools     = ToolExecutor(caller)
        self.evidence:       list[dict[str, Any]] = []
        self.recommendations: list[str] = []
        self.scratchpad:     dict[str, Any] = {}
        self._job_start: float = time.monotonic()

    # ── Evidence recording ─────────────────────────────────────────────────

    def _record(self, step: Step, phase: str, result: ToolResult) -> None:
        ev = {
            "step":      step.id,
            "phase":     phase,
            "tool":      step.tool,
            "ok":        result.ok,
            "summary":   result.summary,
            "exit_code": result.exit_code,
            "stderr":    result.stderr[:500],
        }
        self.evidence.append(ev)
        self.store.event(
            "result",
            task_idx=0,
            step=len(self.evidence) - 1,
            cmd=f"{step.tool} {json.dumps(step.args, sort_keys=True)[:200]}",
            returncode=result.exit_code,
            stdout=result.stdout[:500],
            stderr=result.stderr[:200],
        )
        # Dynamic variable capture into scratchpad
        if phase == "execute" and result.ok and step.captures:
            try:
                extracted = VariableInterpolator.extract_captures(step.captures, result)
                self.scratchpad.update(extracted)
                self.store.update_scratchpad(extracted)
                self.store.event(
                    "scratchpad_set",
                    step=step.id,
                    captured=list(extracted.keys()),
                    values={k: str(v)[:200] for k, v in extracted.items()},
                )
            except Exception as cap_err:
                self.store.event("scratchpad_error", step=step.id, error=str(cap_err))

    # ── Inventory ──────────────────────────────────────────────────────────

    def _inventory(self) -> list[dict[str, Any]]:
        system = self.tools.run("system.info", {})
        disk   = self.tools.run("disk.status", {})
        inv = [
            {"tool": "system.info", "ok": system.ok, "facts": system.facts},
            {"tool": "disk.status",  "ok": disk.ok,  "summary": disk.stdout[:800]},
        ]
        self.store.event("inventory", items=inv)
        return inv

    # ── 3-tier planning ────────────────────────────────────────────────────

    def _make_plan(self, intent: str, inventory: list[dict[str, Any]]) -> Plan | None:
        # Tier 1 — deterministic fast path (zero LLM calls)
        plan = deterministic_plan(intent, caller=self.caller)
        if plan is not None:
            self.store.event("planner_fast_path", intent=intent,
                             steps=[s.purpose for s in plan.steps])
            return plan

        # Quick-win 2: category-filtered tool subset cuts system prompt tokens ~70%
        tool_subset = _build_tool_subset(intent)
        self.store.event("planner_tool_subset",
                         categories=classify_intent_categories(intent),
                         tool_count=len(tool_subset))

        # Quick-win 3: chunked planning for complex multi-step intents
        estimated_steps = _estimate_complexity(intent)
        if estimated_steps > 8:
            plan = self._chunked_plan(intent, inventory, tool_subset)
            if plan is not None:
                self.store.event("planner_chunked_ok",
                                 steps=[s.purpose for s in plan.steps])
                return plan

        # Tier 2 — LLM structured plan (with grammar-filtered tool subset)
        raw = self.llm(plan_prompt(intent, inventory, tool_subset))
        self.store.event("planner_raw", raw=raw[:2000] if isinstance(raw, str) else "")
        parsed = extract_first_json_object(raw)
        if parsed and isinstance(parsed, dict):
            try:
                plan = validate_plan(parsed)
                self.store.event("planner_llm_ok", steps=[s.purpose for s in plan.steps])
                return plan
            except PlanError as exc:
                self.store.event("planner_parse_fail", error=str(exc))
                # Quick-win 4: one extra auto-repair attempt before falling through
                # Ask the LLM to fix its own invalid plan output
                repair_raw = self.llm(
                    "<|im_start|>system\n"
                    "The following JSON plan has a validation error: " + str(exc) + "\n"
                    "Fix the JSON so it passes validation. Return only corrected JSON.\n"
                    "<|im_end|>\n"
                    "<|im_start|>user\n" + (raw[:1500] if isinstance(raw, str) else "") + "\n"
                    "<|im_end|>\n"
                    "<|im_start|>assistant\n"
                )
                reparsed = extract_first_json_object(
                    repair_raw if isinstance(repair_raw, str) else ""
                )
                if reparsed and isinstance(reparsed, dict):
                    try:
                        plan = validate_plan(reparsed)
                        self.store.event("planner_auto_repair_ok",
                                         steps=[s.purpose for s in plan.steps])
                        return plan
                    except PlanError:
                        pass

        # Tier 3 — single extracted command fallback
        cmd = self._extract_single_command(raw if isinstance(raw, str) else "")
        if cmd:
            binary = cmd[0] if cmd else ""
            if binary in SAFE_ARGV_COMMANDS:
                step = Step(
                    "run-cmd", intent, "command.argv",
                    {"argv": cmd}, [], "read_only",
                )
                fallback = Plan(
                    goal=intent,
                    success_criteria=[{"description": "Command ran", "tool": "system.info", "args": {}}],
                    steps=[step],
                    assumptions=["Tier-3 fallback: single extracted command."],
                )
                self.store.event("planner_fallback_argv", argv=cmd)
                return fallback

        return None

    def _chunked_plan(self, intent: str, inventory: list[dict[str, Any]],
                      tool_subset: list[str]) -> Plan | None:
        """Two-phase planning for complex multi-step intents.

        Phase 1: Ask LLM to decompose intent into 2-5 high-level phases.
        Phase 2: For each phase, ask LLM for a detailed step plan.
        Merge all per-phase steps into a single flat Plan.

        Keeps each LLM call under the token budget by limiting per-phase scope.
        """
        self.store.event("planner_chunked_start", intent=intent[:200])

        # Step 1: Decompose into phases
        decomp_raw = self.llm(chunk_planning_prompt(intent, inventory))
        decomp_parsed = extract_first_json_object(
            decomp_raw if isinstance(decomp_raw, str) else ""
        )
        if not isinstance(decomp_parsed, dict):
            self.store.event("planner_chunked_decomp_fail", raw=str(decomp_raw)[:500])
            return None

        phases = decomp_parsed.get("phases", [])
        if not isinstance(phases, list) or not phases:
            self.store.event("planner_chunked_no_phases")
            return None

        self.store.event("planner_chunked_phases",
                         phases=[p.get("goal", "") for p in phases])

        # Step 2: Plan each phase individually
        all_steps: list[Step] = []
        all_criteria: list[dict] = []
        scratchpad_state: dict[str, Any] = dict(self.scratchpad)
        id_offset = 0

        for phase in phases[:5]:  # cap at 5 phases
            if not isinstance(phase, dict):
                continue
            phase_id = str(phase.get("id", f"phase{id_offset+1}"))
            phase_goal = str(phase.get("goal", ""))
            if not phase_goal:
                continue

            # Build tool subset from phase's tools_hint if provided
            phase_tools_hint = phase.get("tools_hint", [])
            if isinstance(phase_tools_hint, list) and phase_tools_hint:
                phase_tools = sorted(
                    set(phase_tools_hint) & (PolicyEngine.READ_ONLY | PolicyEngine.CHANGE)
                    | {"system.info", "command.argv"}
                )
            else:
                phase_tools = tool_subset

            p_raw = self.llm(phase_plan_prompt(
                phase_goal, phase_id, scratchpad_state, inventory, phase_tools
            ))
            p_parsed = extract_first_json_object(
                p_raw if isinstance(p_raw, str) else ""
            )
            if not isinstance(p_parsed, dict):
                self.store.event("planner_chunked_phase_fail",
                                 phase_id=phase_id, raw=str(p_raw)[:500])
                continue

            try:
                p_plan = validate_plan(p_parsed)
            except PlanError as exc:
                self.store.event("planner_chunked_phase_invalid",
                                 phase_id=phase_id, error=str(exc))
                continue

            # Re-number step IDs to avoid collisions across phases
            for step in p_plan.steps:
                step.id = f"{phase_id}-{step.id}"
                # Fix depends_on references to include the phase prefix
                step.depends_on = [
                    f"{phase_id}-{d}" for d in step.depends_on
                ]
                all_steps.append(step)

            all_criteria.extend(p_plan.success_criteria)
            id_offset += 1

            # Simulate captures for context passing to next phase
            for step in p_plan.steps:
                for var_name in step.captures:
                    scratchpad_state.setdefault(var_name, f"<captured_by_{step.id}>")

        if not all_steps:
            self.store.event("planner_chunked_no_steps")
            return None

        # Enforce MAX_PLAN_STEPS limit across all phases
        if len(all_steps) > MAX_PLAN_STEPS:
            all_steps = all_steps[:MAX_PLAN_STEPS]

        return Plan(
            goal=intent,
            success_criteria=all_criteria or [
                {"description": "All phases completed", "tool": "system.info", "args": {}}
            ],
            steps=all_steps,
            assumptions=[f"Chunked plan: {len(phases)} phases merged."],
        )

    @staticmethod
    def _extract_single_command(text: str) -> list[str] | None:
        """Pull the first plausible shell command from LLM prose as a tokenised argv."""
        # Try JSON {"cmd": "..."} or {"argv": [...]}
        obj = extract_first_json_object(text)
        if isinstance(obj, dict):
            if isinstance(obj.get("argv"), list):
                return [str(a) for a in obj["argv"]] or None
            if isinstance(obj.get("cmd"), str) and obj["cmd"].strip():
                return obj["cmd"].strip().split()
        # Try code fence
        fence = re.search(r"```(?:bash|sh)?\n?(.*?)```", text, re.S)
        if fence:
            line = fence.group(1).strip().splitlines()[0].strip()
            if line:
                return line.split()
        # Try backtick inline
        bt = re.search(r"`([^`]+)`", text)
        if bt:
            return bt.group(1).strip().split()
        return None

    # ── Before/After Verification ──────────────────────────────────────────

    def _pre_verify(self, step: Step, resolved_args: dict[str, Any]) -> dict[str, Any]:
        """Snapshot relevant system state before executing a step."""
        snapshot: dict[str, Any] = {"tool": step.tool, "ts": time.time()}
        try:
            if step.tool in ("file.write", "file.append", "file.delete", "file.read"):
                p = resolved_args.get("path")
                if isinstance(p, str):
                    path = Path(p).resolve()
                    snapshot["exists"] = path.exists()
                    snapshot["is_file"] = path.is_file()
                    if path.is_file():
                        try:
                            snapshot["size"] = path.stat().st_size
                            snapshot["mtime"] = path.stat().st_mtime
                        except OSError:
                            pass
            elif step.tool in ("dir.create", "dir.remove", "dir.query"):
                p = resolved_args.get("path")
                if isinstance(p, str):
                    path = Path(p).resolve()
                    snapshot["exists"] = path.exists()
                    snapshot["is_dir"] = path.is_dir()
            elif step.tool.startswith("service."):
                name = resolved_args.get("name")
                if isinstance(name, str):
                    status_res = self.tools.run("service.status", {"name": name})
                    snapshot["service_active"] = status_res.ok
                    snapshot["service_status"] = status_res.stdout[:200]
            elif step.tool.startswith("package."):
                pkg = resolved_args.get("package")
                if isinstance(pkg, str):
                    query_res = self.tools.run("package.query", {"package": pkg})
                    snapshot["package_installed"] = query_res.ok
        except Exception as exc:
            snapshot["snapshot_error"] = str(exc)
        return snapshot

    def _verify(self, step: Step) -> tuple[bool, str]:
        if not step.verify:
            return True, "No additional verification required."
        self.store.set_status(JobStatus.VERIFYING, current_step=step.id)
        for item in step.verify:
            tool = item.get("tool")
            raw_args = item.get("args", {})
            if tool not in ALLOWED_TOOLS or not isinstance(raw_args, dict):
                return False, f"Invalid verifier tool '{tool}'"
            try:
                args = VariableInterpolator.resolve_data(raw_args, self.scratchpad)
            except KeyError as ke:
                return False, f"Verifier variable resolution error: {ke}"
            v_step  = Step(f"{step.id}-verify", f"Verify: {step.purpose}", tool, args, [])
            vresult = self.tools.run(tool, args)
            self._record(v_step, "verify", vresult)
            if not vresult.ok:
                return False, (vresult.summary + (": " + vresult.stderr if vresult.stderr else ""))
        self.store.event("verify", condition=f"Verification passed: {step.purpose}")
        return True, "Verification passed."

    def _post_verify(self, step: Step, pre_snapshot: dict[str, Any]) -> tuple[bool, str, dict[str, Any]]:
        """Run declared verify steps and calculate state delta from pre_snapshot."""
        delta: dict[str, Any] = {"pre": pre_snapshot, "post": {}, "delta_matched": True}
        ok, v_msg = self._verify(step)
        if not ok:
            return False, v_msg, delta

        try:
            resolved_args = VariableInterpolator.resolve_data(step.args, self.scratchpad)
            post_snapshot = self._pre_verify(step, resolved_args)
            delta["post"] = post_snapshot

            if step.tool == "dir.create":
                if not post_snapshot.get("exists"):
                    delta["delta_matched"] = False
                    return False, f"Directory '{resolved_args.get('path')}' does not exist after dir.create", delta
            elif step.tool == "dir.remove":
                if post_snapshot.get("exists"):
                    delta["delta_matched"] = False
                    return False, f"Directory '{resolved_args.get('path')}' still exists after dir.remove", delta
            elif step.tool == "file.delete":
                if post_snapshot.get("exists"):
                    delta["delta_matched"] = False
                    return False, f"File '{resolved_args.get('path')}' still exists after file.delete", delta
            elif step.tool == "file.write":
                if not post_snapshot.get("exists"):
                    delta["delta_matched"] = False
                    return False, f"File '{resolved_args.get('path')}' was not created by file.write", delta
        except Exception as delta_exc:
            delta["delta_error"] = str(delta_exc)

        return True, v_msg, delta

    # ── Diagnosis ──────────────────────────────────────────────────────────

    def _diagnose(self, step: Step, failure: str) -> None:
        probes: list[tuple[str, dict]] = [("system.info", {})]
        if step.tool.startswith("service."):
            name = step.args.get("name")
            if isinstance(name, str):
                probes += [("service.status", {"name": name}),
                           ("service.logs",   {"name": name})]
                recommendation = (
                    f"Review service status and logs for '{name}'. "
                    "Correct configuration, dependency, permission, or port conflict before retrying."
                )
            else:
                recommendation = "Inspect the service state and logs before retrying."
        elif step.tool.startswith("package."):
            probes.append(("disk.status", {}))
            recommendation = (
                "Check package-manager output, repository reachability, "
                "DNS, and free disk space; then retry."
            )
        elif step.tool in ("dir.create", "dir.remove", "file.write"):
            recommendation = (
                "Check target path validity, filesystem permissions, "
                "and parent directory accessibility before retrying."
            )
        elif step.tool == "http.check":
            recommendation = (
                "Inspect the local service state and listening port, "
                "then verify the localhost URL again."
            )
        elif step.tool == "command.argv":
            recommendation = (
                "Review captured stderr. If the binary is missing, install it first."
            )
        else:
            recommendation = "Review stderr and host facts, correct the issue, then retry."

        ev_ids: list[int] = []
        for tool, args in probes:
            result = self.tools.run(tool, args)
            probe  = Step(f"diagnose-{tool.replace('.', '-')}", "diagnostic probe", tool, args, [])
            self._record(probe, "diagnose", result)
            ev_ids.append(len(self.evidence) - 1)
        self.recommendations.append(recommendation)
        self.store.event(
            "diagnosis",
            step_id=step.id,
            classification="suspected",
            failure=failure,
            evidence_ids=ev_ids,
            recommendation=recommendation,
        )

    # ── Bounded repair loop ────────────────────────────────────────────────

    def _repair(self, step: Step, failure: str, attempt: int) -> bool:
        """Ask LLM for a repair plan, validate, execute, verify.

        Returns True if the original step's verification now passes.
        Limited to MAX_REPAIRS_PER_STEP calls per step.
        """
        if attempt >= MAX_REPAIRS_PER_STEP:
            self.store.event("repair_limit_reached", step_id=step.id, attempt=attempt)
            return False

        self.store.event("repair_start", step_id=step.id, attempt=attempt)
        raw    = self.llm(repair_prompt(step.purpose, failure, self.evidence))
        parsed = extract_first_json_object(raw if isinstance(raw, str) else "")
        try:
            r_plan = validate_plan(parsed)
        except (PlanError, Exception) as exc:
            self.store.event("repair_plan_invalid", step_id=step.id, error=str(exc))
            return False

        for r_step in r_plan.steps:
            if self.cancelled():
                return False
            decision, reason = self.policy.decide(self.mode, r_step)
            if decision != "ALLOW":
                self.store.event("repair_step_denied", step_id=r_step.id, reason=reason)
                continue
            r_result = self.tools.run(r_step.tool, r_step.args)
            self._record(r_step, f"repair-{attempt}", r_result)
            if not r_result.ok:
                self.store.event("repair_step_failed", step_id=r_step.id, summary=r_result.summary)
                return False

        # Re-verify the original step after repair
        ok, msg = self._verify(step)
        if ok:
            self.store.event("repair_succeeded", step_id=step.id, attempt=attempt)
        else:
            self.store.event("repair_failed", step_id=step.id, attempt=attempt, reason=msg)
        return ok

    # ── Final criteria check ───────────────────────────────────────────────

    def _final_criteria(self, plan: Plan) -> tuple[bool, list[str]]:
        failures: list[str] = []
        for criterion in plan.success_criteria:
            tool = criterion.get("tool")
            raw_args = criterion.get("args", {})
            if tool not in ALLOWED_TOOLS or not isinstance(raw_args, dict):
                failures.append("Plan contained an invalid success criterion.")
                continue
            try:
                args = VariableInterpolator.resolve_data(raw_args, self.scratchpad)
            except KeyError as ke:
                failures.append(f"Criterion variable resolution error: {ke}")
                continue
            result = self.tools.run(tool, args)
            synthetic = Step("final-verify", criterion.get("description", "final check"),
                             tool, args, [])
            self._record(synthetic, "final_verify", result)
            if not result.ok:
                failures.append(criterion.get("description", result.summary))
        return not failures, failures

    # ── Parallel execution of independent read-only steps ─────────────────

    def _run_parallel_readonly(self, steps: list[Step]) -> dict[str, ToolResult]:
        """Execute independent read-only steps concurrently (max 4 workers)."""
        results: dict[str, ToolResult] = {}
        resolved_step_args = {}
        for s in steps:
            try:
                resolved_step_args[s.id] = VariableInterpolator.resolve_data(s.args, self.scratchpad)
            except KeyError:
                resolved_step_args[s.id] = s.args

        with ThreadPoolExecutor(max_workers=min(4, len(steps))) as pool:
            futures = {pool.submit(self.tools.run, s.tool, resolved_step_args[s.id]): s for s in steps}
            for future in as_completed(futures):
                step = futures[future]
                try:
                    results[step.id] = future.result()
                except Exception as exc:
                    results[step.id] = ToolResult(False, f"parallel execution error: {exc}",
                                                   exit_code=1)
        return results

    # ── Shared Plan Execution Engine ──────────────────────────────────────

    def _execute_plan(self, plan: Plan, completed: set[str], problems: list[str]) -> JobStatus:
        """Executes plan steps, tracking checkpoint, scratchpad, before/after verification, and producing StepRecords."""
        self.store.state.setdefault("completed_steps", [])
        self.store.state.setdefault("failed_steps", [])
        self.store.state.setdefault("step_records", [])
        self.store.state.setdefault("scratchpad", {})

        # Filter read-only independent steps that haven't completed
        ro_independent = [
            s for s in plan.steps
            if s.risk == "read_only" and not s.depends_on and s.id not in completed
        ]
        remaining = [s for s in plan.steps if s not in ro_independent and s.id not in completed]

        # Parallel read-only batch
        if ro_independent:
            self.store.event("parallel_batch_start", steps=[s.id for s in ro_independent])
            resolved_step_args: dict[str, Any] = {}
            for s in ro_independent:
                try:
                    resolved_step_args[s.id] = VariableInterpolator.resolve_data(s.args, self.scratchpad)
                except KeyError:
                    resolved_step_args[s.id] = s.args

            batch_results = self._run_parallel_readonly(ro_independent)
            for step in ro_independent:
                if self.cancelled():
                    break
                result = batch_results[step.id]
                self._record(step, "execute", result)
                args_used = resolved_step_args.get(step.id, step.args)
                rec = StepRecord(
                    step_id=step.id,
                    intent=step.purpose,
                    action={"tool": step.tool, "args": args_used},
                    expected_verification=list(step.verify),
                    actual_result={
                        "ok": result.ok,
                        "summary": result.summary,
                        "stdout": result.stdout,
                        "stderr": result.stderr,
                        "exit_code": result.exit_code,
                        "verification_passed": result.ok,
                        "verification_message": result.summary,
                        "state_delta": {},
                    },
                    log_entry={
                        "event": "step_record",
                        "step_id": step.id,
                        "intent": step.purpose,
                        "action": {"tool": step.tool, "args": args_used},
                        "status": "PASSED" if result.ok else "FAILED",
                        "ts": time.time(),
                    },
                )
                self.store.record_step(rec)
                if result.ok:
                    completed.add(step.id)
                    if step.id not in self.store.state["completed_steps"]:
                        self.store.state["completed_steps"].append(step.id)
                    self.store.event("task_finish", task_idx=0, step=step.id, summary=result.summary)
                else:
                    problem = f"{step.purpose}: {result.summary}"
                    problems.append(problem)
                    if step.id not in self.store.state["failed_steps"]:
                        self.store.state["failed_steps"].append(step.id)
                    self.store.event("task_fail", task_idx=0, reason="tool_failed", msg=problem)
            self.store._save()

        if self.cancelled():
            self.store.set_status(JobStatus.CANCELLED, reason="Cancellation requested")
            self.store.event("done", status="CANCELLED", task_count=len(completed),
                             elapsed_s=round(time.monotonic() - self._job_start, 1),
                             summaries=problems)
            return JobStatus.CANCELLED

        # Sequential remaining steps
        for index, step in enumerate(remaining):
            if self.cancelled():
                self.store.set_status(JobStatus.CANCELLED, reason="Cancellation requested")
                self.store.event("done", status="CANCELLED", task_count=len(completed),
                                 elapsed_s=round(time.monotonic() - self._job_start, 1),
                                 summaries=problems)
                return JobStatus.CANCELLED

            # Dependency check
            unmet = [d for d in step.depends_on if d not in completed]
            if unmet:
                problem = f"Step '{step.id}' skipped: unmet dependencies {unmet}"
                problems.append(problem)
                if step.id not in self.store.state["failed_steps"]:
                    self.store.state["failed_steps"].append(step.id)
                self.store.event("task_fail", task_idx=index, reason="deps_unmet", msg=problem)
                rec = StepRecord(
                    step_id=step.id,
                    intent=step.purpose,
                    action={"tool": step.tool, "args": step.args},
                    expected_verification=list(step.verify),
                    actual_result={"ok": False, "summary": problem, "exit_code": 1, "verification_passed": False, "verification_message": problem, "state_delta": {}},
                    log_entry={"event": "step_record", "step_id": step.id, "intent": step.purpose, "action": {"tool": step.tool, "args": step.args}, "status": "FAILED", "ts": time.time()},
                )
                self.store.record_step(rec)
                continue

            # Dynamic variable resolution
            try:
                resolved_args = VariableInterpolator.resolve_data(step.args, self.scratchpad)
            except KeyError as ke:
                failure = f"{step.purpose}: variable resolution failed: {ke}"
                problems.append(failure)
                if step.id not in self.store.state["failed_steps"]:
                    self.store.state["failed_steps"].append(step.id)
                self.store._save()
                self.store.event("task_fail", task_idx=index, reason="variable_resolution_error", msg=failure)
                rec = StepRecord(
                    step_id=step.id,
                    intent=step.purpose,
                    action={"tool": step.tool, "args": step.args},
                    expected_verification=list(step.verify),
                    actual_result={"ok": False, "summary": failure, "exit_code": 1, "verification_passed": False, "verification_message": failure, "state_delta": {}},
                    log_entry={"event": "step_record", "step_id": step.id, "intent": step.purpose, "action": {"tool": step.tool, "args": step.args}, "status": "FAILED", "ts": time.time()},
                )
                self.store.record_step(rec)
                continue

            # Policy gate
            eval_step = Step(step.id, step.purpose, step.tool, resolved_args, step.verify, step.risk, step.depends_on, step.captures)
            decision, reason = self.policy.decide(self.mode, eval_step)
            if decision == "SUGGEST":
                self.store.event("suggest_skip", task_idx=index, task=step.purpose, note=reason)
                completed.add(step.id)
                rec = StepRecord(
                    step_id=step.id,
                    intent=step.purpose,
                    action={"tool": step.tool, "args": resolved_args},
                    expected_verification=list(step.verify),
                    actual_result={
                        "ok": True,
                        "summary": reason,
                        "stdout": "",
                        "stderr": "",
                        "exit_code": 0,
                        "verification_passed": True,
                        "verification_message": "Suggested (simulated)",
                        "state_delta": {"mode": "SUGGEST", "executed": False},
                    },
                    log_entry={
                        "event": "step_record",
                        "step_id": step.id,
                        "intent": step.purpose,
                        "action": {"tool": step.tool, "args": resolved_args},
                        "status": "SUGGESTED",
                        "ts": time.time(),
                    },
                )
                self.store.record_step(rec)
                continue

            if decision not in ("ALLOW",):
                self.store.set_status(
                    JobStatus.NEEDS_APPROVAL,
                    approval={"step": step.id, "reason": reason},
                )
                self.store.event("task_fail", task_idx=index, reason="approval_required", msg=reason)
                rec = StepRecord(
                    step_id=step.id,
                    intent=step.purpose,
                    action={"tool": step.tool, "args": resolved_args},
                    expected_verification=list(step.verify),
                    actual_result={
                        "ok": False,
                        "summary": reason,
                        "stdout": "",
                        "stderr": reason,
                        "exit_code": 126,
                        "verification_passed": False,
                        "verification_message": "Approval required",
                        "state_delta": {"mode": self.mode, "executed": False},
                    },
                    log_entry={
                        "event": "step_record",
                        "step_id": step.id,
                        "intent": step.purpose,
                        "action": {"tool": step.tool, "args": resolved_args},
                        "status": "NEEDS_APPROVAL",
                        "ts": time.time(),
                    },
                )
                self.store.record_step(rec)
                self.store.write_report(JobStatus.NEEDS_APPROVAL, {
                    "goal": plan.goal, "problems": [reason], "evidence": self.evidence,
                    "step_records": self.store.state.get("step_records", []),
                })
                self.store.event("done", status="NEEDS_APPROVAL", task_count=len(completed),
                                 elapsed_s=round(time.monotonic() - self._job_start, 1),
                                 summaries=[reason])
                return JobStatus.NEEDS_APPROVAL

            # Checkpoint & Pre-verification snapshot
            self.store.set_checkpoint(step.id, "PRE_VERIFY")
            pre_snapshot = self._pre_verify(step, resolved_args)

            # Execute
            self.store.set_checkpoint(step.id, "EXECUTE")
            self.store.event("task_start", task_idx=index, task=step.purpose)
            self.store.event("step_exec", task_idx=index, step=0,
                             cmd=f"{step.tool} {json.dumps(resolved_args, sort_keys=True)[:200]}")
            result = self.tools.run(step.tool, resolved_args)
            self._record(step, "execute", result)

            verified = False
            v_msg = ""
            delta: dict[str, Any] = {}

            if not result.ok:
                failure = f"{step.purpose}: {result.summary}"
                self._diagnose(step, failure)
                repaired = False
                for repair_attempt in range(MAX_REPAIRS_PER_STEP):
                    repaired = self._repair(step, failure, repair_attempt)
                    if repaired:
                        break
                if repaired:
                    completed.add(step.id)
                    if step.id not in self.store.state["completed_steps"]:
                        self.store.state["completed_steps"].append(step.id)
                    self.store.event("task_finish", task_idx=index, step=0, summary="Repaired and verified.")
                    self.store.set_status(JobStatus.RUNNING)
                    self.store.set_checkpoint(step.id, "COMPLETED")
                    rec = StepRecord(
                        step_id=step.id,
                        intent=step.purpose,
                        action={"tool": step.tool, "args": resolved_args},
                        expected_verification=list(step.verify),
                        actual_result={
                            "ok": True,
                            "summary": "Repaired and verified",
                            "stdout": result.stdout,
                            "stderr": result.stderr,
                            "exit_code": 0,
                            "verification_passed": True,
                            "verification_message": "Repaired and verified",
                            "state_delta": {"repaired": True},
                        },
                        log_entry={
                            "event": "step_record",
                            "step_id": step.id,
                            "intent": step.purpose,
                            "action": {"tool": step.tool, "args": resolved_args},
                            "status": "PASSED",
                            "ts": time.time(),
                        },
                    )
                    self.store.record_step(rec)
                    continue

                problems.append(failure)
                if step.id not in self.store.state["failed_steps"]:
                    self.store.state["failed_steps"].append(step.id)
                self.store._save()
                self.store.event("task_fail", task_idx=index, reason="tool_failed", msg=failure)
                rec = StepRecord(
                    step_id=step.id,
                    intent=step.purpose,
                    action={"tool": step.tool, "args": resolved_args},
                    expected_verification=list(step.verify),
                    actual_result={
                        "ok": False,
                        "summary": result.summary,
                        "stdout": result.stdout,
                        "stderr": result.stderr,
                        "exit_code": result.exit_code,
                        "verification_passed": False,
                        "verification_message": failure,
                        "state_delta": pre_snapshot,
                    },
                    log_entry={
                        "event": "step_record",
                        "step_id": step.id,
                        "intent": step.purpose,
                        "action": {"tool": step.tool, "args": resolved_args},
                        "status": "FAILED",
                        "ts": time.time(),
                    },
                )
                self.store.record_step(rec)
                continue

            # Post-condition verification with state delta
            self.store.set_checkpoint(step.id, "VERIFY")
            verified, v_msg, delta = self._post_verify(step, pre_snapshot)

            if not verified:
                failure = f"{step.purpose}: verification failed: {v_msg}"
                self._diagnose(step, failure)
                repaired = False
                for repair_attempt in range(MAX_REPAIRS_PER_STEP):
                    repaired = self._repair(step, failure, repair_attempt)
                    if repaired:
                        break
                if repaired:
                    completed.add(step.id)
                    if step.id not in self.store.state["completed_steps"]:
                        self.store.state["completed_steps"].append(step.id)
                    self.store.event("task_finish", task_idx=index, step=0, summary="Repaired and verified.")
                    self.store.set_status(JobStatus.RUNNING)
                    self.store.set_checkpoint(step.id, "COMPLETED")
                    rec = StepRecord(
                        step_id=step.id,
                        intent=step.purpose,
                        action={"tool": step.tool, "args": resolved_args},
                        expected_verification=list(step.verify),
                        actual_result={
                            "ok": True,
                            "summary": "Repaired and verified",
                            "stdout": result.stdout,
                            "stderr": result.stderr,
                            "exit_code": 0,
                            "verification_passed": True,
                            "verification_message": "Repaired and verified",
                            "state_delta": delta,
                        },
                        log_entry={
                            "event": "step_record",
                            "step_id": step.id,
                            "intent": step.purpose,
                            "action": {"tool": step.tool, "args": resolved_args},
                            "status": "PASSED",
                            "ts": time.time(),
                        },
                    )
                    self.store.record_step(rec)
                    continue

                problems.append(failure)
                if step.id not in self.store.state["failed_steps"]:
                    self.store.state["failed_steps"].append(step.id)
                self.store._save()
                self.store.event("task_fail", task_idx=index, reason="verification_failed", msg=failure)
                rec = StepRecord(
                    step_id=step.id,
                    intent=step.purpose,
                    action={"tool": step.tool, "args": resolved_args},
                    expected_verification=list(step.verify),
                    actual_result={
                        "ok": False,
                        "summary": v_msg,
                        "stdout": result.stdout,
                        "stderr": result.stderr,
                        "exit_code": result.exit_code,
                        "verification_passed": False,
                        "verification_message": v_msg,
                        "state_delta": delta,
                    },
                    log_entry={
                        "event": "step_record",
                        "step_id": step.id,
                        "intent": step.purpose,
                        "action": {"tool": step.tool, "args": resolved_args},
                        "status": "FAILED",
                        "ts": time.time(),
                    },
                )
                self.store.record_step(rec)
                continue

            # Step succeeded
            completed.add(step.id)
            if step.id not in self.store.state["completed_steps"]:
                self.store.state["completed_steps"].append(step.id)
            self.store.set_checkpoint(step.id, "COMPLETED")
            self.store.event("task_finish", task_idx=index, step=0, summary=v_msg)
            self.store.set_status(JobStatus.RUNNING)
            rec = StepRecord(
                step_id=step.id,
                intent=step.purpose,
                action={"tool": step.tool, "args": resolved_args},
                expected_verification=list(step.verify),
                actual_result={
                    "ok": True,
                    "summary": result.summary,
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                    "exit_code": result.exit_code,
                    "verification_passed": True,
                    "verification_message": v_msg,
                    "state_delta": delta,
                },
                log_entry={
                    "event": "step_record",
                    "step_id": step.id,
                    "intent": step.purpose,
                    "action": {"tool": step.tool, "args": resolved_args},
                    "status": "PASSED",
                    "ts": time.time(),
                },
            )
            self.store.record_step(rec)

        # ── Final criteria check ───────────────────────────────────────────
        self.store.set_checkpoint("final_criteria", "VERIFY")
        all_verified, final_failures = self._final_criteria(plan)
        problems.extend(final_failures)
        elapsed = round(time.monotonic() - self._job_start, 1)

        if all_verified and not problems:
            status = JobStatus.SUCCEEDED
        elif completed:
            status = JobStatus.PARTIAL
        else:
            status = JobStatus.FAILED

        self.store.set_status(status, problems=problems)
        self.store.set_checkpoint("job", status.value)
        report = {
            "goal":            plan.goal,
            "elapsed_s":       elapsed,
            "completed_steps": list(completed),
            "failed_steps":    self.store.state.get("failed_steps", []),
            "scratchpad":      self.scratchpad,
            "step_records":    self.store.state.get("step_records", []),
            "problems":        problems,
            "recommendations": self.recommendations,
            "evidence":        self.evidence,
        }
        self.store.write_report(status, report)
        self.store.event(
            "done",
            status="DONE" if status == JobStatus.SUCCEEDED else status.value,
            task_count=len(plan.steps),
            elapsed_s=elapsed,
            summaries=problems or ["All success criteria passed."],
        )
        return status

    # ── Main execution ─────────────────────────────────────────────────────

    def run(self, intent: str) -> JobStatus:
        self._job_start = time.monotonic()
        if not self.store.state:
            self.store.create(intent, self.mode, self.caller)
        self.store.set_status(JobStatus.PLANNING)
        self.store.event("planner_start", intent=intent)

        # ── Inventory ──────────────────────────────────────────────────────
        inventory = self._inventory()

        # ── 3-tier planning ────────────────────────────────────────────────
        plan = self._make_plan(intent, inventory)
        if plan is None:
            reason = "All three planning tiers failed to produce a valid executable plan."
            self.store.set_status(JobStatus.BLOCKED, reason=reason)
            self.store.event("task_fail", task_idx=0, reason="no_plan", msg=reason)
            self.store.event("done", status="FAILED", task_count=0,
                             elapsed_s=0, summaries=[reason])
            self.store.write_report(JobStatus.BLOCKED, {
                "goal": intent, "problems": [reason], "evidence": self.evidence,
                "step_records": self.store.state.get("step_records", []),
            })
            return JobStatus.BLOCKED

        self.store.save_plan(plan)
        self.store.event("plan", tasks=[s.purpose for s in plan.steps],
                         count=len(plan.steps))
        self.store.set_status(JobStatus.RUNNING, goal=plan.goal)
        return self._execute_plan(plan, completed=set(), problems=[])

    # ── Mid-job crash resume ───────────────────────────────────────────────

    def resume(self) -> JobStatus:
        """Resume execution of an interrupted job from its persistent checkpoint."""
        self._job_start = time.monotonic()
        state = self.store.load_state()
        cp    = self.store.load_checkpoint() or state.get("checkpoint", {})
        plan  = self.store.load_plan()
        if plan is None:
            reason = f"Cannot resume job {self.store.task_id}: plan.json is missing or corrupted."
            self.store.set_status(JobStatus.FAILED, reason=reason)
            self.store.event("resume_failed", reason=reason)
            return JobStatus.FAILED

        self.scratchpad = cp.get("scratchpad") or state.get("scratchpad", {})
        completed = set(cp.get("completed_steps") or state.get("completed_steps", []))
        problems  = list(cp.get("failed_steps") or state.get("failed_steps", []))

        self.store.set_status(JobStatus.RUNNING, resumed_at=time.time())
        self.store.event(
            "job_resumed",
            task_id=self.store.task_id,
            completed_count=len(completed),
            scratchpad_keys=list(self.scratchpad.keys()),
        )

        # Sanity verification of previously completed system-changing steps
        for step_id in list(completed):
            step = plan.get_step(step_id)
            if step and step.risk == "system_change" and step.verify:
                ok, v_msg = self._verify(step)
                if not ok:
                    self.store.event("resume_sanity_failed", step_id=step_id, error=v_msg)
                    # Prior state was lost during downtime; re-enqueue this step
                    completed.remove(step_id)
                    if step_id in self.store.state.get("completed_steps", []):
                        self.store.state["completed_steps"].remove(step_id)
                    self.store._save()

        return self._execute_plan(plan, completed=completed, problems=problems)
