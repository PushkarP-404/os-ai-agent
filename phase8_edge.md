
### 16.4 Edge Case Handling & Hardening
During Phase 8 development, we identified and hardened several critical execution edge cases to ensure the daemon doesn't crash or corrupt the kernel subsystem when delegated tasks fail:
1. **Missing Target Software:** If the LLM delegates to a tool that isn't installed (e.g., `{"target_software": "nonexistent_tool"}`), the `FileNotFoundError` is caught by Python, preventing a daemon crash, and returning the exact error text to the kernel.
2. **Infinite Loops / Timeout:** If the target software hangs (e.g., `sleep 15`), a strict 10-second subprocess timeout kills the rogue process and returns `[FAILED] Command timed out` to the user, preventing system lockup.
3. **Non-Zero Exits:** If the command fails (e.g., `ls /nonexistent`), the orchestrator returns the non-zero exit code along with both `STDOUT` and `STDERR` so the user (or LLM) knows exactly why it failed.
4. **Large Outputs & Buffer Overflows:** Syscall #548 restricts the response buffer to 2048 bytes. If a command (e.g., `dmesg`) outputs more than that, the daemon safely truncates the Python string to 2000 characters *before* encoding it into UTF-8. This prevents truncating mid-way through a multi-byte UTF-8 character, which would cause kernel/C-level decode crashes.
5. **Security Implications:** Currently, the daemon executes LLM delegations as `root` without restriction. A malicious prompt (e.g. `rm -rf /`) would execute. This proves the need for the structured "Assist Mode" vs "Suggest Mode" framework outlined in Section 17.

---

*End of document. This is a living record and should be updated as each phase progresses.*
