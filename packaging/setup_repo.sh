#!/bin/sh
# packaging/setup_repo.sh
# One-time bootstrap: signing key, abuild user, local repo dir, HTTP repo service.
# Run as root inside the VM.
set -e

REPO_DIR="/srv/apk-repo"
REPO_PORT="8080"
PACKAGER_USER="packager"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "================================================="
echo "  AI-Agent OS APK Repository Bootstrap"
echo "================================================="

# ── 1. Build toolchain ────────────────────────────────
echo "[1/7] Installing build tools..."
rc-service apk-repo-server start 2>/dev/null || true
apk update || true
apk add alpine-sdk build-base linux-headers abuild cmake git curl python3 || true

# ── 2. Packager user ──────────────────────────────────
echo "[2/7] Setting up packager user..."
if ! id "$PACKAGER_USER" >/dev/null 2>&1; then
    adduser -D -G abuild "$PACKAGER_USER"
fi
# Allow packager to run abuild commands that need root (modules_install, etc.)
if ! grep -q "^%abuild.*ALL" /etc/sudoers 2>/dev/null; then
    echo "%abuild ALL=(ALL) NOPASSWD: ALL" >> /etc/sudoers
fi

# ── 3. Signing key ────────────────────────────────────
echo "[3/7] Generating APK signing key..."
KEY_DIR="/home/$PACKAGER_USER/.abuild"
if [ ! -f "$KEY_DIR"/*.rsa 2>/dev/null ]; then
    su "$PACKAGER_USER" -c "abuild-keygen -a -n"
    # -a: RSA, -n: no passphrase (CI-friendly)
fi

# Install public key so apk trusts our packages
cp "$KEY_DIR"/*.pub /etc/apk/keys/
echo "  -> Key installed: $(ls /etc/apk/keys/*.pub | tail -1)"

# ── 4. Repo directory ─────────────────────────────────
echo "[4/7] Creating repository directories..."
mkdir -p "$REPO_DIR/x86_64"
chown -R "$PACKAGER_USER:abuild" "$REPO_DIR"
chmod -R 755 "$REPO_DIR"

# ── 5. HTTP repo server (OpenRC service) ──────────────
echo "[5/7] Installing HTTP APK repo server..."
# Install the Python HTTP server script
install -Dm755 "$SCRIPT_DIR/repo_server.py" /usr/local/lib/apk-repo-server/repo_server.py

# OpenRC init script for the repo server
cat > /etc/init.d/apk-repo-server << 'OPENRC'
#!/sbin/openrc-run
description="AI-Agent OS local APK repository HTTP server"

REPO_DIR="${REPO_DIR:-/srv/apk-repo}"
REPO_PORT="${REPO_PORT:-8080}"
REPO_LOG="/var/log/apk-repo-server.log"
REPO_PIDFILE="/run/apk-repo-server.pid"

depend() {
    need localmount net
    before ai-agent llama-server
}

start() {
    ebegin "Starting APK repo server on port $REPO_PORT"
    start-stop-daemon --start \
        --background \
        --make-pidfile \
        --pidfile "$REPO_PIDFILE" \
        --stdout "$REPO_LOG" \
        --stderr "$REPO_LOG" \
        --chdir "$REPO_DIR" \
        --exec /usr/bin/python3 \
        -- /usr/local/lib/apk-repo-server/repo_server.py \
            --port "$REPO_PORT" \
            --dir "$REPO_DIR"
    eend $?
}

stop() {
    ebegin "Stopping APK repo server"
    start-stop-daemon --stop \
        --pidfile "$REPO_PIDFILE" \
        --retry "SIGTERM/5/SIGKILL/2"
    eend $?
}
OPENRC
chmod +x /etc/init.d/apk-repo-server

# Enable on boot (starts before ai-agent/llama-server)
rc-update add apk-repo-server default || true
rc-service apk-repo-server restart 2>/dev/null || rc-service apk-repo-server start 2>/dev/null || true

# ── 6. Register repo URL in /etc/apk/repositories ────
echo "[6/7] Registering local HTTP repo..."
REPO_URL="http://127.0.0.1:$REPO_PORT"
if ! grep -qF "$REPO_URL" /etc/apk/repositories; then
    echo "$REPO_URL" >> /etc/apk/repositories
    echo "  -> Added $REPO_URL to /etc/apk/repositories"
else
    echo "  -> Already registered: $REPO_URL"
fi

# ── 7. First-run index (empty but valid) ─────────────
echo "[7/7] Generating initial (empty) APK index..."
if [ ! -f "$REPO_DIR/x86_64/APKINDEX.tar.gz" ]; then
    # Create a minimal empty index
    su "$PACKAGER_USER" -c "
        cd $REPO_DIR/x86_64
        apk index -o APKINDEX.tar.gz 2>/dev/null || true
        abuild-sign -k \$HOME/.abuild/*.rsa APKINDEX.tar.gz 2>/dev/null || true
    "
fi

echo ""
echo "================================================="
echo "  Bootstrap complete!"
echo "  Repo:    $REPO_DIR"
echo "  URL:     $REPO_URL"
echo "  Key:     $KEY_DIR/*.rsa.pub"
echo ""
echo "  Next: run build_all.sh to build packages"
echo "================================================="
