#!/bin/sh
set -e

KERNEL_DIR="/usr/src/linux-6.6.142"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

echo "=================================================="
echo "      AI-Agent OS: Kernel Build & Install         "
echo "=================================================="

if [ ! -d "$KERNEL_DIR" ]; then
    echo "[ERROR] Kernel source directory $KERNEL_DIR not found!"
    exit 1
fi

echo "[STEP 1] Injecting AI Agent Kernel Source Files..."

# 1. Install UAPI header
cp "$SCRIPT_DIR/include/uapi/linux/ai_agent.h" "$KERNEL_DIR/include/uapi/linux/ai_agent.h"
echo "  -> Copied ai_agent.h to include/uapi/linux/"

# 2. Install kernel C source
cp "$SCRIPT_DIR/kernel/ai_agent.c" "$KERNEL_DIR/kernel/ai_agent.c"
echo "  -> Copied ai_agent.c to kernel/"

# 3. Patch arch/x86/entry/syscalls/syscall_64.tbl
if ! grep -q "agent_query" "$KERNEL_DIR/arch/x86/entry/syscalls/syscall_64.tbl"; then
    echo "548	common	agent_query		sys_agent_query" >> "$KERNEL_DIR/arch/x86/entry/syscalls/syscall_64.tbl"
    echo "  -> Added syscall 548 (agent_query) to syscall_64.tbl"
else
    echo "  -> syscall 548 already in syscall_64.tbl"
fi

# 4. Patch include/linux/syscalls.h
if ! grep -q "sys_agent_query" "$KERNEL_DIR/include/linux/syscalls.h"; then
    sed -i '/#endif/i \
asmlinkage long sys_agent_query(pid_t target_pid, const char __user *query, size_t query_len, \\\
				char __user *response, size_t resp_len);' "$KERNEL_DIR/include/linux/syscalls.h"
    echo "  -> Added sys_agent_query declaration to include/linux/syscalls.h"
else
    echo "  -> sys_agent_query already declared in include/linux/syscalls.h"
fi

# 5. Patch kernel/Makefile
if ! grep -q "ai_agent.o" "$KERNEL_DIR/kernel/Makefile"; then
    echo "obj-y += ai_agent.o" >> "$KERNEL_DIR/kernel/Makefile"
    echo "  -> Added obj-y += ai_agent.o to kernel/Makefile"
else
    echo "  -> ai_agent.o already in kernel/Makefile"
fi

echo "[STEP 2] Configuring Kernel..."
cd "$KERNEL_DIR"

if [ ! -f .config ]; then
    echo "  -> Seeding .config from running Alpine kernel (/boot/config-$(uname -r))..."
    cp "/boot/config-$(uname -r)" .config
fi

# Apply custom configuration tweaks
./scripts/config --set-str CONFIG_LOCALVERSION "-ai-agent"
./scripts/config --disable CONFIG_DEBUG_INFO
./scripts/config --disable CONFIG_DEBUG_INFO_DWARF_TOOLCHAIN_DEFAULT
./scripts/config --disable CONFIG_DEBUG_INFO_DWARF5
./scripts/config --enable CONFIG_DEBUG_INFO_NONE

# Run olddefconfig to resolve dependencies
make olddefconfig
echo "  -> Kernel configuration validated."

echo "[STEP 3] Compiling bzImage and modules (using $(nproc) vCPUs)..."
make -j"$(nproc)" bzImage modules

echo "[STEP 4] Installing Kernel Modules..."
make modules_install

echo "[STEP 5] Installing Kernel bzImage to /boot..."
cp arch/x86/boot/bzImage /boot/vmlinuz-ai-agent
cp System.map /boot/System.map-ai-agent
echo "  -> Installed /boot/vmlinuz-ai-agent"

echo "[STEP 6] Generating Initramfs using mkinitfs..."
mkinitfs -o /boot/initramfs-ai-agent 6.6.142-ai-agent
echo "  -> Generated /boot/initramfs-ai-agent"

echo "[STEP 7] Updating extlinux.conf bootloader..."
UUID=$(awk '$2 == "/" {print $1}' /etc/fstab | sed 's/UUID=//')
if [ -z "$UUID" ]; then
    UUID=$(findmnt -n -o UUID /)
fi

echo "Root filesystem UUID: $UUID"

cat << EOF > /boot/extlinux.conf
# AI-Agent OS Bootloader Configuration
DEFAULT menu.c32
PROMPT 0
MENU TITLE AI-Agent OS Boot Menu
TIMEOUT 20

LABEL ai-os
  MENU DEFAULT
  MENU LABEL Linux 6.6.142-ai-agent (AI-Agent OS Kernel)
  LINUX vmlinuz-ai-agent
  INITRD initramfs-ai-agent
  APPEND root=UUID=${UUID} modules=sd-mod,usb-storage,ext4 quiet rootfstype=ext4

LABEL lts
  MENU LABEL Linux lts (Fallback Stock Kernel)
  LINUX vmlinuz-lts
  INITRD initramfs-lts
  APPEND root=UUID=${UUID} modules=sd-mod,usb-storage,ext4 quiet rootfstype=ext4

MENU SEPARATOR
EOF

echo "[SUCCESS] Kernel compilation and installation complete!"
echo "Reboot the VM to boot into 6.6.142-ai-agent."
