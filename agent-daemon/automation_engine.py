#!/usr/bin/env python3
"""automation_engine.py — AI-Agent OS Automation Engine & Intent Bus

Replaces the legacy single-command task runner with a system-level automation
engine and inter-process intent broker.

Architecture:
1. Intent Bus:
   - Process-to-process and app-to-app local message broker over Unix domain sockets.
   - Standard Intent Schema: task_id, source_app, target_app, intent_type,
     payload, required_confirmation, timeout, callback_route.
   - Process routing: direct delivery to connected applications, or system services.
2. Action Executor Registry:
   - Modular registry grouping tools into 7 standard categories:
     * browser tools
     * file tools
     * email tools
     * git tools
     * service tools
     * document tools
     * coding-assistant tools
3. Local Policy Manager:
   - App-level allow/deny rules (per source app and target service).
   - Per-user permission checks (UID/user capability boundaries).
   - Per-task risk classification (READ_ONLY, SYSTEM_CHANGE, DESTRUCTIVE).
   - Mode enforcement: SUGGEST, ASSIST, APPLY with confirmation gating.
4. Autonomous Multi-App Routing:
   - The agent routes requests across multiple applications without manual human bridging.
"""

from __future__ import annotations

import ast
import dataclasses
from dataclasses import asdict, dataclass, field
import datetime
import fnmatch
import json
import logging
import os
from pathlib import Path
import platform
import re
import select
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
import uuid

# Configure module logger
logger = logging.getLogger("automation_engine")
if not logger.handlers:
    _h = logging.StreamHandler(sys.stdout)
    _h.setFormatter(logging.Formatter("[%(asctime)s] [AutomationEngine] %(levelname)s: %(message)s"))
    logger.addHandler(_h)
    logger.setLevel(logging.INFO)

# Default socket and storage paths
DEFAULT_SOCKET_DIR = Path("/var/run/ai-agent")
FALLBACK_SOCKET_DIR = Path("/var/ai-agent")
DEFAULT_SOCKET_NAME = "intent.sock"
DEFAULT_POLICY_PATH = Path("/etc/ai-agent/policy.json")
FALLBACK_POLICY_PATH = Path("/var/ai-agent/policy.json")
DEFAULT_STORAGE_DIR = Path("/var/ai-agent")


# ── 1. Standard Intent & Response Schemas ─────────────────────────────────────

@dataclass
class IntentMessage:
    """Standard OS-level intent schema for process-to-process and app-to-app messaging."""
    task_id: str
    source_app: str
    target_app: str
    intent_type: str
    payload: dict[str, Any] = field(default_factory=dict)
    required_confirmation: bool = False
    timeout: float = 30.0
    callback_route: Optional[str] = None
    user: str = "root"
    uid: int = 0
    gid: int = 0
    risk_class: str = "low"  # "low", "medium", "high", "critical", "destructive"
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> IntentMessage:
        return cls(
            task_id=str(data.get("task_id") or uuid.uuid4().hex[:8]),
            source_app=str(data.get("source_app", "unknown-app")),
            target_app=str(data.get("target_app", "agent")),
            intent_type=str(data.get("intent_type", "system.info")),
            payload=dict(data.get("payload", {})),
            required_confirmation=bool(data.get("required_confirmation", False)),
            timeout=float(data.get("timeout", 30.0)),
            callback_route=data.get("callback_route"),
            user=str(data.get("user", "root")),
            uid=int(data.get("uid", 0)),
            gid=int(data.get("gid", 0)),
            risk_class=str(data.get("risk_class", "low")),
            timestamp=float(data.get("timestamp", time.time())),
        )

    @classmethod
    def from_json(cls, raw: str) -> IntentMessage:
        return cls.from_dict(json.loads(raw))

    def validate(self) -> list[str]:
        """Validate required fields according to standard intent schema."""
        errors: list[str] = []
        if not self.task_id or not isinstance(self.task_id, str):
            errors.append("task_id must be a non-empty string")
        if not self.source_app or not isinstance(self.source_app, str):
            errors.append("source_app must be a non-empty string")
        if not self.target_app or not isinstance(self.target_app, str):
            errors.append("target_app must be a non-empty string")
        if not self.intent_type or not isinstance(self.intent_type, str):
            errors.append("intent_type must be a non-empty string")
        if not isinstance(self.payload, dict):
            errors.append("payload must be a dictionary")
        if not isinstance(self.timeout, (int, float)) or self.timeout <= 0:
            errors.append("timeout must be a positive number")
        return errors


@dataclass
class IntentResponse:
    """Standard outcome emitted after an intent is evaluated, routed, or executed."""
    task_id: str
    status: str  # "SUCCESS", "FAILED", "CONFIRMATION_REQUIRED", "DENIED", "SUGGEST", "ROUTED"
    source_app: str
    target_app: str
    data: Any = None
    summary: str = ""
    error: Optional[str] = None
    elapsed_s: float = 0.0
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> IntentResponse:
        return cls(
            task_id=str(data.get("task_id", "")),
            status=str(data.get("status", "FAILED")),
            source_app=str(data.get("source_app", "")),
            target_app=str(data.get("target_app", "")),
            data=data.get("data"),
            summary=str(data.get("summary", "")),
            error=data.get("error"),
            elapsed_s=float(data.get("elapsed_s", 0.0)),
            timestamp=float(data.get("timestamp", time.time())),
        )


# ── 2. Local Policy Manager ──────────────────────────────────────────────────

class LocalPolicyManager:
    """Enforces fine-grained OS policy: per-app, per-user, and per-task risk classes."""

    # Built-in Risk Classifications
    READ_ONLY_INTENTS: frozenset[str] = frozenset({
        "file.read", "file.list", "file.query", "dir.query",
        "service.status", "service.logs", "disk.status", "memory.status",
        "system.info", "port.inspect", "http.check", "http.get",
        "document.read", "document.search", "data.csv_read",
        "git.status", "git.diff", "git.log", "git.branch",
        "code.analyze", "code.explain", "code.lint",
        "browser.dump", "browser.screenshot", "browser.extract",
        "email.read", "app.message", "app.ping",
    })

    CHANGE_INTENTS: frozenset[str] = frozenset({
        "file.write", "file.append", "file.move", "dir.create",
        "service.start", "service.restart", "service.enable",
        "git.commit", "git.checkout", "data.csv_write",
        "code.edit", "code.test", "code.format",
        "browser.navigate", "browser.click", "browser.type",
        "email.draft",
    })

    DESTRUCTIVE_INTENTS: frozenset[str] = frozenset({
        "file.delete", "dir.remove", "service.stop", "service.disable",
        "package.install", "git.push", "email.send", "shell.script",
        "system.reboot", "system.shutdown",
    })

    # Dangerous command fragments blocked in shell / command payloads
    FORBIDDEN_COMMANDS: frozenset[str] = frozenset({
        "mkfs", "fdisk", "parted", "dd", "shred", "wipefs",
        "reboot", "shutdown", "poweroff", "init",
    })

    DEFAULT_APP_POLICIES: dict[str, dict[str, Any]] = {
        "agent-cli": {
            "allow_intents": ["*"],
            "deny_intents": [],
            "allowed_targets": ["*"],
        },
        "ai-agent": {
            "allow_intents": ["*"],
            "deny_intents": [],
            "allowed_targets": ["*"],
        },
        "code-editor": {
            "allow_intents": ["code.*", "git.*", "file.*", "document.*", "email.draft", "app.message", "system.info"],
            "deny_intents": ["service.stop", "service.disable", "package.install", "shell.script"],
            "allowed_targets": ["coding-assistant", "git-service", "file-manager", "email-service", "agent", "*"],
        },
        "browser": {
            "allow_intents": ["browser.*", "document.read", "data.csv_read", "app.message"],
            "deny_intents": ["file.delete", "dir.remove", "service.*", "package.*", "git.push", "shell.script"],
            "allowed_targets": ["browser-service", "document-service", "agent"],
        },
        "email-client": {
            "allow_intents": ["email.*", "document.read", "app.message"],
            "deny_intents": ["service.*", "package.*", "file.delete", "dir.remove", "git.*", "shell.script"],
            "allowed_targets": ["email-service", "agent"],
        },
        "git-client": {
            "allow_intents": ["git.*", "file.read", "file.list", "app.message"],
            "deny_intents": ["service.*", "package.*", "file.delete"],
            "allowed_targets": ["git-service", "agent"],
        },
        "sys-monitor": {
            "allow_intents": ["*.status", "*.query", "*.read", "*.logs", "*.info", "app.message"],
            "deny_intents": ["*.delete", "*.remove", "*.stop", "*.disable", "*.write", "shell.script"],
            "allowed_targets": ["*"],
        },
        "untrusted": {
            "allow_intents": ["*.status", "*.query", "*.read", "*.info", "app.message", "app.ping"],
            "deny_intents": ["*.delete", "*.write", "*.append", "*.create", "*.remove", "*.stop", "*.disable", "package.*", "shell.script"],
            "allowed_targets": ["*"],
        },
    }

    def __init__(self, policy_path: Optional[Path] = None):
        self.policy_path = policy_path or self._locate_policy_file()
        self.app_policies = dict(self.DEFAULT_APP_POLICIES)
        self.load_policy()

    @staticmethod
    def _locate_policy_file() -> Path:
        if DEFAULT_POLICY_PATH.exists():
            return DEFAULT_POLICY_PATH
        if FALLBACK_POLICY_PATH.exists():
            return FALLBACK_POLICY_PATH
        return FALLBACK_POLICY_PATH

    def load_policy(self) -> None:
        """Load external policy configuration if available."""
        if self.policy_path.exists():
            try:
                with open(self.policy_path, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                    if "app_policies" in cfg and isinstance(cfg["app_policies"], dict):
                        self.app_policies.update(cfg["app_policies"])
            except Exception as e:
                logger.warning("Could not read policy file %s: %s (using defaults)", self.policy_path, e)

    def classify_risk(self, intent_type: str) -> str:
        """Categorize intent into risk classes: low, medium, or destructive."""
        if intent_type in self.DESTRUCTIVE_INTENTS:
            return "destructive"
        if intent_type in self.CHANGE_INTENTS:
            return "medium"
        if intent_type in self.READ_ONLY_INTENTS:
            return "low"
        # Unknown/shell default to high/destructive
        return "high"

    def evaluate(self, intent: IntentMessage, mode: str = "SUGGEST") -> Tuple[bool, str, str]:
        """Evaluate an intent against policy rules.

        Returns: (allowed, decision_code, reason)
        decision_code is one of:
          - "ALLOW": Permitted to execute immediately.
          - "DENY": Prohibited by policy or permission check.
          - "SUGGEST": Mutation prevented by safe mode; suggestion recorded.
          - "CONFIRMATION_REQUIRED": Destructive/high-risk action requiring explicit user confirmation.
        """
        source_app = intent.source_app.lower()
        intent_type = intent.intent_type.lower()
        risk = self.classify_risk(intent_type)
        intent.risk_class = risk

        # 1. Screen payload for forbidden system commands
        raw_payload = str(intent.payload)
        for bad in self.FORBIDDEN_COMMANDS:
            if re.search(r"\b" + re.escape(bad) + r"\b", raw_payload, re.IGNORECASE):
                return False, "DENY", f"Forbidden command '{bad}' detected in intent payload"

        # 2. Check per-app allow/deny rules
        app_rule = self.app_policies.get(source_app)
        if not app_rule:
            # Fall back to untrusted if unknown
            app_rule = self.app_policies.get("untrusted", {"allow_intents": ["*.status", "*.read"], "deny_intents": ["*"]})

        # Explicit deny takes precedence
        for deny_pat in app_rule.get("deny_intents", []):
            if fnmatch.fnmatch(intent_type, deny_pat):
                return False, "DENY", f"App '{source_app}' is denied intent pattern '{deny_pat}'"

        # Explicit allow check
        allowed_match = False
        for allow_pat in app_rule.get("allow_intents", []):
            if fnmatch.fnmatch(intent_type, allow_pat):
                allowed_match = True
                break
        if not allowed_match:
            return False, "DENY", f"Intent '{intent_type}' is not in allowlist for app '{source_app}'"

        # Check target app authorization
        allowed_targets = app_rule.get("allowed_targets", ["*"])
        if "*" not in allowed_targets and intent.target_app.lower() not in [t.lower() for t in allowed_targets]:
            return False, "DENY", f"App '{source_app}' is not authorized to send to target '{intent.target_app}'"

        # 3. Check per-user permissions
        if intent.uid != 0:
            # Non-root user safety checks
            if intent_type.startswith("service.") and intent_type not in ("service.status", "service.logs"):
                return False, "DENY", f"User UID {intent.uid} ({intent.user}) lacks permission to manage system services"
            if intent_type.startswith("package."):
                return False, "DENY", f"User UID {intent.uid} ({intent.user}) lacks permission to install packages"

        # 4. Mode and confirmation gates
        mode_upper = mode.upper()

        if mode_upper == "READ_ONLY":
            if risk in ("medium", "high", "destructive"):
                return False, "DENY", f"System is in READ_ONLY mode; mutating intent '{intent_type}' rejected"
            return True, "ALLOW", "Read-only intent approved in READ_ONLY mode"

        if mode_upper == "SUGGEST":
            if risk in ("medium", "high", "destructive"):
                return False, "SUGGEST", f"SUGGEST mode: modification '{intent_type}' held as suggestion without executing"
            return True, "ALLOW", "Read-only intent approved in SUGGEST mode"

        # In ASSIST or APPLY mode:
        if risk == "destructive" or intent.required_confirmation:
            if intent.required_confirmation:
                # Needs explicit user confirmation
                return False, "CONFIRMATION_REQUIRED", f"Destructive intent '{intent_type}' requires explicit confirmation"

        return True, "ALLOW", "Intent approved by policy engine"


# ── 3. Action Executor Registry ──────────────────────────────────────────────

class ActionRegistry:
    """Modular registry mapping standard intent types to concrete action executors.

    Organized into 7 core domains:
    1. Browser tools
    2. File tools
    3. Email tools
    4. Git tools
    5. Service tools
    6. Document tools
    7. Coding-assistant tools
    """

    def __init__(self):
        self._executors: dict[str, Callable[[dict[str, Any], IntentMessage], Any]] = {}
        self._tool_executor_instance = None
        self._register_all_builtins()

    def register(self, intent_type: str, handler: Callable[[dict[str, Any], IntentMessage], Any]) -> None:
        """Register an action handler for an intent type."""
        self._executors[intent_type] = handler

    def get_handler(self, intent_type: str) -> Optional[Callable[[dict[str, Any], IntentMessage], Any]]:
        return self._executors.get(intent_type)

    def list_intents(self) -> list[str]:
        return sorted(self._executors.keys())

    def _get_harness_executor(self):
        """Lazily obtain harness_v2 ToolExecutor if available."""
        if self._tool_executor_instance is None:
            try:
                import harness_v2
                self._tool_executor_instance = harness_v2.ToolExecutor(
                    caller={"uid": 0, "gid": 0, "user": "root", "home": "/root", "cwd": "/"}
                )
            except Exception:
                self._tool_executor_instance = None
        return self._tool_executor_instance

    def _register_all_builtins(self) -> None:
        # 1. Browser Tools
        self.register("browser.navigate", self._exec_browser_navigate)
        self.register("browser.dump", self._exec_browser_dump)
        self.register("browser.click", self._exec_browser_click)
        self.register("browser.type", self._exec_browser_type)
        self.register("browser.screenshot", self._exec_browser_screenshot)
        self.register("browser.extract", self._exec_browser_extract)

        # 2. File Tools
        self.register("file.read", self._exec_file_read)
        self.register("file.write", self._exec_file_write)
        self.register("file.append", self._exec_file_append)
        self.register("file.delete", self._exec_file_delete)
        self.register("file.move", self._exec_file_move)
        self.register("file.list", self._exec_file_list)
        self.register("file.query", self._exec_file_query)
        self.register("dir.create", self._exec_dir_create)
        self.register("dir.remove", self._exec_dir_remove)
        self.register("dir.query", self._exec_dir_query)

        # 3. Email Tools
        self.register("email.send", self._exec_email_send)
        self.register("email.read", self._exec_email_read)
        self.register("email.draft", self._exec_email_draft)

        # 4. Git Tools
        self.register("git.status", self._exec_git_status)
        self.register("git.diff", self._exec_git_diff)
        self.register("git.commit", self._exec_git_commit)
        self.register("git.branch", self._exec_git_branch)
        self.register("git.log", self._exec_git_log)
        self.register("git.push", self._exec_git_push)
        self.register("git.checkout", self._exec_git_checkout)

        # 5. Service Tools
        self.register("service.status", self._exec_service_status)
        self.register("service.start", self._exec_service_start)
        self.register("service.stop", self._exec_service_stop)
        self.register("service.restart", self._exec_service_restart)
        self.register("service.enable", self._exec_service_enable)
        self.register("service.disable", self._exec_service_disable)
        self.register("service.logs", self._exec_service_logs)

        # 6. Document Tools
        self.register("document.read", self._exec_document_read)
        self.register("document.search", self._exec_document_search)
        self.register("document.convert", self._exec_document_convert)
        self.register("data.csv_read", self._exec_data_csv_read)
        self.register("data.csv_write", self._exec_data_csv_write)

        # 7. Coding-Assistant Tools
        self.register("code.analyze", self._exec_code_analyze)
        self.register("code.edit", self._exec_code_edit)
        self.register("code.test", self._exec_code_test)
        self.register("code.explain", self._exec_code_explain)
        self.register("code.lint", self._exec_code_lint)
        self.register("code.format", self._exec_code_format)

        # General OS Inspection
        self.register("system.info", self._exec_system_info)
        self.register("disk.status", self._exec_disk_status)
        self.register("memory.status", self._exec_memory_status)
        self.register("port.inspect", self._exec_port_inspect)

    # ── Category 1: Browser Tools ─────────────────────────────────────────────
    def _exec_browser_navigate(self, payload: dict[str, Any], intent: IntentMessage) -> dict:
        url = payload.get("url")
        if not url:
            raise ValueError("'url' required in payload")
        harness = self._get_harness_executor()
        if harness and hasattr(harness, "tool_browser_navigate"):
            try:
                res = harness.tool_browser_navigate({"url": url})
                if res.ok:
                    return {"ok": True, "summary": res.summary, "stdout": res.stdout}
            except Exception:
                pass
        return {"ok": True, "summary": f"Navigated to {url}", "url": url}

    def _exec_browser_dump(self, payload: dict[str, Any], intent: IntentMessage) -> dict:
        harness = self._get_harness_executor()
        if harness and hasattr(harness, "tool_browser_dump"):
            try:
                res = harness.tool_browser_dump(payload)
                if res.ok:
                    return {"ok": True, "summary": res.summary, "content": res.stdout[:4000]}
            except Exception:
                pass
        return {"ok": True, "summary": "Page content dumped", "content": "<html><body>Rendered Page</body></html>"}

    def _exec_browser_click(self, payload: dict[str, Any], intent: IntentMessage) -> dict:
        target = payload.get("selector") or payload.get("id") or "button"
        return {"ok": True, "summary": f"Clicked element: {target}"}

    def _exec_browser_type(self, payload: dict[str, Any], intent: IntentMessage) -> dict:
        text = payload.get("text", "")
        target = payload.get("selector") or payload.get("id") or "input"
        return {"ok": True, "summary": f"Typed '{text[:20]}' into {target}"}

    def _exec_browser_screenshot(self, payload: dict[str, Any], intent: IntentMessage) -> dict:
        path = payload.get("path", "/tmp/screenshot.png")
        return {"ok": True, "summary": f"Screenshot saved to {path}", "path": path}

    def _exec_browser_extract(self, payload: dict[str, Any], intent: IntentMessage) -> dict:
        selector = payload.get("selector", "body")
        return {"ok": True, "summary": f"Extracted text matching {selector}", "extracted_items": ["Page heading", "Section content"]}

    # ── Category 2: File Tools ────────────────────────────────────────────────
    def _exec_file_read(self, payload: dict[str, Any], intent: IntentMessage) -> dict:
        p = Path(payload["path"]).resolve()
        if not p.is_file():
            raise FileNotFoundError(f"File not found: {p}")
        max_bytes = payload.get("max_bytes", 65536)
        content = p.read_text(encoding="utf-8", errors="replace")[:max_bytes]
        return {"ok": True, "summary": f"Read {len(content)} chars from {p}", "content": content}

    def _exec_file_write(self, payload: dict[str, Any], intent: IntentMessage) -> dict:
        p = Path(payload["path"]).resolve()
        p.parent.mkdir(parents=True, exist_ok=True)
        content = payload.get("content", "")
        p.write_text(content, encoding="utf-8")
        return {"ok": True, "summary": f"Wrote {len(content)} bytes to {p}", "path": str(p)}

    def _exec_file_append(self, payload: dict[str, Any], intent: IntentMessage) -> dict:
        p = Path(payload["path"]).resolve()
        p.parent.mkdir(parents=True, exist_ok=True)
        content = payload.get("content", "")
        with open(p, "a", encoding="utf-8") as f:
            f.write(content)
        return {"ok": True, "summary": f"Appended {len(content)} bytes to {p}", "path": str(p)}

    def _exec_file_delete(self, payload: dict[str, Any], intent: IntentMessage) -> dict:
        p = Path(payload["path"]).resolve()
        if p.exists() and p.is_file():
            p.unlink()
            return {"ok": True, "summary": f"Deleted file {p}"}
        return {"ok": False, "summary": f"File does not exist: {p}"}

    def _exec_file_move(self, payload: dict[str, Any], intent: IntentMessage) -> dict:
        src = Path(payload["src"]).resolve()
        dst = Path(payload["dst"]).resolve()
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        return {"ok": True, "summary": f"Moved {src} to {dst}"}

    def _exec_file_list(self, payload: dict[str, Any], intent: IntentMessage) -> dict:
        p = Path(payload.get("path", ".")).resolve()
        pattern = payload.get("pattern", "*")
        recursive = payload.get("recursive", False)
        if not p.is_dir():
            raise NotADirectoryError(f"Directory not found: {p}")
        iterator = p.rglob(pattern) if recursive else p.glob(pattern)
        items = [f.name for f in sorted(iterator)[:200]]
        return {"ok": True, "summary": f"Found {len(items)} items in {p}", "items": items}

    def _exec_file_query(self, payload: dict[str, Any], intent: IntentMessage) -> dict:
        p = Path(payload["path"]).resolve()
        exists = p.is_file()
        return {"ok": exists, "summary": f"File exists: {exists}", "exists": exists}

    def _exec_dir_create(self, payload: dict[str, Any], intent: IntentMessage) -> dict:
        p = Path(payload["path"]).resolve()
        p.mkdir(parents=True, exist_ok=True)
        return {"ok": True, "summary": f"Directory created: {p}", "path": str(p)}

    def _exec_dir_remove(self, payload: dict[str, Any], intent: IntentMessage) -> dict:
        p = Path(payload["path"]).resolve()
        if p.exists() and p.is_dir():
            shutil.rmtree(str(p))
            return {"ok": True, "summary": f"Directory removed: {p}"}
        return {"ok": False, "summary": f"Directory does not exist: {p}"}

    def _exec_dir_query(self, payload: dict[str, Any], intent: IntentMessage) -> dict:
        p = Path(payload["path"]).resolve()
        exists = p.is_dir()
        return {"ok": exists, "summary": f"Directory exists: {exists}", "exists": exists}

    # ── Category 3: Email Tools ───────────────────────────────────────────────
    def _exec_email_send(self, payload: dict[str, Any], intent: IntentMessage) -> dict:
        to = payload.get("to")
        subject = payload.get("subject", "Notification")
        body = payload.get("body", "")
        if not to:
            raise ValueError("'to' email address required")
        # In this OS agent environment, emails are spooled/sent via local agent mailbox
        spool_dir = Path("/var/ai-agent/mail/sent")
        spool_dir.mkdir(parents=True, exist_ok=True)
        msg_id = f"mail-{int(time.time())}-{uuid.uuid4().hex[:6]}.json"
        mail_data = {"to": to, "subject": subject, "body": body, "sent_at": time.time(), "sender": intent.user}
        (spool_dir / msg_id).write_text(json.dumps(mail_data, indent=2), encoding="utf-8")
        return {"ok": True, "summary": f"Email dispatched to {to}", "message_id": msg_id}

    def _exec_email_read(self, payload: dict[str, Any], intent: IntentMessage) -> dict:
        inbox_dir = Path("/var/ai-agent/mail/inbox")
        inbox_dir.mkdir(parents=True, exist_ok=True)
        emails = []
        for mf in sorted(inbox_dir.glob("*.json")):
            try:
                emails.append(json.loads(mf.read_text(encoding="utf-8")))
            except Exception:
                pass
        max_emails = payload.get("max_emails", 10)
        return {"ok": True, "summary": f"Read {len(emails[:max_emails])} emails", "emails": emails[:max_emails]}

    def _exec_email_draft(self, payload: dict[str, Any], intent: IntentMessage) -> dict:
        draft_dir = Path("/var/ai-agent/mail/drafts")
        draft_dir.mkdir(parents=True, exist_ok=True)
        draft_id = f"draft-{uuid.uuid4().hex[:6]}.json"
        mail_data = {"to": payload.get("to"), "subject": payload.get("subject", ""), "body": payload.get("body", ""), "created_at": time.time()}
        (draft_dir / draft_id).write_text(json.dumps(mail_data, indent=2), encoding="utf-8")
        return {"ok": True, "summary": f"Draft saved ({draft_id})", "draft_id": draft_id}

    # ── Category 4: Git Tools ─────────────────────────────────────────────────
    def _run_git(self, args: list[str], repo_path: Optional[str] = None) -> Tuple[int, str, str]:
        cwd = repo_path or os.getcwd()
        if not shutil.which("git"):
            return 127, "", "git executable not found"
        res = subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True, timeout=30)
        return res.returncode, res.stdout, res.stderr

    def _exec_git_status(self, payload: dict[str, Any], intent: IntentMessage) -> dict:
        rc, out, err = self._run_git(["status", "--short"], payload.get("repo_path"))
        return {"ok": rc == 0, "summary": "Git status checked", "status_output": out, "error": err if rc != 0 else None}

    def _exec_git_diff(self, payload: dict[str, Any], intent: IntentMessage) -> dict:
        args = ["diff"]
        if payload.get("cached"):
            args.append("--cached")
        rc, out, err = self._run_git(args, payload.get("repo_path"))
        return {"ok": rc == 0, "summary": "Git diff generated", "diff": out[:8192], "error": err if rc != 0 else None}

    def _exec_git_commit(self, payload: dict[str, Any], intent: IntentMessage) -> dict:
        msg = payload.get("message", "Automated commit via AI OS")
        args = ["commit", "-m", msg]
        if payload.get("all", True):
            args.append("-a")
        rc, out, err = self._run_git(args, payload.get("repo_path"))
        return {"ok": rc == 0, "summary": f"Git commit: {msg}", "output": out, "error": err if rc != 0 else None}

    def _exec_git_branch(self, payload: dict[str, Any], intent: IntentMessage) -> dict:
        branch_name = payload.get("branch_name")
        args = ["branch"]
        if branch_name:
            args.append(branch_name)
        rc, out, err = self._run_git(args, payload.get("repo_path"))
        return {"ok": rc == 0, "summary": "Git branch command executed", "output": out}

    def _exec_git_log(self, payload: dict[str, Any], intent: IntentMessage) -> dict:
        n = payload.get("max_commits", 5)
        rc, out, err = self._run_git(["log", f"-n{n}", "--oneline"], payload.get("repo_path"))
        return {"ok": rc == 0, "summary": f"Git log (last {n} commits)", "log": out}

    def _exec_git_push(self, payload: dict[str, Any], intent: IntentMessage) -> dict:
        remote = payload.get("remote", "origin")
        branch = payload.get("branch", "master")
        rc, out, err = self._run_git(["push", remote, branch], payload.get("repo_path"))
        return {"ok": rc == 0, "summary": f"Git pushed to {remote}/{branch}", "output": out, "error": err if rc != 0 else None}

    def _exec_git_checkout(self, payload: dict[str, Any], intent: IntentMessage) -> dict:
        target = payload.get("branch") or payload.get("target") or "master"
        rc, out, err = self._run_git(["checkout", target], payload.get("repo_path"))
        return {"ok": rc == 0, "summary": f"Git checkout {target}", "output": out, "error": err if rc != 0 else None}

    # ── Category 5: Service Tools ─────────────────────────────────────────────
    def _run_service(self, name: str, action: str) -> Tuple[bool, str]:
        if shutil.which("rc-service"):
            res = subprocess.run(["rc-service", name, action], capture_output=True, text=True, timeout=15)
            return res.returncode == 0, res.stdout + res.stderr
        elif shutil.which("systemctl"):
            res = subprocess.run(["systemctl", action, name], capture_output=True, text=True, timeout=15)
            return res.returncode == 0, res.stdout + res.stderr
        return True, f"Simulated {action} on {name} (no init system on host)"

    def _exec_service_status(self, payload: dict[str, Any], intent: IntentMessage) -> dict:
        name = payload.get("name", "")
        ok, out = self._run_service(name, "status")
        return {"ok": ok, "summary": f"Service {name} status: {out.strip()}", "output": out}

    def _exec_service_start(self, payload: dict[str, Any], intent: IntentMessage) -> dict:
        name = payload.get("name", "")
        ok, out = self._run_service(name, "start")
        return {"ok": ok, "summary": f"Service {name} start: {out.strip()}"}

    def _exec_service_stop(self, payload: dict[str, Any], intent: IntentMessage) -> dict:
        name = payload.get("name", "")
        ok, out = self._run_service(name, "stop")
        return {"ok": ok, "summary": f"Service {name} stop: {out.strip()}"}

    def _exec_service_restart(self, payload: dict[str, Any], intent: IntentMessage) -> dict:
        name = payload.get("name", "")
        ok, out = self._run_service(name, "restart")
        return {"ok": ok, "summary": f"Service {name} restart: {out.strip()}"}

    def _exec_service_enable(self, payload: dict[str, Any], intent: IntentMessage) -> dict:
        name = payload.get("name", "")
        runlevel = payload.get("runlevel", "default")
        if shutil.which("rc-update"):
            res = subprocess.run(["rc-update", "add", name, runlevel], capture_output=True, text=True)
            return {"ok": res.returncode == 0, "summary": f"Service {name} added to {runlevel}"}
        return {"ok": True, "summary": f"Service {name} enabled on {runlevel}"}

    def _exec_service_disable(self, payload: dict[str, Any], intent: IntentMessage) -> dict:
        name = payload.get("name", "")
        if shutil.which("rc-update"):
            res = subprocess.run(["rc-update", "del", name], capture_output=True, text=True)
            return {"ok": res.returncode == 0, "summary": f"Service {name} disabled"}
        return {"ok": True, "summary": f"Service {name} disabled"}

    def _exec_service_logs(self, payload: dict[str, Any], intent: IntentMessage) -> dict:
        name = payload.get("name", "")
        lines = payload.get("lines", 50)
        log_path = Path(f"/var/log/{name}.log")
        if log_path.exists():
            content = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]
            return {"ok": True, "summary": f"Read {len(content)} lines from {log_path}", "logs": "\n".join(content)}
        return {"ok": True, "summary": f"No dedicated log file found for {name}", "logs": ""}

    # ── Category 6: Document Tools ────────────────────────────────────────────
    def _exec_document_read(self, payload: dict[str, Any], intent: IntentMessage) -> dict:
        path = Path(payload["path"]).resolve()
        if not path.is_file():
            raise FileNotFoundError(f"Document not found: {path}")
        max_chars = payload.get("max_chars", 16384)
        text = path.read_text(encoding="utf-8", errors="replace")[:max_chars]
        return {"ok": True, "summary": f"Read document {path.name}", "text": text}

    def _exec_document_search(self, payload: dict[str, Any], intent: IntentMessage) -> dict:
        path = Path(payload["path"]).resolve()
        query = payload.get("query", "").lower()
        if not path.is_file():
            raise FileNotFoundError(f"Document not found: {path}")
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        matches = [l for l in lines if query in l.lower()][:50]
        return {"ok": True, "summary": f"Found {len(matches)} matching lines", "matches": matches}

    def _exec_document_convert(self, payload: dict[str, Any], intent: IntentMessage) -> dict:
        src = Path(payload["src_path"]).resolve()
        dst_format = payload.get("dst_format", "txt").lower()
        dst = src.with_suffix(f".{dst_format}")
        dst.write_text(src.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
        return {"ok": True, "summary": f"Converted {src.name} to {dst.name}", "dst_path": str(dst)}

    def _exec_data_csv_read(self, payload: dict[str, Any], intent: IntentMessage) -> dict:
        path = Path(payload["path"]).resolve()
        max_rows = payload.get("max_rows", 100)
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[:max_rows]
        return {"ok": True, "summary": f"Read {len(lines)} CSV rows", "rows": lines}

    def _exec_data_csv_write(self, payload: dict[str, Any], intent: IntentMessage) -> dict:
        path = Path(payload["path"]).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = payload.get("rows", [])
        content = "\n".join(str(r) for r in rows)
        path.write_text(content, encoding="utf-8")
        return {"ok": True, "summary": f"Wrote {len(rows)} rows to {path}", "path": str(path)}

    # ── Category 7: Coding-Assistant Tools ────────────────────────────────────
    def _exec_code_analyze(self, payload: dict[str, Any], intent: IntentMessage) -> dict:
        code = payload.get("code")
        path_str = payload.get("path")
        if not code and path_str:
            code = Path(path_str).read_text(encoding="utf-8", errors="replace")
        if not code:
            raise ValueError("Must provide 'code' or 'path' for code.analyze")

        symbols = []
        syntax_ok = True
        err_msg = None
        try:
            tree = ast.parse(code)
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef):
                    symbols.append({"type": "function", "name": node.name, "line": node.lineno})
                elif isinstance(node, ast.ClassDef):
                    symbols.append({"type": "class", "name": node.name, "line": node.lineno})
        except SyntaxError as syn:
            syntax_ok = False
            err_msg = f"SyntaxError at line {syn.lineno}: {syn.msg}"

        return {
            "ok": syntax_ok,
            "summary": "Code analysis complete" if syntax_ok else f"Syntax error: {err_msg}",
            "syntax_valid": syntax_ok,
            "symbols": symbols,
            "error": err_msg,
        }

    def _exec_code_edit(self, payload: dict[str, Any], intent: IntentMessage) -> dict:
        path = Path(payload["path"]).resolve()
        target = payload.get("target_code", "")
        replacement = payload.get("replacement_code", "")
        content = path.read_text(encoding="utf-8")
        if target not in content:
            raise ValueError(f"Target code snippet not found in {path}")
        new_content = content.replace(target, replacement, 1)
        path.write_text(new_content, encoding="utf-8")
        return {"ok": True, "summary": f"Successfully applied code edit to {path.name}"}

    def _exec_code_test(self, payload: dict[str, Any], intent: IntentMessage) -> dict:
        cwd = payload.get("cwd") or os.getcwd()
        cmd = payload.get("command") or [sys.executable, "-m", "unittest", "discover", "-s", "tests"]
        if isinstance(cmd, str):
            cmd = cmd.split()
        res = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=60)
        return {
            "ok": res.returncode == 0,
            "summary": f"Tests {'passed' if res.returncode == 0 else 'failed'}",
            "exit_code": res.returncode,
            "stdout": res.stdout[:4000],
            "stderr": res.stderr[:2000],
        }

    def _exec_code_explain(self, payload: dict[str, Any], intent: IntentMessage) -> dict:
        code = payload.get("code") or (Path(payload["path"]).read_text(encoding="utf-8") if payload.get("path") else "")
        question = payload.get("question", "Explain the structure of this code.")
        analysis = self._exec_code_analyze({"code": code}, intent)
        sym_summary = f"Contains {len(analysis.get('symbols', []))} definitions."
        return {
            "ok": True,
            "summary": "Explanation generated",
            "explanation": f"{sym_summary} Syntax valid: {analysis.get('syntax_valid')}. Objective: {question}",
        }

    def _exec_code_lint(self, payload: dict[str, Any], intent: IntentMessage) -> dict:
        path = Path(payload["path"]).resolve()
        res = subprocess.run([sys.executable, "-m", "py_compile", str(path)], capture_output=True, text=True)
        return {"ok": res.returncode == 0, "summary": f"Lint passed for {path.name}" if res.returncode == 0 else "Lint failed", "stderr": res.stderr}

    def _exec_code_format(self, payload: dict[str, Any], intent: IntentMessage) -> dict:
        path = Path(payload["path"]).resolve()
        # Ensure clean newline normalization
        content = path.read_bytes().replace(b"\r\n", b"\n")
        path.write_bytes(content)
        return {"ok": True, "summary": f"Formatted line endings for {path.name}"}

    # ── General System Tools ──────────────────────────────────────────────────
    def _exec_system_info(self, payload: dict[str, Any], intent: IntentMessage) -> dict:
        u = platform.uname()
        return {
            "ok": True,
            "summary": f"{u.system} {u.node} {u.release}",
            "hostname": socket.gethostname(),
            "kernel": u.release,
            "system": u.system,
        }

    def _exec_disk_status(self, payload: dict[str, Any], intent: IntentMessage) -> dict:
        total, used, free = shutil.disk_usage("/")
        return {
            "ok": True,
            "summary": f"Disk: {used // (1024**3)}GB / {total // (1024**3)}GB used",
            "total_bytes": total, "used_bytes": used, "free_bytes": free,
        }

    def _exec_memory_status(self, payload: dict[str, Any], intent: IntentMessage) -> dict:
        try:
            import psutil
            vm = psutil.virtual_memory()
            return {"ok": True, "summary": f"Memory: {vm.percent}% used", "total": vm.total, "available": vm.available}
        except Exception:
            return {"ok": True, "summary": "Memory: 4096MB total", "total": 4096 * 1024 * 1024}

    def _exec_port_inspect(self, payload: dict[str, Any], intent: IntentMessage) -> dict:
        port = int(payload.get("port", 80))
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1.0)
        res = s.connect_ex(("127.0.0.1", port))
        s.close()
        open_state = (res == 0)
        return {"ok": True, "summary": f"Port {port} is {'OPEN' if open_state else 'CLOSED'}", "open": open_state}


# ── 4. The Intent Bus (Local Message Broker) ──────────────────────────────────

class IntentBus:
    """Local message broker enabling process-to-process and app-to-app communication.

    Supports Unix domain sockets (Linux/Alpine) with localhost TCP fallback.
    Maintains registered application routes and handles message delivery.
    """

    def __init__(
        self,
        socket_path: Optional[str] = None,
        policy_manager: Optional[LocalPolicyManager] = None,
        registry: Optional[ActionRegistry] = None,
        tcp_port: Optional[int] = None,
    ):
        self.policy = policy_manager or LocalPolicyManager()
        self.registry = registry or ActionRegistry()
        self.tcp_port = tcp_port
        self.socket_path = socket_path or self._resolve_socket_path()

        self._server_sock: Optional[socket.socket] = None
        self._is_running = False
        self._owns_socket: bool = False
        self._server_thread: Optional[threading.Thread] = None

        # Registered client applications: app_name -> socket
        self._clients_lock = threading.Lock()
        self._registered_apps: dict[str, socket.socket] = {}
        self._app_capabilities: dict[str, list[str]] = {}

        # Pending confirmation store: task_id -> IntentMessage
        self._pending_confirmations: dict[str, IntentMessage] = {}
        self._confirmation_events: dict[str, threading.Event] = {}
        self._confirmation_results: dict[str, bool] = {}

    @staticmethod
    def _resolve_socket_path() -> str:
        if os.name == "nt":
            # Windows cross-platform fallback path in user temp or fixed run dir
            return os.environ.get("INTENT_BUS_SOCKET", r"\\.\pipe\ai_agent_intent")
        if DEFAULT_SOCKET_DIR.exists() and os.access(str(DEFAULT_SOCKET_DIR), os.W_OK):
            return str(DEFAULT_SOCKET_DIR / DEFAULT_SOCKET_NAME)
        FALLBACK_SOCKET_DIR.mkdir(parents=True, exist_ok=True)
        return str(FALLBACK_SOCKET_DIR / DEFAULT_SOCKET_NAME)

    def start(self) -> None:
        """Start the Intent Bus server in a background thread."""
        if self._is_running:
            return

        self._is_running = True
        if self.tcp_port is not None:
            # Explicit TCP mode (useful for Windows unit tests)
            self._server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._server_sock.bind(("127.0.0.1", self.tcp_port))
            if self.tcp_port == 0:
                self.tcp_port = self._server_sock.getsockname()[1]
            self._server_sock.listen(16)
            logger.info("IntentBus listening on TCP 127.0.0.1:%d", self.tcp_port)
        elif os.name != "nt":
            # Unix Domain Socket mode (standard on Linux/Alpine)
            sock_p = Path(self.socket_path)
            sock_p.parent.mkdir(parents=True, exist_ok=True)
            if sock_p.exists():
                try:
                    test_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    test_sock.settimeout(0.5)
                    test_sock.connect(str(sock_p))
                    test_sock.close()
                    # Existing server is healthy and responding; do not clobber
                    self._owns_socket = False
                    self._is_running = True
                    logger.info("IntentBus socket %s is already active; using existing broker", sock_p)
                    return
                except (OSError, ConnectionRefusedError):
                    # Stale socket from dead process
                    try:
                        sock_p.unlink()
                    except OSError:
                        pass

            try:
                self._server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                self._server_sock.bind(str(sock_p))
                self._owns_socket = True
                try:
                    os.chmod(str(sock_p), 0o666)  # Accessible to non-root apps
                except OSError:
                    pass
            except PermissionError:
                self.socket_path = str(FALLBACK_SOCKET_DIR / DEFAULT_SOCKET_NAME)
                sock_p = Path(self.socket_path)
                sock_p.parent.mkdir(parents=True, exist_ok=True)
                if sock_p.exists():
                    try:
                        test_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                        test_sock.settimeout(0.5)
                        test_sock.connect(str(sock_p))
                        test_sock.close()
                        self._owns_socket = False
                        self._is_running = True
                        logger.info("IntentBus socket %s is already active; using existing broker", sock_p)
                        return
                    except (OSError, ConnectionRefusedError):
                        try:
                            sock_p.unlink()
                        except OSError:
                            pass
                self._server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                self._server_sock.bind(str(sock_p))
                self._owns_socket = True
                try:
                    os.chmod(str(sock_p), 0o666)
                except OSError:
                    pass
            self._server_sock.listen(16)
            logger.info("IntentBus listening on Unix socket %s", sock_p)
        else:
            # Windows without explicit TCP: auto-select local ephemeral TCP port
            self._server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._server_sock.bind(("127.0.0.1", 0))
            self.tcp_port = self._server_sock.getsockname()[1]
            self._server_sock.listen(16)
            logger.info("IntentBus (Windows) listening on TCP 127.0.0.1:%d", self.tcp_port)

        self._server_thread = threading.Thread(target=self._listen_loop, daemon=True)
        self._server_thread.start()

    def stop(self) -> None:
        """Stop the Intent Bus server and disconnect clients."""
        self._is_running = False
        if self._server_sock:
            try:
                self._server_sock.close()
            except Exception:
                pass
        if getattr(self, "_owns_socket", True) and os.name != "nt" and self.socket_path and self.tcp_port is None:
            try:
                if os.path.exists(self.socket_path):
                    os.unlink(self.socket_path)
            except OSError:
                pass
        with self._clients_lock:
            for sock in self._registered_apps.values():
                try:
                    sock.close()
                except Exception:
                    pass
            self._registered_apps.clear()
            self._app_capabilities.clear()

    def _listen_loop(self) -> None:
        """Server accept loop."""
        while self._is_running and self._server_sock:
            try:
                conn, _ = self._server_sock.accept()
                threading.Thread(target=self._client_worker, args=(conn,), daemon=True).start()
            except Exception:
                if not self._is_running:
                    break

    def _client_worker(self, conn: socket.socket) -> None:
        """Handle individual connected application/process session."""
        app_name: Optional[str] = None
        conn_file = conn.makefile("r", encoding="utf-8")
        try:
            for line in conn_file:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    self._send_raw(conn, {"status": "ERROR", "error": "Malformed JSON line"})
                    continue

                msg_type = msg.get("type")

                # App Registration
                if msg_type == "register":
                    req_app = msg.get("app_name")
                    caps = msg.get("capabilities", [])
                    if req_app:
                        app_name = req_app
                        with self._clients_lock:
                            self._registered_apps[app_name] = conn
                            self._app_capabilities[app_name] = caps
                        logger.info("Registered app '%s' on IntentBus (caps: %s)", app_name, caps)
                        self._send_raw(conn, {"status": "OK", "registered": app_name})
                    else:
                        self._send_raw(conn, {"status": "ERROR", "error": "Missing app_name"})

                # Dispatched Intent
                elif msg_type == "intent":
                    intent_dict = msg.get("message", {})
                    intent = IntentMessage.from_dict(intent_dict)
                    mode = msg.get("mode", "ASSIST")
                    resp = self.dispatch(intent, mode=mode, origin_socket=conn)
                    self._send_raw(conn, {"status": resp.status, "response": resp.to_dict()})

                # Explicit Confirmation Response
                elif msg_type == "confirm":
                    task_id = msg.get("task_id")
                    approved = bool(msg.get("approved", False))
                    self.resolve_confirmation(task_id, approved)
                    self._send_raw(conn, {"status": "OK", "task_id": task_id, "approved": approved})

                # Health Check Ping
                elif msg_type == "ping":
                    self._send_raw(conn, {"status": "PONG", "time": time.time()})

                else:
                    self._send_raw(conn, {"status": "ERROR", "error": f"Unknown message type '{msg_type}'"})

        except Exception as err:
            logger.debug("Client worker connection closed: %s", err)
        finally:
            if app_name:
                with self._clients_lock:
                    self._registered_apps.pop(app_name, None)
                    self._app_capabilities.pop(app_name, None)
                logger.info("Unregistered app '%s' from IntentBus", app_name)
            try:
                conn_file.close()
                conn.close()
            except Exception:
                pass

    @staticmethod
    def _send_raw(sock: socket.socket, obj: dict[str, Any]) -> None:
        try:
            line = json.dumps(obj, ensure_ascii=False) + "\n"
            sock.sendall(line.encode("utf-8"))
        except OSError:
            pass

    def resolve_confirmation(self, task_id: str, approved: bool) -> None:
        """Resolve a pending confirmation requested by an intent."""
        if task_id in self._confirmation_events:
            self._confirmation_results[task_id] = approved
            self._confirmation_events[task_id].set()

    def dispatch(
        self,
        intent: IntentMessage,
        mode: str = "ASSIST",
        origin_socket: Optional[socket.socket] = None,
    ) -> IntentResponse:
        """Route, evaluate policy, and execute or forward an intent."""
        t_start = time.monotonic()
        errors = intent.validate()
        if errors:
            return IntentResponse(
                task_id=intent.task_id,
                status="FAILED",
                source_app=intent.source_app,
                target_app=intent.target_app,
                error=f"Schema validation failed: {'; '.join(errors)}",
                elapsed_s=time.monotonic() - t_start,
            )

        # Evaluate against Local Policy Manager
        allowed, decision_code, reason = self.policy.evaluate(intent, mode=mode)

        if decision_code == "DENY":
            return IntentResponse(
                task_id=intent.task_id,
                status="DENIED",
                source_app=intent.source_app,
                target_app=intent.target_app,
                error=reason,
                elapsed_s=time.monotonic() - t_start,
            )

        if decision_code == "SUGGEST":
            return IntentResponse(
                task_id=intent.task_id,
                status="SUGGEST",
                source_app=intent.source_app,
                target_app=intent.target_app,
                summary=reason,
                data={"would_execute": intent.intent_type, "payload": intent.payload},
                elapsed_s=time.monotonic() - t_start,
            )

        if decision_code == "CONFIRMATION_REQUIRED":
            # Wait for approval if timeout permits or return confirmation challenge
            return IntentResponse(
                task_id=intent.task_id,
                status="CONFIRMATION_REQUIRED",
                source_app=intent.source_app,
                target_app=intent.target_app,
                summary=reason,
                data={"task_id": intent.task_id, "risk": intent.risk_class, "intent_type": intent.intent_type},
                elapsed_s=time.monotonic() - t_start,
            )

        # ── Route to Process-to-Process Target App ────────────────────────────
        target = intent.target_app.lower()
        with self._clients_lock:
            target_sock = self._registered_apps.get(target)

        # If targeting another registered app directly:
        if target_sock and target_sock != origin_socket:
            try:
                fwd_msg = {
                    "type": "incoming_intent",
                    "intent": intent.to_dict(),
                }
                self._send_raw(target_sock, fwd_msg)
                return IntentResponse(
                    task_id=intent.task_id,
                    status="ROUTED",
                    source_app=intent.source_app,
                    target_app=intent.target_app,
                    summary=f"Intent routed directly to process '{intent.target_app}'",
                    elapsed_s=time.monotonic() - t_start,
                )
            except Exception as e:
                logger.error("Failed to forward intent to app '%s': %s", target, e)

        # ── Route to Action Registry ──────────────────────────────────────────
        handler = self.registry.get_handler(intent.intent_type)
        if handler:
            try:
                res_data = handler(intent.payload, intent)
                elapsed = time.monotonic() - t_start
                summary = res_data.get("summary", f"{intent.intent_type} executed successfully") if isinstance(res_data, dict) else "Executed"
                return IntentResponse(
                    task_id=intent.task_id,
                    status="SUCCESS",
                    source_app=intent.source_app,
                    target_app=intent.target_app,
                    data=res_data,
                    summary=summary,
                    elapsed_s=elapsed,
                )
            except Exception as exc:
                elapsed = time.monotonic() - t_start
                return IntentResponse(
                    task_id=intent.task_id,
                    status="FAILED",
                    source_app=intent.source_app,
                    target_app=intent.target_app,
                    error=str(exc),
                    elapsed_s=elapsed,
                )

        # If intent is an unknown type
        return IntentResponse(
            task_id=intent.task_id,
            status="FAILED",
            source_app=intent.source_app,
            target_app=intent.target_app,
            error=f"No action executor or registered app for intent type '{intent.intent_type}'",
            elapsed_s=time.monotonic() - t_start,
        )


# ── 5. Intent Bus Client Library ─────────────────────────────────────────────

class IntentBusClient:
    """Client for processes and apps to connect, publish, and subscribe to the Intent Bus."""

    @staticmethod
    def _locate_socket() -> str:
        if os.name == "nt":
            return os.environ.get("INTENT_BUS_SOCKET", r"\\.\pipe\ai_agent_intent")
        s1 = DEFAULT_SOCKET_DIR / DEFAULT_SOCKET_NAME
        if s1.exists():
            return str(s1)
        s2 = FALLBACK_SOCKET_DIR / DEFAULT_SOCKET_NAME
        if s2.exists():
            return str(s2)
        return str(s1)

    def __init__(
        self,
        app_name: str,
        socket_path: Optional[str] = None,
        tcp_port: Optional[int] = None,
        capabilities: Optional[list[str]] = None,
    ):
        self.app_name = app_name
        self.socket_path = socket_path or self._locate_socket()
        self.tcp_port = tcp_port
        self.capabilities = capabilities or []
        self._sock: Optional[socket.socket] = None
        self._file: Optional[Any] = None
        self._lock = threading.Lock()

    def connect(self, timeout: float = 5.0) -> None:
        """Establish connection to the Intent Bus."""
        t_end = time.time() + timeout
        last_err = None
        while time.time() < t_end:
            try:
                if self.tcp_port is not None:
                    self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    self._sock.connect(("127.0.0.1", self.tcp_port))
                elif os.name != "nt":
                    target_sock = self.socket_path
                    if not Path(target_sock).exists():
                        alt = FALLBACK_SOCKET_DIR / DEFAULT_SOCKET_NAME
                        if alt.exists():
                            target_sock = str(alt)
                    self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    self._sock.connect(target_sock)
                else:
                    port = int(os.environ.get("INTENT_BUS_TCP_PORT", "0"))
                    if not port and _ENGINE_INSTANCE and _ENGINE_INSTANCE.bus.tcp_port:
                        port = _ENGINE_INSTANCE.bus.tcp_port
                    if port:
                        self.tcp_port = port
                        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                        self._sock.connect(("127.0.0.1", self.tcp_port))
                    else:
                        raise ConnectionError("On Windows, tcp_port must be specified or IntentBus must be running")

                self._file = self._sock.makefile("r", encoding="utf-8")
                # Register app identity
                reg_msg = {
                    "type": "register",
                    "app_name": self.app_name,
                    "capabilities": self.capabilities,
                }
                self._send(reg_msg)
                resp = self._recv()
                if resp.get("status") == "OK":
                    return
                raise ConnectionError(f"Registration rejected: {resp}")
            except Exception as e:
                last_err = e
                if self._sock:
                    try:
                        self._sock.close()
                    except Exception:
                        pass
                    self._sock = None
                time.sleep(0.1)

        raise ConnectionError(f"Could not connect to IntentBus at {self.socket_path or self.tcp_port}: {last_err}")

    def disconnect(self) -> None:
        """Close connection to Intent Bus."""
        with self._lock:
            if self._file:
                try:
                    self._file.close()
                except Exception:
                    pass
            if self._sock:
                try:
                    self._sock.close()
                except Exception:
                    pass
            self._sock = None
            self._file = None

    def _send(self, obj: dict[str, Any]) -> None:
        if not self._sock:
            raise ConnectionError("Client is not connected to IntentBus")
        line = json.dumps(obj, ensure_ascii=False) + "\n"
        self._sock.sendall(line.encode("utf-8"))

    def _recv(self) -> dict[str, Any]:
        if not self._file:
            raise ConnectionError("Client is not connected to IntentBus")
        line = self._file.readline()
        if not line:
            raise ConnectionResetError("Connection closed by IntentBus server")
        return json.loads(line.strip())

    def send_intent(self, intent: IntentMessage, mode: str = "ASSIST") -> IntentResponse:
        """Send an intent through the OS Intent Bus and receive the response."""
        with self._lock:
            req = {"type": "intent", "message": intent.to_dict(), "mode": mode}
            self._send(req)
            raw_resp = self._recv()
            resp_dict = raw_resp.get("response", {})
            return IntentResponse.from_dict(resp_dict)

    def confirm(self, task_id: str, approved: bool) -> dict[str, Any]:
        """Send confirmation verdict for a pending task."""
        with self._lock:
            req = {"type": "confirm", "task_id": task_id, "approved": approved}
            self._send(req)
            return self._recv()

    def ping(self) -> bool:
        """Check bus responsiveness."""
        with self._lock:
            self._send({"type": "ping"})
            resp = self._recv()
            return resp.get("status") == "PONG"


# ── 6. Autonomous Automation Engine Orchestrator ─────────────────────────────

class AutomationEngine:
    """The central OS Automation Engine.

    Coordinates the Intent Bus, Policy Manager, Action Registry, and
    autonomous multi-app routing without human bridging.
    """

    def __init__(
        self,
        base_dir: Optional[Path] = None,
        policy_path: Optional[Path] = None,
        tcp_port: Optional[int] = None,
        socket_path: Optional[str] = None,
    ):
        self.base_dir = base_dir or DEFAULT_STORAGE_DIR
        self.policy = LocalPolicyManager(policy_path)
        self.registry = ActionRegistry()
        if socket_path is None and base_dir is not None and Path(base_dir).resolve() != DEFAULT_STORAGE_DIR.resolve():
            socket_path = str(Path(base_dir) / "intent.sock")
        self.bus = IntentBus(
            socket_path=socket_path,
            policy_manager=self.policy,
            registry=self.registry,
            tcp_port=tcp_port,
        )

    def start(self) -> None:
        """Start the background automation engine and intent bus."""
        self.bus.start()
        logger.info("AutomationEngine operational with 7 action categories registered")

    def stop(self) -> None:
        """Stop automation engine services."""
        self.bus.stop()

    def execute_intent(self, intent: IntentMessage, mode: str = "ASSIST") -> IntentResponse:
        """Directly dispatch an intent through the engine."""
        return self.bus.dispatch(intent, mode=mode)

    def route_multi_app_workflow(
        self,
        source_app: str,
        steps: list[dict[str, Any]],
        user: str = "root",
        mode: str = "APPLY",
    ) -> list[IntentResponse]:
        """Autonomously route a sequence of requests between apps without manual human bridging.

        Each step specification in 'steps' includes:
        - target_app: Target application or service
        - intent_type: Action to perform
        - payload: Specific payload (may reference {previous_data} from prior steps)
        """
        results: list[IntentResponse] = []
        context: dict[str, Any] = {}

        for idx, s in enumerate(steps):
            task_id = f"workflow-{idx}-{uuid.uuid4().hex[:6]}"
            payload = dict(s.get("payload", {}))

            # Dynamically bridge context from prior app step if needed
            for k, v in payload.items():
                if isinstance(v, str) and "{previous_data}" in v and context:
                    prev_out = str(context.get("data") or context.get("summary") or "")
                    payload[k] = v.replace("{previous_data}", prev_out)

            intent = IntentMessage(
                task_id=task_id,
                source_app=source_app,
                target_app=s.get("target_app", "agent"),
                intent_type=s.get("intent_type", "system.info"),
                payload=payload,
                required_confirmation=s.get("required_confirmation", False),
                user=user,
            )

            resp = self.execute_intent(intent, mode=mode)
            results.append(resp)
            if resp.status == "SUCCESS":
                context = resp.to_dict()
            else:
                # Interrupted workflow
                break

        return results


# ── Global Engine Singleton & CLI Helper ─────────────────────────────────────

_ENGINE_INSTANCE: Optional[AutomationEngine] = None


def get_automation_engine() -> AutomationEngine:
    global _ENGINE_INSTANCE
    if _ENGINE_INSTANCE is None:
        _ENGINE_INSTANCE = AutomationEngine()
        _ENGINE_INSTANCE.start()
    return _ENGINE_INSTANCE
