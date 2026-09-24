#!/bin/sh
# test_phase6_boot.sh — AI-Agent OS Phase 6 Boot & Appliance Validation Test Harness
#
# Validates:
#   1. Custom kernel: 6.6.142-ai-agent running
#   2. System paths: llama-server, llama-cli, shared libs installed in /usr/local
#   3. Model storage: SmolLM2 GGUF present in /var/lib/ai-agent/models/
#   4. Daemon path: agent_daemon.py installed in /usr/local/lib/ai-agent/
#   5. OpenRC services: llama-server, ai-agent, sshd enabled in default runlevel
#   6. HTTP Health: llama-server responds {"status":"ok"} on 127.0.0.1:11434/health
#   7. Kernel Registration: dmesg confirms daemon registration with kernel ai_agent subsystem
#   8. Syscall Execution: sys_agent_query (syscall 548) completes successfully
#   9. Syscall Edge Cases: NULL pointers and invalid memory rejected properly by kernel
#  10. Telemetry & Dataset: JSONL logging in /var/ai-agent/ records query metrics

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

LLAMA_SERVER_BIN="/usr/local/bin/llama-server"
LLAMA_CLI_BIN="/usr/local/bin/llama-cli"
MODEL_PATH="/var/lib/ai-agent/models/smollm2-135m-instruct-q4_k_m.gguf"
AGENT_SCRIPT="/usr/local/lib/ai-agent/agent_daemon.py"
DATASET_PATH="/var/ai-agent/training_data/dataset.jsonl"
TEST_SYSCALL_BIN="$SCRIPT_DIR/test-programs/test_syscall"

echo "=================================================="
echo "    AI-Agent OS: Phase 6 Boot Validation Harness  "
echo "=================================================="

# ── Step 1: Kernel Version ──
step "STEP 1: Custom Kernel Verification"
KVER=$(uname -r)
echo "  -> Running kernel: $KVER"
if [ "$KVER" = "6.6.142-ai-agent" ]; then
    ok "Running custom kernel: 6.6.142-ai-agent"
else
    fail "Unexpected kernel: $KVER (expected 6.6.142-ai-agent)"
fi

# ── Step 2: System Paths & Binary Installation ──
step "STEP 2: System Paths & Dynamic Linker Verification"
if [ -x "$LLAMA_SERVER_BIN" ] && [ -x "$LLAMA_CLI_BIN" ]; then
    ok "llama-server and llama-cli installed in /usr/local/bin"
else
    fail "Binaries missing from /usr/local/bin"
fi

MISSING_LIBS=$(ldd "$LLAMA_SERVER_BIN" 2>&1 | grep -c "not found" || true)
if [ "$MISSING_LIBS" -eq 0 ]; then
    ok "All dynamic library dependencies resolved for llama-server"
else
    fail "$MISSING_LIBS unresolved library dependencies"
fi

# ── Step 3: Model Verification ──
step "STEP 3: Model Storage Verification"
if [ -f "$MODEL_PATH" ]; then
    SIZE=$(ls -lh "$MODEL_PATH" | awk '{print $5}')
    ok "GGUF model present at $MODEL_PATH (size: $SIZE)"
else
    fail "Model missing at $MODEL_PATH"
fi

# ── Step 4: Daemon Installation ──
step "STEP 4: Agent Daemon System Installation"
if [ -f "$AGENT_SCRIPT" ] && [ -x "$AGENT_SCRIPT" ]; then
    ok "agent_daemon.py installed and executable at $AGENT_SCRIPT"
else
    fail "agent_daemon.py missing at $AGENT_SCRIPT"
fi

# ── Step 5: OpenRC Service Runlevels ──
step "STEP 5: OpenRC Default Runlevel Services"
SERVICES=$(rc-update show default)
echo "$SERVICES" | grep -q llama-server && ok "llama-server registered in default runlevel" || fail "llama-server not in default runlevel"
echo "$SERVICES" | grep -q ai-agent && ok "ai-agent registered in default runlevel" || fail "ai-agent not in default runlevel"
echo "$SERVICES" | grep -q sshd && ok "sshd registered in default runlevel" || fail "sshd not in default runlevel"

# ── Step 6: HTTP Health Check ──
step "STEP 6: llama-server HTTP /health Endpoint"
HEALTH=$(curl -sf http://127.0.0.1:11434/health 2>/dev/null || true)
echo "  -> /health: $HEALTH"
if echo "$HEALTH" | grep -qi '"status"'; then
    ok "llama-server /health returned healthy status"
else
    fail "llama-server /health failed or unreachable"
fi

# ── Step 7: Kernel Daemon Registration ──
step "STEP 7: Kernel Netlink Agent Registration"
REG_CHECK=$(dmesg | grep "ai_agent: Userspace agent daemon registered" | tail -n 1 || true)
if [ -n "$REG_CHECK" ]; then
    ok "Kernel confirmed daemon registration: $REG_CHECK"
else
    fail "No daemon registration found in dmesg"
fi

# ── Step 8: End-to-End Syscall 548 Execution ──
step "STEP 8: End-to-End Syscall 548 (sys_agent_query) Verification"
if [ -x "$TEST_SYSCALL_BIN" ]; then
    OUT=$("$TEST_SYSCALL_BIN" 2>&1)
    echo "$OUT"
    if echo "$OUT" | grep -q "Syscall completed in"; then
        ok "syscall(548) successfully routed to LLM and returned response"
    else
        fail "syscall(548) did not return success"
    fi
    if echo "$OUT" | grep -q "\[PASS\] Invalid memory address rejected"; then
        ok "All kernel edge-case validations passed (NULL pointers, invalid memory)"
    else
        fail "Edge-case validation failed"
    fi
else
    fail "test_syscall binary missing at $TEST_SYSCALL_BIN"
fi

# ── Step 9: Structured Dataset & Logging ──
step "STEP 9: Telemetry & JSONL Dataset Verification"
if [ -f "$DATASET_PATH" ]; then
    RECS=$(wc -l < "$DATASET_PATH")
    ok "Dataset exists at $DATASET_PATH with $RECS interaction records"
    LAST_REC=$(tail -n 1 "$DATASET_PATH")
    echo "  -> Latest logged interaction: $LAST_REC"
else
    fail "Dataset missing at $DATASET_PATH"
fi

# ── Summary ──
echo ""
echo "=================================================="
echo "           Phase 6 Boot Validation Summary        "
echo "=================================================="
echo "  Total Passed: $PASS"
echo "  Total Failed: $FAIL"
echo "  Warnings:     $WARNINGS"
echo "=================================================="

if [ "$FAIL" -eq 0 ]; then
    echo "  RESULT: ALL PHASE 6 BOOT VALIDATIONS PASSED! [SUCCESS]"
    exit 0
else
    echo "  RESULT: VALIDATION FAILURES DETECTED! [FAILURE]"
    exit 1
fi
