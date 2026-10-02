#!/usr/bin/env python3
"""
deploy_kernel.py — Full kernel deploy pipeline
================================================
1. Waits for SSH on port 2222 (retries until VM is up)
2. Syncs all changed source files to the VM
3. Uploads the updated ai_agent.c (hardened version)
4. Runs custom-kernel/build_kernel.sh inside the VM
5. Reboots the VM
6. Waits for it to come back, then runs the hardening test suite

Usage:
    python deploy_kernel.py
    python deploy_kernel.py --no-reboot    # Build only, don't reboot
    python deploy_kernel.py --test-only    # Skip build, just run tests
"""

import argparse
import os
import sys
import time
import logging
import paramiko

# Suppress paramiko's internal transport thread tracebacks (banner errors
# during sshd startup are expected and handled — no need to log them).
logging.getLogger("paramiko").setLevel(logging.CRITICAL)
logging.getLogger("paramiko.transport").setLevel(logging.CRITICAL)

HOST      = "127.0.0.1"
PORT      = 2222
USER      = "root"
PASSWORD  = os.environ.get("VM_PASSWORD", "password")

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))

# Files to sync: (local_path, remote_path)
SYNC_FILES = [
    # Agent daemon
    (r"agent-daemon/agent_daemon.py",     "/usr/local/lib/ai-agent/agent_daemon.py"),
    (r"agent-daemon/logger.py",           "/usr/local/lib/ai-agent/logger.py"),
    (r"agent-daemon/cdp_controller.py",   "/home/aiuser/cdp_controller.py"),
    # Dashboard
    (r"dashboard/dashboard.py",           "/root/os-ai-agent/dashboard/dashboard.py"),
    # Kernel source (the hardened version)
    (r"custom-kernel/kernel/ai_agent.c",  "/root/os-ai-agent/custom-kernel/kernel/ai_agent.c"),
    (r"custom-kernel/include/uapi/linux/ai_agent.h", "/root/os-ai-agent/custom-kernel/include/uapi/linux/ai_agent.h"),
    # Build script
    (r"custom-kernel/build_kernel.sh",    "/root/os-ai-agent/custom-kernel/build_kernel.sh"),
    # Hardening test suite
    (r"test_syscall_hardening.sh",        "/root/test_syscall_hardening.sh"),
]


def wait_for_ssh(timeout_s=180, interval_s=5):
    """Block until SSH is fully reachable or timeout expires. Returns True on success.

    Distinguishes three states:
      - Connection refused        → VM not up yet, keep waiting
      - SSH banner error (10054)  → Port open, sshd starting, keep waiting
      - Clean auth success        → Ready
    """
    print(f"[DEPLOY] Waiting for SSH on {HOST}:{PORT}  (timeout {timeout_s}s)...")
    deadline = time.time() + timeout_s
    last_state = ""
    while time.time() < deadline:
        try:
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            client.connect(HOST, port=PORT, username=USER, key_filename=os.path.join(REPO_ROOT, "id_rsa_wsl"), timeout=5)
            client.close()
            print(f"\n[DEPLOY] SSH reachable!")
            return True
        except paramiko.ssh_exception.SSHException as e:
            # Banner error = port is open but sshd not ready yet — just retry
            state = "banner"
            if state != last_state:
                sys.stdout.write(" [port open, sshd starting]");
                last_state = state
            sys.stdout.write(".")
            sys.stdout.flush()
            time.sleep(interval_s)
        except (ConnectionRefusedError, TimeoutError, OSError):
            # Port not open yet — VM still booting
            state = "refused"
            if state != last_state:
                sys.stdout.write(" [waiting for port]");
                last_state = state
            sys.stdout.write(".")
            sys.stdout.flush()
            time.sleep(interval_s)
        except Exception:
            sys.stdout.write(".")
            sys.stdout.flush()
            time.sleep(interval_s)
    print("\n[DEPLOY] Timeout waiting for SSH.")
    return False


def make_ssh():
    """Return a connected SSHClient."""
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(HOST, port=PORT, username=USER, key_filename=os.path.join(REPO_ROOT, "id_rsa_wsl"), timeout=10)
    return client


def run_remote(ssh, cmd, label="", stream=True):
    """Run a command on the VM, streaming output. Returns exit code."""
    if label:
        print(f"\n[REMOTE] {label}")
    print(f"  $ {cmd}")
    _, stdout, stderr = ssh.exec_command(cmd, get_pty=True)
    output_lines = []
    for line in iter(stdout.readline, ""):
        safe_line = line.encode('ascii', errors='replace').decode('ascii')
        print(f"    {safe_line}", end="")
        output_lines.append(safe_line)
    exit_code = stdout.channel.recv_exit_status()
    if exit_code != 0:
        err = stderr.read().decode(errors="replace")
        if err.strip():
            print(f"  [STDERR] {err}")
    return exit_code, "".join(output_lines)


def sync_files(ssh):
    """Upload all SYNC_FILES via SFTP."""
    print("\n[DEPLOY] Syncing files to VM...")
    sftp = ssh.open_sftp()

    # Ensure directories exist
    dirs_needed = set()
    for _, remote in SYNC_FILES:
        dirs_needed.add(os.path.dirname(remote))
    for d in sorted(dirs_needed):
        try:
            run_remote(ssh, f"mkdir -p {d}", stream=False)
        except Exception:
            pass

    for local_rel, remote in SYNC_FILES:
        local = os.path.join(REPO_ROOT, local_rel.replace("\\", os.sep))
        if not os.path.exists(local):
            print(f"  [WARN] Local file not found, skipping: {local_rel}")
            continue
        try:
            sftp.put(local, remote)
            size = os.path.getsize(local)
            print(f"  [OK]  {local_rel:<55} -> {remote}  ({size} bytes)")
        except Exception as e:
            print(f"  [FAIL] {local_rel}: {e}")

    sftp.close()
    print("[DEPLOY] Sync complete.")


def build_kernel(ssh):
    """Copy source into kernel tree and compile."""
    print("\n[DEPLOY] ==============================================")
    print("[DEPLOY]   STEP: Build Kernel")
    print("[DEPLOY] ==============================================")

    # Make build_kernel.sh executable
    run_remote(ssh, "chmod +x /root/os-ai-agent/custom-kernel/build_kernel.sh", stream=False)

    # Run the build script (this takes 30–90 minutes on the VM)
    print("[DEPLOY] Running build_kernel.sh — this will take a while...")
    print("[DEPLOY] Output is streamed live:\n")
    exit_code, _ = run_remote(
        ssh,
        "cd /root/os-ai-agent && /root/os-ai-agent/custom-kernel/build_kernel.sh 2>&1",
        label="build_kernel.sh"
    )

    if exit_code == 0:
        print("\n[DEPLOY] [OK] Kernel build SUCCESS")
        return True
    else:
        print(f"\n[DEPLOY] [FAIL] Kernel build FAILED (exit code {exit_code})")
        return False


def reboot_vm(ssh):
    """Send reboot command (SSH will disconnect)."""
    print("\n[DEPLOY] Rebooting VM...")
    try:
        ssh.exec_command("reboot")
    except Exception:
        pass  # SSH drops on reboot — expected
    time.sleep(3)


def run_hardening_tests(ssh):
    """Run the test suite and print results."""
    print("\n[DEPLOY] ==============================================")
    print("[DEPLOY]   STEP: Run Hardening Tests")
    print("[DEPLOY] ==============================================")
    run_remote(ssh, "chmod +x /root/test_syscall_hardening.sh", stream=False)
    exit_code, _ = run_remote(
        ssh,
        "bash /root/test_syscall_hardening.sh 2>&1",
        label="test_syscall_hardening.sh"
    )
    if exit_code == 0:
        print("\n[DEPLOY] [OK] All hardening tests PASSED")
    else:
        print(f"\n[DEPLOY] [FAIL] Some hardening tests FAILED (exit code {exit_code})")
    return exit_code == 0


def main():
    parser = argparse.ArgumentParser(description="Deploy hardened kernel to QEMU VM.")
    parser.add_argument("--no-reboot",   action="store_true", help="Build but don't reboot.")
    parser.add_argument("--test-only",   action="store_true", help="Skip build; only run tests.")
    parser.add_argument("--sync-only",   action="store_true", help="Only sync files, no build.")
    parser.add_argument("--wait-timeout", type=int, default=300,
                        help="Seconds to wait for SSH after reboot (default 300).")
    args = parser.parse_args()

    # ── Wait for VM to be reachable ──────────────────────────────────────────
    if not wait_for_ssh(timeout_s=180):
        print("\n[DEPLOY] VM did not respond within 3 minutes.")
        print("  - If VM is not running: powershell -File boot.ps1")
        print("  - If VM is running but SSH fails: check serial.log for boot errors")
        sys.exit(1)

    ssh = make_ssh()
    print("[DEPLOY] Connected to VM.")

    try:
        if args.test_only:
            run_hardening_tests(ssh)
            return

        # ── Sync all files ──────────────────────────────────────────────────
        sync_files(ssh)

        if args.sync_only:
            print("[DEPLOY] Sync-only mode — done.")
            return

        # ── Build kernel ────────────────────────────────────────────────────
        build_ok = build_kernel(ssh)
        if not build_ok:
            print("[DEPLOY] Aborting due to build failure.")
            sys.exit(1)

        if args.no_reboot:
            print("[DEPLOY] --no-reboot set — skipping reboot.")
            print("[DEPLOY] To boot the new kernel, reboot the VM and select")
            print("         'Linux 6.6.142-ai-agent' in the boot menu.")
            return

        # ── Reboot ──────────────────────────────────────────────────────────
        reboot_vm(ssh)
        ssh.close()

        # ── Wait for VM to come back ─────────────────────────────────────────
        print(f"[DEPLOY] Waiting for VM to reboot (up to {args.wait_timeout}s)...")
        time.sleep(15)  # Give it time to actually start rebooting

        if not wait_for_ssh(timeout_s=args.wait_timeout):
            print("[DEPLOY] VM did not come back within timeout.")
            print("[DEPLOY] Check the QEMU window for boot errors.")
            sys.exit(1)

        # ── Post-reboot: run hardening tests ─────────────────────────────────
        ssh = make_ssh()
        print("[DEPLOY] VM is back up. Running hardening tests...")
        time.sleep(5)  # Let services start
        run_hardening_tests(ssh)

    finally:
        try:
            ssh.close()
        except Exception:
            pass

    print("\n[DEPLOY] ==============================================")
    print("[DEPLOY]   Kernel deploy pipeline COMPLETE")
    print("[DEPLOY] ==============================================\n")


if __name__ == "__main__":
    main()
