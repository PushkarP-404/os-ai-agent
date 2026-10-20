#!/usr/bin/env python3
"""
sync_repo.py — AI-Agent OS: Upload packaging files to VM and trigger APK builds.

Workflow:
  1. Upload all APKBUILD scripts and helper files to the VM
  2. Upload the repo_server.py HTTP server
  3. Run setup_repo.sh (idempotent one-time bootstrap)
  4. Optionally upload the LoRA GGUF if it exists on the host
  5. Run build_all.sh to build packages and regenerate the signed repo index
  6. Print install instructions

Usage:
  python sync_repo.py                  # full build
  python sync_repo.py --skip-llama     # skip llama-server (slow cmake build)
  python sync_repo.py --only ai-agent-daemon
  python sync_repo.py --setup-only     # just bootstrap, don't build
"""

import argparse
import os
import sys
if sys.stdout and hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if sys.stderr and hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
import paramiko


# ── Connection defaults ───────────────────────────────────────────────────────
HOST = "127.0.0.1"
PORT = 2222
USER = "root"
PASSWORD = os.environ.get("VM_PASSWORD", "password_123")

# ── Local paths (Windows) ─────────────────────────────────────────────────────
LOCAL_BASE = r"C:\qemu-alpine\os-ai-agent"
LOCAL_PKG  = os.path.join(LOCAL_BASE, "packaging")
LOCAL_LORA = r"C:\qemu-alpine\distillation-fine-tuning\os_agent_lora.gguf"

# ── Remote paths (Alpine VM) ──────────────────────────────────────────────────
REMOTE_BASE = "/root/os-ai-agent"
REMOTE_PKG  = f"{REMOTE_BASE}/packaging"
REMOTE_LORA = "/var/lib/ai-agent/models/os_agent_lora.gguf"


def connect():
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    print(f"Connecting to {HOST}:{PORT}...")
    ssh.connect(HOST, port=PORT, username=USER, password=PASSWORD)
    return ssh


def run(ssh, cmd, check=True):
    """Run a remote command and stream output."""
    print(f"\n[VM] $ {cmd}")
    _, stdout, stderr = ssh.exec_command(cmd, get_pty=True)
    out = stdout.read().decode(errors="replace")
    err = stderr.read().decode(errors="replace")
    if out.strip():
        print(out)
    if err.strip():
        print(err, file=sys.stderr)
    rc = stdout.channel.recv_exit_status()
    if check and rc != 0:
        raise RuntimeError(f"Command failed (exit {rc}): {cmd}")
    return rc


def upload_packaging(sftp, ssh):
    """Upload all APKBUILD files, helper scripts, and the repo_server.py."""
    packages = [
        "llama-server-musl",
        "ai-agent-daemon",
        "ai-agent-kernel-modules",
        "ai-agent-model-lora",
    ]

    print("\n[sync] Uploading packaging files...")
    run(ssh, f"mkdir -p {REMOTE_PKG}", check=False)

    # Top-level scripts
    for fname in ["setup_repo.sh", "build_all.sh", "repo_server.py"]:
        local  = os.path.join(LOCAL_PKG, fname)
        remote = f"{REMOTE_PKG}/{fname}"
        if os.path.exists(local):
            sftp.put(local, remote)
            print(f"  -> {fname}")
        else:
            print(f"  [WARN] {fname} not found locally, skipping")

    run(ssh, f"chmod +x {REMOTE_PKG}/setup_repo.sh {REMOTE_PKG}/build_all.sh")

    # Package subdirectories
    for pkg in packages:
        local_dir  = os.path.join(LOCAL_PKG, pkg)
        remote_dir = f"{REMOTE_PKG}/{pkg}"
        if not os.path.isdir(local_dir):
            print(f"  [WARN] {pkg}/ not found locally, skipping")
            continue
        run(ssh, f"mkdir -p {remote_dir}", check=False)
        for fname in os.listdir(local_dir):
            local_f  = os.path.join(local_dir, fname)
            remote_f = f"{remote_dir}/{fname}"
            if os.path.isfile(local_f):
                sftp.put(local_f, remote_f)
                print(f"  -> {pkg}/{fname}")

    # Also upload the current agent-daemon Python files so the APKBUILD's
    # package() step always gets the latest versions
    print("\n[sync] Uploading latest daemon source files...")
    run(ssh, "mkdir -p /usr/local/lib/ai-agent", check=False)
    daemon_dir = os.path.join(LOCAL_BASE, "agent-daemon")
    for fname in os.listdir(daemon_dir):
        if fname.endswith(".py"):
            local  = os.path.join(daemon_dir, fname)
            remote = f"/usr/local/lib/ai-agent/{fname}"
            sftp.put(local, remote)
            print(f"  -> /usr/local/lib/ai-agent/{fname}")

    # agent-cli source
    run(ssh, "mkdir -p /root/os-ai-agent/agent-cli", check=False)
    for fname in ["agent-cli.c", "Makefile"]:
        local  = os.path.join(LOCAL_BASE, "agent-cli", fname)
        remote = f"/root/os-ai-agent/agent-cli/{fname}"
        if os.path.exists(local):
            sftp.put(local, remote)
            print(f"  -> /root/os-ai-agent/agent-cli/{fname}")

    # kernel-module source
    run(ssh, "mkdir -p /root/os-ai-agent/kernel-module", check=False)
    for fname in ["ai_process_hook.c", "Makefile"]:
        local  = os.path.join(LOCAL_BASE, "kernel-module", fname)
        remote = f"/root/os-ai-agent/kernel-module/{fname}"
        if os.path.exists(local):
            sftp.put(local, remote)
            print(f"  -> /root/os-ai-agent/kernel-module/{fname}")


def upload_lora(sftp, ssh):
    """Upload LoRA GGUF to VM if it exists on the host."""
    if not os.path.exists(LOCAL_LORA):
        print("\n[sync] LoRA GGUF not found on host — skipping upload.")
        print(f"       Expected at: {LOCAL_LORA}")
        return False
    size_mb = os.path.getsize(LOCAL_LORA) / 1024 / 1024
    print(f"\n[sync] Uploading LoRA GGUF ({size_mb:.1f} MB)...")
    run(ssh, "mkdir -p /var/lib/ai-agent/models", check=False)
    sftp.put(LOCAL_LORA, REMOTE_LORA)
    print(f"  -> {REMOTE_LORA}")
    return True


def main():
    parser = argparse.ArgumentParser(description="AI-Agent OS: sync packaging + build APKs")
    parser.add_argument("--skip-llama",  action="store_true",
                        help="Skip llama-server-musl (slow cmake build)")
    parser.add_argument("--only",        metavar="PKG",
                        help="Build only this package")
    parser.add_argument("--setup-only",  action="store_true",
                        help="Run setup_repo.sh only — do not build packages")
    parser.add_argument("--no-lora",     action="store_true",
                        help="Skip LoRA GGUF upload even if present")
    args = parser.parse_args()

    ssh = connect()
    sftp = ssh.open_sftp()

    try:
        upload_packaging(sftp, ssh)

        if not args.no_lora:
            upload_lora(sftp, ssh)

        # Bootstrap (idempotent)
        print("\n[sync] Running setup_repo.sh (bootstrap)...")
        run(ssh, f"sh {REMOTE_PKG}/setup_repo.sh")

        if args.setup_only:
            print("\n[sync] --setup-only: bootstrap done. Skipping build.")
            return

        # Build
        build_flags = ""
        if args.skip_llama:
            build_flags += " --skip-llama"
        if args.only:
            build_flags += f" --only {args.only}"

        print(f"\n[sync] Running build_all.sh{build_flags}...")
        run(ssh, f"sh {REMOTE_PKG}/build_all.sh{build_flags}")

        print("\n")
        print("=" * 55)
        print("  APK repository ready!")
        print(f"  Repo URL: http://127.0.0.1:8080  (inside VM)")
        print(f"  QEMU host: http://127.0.0.1:8080  (if port-forwarded)")
        print()
        print("  Install packages in the VM:")
        print("    apk add ai-agent-daemon")
        print("    apk add llama-server-musl")
        print("    apk add ai-agent-kernel-modules")
        print("    apk add ai-agent-model-lora")
        print("=" * 55)

    finally:
        sftp.close()
        ssh.close()


if __name__ == "__main__":
    main()
