#!/bin/bash
# boot.sh â€” Launch Elenchus Appliance v0.1 in QEMU (Linux/macOS)
#
# Usage:
#   ./boot.sh [image_path]
#
# Default SSH port forward: localhost:2222 -> guest:22
# Default credentials: root / password

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
echo "       Starting Elenchus Appliance v0.1        "
echo "=================================================="
echo "  Image:    $IMAGE"
echo "  RAM:      $MEMORY"
echo "  vCPUs:    $CPUS"
echo "  SSH Port: $SSH_PORT (root / password)"
echo "--------------------------------------------------"

GUI_ARGS=()
if [ "${GUI:-0}" = "1" ] || [ "$1" = "--gui" ] || [ "$2" = "--gui" ]; then
    GUI_ARGS=(
        -device virtio-tablet-pci
        -usb
        -device usb-tablet
        -vga virtio
        -display sdl
    )
else
    GUI_ARGS=(-display none)
fi

ACCEL_ARGS=()
if [ -e /dev/kvm ] && [ -w /dev/kvm ]; then
    ACCEL_ARGS=(-enable-kvm -cpu host)
elif [ "$(uname)" = "Darwin" ]; then
    ACCEL_ARGS=(-accel hvf -cpu host)
else
    ACCEL_ARGS=(-accel tcg,thread=multi)
fi

BIOS_ARGS=()
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "$SCRIPT_DIR/bios/elenchus-bios.bin" ]; then
    BIOS_ARGS=(-bios "$SCRIPT_DIR/bios/elenchus-bios.bin")
elif [ -f "bios/elenchus-bios.bin" ]; then
    BIOS_ARGS=(-bios "bios/elenchus-bios.bin")
elif [ -f "../elenchus-bios.bin" ]; then
    BIOS_ARGS=(-bios "../elenchus-bios.bin")
fi

exec qemu-system-x86_64 \
    -m "$MEMORY" \
    -smp "$CPUS" \
    -hda "$IMAGE" \
    -boot c \
    -netdev user,id=n1,hostfwd=tcp:127.0.0.1:${SSH_PORT}-:22 \
    -device e1000,netdev=n1 \
    "${ACCEL_ARGS[@]}" \
    "${BIOS_ARGS[@]}" \
    "${GUI_ARGS[@]}"
