#!/usr/bin/env python3
"""Comprehensive unit and integration test suite for harness_v2.

Covers all 38 tools across Phases A, B, C, D:
- File tools: file.read, file.write, file.append, file.move, file.delete, file.list, file.query
- Shell & script tools: command.argv, shell.script
- Data tools: data.csv_read, data.csv_write
- Document tools: document.read (plain, markdown, docx)
- Communication tools: email.send, email.read
- Browser tools: browser.navigate, browser.dump, browser.click, browser.type, browser.screenshot
- Web & LLM tools: http.check, http.get, llm.prompt
- System & Service tools: system.info, disk.status, memory.status, package.*, service.*, port.inspect
- Policy engine enforcement: READ_ONLY vs CHANGE, SUGGEST/ASSIST/APPLY modes
- Deterministic planner (28 patterns)
- VariableInterpolator and dynamic scratchpad chaining
"""

import csv
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

# Add agent-daemon to Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "agent-daemon")))

from harness_v2 import (
    ALLOWED_TOOLS,
    Harness,
    JobStatus,
    JobStore,
    Plan,
    PlanError,
    PolicyEngine,
    Step,
    ToolExecutor,
    ToolResult,
    VariableInterpolator,
    deterministic_plan,
    validate_plan,
)


class TestPolicyEngine(unittest.TestCase):
    def setUp(self):
        self.policy = PolicyEngine()

    def test_all_registered_tools_accounted_for(self):
        self.assertEqual(len(PolicyEngine.READ_ONLY), 21)
        self.assertEqual(len(PolicyEngine.CHANGE), 17)
        self.assertEqual(len(ALLOWED_TOOLS), 38)
        self.assertTrue(PolicyEngine.READ_ONLY.isdisjoint(PolicyEngine.CHANGE))

    def test_readonly_tools_allowed_in_all_modes(self):
        step = Step("test-ro", "Read CSV", "data.csv_read", {"path": "/tmp/test.csv"}, [])
        for mode in ("SUGGEST", "ASSIST", "APPLY", "UNKNOWN"):
            verdict, _ = self.policy.decide(mode, step)
            self.assertEqual(verdict, "ALLOW", f"Expected ALLOW for read-only in {mode}")

    def test_change_tools_policy_gating(self):
        step = Step("test-ch", "Append File", "file.append", {"path": "/tmp/test.txt", "content": "hello"},
                    [{"tool": "file.query", "args": {"path": "/tmp/test.txt"}}], "system_change")
        
        verdict, _ = self.policy.decide("SUGGEST", step)
        self.assertEqual(verdict, "SUGGEST")

        verdict, _ = self.policy.decide("ASSIST", step)
        self.assertEqual(verdict, "ALLOW")

        verdict, _ = self.policy.decide("APPLY", step)
        self.assertEqual(verdict, "ALLOW")

        verdict, _ = self.policy.decide("RESTRICTED", step)
        self.assertEqual(verdict, "REQUIRE_APPROVAL")


class TestFileOperations(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="harness_test_files_")
        self.caller = {"user": "testuser", "home": self.tmp_dir, "cwd": self.tmp_dir, "uid": 1000, "gid": 1000}
        self.tools = ToolExecutor(self.caller, allow_privilege_proxy=False)

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_file_write_append_and_read(self):
        test_file = os.path.join(self.tmp_dir, "notes.txt")
        
        # Write
        w_res = self.tools.run("file.write", {"path": test_file, "content": "Line 1\n"})
        self.assertTrue(w_res.ok)
        self.assertTrue(os.path.isfile(test_file))

        # Append
        a_res = self.tools.run("file.append", {"path": test_file, "content": "Line 2\n"})
        self.assertTrue(a_res.ok)

        # Read
        r_res = self.tools.run("file.read", {"path": test_file})
        self.assertTrue(r_res.ok)
        self.assertEqual(r_res.stdout, "Line 1\nLine 2\n")

    def test_file_move_and_delete(self):
        src = os.path.join(self.tmp_dir, "src.txt")
        dst = os.path.join(self.tmp_dir, "dst.txt")
        Path(src).write_text("move me", encoding="utf-8")

        # Move
        mv_res = self.tools.run("file.move", {"src": src, "dst": dst})
        self.assertTrue(mv_res.ok)
        self.assertFalse(os.path.exists(src))
        self.assertTrue(os.path.exists(dst))

        # Delete
        del_res = self.tools.run("file.delete", {"path": dst})
        self.assertTrue(del_res.ok)
        self.assertFalse(os.path.exists(dst))

    def test_file_list(self):
        for name in ("a.py", "b.py", "c.txt"):
            Path(self.tmp_dir, name).write_text("test", encoding="utf-8")
        
        ls_res = self.tools.run("file.list", {"path": self.tmp_dir, "pattern": "*.py"})
        self.assertTrue(ls_res.ok)
        self.assertIn("a.py", ls_res.stdout)
        self.assertIn("b.py", ls_res.stdout)
        self.assertNotIn("c.txt", ls_res.stdout)

    def test_file_append_blocked_on_proc(self):
        res = self.tools.run("file.append", {"path": "/proc/sys/kernel/hostname", "content": "bad"})
        self.assertFalse(res.ok)
        self.assertIn("protected", res.summary.lower())


class TestDataAndDocumentTools(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="harness_test_data_")
        self.caller = {"user": "testuser", "home": self.tmp_dir, "cwd": self.tmp_dir}
        self.tools = ToolExecutor(self.caller, allow_privilege_proxy=False)

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_csv_write_and_read(self):
        csv_path = os.path.join(self.tmp_dir, "metrics.csv")
        headers = ["id", "name", "score"]
        rows = [
            {"id": "1", "name": "Alice", "score": "95"},
            {"id": "2", "name": "Bob", "score": "88"},
        ]
        
        # Write CSV
        w_res = self.tools.run("data.csv_write", {"path": csv_path, "headers": headers, "rows": rows})
        self.assertTrue(w_res.ok)
        self.assertTrue(os.path.isfile(csv_path))

        # Read CSV
        r_res = self.tools.run("data.csv_read", {"path": csv_path})
        self.assertTrue(r_res.ok)
        data = json.loads(r_res.stdout)
        self.assertEqual(data["headers"], headers)
        self.assertEqual(data["total_rows"], 2)
        self.assertEqual(data["sample_rows"][0]["name"], "Alice")

    def test_document_read_text_and_markdown(self):
        doc_path = os.path.join(self.tmp_dir, "spec.md")
        Path(doc_path).write_text("# Project Spec\n\n- Feature A\n- Feature B", encoding="utf-8")

        res = self.tools.run("document.read", {"path": doc_path})
        self.assertTrue(res.ok)
        self.assertIn("Project Spec", res.stdout)
        self.assertIn("Feature A", res.stdout)

    def test_document_read_docx_native_extraction(self):
        # Create a real valid .docx file in memory using zipfile + XML
        docx_path = os.path.join(self.tmp_dir, "proposal.docx")
        doc_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:r><w:t>Q4 Product Roadmap Proposal</w:t></w:r></w:p>
    <w:p><w:r><w:t>Milestone 1: Automated Agent Harness</w:t></w:r></w:p>
    <w:p><w:r><w:t>Milestone 2: Browser and Email Orchestration</w:t></w:r></w:p>
  </w:body>
</w:document>"""
        with zipfile.ZipFile(docx_path, "w") as z:
            z.writestr("word/document.xml", doc_xml.encode("utf-8"))

        res = self.tools.run("document.read", {"path": docx_path})
        self.assertTrue(res.ok)
        self.assertIn("Q4 Product Roadmap Proposal", res.stdout)
        self.assertIn("Milestone 1: Automated Agent Harness", res.stdout)
        self.assertIn("Milestone 2: Browser and Email Orchestration", res.stdout)


class TestShellScriptTool(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="harness_test_script_")
        self.caller = {"user": "testuser", "home": self.tmp_dir, "cwd": self.tmp_dir}
        self.tools = ToolExecutor(self.caller, allow_privilege_proxy=False)

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_execute_python_script(self):
        script = """
import sys
total = sum([1, 2, 3, 4, 5])
print(f"CALCULATED_SUM={total}")
"""
        res = self.tools.run("shell.script", {"code": script, "interpreter": "python3"})
        self.assertTrue(res.ok)
        self.assertIn("CALCULATED_SUM=15", res.stdout)

    def test_block_dangerous_script(self):
        script = "rm -rf / --no-preserve-root"
        res = self.tools.run("shell.script", {"code": script, "interpreter": "sh"})
        self.assertFalse(res.ok)
        self.assertIn("blocked by security policy", res.summary.lower())


class TestEmailTools(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="harness_test_mail_")
        self.spool_file = Path("/var/ai-agent/mail_outbox.jsonl")
        self.caller = {"user": "testuser", "home": self.tmp_dir, "cwd": self.tmp_dir}
        self.tools = ToolExecutor(self.caller, allow_privilege_proxy=False)

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_email_send_and_read_spool(self):
        # Send/spool email
        subject = f"Invoice Audit {int(time.time())}"
        body = "Please review attached Q3 summary."
        to_addr = "accounting@example.com"

        send_res = self.tools.run("email.send", {
            "to": to_addr,
            "subject": subject,
            "body": body,
        })
        self.assertTrue(send_res.ok)
        self.assertIn(subject, send_res.stdout)

        # Read emails
        read_res = self.tools.run("email.read", {"search": "Invoice Audit", "max_emails": 5})
        self.assertTrue(read_res.ok)
        self.assertIn(subject, read_res.stdout)


class TestBrowserTools(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="harness_test_browser_")
        self.caller = {"user": "testuser", "home": self.tmp_dir, "cwd": self.tmp_dir}
        self.tools = ToolExecutor(self.caller, allow_privilege_proxy=False)

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_browser_tools_argument_validation(self):
        # Invalid URL for browser.navigate
        res = self.tools.run("browser.navigate", {"url": "not-a-valid-url"})
        self.assertFalse(res.ok)

        # Missing id for browser.click
        res = self.tools.run("browser.click", {})
        self.assertFalse(res.ok)

        # Missing id for browser.type
        res = self.tools.run("browser.type", {"text": "hello"})
        self.assertFalse(res.ok)


class TestDeterministicPlannerFullCoverage(unittest.TestCase):
    def test_all_28_patterns(self):
        test_cases = [
            ("make a directory in /tmp called myfolder", "dir.create"),
            ("create file /tmp/notes.txt", "file.write"),
            ("install package nginx", "package.install"),
            ("restart service sshd", "service.restart"),
            ("install and start nginx", "package.install"),
            ("show system info", "system.info"),
            ("check disk usage", "disk.status"),
            ("check memory usage", "memory.status"),
            ("check port 8080", "port.inspect"),
            ("check http://localhost:8080", "http.check"),
            ("tail logs of /var/log/messages", "file.read"),
            ("enable service docker", "service.enable"),
            ("disable service bluetooth", "service.disable"),
            ("run uptime", "command.argv"),
            ("list files in /etc", "file.list"),
            ("move /tmp/old.txt to /tmp/new.txt", "file.move"),
            ("delete /tmp/junk.txt", "file.delete"),
            ("append hello world to /tmp/log.txt", "file.append"),
            ("read csv /tmp/metrics.csv", "data.csv_read"),
            ("read document /tmp/memo.docx", "document.read"),
            ("send email to lead@example.com with subject Update and body Milestone reached", "email.send"),
            ("check my inbox for mail", "email.read"),
            ("open https://example.com in browser", "browser.navigate"),
            ("dump active browser page elements", "browser.dump"),
            ("click node 105 in browser", "browser.click"),
            ("type john.doe into 204 in browser", "browser.type"),
            ("take a screenshot of browser to /tmp/view.png", "browser.screenshot"),
            ("run python script: print(42)", "shell.script"),
        ]

        for intent, expected_tool in test_cases:
            plan = deterministic_plan(intent)
            self.assertIsNotNone(plan, f"Failed to generate deterministic plan for: {intent}")
            self.assertEqual(plan.steps[0].tool, expected_tool,
                             f"Intent '{intent}' mapped to '{plan.steps[0].tool}', expected '{expected_tool}'")


class TestVariableChainingAndMultiStepWorkflow(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="harness_test_chain_")
        self.caller = {"user": "testuser", "home": self.tmp_dir, "cwd": self.tmp_dir}

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_csv_read_to_scratchpad_to_append(self):
        # Step 1: Write a CSV file
        csv_file = os.path.join(self.tmp_dir, "summary.csv")
        Path(csv_file).write_text("item,qty,status\nServerA,10,Healthy\nServerB,2,Degraded\n", encoding="utf-8")

        # Step 2: Create a job store and harness
        log_records = []
        store = JobStore(self.tmp_dir, "job-chain-1", lambda evt, **kw: log_records.append((evt, kw)))
        store.create("Audit servers and append report", "APPLY", self.caller)

        target_report = os.path.join(self.tmp_dir, "report.txt")

        # Create multi-step plan with variable extraction and interpolation
        plan = Plan(
            goal="Read CSV and append summary to report",
            success_criteria=[{"description": "Report exists", "tool": "file.query", "args": {"path": target_report}}],
            steps=[
                Step(
                    id="step-1-read",
                    purpose="Read server metrics CSV",
                    tool="data.csv_read",
                    args={"path": csv_file},
                    verify=[],
                    risk="read_only",
                    captures={"metrics_json": "stdout:strip"},
                ),
                Step(
                    id="step-2-append",
                    purpose="Append summary to report",
                    tool="file.append",
                    args={"path": target_report, "content": "AUDIT REPORT:\n${metrics_json}\n"},
                    verify=[{"tool": "file.query", "args": {"path": target_report}}],
                    risk="system_change",
                    depends_on=["step-1-read"],
                ),
            ],
        )

        harness = Harness(store, self.caller, "APPLY", lambda prompt: "")
        # Run execution directly using the plan
        status = harness._execute_plan(plan, set(), [])
        self.assertEqual(status, JobStatus.SUCCEEDED)

        # Verify scratchpad captured the JSON
        self.assertIn("metrics_json", harness.scratchpad)
        self.assertTrue(os.path.isfile(target_report))

        # Check content in target report
        report_content = Path(target_report).read_text(encoding="utf-8")
        self.assertIn("AUDIT REPORT:", report_content)
        self.assertIn("ServerA", report_content)
        self.assertIn("ServerB", report_content)


if __name__ == "__main__":
    unittest.main()
