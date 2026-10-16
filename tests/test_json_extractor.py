#!/usr/bin/env python3
"""Unit tests for robust JSON extractor and command sanitizer."""

import os
import sys
import unittest

# Add agent-daemon to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "agent-daemon")))

from task_runner import extract_first_json_object, sanitize_cmd, extract_action
from harness_v2 import validate_plan, PlanError


class TestJsonExtractor(unittest.TestCase):
    def test_pure_json_object(self):
        text = '{"cmd": "ls -la", "timeout": 30}'
        res = extract_first_json_object(text)
        self.assertEqual(res, {"cmd": "ls -la", "timeout": 30})

    def test_pure_json_array(self):
        text = '[{"tool": "dir.create", "args": {"path": "/tmp/test"}}]'
        res = extract_first_json_object(text)
        self.assertEqual(res, [{"tool": "dir.create", "args": {"path": "/tmp/test"}}])

    def test_conversational_prefix_and_suffix(self):
        text = (
            "Certainly! Here is your structured command:\n"
            "{\"cmd\": \"mkdir -p /root/Desktop/Test\"}\n"
            "Let me know if you need additional assistance."
        )
        res = extract_first_json_object(text)
        self.assertEqual(res, {"cmd": "mkdir -p /root/Desktop/Test"})

    def test_markdown_code_fence(self):
        text = "```json\n{\"goal\": \"inspect\", \"steps\": []}\n```"
        res = extract_first_json_object(text)
        self.assertEqual(res, {"goal": "inspect", "steps": []})

    def test_nested_brackets_inside_strings(self):
        text = '{"pattern": "Find all {items} in [folder]", "count": 5}'
        res = extract_first_json_object(text)
        self.assertEqual(res, {"pattern": "Find all {items} in [folder]", "count": 5})

    def test_escaped_quotes_inside_strings(self):
        text = '{"cmd": "echo \\"Hello World\\" > test.txt"}'
        res = extract_first_json_object(text)
        self.assertEqual(res, {"cmd": 'echo "Hello World" > test.txt'})

    def test_trailing_comma_recovery(self):
        text = '{"cmd": "df -h", "flag": true,}'
        res = extract_first_json_object(text)
        self.assertEqual(res, {"cmd": "df -h", "flag": True})

    def test_llm_error_text(self):
        text = "[LLM ERROR] <urlopen error [Errno 111] Connection refused>"
        res = extract_first_json_object(text)
        self.assertIsNone(res)

    def test_empty_and_whitespace(self):
        self.assertIsNone(extract_first_json_object(""))
        self.assertIsNone(extract_first_json_object("   \n\t  "))
        self.assertIsNone(extract_first_json_object(None))

    def test_truncated_json(self):
        text = '{"goal": "install curl", "steps": [{"tool": "package.install"'
        self.assertIsNone(extract_first_json_object(text))


class TestSanitizeCmd(unittest.TestCase):
    def test_strips_surrounding_quotes(self):
        self.assertEqual(sanitize_cmd('"mkdir -p /test"'), "mkdir -p /test")
        self.assertEqual(sanitize_cmd("'touch /file.txt'"), "touch /file.txt")

    def test_normalizes_whitespace(self):
        self.assertEqual(sanitize_cmd("  apk    add   curl  "), "apk add curl")

    def test_removes_null_bytes(self):
        self.assertEqual(sanitize_cmd("ls\x00 -la"), "ls -la")

    def test_empty_or_none(self):
        self.assertEqual(sanitize_cmd(""), "")
        self.assertEqual(sanitize_cmd(None), "")


class TestExtractAction(unittest.TestCase):
    def test_direct_json_cmd(self):
        raw = '{"cmd": "touch notes.txt"}'
        action, cmd = extract_action(raw)
        self.assertEqual(action, "execute")
        self.assertEqual(cmd, "touch notes.txt")

    def test_quoted_in_prose(self):
        raw = "You should run `df -h` to see your storage."
        action, cmd = extract_action(raw)
        self.assertEqual(action, "execute")
        self.assertEqual(cmd, "df -h")

    def test_error_string(self):
        raw = "[LLM ERROR] Model timed out"
        action, cmd = extract_action(raw)
        self.assertEqual(action, "unknown")


class TestPlanValidationIntegration(unittest.TestCase):
    def test_valid_plan_extraction(self):
        raw = """
        I have analyzed your request. Here is the validated execution plan:
        ```json
        {
            "goal": "Verify system disk status",
            "assumptions": ["Root filesystem mounted"],
            "success_criteria": [{"tool": "disk.status", "args": {}}],
            "steps": [
                {
                    "id": "step-1",
                    "purpose": "Check disk usage",
                    "tool": "disk.status",
                    "args": {},
                    "depends_on": [],
                    "verify": []
                }
            ]
        }
        ```
        Proceeding with automated execution.
        """
        obj = extract_first_json_object(raw)
        self.assertIsNotNone(obj)
        plan = validate_plan(obj)
        self.assertEqual(plan.goal, "Verify system disk status")
        self.assertEqual(len(plan.steps), 1)
        self.assertEqual(plan.steps[0].tool, "disk.status")


if __name__ == "__main__":
    unittest.main()
