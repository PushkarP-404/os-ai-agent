# Testing and Tracing Guide: AI-Agent OS

This guide provides end-to-end instructions for verifying every phase of the AI-Agent OS architecture. It is designed for developers who want to trace the execution flow from the deepest kernel syscalls up to the graphical user dashboard.

---

## Phase 1 & 2: The Kernel Subsystem

**Objective:** Verify that the custom Linux kernel (`6.6.142-ai-agent`) is running and that the `ai_agent` module is successfully intercepting syscalls.

1. **Check the Kernel Version:**
   Run `uname -r`. You should see `6.6.142-ai-agent` or similar.
2. **Verify Syscall Interception:**
   Run the test C program:
   ```bash
   cd /root/os-ai-agent/test-programs
   make
   ./test_syscall_stress
   ```
   *Expected Result:* The program triggers `sys_agent_query`. You should see the kernel `dmesg` ring buffer log the interception.
3. **Trace the Code:** 
   Check `custom-kernel/kernel/ai_agent.c`. Look for the `SYSCALL_DEFINE2(agent_query, ...)` macro. This is the entry point for all agent intents.

---

## Phase 3 & 4: Netlink & The Python Daemon

**Objective:** Verify that the kernel is passing process events and intents to userspace via the Netlink socket.

1. **Stop the background daemon temporarily:**
   ```bash
   sudo rc-service ai-agent stop
   ```
2. **Run the daemon manually to view live output:**
   ```bash
   sudo python3 /usr/local/lib/ai-agent/agent_daemon.py
   ```
3. **Trigger a Process Event (Phase 3):**
   Open a second terminal and run any command (e.g., `ls` or `sh`).
   *Expected Result:* The daemon terminal will output `[KERNEL EVENT] Process created: ls (PID: ...)`.
4. **Trigger an Intent (Phase 4):**
   In the second terminal, run:
   ```bash
   agent-cli "/assist whoami"
   ```
   *Expected Result:* The daemon terminal will receive the intent, process it, and return the result. `agent-cli` will print the LLM's response.
5. **Trace the Code:**
   Review `agent-daemon/agent_daemon.py`. Notice the `socket.AF_NETLINK` binding and the unpacking of `struct.calcsize(QUERY_FORMAT)`.

---

## Phase 5 & 6: The Local LLM Engine (llama.cpp)

**Objective:** Verify that the native musl-compiled `llama-server` is running in the background and serving the GGUF models.

1. **Check the Service Status:**
   ```bash
   rc-service llama-server status
   ```
2. **Test the Inference API Directly:**
   ```bash
   curl http://127.0.0.1:11434/completion -d '{"prompt": "Hello", "n_predict": 10}'
   ```
   *Expected Result:* A JSON response from the local SmolLM2 or Qwen model.
3. **Trace the Code:**
   Review `/etc/init.d/llama-server` (or `agent-daemon/openrc/llama-server`). This script launches the C++ binary as an OpenRC daemon.

---

## Phase 7 & 8: GUI Orchestration & Fine-Tuning

**Objective:** Verify that the agent can control the XFCE desktop via the Chrome DevTools Protocol (CDP) and that fine-tuning scripts are functional.

1. **Test CDP GUI Control:**
   With the XFCE GUI booted, run:
   ```bash
   python3 /root/os-ai-agent/agent-daemon/atspi_dumper.py
   ```
   *Expected Result:* The script will output a JSON representation of the current GUI tree (windows, buttons, text fields) using ATSPI/CDP.
2. **Verify Fine-Tuning Data:**
   Check the `/var/ai-agent/training_data/dataset.jsonl` file. It should contain historical queries and responses that can be fed into `train_lora.py`.

---

## Phase 10: Planner/Worker & eBPF Sensors

**Objective:** Verify the Sentinel Harness architecture. The Planner must break down complex intents, and the Worker must execute them sequentially, relying on capabilities.

1. **Test Delegation-First Logic:**
   Run:
   ```bash
   agent-cli "/assist write a react app"
   ```
2. **Watch the Daemon Logs:**
   Open `/var/log/ai-agent.log`.
   *Expected Result:* The Planner should generate a JSON array of tasks. It should recognize `antigravity` or `code` if installed, bypassing manual shell scripts to delegate to the native IDE.
3. **Test eBPF Sensors:**
   Run the eBPF trace script:
   ```bash
   python3 /root/os-ai-agent/ebpf_sensors/os_state_sensor.py
   ```
   *Expected Result:* When you open a file anywhere in the OS, this script will intercept the `sys_openat` syscall via BCC and print the event.

---

## Phase 11: Sentinel Control Center & Mail Bridge

**Objective:** Verify the graphical Rust/Slint Sentinel dock and remote email triggers.

1. **Boot the GUI VM:**
   From `c:\qemu-alpine` (or `os-ai-agent`), launch QEMU in graphical desktop mode:
   ```powershell
   .\boot.ps1 -Gui
   # or directly:
   powershell -ExecutionPolicy Bypass -File c:\qemu-alpine\boot_gui.ps1
   ```
2. **Launch & Inspect the Rust/Slint Sentinel Dock:**
   The collapsed dock (`--dock`) auto-starts on XFCE login as `aiuser`. You can also press `<Super>Space` / `<Alt>Space`, click **Sentinel Control Center** on the Desktop, or launch from terminal:
   ```bash
   ai-agent-dashboard            # Opens Panel view (560x360)
   ai-agent-dashboard --dock     # Opens Collapsed Dock strip (560x46)
   ai-agent-dashboard --prompt   # Opens Full AI Console (560x580)
   ```
   *Expected Result:* A dark **Obsidian Lumina** window (`elenchus-dashboard`, ~15-16 MB `VmRSS`) displaying CPU/RAM/Disk SVG gauges, active model (`os_planner_lora.gguf`), `ai-agent` / `llama-server` / `lightpanda` `RUN` pills, Intent Bus monitor, and ChatML streaming + `/do <task>` execution via `agent-cli`.
3. **Verify Memory Footprint & Process Health:**
   ```bash
   grep -E 'Name|VmRSS|Threads' /proc/$(pidof elenchus-dashboard)/status
   ```
4. **Test the Mail Bridge:**
   Run the mail bridge manually:
   ```bash
   python3 /root/os-ai-agent/remote-bridges/mail_trigger.py
   ```
   Send an email to your configured `EMAIL_ACCOUNT` with the subject `[AGENT-CMD]` and body `ls -la`. The script will parse the email and execute it natively via `agent-cli`.

---

**End of Trace.** If all tests pass, the AI-Agent OS is fully functional from Kernel Ring 0 to the Remote Cloud.
