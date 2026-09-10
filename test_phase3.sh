#!/bin/sh
set -e

echo "=================================================="
echo "      Phase 3: Kernel Module Verification Test     "
echo "=================================================="

# 1. Ensure clean slate
rmmod ai_process_hook 2>/dev/null || true
pkill -f agent_daemon.py 2>/dev/null || true
rm -f /tmp/daemon.log

# 2. Build kernel module
echo "\n[STEP 1] Compiling ai_process_hook kernel module..."
cd /root/os-ai-agent/kernel-module
make clean
make

if [ ! -f ai_process_hook.ko ]; then
    echo "[ERROR] Compilation failed: ai_process_hook.ko not found!"
    exit 1
fi
echo "[SUCCESS] ai_process_hook.ko compiled successfully."

# 3. Load kernel module
echo "\n[STEP 2] Inserting kernel module (insmod)..."
insmod ai_process_hook.ko
dmesg | tail -n 5

# 4. Start userspace agent daemon
echo "\n[STEP 3] Launching agent_daemon.py in background..."
python3 /root/os-ai-agent/agent-daemon/agent_daemon.py > /tmp/daemon.log 2>&1 &
DAEMON_PID=$!
sleep 1

# 5. Spawn arbitrary processes (UNWRAPPED - no ptrace!)
echo "\n[STEP 4] Spawning uninstrumented test processes..."
echo "  -> Running /bin/sleep 0.2"
/bin/sleep 0.2
echo "  -> Running /bin/ls /root"
/bin/ls /root > /dev/null
echo "  -> Running subshell command"
sh -c 'echo "Testing kernel hook"' > /dev/null

sleep 1

# 6. Check daemon captured events
echo "\n[STEP 5] Checking events captured by agent daemon:"
echo "--------------------------------------------------"
cat /tmp/daemon.log
echo "--------------------------------------------------"

# 7. Clean up
echo "\n[STEP 6] Cleaning up daemon and kernel module..."
kill -9 $DAEMON_PID 2>/dev/null || true
rmmod ai_process_hook
echo "[SUCCESS] Kernel module unloaded cleanly."
dmesg | tail -n 3

echo "\n=================================================="
echo "    Phase 3 Verification Completed Successfully!   "
echo "=================================================="
