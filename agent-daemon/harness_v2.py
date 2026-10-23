#!/usr/bin/env python3
"""Verified, policy-controlled task harness for AI-Agent OS  (v2.2).

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

import json
import os
import posixpath
import re
import shutil
import socket
import struct
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Any, Callable

# ── Public constants ─────────────────────────────────────────────────────────
MAX_REPAIRS_PER_STEP = 2
MAX_PLAN_STEPS = 20
TOOL_TIMEOUT_DEFAULT = 60    # seconds per tool call
PLAN_LLM_N_PREDICT  = 700   # token budget for plan generation
REPAIR_LLM_N_PREDICT = 500  # token budget for repair plan

# Commands whose first token is safe for command.argv (Linux read+light-write)
SAFE_ARGV_COMMANDS: frozenset[str] = frozenset({
    "ls", "cat", "head", "tail", "wc", "grep", "find", "awk", "sed",
    "sort", "uniq", "cut", "tr", "diff", "cp", "mv", "mkdir", "rm",
    "touch", "chmod", "chown", "ln", "stat", "du", "df", "free", "ps",
    "ss", "ip", "ping", "curl", "wget", "tar", "gzip", "gunzip",
    "python3", "python", "sh", "bash",
    "apk", "rc-service", "rc-update",
    "echo", "printf", "date", "whoami", "hostname", "uname",
    "nc", "socat", "openssl",
})


# ── Status enum ───────────────────────────────────────────────────────────────
class JobStatus(str, Enum):
    PLANNING       = "PLANNING"
    RUNNING        = "RUNNING"
    VERIFYING      = "VERIFYING"
    NEEDS_APPROVAL = "NEEDS_APPROVAL"
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
        self.base        = Path(base_dir) / "jobs" / task_id
        self.state_path  = self.base / "job.json"
        self.events_path = self.base / "events.jsonl"
        self.plan_path   = self.base / "plan.json"
        self.report_path = self.base / "report.json"
        self.task_id     = task_id
        self.legacy_log  = legacy_log
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
            "scratchpad":      {},
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
        "port.inspect", "file.read", "http.check",
        "dir.query", "file.query", "command.argv",
    })
    CHANGE: frozenset[str] = frozenset({
        "package.install", "service.start", "service.restart",
        "service.stop", "service.enable", "service.disable",
        "dir.create", "dir.remove", "file.write",
    })

    def decide(self, mode: str, step: Step) -> tuple[str, str]:
        if step.tool not in self.READ_ONLY | self.CHANGE:
            return "DENY", f"Tool '{step.tool}' is not registered."
        if step.tool in self.READ_ONLY:
            return "ALLOW", "Read-only inspection is allowed."
        if mode == "SUGGEST":
            return "SUGGEST", "SUGGEST mode: no system changes are executed."
        if mode in {"ASSIST", "APPLY"}:
            return "ALLOW", "Requested system action is allowed by the current local policy."
        return "REQUIRE_APPROVAL", "The selected mode does not authorize this system change."


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
            effective_cwd = self.caller.get("home", "/")
        exec_env = {
            "PATH": os.environ.get("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"),
            "HOME": self.caller.get("home", "/"),
            "USER": self.caller.get("user", "root"),
        }
        if "SYSTEMROOT" in os.environ:
            exec_env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
        if "WINDIR" in os.environ:
            exec_env["WINDIR"] = os.environ["WINDIR"]

        result = subprocess.run(
            argv,
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
        info: dict[str, Any] = {
            "hostname": socket.gethostname(),
            "kernel":   os.uname().release,
        }
        try:
            info["os_release"] = Path("/etc/os-release").read_text(encoding="utf-8")[:2048]
        except OSError:
            pass
        return ToolResult(True, "Collected system information", facts=[info])

    def tool_disk_status(self, args: dict[str, Any]) -> ToolResult:
        return self._exec(["df", "-h"])

    def tool_memory_status(self, args: dict[str, Any]) -> ToolResult:
        return self._exec(["free", "-m"])

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
        if not isinstance(raw_path, str) or not raw_path.startswith("/"):
            raise ValueError("'path' must be an absolute path")
        path = Path(raw_path).resolve()
        allowed = (
            Path("/etc"),
            Path("/var/log"),
            Path(self.caller.get("home", "/nonexistent")),
            Path("/tmp"),
            Path("/srv"),
            Path("/var/ai-agent"),
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
        full_path = shutil.which(argv[0]) or argv[0]
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
        if any(path == root or root in path.parents for root in blocked_roots):
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


ALLOWED_TOOLS = PolicyEngine.READ_ONLY | PolicyEngine.CHANGE


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
    if not isinstance(raw_steps, list) or not raw_steps or len(raw_steps) > MAX_PLAN_STEPS:
        raise PlanError(f"plan.steps must contain 1–{MAX_PLAN_STEPS} steps")
    steps: list[Step] = []
    ids: set[str] = set()
    for raw in raw_steps:
        if not isinstance(raw, dict):
            raise PlanError("each plan step must be an object")
        step_id  = raw.get("id")
        purpose  = raw.get("purpose")
        tool     = raw.get("tool")
        step_args = raw.get("args")
        if (not isinstance(step_id, str)
                or not re.fullmatch(r"[a-z][a-z0-9_\-]{0,63}", step_id)
                or step_id in ids):
            raise PlanError("every step needs a unique lowercase id (a-z0-9_-)")
        if not isinstance(purpose, str) or not purpose.strip():
            raise PlanError(f"step '{step_id}' is missing purpose")
        if tool not in ALLOWED_TOOLS or not isinstance(step_args, dict):
            raise PlanError(f"step '{step_id}' references invalid tool '{tool}' or bad args")
        verify = raw.get("verify", [])
        if not isinstance(verify, list):
            raise PlanError(f"step '{step_id}' has invalid verification list")
        # Validate each verifier references a real tool
        for v in verify:
            if not isinstance(v, dict) or v.get("tool") not in ALLOWED_TOOLS:
                raise PlanError(f"step '{step_id}' has invalid verifier: {v!r}")
        risk = "read_only" if tool in PolicyEngine.READ_ONLY else "system_change"
        if risk == "system_change" and not verify:
            raise PlanError(f"system-changing step '{step_id}' must include verification steps")
        deps = raw.get("depends_on", [])
        if not isinstance(deps, list) or not all(isinstance(d, str) for d in deps):
            raise PlanError(f"step '{step_id}' has invalid depends_on list")
        captures = raw.get("captures", {})
        if not isinstance(captures, dict):
            raise PlanError(f"step '{step_id}' has invalid captures dict")
        for cap_k, cap_v in captures.items():
            if not isinstance(cap_k, str) or not isinstance(cap_v, str):
                raise PlanError(f"step '{step_id}' captures must map strings to strings")
        ids.add(step_id)
        steps.append(Step(step_id, purpose, tool, step_args, verify, risk, deps, captures))
    # Validate dependency graph — no forward refs to unknown IDs, no self-dep
    for step in steps:
        for dep in step.depends_on:
            if dep not in ids or dep == step.id:
                raise PlanError(f"step '{step.id}' has unknown or self-referential dep '{dep}'")
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
def plan_prompt(intent: str, inventory: list[dict[str, Any]]) -> str:
    tools = sorted(ALLOWED_TOOLS)
    inv_text = json.dumps(inventory, ensure_ascii=False)[:2000]
    return (
        "<|im_start|>system\n"
        "You are the AI-Agent OS planner. Return ONE JSON object only. "
        "No markdown fences. No prose before or after the JSON.\n"
        f"Registered tools: {tools}\n"
        "Schema:\n"
        '{"goal":string,"assumptions":[string],"success_criteria":[{"description":string,"tool":string,"args":{}}],'
        '"steps":[{"id":string,"purpose":string,"tool":string,"args":{},'
        '"depends_on":[string],"verify":[{"tool":string,"args":{}}]}]}\n'
        "Rules:\n"
        "- Every system-changing step MUST have at least one verify entry.\n"
        "- Step ids are lowercase alphanumeric with hyphens only.\n"
        "- depends_on lists step ids that must complete before this step runs.\n"
        "- Use command.argv for tasks the typed tools cannot cover; "
        '  args must be {"argv":["binary","arg1",...]}'
        " where binary is in the safe allowlist.\n"
        "<|im_end|>\n"
        "<|im_start|>user\n"
        f"Intent: {intent}\n"
        f"System inventory: {inv_text}\n"
        "<|im_end|>\n"
        "<|im_start|>assistant\n"
    )


def repair_prompt(purpose: str, failure: str, evidence: list[dict[str, Any]]) -> str:
    ev_text = json.dumps(evidence[-6:], ensure_ascii=False)[:1200]
    tools   = sorted(ALLOWED_TOOLS)
    return (
        "<|im_start|>system\n"
        f"Step '{purpose}' failed with: {failure}\n"
        f"Evidence: {ev_text}\n"
        "Return a JSON repair plan (same schema as a normal plan). "
        f"Registered tools: {tools}. Max 3 steps. "
        "Each system-changing step needs verify. No prose, just JSON.\n"
        "<|im_end|>\n"
        "<|im_start|>user\nRepair plan:\n<|im_end|>\n"
        "<|im_start|>assistant\n"
    )


# ── Deterministic fast-path planner ──────────────────────────────────────────
def deterministic_plan(intent: str, caller: dict[str, Any] | None = None) -> Plan | None:
    """Compile common local OS intents without spending a model request.

    Patterns handled (14):
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
    svc_m = re.search(r"\b(restart|start|stop)\s+([a-z0-9][a-z0-9._+\-]{0,127})\b", text_lower)

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
            r"\b(?:show|tail|last|read)\s+(?:(?:last|the)\s+)?(\d+)?\s*lines?\s+"
            r"(?:of\s+)?(/[^\s]+)", text, re.IGNORECASE)
        if lm:
            n_lines = int(lm.group(1)) if lm.group(1) else 50
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
            r"^(ls|cat|head|tail|df|free|ps|ss|ip|find|grep|uname|whoami|hostname|date)\b(.*)$",
            text.strip(), re.IGNORECASE)
        if ro_cmd_m:
            binary  = ro_cmd_m.group(1).lower()
            rest    = ro_cmd_m.group(2).strip()
            argv    = [binary] + (rest.split() if rest else [])
            steps.append(Step("run-command", f"Run: {' '.join(argv)}",
                              "command.argv", {"argv": argv}, []))
            criteria.append({"description": f"Command '{argv[0]}' executed",
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

        # Tier 2 — LLM structured plan
        raw = self.llm(plan_prompt(intent, inventory))
        self.store.event("planner_raw", raw=raw[:2000] if isinstance(raw, str) else "")
        parsed = extract_first_json_object(raw)
        if parsed and isinstance(parsed, dict):
            try:
                plan = validate_plan(parsed)
                self.store.event("planner_llm_ok", steps=[s.purpose for s in plan.steps])
                return plan
            except PlanError as exc:
                self.store.event("planner_parse_fail", error=str(exc))

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

    # ── Verification ───────────────────────────────────────────────────────

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
        """Executes plan steps, tracking checkpoint, scratchpad, and verification."""
        # Filter read-only independent steps that haven't completed
        ro_independent = [
            s for s in plan.steps
            if s.risk == "read_only" and not s.depends_on and s.id not in completed
        ]
        remaining = [s for s in plan.steps if s not in ro_independent and s.id not in completed]

        # Parallel read-only batch
        if ro_independent:
            self.store.event("parallel_batch_start",
                             steps=[s.id for s in ro_independent])
            batch_results = self._run_parallel_readonly(ro_independent)
            for step in ro_independent:
                if self.cancelled():
                    break
                result = batch_results[step.id]
                self._record(step, "execute", result)
                if result.ok:
                    completed.add(step.id)
                    if step.id not in self.store.state["completed_steps"]:
                        self.store.state["completed_steps"].append(step.id)
                    self.store.event("task_finish", task_idx=0, step=step.id,
                                     summary=result.summary)
                else:
                    problem = f"{step.purpose}: {result.summary}"
                    problems.append(problem)
                    if step.id not in self.store.state["failed_steps"]:
                        self.store.state["failed_steps"].append(step.id)
                    self.store.event("task_fail", task_idx=0, reason="tool_failed",
                                     msg=problem)
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
                continue

            # Policy gate
            decision, reason = self.policy.decide(self.mode, step)
            if decision == "SUGGEST":
                self.store.event("suggest_skip", task_idx=index, task=step.purpose, note=reason)
                completed.add(step.id)
                continue
            if decision not in ("ALLOW",):
                self.store.set_status(
                    JobStatus.NEEDS_APPROVAL,
                    approval={"step": step.id, "reason": reason},
                )
                self.store.event("task_fail", task_idx=index,
                                 reason="approval_required", msg=reason)
                self.store.write_report(JobStatus.NEEDS_APPROVAL, {
                    "goal": plan.goal, "problems": [reason], "evidence": self.evidence,
                })
                self.store.event("done", status="NEEDS_APPROVAL", task_count=len(completed),
                                 elapsed_s=round(time.monotonic() - self._job_start, 1),
                                 summaries=[reason])
                return JobStatus.NEEDS_APPROVAL

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
                continue

            # Checkpoint & Execute
            self.store.event("task_start", task_idx=index, task=step.purpose)
            self.store.event("step_exec", task_idx=index, step=0,
                             cmd=f"{step.tool} {json.dumps(resolved_args, sort_keys=True)[:200]}")
            result = self.tools.run(step.tool, resolved_args)
            self._record(step, "execute", result)

            if not result.ok:
                failure = f"{step.purpose}: {result.summary}"
                self._diagnose(step, failure)
                # Bounded repair loop
                repaired = False
                for repair_attempt in range(MAX_REPAIRS_PER_STEP):
                    repaired = self._repair(step, failure, repair_attempt)
                    if repaired:
                        break
                if repaired:
                    completed.add(step.id)
                    if step.id not in self.store.state["completed_steps"]:
                        self.store.state["completed_steps"].append(step.id)
                    self.store.event("task_finish", task_idx=index, step=0,
                                     summary="Repaired and verified.")
                    self.store.set_status(JobStatus.RUNNING)
                    self.store._save()
                    continue
                problems.append(failure)
                if step.id not in self.store.state["failed_steps"]:
                    self.store.state["failed_steps"].append(step.id)
                self.store._save()
                self.store.event("task_fail", task_idx=index,
                                 reason="tool_failed", msg=failure)
                # Non-fatal: continue remaining steps unless they depend on this one
                continue

            # Verify
            verified, v_msg = self._verify(step)
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
                    self.store.event("task_finish", task_idx=index, step=0,
                                     summary="Repaired and verified.")
                    self.store.set_status(JobStatus.RUNNING)
                    self.store._save()
                    continue
                problems.append(failure)
                if step.id not in self.store.state["failed_steps"]:
                    self.store.state["failed_steps"].append(step.id)
                self.store._save()
                self.store.event("task_fail", task_idx=index,
                                 reason="verification_failed", msg=failure)
                continue

            # Step succeeded
            completed.add(step.id)
            if step.id not in self.store.state["completed_steps"]:
                self.store.state["completed_steps"].append(step.id)
            self.store._save()
            self.store.event("task_finish", task_idx=index, step=0, summary=v_msg)
            self.store.set_status(JobStatus.RUNNING)

        # ── Final criteria check ───────────────────────────────────────────
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
        report = {
            "goal":            plan.goal,
            "elapsed_s":       elapsed,
            "completed_steps": list(completed),
            "failed_steps":    self.store.state.get("failed_steps", []),
            "scratchpad":      self.scratchpad,
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
            })
            return JobStatus.BLOCKED

        self.store.save_plan(plan)
        self.store.event("plan", tasks=[s.purpose for s in plan.steps],
                         count=len(plan.steps))
        self.store.set_status(JobStatus.RUNNING, goal=plan.goal)
        return self._execute_plan(plan, completed=set(), problems=[])

