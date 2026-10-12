# AI-Agent OS — Multi-Repo Architecture & Contributor Guide

AI-Agent OS is divided into **three specialized GitHub repositories**. This modular architecture separates **system development**, **model intelligence training**, and **package distribution**.

---

## 🗺️ System Overview: The 3 Repositories

| Repository | GitHub Target | Primary Role | Key Technologies |
|---|---|---|---|
| **`os-ai-agent`** | [`PushkarP-404/os-ai-agent`](https://github.com/PushkarP-404/os-ai-agent) | **Core Operating System** | Linux Kernel 6.6 (C), LKM, OpenRC, Python, Alpine Linux |
| **`distillation-fine-tuning`** | [`PushkarP-404/distillation-fine-tuning`](https://github.com/PushkarP-404/distillation-fine-tuning) | **AI Brain & Intelligence** | PyTorch, Hugging Face, PEFT/LoRA, llama.cpp GGUF, Groq API |
| **`ai-agent-apk-repo`** | [`PushkarP-404/ai-agent-apk-repo`](https://github.com/PushkarP-404/ai-agent-apk-repo) | **Public Package Mirror** | Alpine APK format, cryptographic signing (`abuild-sign`), GitHub Pages |

---

## 🔄 How Information & Artifacts Flow

```
[ distillation-fine-tuning ]
   │
   │ 1. Train model on OS syscall events
   │ 2. Export LoRA adapter to GGUF format
   ▼
os_agent_lora.gguf (37.3 MB)
   │
   ▼
[ os-ai-agent ]
   │
   │ 1. Native C/C++ compilation (agent-cli, kernel hook)
   │ 2. Build APKs inside Alpine VM via APKBUILD manifests
   │ 3. Cryptographically sign packages and index
   ▼
.apk binary packages + APKINDEX.tar.gz
   │
   ▼
[ ai-agent-apk-repo ]
   │
   │ 1. Static HTTP hosting via GitHub Pages
   │ 2. Serves packages to end-users worldwide
   ▼
End-User / Live OS (apk add ai-agent-daemon)
```

---

## 🛠️ Repository Details & Setup

### 1. `distillation-fine-tuning` (The Brain)
Responsible for dataset generation, teacher-model distillation, and training lightweight edge adapters for the OS.

* **Main Outputs**: `os_agent_lora.gguf`
* **Prerequisites**: Python 3.10+, PyTorch, Hugging Face `transformers`, `peft`
* **Setup & Development Flow**:
  1. Clone repository: `git clone https://github.com/PushkarP-404/distillation-fine-tuning.git`
  2. Install dependencies: `pip install -r requirements.txt` (torch, transformers, peft, datasets, groq)
  3. Generate synthetic syscall logs: `python 00_generate_synthetic_dataset.py`
  4. Distill expert reasoning with teacher model: `python 01_distill_groq.py`
  5. Format to ChatML: `python 02_format_dataset.py`
  6. Train LoRA adapter: `python train_lora.py`
  7. Export to GGUF: `python export_gguf.py`
  8. Validate model: `python 03_eval_distilled_agent.py`

---

### 2. `os-ai-agent` (The Operating System)
Contains all operating system code: kernel module, userspace daemon, system CLI, OpenRC init scripts, and packaging build definitions.

* **Key Directories**:
  * `kernel-module/`: Loadable Kernel Module (`ai_process_hook.c`) capturing process events.
  * `agent-daemon/`: Python daemon listening on Netlink bus and managing LLM queries.
  * `agent-cli/`: Static C binary for issuing syscall 548 queries.
  * `packaging/`: Alpine Linux `APKBUILD` recipes and build automation scripts.
* **Prerequisites**: QEMU x86_64, Alpine Linux VM with custom kernel `6.6.142-ai-agent`
* **Setup & Development Flow**:
  1. Clone repository: `git clone https://github.com/PushkarP-404/os-ai-agent.git`
  2. Boot development VM: `python boot_and_enable_sshd.py`
  3. Sync changes and trigger package build: `python packaging/sync_repo.py`
  4. Test syscall live: `/root/os-ai-agent/test-programs/test_syscall`

---

### 3. `ai-agent-apk-repo` (The Distribution Mirror)
The public-facing CDN repository that allows anyone running Alpine Linux to install or update the OS components using native `apk` commands.

* **Key Directories**:
  * `x86_64/`: Contains the binary `.apk` archives and signed `APKINDEX.tar.gz`.
  * `keys/`: Public RSA verification key (`packager-*.rsa.pub`).
  * `index.html`: Web overview rendered by GitHub Pages.
* **Prerequisites**: Git, GitHub Pages enabled on branch `master`.
* **Setup & Deployment Flow**:
  1. Clone repository: `git clone https://github.com/PushkarP-404/ai-agent-apk-repo.git`
  2. To update packages from a new build in `os-ai-agent`:
     * Run `python packaging/export_public_repo.py` inside `os-ai-agent`.
  3. Commit and push:
     ```bash
     git add -A
     git commit -m "chore: release updated apk packages"
     git push origin master
     ```

---

## 🤝 Contribution Guidelines

1. **Improving Model Decisions**:
   * Contribute to `distillation-fine-tuning`. Add new syscall query patterns or prompt templates.
   * Run `export_gguf.py` and test accuracy with `03_eval_distilled_agent.py`.

2. **Modifying OS Services, CLI, or Kernel**:
   * Contribute to `os-ai-agent`.
   * Ensure C code builds cleanly with `make CC=gcc CFLAGS="-Wall -O2"`.
   * Update package version in `packaging/<pkg>/APKBUILD` (increment `pkgver` or `pkgrel`).

3. **Releasing Updates to Users**:
   * Once changes in `os-ai-agent` are verified inside the VM, run `export_public_repo.py`.
   * Push the updated `.apk` files and `APKINDEX.tar.gz` to `ai-agent-apk-repo`.
