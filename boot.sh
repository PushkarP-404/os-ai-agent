#!/bin/bash
# boot.sh — Launch AI-Agent OS Appliance v0.1 in QEMU (Linux/macOS)
#
# Usage:
#   ./boot.sh [image_path]
#
# Default SSH port forward: localhost:2222 -> guest:22
# Default credentials: root / aPushkar@12784

IMAGE="${1:-ai-agent-os-v0.1.qcow2}"
MEMORY="${MEMORY:-4G}"
CPUS="${CPUS:-4}"
SSH_PORT="${SSH_PORT:-2222}"

if [ ! -f "$IMAGE" ]; then
    if [ -f "../$IMAGE" ]; then
        IMAGE="../$IMAGE"
    elif [ -f "/qemu-alpine/$IMAGE" ]; then
        IMAGE="/qemu-alpine/$IMAGE"
    else
        echo "Error: Image file '$IMAGE' not found." >&2
        exit 1
    fi
fi

if ! command -v qemu-system-x86_64 >/dev/null 2>&1; then
    echo "Error: qemu-system-x86_64 not found in PATH." >&2
    echo "Install with: brew install qemu (macOS) or apt install qemu-system-x86 (Ubuntu/Debian)" >&2
    exit 1
fi

echo "=================================================="
echo "       Starting AI-Agent OS Appliance v0.1        "
echo "=================================================="
echo "  Image:    $IMAGE"
echo "  RAM:      $MEMORY"
echo "  vCPUs:    $CPUS"
echo "  SSH Port: $SSH_PORT (root / aPushkar@12784)"
echo "--------------------------------------------------"

exec qemu-system-x86_64 \
    -m "$MEMORY" \
    -smp "$CPUS" \
    -hda "$IMAGE" \
    -boot c \
    -net "user,hostfwd=tcp:127.0.0.1:${SSH_PORT}-:22" \
    -net nic \
    -serial stdio \
    -nographic
