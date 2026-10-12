#!/usr/bin/env python3
"""
export_public_repo.py — AI-Agent OS: Export APK repository from VM for GitHub Pages.

Fetches:
  - /srv/apk-repo/x86_64/*.apk and APKINDEX.tar.gz
  - /etc/apk/keys/packager-*.rsa.pub
Writes to a clean git-ready folder:
  c:\\qemu-alpine\\ai-agent-apk-repo
Includes:
  - index.html (web overview & installation instructions for users)
  - README.md
"""

import os
import sys
import paramiko

if sys.stdout and hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

HOST = "127.0.0.1"
PORT = 2222
USER = "root"
PASSWORD = os.environ.get("VM_PASSWORD", "password")

LOCAL_EXPORT_DIR = r"C:\qemu-alpine\ai-agent-apk-repo"
REMOTE_REPO_DIR = "/srv/apk-repo/x86_64"
REMOTE_KEY_DIR  = "/etc/apk/keys"


def main():
    print("==================================================")
    print("  AI-Agent OS: Exporting APK Repo for GitHub      ")
    print("==================================================")

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    print(f"Connecting to {HOST}:{PORT}...")
    try:
        ssh.connect(HOST, port=PORT, username=USER, password=PASSWORD, timeout=10)
    except Exception as e:
        print(f"[ERROR] Failed to connect to VM: {e}")
        print("Please ensure the Alpine VM is booted (python boot_and_enable_sshd.py).")
        sys.exit(1)

    sftp = ssh.open_sftp()

    # 1. Prepare local directory structure
    local_x86 = os.path.join(LOCAL_EXPORT_DIR, "x86_64")
    local_keys = os.path.join(LOCAL_EXPORT_DIR, "keys")
    os.makedirs(local_x86, exist_ok=True)
    os.makedirs(local_keys, exist_ok=True)

    # 2. Download signing key
    print("\n[1/3] Downloading public signing key...")
    key_files = sftp.listdir(REMOTE_KEY_DIR)
    pub_key_name = None
    for k in key_files:
        if k.startswith("packager-") and k.endswith(".rsa.pub"):
            pub_key_name = k
            remote_k = f"{REMOTE_KEY_DIR}/{k}"
            local_k = os.path.join(local_keys, k)
            sftp.get(remote_k, local_k)
            print(f"  -> Downloaded: keys/{k}")
            break

    if not pub_key_name:
        print("  [WARN] No packager-*.rsa.pub found in /etc/apk/keys!")
        pub_key_name = "packager.rsa.pub"

    # 3. Download APK packages and APKINDEX
    print("\n[2/3] Downloading APK packages and index...")
    repo_files = sftp.listdir(REMOTE_REPO_DIR)
    for f in repo_files:
        if f.endswith(".apk") or f.startswith("APKINDEX"):
            remote_f = f"{REMOTE_REPO_DIR}/{f}"
            local_f = os.path.join(local_x86, f)
            size_mb = sftp.stat(remote_f).st_size / 1024 / 1024
            print(f"  -> Downloading {f} ({size_mb:.2f} MB)...")
            sftp.get(remote_f, local_f)

    sftp.close()
    ssh.close()

    # 4. Generate README.md and index.html
    print("\n[3/3] Generating repository documentation and index.html...")
    readme_path = os.path.join(LOCAL_EXPORT_DIR, "README.md")
    with open(readme_path, "w", encoding="utf-8") as f:
        f.write(f"""# AI-Agent OS — Official APK Package Repository

This repository hosts official packages for **AI-Agent OS** (an autonomous Alpine-derived Linux distribution).

## Available Packages

| Package | Version | Description |
|---|---|---|
| `ai-agent-daemon` | `0.1.0-r0` | Kernel-userspace agent daemon, task engine, and `agent-cli` |
| `llama-server-musl` | `0.1.0-r0` | Native musl HTTP inference server with bundled runtime libraries |
| `ai-agent-model-lora` | `0.1.0-r0` | Fine-tuned distilled LoRA adapter GGUF (`os_agent_lora.gguf`) |
| `ai-agent-kernel-modules` | `0.1.0-r0` | Out-of-tree kernel module (`ai_process_hook.ko`) for `6.6.142-ai-agent` |

---

## Installation Instructions (for any Alpine Linux system)

### 1. Trust the Public Signing Key
```sh
wget -P /etc/apk/keys/ https://PushkarP-404.github.io/ai-agent-apk-repo/keys/{pub_key_name}
```

### 2. Add the Repository
```sh
echo "https://PushkarP-404.github.io/ai-agent-apk-repo" >> /etc/apk/repositories
```

### 3. Update & Install Packages
```sh
apk update
apk add ai-agent-daemon llama-server-musl ai-agent-model-lora ai-agent-kernel-modules
```

---
*Built with Alpine `abuild` and cryptographically signed.*
""")

    # Generate HTML landing page for GitHub Pages
    index_html = os.path.join(LOCAL_EXPORT_DIR, "index.html")
    with open(index_html, "w", encoding="utf-8") as f:
        f.write(f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>AI-Agent OS — Package Repository</title>
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;800&family=JetBrains+Mono&display=swap">
  <style>
    :root {{
      --bg: #0d1117;
      --card: #161b22;
      --border: #30363d;
      --text: #e6edf3;
      --accent: #58a6ff;
      --accent-subtle: #1f6feb22;
      --green: #238636;
      --green-light: #3fb950;
    }}
    body {{
      background: var(--bg);
      color: var(--text);
      font-family: 'Inter', sans-serif;
      margin: 0;
      padding: 40px 20px;
      display: flex;
      justify-content: center;
    }}
    .container {{
      max-width: 800px;
      width: 100%;
    }}
    h1 {{
      font-size: 2.4rem;
      font-weight: 800;
      margin-bottom: 8px;
      background: linear-gradient(90deg, #58a6ff, #bc8cff);
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
    }}
    p.lead {{
      color: #8b949e;
      font-size: 1.1rem;
      margin-top: 0;
      margin-bottom: 30px;
    }}
    .card {{
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 24px;
      margin-bottom: 24px;
      box-shadow: 0 4px 20px rgba(0,0,0,0.3);
    }}
    h2 {{
      font-size: 1.3rem;
      margin-top: 0;
      color: var(--accent);
      border-bottom: 1px solid var(--border);
      padding-bottom: 10px;
    }}
    pre {{
      background: #090d13;
      border: 1px solid var(--border);
      padding: 14px 18px;
      border-radius: 6px;
      overflow-x: auto;
      font-family: 'JetBrains Mono', monospace;
      font-size: 0.95rem;
      color: #79c0ff;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      margin-top: 15px;
    }}
    th, td {{
      padding: 10px 14px;
      text-align: left;
      border-bottom: 1px solid var(--border);
    }}
    th {{
      color: #8b949e;
      font-size: 0.85rem;
      text-transform: uppercase;
    }}
    .badge {{
      display: inline-block;
      padding: 2px 8px;
      font-size: 0.8rem;
      background: #23863633;
      color: #3fb950;
      border: 1px solid #238636;
      border-radius: 12px;
      font-family: 'JetBrains Mono', monospace;
    }}
  </style>
</head>
<body>
  <div class="container">
    <h1>AI-Agent OS</h1>
    <p class="lead">Official Alpine Linux APK Package Repository</p>

    <div class="card">
      <h2>Quick Install Instructions</h2>
      <p>Run the following commands inside Alpine Linux:</p>

      <p><strong>1. Trust the Public Signing Key:</strong></p>
      <pre>wget -P /etc/apk/keys/ https://PushkarP-404.github.io/ai-agent-apk-repo/keys/{pub_key_name}</pre>

      <p><strong>2. Register Repository:</strong></p>
      <pre>echo "https://PushkarP-404.github.io/ai-agent-apk-repo" >> /etc/apk/repositories</pre>

      <p><strong>3. Install Packages:</strong></p>
      <pre>apk update
apk add ai-agent-daemon llama-server-musl ai-agent-model-lora ai-agent-kernel-modules</pre>
    </div>

    <div class="card">
      <h2>Available Packages</h2>
      <table>
        <thead>
          <tr>
            <th>Package</th>
            <th>Version</th>
            <th>Type</th>
            <th>Status</th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <td><strong>ai-agent-daemon</strong></td>
            <td>0.1.0-r0</td>
            <td>Daemon + CLI</td>
            <td><span class="badge">Signed & Ready</span></td>
          </tr>
          <tr>
            <td><strong>llama-server-musl</strong></td>
            <td>0.1.0-r0</td>
            <td>Inference Engine</td>
            <td><span class="badge">Signed & Ready</span></td>
          </tr>
          <tr>
            <td><strong>ai-agent-model-lora</strong></td>
            <td>0.1.0-r0</td>
            <td>Fine-Tuned Adapter</td>
            <td><span class="badge">Signed & Ready</span></td>
          </tr>
          <tr>
            <td><strong>ai-agent-kernel-modules</strong></td>
            <td>0.1.0-r0</td>
            <td>LKM (6.6.142)</td>
            <td><span class="badge">Signed & Ready</span></td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</body>
</html>
""")

    print(f"\n[SUCCESS] Exported repository ready at: {LOCAL_EXPORT_DIR}")
    print("Files ready to be pushed to GitHub:")
    for root, dirs, files in os.walk(LOCAL_EXPORT_DIR):
        for f in files:
            rel = os.path.relpath(os.path.join(root, f), LOCAL_EXPORT_DIR)
            print(f"  - {rel}")


if __name__ == "__main__":
    main()
