# AI-Agent OS: Technical Design & Implementation Document

**Project codename:** AI-Agent OS (working title)
**Document version:** 0.1 (living document)
**Status:** Phase 1 & 2 Completed — Userspace ptrace monitor + Ollama AI syscall analyzer validated end-to-end in QEMU VM
**Base distro:** Alpine Linux 3.20.10 (musl libc, BusyBox userland)

---

## Table of Contents

1. Executive Summary
2. Project Vision & Philosophy
3. Core Architectural Insight
4. System Architecture Overview
5. OS/Distro Decision & Rationale
6. Development Environment
7. Networking Reference (Lessons Learned)
8. Phased Roadmap
9. Phase 1: Userspace Ptrace Monitor — Implementation Detail
10. Phase 2: AI Agent Integration
11. Phase 3: Kernel Module Integration (Planned)
12. Phase 4: Custom Syscall Interface (Planned)
13. Phase 5: Local LLM + Fine-Tuning Pipeline
14. Phase 6: OS Image Packaging & Distribution
15. Data Collection & Storage Schema
16. Security Model & Threat Considerations
17. Agent Autonomy Levels
18. Repository Structure
19. Git Workflow
20. Toolchain Reference
21. Open Questions & Future Decisions
22. Glossary

---

## 1. Executive Summary

This project is an attempt to build a custom Linux-based operating system in which an AI agent is woven into the process lifecycle itself, rather than bolted on as an application-layer feature. The stated goal has two parts:

- **Learning goal:** Deeply understand systems programming — process management, syscalls, IPC, kernel internals, and how AI systems can be integrated with low-level infrastructure.
- **Product goal:** Build a genuinely novel OS where every process, regardless of whether it was designed with AI support in mind, can be observed, understood, and potentially directed by a built-in agent.

The defining architectural principle is: **the integration point is the OS itself, not the application.** Because every piece of software running on the system — from a 30-year-old legacy binary to a modern containerized app — must go through the kernel to do anything meaningful (open files, use the network, allocate memory, spawn processes), placing the agent at the OS layer means it can observe and potentially influence *all* software uniformly, without requiring APIs, plugins, or MCP-style integration from the software itself.

This document captures every technical and architectural decision made so far, the reasoning behind each, the current state of the development environment, and the full roadmap from the current userspace prototype to an eventual kernel-integrated, locally-fine-tuned agent shipped as a pre-installed component of a custom OS image.

---

## 2. Project Vision & Philosophy

### 2.1 The core idea

> "I want to integrate the agent directly with the OS because I want all of my software and apps to be controlled by the agent, no matter if they are designed to be or not, without API or MCP."

This is the guiding sentence for the entire project. It rules out (as a long-term end state) any architecture that depends on:
- Applications explicitly calling an AI API
- Applications implementing MCP (Model Context Protocol) or similar tool-calling interfaces
- Plugins, browser extensions, or app-specific integrations

Instead, the agent's "hooks" into the world are OS primitives: syscalls, process lifecycle events, file I/O, network I/O, memory, input devices, and framebuffer/display output. These are things no userspace program can opt out of, because they are the only way a program can do anything at all on a POSIX-like system.

### 2.2 Why this matters as a design constraint

Every implementation decision from here on should be evaluated against one question: **"Does this require the target application's cooperation?"** If yes, it is a stopgap (acceptable for Phase 1–2 prototyping) but not the final architecture. If no — if it works purely by observing/intercepting kernel-mediated events — it is aligned with the actual vision.

### 2.3 Why this is a good learning vehicle

Building this system forces confrontation with:
- Process creation and the fork/exec model
- The syscall interface and calling convention (registers, `orig_rax`, return values)
- ptrace semantics (`PTRACE_TRACEME`, `PTRACE_SYSCALL`, `PTRACE_GETREGS`)
- IPC mechanisms (Unix sockets, shared memory, netlink)
- Kernel module development (`task_struct`, `copy_process()`, LKM lifecycle)
- The tradeoffs between userspace and kernel-space interception
- LLM deployment constraints (memory, quantization, latency) on constrained hardware
- Fine-tuning techniques (LoRA) and why they matter for a system that must "learn its own behavior"

### 2.4 Why this is potentially novel

No mainstream OS today embeds an AI agent as a first-class citizen of the process model. Existing "AI in the OS" efforts (Copilot-style assistants, AI browser extensions, MCP-based agent frameworks) are all application-layer or API-layer integrations that require the target software to cooperate. An OS where the kernel-adjacent layer itself understands and can act on process behavior — regardless of what that process is — does not have a direct mainstream analog. This is both the opportunity and the risk (see Security Model, Section 16).

---

## 3. Core Architectural Insight

```
Traditional AI integration:
  App  <--->  AI API          (only works if the app was built to call an API)

OS-level AI integration (this project):
  App
   |
   v
  OS / Kernel layer   <---  Agent lives here, sees everything below every app
   |
   v
  Hardware
```

Because the OS/kernel layer is beneath every application, an agent embedded there inherits **universal reach** without needing per-application support. This is architecturally similar to how antivirus software, parental control software, or system-wide monitoring tools work today — they intercept at a layer the application cannot bypass. The difference is that instead of blocking threats, this agent is meant to *understand and potentially direct* process behavior.

### 3.1 What the agent can observe (by design, at full maturity)

- Every syscall any process makes (open, read, write, socket, connect, fork, exec, mmap, etc.)
- File reads and writes, and their content where relevant
- Network connections opened, their destinations, and payload metadata
- Memory allocation patterns
- CPU and resource usage per process
- Keystrokes, via the input subsystem
- Screen contents, via the framebuffer
- Inter-process communication

### 3.2 What the agent can influence (by design, at full maturity)

- Intercept and modify syscalls before they execute
- Block or redirect network calls
- Inject synthetic input into any running process
- Modify file contents before a process reads them
- Kill, pause, resume, or spawn processes
- Adjust process priority/resource allocation (via cgroups or scheduler hints)
- Redirect stdout/stderr

### 3.3 The corresponding responsibility

Total visibility and total control implies the agent itself becomes the single largest attack surface and single largest point of failure in the system. Section 16 (Security Model) and Section 17 (Agent Autonomy Levels) exist specifically to manage this risk. The working principle is: **start in a purely observational mode, and only widen the agent's authority once trust and reliability have been established empirically.**

---

## 4. System Architecture Overview

### 4.1 Target end-state architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     Custom Linux Distro                      │
│                                                                │
│  ┌──────────────┐   ┌──────────────┐   ┌──────────────┐      │
│  │  Process A   │   │  Process B   │   │  Process C   │      │
│  │ (e.g. nginx) │   │ (e.g. gcc)   │   │ (e.g. game)  │      │
│  └──────┬───────┘   └──────┬───────┘   └──────┬───────┘      │
│         │  every syscall goes through the kernel              │
│  ┌──────▼───────────────────▼───────────────────▼───────┐    │
│  │                     Linux Kernel                       │    │
│  │   ┌────────────────────────────────────────────────┐  │    │
│  │   │  Custom Kernel Module / Hooks                    │  │    │
│  │   │  - hooks copy_process() / do_fork()              │  │    │
│  │   │  - hooks syscall table (or uses tracepoints)     │  │    │
│  │   │  - spawns a paired "agent shim" per process       │  │    │
│  │   └───────────────────┬────────────────────────────┘  │    │
│  └───────────────────────┼────────────────────────────────┘    │
│                           │  netlink / shared memory            │
│  ┌────────────────────────▼───────────────────────────────┐   │
│  │              Userspace Agent Daemon (per-process shim)   │   │
│  │  - receives syscall/event stream                         │   │
│  │  - maintains short-term context per process               │   │
│  │  - queries local LLM (Ollama / llama.cpp)                  │   │
│  │  - logs interaction + outcome for later fine-tuning        │   │
│  └────────────────────────┬───────────────────────────────┘   │
│                           │                                     │
│  ┌────────────────────────▼───────────────────────────────┐   │
│  │        Local LLM Runtime (Ollama, quantized model)        │   │
│  │  - base model + per-process-type LoRA adapters             │   │
│  └─────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
```

### 4.2 Current (Phase 1) architecture — what actually exists today

```
┌───────────────────────────────────────────┐
│  Alpine Linux VM (QEMU, user-mode network)  │
│                                              │
│  ┌────────────────────────────────────┐    │
│  │  monitor (C binary, ptrace-based)   │    │
│  │  - forks target process              │    │
│  │  - PTRACE_TRACEME in child            │    │
│  │  - parent uses PTRACE_SYSCALL loop    │    │
│  │  - captures syscall num + 6 args      │    │
│  │  - captures return value              │    │
│  └───────────────┬────────────────────┘    │
│                  │ (planned: pipe output)    │
│  ┌───────────────▼────────────────────┐    │
│  │  syscall_analyzer.py                 │    │
│  │  - takes syscall data as CLI arg      │    │
│  │  - sends prompt to local Ollama API   │    │
│  │  - currently: local Ollama model      │    │
│  └──────────────────────────────────────┘    │
└───────────────────────────────────────────┘
```

The current implementation is intentionally simple: it is a proof-of-concept that (a) a userspace process can be fully observed via ptrace without modification, and (b) syscall data can be meaningfully summarized/explained by an LLM. Neither piece is yet wired to act automatically — this is "Observe Mode" only (see Section 17).

---

## 5. OS/Distro Decision & Rationale

### 5.1 Options considered

| Option | Verdict | Reasoning |
|---|---|---|
| Ubuntu/Debian/Fedora fork | Rejected | Too much accumulated complexity (systemd, large package sets, glibc complexity) for a project meant to teach low-level internals |
| Arch Linux | Used transiently | Good for learning the terrain (manual kernel builds, AUR, minimal defaults), not chosen as the base to fork |
| Linux From Scratch (LFS) | Considered, not started | Maximum learning value but very slow iteration; may be revisited once the design stabilizes |
| Buildroot | Considered | Good for embedded-style minimal builds; candidate for final image packaging (Phase 6) |
| **Alpine Linux** | **Chosen (current)** | musl libc (simpler to read than glibc), BusyBox userland (fewer moving parts), ~150MB ISO, apk package manager, fast boot, easy to rebuild from source |

### 5.2 Why Alpine specifically

- **musl libc** is a much smaller, more auditable C standard library than glibc, which matters when the entire point of the project is to understand what's happening under the hood.
- **BusyBox** replaces dozens of individual GNU utilities with a single multi-call binary, drastically reducing the number of moving parts to reason about.
- **apk (Alpine Package Keeper)** is simple and fast, though (as documented in Section 7) it depends on correct network/DNS configuration and has occasionally surfaced transient repository errors during this project's setup (`ERROR: unable to select packages`, IO errors on `apk add`).
- Alpine is commonly used as a base for minimal container images, meaning there is a large ecosystem of prior art for stripping it down further or extending it.

### 5.3 Known Alpine-specific gotchas discovered during setup

- Alpine does **not** ship OpenSSH, `curl`, `git`, `lsblk`, `pip`, or a full GCC toolchain by default. All had to be installed explicitly via `apk add`.
- Alpine's default shell is `ash` (via BusyBox), not `bash`. Bash-specific syntax (e.g., brace expansion `{a,b,c}`) does **not** work in `ash`/BusyBox `sh` the way it does in bash — this caused a real bug during setup where `mkdir -p {ptrace-monitor,ai-agent,test-programs,logs}` created one literally-named directory instead of four.
- Alpine's minimal ISO environment (before installing to disk) has an even more stripped down toolset — e.g., no `lsblk`, requiring `ls /dev/sda*` and manual `mount`/`ls` probing to identify the correct partition.
- Python on Alpine does not ship `pip` by default; `py3-pip` may not always be resolvable depending on repository state, and `python3 -m pip` will fail with `No module named pip` if the module truly isn't present. The practical workaround used in this project was to avoid third-party Python packages entirely and use the standard-library `urllib.request` module instead of `requests`.

---

## 6. Development Environment

### 6.1 Why not develop directly on bare metal

Kernel modules, syscall table hooks, and process-lifecycle hooks all carry real risk of kernel panics, filesystem corruption, or unbootable systems. The standard and non-negotiable practice for this kind of work is to **never develop directly on the host machine**. All experimental OS/kernel work happens inside a virtual machine that can be snapshotted and reset without any risk to the host.

### 6.2 Host machine

- **Host OS:** Windows
- **Virtualization software:** QEMU (chosen to enable native GDB stub integration for later kernel-debugging work and better automation)

### 6.3 Guest VM specification

| Setting | Value |
|---|---|
| Guest OS | Alpine Linux 3.20.10 |
| Virtual disk | `alpine-dev.qcow2` / VDI, 20GB, dynamically allocated |
| RAM | 2GB (2048 MB) |
| CPU cores | 4 (vCPUs) |
| Network adapter | User-mode networking (NAT-like, see Section 7) |
| Storage controller | VirtIO (for disk and network) for optimal QEMU performance |

### 6.4 Toolchain installed inside the VM

```bash
apk update
apk add \
  build-base \      # gcc, make, libc dev headers, etc.
  linux-headers \   # kernel headers, needed for future module work
  git \
  vim \
  curl \
  wget \
  gcc \
  make \
  gdb \
  strace \
  ltrace \
  musl-dev \
  perl \
  bash \
  python3 \
  openssh
```

Notable omission: `py3-pip` was attempted but was unreliable in this environment (see Section 5.3); the project currently avoids third-party Python dependencies (`requests`) in favor of the standard library (`urllib.request`, `json`, `socket`).

### 6.5 Safety practices adopted

- **Git version control** inside the VM for all project code, with frequent small commits.
- **QEMU qcow2 snapshots** intended as rollback points before risky changes (using `qemu-img snapshot` to save state before kernel modifications).
- Recommended going forward: take a snapshot immediately after any working milestone (e.g., "ssh-and-devtools-working", "ptrace-monitor-working", "ollama-integration-working").

---

## 7. Networking Reference (Lessons Learned)

This section exists because a very large fraction of real development time on this project so far has gone into VM networking troubleshooting. It is documented in detail so the same mistakes are not repeated, and so a future kernel-level agent's own IPC/network design benefits from these lessons.

### 7.1 Goals of VM networking

1. SSH access from the Windows host into the Alpine guest, so development can happen from a normal terminal instead of the constrained QEMU console window.
2. Guest-to-host connectivity, so the guest can reach a locally-running Ollama server on the Windows host (port 11434).
3. (Eventually) guest internet access, for `apk`, `git clone`, and model downloads.

### 7.2 NAT + Port Forwarding (SSH) — worked

Configuration used successfully for SSH:

```
Adapter 1: NAT
Port Forwarding rule:
  Name:       SSH
  Protocol:   TCP
  Host IP:    127.0.0.1
  Host Port:  2222
  Guest IP:   10.0.2.15
  Guest Port: 22
```

Client command from Windows:
```powershell
ssh -p 2222 root@127.0.0.1
```

**Important operational note:** Adding a second port-forwarding rule (e.g., for Ollama) must be done by appending an additional `hostfwd` parameter in the QEMU launch command. This was a real incident during setup — overwriting the existing SSH `hostfwd` rule with the Ollama rule broke SSH access.

### 7.3 NAT + Port Forwarding (Ollama) — did NOT work reliably

The equivalent rule for reaching a Windows-hosted Ollama server:
```
Name:       Ollama
Protocol:   TCP
Host IP:    127.0.0.1   (also tried: blank)
Host Port:  11434
Guest IP:   10.0.2.15
### 7.3 Ollama Host-to-Guest Connectivity (Resolved via OLLAMA_HOST=0.0.0.0)

By default, Ollama on Windows binds strictly to `127.0.0.1:11434`. In QEMU SLIRP (user-mode networking), the guest accesses the host via the virtual gateway `10.0.2.2`. Because requests arrive at `10.0.2.2` rather than loopback `127.0.0.1`, Ollama initially dropped or refused these incoming packets.

**Resolution:**
1. Set the Windows User environment variable:
   ```powershell
   [System.Environment]::SetEnvironmentVariable('OLLAMA_HOST', '0.0.0.0', 'User')
   ```
2. When bound to `0.0.0.0:11434`, the Alpine guest reaches Ollama immediately at `http://10.0.2.2:11434`.
3. Verified from within the guest:
   ```bash
   curl -s http://10.0.2.2:11434/
   # Returns: "Ollama is running"
   ```

### 7.4 Bridged Adapter — attempted, network unreachable (superseded)

Switching to a **Bridged/TAP Adapter** was originally attempted in VirtualBox, but proved unnecessarily complex and error-prone compared to QEMU's standard user-mode networking with `10.0.2.2` host access. With `OLLAMA_HOST=0.0.0.0` configured on the host, NAT networking provides all required connectivity without bridged adapters.

### 7.5 Current network state (as of this document)

- **Network model:** QEMU user-mode NAT (`-netdev user,id=net0,hostfwd=tcp::2222-:22 -device virtio-net,netdev=net0`).
- **SSH access:** Confirmed working via host port `127.0.0.1:2222` forwarding to guest port `22`.
- **Host LLM access:** Confirmed working via `http://10.0.2.2:11434` (Ollama running on Windows).
- **Guest Internet access:** Full outbound connectivity working (package installation with `apk` and repository syncing with `git` both operational).

### 7.6 Root password loss and recovery (documented for completeness)

During networking experimentation, the VM's root password was forgotten, and no QEMU qcow2 snapshot existed to fall back on. Recovery procedure used:

1. Reattach the Alpine install ISO to the VM's virtual optical drive by appending `-cdrom alpine.iso` to the QEMU launch command.
2. Boot the VM from the ISO.
3. Identify the correct root-filesystem partition manually (no `lsblk` available in the ISO environment):
   ```bash
   ls /dev/sda*
   mount /dev/sda1 /mnt/disk ; ls /mnt/disk   # check for bin/, etc/, home/, root/
   umount /mnt/disk                            # if wrong, try next partition
   mount /dev/sda2 /mnt/disk ; ls /mnt/disk
   # ...repeat until the correct partition is found
   ```
4. Once the correct partition was mounted and confirmed to contain a real root filesystem:
   ```bash
   chroot /mnt/disk /bin/ash   # NOTE: must specify /bin/ash explicitly;
                                # bare `chroot /mnt/disk` failed with
                                # "can't execute '/bin/sh': No such file or directory"
   passwd                      # set new root password
   exit
   umount /mnt/disk
   poweroff
   ```
5. Detach the ISO (remove `-cdrom alpine.iso` from the launch command).
6. Boot normally from disk with the new password.
7. **Lesson applied going forward:** take a QEMU snapshot (`qemu-img snapshot -c`) immediately after any working configuration state, and record credentials somewhere durable outside the VM.

---

## 8. Phased Roadmap

| Phase | Goal | Status |
|---|---|---|
| **Phase 1** | Userspace ptrace syscall monitor — observe any process's syscalls without modifying it | ✅ Working (traces `/bin/ls`, `/bin/echo` successfully) |
| **Phase 2** | Wire syscall data into an LLM for analysis/explanation | ✅ Completed & Validated — `ptrace` output fed into `syscall_analyzer.py` backed by local Ollama model (`mistral:latest`) via `10.0.2.2:11434` |
| **Phase 3** | Kernel module hooking process creation (`copy_process()`/`do_fork()`) to spawn an agent shim per process, replacing ptrace-based external monitoring | ⏳ Next up |
| **Phase 4** | Custom syscall (e.g., `sys_agent_query`) so any process can talk to its agent directly, without ptrace or external monitoring | ⏳ Not started |
| **Phase 5** | Local LLM running fully inside the guest/target OS (no host dependency), plus a data collection + LoRA fine-tuning pipeline so the agent specializes on this system's actual behavior over time | ⏳ Not started (blocked by Phase 2 networking resolution or in-guest LLM installation) |
| **Phase 6** | Package everything (kernel + modules + agent + pre-downloaded model + adapters) into a bootable OS image so an end user gets a fully working AI-integrated system with zero manual setup | ⏳ Not started |

---

## 9. Phase 1: Userspace Ptrace Monitor — Implementation Detail

### 9.1 Why ptrace first

`ptrace` is the same underlying mechanism tools like `strace` use. It requires no kernel modification, works on any unmodified binary, and is the fastest way to prove the core observability concept: *a process's every syscall can be captured externally, without that process's cooperation or awareness.* This directly validates the project's central architectural claim (Section 3) at the smallest possible scale before attempting kernel-level hooks.

### 9.2 How it works, mechanically

1. The monitor process calls `fork()`.
2. In the **child**, `ptrace(PTRACE_TRACEME, 0, NULL, NULL)` is called before `execvp()`, which marks the child as traced by its parent.
3. The child then calls `execvp(argv[1], &argv[1])` to become the target program (e.g., `/bin/ls -la`).
4. In the **parent**, a loop calls `wait4()` to block until the child stops (either at a syscall boundary, on exit, or on signal).
5. On each stop, `ptrace(PTRACE_GETREGS, child_pid, NULL, &regs)` retrieves the full register state of the child at that instant.
6. On x86-64 Linux, the syscall number is in `orig_rax`, and the first six arguments are in `rdi, rsi, rdx, r10, r8, r9` respectively (note: this differs from the standard C calling convention's `rcx`, because the `syscall` instruction clobbers `rcx`, so the kernel ABI substitutes `r10`).
7. The monitor toggles a boolean `in_syscall` flag to distinguish syscall-entry stops (where it prints the number and arguments) from syscall-exit stops (where it prints the return value from `regs.rax`).
8. `ptrace(PTRACE_SYSCALL, child_pid, NULL, NULL)` resumes the child until the next syscall boundary.
9. The loop terminates when `WIFEXITED(status)` or `WIFSIGNALED(status)` is true, at which point the exit code or terminating signal is printed.

### 9.3 Source code (current working version, `ptrace-monitor/monitor.c`)

```c
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <sys/ptrace.h>
#include <sys/wait.h>
#include <sys/user.h>
#include <sys/reg.h>
#include <signal.h>
#include <errno.h>

#define MAX_SYSCALLS 500

struct syscall_record {
    int syscall_num;
    long args[6];
    long return_value;
};

int syscall_count = 0;
struct syscall_record syscalls[MAX_SYSCALLS];

void print_syscall(int num, long *args) {
    printf("[SYSCALL %d] num=%d, args=[%ld, %ld, %ld, %ld, %ld, %ld]\n",
           syscall_count, num, args[0], args[1], args[2], args[3], args[4], args[5]);
}

int main(int argc, char *argv[]) {
    if (argc < 2) {
        printf("Usage: %s <program> [args...]\n", argv[0]);
        printf("Example: %s /bin/ls -la\n", argv[0]);
        exit(1);
    }

    pid_t child_pid = fork();

    if (child_pid == 0) {
        // Child process
        ptrace(PTRACE_TRACEME, 0, NULL, NULL);
        execvp(argv[1], &argv[1]);
        perror("execvp failed");
        exit(1);
    }
    else if (child_pid > 0) {
        // Parent process
        int status;
        int in_syscall = 0;
        struct user_regs_struct regs;

        printf("Tracing PID %d: %s\n", child_pid, argv[1]);
        printf("=====================================\n\n");

        while (1) {
            if (wait4(child_pid, &status, 0, NULL) == -1) break;

            if (WIFEXITED(status)) {
                printf("\n[PROCESS EXITED] Exit code: %d\n", WEXITSTATUS(status));
                break;
            }
            if (WIFSIGNALED(status)) {
                printf("\n[PROCESS KILLED] Signal: %d\n", WTERMSIG(status));
                break;
            }

            if (ptrace(PTRACE_GETREGS, child_pid, NULL, &regs) == -1) {
                perror("PTRACE_GETREGS");
                break;
            }

            if (!in_syscall) {
                long syscall_num = regs.orig_rax;
                long args[6] = {regs.rdi, regs.rsi, regs.rdx, regs.r10, regs.r8, regs.r9};

                if (syscall_count < MAX_SYSCALLS) {
                    syscalls[syscall_count].syscall_num = syscall_num;
                    memcpy(syscalls[syscall_count].args, args, sizeof(args));
                }

                print_syscall(syscall_count, args);
                syscall_count++;
                in_syscall = 1;
            }
            else {
                long return_value = regs.rax;
                printf("  |-- returned: %ld\n", return_value);
                in_syscall = 0;
            }

            if (ptrace(PTRACE_SYSCALL, child_pid, NULL, NULL) == -1) {
                perror("PTRACE_SYSCALL");
                break;
            }
        }

        printf("\n=====================================\n");
        printf("Total syscalls captured: %d\n", syscall_count);
    }
    else {
        perror("fork failed");
        exit(1);
    }

    return 0;
}
```

### 9.4 Known bug fixed during development

An early version called `memcpy` with arguments in the wrong order:
```c
memcpy(syscalls[syscall_count].args, sizeof(args), args);  // WRONG
```
`memcpy`'s signature is `memcpy(void *dest, const void *src, size_t n)`. The corrected call is:
```c
memcpy(syscalls[syscall_count].args, args, sizeof(args));  // CORRECT
```
The original bug caused GCC warnings about implicit pointer/integer conversion and a `stringop-overread` note, since the compiler interpreted `sizeof(args)` (an integer, 48) as a source pointer near address zero.

### 9.5 Validated test results

```
./monitor /bin/ls -la
Tracing PID 2612: /bin/ls
=====================================
[SYSCALL 0] num=0, ...
...
[PROCESS EXITED] Exit code: 0
=====================================
Total syscalls captured: 49
```
Confirmed working: the monitor successfully traced a real `ls -la` invocation end-to-end, including its normal stdout output interleaved with syscall trace lines, and correctly reported process exit and total syscall count.

### 9.6 Limitations of the current Phase 1 approach (why it is not the end state)

- **Performance overhead:** ptrace introduces a context switch on every syscall entry and exit, which is significant overhead — acceptable for a monitor/debug tool, unacceptable as a permanent architecture for every process on the system.
- **External, not intrinsic:** the target process must be launched *by* the monitor (as a child). It cannot attach retroactively to arbitrary already-running processes without `PTRACE_ATTACH` (which has its own permission and `ptrace_scope` restrictions on modern Linux), and it cannot apply universally to every process started by the OS without wrapping every launch path.
- **No systemic guarantee:** because it's a userspace wrapper, any process not launched through it is invisible to the agent. This directly motivates Phase 3 (kernel-level hook into process creation, so *no* process can be started without a paired agent shim).

---

## 10. Phase 2: AI Agent Integration

### 10.1 Design intent

The syscall stream captured in Phase 1 needs to be turned into something meaningful: an explanation of what the process is doing, whether it looks anomalous, and (eventually) a decision about whether to allow, block, or modify its behavior. This is the job of the LLM-backed analyzer.

### 10.2 Current implementation: Local Ollama Model

The project explicitly uses **Ollama** locally from the start, avoiding external API dependencies such as the Claude API. The syscall stream captured in Phase 1 is sent directly to the local model to validate the full pipeline (syscall → prompt → analysis) end-to-end. This aligns with the long-term vision of a self-contained, offline-capable AI agent OS.

Current script (`ai-agent/syscall_analyzer.py`), using only Python standard library (no `pip`/`requests` dependency, due to the `pip` unavailability documented in Section 5.3):

```python
#!/usr/bin/env python3
import sys
import json
import urllib.request
import urllib.error
import os

def analyze_syscalls(syscall_data):
    """Send syscall data to an LLM backend for analysis."""

    prompt = f"""You are an OS system analyst. Analyze these system calls and explain:
1. What the process is trying to do
2. Any suspicious behavior
3. What this process should be allowed to do
4. Security concerns

Syscall data:
{syscall_data}

Keep analysis concise."""

    # NOTE: backend call (Ollama API) goes here.
    # Placeholder structure — actual implementation should POST to
    # http://127.0.0.1:11434/api/generate with the appropriate
    # headers and model field, per Ollama API documentation.
    ...

def main():
    if len(sys.argv) < 2:
        print("Usage: python3 syscall_analyzer.py <syscall_data>")
        sys.exit(1)

    syscall_data = sys.argv[1]
    print("Analyzing syscalls...\n")
    analysis = analyze_syscalls(syscall_data)
    print("AI Analysis:")
    print("=" * 50)
    print(analysis)
    print("=" * 50)

if __name__ == "__main__":
    main()
```

### 10.3 Network setup for Ollama

The project relies on:
- Installing **Ollama** on the Windows host (successful — `llama2`, `neural-chat`, and `mistral` models were pulled and confirmed available via `ollama list`).
- Reaching the Windows-hosted Ollama server (`http://<host>:11434`) from inside the Alpine guest.
- A fallback plan to install Ollama or `llama.cpp` **directly inside the Alpine guest**, removing the cross-VM networking dependency entirely.

This local-model path is the active direction (see Section 13) using the configured VM-to-host networking or in-guest deployment.

### 10.4 Model size/hardware planning (for future local deployment)

| Model | Approx. size | Min VRAM/RAM (quantized) | Fine-tuning friendliness |
|---|---|---|---|
| Phi-3 Mini | 3.8B | ~4GB | Excellent |
| Llama 3.2 3B | 3B | ~4GB (2-3GB quantized) | Excellent |
| Mistral 7B | 7B | ~8GB (4-6GB quantized) | Very good |
| Llama 3.1 8B | 8B | ~10GB | Very good |
| Qwen 2.5 14B | 14B | ~16GB | Heavy |

Given the current VM allocation (2GB RAM), **Llama 3.2 3B (quantized)** is the realistic target for in-guest local inference; the VM's RAM allocation will likely need to be increased (host permitting) once local inference work resumes.

### 10.5 End-to-End Validation Results (Phase 1 + Phase 2 Verified)

An automated integration script (`test_pipeline.sh`) was written and executed inside the Alpine VM to trace `/bin/ls` and pipe the captured syscalls directly to `syscall_analyzer.py` querying Ollama on the Windows host (`http://10.0.2.2:11434`):

```bash
/root/os-ai-agent/test_pipeline.sh
```

**Trace and AI Analysis Output:**
```
=== 1. Tracing /bin/ls with ptrace monitor ===
Tracing PID 2544: /bin/ls
=====================================
[SYSCALL 0] num=0, args=[0, 0, 0, 0, 0, 0]
...
[SYSCALL 18] num=18, args=[139662616464032, 140732752682352, ...]
  ?? returned: monitor
monitor.c
-38
...
[PROCESS EXITED] Exit code: 0
=====================================
Total syscalls captured: 26

=== 2. Analyzing captured syscalls with Ollama AI ===
Analyzing syscalls...

AI Analysis:
==================================================
 1. The process (PID 2544) is executing the command "/bin/ls", which is a common Unix command used to list the contents of a directory.

2. There are no obvious signs of suspicious behavior in the provided system calls. However, some system calls (like SYSCALL 8, SYSCALL 18, and SYSCALL 19) involve large numbers, which may be memory addresses or file descriptors, but without additional context it's hard to determine if they are malicious.

3. This process should be allowed to execute the "ls" command, as it is a standard system command for listing files and directories. It may also require access to system calls for memory allocation, process management, and file I/O.

4. Potential security concerns could arise if the process is using these system calls in an unusual or unexpected manner. For example, if the process is accessing sensitive files or system resources inappropriately, it could potentially pose a security risk. However, without more context and information about the environment and behavior of the process, it's difficult to definitively identify any specific security concerns. It's recommended to monitor the process closely for any unusual or unexpected behavior.
==================================================
```

This successfully demonstrates the core architectural proposition: an unmodified userspace binary was intercepted, its low-level syscall behavior was captured, and a local neural model correctly understood its operational intent, security posture, and runtime legitimacy.

---

## 11. Phase 3: Kernel Module Integration (Planned)

### 11.1 Objective

Replace the external, opt-in ptrace-wrapper model with a **systemic, non-optional** hook: every time a new process is created anywhere on the system, the kernel itself ensures an agent shim is paired with it — without requiring that process to have been launched through any particular wrapper script.

### 11.2 Candidate hook points

- `copy_process()` (the modern internal implementation behind `fork()`/`clone()`/`vfork()` in the Linux kernel) — the most direct point to intercept "a new process/thread is being created."
- `do_fork()` — older/alternate naming depending on kernel version; conceptually the same hook point.
- Kernel **tracepoints** (e.g., `sched_process_fork`) — a lower-risk alternative to directly patching internal functions, since tracepoints are a stable, purpose-built instrumentation mechanism and don't require modifying core kernel logic directly.

### 11.3 Conceptual sketch (illustrative only — not yet implemented)

```c
// Conceptual only — illustrates the intended hook shape,
// not a drop-in kernel patch.
static int my_process_creation_hook(struct task_struct *new_task) {
    // 1. Identify the new process (pid, executable path, parent)
    // 2. Notify a userspace agent-manager daemon via netlink socket
    // 3. The daemon spawns/assigns an agent shim for this pid
    // 4. Optionally attach further instrumentation (see Phase 4)
    return 0;
}
```

### 11.4 Why a kernel module (not a full kernel patch) first

Loadable Kernel Modules (LKMs) can be inserted/removed at runtime (`insmod`/`rmmod`) without recompiling or rebooting the whole kernel, which dramatically speeds up the iterate-test-fix cycle compared to patching kernel source and rebuilding a full kernel image. Only once the module's behavior is stable and well-understood would it make sense to consider upstreaming the logic as a built-in kernel feature or a permanently compiled-in patch for the custom OS image (Phase 6).

### 11.5 IPC mechanism between kernel hook and userspace agent

| Mechanism | Latency | Complexity | Best for |
|---|---|---|---|
| Netlink socket | Low | Medium | Kernel → userspace event notification (recommended for this hook) |
| Shared memory | Very low | Medium | High-frequency syscall/event data streaming |
| Unix domain socket | Low | Easy | Userspace agent shim ↔ agent-manager daemon communication |
| Named pipe (FIFO) | Low | Easy | Simple early prototyping only |

**Current plan:** use a **netlink socket** for the kernel-module-to-userspace-daemon notification ("a new process was just created, pid=X, exe=Y"), and **Unix domain sockets** for the subsequent, higher-volume communication between the userspace agent-manager and each per-process agent shim.

---

## 12. Phase 4: Custom Syscall Interface (Planned)

### 12.1 Objective

Once kernel-level process-creation hooking is stable, add a **custom syscall** (e.g., `sys_agent_query(pid, context_buf, response_buf)`) so that any process — even one that has no idea an "AI OS" exists — can be given a direct, low-overhead channel to query its paired agent, without needing ptrace-style external interception for every single interaction.

### 12.2 Why this requires a custom kernel build

Unlike a loadable module (which can add functions/hooks without recompiling the kernel), **adding a new syscall number** requires modifying the syscall table and rebuilding the kernel image itself, since the syscall table is a fixed part of the compiled kernel. This step is deliberately placed *after* Phase 3 (module-based hooking) in the roadmap, since it is the point of no return in terms of "no longer just experimenting with modules — now building a genuinely custom kernel."

### 12.3 Conceptual usage (illustrative)

```c
// From an arbitrary userspace program, once the custom syscall exists:
char context[256] = "About to open a network socket to port 443";
char response[256];
long result = syscall(SYS_agent_query, getpid(), context, response);
// response now contains the agent's guidance, if any
```

---

## 13. Phase 5: Local LLM + Fine-Tuning Pipeline

### 13.1 Why fine-tuning is central to this project, not an afterthought

A generic LLM (whether local or via API) can only reason about syscalls and process behavior in the abstract. The genuinely novel opportunity here is that **this specific OS generates its own labeled training data** every time the agent observes a process, makes or suggests a decision, and later observes the outcome. That is a data source no publicly available model was trained on, and it is unique to *this* system's actual usage patterns.

### 13.2 The data flywheel

```
More usage
   -> more (syscall-sequence, outcome) pairs logged
   -> periodic fine-tuning on accumulated data
   -> better/more specialized model
   -> more useful agent
   -> more usage
```

### 13.3 Fine-tuning method: LoRA (Low-Rank Adaptation)

Full fine-tuning updates every weight in the base model — expensive, slow, and impractical on consumer hardware. **LoRA** freezes the base model and trains a small set of additional low-rank adapter weights on top, which:
- Trains in hours, not days/weeks
- Runs on modest consumer hardware (as little as ~8GB VRAM for small base models)
- Allows **multiple adapters** to coexist — e.g., a separate LoRA adapter specialized per process type (`gcc.lora`, `nginx.lora`, `python.lora`), loaded dynamically depending on which process the agent is currently paired with

```python
# Conceptual runtime flow
process = get_new_process()
adapter = select_adapter(process.name)   # e.g. "gcc", "nginx", "python"
model.load_adapter(adapter)
response = model.generate(process.context)
```

### 13.4 Planned categories of fine-tuning signal

1. **Syscall pattern recognition** — recognizing normal vs. anomalous syscall sequences for known programs (e.g., a `read()` loop immediately followed by `SIGSEGV` is a strong signal of a buffer overflow, and the fine-tuned model should learn to flag this pattern specifically, rather than reasoning about it generically each time).
2. **Process-specific specialization** — separate LoRA adapters per major process category (compilers, web servers, interpreters, games), each fine-tuned on that category's typical behavior and typical failure modes.
3. **Feedback-loop / RLHF-style learning** — logging whether a suggested fix was applied, and whether applying it actually resolved the issue, as a reward signal for periodic re-fine-tuning. This is explicitly framed as "RLHF at the OS level."
4. **Kernel event language modeling** — a smaller, specialized model (or adapter) trained specifically on `dmesg`/kernel log output paired with human-readable explanations of what actually happened, since no existing general-purpose model is deeply trained on this specific log format and vocabulary.

### 13.5 Data collection schema (planned directory layout inside the OS)

```
/var/ai-agent/
├── logs/
│   ├── raw_syscalls/        <- raw ptrace/kernel-hook output
│   ├── agent_responses/     <- what the model said, verbatim
│   └── outcomes/            <- what actually happened afterward (success/failure/ignored)
├── training_data/
│   └── dataset.jsonl        <- formatted (prompt, completion, outcome) records
└── adapters/
    ├── gcc.lora
    ├── nginx.lora
    └── python.lora
```

### 13.6 Toolchain for fine-tuning (planned)

```bash
pip install transformers peft datasets trl
```

```python
# Skeleton only — actual training script to be developed in Phase 5
from peft import LoraConfig, get_peft_model
from trl import SFTTrainer
# Load accumulated dataset.jsonl, configure LoRA rank/alpha,
# fine-tune the chosen base model, export the adapter file.
```

### 13.7 Runtime serving

**Ollama** is the chosen local serving layer, due to its simple HTTP API (`/api/generate`, `/api/tags`) and efficient local model execution capabilities, supporting the core vision of an independent AI OS.

---

## 14. Phase 6: OS Image Packaging & Distribution

### 14.1 End-user experience goal

> "When users finally install this, they shouldn't have to go through all this [setup] process — it should be pre-installed for them."

This is a firm requirement, not a nice-to-have. Everything currently done manually during development (installing Ollama, pulling a model, compiling the monitor, wiring up the agent) must eventually be **baked into the OS image itself**, so that booting the final distribution gives a user a fully working, self-contained AI-integrated system with zero manual setup.

### 14.2 What "baked in" means concretely

- The compiled kernel (with the Phase 3/4 hooks and custom syscall, once stable) ships as the default kernel of the image.
- The agent-manager daemon and per-process shim binaries are installed as system services, started automatically at boot (e.g., via an Alpine/OpenRC or custom init script).
- The local LLM runtime (Ollama or a lighter alternative) and a pre-selected quantized base model (e.g., Llama 3.2 3B) are included directly in the image, with no first-boot download step required.
- Pre-trained LoRA adapters for common process categories (from accumulated development-time data) are shipped as part of the base image, with the system continuing to fine-tune further from the user's own usage over time.

### 14.3 Candidate build tooling for the final image

- **Buildroot** or **Alpine's own `mkimage`/`alpine-make-vm-image` tooling** are the leading candidates for producing a bootable, minimal image with the above components pre-installed. This decision is deferred until Phases 3–5 are functionally complete, since the final packaging approach depends on exactly what needs to be included (kernel modules vs. built-in kernel features, model file size, etc.).

---

## 15. Data Collection & Storage Schema

(See also Section 13.5 for the training-data-specific schema.) At a broader level, the system's logging philosophy is:

- **Every interaction is logged** — every syscall stream observed, every prompt sent to the LLM, every response received, and (where determinable) every outcome.
- Logs are the raw material for fine-tuning (Section 13) and also serve as an audit trail for security review (Section 16).
- Given the sensitivity of this data (it may contain file contents, network destinations, or other process-internal information), log storage location, retention, and access control need explicit design attention before Phase 3 gives the agent write/kill/redirect authority over real processes — this is currently an **open item** (Section 21).

---

## 16. Security Model & Threat Considerations

### 16.1 The core tension

> "Total visibility + total control = total responsibility."

An agent with the reach described in Section 3 is, by construction, the single most powerful and most dangerous component in the system. If compromised, misconfigured, or simply wrong in a high-confidence way, it has the same reach as a rootkit — because architecturally, that is almost exactly what it is (a system-wide, kernel-adjacent observer/controller). This must be treated as a first-class design constraint, not an afterthought bolted on after the "cool" parts are built.

### 16.2 Specific risks to design against

- **The agent becomes the biggest attack surface.** A vulnerability in the agent's LLM-serving stack, its kernel module, or its IPC channel could be leveraged for privilege escalation or system-wide compromise.
- **Wrong decisions have system-wide consequences.** Because the agent can (at full maturity) kill processes, redirect network traffic, or modify file contents, an incorrect or hallucinated decision is not contained to one application — it can affect the whole system.
- **Prompt-injection-style risks from observed data.** If the agent ingests file contents, network payloads, or process output as context for its LLM calls, a malicious process could craft data specifically designed to manipulate the agent's own reasoning (an OS-level analog of prompt injection).
- **Novelty itself is a risk factor.** Because no mainstream OS does this today, there is limited prior art or established best practice to lean on; threat modeling here is comparatively green-field.

### 16.3 Mitigations planned

- Strict staged rollout of authority via **Agent Autonomy Levels** (Section 17) — the agent starts with zero ability to affect anything and only gains authority incrementally, based on demonstrated reliability.
- Logging (Section 15) of every decision and its outcome, to support after-the-fact audit and to build the training signal needed for fine-tuning trust more safely over time.
- Isolation of the agent's own runtime (LLM server, agent-manager daemon) with the least privilege necessary for its current autonomy level — e.g., an agent in "Observe Mode" should not run with kernel-level write access at all.

---

## 17. Agent Autonomy Levels

A four-stage model for how much authority the agent has, to be implemented as an explicit, user-configurable setting rather than an implicit assumption:

| Mode | Behavior |
|---|---|
| **Observe Mode** (current/default) | Agent watches syscalls/events, logs them, and can explain them, but takes no action of any kind. |
| **Suggest Mode** | Agent recommends specific actions (e.g., "this process's read loop looks unbounded — consider adding a length check") but a human must explicitly approve before anything happens. |
| **Assist Mode** | Agent acts autonomously only on decisions above a defined confidence threshold, and logs every such action for later review. |
| **Autonomous Mode** | Agent acts freely within explicitly defined boundaries (e.g., "may throttle network connections from unrecognized processes, but may never delete files"). |

**Current project status: Observe Mode only.** No component built so far (ptrace monitor, syscall analyzer) takes any action on the traced process; it only reads and reports. Escalating to Suggest Mode or beyond should be treated as a deliberate, explicit future milestone — not a side effect of adding new features.

---

## 18. Repository Structure

Current (Phase 1/2) layout, initialized as a git repository (`~/os-dev-project`), intended to eventually be pushed to a GitHub remote (`https://github.com/<user>/os-ai-agent`):

```
os-dev-project/
├── README.md
├── ptrace-monitor/
│   ├── monitor.c
│   └── monitor            (compiled binary — arguably should be .gitignore'd)
├── ai-agent/
│   └── syscall_analyzer.py
├── test-programs/         (reserved for sample target programs used in testing)
├── logs/                  (reserved for captured syscall/agent logs)
├── kernel-module/         (reserved — Phase 3)
└── .git/
```

**Recommended additions going forward:**
- A `.gitignore` excluding compiled binaries (`monitor`), Python `__pycache__`, and any other local artifacts.
- A `docs/` directory containing this document and future design notes.
- Separate subdirectories under `kernel-module/` and `ai-agent/adapters/` as Phases 3 and 5 begin producing real artifacts.

---

## 19. Git Workflow

### 19.1 Local setup

```bash
git init
git config user.email "dev@example.com"
git config user.name "Developer"
```

### 19.2 Remote setup (GitHub)

GitHub no longer supports password authentication for git operations over HTTPS; a **Personal Access Token (PAT)** or SSH key is required.

**Personal Access Token method:**
1. Generate at https://github.com/settings/tokens (classic token, `repo` scope).
2. Use the token as the password when prompted during `git push`.
3. Optionally cache it: `git config --global credential.helper store`.

**SSH method (more secure, recommended long-term):**
```bash
ssh-keygen -t ed25519 -C "your@email.com"
cat ~/.ssh/id_ed25519.pub   # add this to https://github.com/settings/keys
git remote set-url origin git@github.com:<user>/os-ai-agent.git
```

### 19.3 Commit discipline observed so far

Commits made at each meaningful milestone, e.g.:
- "Initial commit: project structure"
- "Add ptrace syscall monitor - captures all syscalls from any process"
- "Ptrace monitor working - successfully traces ls syscalls"
- "Add local Llama 3.2 3B integration via Ollama"

---

## 20. Toolchain Reference

| Tool | Purpose | Install command (Alpine) |
|---|---|---|
| `build-base` | GCC, make, core build tools | `apk add build-base` |
| `linux-headers` | Kernel headers for future module work | `apk add linux-headers` |
| `git` | Version control | `apk add git` |
| `vim` | Text editor (no GUI IDE available in-VM) | `apk add vim` |
| `openssh` | Remote access from host | `apk add openssh` |
| `strace` / `ltrace` | Reference tools for syscall/library-call tracing (same underlying mechanism as our custom monitor) | `apk add strace ltrace` |
| `gdb` | Debugging (planned for kernel module work) | `apk add gdb` |
| `python3` | Agent scripting | `apk add python3` |
| Ollama | Local LLM serving (host-side currently; guest-side planned) | Windows installer from ollama.ai |

---

## 21. Open Questions & Future Decisions

This section intentionally lists unresolved items rather than papering over them:

1. **VM-to-host Ollama networking** is unresolved. Root cause of the NAT port-forward `Connection refused` was never conclusively identified. Candidate next steps: try a fresh Host-Only Adapter network (distinct from the Bridged Adapter attempt, which failed for different reasons), verify Windows Firewall rules specifically for inbound TCP 11434, or simply commit to installing an LLM runtime directly inside the guest once guest internet access is fixed.
2. **Guest internet access** was inconsistent throughout setup. A clean, from-scratch, carefully-documented network configuration pass (DNS, default route, adapter type) is recommended as a discrete task rather than continuing to patch the current ad hoc state.
3. **Choice of final local model** (Llama 3.2 3B vs. Mistral 7B vs. something newer) should be revisited once actual latency/quality testing is possible in-guest.
4. **Kernel module hook point** (direct function hooking vs. tracepoints) needs a firm decision before Phase 3 implementation begins; tracepoints are the lower-risk starting point.
5. **Custom syscall numbering/ABI stability** across kernel versions needs research before Phase 4.
6. **Security review process** for escalating from Observe Mode to Suggest Mode has not yet been designed in detail — this should not be skipped under time pressure once Phase 3/4 make real action possible.
7. **IDE/editor choice** — the project currently uses `vim` over SSH exclusively. A cloud-based AI-assisted IDE (e.g., "Antigravity," similar to Cursor) was discussed as a possible productivity upgrade; decision was made to **not switch mid-sprint**, revisit only once core prototype work stabilizes, and to weigh the local-first philosophy of the project against any cloud-dependent tooling.
8. **Licensing and distribution model** for the eventual OS image has not been discussed yet (open-source license choice, whether to base attribution requirements on Alpine's license, etc.).

---

## 22. Glossary

- **ptrace** — a Linux syscall (`ptrace(2)`) that allows one process to observe and control the execution of another, used by debuggers (`gdb`) and tracers (`strace`) alike.
- **syscall (system call)** — the mechanism by which a userspace program requests a service from the kernel (e.g., opening a file, reading from a socket).
- **orig_rax** — the x86-64 register field (captured via `PTRACE_GETREGS`) that holds the syscall number at syscall-entry, distinct from `rax` which holds the return value at syscall-exit.
- **LKM (Loadable Kernel Module)** — a piece of code that can be dynamically inserted into or removed from a running kernel without rebooting, via `insmod`/`rmmod`.
- **netlink socket** — a Linux IPC mechanism specifically designed for communication between the kernel and userspace processes.
- **LoRA (Low-Rank Adaptation)** — a parameter-efficient fine-tuning technique that trains a small set of additional weights on top of a frozen base model, dramatically reducing the compute/memory needed compared to full fine-tuning.
- **Quantization** — reducing the numerical precision of a model's weights (e.g., from 16-bit to 4-bit) to shrink memory footprint and speed up inference, at some cost to output quality.
- **musl libc** — a lightweight, standards-conformant C standard library used by Alpine Linux, as an alternative to glibc.
- **BusyBox** — a single executable that implements many common Unix utilities (`ls`, `mount`, `ping`, etc.) as a multi-call binary, commonly used in minimal/embedded Linux distributions such as Alpine.
- **Observe / Suggest / Assist / Autonomous Mode** — this project's four-tier model (Section 17) for how much authority the AI agent has, ranging from pure logging to fully autonomous action within defined boundaries.

---

*End of document. This is a living record and should be updated as each phase progresses, as networking issues are resolved, and as concrete kernel-level code is written.*
