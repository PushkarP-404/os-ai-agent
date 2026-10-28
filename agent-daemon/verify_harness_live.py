#!/usr/bin/env python3
"""verify_harness_live.py — Independent Live Verification of Harness Core & Automation Engine

Performs 6 rigorous empirical probes against the live Alpine Linux environment:
1. Strict Plan Validation & DAG Cycle Detection
2. Policy Engine, Destructive Tool Allowlists & Command Screening
3. 5-Element StepRecord Audit Trail in report.json and events.jsonl
4. Before/After Verification with State Delta Calculation
5. Simulated Crash & Checkpoint/Resume Sanity Check
6. Live Intent Bus Unix Domain Socket & Multi-App Routing
"""

import json
import os
from pathlib import Path
import shutil
import socket
import sys
import tempfile
import time

# Ensure harness modules can be imported
sys.path.insert(0, "/usr/lib/ai-agent")

import harness_v2
from harness_v2 import (
    Harness,
    JobStatus,
    JobStore,
    Plan,
    PlanError,
    PolicyEngine,
    Step,
    StepRecord,
    ToolExecutor,
    validate_plan,
)

import automation_engine
from automation_engine import (
    ActionRegistry,
    AutomationEngine,
    IntentBusClient,
    IntentMessage,
    IntentResponse,
    LocalPolicyManager,
)


def probe_1_strict_plan_validation():
    print("\n--- [PROBE 1/6: Strict Plan Validation & DAG Cycle Detection] ---")
    # 1a. Missing required argument
    try:
        validate_plan({
            "goal": "Test missing args",
            "success_criteria": [{"tool": "disk.status", "args": {}}],
            "steps": [{"id": "s1", "purpose": "write without content", "tool": "file.write", "args": {"path": "/tmp/a.txt"}, "verify": [{"tool": "file.query", "args": {"path": "/tmp/a.txt"}}], "risk": "system_change"}]
        })
        raise AssertionError("Failed to reject missing 'content' in file.write")
    except PlanError as e:
        assert "missing required arg" in str(e) or "content" in str(e), f"Unexpected error: {e}"
        print("[PASS] Missing required argument rejected with PlanError:", e)

    # 1b. Unregistered fake tool
    try:
        validate_plan({
            "goal": "Test fake tool",
            "success_criteria": [{"tool": "disk.status", "args": {}}],
            "steps": [{"id": "s1", "purpose": "invoke rootkit", "tool": "kernel.rootkit", "args": {}, "verify": []}]
        })
        raise AssertionError("Failed to reject unregistered tool")
    except PlanError as e:
        assert "invalid tool" in str(e), f"Unexpected error: {e}"
        print("[PASS] Unregistered tool rejected with PlanError:", e)

    # 1c. 2-node cycle (s1 -> s2 -> s1)
    try:
        validate_plan({
            "goal": "Test 2-node cycle",
            "success_criteria": [{"tool": "disk.status", "args": {}}],
            "steps": [
                {"id": "s1", "purpose": "step 1", "tool": "disk.status", "args": {}, "verify": [], "depends_on": ["s2"]},
                {"id": "s2", "purpose": "step 2", "tool": "memory.status", "args": {}, "verify": [], "depends_on": ["s1"]},
            ]
        })
        raise AssertionError("Failed to reject 2-node dependency cycle")
    except PlanError as e:
        assert "cycle" in str(e).lower(), f"Unexpected error: {e}"
        print("[PASS] 2-node DAG cycle rejected with PlanError:", e)

    # 1d. Mutating tool declaring read_only risk
    try:
        validate_plan({
            "goal": "Test risk mismatch",
            "success_criteria": [{"tool": "disk.status", "args": {}}],
            "steps": [{"id": "s1", "purpose": "write as read_only", "tool": "file.write", "args": {"path": "/tmp/a.txt", "content": "hi"}, "verify": [{"tool": "file.query", "args": {"path": "/tmp/a.txt"}}], "risk": "read_only"}]
        })
        raise AssertionError("Failed to reject mutating tool with read_only risk")
    except PlanError as e:
        assert "declared risk 'read_only'" in str(e), f"Unexpected error: {e}"
        print("[PASS] Risk mismatch rejected with PlanError:", e)

    # 1e. Mutating tool with empty verify list
    try:
        validate_plan({
            "goal": "Test empty verify",
            "success_criteria": [{"tool": "disk.status", "args": {}}],
            "steps": [{"id": "s1", "purpose": "write with no verifiers", "tool": "file.write", "args": {"path": "/tmp/a.txt", "content": "hi"}, "verify": [], "risk": "system_change"}]
        })
        raise AssertionError("Failed to reject mutating tool with empty verify list")
    except PlanError as e:
        assert "must include verification" in str(e), f"Unexpected error: {e}"
        print("[PASS] Empty verify list for mutating tool rejected:", e)


def probe_2_policy_engine_and_allowlists():
    print("\n--- [PROBE 2/6: Policy Engine & Destructive Command Screening] ---")
    pe = PolicyEngine()
    step_ro = Step("s_ro", "Disk check", "disk.status", {}, [])
    step_mut = Step("s_mut", "Create file", "file.write", {"path": "/tmp/probe.txt", "content": "probe"}, [{"tool": "file.query", "args": {"path": "/tmp/probe.txt"}}], "system_change")
    step_destruct = Step("s_des", "Delete file", "file.delete", {"path": "/tmp/probe.txt"}, [{"tool": "file.query", "args": {"path": "/tmp/probe.txt"}}], "system_change")

    # 2a. SUGGEST mode
    dec_ro, _ = pe.decide("SUGGEST", step_ro)
    dec_mut, _ = pe.decide("SUGGEST", step_mut)
    dec_des, _ = pe.decide("SUGGEST", step_destruct)
    assert dec_ro == "ALLOW", f"SUGGEST should allow read-only: got {dec_ro}"
    assert dec_mut == "SUGGEST", f"SUGGEST should hold mutation: got {dec_mut}"
    assert dec_des == "SUGGEST", f"SUGGEST should hold destructive: got {dec_des}"
    print("[PASS] SUGGEST mode correctly gates all mutations (0 destructive actions execute)")

    # 2b. ASSIST mode
    dec_assist_mut, _ = pe.decide("ASSIST", step_mut)
    dec_assist_des, _ = pe.decide("ASSIST", step_destruct)
    assert dec_assist_mut == "ALLOW", f"ASSIST should allow normal change: got {dec_assist_mut}"
    assert dec_assist_des == "REQUIRE_APPROVAL", f"ASSIST must require approval for destructive: got {dec_assist_des}"
    print("[PASS] ASSIST mode mandates REQUIRE_APPROVAL for destructive tools")

    # 2c. READ_ONLY mode
    dec_ro_mut, _ = pe.decide("READ_ONLY", step_mut)
    assert dec_ro_mut == "DENY", f"READ_ONLY mode must deny changes: got {dec_ro_mut}"
    print("[PASS] READ_ONLY mode strictly denies mutating steps")

    # 2d. Forbidden command screening in command.argv
    step_mkfs = Step("s_evil", "Format drive", "command.argv", {"argv": ["mkfs.ext4", "/dev/sda1"]}, [])
    step_dd = Step("s_dd", "Wipe disk", "command.argv", {"argv": ["dd", "if=/dev/zero", "of=/dev/sda"]}, [])
    step_rm_root = Step("s_rm", "Delete root", "command.argv", {"argv": ["rm", "-rf", "/"]}, [])
    assert pe.decide("APPLY", step_mkfs)[0] == "DENY"
    assert pe.decide("APPLY", step_dd)[0] == "DENY"
    assert pe.decide("APPLY", step_rm_root)[0] == "DENY"
    print("[PASS] Forbidden commands (mkfs, dd, rm -rf /) strictly DENIED by screening")


def probe_3_five_element_step_record():
    print("\n--- [PROBE 3/6: 5-Element StepRecord Audit Trail] ---")
    tmp_dir = tempfile.mkdtemp(prefix="probe_step_rec_")
    task_id = "rec-task-01"
    store = JobStore(tmp_dir, task_id, lambda *a, **k: None)
    caller = {"uid": 0, "gid": 0, "user": "root", "home": tmp_dir, "cwd": tmp_dir}
    harness = Harness(store, caller, "APPLY", lambda p: "")
    store.create("check disk status", "APPLY", caller)
    status = harness.run("check disk status")
    assert status == JobStatus.SUCCEEDED, f"Task failed: {status}"

    # Verify report.json on disk
    report_file = Path(tmp_dir) / "jobs" / task_id / "report.json"
    assert report_file.exists(), f"report.json missing at {report_file}"
    with open(report_file, "r", encoding="utf-8") as f:
        rep = json.load(f)

    assert "step_records" in rep, "report.json missing step_records"
    records = rep["step_records"]
    assert len(records) > 0, "No step records in report.json"

    r0 = records[0]
    required_elements = ["intent", "action", "expected_verification", "actual_result", "log_entry"]
    for elem in required_elements:
        assert elem in r0, f"StepRecord missing required element: {elem}"
        print(f"  [CHECK] StepRecord contains '{elem}': {type(r0[elem]).__name__}")

    assert r0["log_entry"]["event"] == "step_record"
    assert r0["actual_result"]["exit_code"] == 0
    print("[PASS] All 5 required StepRecord elements verified in persistent report.json")
    shutil.rmtree(tmp_dir, ignore_errors=True)


def probe_4_before_after_verification_and_state_delta():
    print("\n--- [PROBE 4/6: Before/After Verification & State Delta] ---")
    tmp_dir = tempfile.mkdtemp(prefix="probe_delta_")
    target_file = Path(tmp_dir) / "probe_target.txt"

    # Step: file.write
    step_write = Step(
        "s_write", "Write sample file", "file.write",
        {"path": str(target_file), "content": "Initial payload string\n"},
        [{"tool": "file.query", "args": {"path": str(target_file)}}],
        "system_change"
    )

    caller = {"uid": 0, "gid": 0, "user": "root", "home": tmp_dir, "cwd": tmp_dir}
    store = JobStore(tmp_dir, "delta-01", lambda *a, **k: None)
    harness = Harness(store, caller, "APPLY", lambda p: "")

    # Pre-verify snapshot
    snap_before = harness._pre_verify(step_write, step_write.args)
    print("  [DELTA] Pre-verify snapshot (exists):", snap_before.get("exists"))
    assert snap_before.get("exists") is False

    # Execute
    res = harness.tools.run("file.write", step_write.args)
    assert res.ok is True

    # Post-verify delta
    post_ok, post_msg, delta = harness._post_verify(step_write, snap_before)
    print("  [DELTA] Post-verify outcome:", post_ok, post_msg)
    print("  [DELTA] State delta computed:", delta)
    assert post_ok is True
    assert delta.get("delta_matched") is True
    assert delta["pre"]["exists"] is False
    assert delta["post"]["exists"] is True
    assert delta["post"]["size"] > 0

    print("[PASS] Before/After state delta accurately captured file creation and size")
    shutil.rmtree(tmp_dir, ignore_errors=True)


def probe_5_crash_and_checkpoint_resume():
    print("\n--- [PROBE 5/6: Crash Interruption & Atomic Checkpoint/Resume] ---")
    tmp_dir = tempfile.mkdtemp(prefix="probe_resume_")
    task_id = "crash-resume-01"
    f1 = Path(tmp_dir) / "step1.txt"
    f2 = Path(tmp_dir) / "step2.txt"

    plan = Plan(
        goal="Simulated 2-step pipeline with mid-flight crash",
        success_criteria=[
            {"tool": "file.query", "args": {"path": str(f1)}},
            {"tool": "file.query", "args": {"path": str(f2)}},
        ],
        steps=[
            Step("step1", "Create file 1", "file.write", {"path": str(f1), "content": "File 1 content\n"},
                 [{"tool": "file.query", "args": {"path": str(f1)}}], "system_change"),
            Step("step2", "Create file 2", "file.write", {"path": str(f2), "content": "File 2 content\n"},
                 [{"tool": "file.query", "args": {"path": str(f2)}}], "system_change", depends_on=["step1"]),
        ]
    )

    caller = {"uid": 0, "gid": 0, "user": "root", "home": tmp_dir, "cwd": tmp_dir}
    store1 = JobStore(tmp_dir, task_id, lambda *a, **k: None)
    store1.create(plan.goal, "APPLY", caller)
    store1.save_plan(plan)

    # Step 1 executes successfully
    res1 = ToolExecutor(caller).run("file.write", plan.steps[0].args)
    assert res1.ok is True
    assert f1.exists()

    # Emulate crash right before step 2: atomically save checkpoint.json
    store1.state["completed_steps"] = ["step1"]
    store1.state["failed_steps"] = []
    store1.set_checkpoint("step2", "INTERRUPTED", {"interrupted_reason": "simulated SIGKILL"})

    ckpt_file = Path(tmp_dir) / "jobs" / task_id / "checkpoint.json"
    assert ckpt_file.exists(), "checkpoint.json missing after crash"
    print("  [CHECKPOINT] Atomic checkpoint verified on disk:", ckpt_file.name)

    # Now create a fresh Harness instance simulating daemon reboot and resume
    store2 = JobStore(tmp_dir, task_id, lambda *a, **k: None)
    harness2 = Harness(store2, caller, "APPLY", lambda p: "")
    status = harness2.resume()

    print("  [RESUME] Status returned by harness.resume():", status)
    assert status == JobStatus.SUCCEEDED
    assert f2.exists(), "Step 2 should have been completed upon resume"
    assert "step1" in store2.state["completed_steps"]
    assert "step2" in store2.state["completed_steps"]
    print("[PASS] Checkpoint resume re-verified step 1 sanity and completed step 2 seamlessly")
    shutil.rmtree(tmp_dir, ignore_errors=True)


def probe_6_intent_bus_socket_and_multi_app_routing():
    print("\n--- [PROBE 6/6: Live Intent Bus Socket Broker & Inter-App Routing] ---")
    sock_path = "/var/run/ai-agent/intent.sock"
    if not Path(sock_path).exists():
        sock_path = "/var/ai-agent/intent.sock"
    assert Path(sock_path).exists(), f"Intent bus socket missing at {sock_path}"

    # 6a. Direct client connect and ping over live Unix socket
    client = IntentBusClient("probe-client", socket_path=sock_path)
    client.connect(timeout=3.0)
    assert client.ping() is True, "Intent bus failed ping"
    print("[PASS] Live Unix socket connect and ping operational on", sock_path)

    # 6b. Single-intent dispatch from code-editor to coding-assistant
    intent_code = IntentMessage(
        task_id="probe-code-01",
        source_app="code-editor",
        target_app="coding-assistant",
        intent_type="code.analyze",
        payload={"code": "class AIKernel:\n    def execute(self):\n        return True\n"}
    )
    resp = client.send_intent(intent_code, mode="APPLY")
    assert resp.status == "SUCCESS", f"Intent dispatch failed: {resp.error}"
    assert resp.data.get("syntax_valid") is True
    print(f"[PASS] Inter-app intent dispatch: {resp.source_app} -> {resp.target_app} returned SUCCESS ({resp.elapsed_s*1000:.1f}ms)")
    client.disconnect()

    # 6c. Autonomous multi-app workflow chain routing
    engine = automation_engine.get_automation_engine()
    test_out = Path("/tmp/probe_autoworkflow.txt")
    if test_out.exists():
        test_out.unlink()

    workflow = [
        {"target_app": "coding-assistant", "intent_type": "code.explain", "payload": {"code": "x = 42"}},
        {"target_app": "file-manager", "intent_type": "file.write", "payload": {"path": str(test_out), "content": "result = 42\n"}},
        {"target_app": "file-manager", "intent_type": "file.query", "payload": {"path": str(test_out)}},
        {"target_app": "email-service", "intent_type": "email.draft", "payload": {"to": "audit@os-ai.local", "subject": "Probe 6 Done"}},
    ]

    resps = engine.route_multi_app_workflow("code-editor", workflow, mode="APPLY")
    assert len(resps) == 4
    for idx, r in enumerate(resps):
        assert r.status == "SUCCESS", f"Workflow step {idx} failed: {r.error}"
        print(f"  [ROUTED STEP {idx+1}/4] target={r.target_app} status={r.status} summary={r.summary[:50]}")

    assert test_out.exists()
    print("[PASS] Autonomous multi-app workflow chain routed without human manual bridging")


def main():
    print("==================================================================")
    print("       AI-AGENT OS: INDEPENDENT LIVE HARNESS & ENGINE AUDIT       ")
    print("==================================================================")
    start_time = time.time()

    probe_1_strict_plan_validation()
    probe_2_policy_engine_and_allowlists()
    probe_3_five_element_step_record()
    probe_4_before_after_verification_and_state_delta()
    probe_5_crash_and_checkpoint_resume()
    probe_6_intent_bus_socket_and_multi_app_routing()

    elapsed = time.time() - start_time
    print("\n==================================================================")
    print(f"AUDIT RESULT: ALL 6 EMPIRICAL PROBES PASSED PERFECTLY ({elapsed:.2f}s)")
    print("==================================================================")


if __name__ == "__main__":
    main()
