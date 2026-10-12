#!/bin/sh
# packaging/build_all.sh
# Master build script — runs INSIDE the Alpine VM as root.
# Builds all AI-Agent OS APK packages and regenerates the signed repo index.
#
# Usage:
#   sh /root/os-ai-agent/packaging/build_all.sh [--skip-llama] [--only <pkg>]
set -e

REPO_DIR="/srv/apk-repo"
PACKAGER_USER="packager"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Packages in dependency order (kernel-modules first, then llama, then daemon on top)
ALL_PKGS="ai-agent-kernel-modules llama-server-musl ai-agent-daemon"

# Parse args
ONLY_PKG=""
SKIP_LLAMA=0
for arg in "$@"; do
    case "$arg" in
        --skip-llama) SKIP_LLAMA=1 ;;
        --only) shift; ONLY_PKG="$1" ;;
        --only=*) ONLY_PKG="${arg#--only=}" ;;
    esac
done

if [ -n "$ONLY_PKG" ]; then
    BUILD_PKGS="$ONLY_PKG"
elif [ "$SKIP_LLAMA" = "1" ]; then
    BUILD_PKGS="ai-agent-kernel-modules ai-agent-daemon"
else
    BUILD_PKGS="$ALL_PKGS"
fi

echo "================================================="
echo "  AI-Agent OS — APK Package Builder"
echo "  Packages: $BUILD_PKGS"
echo "================================================="

# Ensure permissions for packager user to read/write source files and repo
chmod 755 /root
chmod -R a+rwX /root/os-ai-agent
chmod -R a+rX /var/lib/ai-agent 2>/dev/null || true
mkdir -p "$REPO_DIR/x86_64"
chown -R "$PACKAGER_USER:abuild" "$REPO_DIR"

# ── Build each package ────────────────────────────────
for pkg in $BUILD_PKGS; do
    PKG_DIR="$SCRIPT_DIR/$pkg"
    if [ ! -f "$PKG_DIR/APKBUILD" ]; then
        echo "[WARN] No APKBUILD found for $pkg at $PKG_DIR, skipping."
        continue
    fi

    echo ""
    echo "─────────────────────────────────────────────────"
    echo "  Building: $pkg"
    echo "─────────────────────────────────────────────────"

    # abuild MUST NOT run as root — use the packager user
    # Copy APKBUILD into packager's home for abuild
    PKG_BUILD_DIR="/home/$PACKAGER_USER/build/$pkg"
    mkdir -p "$PKG_BUILD_DIR"
    cp "$PKG_DIR/APKBUILD" "$PKG_BUILD_DIR/APKBUILD"
    # Copy any supplementary files (pre-install scripts, patches, etc.)
    for f in "$PKG_DIR"/*; do
        [ "$f" != "$PKG_DIR/APKBUILD" ] && cp "$f" "$PKG_BUILD_DIR/" 2>/dev/null || true
    done
    chown -R "$PACKAGER_USER:abuild" "$PKG_BUILD_DIR"

    # Run abuild as packager user
    su "$PACKAGER_USER" -c "
        set -e
        cd '$PKG_BUILD_DIR'
        # Clean previous build artifacts
        abuild clean 2>/dev/null || true
        # Build: checksums → unpack → build → package → sign → copy
        # -r  = install missing makedepends automatically
        # -P  = output packages to REPO_DIR
        REPODEST='$REPO_DIR' abuild -r
    "

    echo "  ✓ $pkg built successfully"
done

# ── Handle ai-agent-model-lora separately (only if GGUF exists) ──────────────
LORA_GGUF="/var/lib/ai-agent/models/os_agent_lora.gguf"
if [ -f "$LORA_GGUF" ]; then
    echo ""
    echo "─────────────────────────────────────────────────"
    echo "  Building: ai-agent-model-lora (LoRA GGUF found)"
    echo "─────────────────────────────────────────────────"
    PKG_BUILD_DIR="/home/$PACKAGER_USER/build/ai-agent-model-lora"
    mkdir -p "$PKG_BUILD_DIR"
    cp "$SCRIPT_DIR/ai-agent-model-lora/APKBUILD" "$PKG_BUILD_DIR/APKBUILD"
    chown -R "$PACKAGER_USER:abuild" "$PKG_BUILD_DIR"
    su "$PACKAGER_USER" -c "
        set -e
        cd '$PKG_BUILD_DIR'
        abuild clean 2>/dev/null || true
        REPODEST='$REPO_DIR' abuild -r
    "
    echo "  ✓ ai-agent-model-lora built"
else
    echo ""
    echo "[INFO] $LORA_GGUF not found — skipping ai-agent-model-lora"
    echo "       Run fine-tuning pipeline and sync GGUF to VM, then re-run build_all.sh"
fi

# ── Regenerate signed APKINDEX ────────────────────────────────────────────────
echo ""
echo "─────────────────────────────────────────────────"
echo "  Regenerating signed APK index..."
echo "─────────────────────────────────────────────────"

# Consolidate all built packages into $REPO_DIR/x86_64/
mkdir -p "$REPO_DIR/x86_64"
find "$REPO_DIR" -name "*.apk" -not -path "$REPO_DIR/x86_64/*" -exec cp -f {} "$REPO_DIR/x86_64/" \; 2>/dev/null || true

APK_FILES=$(find "$REPO_DIR/x86_64" -name "*.apk" 2>/dev/null)
if [ -z "$APK_FILES" ]; then
    echo "[WARN] No .apk files found in $REPO_DIR/x86_64 — index will be empty."
fi

# apk index must run as root (needs access to all .apk files)
apk index \
    --rewrite-arch x86_64 \
    -o "$REPO_DIR/x86_64/APKINDEX.tar.gz" \
    "$REPO_DIR/x86_64/"*.apk 2>/dev/null || \
apk index \
    -o "$REPO_DIR/x86_64/APKINDEX.tar.gz" 2>/dev/null || true

# Sign the index as packager user
su "$PACKAGER_USER" -c "
    abuild-sign -k \"\$HOME/.abuild/\"*.rsa '$REPO_DIR/x86_64/APKINDEX.tar.gz'
"

# ── Start / restart the HTTP repo server ─────────────────────────────────────
echo ""
echo "  Starting/reloading apk-repo-server..."
if rc-service apk-repo-server status >/dev/null 2>&1; then
    rc-service apk-repo-server restart
else
    rc-service apk-repo-server start
fi

# ── Refresh local apk index ───────────────────────────────────────────────────
sleep 1   # give server a moment to start
apk update 2>/dev/null || true

echo ""
echo "================================================="
echo "  Build complete! Packages in $REPO_DIR/x86_64/"
echo ""
ls -lh "$REPO_DIR/x86_64/"*.apk 2>/dev/null || echo "  (no packages yet)"
echo ""
echo "  Install with:"
echo "    apk add ai-agent-daemon"
echo "    apk add llama-server-musl"
echo "    apk add ai-agent-kernel-modules"
echo "    apk add ai-agent-model-lora    # (if LoRA GGUF was available)"
echo "================================================="
