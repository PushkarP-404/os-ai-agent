#!/bin/sh
set -e

echo "=== 1. Tracing /bin/ls with ptrace monitor ==="
cd /root/os-ai-agent/ptrace-monitor
./monitor /bin/ls > /tmp/trace.log 2>&1 || true
cat /tmp/trace.log

echo ""
echo "=== 2. Analyzing captured syscalls with Ollama AI ==="
python3 /root/os-ai-agent/ai-agent/syscall_analyzer.py "$(cat /tmp/trace.log)"
