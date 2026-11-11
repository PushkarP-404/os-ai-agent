#!/bin/bash
# packaging/build_iso.sh
# Builds a hybrid BIOS + UEFI bootable live ISO (elenchus-os-v0.1-x86_64.iso)
# from the current VM appliance disk (ai-agent-os-v0.1.qcow2).
#
# Runs as root on Linux or inside WSL2 Ubuntu.
# Required tools: qemu-img, losetup, e2fsck, mksquashfs, cpio, gzip, grub-mkrescue, xorriso
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
WORKSPACE_ROOT="$(cd "$REPO_ROOT/.." && pwd)"

INPUT_QCOW2="${1:-$WORKSPACE_ROOT/ai-agent-os-v0.1.qcow2}"
OUTPUT_ISO="${2:-$WORKSPACE_ROOT/elenchus-os-v0.1-x86_64.iso}"
WORK_DIR="${WORK_DIR:-/root/iso-build}"
MNT_DIR="$WORK_DIR/mnt"
INITRD_DIR="$WORK_DIR/initrd"
ISO_ROOT="$WORK_DIR/iso-root"
RAW_DISK="$WORK_DIR/disk.raw"

if [ "$(id -u)" -ne 0 ]; then
    echo "Error: build_iso.sh must run as root (for losetup/mount/mksquashfs ownership)." >&2
    exit 1
fi

if [ ! -f "$INPUT_QCOW2" ]; then
    echo "Error: Input QCOW2 image not found at: $INPUT_QCOW2" >&2
    exit 1
fi

for tool in qemu-img losetup e2fsck mksquashfs cpio gzip grub-mkrescue xorriso; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        echo "Error: Required tool '$tool' not found in PATH." >&2
        exit 1
    fi
done

echo "=================================================="
echo "  Elenchus OS — Bootable Live ISO Pipeline"
echo "=================================================="
echo "  Input QCOW2: $INPUT_QCOW2"
echo "  Output ISO:  $OUTPUT_ISO"
echo "  Work Dir:    $WORK_DIR"
echo "--------------------------------------------------"

cleanup() {
    set +e
    umount -R "$MNT_DIR" 2>/dev/null || true
    if [ -n "${LOOP_DEV:-}" ]; then
        losetup -d "$LOOP_DEV" 2>/dev/null || true
    fi
}
trap cleanup EXIT

umount -R "$MNT_DIR" 2>/dev/null || true
rm -rf "$INITRD_DIR" "$ISO_ROOT"
mkdir -p "$MNT_DIR" "$INITRD_DIR" "$ISO_ROOT/boot/grub" "$ISO_ROOT/live"

# 1. Convert QCOW2 -> temporary raw disk (or reuse if identical timestamp/size)
NEED_CONVERT=1
STAMP_FILE="$WORK_DIR/disk.stamp"
SRC_STAT="$(stat -c '%Y:%s' "$INPUT_QCOW2")"
if [ -f "$RAW_DISK" ] && [ -f "$STAMP_FILE" ] && [ "$(cat "$STAMP_FILE")" = "$SRC_STAT" ]; then
    echo "[1/6] Reusing up-to-date raw disk image in $RAW_DISK"
    NEED_CONVERT=0
fi

if [ "$NEED_CONVERT" = "1" ]; then
    echo "[1/6] Converting QCOW2 -> temporary raw image..."
    rm -f "$RAW_DISK" "$STAMP_FILE"
    qemu-img convert -f qcow2 -O raw "$INPUT_QCOW2" "$RAW_DISK"
    echo "$SRC_STAT" > "$STAMP_FILE"
fi

# 2. Replay ext4 journal cleanly on the temporary raw copy and mount partitions
echo "[2/6] Attaching disk partitions & verifying filesystems..."
LOOP_DEV="$(losetup -fP --show "$RAW_DISK")"
e2fsck -p -f "${LOOP_DEV}p1" >/dev/null 2>&1 || true
e2fsck -p -f "${LOOP_DEV}p3" >/dev/null 2>&1 || true

mount -o ro "${LOOP_DEV}p3" "$MNT_DIR"
mount -o ro "${LOOP_DEV}p1" "$MNT_DIR/boot"

KERNEL_VER="6.6.142-ai-agent"
if [ ! -f "$MNT_DIR/boot/vmlinuz-ai-agent" ] || [ ! -f "$MNT_DIR/boot/initramfs-ai-agent" ]; then
    echo "Error: Custom kernel (/boot/vmlinuz-ai-agent or initramfs-ai-agent) missing in disk image!" >&2
    exit 1
fi
if [ ! -f "$MNT_DIR/usr/local/bin/elenchus-dashboard" ]; then
    echo "Error: /usr/local/bin/elenchus-dashboard missing in disk image!" >&2
    exit 1
fi

# 3. Build Live Initramfs (with squashfs, isofs, overlayfs, CD/USB/NVMe drivers + elenchus_live hook)
echo "[3/6] Building Live Initramfs (initramfs-elenchus-live)..."
(
    cd "$INITRD_DIR"
    zcat "$MNT_DIR/boot/initramfs-ai-agent" | cpio -idmu --quiet

    MOD_SRC="$MNT_DIR/lib/modules/$KERNEL_VER"
    MOD_DST="$INITRD_DIR/lib/modules/$KERNEL_VER"
    mkdir -p "$MOD_DST/kernel"

    # Copy module metadata so modprobe resolves dependencies cleanly
    cp -f "$MOD_SRC"/modules.* "$MOD_DST/" 2>/dev/null || true

    # Copy storage, filesystem, USB, CDROM, and input modules needed on bare metal & VMs
    for subdir in \
        kernel/fs/squashfs \
        kernel/fs/isofs \
        kernel/fs/overlayfs \
        kernel/fs/fat \
        kernel/fs/nls \
        kernel/drivers/block \
        kernel/drivers/cdrom \
        kernel/drivers/scsi \
        kernel/drivers/ata \
        kernel/drivers/usb \
        kernel/drivers/nvme \
        kernel/drivers/virtio \
        kernel/drivers/hid \
        kernel/drivers/input \
        kernel/block \
        kernel/lib \
        kernel/crypto
    do
        if [ -d "$MOD_SRC/$subdir" ]; then
            mkdir -p "$MOD_DST/$(dirname "$subdir")"
            cp -a "$MOD_SRC/$subdir" "$MOD_DST/$(dirname "$subdir")/"
        fi
    done

    # Inject elenchus_live boot option and squashfs+overlayfs live mount logic into /init
    python3 - << 'PYEOF'
from pathlib import Path

init_file = Path("init")
text = init_file.read_text(encoding="utf-8")

if "elenchus_live" not in text:
    text = text.replace(
        "blacklist overlaytmpfs",
        "elenchus_live blacklist overlaytmpfs",
        1,
    )

hook_marker = "# check if root=... was set\nif [ -n \"$KOPT_root\" ]; then"
live_block = """# Boot Elenchus Live ISO (squashfs + writable tmpfs overlay)
if [ "$KOPT_elenchus_live" = "yes" ]; then
\tebegin "Booting Elenchus Live OS (squashfs + overlayfs)"
\tmodprobe -a cdrom sr_mod sd_mod scsi_mod loop isofs squashfs overlay \\
\t\tata_piix ahci libahci usb-storage uas xhci-pci xhci-hcd ehci-pci ehci-hcd \\
\t\tvirtio_pci virtio_blk virtio_scsi nvme vfat nls_cp437 nls_utf8 2>/dev/null || true
\tmdev -s 2>/dev/null || true
\tmkdir -p /media/cdrom /media/root-ro /media/root-rw $sysroot/media/cdrom $sysroot/media/root-ro $sysroot/media/root-rw

\tFOUND_DEV=""
\tfor attempt in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
\t\tmdev -s 2>/dev/null || true
\t\tfor dev in /dev/sr* /dev/cdrom /dev/sd*[0-9]* /dev/sd* /dev/vd* /dev/nvme*n*p*; do
\t\t\t[ -b "$dev" ] || continue
\t\t\tif mount -o ro "$dev" /media/cdrom 2>/dev/null || mount -t iso9660 -o ro "$dev" /media/cdrom 2>/dev/null; then
\t\t\t\tif [ -f /media/cdrom/live/filesystem.squashfs ] && [ -f /media/cdrom/.elenchus-iso ]; then
\t\t\t\t\tFOUND_DEV="$dev"
\t\t\t\t\tbreak 2
\t\t\t\tfi
\t\t\t\tumount /media/cdrom 2>/dev/null || true
\t\t\tfi
\t\tdone
\t\tsleep 1
\tdone

\tif [ -z "$FOUND_DEV" ]; then
\t\techo "ERROR: Could not locate /live/filesystem.squashfs on CD-ROM or USB media!"
\t\techo "Detected block devices:"
\t\tls -l /dev/sr* /dev/sd* /dev/vd* /dev/nvme* 2>/dev/null || true
\t\texec /bin/busybox sh
\tfi

\t[ -e /dev/loop0 ] || mknod /dev/loop0 b 7 0 2>/dev/null || true
\tmount -t squashfs -o loop,ro /media/cdrom/live/filesystem.squashfs /media/root-ro
\tmount -t tmpfs -o mode=0755,size=50%,rw root-tmpfs /media/root-rw
\tmkdir -p /media/root-rw/work /media/root-rw/root
\tmount -t overlay -o lowerdir=/media/root-ro,upperdir=/media/root-rw/root,workdir=/media/root-rw/work overlayfs "$sysroot"

\t# Override disk-specific fstab UUIDs so OpenRC localmount/fsck never stalls in Live mode
\tcat << 'FSTAB' > "$sysroot/etc/fstab"
tmpfs\t/tmp\ttmpfs\tnosuid,nodev\t0 0
/dev/cdrom\t/media/cdrom\tiso9660\tnoauto,ro\t0 0
FSTAB
\teend 0

\tcat "$ROOT"/proc/mounts 2>/dev/null | while read DEV DIR TYPE OPTS ; do
\t\tif [ "$DIR" != "/" -a "$DIR" != "$sysroot" -a -d "$DIR" ]; then
\t\t\tmkdir -p "$sysroot/$DIR"
\t\t\t$MOCK mount -o move "$DIR" "$sysroot/$DIR" 2>/dev/null || true
\t\tfi
\tdone
\t$MOCK sync
\texec switch_root $switch_root_opts "$sysroot" $chart_init "$KOPT_init" $KOPT_init_args
\techo "initramfs emergency recovery shell launched"
\texec /bin/busybox sh
fi

""" + hook_marker

assert hook_marker in text, "Expected root mount marker not found in initramfs /init"
text = text.replace(hook_marker, live_block, 1)
init_file.write_text(text, encoding="utf-8")
PYEOF

    chmod +x init
    find . -print0 | cpio --null -o --format=newc --quiet | gzip -1 > "$ISO_ROOT/boot/initramfs-elenchus-live"
)

# 4. Copy kernel and prepare GRUB configuration (BIOS + UEFI)
echo "[4/6] Staging kernel & GRUB bootloader..."
cp "$MNT_DIR/boot/vmlinuz-ai-agent" "$ISO_ROOT/boot/vmlinuz-ai-agent"
touch "$ISO_ROOT/.elenchus-iso"

cat << 'EOF' > "$ISO_ROOT/boot/grub/grub.cfg"
set timeout=3
set default=0

menuentry "Elenchus OS v0.1 (Live Desktop - Kernel 6.6.142-ai-agent + Rust/Slint UI)" {
    linux /boot/vmlinuz-ai-agent elenchus_live=yes modules=cdrom,sr_mod,sd_mod,loop,isofs,squashfs,overlay,ata_piix,ahci,usb-storage,xhci-pci,ehci-pci,nvme,virtio_pci,virtio_blk quiet elevator=none transparent_hugepage=madvise mitigations=off console=tty0
    initrd /boot/initramfs-elenchus-live
}

menuentry "Elenchus OS v0.1 (Verbose Console + Serial Debug)" {
    linux /boot/vmlinuz-ai-agent elenchus_live=yes modules=cdrom,sr_mod,sd_mod,loop,isofs,squashfs,overlay,ata_piix,ahci,usb-storage,xhci-pci,ehci-pci,nvme,virtio_pci,virtio_blk console=ttyS0 console=tty0 debug_init=1
    initrd /boot/initramfs-elenchus-live
}
EOF

# 5. Compress root filesystem into filesystem.squashfs
# Excludes ephemeral runtime PIDs/locks/sockets, legacy unused /usr/lib/ollama (2.1 GB),
# and heavy kernel build intermediate tree /usr/src/linux-6.6.142 (3.9 GB) to keep ISO ~1.3 GB.
echo "[5/6] Compressing root filesystem to filesystem.squashfs (zstd)..."
COMP_OPT="-comp zstd -Xcompression-level 6"
if ! mksquashfs -help 2>&1 | grep -q zstd; then
    COMP_OPT="-comp xz"
fi

mksquashfs "$MNT_DIR" "$ISO_ROOT/live/filesystem.squashfs" \
    $COMP_OPT \
    -b 262144 \
    -noappend \
    -wildcards \
    -e \
        "lost+found" \
        "boot/lost+found" \
        "proc/*" \
        "sys/*" \
        "dev/*" \
        "run/*" \
        "tmp/*" \
        "tmp/.[!.]*" \
        "var/run/*" \
        "var/tmp/*" \
        "var/ai-agent/*.sock" \
        "var/cache/apk/*" \
        "var/log/*.log" \
        "usr/src/linux-6.6.142" \
        "usr/src/linux-6.6.142.tar.xz" \
        "usr/lib/ollama"

# 6. Build hybrid BIOS + UEFI ISO via grub-mkrescue / xorriso
echo "[6/6] Assembling bootable ISO image..."
TMP_ISO="$WORK_DIR/elenchus-os-out.iso"
rm -f "$TMP_ISO"
grub-mkrescue -o "$TMP_ISO" "$ISO_ROOT" -- -volid "ELENCHUS_OS" 2>&1

mkdir -p "$(dirname "$OUTPUT_ISO")"
mv -f "$TMP_ISO" "$OUTPUT_ISO"

echo ""
echo "=================================================="
echo "  ISO build complete!"
echo "  Path: $OUTPUT_ISO"
ls -lh "$OUTPUT_ISO"
echo "=================================================="
