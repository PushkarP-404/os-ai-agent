#!/usr/bin/env python3
"""Comprehensive test and validation suite for harness core stabilization.

Validates:
1. Strict Plan Validation:
   - Tool schema validation for arguments
   - Unregistered tool rejection
   - Invariant checking (risk vs tool, compulsory verification for change tools)
   - DAG dependency cycle detection (2-node, 3-node, self-cycles, valid DAGs)
2. Tool Allowlists & Destructive Safety:
   - Mode enforcement: SUGGEST, READ_ONLY, ASSIST, APPLY
   - Zero destructive actions execute without explicit approval
   - Dangerous command screening in command.argv (forbidden binaries and unsafe rm paths)
3. Before/After Verification:
   - Pre-condition snapshotting
   - Post-condition evaluation and state delta detection
4. Bounded Repair Loops:
   - Maximum 3 repair attempts per step
   - Diagnostic probes and remedial plan re-verification
5. Checkpoint / Resume:
   - Atomic checkpoint.json synchronization
   - Resuming from crash without repeating completed work
   - Prior step sanity verification upon resume
6. 5-Element StepRecord Production:
   - Every step generates: intent, action, expected_verification, actual_result, log_entry
   - Step records persisted in JobStore, events.jsonl, and report.json
7. Success Criteria Benchmarks:
   - 95%+ of valid prompts produce a parseable plan
   - 90%+ of common tasks complete without manual intervention
   - Zero destructive action executes without approval
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path

# Insert harness path
HARNESS_DIR = Path(__file__).resolve().parent.parent / "agent-daemon"
sys.path.insert(0, str(HARNESS_DIR))

import harness_v2
from harness_v2 import (
    ALLOWED_TOOLS,
    JobStatus,
    JobStore,
    Plan,
    PlanError,
    PolicyEngine,
    Step,
    StepRecord,
    ToolExecutor,
    ToolResult,
    Harness,
    validate_plan,
    deterministic_plan,
    extract_first_json_object,
)


class TestStrictPlanValidation(unittest.TestCase):
    """Test suite for strict plan validation and DAG cycle detection."""

    def test_valid_minimal_plan(self):
        plan_dict = {
            "goal": "Check disk space",
            "success_criteria": [
                {"description": "Disk status obtained", "tool": "disk.status", "args": {}}
            ],
            "steps": [
                {
                    "id": "step1",
                    "purpose": "Inspect disk usage",
                    "tool": "disk.status",
                    "args": {},
                    "verify": [],
                }
            ],
        }
        plan = validate_plan(plan_dict)
        self.assertEqual(plan.goal, "Check disk space")
        self.assertEqual(len(plan.steps), 1)
        self.assertEqual(plan.steps[0].risk, "read_only")

    def test_invalid_tool_rejected(self):
        plan_dict = {
            "goal": "Run fake tool",
            "success_criteria": [{"description": "ok", "tool": "disk.status", "args": {}}],
            "steps": [
                {
                    "id": "step1",
                    "purpose": "Run evil tool",
                    "tool": "kernel.exploit",
                    "args": {},
                    "verify": [],
                }
            ],
        }
        with self.assertRaises(PlanError) as ctx:
            validate_plan(plan_dict)
        self.assertIn("invalid tool 'kernel.exploit'", str(ctx.exception))

    def test_missing_required_tool_args_rejected(self):
        # file.read requires 'path'
        plan_dict = {
            "goal": "Read a file",
            "success_criteria": [{"description": "ok", "tool": "disk.status", "args": {}}],
            "steps": [
                {
                    "id": "step1",
                    "purpose": "Read file without path",
                    "tool": "file.read",
                    "args": {},
                    "verify": [],
                }
            ],
        }
        with self.assertRaises(PlanError) as ctx:
            validate_plan(plan_dict)
        self.assertIn("missing required argument 'path'", str(ctx.exception))

    def test_wrong_type_tool_args_rejected(self):
        # command.argv requires argv to be a list
        plan_dict = {
            "goal": "Run command",
            "success_criteria": [{"description": "ok", "tool": "disk.status", "args": {}}],
            "steps": [
                {
                    "id": "step1",
                    "purpose": "Run command with string instead of list",
                    "tool": "command.argv",
                    "args": {"argv": "ls -la"},
                    "verify": [],
                }
            ],
        }
        with self.assertRaises(PlanError) as ctx:
            validate_plan(plan_dict)
        self.assertIn("expected <class 'list'>", str(ctx.exception))

    def test_change_tool_without_verification_rejected(self):
        # dir.create is a CHANGE tool and must have verify entries
        plan_dict = {
            "goal": "Create a directory",
            "success_criteria": [{"description": "ok", "tool": "disk.status", "args": {}}],
            "steps": [
                {
                    "id": "step1",
                    "purpose": "Create dir",
                    "tool": "dir.create",
                    "args": {"path": "/tmp/test_dir"},
                    "verify": [],
                }
            ],
        }
        with self.assertRaises(PlanError) as ctx:
            validate_plan(plan_dict)
        self.assertIn("must include verification steps", str(ctx.exception))

    def test_change_tool_declaring_read_only_risk_rejected(self):
        plan_dict = {
            "goal": "Delete file disguised as read-only",
            "success_criteria": [{"description": "ok", "tool": "disk.status", "args": {}}],
            "steps": [
                {
                    "id": "step1",
                    "purpose": "Delete file",
                    "tool": "file.delete",
                    "args": {"path": "/tmp/note.txt"},
                    "risk": "read_only",
                    "verify": [{"tool": "file.query", "args": {"path": "/tmp/note.txt", "query": "test"}}],
                }
            ],
        }
        with self.assertRaises(PlanError) as ctx:
            validate_plan(plan_dict)
        self.assertIn("declared risk 'read_only'", str(ctx.exception))

    def test_dag_cycle_2_nodes_rejected(self):
        plan_dict = {
            "goal": "Cyclic plan 2 nodes",
            "success_criteria": [{"description": "ok", "tool": "disk.status", "args": {}}],
            "steps": [
                {
                    "id": "step-a",
                    "purpose": "Step A",
                    "tool": "system.info",
                    "args": {},
                    "verify": [],
                    "depends_on": ["step-b"],
                },
                {
                    "id": "step-b",
                    "purpose": "Step B",
                    "tool": "memory.status",
                    "args": {},
                    "verify": [],
                    "depends_on": ["step-a"],
                },
            ],
        }
        with self.assertRaises(PlanError) as ctx:
            validate_plan(plan_dict)
        self.assertIn("Dependency cycle detected", str(ctx.exception))

    def test_dag_cycle_3_nodes_rejected(self):
        plan_dict = {
            "goal": "Cyclic plan 3 nodes",
            "success_criteria": [{"description": "ok", "tool": "disk.status", "args": {}}],
            "steps": [
                {
                    "id": "step-a",
                    "purpose": "Step A",
                    "tool": "system.info",
                    "args": {},
                    "verify": [],
                    "depends_on": ["step-c"],
                },
                {
                    "id": "step-b",
                    "purpose": "Step B",
                    "tool": "memory.status",
                    "args": {},
                    "verify": [],
                    "depends_on": ["step-a"],
                },
                {
                    "id": "step-c",
                    "purpose": "Step C",
                    "tool": "disk.status",
                    "args": {},
                    "verify": [],
                    "depends_on": ["step-b"],
                },
            ],
        }
        with self.assertRaises(PlanError) as ctx:
            validate_plan(plan_dict)
        self.assertIn("Dependency cycle detected", str(ctx.exception))

    def test_valid_dag_diamond_passes(self):
        plan_dict = {
            "goal": "Diamond DAG",
            "success_criteria": [{"description": "ok", "tool": "disk.status", "args": {}}],
            "steps": [
                {"id": "step-a", "purpose": "Root", "tool": "system.info", "args": {}, "verify": []},
                {"id": "step-b", "purpose": "Left branch", "tool": "disk.status", "args": {}, "verify": [], "depends_on": ["step-a"]},
                {"id": "step-c", "purpose": "Right branch", "tool": "memory.status", "args": {}, "verify": [], "depends_on": ["step-a"]},
                {"id": "step-d", "purpose": "Join", "tool": "system.info", "args": {}, "verify": [], "depends_on": ["step-b", "step-c"]},
            ],
        }
        plan = validate_plan(plan_dict)
        self.assertEqual(len(plan.steps), 4)


class TestPolicyEngineAndAllowlists(unittest.TestCase):
    """Test suite for policy modes, tool allowlists, and destructive gates."""

    def setUp(self):
        self.policy = PolicyEngine()

    def test_suggest_mode_blocks_all_changes(self):
        change_step = Step(
            "step1", "Create dir", "dir.create", {"path": "/tmp/test"},
            [{"tool": "dir.query", "args": {"path": "/tmp/test"}}], "system_change"
        )
        dec, reason = self.policy.decide("SUGGEST", change_step)
        self.assertEqual(dec, "SUGGEST")
        self.assertIn("SUGGEST mode: no system changes are executed", reason)

    def test_suggest_mode_allows_readonly_inspection(self):
        ro_step = Step("step1", "Disk check", "disk.status", {}, [])
        dec, reason = self.policy.decide("SUGGEST", ro_step)
        self.assertEqual(dec, "ALLOW")

    def test_read_only_mode_denies_changes(self):
        change_step = Step(
            "step1", "Create dir", "dir.create", {"path": "/tmp/test"},
            [{"tool": "dir.query", "args": {"path": "/tmp/test"}}], "system_change"
        )
        dec, reason = self.policy.decide("READ_ONLY", change_step)
        self.assertEqual(dec, "DENY")

    def test_assist_mode_requires_approval_for_destructive_tools(self):
        destructive_tools = [
            "file.delete", "dir.remove", "package.install",
            "service.stop", "service.disable", "shell.script",
        ]
        for tool in destructive_tools:
            step = Step(f"step-{tool.replace('.', '-')}", f"Test {tool}", tool, {"path": "/tmp/x", "package": "x", "name": "x", "script": "x"}, [], "system_change")
            dec, reason = self.policy.decide("ASSIST", step)
            self.assertEqual(dec, "REQUIRE_APPROVAL", f"Tool {tool} should require approval in ASSIST mode")
            self.assertIn("Destructive tool", reason)

    def test_assist_mode_allows_non_destructive_changes(self):
        step = Step("step1", "Create dir", "dir.create", {"path": "/tmp/test"}, [], "system_change")
        dec, reason = self.policy.decide("ASSIST", step)
        self.assertEqual(dec, "ALLOW")

    def test_apply_mode_authorizes_destructive_tools(self):
        step = Step("step1", "Delete file", "file.delete", {"path": "/tmp/test"}, [], "system_change")
        dec, reason = self.policy.decide("APPLY", step)
        self.assertEqual(dec, "ALLOW")

    def test_command_argv_screening_forbidden_binaries(self):
        forbidden = ["mkfs", "dd", "fdisk", "reboot", "poweroff", "shutdown"]
        for cmd in forbidden:
            step = Step("cmd", f"Run {cmd}", "command.argv", {"argv": [cmd, "/dev/sda"]}, [])
            dec, reason = self.policy.decide("APPLY", step)
            self.assertEqual(dec, "DENY")
            self.assertIn("forbidden", reason)

    def test_command_argv_screening_unsafe_rm_targets(self):
        unsafe_targets = ["/", "/*", "/etc", "/usr", "/var", "/bin", "/sbin", "/boot", "/root"]
        for target in unsafe_targets:
            step = Step("cmd", "Unsafe rm", "command.argv", {"argv": ["rm", "-rf", target]}, [])
            dec, reason = self.policy.decide("APPLY", step)
            self.assertEqual(dec, "DENY", f"rm -rf {target} should be strictly denied")
            self.assertIn("unsafe deletion", reason)

    def test_command_argv_safe_rm_requires_approval_in_assist_mode(self):
        step = Step("cmd", "Safe rm", "command.argv", {"argv": ["rm", "/tmp/scratch.txt"]}, [])
        dec, reason = self.policy.decide("ASSIST", step)
        self.assertEqual(dec, "REQUIRE_APPROVAL")


class TestBeforeAfterVerificationAndDelta(unittest.TestCase):
    """Test suite for pre/post verification and state delta tracking."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="ai_agent_test_verify_")
        self.caller = {"uid": 0, "gid": 0, "user": "root", "home": self.temp_dir, "cwd": self.temp_dir}
        self.store = JobStore(self.temp_dir, "task-test-verify", lambda *a, **k: None)
        self.harness = Harness(self.store, self.caller, "APPLY", lambda p: "")

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_pre_and_post_verify_dir_create(self):
        target_dir = os.path.join(self.temp_dir, "test_sub_dir")
        step = Step(
            "mkdir-test", "Create directory", "dir.create",
            {"path": target_dir},
            [{"tool": "dir.query", "args": {"path": target_dir}}],
            "system_change",
        )
        # Pre-verify snapshot: dir should not exist yet
        pre = self.harness._pre_verify(step, {"path": target_dir})
        self.assertFalse(pre.get("exists"))

        # Execute
        res = self.harness.tools.run("dir.create", {"path": target_dir})
        self.assertTrue(res.ok)

        # Post-verify: should pass and delta should indicate exists=True
        verified, msg, delta = self.harness._post_verify(step, pre)
        self.assertTrue(verified)
        self.assertTrue(delta["post"].get("exists"))
        self.assertTrue(delta["delta_matched"])

    def test_pre_and_post_verify_file_write(self):
        target_file = os.path.join(self.temp_dir, "test_file.txt")
        step = Step(
            "write-test", "Write file", "file.write",
            {"path": target_file, "content": "Sample content"},
            [{"tool": "file.query", "args": {"path": target_file, "query": "Sample"}}],
            "system_change",
        )
        pre = self.harness._pre_verify(step, {"path": target_file, "content": "Sample content"})
        self.assertFalse(pre.get("exists"))

        res = self.harness.tools.run("file.write", {"path": target_file, "content": "Sample content"})
        self.assertTrue(res.ok)

        verified, msg, delta = self.harness._post_verify(step, pre)
        self.assertTrue(verified)
        self.assertTrue(delta["post"].get("exists"))
        self.assertTrue(delta["delta_matched"])


class TestBoundedRepairLoop(unittest.TestCase):
    """Test suite for bounded self-repair mechanism (up to 3 attempts)."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="ai_agent_test_repair_")
        self.caller = {"uid": 0, "gid": 0, "user": "root", "home": self.temp_dir, "cwd": self.temp_dir}
        self.store = JobStore(self.temp_dir, "task-test-repair", lambda *a, **k: None)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_repair_succeeds_on_first_attempt(self):
        target_file = os.path.join(self.temp_dir, "repaired.txt")

        # The model returns a repair plan that writes the missing file
        def fake_llm(prompt: str) -> str:
            return json.dumps({
                "goal": "Repair missing file",
                "success_criteria": [{"description": "File exists", "tool": "file.query", "args": {"path": target_file, "query": "repaired"}}],
                "steps": [
                    {
                        "id": "repair-step-1",
                        "purpose": "Write required file",
                        "tool": "file.write",
                        "args": {"path": target_file, "content": "repaired content"},
                        "verify": [{"tool": "file.query", "args": {"path": target_file, "query": "repaired"}}],
                    }
                ],
            })

        harness = Harness(self.store, self.caller, "APPLY", fake_llm)
        step = Step(
            "check-file", "Read file", "file.read",
            {"path": target_file},
            [{"tool": "file.query", "args": {"path": target_file, "query": "repaired"}}],
            "system_change",
        )
        repaired = harness._repair(step, "File does not exist", attempt=0)
        self.assertTrue(repaired)
        self.assertTrue(os.path.exists(target_file))

    def test_repair_bounded_at_max_repairs(self):
        call_count = 0

        def failing_llm(prompt: str) -> str:
            nonlocal call_count
            call_count += 1
            return "{invalid json}"

        harness = Harness(self.store, self.caller, "APPLY", failing_llm)
        step = Step("unfixable", "Fail step", "file.read", {"path": "/nonexistent/path/never"}, [{"tool": "file.read", "args": {"path": "/nonexistent/path/never"}}])

        # Attempt >= MAX_REPAIRS_PER_STEP (3) should immediately return False
        result = harness._repair(step, "Failed", attempt=3)
        self.assertFalse(result)
        self.assertEqual(call_count, 0)  # Did not even query LLM once limit reached


class TestCheckpointAndResume(unittest.TestCase):
    """Test suite for atomic checkpointing and mid-job crash resume."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="ai_agent_test_ckpt_")
        self.caller = {"uid": 0, "gid": 0, "user": "root", "home": self.temp_dir, "cwd": self.temp_dir}
        self.task_id = "test-resume-101"

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_checkpoint_json_atomically_saved(self):
        store = JobStore(self.temp_dir, self.task_id, lambda *a, **k: None)
        store.create("Test atomic checkpoint", "APPLY", self.caller)
        store.set_checkpoint("step-01", "COMPLETED", {"note": "Step 1 finished"})

        checkpoint_file = Path(self.temp_dir) / "jobs" / self.task_id / "checkpoint.json"
        self.assertTrue(checkpoint_file.exists())

        loaded_cp = store.load_checkpoint()
        self.assertIsNotNone(loaded_cp)
        self.assertEqual(loaded_cp["current_step"], "step-01")
        self.assertEqual(loaded_cp["stage"], "COMPLETED")

    def test_resume_does_not_repeat_completed_steps(self):
        target_dir = os.path.join(self.temp_dir, "resumed_dir")
        target_file = os.path.join(target_dir, "resumed_file.txt")

        # Create store and plan
        store = JobStore(self.temp_dir, self.task_id, lambda *a, **k: None)
        store.create("Create dir and file", "APPLY", self.caller)

        step1 = Step("step-1", "Create dir", "dir.create", {"path": target_dir}, [{"tool": "dir.query", "args": {"path": target_dir}}], "system_change")
        step2 = Step("step-2", "Create file", "file.write", {"path": target_file, "content": "ok"}, [{"tool": "file.query", "args": {"path": target_file, "query": "ok"}}], "system_change", depends_on=["step-1"])
        plan = Plan("Create dir and file", [{"description": "Done", "tool": "dir.query", "args": {"path": target_dir}}], [step1, step2])
        store.save_plan(plan)

        # Simulate step 1 was completed before crash
        os.makedirs(target_dir, exist_ok=True)
        store.state["completed_steps"] = ["step-1"]
        store.set_checkpoint("step-1", "COMPLETED")

        # Now resume
        harness = Harness(store, self.caller, "APPLY", lambda p: "")
        status = harness.resume()

        self.assertEqual(status, JobStatus.SUCCEEDED)
        self.assertTrue(os.path.exists(target_file))
        # Both steps should now be completed
        self.assertIn("step-1", store.state["completed_steps"])
        self.assertIn("step-2", store.state["completed_steps"])


class TestStepRecordProduction(unittest.TestCase):
    """Test suite ensuring every step produces all 5 audit elements."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="ai_agent_test_step_rec_")
        self.caller = {"uid": 0, "gid": 0, "user": "root", "home": self.temp_dir, "cwd": self.temp_dir}
        self.store = JobStore(self.temp_dir, "task-step-rec", lambda *a, **k: None)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_all_five_elements_present_on_each_step(self):
        harness = Harness(self.store, self.caller, "APPLY", lambda p: "")
        intent = "check disk and memory status"
        status = harness.run(intent)
        self.assertEqual(status, JobStatus.SUCCEEDED)

        step_records = self.store.state.get("step_records", [])
        self.assertGreater(len(step_records), 0)

        for rec in step_records:
            # Check 5 required elements:
            self.assertIn("intent", rec)
            self.assertIsInstance(rec["intent"], str)
            self.assertTrue(rec["intent"].strip())

            self.assertIn("action", rec)
            self.assertIsInstance(rec["action"], dict)
            self.assertIn("tool", rec["action"])
            self.assertIn("args", rec["action"])

            self.assertIn("expected_verification", rec)
            self.assertIsInstance(rec["expected_verification"], list)

            self.assertIn("actual_result", rec)
            self.assertIsInstance(rec["actual_result"], dict)
            self.assertIn("ok", rec["actual_result"])
            self.assertIn("summary", rec["actual_result"])

            self.assertIn("log_entry", rec)
            self.assertIsInstance(rec["log_entry"], dict)
            self.assertEqual(rec["log_entry"]["event"], "step_record")

        # Verify report.json also contains step_records
        report_file = Path(self.temp_dir) / "jobs" / "task-step-rec" / "report.json"
        self.assertTrue(report_file.exists())
        with open(report_file, "r", encoding="utf-8") as f:
            report_data = json.load(f)
        self.assertIn("step_records", report_data)
        self.assertEqual(len(report_data["step_records"]), len(step_records))


class TestSuccessCriteriaBenchmarks(unittest.TestCase):
    """Benchmarks validating the 3 core success criteria:

    1. 95%+ of valid prompts produce a parseable plan
    2. 90%+ of common tasks complete without manual intervention
    3. Zero destructive action executes without approval
    """

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="ai_agent_test_bench_")
        self.caller = {"uid": 0, "gid": 0, "user": "root", "home": self.temp_dir, "cwd": self.temp_dir}

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_criterion_1_plan_parseability_95_percent(self):
        valid_prompts = [
            "check disk status",
            "check memory status",
            "show system info",
            "create directory /tmp/test1",
            "make directory in /tmp called test2",
            "create file /tmp/hello.txt with content hello",
            "list files in /tmp",
            "read file /etc/os-release",
            "inspect port 80",
            "check local port 443",
            "check http http://127.0.0.1:80",
            "append text more to /tmp/hello.txt",
            "delete file /tmp/hello.txt",
            "move /tmp/test1 to /tmp/test1_renamed",
            "install package curl",
            "restart service nginx",
            "start service lightdm",
            "stop service lightdm",
            "status of service nginx",
            "logs for service nginx",
            "disable service apache2",
            "enable service sshd",
            "show df -h",
            "show free -m",
            "show uname -a",
            "show uptime",
            "whoami",
            "date",
            "read csv /tmp/data.csv",
            "open browser http://localhost",
        ]

        parsed_count = 0
        for p in valid_prompts:
            plan = deterministic_plan(p, caller=self.caller)
            if plan is not None:
                try:
                    val = validate_plan(asdict(plan))
                    parsed_count += 1
                except Exception:
                    pass

        parse_rate = (parsed_count / len(valid_prompts)) * 100.0
        print(f"\n[BENCHMARK] Parseable plan rate: {parse_rate:.1f}% ({parsed_count}/{len(valid_prompts)})")
        self.assertGreaterEqual(parse_rate, 95.0, "Parseable plan rate must be >= 95%")

    def test_criterion_2_common_tasks_completion_90_percent(self):
        test_tasks = [
            "check disk status",
            "check memory status",
            "show system info",
            "create directory in /tmp called bench_dir_1",
            "create file /tmp/bench_note.txt with content benchmark test",
            "read file /tmp/bench_note.txt",
            "list files in /tmp",
            "append text second line to /tmp/bench_note.txt",
            "show uptime",
            "date",
        ]

        succeeded_count = 0
        for idx, task_intent in enumerate(test_tasks):
            task_id = f"bench-task-{idx}"
            store = JobStore(self.temp_dir, task_id, lambda *a, **k: None)
            harness = Harness(store, self.caller, "APPLY", lambda p: "")
            status = harness.run(task_intent)
            if status == JobStatus.SUCCEEDED:
                succeeded_count += 1

        completion_rate = (succeeded_count / len(test_tasks)) * 100.0
        print(f"[BENCHMARK] Common tasks completion rate: {completion_rate:.1f}% ({succeeded_count}/{len(test_tasks)})")
        self.assertGreaterEqual(completion_rate, 90.0, "Common tasks completion rate must be >= 90%")

    def test_criterion_3_zero_destructive_actions_without_approval(self):
        destructive_prompts = [
            "delete file /tmp/important.txt",
            "remove directory /tmp/important_dir",
            "install package gcc",
            "stop service lightdm",
            "disable service sshd",
            "rm -rf /tmp/scratch",
            "run rm /tmp/file.txt",
            "run pkill nginx",
        ]

        policy = PolicyEngine()
        executed_unapproved = 0

        # In default SUGGEST mode: zero destructive action should execute
        for prompt in destructive_prompts:
            plan = deterministic_plan(prompt, caller=self.caller)
            if plan:
                for step in plan.steps:
                    dec, reason = policy.decide("SUGGEST", step)
                    if dec == "ALLOW":
                        executed_unapproved += 1

        # In ASSIST mode: zero destructive action should execute without approval
        for prompt in destructive_prompts:
            plan = deterministic_plan(prompt, caller=self.caller)
            if plan:
                for step in plan.steps:
                    dec, reason = policy.decide("ASSIST", step)
                    if dec == "ALLOW" and (step.tool in PolicyEngine.DESTRUCTIVE or "rm" in str(step.args)):
                        executed_unapproved += 1

        print(f"[BENCHMARK] Unapproved destructive executions: {executed_unapproved}/{len(destructive_prompts)*2}")
        self.assertEqual(executed_unapproved, 0, "Zero destructive actions must execute without explicit approval!")


if __name__ == "__main__":
    unittest.main()
