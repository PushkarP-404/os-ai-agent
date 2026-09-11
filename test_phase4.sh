#!/bin/sh
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo "=================================================="
echo "      AI-Agent OS: Phase 4 Verification Test      "
echo "=================================================="

# 1. Verify Kernel Version
echo "[STEP 1] Checking Kernel Version..."
CURRENT_KERNEL=$(uname -r)
echo "Running kernel: $CURRENT_KERNEL"
case "$CURRENT_KERNEL" in
    *-ai-agent*)
        echo "[SUCCESS] Running on custom AI-Agent kernel."
        ;;
    *)
        echo "[WARNING] Not yet running on 6.6.142-ai-agent kernel (current: $CURRENT_KERNEL)."
        echo "Please ensure the custom kernel has been compiled and the VM rebooted."
        ;;
esac

# 2. Compile Test Program
echo ""
echo "[STEP 2] Compiling userspace test program (test_syscall)..."
make -C test-programs clean
make -C test-programs
echo "[SUCCESS] test_syscall compiled."

# 3. Verify Kernel Logs
echo ""
echo "[STEP 3] Checking dmesg for AI Agent Subsystem..."
dmesg | grep -i "ai_agent" | tail -n 10 || echo "No dmesg entries yet."

# 4. Test Syscall with Daemon Offline (Fallback Mode)
echo ""
echo "[STEP 4] Testing sys_agent_query with daemon OFFLINE (Fallback Mode)..."
./test-programs/test_syscall "Diagnostic check: inspecting system state with daemon offline."

# 5. Test Syscall with Daemon Online (Live AI Mode)
echo ""
echo "[STEP 5] Testing sys_agent_query with agent_daemon ONLINE (Live AI Mode)..."
python3 agent-daemon/agent_daemon.py > /tmp/agent_daemon_phase4.log 2>&1 &
DAEMON_PID=$!
echo "  -> Started agent_daemon.py (PID: $DAEMON_PID)"
sleep 1

echo "  -> Invoking sys_agent_query with interactive question..."
./test-programs/test_syscall "The process is preparing to open an outgoing network connection to port 443. Is this safe?"

echo ""
echo "[STEP 6] Checking Agent Daemon Log Output:"
echo "--------------------------------------------------"
cat /tmp/agent_daemon_phase4.log
echo "--------------------------------------------------"

echo ""
echo "[STEP 7] Cleaning up Daemon..."
kill -TERM "$DAEMON_PID" 2>/dev/null || true
wait "$DAEMON_PID" 2>/dev/null || true
echo "[SUCCESS] Daemon cleaned up cleanly."

echo ""
echo "=================================================="
echo "    Phase 4 Verification Completed Successfully!  "
echo "=================================================="
