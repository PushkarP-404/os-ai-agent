# AI-Agent OS

> An operating system architecture where an AI agent is integrated directly into the Linux kernel and process lifecycle, allowing any process to be observed, understood, and directed without app-level APIs, plugins, or MCP.

[![Status](https://img.shields.io/badge/Status-Phases%201--9%20Complete%20%26%20Validated-brightgreen.svg)]()
[![Kernel](https://img.shields.io/badge/Kernel-6.6.142--ai--agent-blue.svg)]()
[![Distro](https://img.shields.io/badge/Base%20Distro-Alpine%203.20%20(musl)-blue.svg)]()
[![Inference](https://img.shields.io/badge/Inference-llama.cpp%20(native%20musl)-orange.svg)]()
[![Model](https://img.shields.io/badge/Model-SmolLM2--135M--Instruct--Q4_K_M-purple.svg)]()

---

## ðŸŒŸ The Core Vision

Modern AI assistants integrate with applications via plugins, APIs, or protocols like MCP (Model Context Protocol). This requires every application to be explicitly rewritten or adapted to support AI.

**AI-Agent OS flips this model:**
> **The integration point is the operating system itself, not the application.**

Every process : legacy binaries, CLI tools, services, scripts, containers â€” must execute system calls through the kernel. By embedding an agent at the OS layer, the system provides:

- **Universal Observability:** Intercept process events and syscall activity without application knowledge.
- **Synchronous Kernel-AI Syscall (`sys_agent_query` #548):** Any program can query the operating system's built-in reasoning engine natively.
- **Self-Contained In-Guest Inference:** Powered by native musl `llama.cpp` and SmolLM2, operating entirely offline with zero host or cloud dependencies.
- **Continuous Telemetry & Fine-Tuning Pipeline:** Every query, latency metric, and verdict is logged to `/var/ai-agent/` in JSONL format for LoRA domain adaptation.

---

## ðŸ’» Hardware Requirements

Because the AI Agent OS runs a local LLM inference engine (`llama-server`) directly inside the guest VM alongside a graphical desktop, hardware requirements scale based on the intelligence level you desire.

### Minimum Requirements (Basic Harness)
*Runs the default `SmolLM2-135M-Instruct` agent, Alpine Linux, and XFCE4.*
* **CPU:** 2+ Cores (x86_64 or ARM64 with hardware virtualization VT-x/AMD-V enabled)
* **RAM:** 2 GB 
* **Storage:** 5 GB available space
* **GPU:** None required (LLM runs on CPU, GUI uses software rendering)

### Recommended Requirements (Sentinel Harness & Fine-Tuning)
*Capable of running larger models (e.g. Llama-3.2-1B/3B) for the Planner/Worker architecture, complex browser automation, and in-guest LoRA fine-tuning.*
* **CPU:** 4-8 Cores (Modern processor for fast `llama.cpp` CPU inference)
* **RAM:** 8 GB+ 
* **Storage:** 20 GB SSD (To store `.gguf` weights, LoRA adapters, and JSONL datasets)
* **GPU:** Optional but highly recommended (GPU passthrough via VFIO, or host-side serving, drastically reduces latency for multi-step ReAct loops)

---

## ðŸ—ï¸ System Architecture

```mermaid
graph TD
    subgraph Userspace ["Userspace Applications"]
        App["Unmodified Application / Binary"]
        TestProg["test_syscall (Process Query)"]
    end

    subgraph Kernel ["Linux Kernel 6.6.142-ai-agent"]
        Syscall["sys_agent_query (Syscall #548)"]
        KHook["kretprobe kernel_clone (LKM Hook)"]
        Netlink["Netlink IPC Channel (Proto 31)"]
        WaitQueue["Interruptible WaitQueue (15s Timeout)"]
        Fallback["Kernel Diagnostics Fallback"]
    end

    subgraph UserServices ["System AI Services (/usr/local)"]
        Daemon["agent_daemon.py (OpenRC Service)"]
        LlamaServer["llama-server (:11434, 4 Threads)"]
        Model["SmolLM2-135M GGUF (100.6MB)"]
        Dataset["/var/ai-agent/training_data/dataset.jsonl"]
    end

    TestProg -->|syscall 548| Syscall
    App -->|fork / clone| KHook
    KHook -->|Process Event| Netlink
    Syscall --> WaitQueue
    Syscall -->|AI_MSG_SYSCALL_QUERY| Netlink
    Netlink <-->|Bidirectional IPC| Daemon
    Daemon -->|HTTP /completion| LlamaServer
    LlamaServer --> Model
    Daemon -->|AI_MSG_SYSCALL_RESP| Netlink
    Netlink --> WaitQueue
    WaitQueue -->|Copy to User| TestProg
    WaitQueue -.->|On Timeout/Offline| Fallback
    Daemon -->|Log Interaction| Dataset
```

---

## ðŸš€ Quick Start: Booting the OS Appliance

The system is packaged into a self-contained, bootable QCOW2 disk image: `ai-agent-os-v0.1.qcow2`.

### Requirements

- [QEMU](https://www.qemu.org/) (`qemu-system-x86_64`) installed.
- 4GB RAM available on host.

### One-Command Boot

**Windows (PowerShell):**

```powershell
.\boot.ps1
```

**Linux / macOS (Bash):**

```bash
chmod +x boot.sh
./boot.sh
```

The system automatically initializes:

1. Boots `Linux 6.6.142-ai-agent` directly via Syslinux.
2. Starts native `llama-server` on `127.0.0.1:11434`.
3. Starts `agent_daemon.py` and registers PID with the kernel.
4. Starts `sshd` and forwards SSH to `localhost:2222`.

### Accessing the Guest

- **SSH:** `ssh -p 2222 root@127.0.0.1`
- **Web / API:** `http://127.0.0.1:11434/health` inside the guest.

---

## ðŸ§ª Automated Boot & System Verification

The system includes an automated test harness that exercises all layers of the OS stack:

```bash
/root/os-ai-agent/test_phase6_boot.sh
```

### Verification Output

```
==================================================
    AI-Agent OS: Phase 6 Boot Validation Harness  
==================================================
[PASS] Running custom kernel: 6.6.142-ai-agent
[PASS] llama-server and llama-cli installed in /usr/local/bin
[PASS] All dynamic library dependencies resolved for llama-server
[PASS] GGUF model present at /var/lib/ai-agent/models/smollm2-135m-instruct-q4_k_m.gguf (size: 100.6M)
[PASS] agent_daemon.py installed and executable at /usr/local/lib/ai-agent/agent_daemon.py
[PASS] llama-server registered in default runlevel
[PASS] ai-agent registered in default runlevel
[PASS] sshd registered in default runlevel
[PASS] llama-server /health returned healthy status
[PASS] Kernel confirmed daemon registration in dmesg
[PASS] syscall(548) successfully routed to LLM and returned response in 4434.92 ms
[PASS] All kernel edge-case validations passed (NULL pointers, invalid memory)
[PASS] Dataset exists at /var/ai-agent/training_data/dataset.jsonl with 18 records

==================================================
           Phase 6 Boot Validation Summary        
==================================================
  Total Passed: 13
  Total Failed: 0
  Warnings:     0
  RESULT: ALL PHASE 6 BOOT VALIDATIONS PASSED! [SUCCESS]
==================================================
```

---

## ðŸ› ï¸ Invoking the Syscall from C

Any userspace program can query the operating system agent directly:

```c
#include <stdio.h>
#include <unistd.h>
#include <sys/syscall.h>

#define SYS_AGENT_QUERY 548

int main(void) {
    char response[512] = {0};
    const char *query = "Checking process safety and resource state.";

    long ret = syscall(SYS_AGENT_QUERY, 0 /* target PID */, query, strlen(query), response, sizeof(response));
    if (ret >= 0) {
        printf("Kernel AI Agent Verdict: %s\n", response);
    } else {
        perror("sys_agent_query failed");
    }
    return 0;
}
```

---

## ðŸ“ Repository Structure

```
os-ai-agent/
â”œâ”€â”€ agent-daemon/                 # Userspace Agent Daemon & Services
â”‚   â”œâ”€â”€ agent_daemon.py           # Unified Netlink daemon (Process events + Syscall #548)
â”‚   â”œâ”€â”€ logger.py                 # Telemetry & JSONL fine-tuning dataset logger
â”‚   â””â”€â”€ openrc/                   # OpenRC service scripts
â”‚       â”œâ”€â”€ llama-server          # Native musl inference server service
â”‚       â””â”€â”€ ai-agent              # Unified agent daemon service
â”œâ”€â”€ custom-kernel/                # Custom Linux Kernel Source & Headers
â”‚   â”œâ”€â”€ kernel/ai_agent.c         # sys_agent_query (#548) implementation
â”‚   â””â”€â”€ include/uapi/linux/ai_agent.h # UAPI syscall & Netlink definitions
â”œâ”€â”€ kernel-module/                # Loadable Kernel Module (LKM)
â”‚   â””â”€â”€ ai_process_hook.c         # kretprobe hook on kernel_clone
â”œâ”€â”€ ptrace-monitor/               # Phase 1 Userspace Monitor
â”‚   â””â”€â”€ monitor.c                 # ptrace-based syscall interceptor
â”œâ”€â”€ agent-cli/                    # Phase 8 Intent CLI
â”‚   â”œâ”€â”€ agent-cli.c               # Invokes Syscall #548 to dispatch JSON intents
â”‚   â””â”€â”€ Makefile
â”œâ”€â”€ test-programs/                # Syscall Validation Programs
â”‚   â”œâ”€â”€ test_syscall.c            # Single-process & edge-case test
â”‚   â””â”€â”€ test_syscall_stress.c     # Multi-threaded concurrent stress test
â”œâ”€â”€ boot.ps1                      # Windows PowerShell one-command boot launcher
â”œâ”€â”€ boot.sh                       # Linux / macOS Bash one-command boot launcher
â”œâ”€â”€ test_phase6_boot.sh           # Phase 6 automated verification harness
â”œâ”€â”€ AI_Agent_OS_Technical_Documentation.md # Exhaustive technical architecture & reference
â””â”€â”€ README.md
```

---

## ðŸ¤ Contributing & Security Vulnerabilities

Since this project introduces deep, kernel-level changes (such as custom system calls and Netlink IPC hooks), there is a potential for security vulnerabilities or stability issues that we might have missed during initial development.

We highly encourage the open-source community to review the code, look out for potential exploits, memory leaks, or race conditions, and contribute patches.

If you discover a security vulnerability:

- Please open an issue in the repository with a detailed reproduction case.
- Alternatively, you can email me directly to report sensitive security issues before they are publicly disclosed.

All contributions, whether they are security fixes, performance improvements, or documentation updates, are welcome!

---

## ðŸ“œ Roadmap & Milestone Summary

| Phase | Milestone | Status |
| --- | --- | --- |
| **Phase 1** | Userspace `ptrace` syscall monitor | âœ… Validated |
| **Phase 2** | LLM syscall explanation & analysis pipeline | âœ… Validated |
| **Phase 3** | LKM `kernel_clone` hook + Netlink IPC (Proto 31) | âœ… Validated |
| **Phase 4** | Custom kernel `6.6.142-ai-agent` + `sys_agent_query` (#548) | âœ… Validated |
| **Phase 5** | In-guest native musl `llama.cpp` + SmolLM2-135M | âœ… Validated |
| **Phase 6** | Self-contained compressed OS appliance image (`ai-agent-os-v0.1.qcow2`) | âœ… Validated |
| **Phase 7** | OS Agent Fine-Tuning (LoRA) â€” The OS continuously learns from its own `.jsonl` logs | âœ… Validated |
| **Phase 8** | Intent-Driven Execution (Orchestrator OS) â€” Automatic task delegation via sub-processes | âœ… Validated |
| **Phase 9** | Universal Visual Orchestration â€” Computer Use and GUI Automation via Chrome DevTools Protocol | âœ… Validated |

---

## ðŸ“– Complete Documentation

For the comprehensive design document, threat modeling, kernel internals, and benchmarking details, see:  
ðŸ‘‰ **[AI_Agent_OS_Technical_Documentation.md](AI_Agent_OS_Technical_Documentation.md)**
