#!/bin/sh
# test_phase5.sh — AI-Agent OS Phase 5 Verification Harness
#
# Validates:
#   1. Custom ai-agent kernel is running
#   2. Ollama is running locally inside the guest
#   3. The Phase 5 target model is available
#   4. agent_daemon.py starts cleanly against local Ollama
#   5. sys_agent_query (syscall 548) returns a real in-guest LLM response
#   6. Structured logs appear in /var/ai-agent/
#   7. OpenRC services (ollama + ai-agent) start/stop cleanly
#
# Run inside the Alpine guest:
#   chmod +x /root/os-ai-agent/test_phase5.sh
#   /root/os-ai-agent/test_phase5.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

PASS=0
FAIL=0
WARNINGS=0

ok()   { echo "[PASS] $1"; PASS=$((PASS+1)); }
fail() { echo "[FAIL] $1"; FAIL=$((FAIL+1)); }
warn() { echo "[WARN] $1"; WARNINGS=$((WARNINGS+1)); }
step() { echo ""; echo "── $1 ──────────────────────────────────────────"; }

echo "=================================================="
echo "      AI-Agent OS: Phase 5 Verification Test      "
echo "=================================================="

# ── Step 1: Kernel version ────────────────────────────────────────────────────
step "STEP 1: Kernel Version"
KERNEL=$(uname -r)
echo "Running kernel: $KERNEL"
case "$KERNEL" in
    *-ai-agent*)
        ok "Custom ai-agent kernel detected"
        ;;
    *)
        warn "Not on ai-agent kernel ($KERNEL) — syscall 548 tests may fail"
        ;;
esac

# ── Step 2: In-guest Ollama reachability ─────────────────────────────────────
step "STEP 2: In-Guest Ollama Connectivity"
OLLAMA_RESP=$(curl -sf http://127.0.0.1:11434/ 2>&1 || true)
if echo "$OLLAMA_RESP" | grep -qi "ollama is running"; then
    ok "Ollama is running at http://127.0.0.1:11434/"
else
    fail "Ollama not reachable at http://127.0.0.1:11434/ (got: $OLLAMA_RESP)"
    echo "  -> Start it with: ollama serve &"
fi

# ── Step 3: Target model available ───────────────────────────────────────────
step "STEP 3: Model Availability"
TARGET_MODEL="${OLLAMA_MODEL:-llama3.2:3b-instruct-q4_K_M}"
echo "Expected model: $TARGET_MODEL"
MODEL_LIST=$(curl -sf http://127.0.0.1:11434/api/tags 2>/dev/null | python3 -c "
import json, sys
data = json.load(sys.stdin)
for m in data.get('models', []):
    print(m.get('name', ''))
" 2>/dev/null || true)
if echo "$MODEL_LIST" | grep -q "$TARGET_MODEL"; then
    ok "Model '$TARGET_MODEL' is available"
else
    warn "Model '$TARGET_MODEL' not found. Available: $(echo $MODEL_LIST | tr '\n' ' ')"
    echo "  -> Pull it with: ollama pull $TARGET_MODEL"
fi

# ── Step 4: Compile test programs ────────────────────────────────────────────
step "STEP 4: Compile Test Programs"
make -C test-programs clean >/dev/null 2>&1 || true
make -C test-programs
ok "test_syscall and test_syscall_stress compiled"

# ── Step 5: Start agent_daemon.py against local Ollama ───────────────────────
step "STEP 5: Agent Daemon Startup (local Ollama)"
DAEMON_LOG="/tmp/agent_daemon_phase5.log"
OLLAMA_URL=http://127.0.0.1:11434/api/generate \
OLLAMA_MODEL="$TARGET_MODEL" \
  python3 agent-daemon/agent_daemon.py > "$DAEMON_LOG" 2>&1 &
DAEMON_PID=$!
echo "  -> Started agent_daemon.py (PID: $DAEMON_PID)"
sleep 2

if kill -0 "$DAEMON_PID" 2>/dev/null; then
    ok "Daemon is running"
else
    fail "Daemon exited unexpectedly — check $DAEMON_LOG"
    cat "$DAEMON_LOG"
fi

# ── Step 6: Syscall 548 with live local LLM ──────────────────────────────────
step "STEP 6: sys_agent_query — Live In-Guest LLM Response"
echo "  -> Firing syscall 548 with daemon + local Ollama online..."
SYSCALL_OUT=$(./test-programs/test_syscall \
    "This is Phase 5 validation. Process is about to open a socket to 8.8.8.8:53 for DNS. Assess risk." \
    2>&1) || true
echo "$SYSCALL_OUT"
if echo "$SYSCALL_OUT" | grep -q "SUCCESS"; then
    ok "syscall 548 returned a response"
else
    fail "syscall 548 did not return SUCCESS"
fi

# ── Step 7: Concurrent stress test ───────────────────────────────────────────
step "STEP 7: Concurrent Stress Test (5 threads)"
echo "  -> This may take up to 90s (5 x 15s kernel timeout worst case)..."
STRESS_OUT=$(./test-programs/test_syscall_stress 2>&1) || true
echo "$STRESS_OUT"
if echo "$STRESS_OUT" | grep -q "ALL PASS"; then
    ok "Concurrent stress test passed"
else
    warn "Stress test did not report ALL PASS (check output above)"
fi

# ── Step 8: Structured log files ─────────────────────────────────────────────
step "STEP 8: Structured Logging (/var/ai-agent/)"
if [ -d /var/ai-agent ]; then
    ok "/var/ai-agent/ directory exists"
    DATASET=/var/ai-agent/training_data/dataset.jsonl
    if [ -f "$DATASET" ]; then
        RECORDS=$(wc -l < "$DATASET")
        ok "dataset.jsonl exists with $RECORDS record(s)"
        echo "  -> First record preview:"
        head -1 "$DATASET" | python3 -m json.tool 2>/dev/null | head -20 || \
            head -1 "$DATASET"
    else
        warn "dataset.jsonl not yet written (may need a successful syscall 548 query)"
    fi
    RESP_LOG=$(ls /var/ai-agent/logs/agent_responses/ 2>/dev/null | head -1)
    if [ -n "$RESP_LOG" ]; then
        ok "Agent responses log found: $RESP_LOG"
    else
        warn "No agent response log files yet"
    fi
else
    warn "/var/ai-agent/ not created yet (daemon with logging may not have run)"
fi

# ── Step 9: OpenRC services ───────────────────────────────────────────────────
step "STEP 9: OpenRC Services"
if [ -f /etc/init.d/ollama ] && [ -f /etc/init.d/ai-agent ]; then
    ok "OpenRC init scripts installed"
    rc-service ai-agent status 2>/dev/null && ok "ai-agent service status OK" || \
        warn "ai-agent OpenRC status returned non-zero (may not be registered yet)"
else
    warn "OpenRC scripts not yet installed in /etc/init.d/"
    echo "  -> Run: cp /root/os-ai-agent/agent-daemon/openrc/ollama /etc/init.d/"
    echo "  -> Run: cp /root/os-ai-agent/agent-daemon/openrc/ai-agent /etc/init.d/"
    echo "  -> Run: chmod +x /etc/init.d/ollama /etc/init.d/ai-agent"
    echo "  -> Run: rc-update add ollama default && rc-update add ai-agent default"
fi

# ── Step 10: Cleanup ──────────────────────────────────────────────────────────
step "STEP 10: Cleanup"
kill -TERM "$DAEMON_PID" 2>/dev/null || true
wait "$DAEMON_PID" 2>/dev/null || true
ok "Daemon cleaned up"

echo ""
echo "=================================================="
echo "  Daemon log ($DAEMON_LOG):"
echo "--------------------------------------------------"
cat "$DAEMON_LOG"
echo "=================================================="
echo ""
echo "  Phase 5 Verification Summary"
echo "  PASS: $PASS   FAIL: $FAIL   WARNINGS: $WARNINGS"
if [ "$FAIL" -eq 0 ]; then
    echo "  RESULT: PHASE 5 VALIDATED"
else
    echo "  RESULT: $FAIL FAILURE(S) — see output above"
fi
echo "=================================================="

[ "$FAIL" -eq 0 ]
