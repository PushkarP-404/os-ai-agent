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

# Packages in dependency order (kernel-modules first, then llama + dashboard, then daemon on top)
ALL_PKGS="ai-agent-kernel-modules llama-server-musl ai-agent-dashboard ai-agent-daemon"

# Parse args
ONLY_PKG=""
SKIP_LLAMA=0
SKIP_LORA=0
while [ "$#" -gt 0 ]; do
    case "$1" in
        --skip-llama) SKIP_LLAMA=1 ;;
        --skip-lora)  SKIP_LORA=1 ;;
        --only)
            shift
            ONLY_PKG="${1:-}"
            ;;
        --only=*) ONLY_PKG="${1#--only=}" ;;
    esac
    shift || true
done

if [ -n "$ONLY_PKG" ]; then
    BUILD_PKGS="$ONLY_PKG"
elif [ "$SKIP_LLAMA" = "1" ]; then
    BUILD_PKGS="ai-agent-kernel-modules ai-agent-dashboard ai-agent-daemon"
else
    BUILD_PKGS="$ALL_PKGS"
fi

regenerate_index() {
    mkdir -p "$REPO_DIR/x86_64"
    # Clean accidental nested arch symlink if present
    [ -L "$REPO_DIR/x86_64/x86_64" ] && rm -f "$REPO_DIR/x86_64/x86_64" 2>/dev/null || true
    find "$REPO_DIR" -maxdepth 3 -name "*.apk" -not -path "$REPO_DIR/x86_64/*" -exec cp -f {} "$REPO_DIR/x86_64/" \; 2>/dev/null || true

    APK_FILES=$(find "$REPO_DIR/x86_64" -maxdepth 1 -name "*.apk" 2>/dev/null)
    if [ -z "$APK_FILES" ]; then
        echo "[WARN] No .apk files found in $REPO_DIR/x86_64 — index will be empty."
        return 0
    fi

    apk index \
        --rewrite-arch x86_64 \
        -o "$REPO_DIR/x86_64/APKINDEX.tar.gz" \
        "$REPO_DIR/x86_64/"*.apk 2>/dev/null || \
    apk index \
        -o "$REPO_DIR/x86_64/APKINDEX.tar.gz" 2>/dev/null || true

    su "$PACKAGER_USER" -c "
        abuild-sign -k \"\$HOME/.abuild/\"*.rsa '$REPO_DIR/x86_64/APKINDEX.tar.gz'
    "
}

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
    # Copy APKBUILD + hook scripts into packager's home with Unix LF endings
    PKG_BUILD_DIR="/home/$PACKAGER_USER/build/$pkg"
    mkdir -p "$PKG_BUILD_DIR"
    for f in "$PKG_DIR"/*; do
        [ -f "$f" ] || continue
        base="$(basename "$f")"
        tr -d '\r' < "$f" > "$PKG_BUILD_DIR/$base"
        case "$base" in
            *.pre-*|*.post-*|*.sh) chmod 755 "$PKG_BUILD_DIR/$base" ;;
        esac
    done
    chown -R "$PACKAGER_USER:abuild" "$PKG_BUILD_DIR"

    # Run abuild as packager user (-d skips sudo apk builddeps check since setup_repo.sh installs toolchain as root)
    su "$PACKAGER_USER" -c "
        set -e
        cd '$PKG_BUILD_DIR'
        abuild clean 2>/dev/null || true
        REPODEST='$REPO_DIR' abuild -d -r
    "

    # Refresh index immediately so downstream packages (e.g. ai-agent-daemon -> ai-agent-dashboard) resolve intra-repo deps
    regenerate_index
    apk update 2>/dev/null || true

    echo "  ✓ $pkg built successfully"
done

# ── Handle ai-agent-model-lora separately (only if GGUF exists and not already built) ──
MODELS_DIR="/var/lib/ai-agent/models"
if [ -z "$ONLY_PKG" ] && [ "$SKIP_LORA" = "0" ] && { [ -f "$MODELS_DIR/os_planner_lora.gguf" ] || [ -f "$MODELS_DIR/os_agent_lora.gguf" ]; }; then
    echo ""
    echo "─────────────────────────────────────────────────"
    echo "  Building: ai-agent-model-lora (GGUF model found)"
    echo "─────────────────────────────────────────────────"
    PKG_BUILD_DIR="/home/$PACKAGER_USER/build/ai-agent-model-lora"
    mkdir -p "$PKG_BUILD_DIR"
    cp "$SCRIPT_DIR/ai-agent-model-lora/APKBUILD" "$PKG_BUILD_DIR/APKBUILD"
    chown -R "$PACKAGER_USER:abuild" "$PKG_BUILD_DIR"
    su "$PACKAGER_USER" -c "
        set -e
        cd '$PKG_BUILD_DIR'
        abuild clean 2>/dev/null || true
        REPODEST='$REPO_DIR' abuild -d -r
    "
    echo "  ✓ ai-agent-model-lora built"
else
    echo ""
    echo "[INFO] GGUF models not found in $MODELS_DIR — skipping ai-agent-model-lora"
    echo "       Run fine-tuning pipeline and sync GGUF to VM, then re-run build_all.sh"
fi

# ── Regenerate signed APKINDEX ────────────────────────────────────────────────
echo ""
echo "─────────────────────────────────────────────────"
echo "  Regenerating signed APK index..."
echo "─────────────────────────────────────────────────"
regenerate_index

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
echo "    apk add ai-agent-dashboard"
echo "    apk add llama-server-musl"
echo "    apk add ai-agent-kernel-modules"
echo "    apk add ai-agent-model-lora    # (if LoRA GGUF was available)"
echo "================================================="
