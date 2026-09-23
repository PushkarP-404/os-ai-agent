#!/bin/sh
# test_phase5.sh — AI-Agent OS Phase 5 Verification Harness
#
# Phase 5 (native musl llama.cpp): validates:
#   1. Custom ai-agent kernel is running
#   2. llama-server binary exists and is executable
#   3. SmolLM2 GGUF model exists in /root/models/
#   4. llama-server starts and responds to /health
#   5. llama-server /completion returns a real in-guest LLM response
#   6. agent_daemon.py starts cleanly against llama-server
#   7. sys_agent_query (syscall 548) returns a real in-guest LLM response
#   8. Structured logs appear in /var/ai-agent/
#   9. OpenRC services (llama-server + ai-agent) present in /etc/init.d/
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

LLAMA_SERVER_BIN="${LLAMA_SERVER_BIN:-/root/llama.cpp/build/bin/llama-server}"
LLAMA_CLI_BIN="${LLAMA_CLI_BIN:-/root/llama.cpp/build/bin/llama-cli}"
MODEL_PATH="${MODEL_PATH:-/root/models/smollm2-135m-instruct-q4_k_m.gguf}"
LLAMA_PORT=11434
LLAMA_HOST=127.0.0.1

echo "=================================================="
echo "      AI-Agent OS: Phase 5 Verification Test      "
echo "      (Native musl llama.cpp + SmolLM2-135M)      "
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

# ── Step 2: llama-server binary ───────────────────────────────────────────────
step "STEP 2: llama-server Binary"
if [ -x "$LLAMA_SERVER_BIN" ]; then
    ok "llama-server binary found: $LLAMA_SERVER_BIN"
    FILE_SIZE=$(du -h "$LLAMA_SERVER_BIN" | cut -f1)
    echo "  -> Binary size: $FILE_SIZE"
else
    fail "llama-server not found at $LLAMA_SERVER_BIN"
    echo "  -> Build with: make -C /root/llama.cpp/build -j4 llama-server"
fi

# ── Step 3: GGUF model file ───────────────────────────────────────────────────
step "STEP 3: GGUF Model File"
if [ -f "$MODEL_PATH" ]; then
    MODEL_SIZE=$(du -h "$MODEL_PATH" | cut -f1)
    ok "Model found: $MODEL_PATH ($MODEL_SIZE)"
else
    fail "Model not found at $MODEL_PATH"
    echo "  -> Download with:"
    echo "     curl -L -o $MODEL_PATH https://huggingface.co/bartowski/SmolLM2-135M-Instruct-GGUF/resolve/main/SmolLM2-135M-Instruct-Q4_K_M.gguf"
fi

# ── Step 4: llama-cli smoke test (offline, no HTTP) ──────────────────────────
step "STEP 4: llama-cli Direct Inference Smoke Test"
if [ -x "$LLAMA_CLI_BIN" ] && [ -f "$MODEL_PATH" ]; then
    echo "  -> Running: llama-cli -m $MODEL_PATH -p 'Say hello in 3 words' -n 20 --no-display-prompt"
    CLI_OUT=$("$LLAMA_CLI_BIN" \
        -m "$MODEL_PATH" \
        -p "Say hello in 3 words" \
        -n 20 \
        --no-display-prompt \
        --log-disable 2>/dev/null) || true
    echo "  -> CLI output: $CLI_OUT"
    if [ -n "$CLI_OUT" ]; then
        ok "llama-cli produced output: '$CLI_OUT'"
    else
        warn "llama-cli returned empty output (model may need warmup)"
    fi
else
    warn "Skipping llama-cli test (binary or model missing)"
fi

# ── Step 5: Start llama-server ────────────────────────────────────────────────
step "STEP 5: Start llama-server on :$LLAMA_PORT"
# Kill any lingering llama-server
pkill -9 -f llama-server 2>/dev/null || true
sleep 1

LLAMA_LOG="/tmp/llama_server_phase5.log"
"$LLAMA_SERVER_BIN" \
    --model "$MODEL_PATH" \
    --host "$LLAMA_HOST" \
    --port "$LLAMA_PORT" \
    --threads 2 \
    --ctx-size 512 \
    --n-predict 200 \
    --log-disable \
    > "$LLAMA_LOG" 2>&1 &
LLAMA_PID=$!
echo "  -> Started llama-server (PID: $LLAMA_PID). Waiting 8s for init..."
sleep 8

if kill -0 "$LLAMA_PID" 2>/dev/null; then
    ok "llama-server is running"
else
    fail "llama-server exited unexpectedly — check $LLAMA_LOG"
    cat "$LLAMA_LOG"
    exit 1
fi

# ── Step 6: llama-server /health ─────────────────────────────────────────────
step "STEP 6: llama-server HTTP /health"
HEALTH=$(curl -sf "http://$LLAMA_HOST:$LLAMA_PORT/health" 2>/dev/null || true)
echo "  -> /health response: $HEALTH"
if echo "$HEALTH" | grep -qi '"status"'; then
    ok "llama-server /health responded"
else
    fail "llama-server /health did not respond (got: $HEALTH)"
fi

# ── Step 7: llama-server /completion ─────────────────────────────────────────
step "STEP 7: llama-server /completion API"
COMPLETION=$(curl -sf -X POST "http://$LLAMA_HOST:$LLAMA_PORT/completion" \
    -H "Content-Type: application/json" \
    -d '{"prompt":"Say hello in exactly 3 words","n_predict":20,"temperature":0.1}' \
    2>/dev/null || true)
echo "  -> /completion response: $COMPLETION"
CONTENT=$(echo "$COMPLETION" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('content',''))" 2>/dev/null || true)
if [ -n "$CONTENT" ]; then
    ok "llama-server /completion returned: '$CONTENT'"
else
    fail "llama-server /completion returned empty content"
fi

# ── Step 8: Compile test programs ────────────────────────────────────────────
step "STEP 8: Compile Test Programs"
make -C test-programs clean > /dev/null 2>&1 || true
make -C test-programs
ok "test_syscall and test_syscall_stress compiled"

# ── Step 9: Start agent_daemon.py against llama-server ───────────────────────
step "STEP 9: Agent Daemon Startup (→ llama-server)"
DAEMON_LOG="/tmp/agent_daemon_phase5.log"
LLAMA_URL="http://$LLAMA_HOST:$LLAMA_PORT/completion" \
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

# ── Step 10: sys_agent_query (syscall 548) → in-guest LLM ───────────────────
step "STEP 10: sys_agent_query — Live In-Guest LLM (Native musl)"
echo "  -> Firing syscall 548 with daemon + llama-server online..."
SYSCALL_OUT=$(./test-programs/test_syscall \
    "Phase 5 validation. Process is opening a socket to 8.8.8.8:53 for DNS. Assess risk." \
    2>&1) || true
echo "$SYSCALL_OUT"
if echo "$SYSCALL_OUT" | grep -q "SUCCESS"; then
    ok "syscall 548 returned a response from in-guest LLM"
else
    fail "syscall 548 did not return SUCCESS"
fi

# ── Step 11: Concurrent stress test ──────────────────────────────────────────
step "STEP 11: Concurrent Stress Test (5 threads)"
echo "  -> This may take up to 90s (5 x 15s kernel timeout worst case)..."
STRESS_OUT=$(./test-programs/test_syscall_stress 2>&1) || true
echo "$STRESS_OUT"
if echo "$STRESS_OUT" | grep -q "ALL PASS"; then
    ok "Concurrent stress test passed"
else
    warn "Stress test did not report ALL PASS (check output above)"
fi

# ── Step 12: Structured log files ────────────────────────────────────────────
step "STEP 12: Structured Logging (/var/ai-agent/)"
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
    warn "/var/ai-agent/ not created yet"
fi

# ── Step 13: OpenRC scripts ───────────────────────────────────────────────────
step "STEP 13: OpenRC Service Scripts"
NEED_RC=0
for svc in llama-server ai-agent; do
    if [ -f "/etc/init.d/$svc" ]; then
        ok "OpenRC script /etc/init.d/$svc installed"
    else
        warn "OpenRC script /etc/init.d/$svc NOT installed"
        NEED_RC=1
    fi
done
if [ "$NEED_RC" -eq 1 ]; then
    echo "  -> Install with:"
    echo "     cp /root/os-ai-agent/agent-daemon/openrc/llama-server /etc/init.d/"
    echo "     cp /root/os-ai-agent/agent-daemon/openrc/ai-agent /etc/init.d/"
    echo "     chmod +x /etc/init.d/llama-server /etc/init.d/ai-agent"
    echo "     rc-update add llama-server default && rc-update add ai-agent default"
fi

# ── Step 14: Cleanup ──────────────────────────────────────────────────────────
step "STEP 14: Cleanup"
kill -TERM "$DAEMON_PID" 2>/dev/null || true
wait "$DAEMON_PID" 2>/dev/null || true
kill -TERM "$LLAMA_PID" 2>/dev/null || true
wait "$LLAMA_PID" 2>/dev/null || true
ok "Processes cleaned up"

echo ""
echo "=================================================="
echo "  Daemon log ($DAEMON_LOG):"
echo "--------------------------------------------------"
tail -30 "$DAEMON_LOG"
echo ""
echo "  llama-server log ($LLAMA_LOG):"
echo "--------------------------------------------------"
tail -10 "$LLAMA_LOG"
echo "=================================================="
echo ""
echo "  Phase 5 Verification Summary"
echo "  PASS: $PASS   FAIL: $FAIL   WARNINGS: $WARNINGS"
if [ "$FAIL" -eq 0 ]; then
    echo "  RESULT: PHASE 5 VALIDATED ✓ (native musl llama.cpp)"
else
    echo "  RESULT: $FAIL FAILURE(S) — see output above"
fi
echo "=================================================="

[ "$FAIL" -eq 0 ]
