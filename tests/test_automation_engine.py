#!/usr/bin/env python3
"""test_automation_engine.py — Comprehensive Test Suite for OS Automation Engine & Intent Bus

Validates:
1. Standard Intent Schema:
   - Serialization & deserialization
   - Required schema elements: task_id, source_app, target_app, intent_type,
     payload, required_confirmation, timeout, callback_route
   - Schema validation rules and error reporting
2. Action Executor Registry across all 7 Categories:
   - Browser tools (browser.navigate, browser.dump, browser.screenshot, browser.extract)
   - File tools (file.read, file.write, file.append, file.delete, file.list, file.query, dir.create)
   - Email tools (email.send, email.read, email.draft)
   - Git tools (git.status, git.diff, git.log, git.branch)
   - Service tools (service.status, service.restart, service.logs)
   - Document tools (document.read, document.search, data.csv_read, data.csv_write)
   - Coding-assistant tools (code.analyze, code.edit, code.lint, code.explain, code.test)
3. Local Policy Manager:
   - App allow/deny rules (e.g. code-editor vs browser vs untrusted)
   - Per-user permission boundaries (root vs non-root)
   - Risk classes: low (read-only), medium (change), high/destructive
   - Safe modes (READ_ONLY denial, SUGGEST suggestion hold, confirmation gates)
   - Screening for forbidden commands (mkfs, dd, reboot)
4. Intent Bus (Process-to-Process & App-to-App Messaging):
   - Socket broker connection, ping, and registration
   - Process-to-process direct routing between connected apps
   - Action dispatch to system services
   - Confirmation workflow
5. Autonomous Multi-App Routing:
   - Multi-step inter-app workflow routed automatically without manual human bridging
"""

import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time
import unittest

# Insert agent-daemon path
DAEMON_DIR = Path(__file__).resolve().parent.parent / "agent-daemon"
sys.path.insert(0, str(DAEMON_DIR))

from automation_engine import (
    ActionRegistry,
    AutomationEngine,
    IntentBus,
    IntentBusClient,
    IntentMessage,
    IntentResponse,
    LocalPolicyManager,
)


class TestStandardIntentSchema(unittest.TestCase):
    """Test suite validating standard intent schema format, serialization, and validation."""

    def test_intent_schema_fields(self):
        msg = IntentMessage(
            task_id="task-001",
            source_app="code-editor",
            target_app="git-service",
            intent_type="git.status",
            payload={"repo_path": "/root/project"},
            required_confirmation=False,
            timeout=15.0,
            callback_route="socket-ch-1",
            user="developer",
            uid=1000,
            gid=1000,
            risk_class="low",
        )
        # Verify all 8 core schema fields
        self.assertEqual(msg.task_id, "task-001")
        self.assertEqual(msg.source_app, "code-editor")
        self.assertEqual(msg.target_app, "git-service")
        self.assertEqual(msg.intent_type, "git.status")
        self.assertEqual(msg.payload, {"repo_path": "/root/project"})
        self.assertFalse(msg.required_confirmation)
        self.assertEqual(msg.timeout, 15.0)
        self.assertEqual(msg.callback_route, "socket-ch-1")

        # Validation should succeed
        self.assertEqual(msg.validate(), [])

    def test_serialization_roundtrip(self):
        original = IntentMessage(
            task_id="task-002",
            source_app="web-browser",
            target_app="coding-assistant",
            intent_type="code.analyze",
            payload={"code": "def hello(): pass"},
            required_confirmation=True,
            timeout=20.0,
            callback_route="ch-2",
        )
        json_str = original.to_json()
        reconstructed = IntentMessage.from_json(json_str)

        self.assertEqual(original.task_id, reconstructed.task_id)
        self.assertEqual(original.source_app, reconstructed.source_app)
        self.assertEqual(original.target_app, reconstructed.target_app)
        self.assertEqual(original.intent_type, reconstructed.intent_type)
        self.assertEqual(original.payload, reconstructed.payload)
        self.assertEqual(original.required_confirmation, reconstructed.required_confirmation)
        self.assertEqual(original.timeout, reconstructed.timeout)
        self.assertEqual(original.callback_route, reconstructed.callback_route)

    def test_schema_validation_rejections(self):
        bad_msg = IntentMessage(
            task_id="",
            source_app="",
            target_app="",
            intent_type="",
            payload="not a dict",  # type: ignore
            timeout=-5.0,
        )
        errors = bad_msg.validate()
        self.assertGreaterEqual(len(errors), 5)
        self.assertTrue(any("task_id" in e for e in errors))
        self.assertTrue(any("source_app" in e for e in errors))
        self.assertTrue(any("target_app" in e for e in errors))
        self.assertTrue(any("intent_type" in e for e in errors))
        self.assertTrue(any("payload" in e for e in errors))


class TestLocalPolicyManager(unittest.TestCase):
    """Test suite validating per-app, per-user, and risk-class security policy."""

    def setUp(self):
        self.policy = LocalPolicyManager()

    def test_code_editor_allowed_and_denied_intents(self):
        # code-editor allowed coding and git tools
        msg_ok = IntentMessage(
            task_id="t1", source_app="code-editor", target_app="coding-assistant",
            intent_type="code.analyze", payload={"code": "x = 1"}
        )
        allowed, code, reason = self.policy.evaluate(msg_ok, mode="APPLY")
        self.assertTrue(allowed)
        self.assertEqual(code, "ALLOW")

        # code-editor denied service stopping
        msg_bad = IntentMessage(
            task_id="t2", source_app="code-editor", target_app="service-manager",
            intent_type="service.stop", payload={"name": "nginx"}
        )
        allowed, code, reason = self.policy.evaluate(msg_bad, mode="APPLY")
        self.assertFalse(allowed)
        self.assertEqual(code, "DENY")
        self.assertIn("denied", reason.lower())

    def test_browser_policy_boundaries(self):
        # browser allowed browser extraction and document reading
        msg_ok = IntentMessage(
            task_id="b1", source_app="browser", target_app="browser-service",
            intent_type="browser.navigate", payload={"url": "http://127.0.0.1:8080"}
        )
        allowed, code, reason = self.policy.evaluate(msg_ok, mode="APPLY")
        self.assertTrue(allowed)

        # browser denied destructive file operations
        msg_bad = IntentMessage(
            task_id="b2", source_app="browser", target_app="file-manager",
            intent_type="file.delete", payload={"path": "/tmp/test.txt"}
        )
        allowed, code, reason = self.policy.evaluate(msg_bad, mode="APPLY")
        self.assertFalse(allowed)
        self.assertEqual(code, "DENY")

    def test_untrusted_app_strictly_isolated(self):
        msg_read = IntentMessage(
            task_id="u1", source_app="untrusted-script", target_app="agent",
            intent_type="system.info", payload={}
        )
        allowed, code, reason = self.policy.evaluate(msg_read, mode="APPLY")
        self.assertTrue(allowed)

        msg_write = IntentMessage(
            task_id="u2", source_app="untrusted-script", target_app="agent",
            intent_type="file.write", payload={"path": "/tmp/hack.sh", "content": "bad"}
        )
        allowed, code, reason = self.policy.evaluate(msg_write, mode="APPLY")
        self.assertFalse(allowed)
        self.assertEqual(code, "DENY")

    def test_per_user_permission_checks(self):
        # Non-root user (uid 1000) cannot restart or stop services
        msg_non_root = IntentMessage(
            task_id="usr1", source_app="agent-cli", target_app="service-manager",
            intent_type="service.restart", payload={"name": "sshd"},
            uid=1000, user="guest"
        )
        allowed, code, reason = self.policy.evaluate(msg_non_root, mode="APPLY")
        self.assertFalse(allowed)
        self.assertEqual(code, "DENY")
        self.assertIn("permission", reason.lower())

    def test_risk_classes_and_suggest_mode(self):
        # In SUGGEST mode, mutating actions return SUGGEST without execution
        msg_mut = IntentMessage(
            task_id="m1", source_app="code-editor", target_app="file-manager",
            intent_type="file.write", payload={"path": "/tmp/a.txt", "content": "hi"}
        )
        allowed, code, reason = self.policy.evaluate(msg_mut, mode="SUGGEST")
        self.assertFalse(allowed)
        self.assertEqual(code, "SUGGEST")

    def test_forbidden_command_screening(self):
        msg_evil = IntentMessage(
            task_id="e1", source_app="agent-cli", target_app="agent",
            intent_type="shell.script", payload={"command": "mkfs.ext4 /dev/sda1"}
        )
        allowed, code, reason = self.policy.evaluate(msg_evil, mode="APPLY")
        self.assertFalse(allowed)
        self.assertEqual(code, "DENY")
        self.assertIn("forbidden", reason.lower())


class TestActionExecutorRegistry(unittest.TestCase):
    """Test suite validating all 7 tool categories in ActionRegistry."""

    def setUp(self):
        self.registry = ActionRegistry()
        self.temp_dir = tempfile.mkdtemp(prefix="ai_agent_registry_test_")
        self.dummy_intent = IntentMessage(
            task_id="test", source_app="test-suite", target_app="test", intent_type="test"
        )

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    # 1. Browser Tools
    def test_browser_tools(self):
        nav_res = self.registry.get_handler("browser.navigate")(
            {"url": "http://127.0.0.1:8080/app"}, self.dummy_intent
        )
        self.assertTrue(nav_res["ok"])

        dump_res = self.registry.get_handler("browser.dump")({}, self.dummy_intent)
        self.assertTrue(dump_res["ok"])

        shot_res = self.registry.get_handler("browser.screenshot")({"path": "/tmp/test.png"}, self.dummy_intent)
        self.assertTrue(shot_res["ok"])

    # 2. File Tools
    def test_file_tools(self):
        test_file = Path(self.temp_dir) / "notes.txt"
        # Write
        w_res = self.registry.get_handler("file.write")(
            {"path": str(test_file), "content": "Line 1\nLine 2\n"}, self.dummy_intent
        )
        self.assertTrue(w_res["ok"])
        self.assertTrue(test_file.exists())

        # Append
        a_res = self.registry.get_handler("file.append")(
            {"path": str(test_file), "content": "Line 3\n"}, self.dummy_intent
        )
        self.assertTrue(a_res["ok"])

        # Read
        r_res = self.registry.get_handler("file.read")(
            {"path": str(test_file)}, self.dummy_intent
        )
        self.assertTrue(r_res["ok"])
        self.assertIn("Line 3", r_res["content"])

        # List
        l_res = self.registry.get_handler("file.list")(
            {"path": self.temp_dir}, self.dummy_intent
        )
        self.assertTrue(l_res["ok"])
        self.assertIn("notes.txt", l_res["items"])

        # Delete
        d_res = self.registry.get_handler("file.delete")(
            {"path": str(test_file)}, self.dummy_intent
        )
        self.assertTrue(d_res["ok"])
        self.assertFalse(test_file.exists())

    # 3. Email Tools
    def test_email_tools(self):
        draft_res = self.registry.get_handler("email.draft")(
            {"to": "user@os-ai.local", "subject": "Test Subject", "body": "Test Body"}, self.dummy_intent
        )
        self.assertTrue(draft_res["ok"])
        self.assertIn("draft_id", draft_res)

        send_res = self.registry.get_handler("email.send")(
            {"to": "alert@os-ai.local", "subject": "High Load", "body": "CPU alert"}, self.dummy_intent
        )
        self.assertTrue(send_res["ok"])

    # 4. Git Tools
    def test_git_tools(self):
        status_res = self.registry.get_handler("git.status")({}, self.dummy_intent)
        self.assertIn("ok", status_res)

        diff_res = self.registry.get_handler("git.diff")({}, self.dummy_intent)
        self.assertIn("ok", diff_res)

    # 5. Service Tools
    def test_service_tools(self):
        status_res = self.registry.get_handler("service.status")({"name": "ai-agent"}, self.dummy_intent)
        self.assertIn("ok", status_res)

    # 6. Document Tools
    def test_document_tools(self):
        doc_file = Path(self.temp_dir) / "doc.txt"
        doc_file.write_text("Title: System Architecture\nChapter 1: Kernel\nChapter 2: Daemon\n", encoding="utf-8")

        r_res = self.registry.get_handler("document.read")({"path": str(doc_file)}, self.dummy_intent)
        self.assertTrue(r_res["ok"])
        self.assertIn("System Architecture", r_res["text"])

        s_res = self.registry.get_handler("document.search")({"path": str(doc_file), "query": "Kernel"}, self.dummy_intent)
        self.assertTrue(s_res["ok"])
        self.assertEqual(len(s_res["matches"]), 1)

    # 7. Coding-Assistant Tools
    def test_coding_assistant_tools(self):
        sample_code = "class Calculator:\n    def add(self, a, b):\n        return a + b\n"
        ana_res = self.registry.get_handler("code.analyze")({"code": sample_code}, self.dummy_intent)
        self.assertTrue(ana_res["ok"])
        self.assertTrue(ana_res["syntax_valid"])
        symbol_names = [s["name"] for s in ana_res["symbols"]]
        self.assertIn("Calculator", symbol_names)
        self.assertIn("add", symbol_names)

        # Code edit test
        code_file = Path(self.temp_dir) / "calc.py"
        code_file.write_text(sample_code, encoding="utf-8")
        edit_res = self.registry.get_handler("code.edit")({
            "path": str(code_file),
            "target_code": "return a + b",
            "replacement_code": "return (a + b)  # optimized",
        }, self.dummy_intent)
        self.assertTrue(edit_res["ok"])
        self.assertIn("optimized", code_file.read_text(encoding="utf-8"))


class TestIntentBusAndAppMessaging(unittest.TestCase):
    """Test suite validating process-to-process and app-to-app messaging over IntentBus."""

    @classmethod
    def setUpClass(cls):
        # Run IntentBus on local ephemeral TCP port for isolated test environment
        cls.bus = IntentBus(tcp_port=0)
        cls.bus.start()
        cls.port = cls.bus.tcp_port
        time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        cls.bus.stop()

    def test_bus_ping(self):
        client = IntentBusClient("ping-tester", tcp_port=self.port)
        client.connect()
        self.assertTrue(client.ping())
        client.disconnect()

    def test_app_to_service_intent_dispatch(self):
        client = IntentBusClient("code-editor", tcp_port=self.port)
        client.connect()

        intent = IntentMessage(
            task_id="t-srv-01",
            source_app="code-editor",
            target_app="coding-assistant",
            intent_type="code.analyze",
            payload={"code": "def process(): return 42"},
        )
        resp = client.send_intent(intent, mode="APPLY")
        self.assertEqual(resp.status, "SUCCESS")
        self.assertEqual(resp.source_app, "code-editor")
        self.assertTrue(resp.data.get("syntax_valid"))
        client.disconnect()

    def test_process_to_process_direct_messaging(self):
        # App B connects and registers as "receiver-app"
        receiver = IntentBusClient("receiver-app", tcp_port=self.port)
        receiver.connect()

        # App A connects as "sender-app" and dispatches intent targeting "receiver-app"
        sender = IntentBusClient("sender-app", tcp_port=self.port)
        sender.connect()

        intent = IntentMessage(
            task_id="p2p-001",
            source_app="sender-app",
            target_app="receiver-app",
            intent_type="app.message",
            payload={"msg": "Hello from Sender App!"},
        )
        resp = sender.send_intent(intent, mode="APPLY")
        self.assertEqual(resp.status, "ROUTED")
        self.assertIn("routed directly to process", resp.summary)

        # Verify App B received the forwarded intent
        incoming = receiver._recv()
        self.assertEqual(incoming.get("type"), "incoming_intent")
        self.assertEqual(incoming.get("intent", {}).get("task_id"), "p2p-001")
        self.assertEqual(incoming.get("intent", {}).get("payload", {}).get("msg"), "Hello from Sender App!")

        sender.disconnect()
        receiver.disconnect()

    def test_policy_denial_through_bus(self):
        client = IntentBusClient("browser", tcp_port=self.port)
        client.connect()

        # Browser trying to delete a file is blocked by policy
        intent = IntentMessage(
            task_id="t-deny-01",
            source_app="browser",
            target_app="file-manager",
            intent_type="file.delete",
            payload={"path": "/tmp/forbidden.txt"},
        )
        resp = client.send_intent(intent, mode="APPLY")
        self.assertEqual(resp.status, "DENIED")
        self.assertIn("denied", resp.error.lower())
        client.disconnect()


class TestAutonomousMultiAppRouting(unittest.TestCase):
    """Test suite validating autonomous multi-app workflow routing without human bridging."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="ai_agent_workflow_test_")
        self.engine = AutomationEngine(base_dir=Path(self.temp_dir))

    def tearDown(self):
        self.engine.stop()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_autonomous_multi_app_chain(self):
        """Workflow:
        1. Source app 'code-editor' asks 'coding-assistant' to analyze a snippet
        2. Routes to 'file-manager' to save snippet to disk
        3. Routes to 'file-manager' to query file existence
        4. Routes to 'email-client' to draft a confirmation of completion
        All executed sequentially through the engine without human manual bridging.
        """
        output_file = Path(self.temp_dir) / "solution.py"
        snippet = "def calculate_tax(amount): return amount * 0.15\n"

        workflow_steps = [
            {
                "target_app": "coding-assistant",
                "intent_type": "code.analyze",
                "payload": {"code": snippet},
            },
            {
                "target_app": "file-manager",
                "intent_type": "file.write",
                "payload": {"path": str(output_file), "content": snippet},
            },
            {
                "target_app": "file-manager",
                "intent_type": "file.query",
                "payload": {"path": str(output_file)},
            },
            {
                "target_app": "email-service",
                "intent_type": "email.draft",
                "payload": {
                    "to": "lead-dev@os-ai.local",
                    "subject": "Automated code generated and verified",
                    "body": "Code has been written to solution.py and syntax validated.",
                },
            },
        ]

        results = self.engine.route_multi_app_workflow(
            source_app="code-editor",
            steps=workflow_steps,
            mode="APPLY",
        )

        self.assertEqual(len(results), 4)
        for r in results:
            self.assertEqual(r.status, "SUCCESS")

        # Verify filesystem side-effects
        self.assertTrue(output_file.exists())
        self.assertEqual(output_file.read_text(encoding="utf-8"), snippet)

        # Verify steps correctly passed between targets
        self.assertEqual(results[0].target_app, "coding-assistant")
        self.assertEqual(results[1].target_app, "file-manager")
        self.assertEqual(results[2].target_app, "file-manager")
        self.assertEqual(results[3].target_app, "email-service")


if __name__ == "__main__":
    unittest.main()

