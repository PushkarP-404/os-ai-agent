#!/usr/bin/env python3
"""Verified, policy-controlled task harness for AI-Agent OS.

This module deliberately separates model output from operating-system effects.
The model may return a strict plan that references registered tools, while the
executor builds argv lists itself and verifies each requested outcome.
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
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Any, Callable


class JobStatus(str, Enum):
    PLANNING = "PLANNING"
    RUNNING = "RUNNING"
    VERIFYING = "VERIFYING"
    NEEDS_APPROVAL = "NEEDS_APPROVAL"
    SUCCEEDED = "SUCCEEDED"
    PARTIAL = "PARTIAL"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


TERMINAL_STATUSES = {
    JobStatus.SUCCEEDED, JobStatus.PARTIAL, JobStatus.BLOCKED,
    JobStatus.FAILED, JobStatus.CANCELLED,
}


class PlanError(ValueError):
    """A planner response is not a safe, executable harness plan."""


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


@dataclass
class Plan:
    goal: str
    success_criteria: list[dict[str, Any]]
    steps: list[Step]
    assumptions: list[str] = field(default_factory=list)


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


class JobStore:
    """Durable job state plus an append-only event stream.

    The legacy task log remains available for the existing C CLI. New durable
    state is placed in /var/ai-agent/jobs/<job-id>/ and can later drive a local
    event socket without changing the executor's contract.
    """

    def __init__(self, base_dir: str, task_id: str, legacy_log: Callable[..., None]):
        self.base = Path(base_dir) / "jobs" / task_id
        self.state_path = self.base / "job.json"
        self.events_path = self.base / "events.jsonl"
        self.plan_path = self.base / "plan.json"
        self.report_path = self.base / "report.json"
        self.task_id = task_id
        self.legacy_log = legacy_log
        self.state: dict[str, Any] = {}

    def create(self, intent: str, mode: str, caller: dict[str, Any]) -> None:
        self.base.mkdir(parents=True, exist_ok=True)
        self.state = {
            "job_id": self.task_id,
            "intent": intent,
            "mode": mode,
            "status": JobStatus.PLANNING.value,
            "created_at": time.time(),
            "updated_at": time.time(),
            "caller": {
                "uid": caller.get("uid"), "gid": caller.get("gid"),
                "user": caller.get("user"), "cwd": caller.get("cwd"),
            },
            "completed_steps": [],
            "failed_steps": [],
            "approval": None,
        }
        self._save()
        self.event("job_start", task_id=self.task_id, intent=intent, mode=mode)

    def _save(self) -> None:
        self.state["updated_at"] = time.time()
        _atomic_json(self.state_path, self.state)

    def event(self, event: str, **payload: Any) -> None:
        record = {"event": event, "ts": time.time(), "job_id": self.task_id, **payload}
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        with self.events_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
            stream.flush()
        self.legacy_log(event, **payload)

    def set_status(self, status: JobStatus, **extra: Any) -> None:
        self.state["status"] = status.value
        self.state.update(extra)
        self._save()
        self.event("job_status", status=status.value, **extra)

    def save_plan(self, plan: Plan) -> None:
        payload = {
            "goal": plan.goal,
            "assumptions": plan.assumptions,
            "success_criteria": plan.success_criteria,
            "steps": [asdict(step) for step in plan.steps],
        }
        _atomic_json(self.plan_path, payload)
        self.state["goal"] = plan.goal
        self.state["success_criteria"] = plan.success_criteria
        self._save()

    def write_report(self, status: JobStatus, report: dict[str, Any]) -> None:
        report = {"job_id": self.task_id, "status": status.value, **report}
        _atomic_json(self.report_path, report)
        self.state["report_path"] = str(self.report_path)
        self._save()


class PolicyEngine:
    """Small policy core; policies can later be loaded from /etc/ai-agent."""

    READ_ONLY = {
        "system.info", "disk.status", "memory.status", "package.query",
        "service.status", "service.logs", "port.inspect", "file.read",
        "http.check", "dir.query", "file.query",
    }
    CHANGE = {
        "package.install", "service.start", "service.restart", "service.stop",
        "dir.create", "dir.remove", "file.write",
    }

    def decide(self, mode: str, step: Step) -> tuple[str, str]:
        if step.tool not in self.READ_ONLY | self.CHANGE:
            return "DENY", f"Tool '{step.tool}' is not registered."
        if step.tool in self.READ_ONLY:
            return "ALLOW", "Read-only inspection is allowed."
        if mode == "SUGGEST":
            return "SUGGEST", "Suggest mode never changes the system."
        # The prompt is an explicit task request. APPLY can later be made the
        # only mode for protected actions via /etc/ai-agent/policy.json.
        if mode in {"ASSIST", "APPLY"}:
            return "ALLOW", "Requested system action is allowed by the current local policy."
        return "REQUIRE_APPROVAL", "The selected mode does not authorize this system change."


class PrivilegedToolClient:
    """Client for the narrow root-owned local tool broker.

    Only system-changing registered tools are sent to the broker. The daemon
    remains unprivileged and the broker verifies the Unix peer before acting.
    """

    def __init__(self, path: str = "/run/ai-agent/privileged.sock"):
        self.path = path

    def run(self, tool: str, args: dict[str, Any], caller: dict[str, Any] | None = None) -> ToolResult:
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
            payload = json.loads(bytes(response).split(b"\n", 1)[0].decode("utf-8"))
            return ToolResult(**payload)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return ToolResult(False, "Privileged tool broker is unavailable", stderr=str(exc), exit_code=125)


class ToolExecutor:
    """Registered OS tools that never interpret model-provided shell text."""

    def __init__(self, caller: dict[str, Any], command_timeout: int = 60,
                 allow_privilege_proxy: bool = True):
        self.caller = caller
        self.command_timeout = command_timeout
        self.privileged = PrivilegedToolClient() if allow_privilege_proxy else None

    def run(self, tool: str, args: dict[str, Any]) -> ToolResult:
        if tool in PolicyEngine.CHANGE and os.geteuid() != 0 and self.privileged:
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

    def _exec(self, argv: list[str], timeout: int | None = None) -> ToolResult:
        result = subprocess.run(
            argv, capture_output=True, text=True,
            timeout=timeout or self.command_timeout,
            cwd=self.caller.get("cwd") if os.path.isdir(self.caller.get("cwd", "")) else None,
            env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "HOME": self.caller.get("home", "/")},
        )
        stdout, stderr = result.stdout[:8192], result.stderr[:4096]
        return ToolResult(
            result.returncode == 0,
            f"{' '.join(argv)} exited {result.returncode}",
            stdout=stdout, stderr=stderr, exit_code=result.returncode,
        )

    @staticmethod
    def _name(args: dict[str, Any], key: str = "name") -> str:
        value = args.get(key)
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}", value):
            raise ValueError(f"'{key}' must be a simple package or service name")
        return value

    def tool_system_info(self, args: dict[str, Any]) -> ToolResult:
        info = {"hostname": socket.gethostname(), "kernel": os.uname().release}
        try:
            info["os_release"] = Path("/etc/os-release").read_text(encoding="utf-8")[:4096]
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

    def tool_package_install(self, args: dict[str, Any]) -> ToolResult:
        package = self._name(args, "package")
        if shutil.which("apk"):
            return self._exec(["apk", "add", package], timeout=180)
        return ToolResult(False, "No supported package manager found", exit_code=127)

    def _service(self, action: str, args: dict[str, Any]) -> ToolResult:
        name = self._name(args)
        if os.path.exists("/sbin/rc-service"):
            return self._exec(["rc-service", name, action])
        return ToolResult(False, "OpenRC is not available", exit_code=127)

    def tool_service_status(self, args: dict[str, Any]) -> ToolResult:
        return self._service("status", args)

    def tool_service_start(self, args: dict[str, Any]) -> ToolResult:
        return self._service("start", args)

    def tool_service_restart(self, args: dict[str, Any]) -> ToolResult:
        return self._service("restart", args)

    def tool_service_stop(self, args: dict[str, Any]) -> ToolResult:
        return self._service("stop", args)

    def tool_service_logs(self, args: dict[str, Any]) -> ToolResult:
        name = self._name(args)
        log_path = Path("/var/log") / f"{name}.log"
        if not log_path.is_file():
            return ToolResult(False, f"No readable log at {log_path}", exit_code=1)
        text = log_path.read_text(encoding="utf-8", errors="replace")[-8192:]
        return ToolResult(True, f"Read recent log for {name}", stdout=text)

    def tool_port_inspect(self, args: dict[str, Any]) -> ToolResult:
        port = args.get("port")
        if not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError("'port' must be an integer between 1 and 65535")
        if shutil.which("ss"):
            return self._exec(["ss", "-ltnp", f"sport = :{port}"])
        return ToolResult(False, "'ss' is not installed", exit_code=127)

    def tool_file_read(self, args: dict[str, Any]) -> ToolResult:
        raw_path = args.get("path")
        if not isinstance(raw_path, str) or not raw_path.startswith("/"):
            raise ValueError("'path' must be an absolute path")
        path = Path(raw_path).resolve()
        allowed = (Path("/etc"), Path("/var/log"), Path(self.caller.get("home", "/nonexistent")))
        if not any(path == root or root in path.parents for root in allowed):
            raise ValueError("path is outside the readable harness scope")
        if not path.is_file():
            return ToolResult(False, f"File does not exist: {path}", exit_code=1)
        return ToolResult(True, f"Read {path}", stdout=path.read_text(encoding="utf-8", errors="replace")[:8192])

    def tool_http_check(self, args: dict[str, Any]) -> ToolResult:
        url = args.get("url")
        if not isinstance(url, str) or not re.fullmatch(r"http://(127\.0\.0\.1|localhost)(?::\d+)?(?:/[A-Za-z0-9._~:/?#[\]@!$&'()*+,;=%-]*)?", url):
            raise ValueError("HTTP checks are limited to localhost URLs")
        if not shutil.which("curl"):
            return ToolResult(False, "curl is required for http.check", exit_code=127)
        return self._exec(["curl", "--fail", "--silent", "--show-error", "--max-time", "10", url], timeout=15)

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
        if uid is not None and gid is not None and os.geteuid() == 0:
            try:
                os.chown(path, int(uid), int(gid))
            except OSError:
                pass
        return ToolResult(True, f"Directory created: {path}")

    def tool_dir_query(self, args: dict[str, Any]) -> ToolResult:
        raw_path = args.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ValueError("'path' must be a non-empty string")
        path = Path(raw_path).resolve()
        if path.is_dir():
            return ToolResult(True, f"Directory exists: {path}")
        return ToolResult(False, f"Directory does not exist: {path}", exit_code=1)

    def tool_dir_remove(self, args: dict[str, Any]) -> ToolResult:
        raw_path = args.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ValueError("'path' must be a non-empty string")
        path = Path(raw_path).resolve()
        protected = (
            Path("/"), Path("/bin"), Path("/sbin"), Path("/usr"), Path("/etc"),
            Path("/var"), Path("/home"), Path("/root"), Path("/dev"), Path("/proc"), Path("/sys"),
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
        content = args.get("content", "")
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
        if uid is not None and gid is not None and os.geteuid() == 0:
            try:
                os.chown(path, int(uid), int(gid))
            except OSError:
                pass
        return ToolResult(True, f"File created: {path}")

    def tool_file_query(self, args: dict[str, Any]) -> ToolResult:
        raw_path = args.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ValueError("'path' must be a non-empty string")
        path = Path(raw_path).resolve()
        if path.is_file():
            return ToolResult(True, f"File exists: {path}")
        return ToolResult(False, f"File does not exist: {path}", exit_code=1)


ALLOWED_TOOLS = PolicyEngine.READ_ONLY | PolicyEngine.CHANGE


def validate_plan(value: Any) -> Plan:
    if not isinstance(value, dict):
        raise PlanError("plan must be a JSON object")
    goal = value.get("goal")
    criteria = value.get("success_criteria")
    raw_steps = value.get("steps")
    if not isinstance(goal, str) or not goal.strip():
        raise PlanError("plan.goal must be a non-empty string")
    if not isinstance(criteria, list) or not criteria:
        raise PlanError("plan.success_criteria must be a non-empty list")
    if not isinstance(raw_steps, list) or not raw_steps or len(raw_steps) > 20:
        raise PlanError("plan.steps must contain between 1 and 20 steps")
    steps: list[Step] = []
    ids: set[str] = set()
    for raw in raw_steps:
        if not isinstance(raw, dict):
            raise PlanError("each plan step must be an object")
        step_id, purpose, tool, args = raw.get("id"), raw.get("purpose"), raw.get("tool"), raw.get("args")
        if not isinstance(step_id, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", step_id) or step_id in ids:
            raise PlanError("every step needs a unique lowercase id")
        if not isinstance(purpose, str) or not purpose.strip():
            raise PlanError(f"step '{step_id}' is missing purpose")
        if tool not in ALLOWED_TOOLS or not isinstance(args, dict):
            raise PlanError(f"step '{step_id}' references an invalid tool or arguments")
        verify = raw.get("verify", [])
        if not isinstance(verify, list):
            raise PlanError(f"step '{step_id}' has invalid verification")
        risk = "read_only" if tool in PolicyEngine.READ_ONLY else "system_change"
        if risk == "system_change" and not verify:
            raise PlanError(f"system-changing step '{step_id}' must have verification")
        dependencies = raw.get("depends_on", [])
        if not isinstance(dependencies, list) or not all(isinstance(dep, str) for dep in dependencies):
            raise PlanError(f"step '{step_id}' has invalid dependencies")
        ids.add(step_id)
        steps.append(Step(step_id, purpose, tool, args, verify, risk, dependencies))
    for step in steps:
        if any(dep not in ids or dep == step.id for dep in step.depends_on):
            raise PlanError(f"step '{step.id}' has an invalid dependency")
    return Plan(goal=goal.strip(), success_criteria=criteria, steps=steps, assumptions=value.get("assumptions", []))


def plan_prompt(intent: str, inventory: list[dict[str, Any]]) -> str:
    tools = sorted(ALLOWED_TOOLS)
    return (
        "<|im_start|>system\n"
        "You are the AI-Agent OS planner. Return one JSON object only. Do not return shell commands. "
        "Use only the registered tools and ensure every system-changing step has verification. "
        f"Registered tools: {tools}.\n"
        "Schema: {\"goal\":string,\"assumptions\":[string],\"success_criteria\":[object],"
        "\"steps\":[{\"id\":string,\"purpose\":string,\"tool\":string,\"args\":object,"
        "\"depends_on\":[string],\"verify\":[{\"tool\":string,\"args\":object}]}]}.\n"
        "<|im_end|>\n<|im_start|>user\n"
        f"Intent: {intent}\nInventory: {json.dumps(inventory, ensure_ascii=False)[:6000]}"
        "\n<|im_end|>\n<|im_start|>assistant\n"
    )


def deterministic_plan(intent: str, caller: dict[str, Any] | None = None) -> Plan | None:
    """Compile common local OS intents without spending a model request.

    This gives the appliance a reliable fast path for the initial typed-tool
    catalog. Requests outside this narrow grammar still require a validated
    model plan and are blocked if the model cannot provide one.
    """
    caller = caller or {}
    home_dir = caller.get("home") or "/root"
    cwd_dir = caller.get("cwd") or home_dir
    uid = caller.get("uid")
    gid = caller.get("gid")

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
        else:
            if name.startswith("~/"):
                return posixpath.normpath(posixpath.join(home_dir, name[2:]))
            elif name.startswith("/"):
                return posixpath.normpath(name)
            elif name.lower().startswith("desktop/"):
                return posixpath.normpath(posixpath.join(home_dir, name))
            else:
                return posixpath.normpath(posixpath.join(cwd_dir, name))

    raw_clean = intent.strip()
    clean_intent = re.sub(r"^/+(?:assist|suggest|apply)?\s*", "", raw_clean, flags=re.IGNORECASE)
    text = " ".join(clean_intent.split())
    text_lower = text.lower()
    steps: list[Step] = []
    criteria: list[dict[str, Any]] = []

    # 1. Directory / folder creation
    dir_match = None
    m = re.search(r"\b(?:create|make|mkdir)\s+(?:a\s+)?(?:folder|directory|dir)\s+in\s+([^\s]+)\s+(?:(?:called|named)\s+)?([^\s]+)", text, re.IGNORECASE)
    if m:
        dir_match = (m.group(2), m.group(1))
    else:
        m = re.search(r"\b(?:create|make|mkdir)\s+(?:a\s+)?(?:folder|directory|dir)\s+(?:(?:called|named)\s+)?([^\s]+)\s+in\s+([^\s]+)", text, re.IGNORECASE)
        if m:
            dir_match = (m.group(1), m.group(2))
        else:
            m = re.search(r"\b(?:create|make|mkdir)\s+(?:a\s+)?(?:folder|directory|dir)\s+(?:(?:called|named)\s+)?([^\s]+)", text, re.IGNORECASE)
            if m:
                dir_match = (m.group(1), None)
            else:
                m = re.search(r"\bmkdir\s+(?:-[a-zA-Z]+\s+)?([^\s]+)", text, re.IGNORECASE)
                if m:
                    dir_match = (m.group(1), None)

    if dir_match:
        name, loc = dir_match
        target_path = resolve_target(name, loc)
        args: dict[str, Any] = {"path": target_path}
        if uid is not None and gid is not None:
            args["uid"] = uid
            args["gid"] = gid
        steps.append(Step("create-directory", f"Create directory {target_path}", "dir.create",
                          args, [{"tool": "dir.query", "args": {"path": target_path}}],
                          "system_change"))
        criteria.append({"description": f"Directory {target_path} exists", "tool": "dir.query",
                         "args": {"path": target_path}})

    # 2. File creation
    if not steps:
        file_match = None
        m = re.search(r"\b(?:create|make|touch)\s+(?:a\s+)?file\s+in\s+([^\s]+)\s+(?:(?:called|named)\s+)?([^\s]+)", text, re.IGNORECASE)
        if m:
            file_match = (m.group(2), m.group(1))
        else:
            m = re.search(r"\b(?:create|make|touch)\s+(?:a\s+)?file\s+(?:(?:called|named)\s+)?([^\s]+)\s+in\s+([^\s]+)", text, re.IGNORECASE)
            if m:
                file_match = (m.group(1), m.group(2))
            else:
                m = re.search(r"\b(?:create|make|touch)\s+(?:a\s+)?file\s+(?:(?:called|named)\s+)?([^\s]+)", text, re.IGNORECASE)
                if m:
                    file_match = (m.group(1), None)
                else:
                    m = re.search(r"\btouch\s+([^\s]+)", text, re.IGNORECASE)
                    if m:
                        file_match = (m.group(1), None)
        if file_match:
            name, loc = file_match
            target_path = resolve_target(name, loc)
            args = {"path": target_path, "content": ""}
            if uid is not None and gid is not None:
                args["uid"] = uid
                args["gid"] = gid
            steps.append(Step("create-file", f"Create file {target_path}", "file.write",
                              args, [{"tool": "file.query", "args": {"path": target_path}}],
                              "system_change"))
            criteria.append({"description": f"File {target_path} exists", "tool": "file.query",
                             "args": {"path": target_path}})

    # 3. Package installation
    if not steps:
        package_match = re.search(r"\binstall\s+([a-z0-9][a-z0-9._+-]{0,127})", text_lower)
        if package_match:
            package = package_match.group(1)
            steps.append(Step("install-package", f"Install package {package}", "package.install",
                              {"package": package}, [{"tool": "package.query", "args": {"package": package}}],
                              "system_change"))
            criteria.append({"description": f"Package {package} is installed", "tool": "package.query",
                             "args": {"package": package}})

    # 4. Service restart / start
    if not steps:
        service_match = re.search(r"\b(restart|start)\s+([a-z0-9][a-z0-9._+-]{0,127})", text_lower)
        if service_match:
            action, service = service_match.groups()
            step_id = f"{action}-service"
            dependencies = ["install-package"] if package_match and package_match.group(1) == service else []
            steps.append(Step(step_id, f"{action.title()} service {service}", f"service.{action}",
                              {"name": service}, [{"tool": "service.status", "args": {"name": service}}],
                              "system_change", dependencies))
            criteria.append({"description": f"Service {service} is running", "tool": "service.status",
                             "args": {"name": service}})

    # 5. Monitoring: system information
    if not steps and any(token in text_lower for token in ("system information", "system info", "system status", "os information")):
        steps.append(Step("inspect-system", "Collect system information", "system.info", {}, []))
        criteria.append({"description": "System information was collected", "tool": "system.info", "args": {}})

    # 6. Monitoring: disk usage / status / space
    if not steps and any(token in text_lower for token in ("disk usage", "disk status", "disk space")):
        steps.append(Step("inspect-disk", "Collect disk usage", "disk.status", {}, []))
        criteria.append({"description": "Disk usage was collected", "tool": "disk.status", "args": {}})

    # 7. Monitoring: memory usage / status
    if not steps and any(token in text_lower for token in ("memory usage", "memory status", "memory information")):
        steps.append(Step("inspect-memory", "Collect memory usage", "memory.status", {}, []))
        criteria.append({"description": "Memory usage was collected", "tool": "memory.status", "args": {}})

    # 8. Monitoring: port inspection
    if not steps:
        port_match = re.search(r"\b(?:port|listening on)\s+(\d{1,5})\b", text_lower)
        if port_match and any(token in text_lower for token in ("check", "inspect", "listen", "listening", "what is")):
            port = int(port_match.group(1))
            if 1 <= port <= 65535:
                steps.append(Step("inspect-port", f"Inspect listening port {port}", "port.inspect", {"port": port}, []))
                criteria.append({"description": f"Port {port} was inspected", "tool": "port.inspect", "args": {"port": port}})

    # 9. Monitoring: local http check
    if not steps:
        url_match = re.search(r"\b(http://(?:127\.0\.0\.1|localhost)(?::\d+)?(?:/[^\s]*)?)", text_lower)
        if url_match and any(token in text_lower for token in ("check", "verify", "test", "health")):
            url = url_match.group(1).rstrip(".,;:)")
            steps.append(Step("check-http", f"Check local URL {url}", "http.check", {"url": url}, []))
            criteria.append({"description": f"Local URL {url} is reachable", "tool": "http.check", "args": {"url": url}})

    if not steps:
        return None
    return Plan(goal=intent, success_criteria=criteria, steps=steps,
                assumptions=["Plan was generated by the local typed-task compiler."])


class Harness:
    def __init__(self, store: JobStore, caller: dict[str, Any], mode: str,
                 llm: Callable[[str], str], cancelled: Callable[[], bool] | None = None):
        self.store, self.caller, self.mode, self.llm = store, caller, mode, llm
        self.cancelled = cancelled or (lambda: False)
        self.policy = PolicyEngine()
        self.tools = ToolExecutor(caller)
        self.evidence: list[dict[str, Any]] = []
        self.recommendations: list[str] = []

    def _record_result(self, step: Step, phase: str, result: ToolResult) -> None:
        self.evidence.append({
            "step": step.id, "phase": phase, "tool": step.tool,
            "ok": result.ok, "summary": result.summary,
            "exit_code": result.exit_code, "stderr": result.stderr[:500],
        })
        self.store.event("result", task_idx=0, step=len(self.evidence) - 1,
                         cmd=f"{step.tool} {json.dumps(step.args, sort_keys=True)}",
                         returncode=result.exit_code, stdout=result.stdout, stderr=result.stderr)

    def _inventory(self) -> list[dict[str, Any]]:
        system = self.tools.run("system.info", {})
        disk = self.tools.run("disk.status", {})
        inventory = [{"tool": "system.info", "ok": system.ok, "facts": system.facts}]
        inventory.append({"tool": "disk.status", "ok": disk.ok, "summary": disk.stdout[:1000]})
        self.store.event("inventory", items=inventory)
        return inventory

    def _verify(self, step: Step) -> tuple[bool, str]:
        if not step.verify:
            return True, "No additional verification required for inspection."
        self.store.set_status(JobStatus.VERIFYING, current_step=step.id)
        for item in step.verify:
            tool, args = item.get("tool"), item.get("args")
            if tool not in ALLOWED_TOOLS or not isinstance(args, dict):
                return False, "Plan contained an invalid verifier."
            verifier = Step(step.id + "-verify", "verify " + step.purpose, tool, args, [])
            result = self.tools.run(tool, args)
            self._record_result(verifier, "verify", result)
            if not result.ok:
                return False, result.summary + (": " + result.stderr if result.stderr else "")
        self.store.event("verify", condition=f"Verification passed for {step.purpose}")
        return True, "Verification passed."

    def _diagnose(self, step: Step, failure: str) -> None:
        """Collect bounded, tool-specific facts before reporting a failure.

        This first V2 slice intentionally reports evidence and a safe next
        action. Automatic repair is added only after a strict repair-plan
        schema and rollback implementation are available.
        """
        probes: list[tuple[str, dict[str, Any]]] = [("system.info", {})]
        if step.tool.startswith("service."):
            name = step.args.get("name")
            if isinstance(name, str):
                probes.extend([("service.status", {"name": name}), ("service.logs", {"name": name})])
                recommendation = (
                    f"Review the service status and recent logs for '{name}', then correct the "
                    "reported configuration, dependency, permission, or port conflict before retrying."
                )
            else:
                recommendation = "Inspect the service state and logs before retrying."
        elif step.tool.startswith("package."):
            probes.append(("disk.status", {}))
            recommendation = (
                "Check package-manager output, repository reachability, DNS, and free disk space; "
                "then retry the package action."
            )
        elif step.tool.startswith("dir.") or step.tool.startswith("file."):
            recommendation = (
                "Check target path validity, filesystem permissions, and parent directory accessibility before retrying."
            )
        elif step.tool == "http.check":
            recommendation = (
                "Inspect the local service state and its listening port, then verify the localhost URL again."
            )
        else:
            recommendation = "Review the captured stderr and host facts, correct the underlying issue, then retry."

        evidence_ids: list[int] = []
        for tool, args in probes:
            result = self.tools.run(tool, args)
            self._record_result(Step("diagnose-" + tool.replace(".", "-"), "diagnostic probe", tool, args, []),
                                "diagnose", result)
            evidence_ids.append(len(self.evidence) - 1)
        self.recommendations.append(recommendation)
        self.store.event("diagnosis", step_id=step.id, classification="suspected",
                         failure=failure, evidence_ids=evidence_ids, recommendation=recommendation)

    def _final_criteria(self, plan: Plan) -> tuple[bool, list[str]]:
        failures: list[str] = []
        for criterion in plan.success_criteria:
            tool, args = criterion.get("tool"), criterion.get("args")
            if tool not in ALLOWED_TOOLS or not isinstance(args, dict):
                failures.append("Plan contained an invalid success criterion.")
                continue
            result = self.tools.run(tool, args)
            synthetic = Step("final-verify", "final success criterion", tool, args, [])
            self._record_result(synthetic, "final_verify", result)
            if not result.ok:
                failures.append(criterion.get("description", result.summary))
        return not failures, failures

    def run(self, intent: str) -> JobStatus:
        self.store.set_status(JobStatus.PLANNING)
        self.store.event("planner_start", intent=intent)
        inventory = self._inventory()
        raw = self.llm(plan_prompt(intent, inventory))
        try:
            cleaned_raw = raw.strip()
            match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", cleaned_raw, re.DOTALL)
            if match:
                cleaned_raw = match.group(1)
            else:
                match = re.search(r"(\{.*\})", cleaned_raw, re.DOTALL)
                if match:
                    cleaned_raw = match.group(1)
            plan = validate_plan(json.loads(cleaned_raw))
        except (json.JSONDecodeError, PlanError) as exc:
            plan = deterministic_plan(intent, caller=self.caller)
            if plan is None:
                reason = f"Planner returned an invalid structured plan: {exc}"
                self.store.set_status(JobStatus.BLOCKED, reason=reason)
                self.store.event("task_fail", task_idx=0, reason="invalid_plan", msg=reason)
                self.store.event("done", status="FAILED", task_count=0, elapsed_s=0, summaries=[reason])
                self.store.write_report(JobStatus.BLOCKED, {"goal": intent, "problems": [reason], "evidence": self.evidence})
                return JobStatus.BLOCKED
            self.store.event("planner_fallback", reason="Used deterministic typed-task plan after invalid model plan.")

        self.store.save_plan(plan)
        self.store.event("plan", tasks=[step.purpose for step in plan.steps], count=len(plan.steps))
        self.store.set_status(JobStatus.RUNNING, goal=plan.goal)
        problems: list[str] = []
        for index, step in enumerate(plan.steps):
            if self.cancelled():
                self.store.set_status(JobStatus.CANCELLED, reason="Cancellation requested")
                self.store.event("done", status="CANCELLED", task_count=index, elapsed_s=0, summaries=problems)
                return JobStatus.CANCELLED
            if any(dep not in self.store.state["completed_steps"] for dep in step.depends_on):
                problems.append(f"Step '{step.id}' has unmet dependencies.")
                self.store.state["failed_steps"].append(step.id)
                continue
            decision, reason = self.policy.decide(self.mode, step)
            if decision == "SUGGEST":
                self.store.event("suggest_skip", task_idx=index, task=step.purpose, note=reason)
                continue
            if decision != "ALLOW":
                self.store.set_status(JobStatus.NEEDS_APPROVAL, approval={"step": step.id, "reason": reason})
                self.store.event("task_fail", task_idx=index, reason="approval_required", msg=reason)
                self.store.write_report(JobStatus.NEEDS_APPROVAL, {"goal": plan.goal, "problems": [reason], "evidence": self.evidence})
                self.store.event("done", status="FAILED", task_count=index, elapsed_s=0, summaries=[reason])
                return JobStatus.NEEDS_APPROVAL
            self.store.event("task_start", task_idx=index, task=step.purpose)
            self.store.event("step", task_idx=index, step=0, cwd=self.caller.get("cwd", "/"))
            self.store.event("step_exec", task_idx=index, step=0, cmd=f"{step.tool} {json.dumps(step.args, sort_keys=True)}")
            result = self.tools.run(step.tool, step.args)
            self._record_result(step, "execute", result)
            if not result.ok:
                problem = f"{step.purpose}: {result.summary}"
                problems.append(problem)
                self._diagnose(step, problem)
                self.store.state["failed_steps"].append(step.id)
                self.store._save()
                self.store.event("task_fail", task_idx=index, reason="tool_failed", msg=problem)
                break
            verified, message = self._verify(step)
            if not verified:
                problem = f"{step.purpose}: verification failed: {message}"
                problems.append(problem)
                self._diagnose(step, problem)
                self.store.state["failed_steps"].append(step.id)
                self.store._save()
                self.store.event("task_fail", task_idx=index, reason="verification_failed", msg=problem)
                break
            self.store.state["completed_steps"].append(step.id)
            self.store._save()
            self.store.event("task_finish", task_idx=index, step=0, summary=message)
            self.store.set_status(JobStatus.RUNNING)

        all_verified, final_failures = self._final_criteria(plan)
        problems.extend(final_failures)
        status = JobStatus.SUCCEEDED if all_verified and not problems else JobStatus.PARTIAL
        self.store.set_status(status, problems=problems)
        report = {"goal": plan.goal, "completed_steps": self.store.state["completed_steps"],
                  "failed_steps": self.store.state["failed_steps"], "problems": problems,
                  "recommendations": self.recommendations, "evidence": self.evidence}
        self.store.write_report(status, report)
        self.store.event("done", status="DONE" if status == JobStatus.SUCCEEDED else "FAILED",
                         task_count=len(plan.steps), elapsed_s=0, summaries=problems or ["All success criteria passed."])
        return status
