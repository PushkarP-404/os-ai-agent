# Harness V2 Rollout

Harness V2 replaces free-form model-to-shell execution with the standard
verified task workflow.

## What V2 changes

- Jobs are stored in `/var/ai-agent/jobs/<job-id>/` with `job.json`, validated
  `plan.json`, append-only events, and `report.json`.
- Model output must be a JSON plan that uses registered tools. Invalid JSON,
  unknown tools, missing verification, and invalid dependencies stop the job
  before an OS action is run.
- Tool execution uses fixed argv lists. V2 does not pass model text to `sh -c`.
- A system-changing step succeeds only after its declared verifier succeeds.
- Failures trigger bounded service/package/system evidence collection and a
  report with a safe next recommendation.
- The normal `ai-agent` daemon stays unprivileged. System-changing V2 tools
  are routed through the local root-owned `ai-agent-privileged` broker, which
  accepts only the `ai-agent` Unix-socket peer and only registered tools.

## Initial registered tools

Read-only tools:

- `system.info`, `disk.status`, `memory.status`
- `package.query`
- `service.status`, `service.logs`
- `port.inspect`, `file.read`, `http.check`

System-changing tools:

- `package.install`
- `service.start`, `service.restart`, `service.stop`

The first V2 release deliberately excludes arbitrary shell commands, file
writes, remote network access, and process termination. They need a typed
tool, policy rule, rollback behavior, and integration coverage before being
added.

## Enable in the guest

1. Deploy the updated daemon files, broker, and OpenRC scripts. The revised
   `sync.py` uploads all of them to both installed daemon locations.
2. Start the broker before the normal daemon:

   ```sh
   rc-service ai-agent-privileged start
   rc-service ai-agent restart
   ```

3. V2 is enabled by default. To make the setting explicit in
   `/etc/conf.d/ai-agent`, use:

   ```sh
   HARNESS_V2="1"
   ```

4. Use `agent-cli` with a structured task request. `/suggest` remains
   non-executing. `/apply` explicitly requests an apply-mode task:

   ```sh
   agent-cli "/apply restart nginx and verify it is running"
   ```

Set `HARNESS_V2="0"` only as a temporary rollback to the legacy executor
while investigating a deployment issue.

## Required next implementation slices

1. Add a strict repair-plan schema, rollback artifacts, and bounded automatic
   repair execution after the current diagnostic stage.
2. Add an event socket so the CLI receives daemon events without polling the
   legacy task JSONL file.
3. Add explicit CLI approval/resume/cancel/status controls backed by durable
   job state.
4. Add typed file-edit and package/service configuration tools with backup,
   scope validation, and rollback.
5. Exercise the workflow in QEMU with valid/invalid plans, service failures,
   package failures, cancellations, broker denial, and recovery after restart.
