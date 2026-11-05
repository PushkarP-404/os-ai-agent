# AI-Agent OS — Project Summary and Quick User Guide

## What this software is

AI-Agent OS is an experimental Alpine Linux–based operating system that embeds a local AI agent into the OS process and syscall layer. Its purpose is to observe system activity and let programs or users query an OS-level agent, without requiring each application to add its own AI plugin or API integration.

The main system is built around a custom Linux 6.6.142 kernel, a userspace agent daemon, a local `llama.cpp` inference server and a small SmolLM2 model. The current workspace also contains a bootable QEMU disk image and three supporting repositories for the OS, model training, and package distribution. Treat this as a research/development project; the README and technical documentation describe validated milestones, but the software is not presented as a production-hardened general-purpose OS.

## The three main folders

| Folder | Purpose | What you find there |
| --- | --- | --- |
| `os-ai-agent/` | The operating system and runtime | Custom kernel changes, process hook, agent daemon, intent CLI, dashboard, eBPF sensor, tests, boot scripts, and APK build recipes. Start here to understand or change OS behavior. |
| `distillation-fine-tuning/` | Model data and training pipeline | Synthetic telemetry generation, teacher-model distillation, ChatML formatting, LoRA training/evaluation, and GGUF export/deployment scripts. This is for developers who want to adapt the small local model. |
| `ai-agent-apk-repo/` | Alpine package distribution | Signed APK packages, package index, public verification key, and a small repository landing page. This lets Alpine systems install the components with `apk`. |

At the workspace root, `ai-agent-os-v0.1.qcow2` is the main bootable VM disk. The other large QCOW2/ISO files and scripts are development, installation, and VM support artifacts. The detailed design reference is `AI_Agent_OS_Technical_Documentation.md` (also copied into `os-ai-agent/`).

## Main features

- **Kernel-to-agent query path:** custom syscall `sys_agent_query` (x86-64 syscall number 548) lets compatible programs send a query to the agent and receive a response through the kernel and daemon.
- **Natural-language intent CLI:** `agent-cli` submits an instruction and streams task/step output while the daemon runs the task engine.
- **Process and system observation:** a kernel process hook and optional eBPF sensors report process activity and selected file/network syscall context to the agent.
- **Local inference:** the appliance runs `llama-server` and a compact SmolLM2 GGUF model inside the guest, so normal inference does not require a cloud model service.
- **Desktop control tools:** the project includes a Sentinel dashboard and browser/desktop bridges for GUI-oriented automation workflows.
- **Telemetry and model improvement:** agent interactions are logged as JSONL; a separate pipeline can distill examples and train a LoRA adapter for the small model.
- **Alpine packaging:** daemon, inference server, model adapter, and kernel modules have APK packaging/distribution support.

## How to use the bootable appliance

### Requirements

- Windows with QEMU (`qemu-system-x86_64`) available, either at the default QEMU path or on `PATH`.
- About 4 GB of free host RAM for the default VM settings; the disk image itself is several GB.

### Start the VM

From the workspace root in PowerShell:

```powershell
.\os-ai-agent\boot.ps1 -Image .\ai-agent-os-v0.1.qcow2
```

The launcher starts QEMU with 4 GB RAM and 4 virtual CPUs by default, and forwards guest SSH to host port 2222. Adjust `-MemoryMB`, `-Cores`, or `-SshPort` if needed. The script starts a headless VM.

Connect from another PowerShell window:

```powershell
ssh -p 2222 root@127.0.0.1
```

The checked-in launcher/documentation currently use the initial `root` / `password` credentials. Change them before exposing the VM or its forwarded services to an untrusted network.

### Ask the agent to perform an intent

Inside the guest, use the CLI with one quoted instruction:

```sh
agent-cli "check disk usage and report the largest directories"
```

It prints a plan or execution steps and streams task results. The project README also shows `/suggest ...` and `/assist ...` intent prefixes. Keep in mind that task execution may invoke system commands; review the requested operation and its output, especially for changes to the guest system.

### Use the desktop dashboard

The documented appliance launch is headless. To use the graphical desktop, start QEMU with a GUI display backend instead of the headless flags, then in the guest open a terminal and run the dashboard as root:

```sh
python3 /root/os-ai-agent/dashboard/dashboard.py
```

The repository README documents the guest desktop login as `aiuser` and the initial root password as `password`. The image may not include the repository checkout at that exact path in every build; if the command is missing, inspect the installed dashboard location or use the source checkout path.

## Installing packages on another Alpine system

The APK repository README describes this flow. Add its public signing key, add the repository URL to `/etc/apk/repositories`, then install the needed packages:

```sh
wget -P /etc/apk/keys/ https://PushkarP-404.github.io/ai-agent-apk-repo/keys/packager-6acb642e.rsa.pub
echo "https://PushkarP-404.github.io/ai-agent-apk-repo" >> /etc/apk/repositories
apk update
apk add ai-agent-daemon llama-server-musl ai-agent-model-lora ai-agent-kernel-modules
```

The custom kernel syscall is part of the custom kernel build; installing APKs on a stock Alpine kernel alone does not provide syscall 548.

## Re-training or adapting the model

From `distillation-fine-tuning/`, install the Python dependencies described in its README. The teacher-distillation step needs a Groq API key; the deployed inference model itself runs locally. Typical pipeline:

```sh
python 00_generate_synthetic_dataset.py
python 01_distill_groq.py --dry-run
python 02_format_dataset.py
python train_lora.py
python 03_eval_distilled_agent.py
python export_gguf.py
```

Run `01_distill_groq.py` without `--dry-run` to process the full dataset. Review the README and script configuration for model names, hardware needs, and deployment steps before training; fine-tuning can require substantial disk, memory, and compute.

## Where to read next

- `os-ai-agent/README.md` — appliance boot, architecture, and features.
- `os-ai-agent/AI_Agent_OS_Technical_Documentation.md` — detailed design, interfaces, testing, and limitations.
- `distillation-fine-tuning/README.md` — training workflow and prerequisites.
- `ai-agent-apk-repo/README.md` — APK installation instructions.

