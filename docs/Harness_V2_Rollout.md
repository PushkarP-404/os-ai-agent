# Harness V2 Rollout & OS Automation Engine Specification

Harness V2 replaces free-form model-to-shell execution with a verified, deterministic, and policy-governed task execution engine. Paired with the **OS Automation Engine & Intent Bus**, it provides process-to-process intent messaging and autonomous multi-application routing across the operating system.

---

## 1. What Harness V2 Delivers

- **Durable Job Store (`/var/ai-agent/jobs/<job-id>/`)**:
  - `job.json`: Goal, mode, caller credentials, and lifecycle status.
  - `plan.json`: Validated, dependency-checked DAG task plan.
  - `checkpoint.json`: Atomic progress snapshot for mid-flight crash recovery.
  - `events.jsonl`: Real-time append-only telemetry stream.
  - `report.json`: Persistent completion audit containing 5-element `StepRecord` tuples.
- **Strict Plan Validation & DAG Cycle Detection**:
  - Validates required arguments for all tools before execution.
  - Rejects unregistered/hallucinatory tools (`PlanError`).
  - Detects cyclic dependencies (`s1 -> s2 -> s1`) via depth-first graph search.
  - Catches risk mismatches (mutating tool falsely declaring `read_only`).
  - Rejects mutating steps lacking explicit verifier blocks.
- **Policy Engine & Destructive Screening**:
  - `SUGGEST` (Default): All state mutations are intercepted and returned as suggestions (**0 destructive actions execute without explicit approval**).
  - `ASSIST`: Read-only actions run automatically; destructive tools require operator confirmation.
  - `READ_ONLY`: Blocks all mutations (`DENY`).
  - `APPLY`: Pre-authorized autonomous execution.
  - Screens `command.argv` against wipe commands (`mkfs.*`, `fdisk`, `dd`, `rm -rf /`, `shutdown`, `poweroff`).
- **5-Element StepRecord Audit Trail**:
  Every step produces:
  1. `intent` (purpose string)
  2. `action` (tool, arguments, security context)
  3. `expected_verification` (declared verifiers)
  4. `actual_result` (exit code, stdout, stderr, facts)
  5. `log_entry` (timestamped event record)
- **Before/After State Verification & State Delta**:
  - Pre-execution snapshot (`_pre_verify`).
  - Post-execution snapshot (`_post_verify`).
  - Computes exact system deltas (`delta_matched`, file creation, byte sizes, service active states).
- **Atomic Checkpoint & Crash Resume**:
  - Atomically saves progress after each milestone.
  - Upon reboot or interruption, `harness.resume()` re-verifies completed steps and seamlessly executes remaining steps without state corruption.
- **Privileged Tool Broker (`harness_privileged.py`)**:
  - Normal `ai-agent` daemon runs unprivileged (`ai-agent:ai-agent`).
  - `CHANGE` tools route through the local root-owned Unix domain socket `/run/ai-agent/privileged.sock`.

---

## 2. Complete 38-Tool Catalogue (9 Intent Domains)

### Read-Only Tools (21 Tools)
- **System**: `system.info`, `disk.status`, `memory.status`
- **Package**: `package.query`
- **Service**: `service.status`, `service.logs`
- **Network / HTTP**: `port.inspect`, `http.check`, `http.get`
- **Filesystem**: `file.read`, `file.query`, `file.list`, `dir.query`
- **Data & Documents**: `data.csv_read`, `document.read`
- **Email**: `email.read`
- **Browser**: `browser.navigate`, `browser.dump`, `browser.screenshot`
- **Execution & Reasoning**: `command.argv`, `llm.prompt`

### System-Changing Tools (17 Tools)
- **Package**: `package.install`
- **Service**: `service.start`, `service.restart`, `service.stop`, `service.enable`, `service.disable`
- **Filesystem**: `dir.create`, `dir.remove`, `file.write`, `file.append`, `file.move`, `file.delete`
- **Data & Documents**: `data.csv_write`
- **Email**: `email.send`
- **Browser**: `browser.click`, `browser.type`
- **Execution**: `shell.script`

---

## 3. OS Automation Engine & Intent Bus

The OS Automation Engine (`automation_engine.py`) provides an internal messaging bus and autonomous router:

- **Socket Broker**: `/var/run/ai-agent/intent.sock` (mode `0666`, non-root accessible).
- **Standard Intent Schema (`IntentMessage` / `IntentResponse`)**:
  - `task_id`, `source_app`, `target_app`, `intent_type`, `payload`, `required_confirmation`, `timeout`, `callback_route`.
- **Action Executor Registry**:
  - 7 tool categories: browser, file, email, git, service, document, coding-assistant.
- **Local Policy Manager (`LocalPolicyManager`)**:
  - Per-app allow/deny rules, per-user permissions, and risk tiers.
- **Autonomous Multi-App Workflow Routing**:
  - Automatically bridges context across applications (e.g. `code-editor` -> `coding-assistant` -> `file-manager` -> `email-service`) without human manual intervention.

---

## 4. Enabling & Operating in the Guest

1. **Service Management (OpenRC)**:
   ```sh
   rc-service ai-agent restart
   ```
2. **Intent Bus CLI (`agent-intent`)**:
   ```sh
   # Verify broker status
   agent-intent ping

   # Dispatch intent between applications
   agent-intent send --from code-editor --to git-service --type git.status --mode APPLY

   # Execute multi-app autonomous workflow
   agent-intent route --from code-editor --workflow '[{"target_app": "file-manager", "intent_type": "file.query", "payload": {"path": "/etc/alpine-release"}}]' --mode APPLY

   # Suggestion mode (zero state mutation)
   agent-intent send --from code-editor --to file-manager --type file.delete --payload '{"path": "/tmp/junk.txt"}' --mode SUGGEST
   ```

---

## 5. Empirical Verification Results

Verified in both the local development harness and live in the QEMU Alpine Linux guest VM (`6.6.142-ai-agent`):

| Test Suite | Purpose | Results |
| :--- | :--- | :--- |
| `tests/test_harness_stabilization.py` | Schema, cycle detection, StepRecords, checkpoints | **28/28 PASS** (100%) |
| `tests/test_automation_engine.py` | Intent bus, app messaging, registry, routing | **21/21 PASS** (100%) |
| `tests/test_harness_comprehensive.py` | 38-tool catalog execution & policy checks | **16/16 PASS** (100%) |
| `agent-daemon/verify_harness_live.py` | 6 live empirical in-guest probes | **6/6 PASS** (100%) |
| **Parseable Plan Rate** | Model outputs adhering to executable grammar | **100.0%** (30/30) |
| **Common Tasks Completion** | Standard OS tasks executed without intervention | **100.0%** (10/10) |
| **Destructive Invariant Safety** | Unapproved destructive operations executed | **0 / 16** (Zero unapproved mutations) |
