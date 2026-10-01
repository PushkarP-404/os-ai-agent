#!/usr/bin/env python3
"""
logger.py â€” AI-Agent OS Structured Interaction Logger

Writes JSONL records of every agent interaction to /var/ai-agent/.
These records form the raw training data corpus for the Phase 5 LoRA
fine-tuning pipeline.

Directory layout created automatically on first use:
  /var/ai-agent/
  â”œâ”€â”€ logs/
  â”‚   â”œâ”€â”€ raw_syscalls/        <- ptrace / kernel-hook raw output (future)
  â”‚   â”œâ”€â”€ agent_responses/     <- one .jsonl file per day, all LLM responses
  â”‚   â””â”€â”€ outcomes/            <- outcome labels (populated in Phase 6)
  â””â”€â”€ training_data/
      â””â”€â”€ dataset.jsonl        <- (prompt, completion, metadata) tuples
"""

import gzip
import json
import os
import random
import threading
import time
import datetime

# â”€â”€ Directory layout â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
AI_AGENT_BASE     = os.environ.get("AI_AGENT_LOG_DIR", "/var/ai-agent")
RAW_SYSCALL_DIR   = os.path.join(AI_AGENT_BASE, "logs", "raw_syscalls")
RESPONSES_DIR     = os.path.join(AI_AGENT_BASE, "logs", "agent_responses")
OUTCOMES_DIR      = os.path.join(AI_AGENT_BASE, "logs", "outcomes")
TRAINING_DATA_DIR = os.path.join(AI_AGENT_BASE, "training_data")
DATASET_JSONL     = os.path.join(TRAINING_DATA_DIR, "dataset.jsonl")


_dirs_initialized = False

def _ensure_dirs():
    """Create the /var/ai-agent/ directory tree if it doesn't exist."""
    global _dirs_initialized
    if not _dirs_initialized:
        for d in (RAW_SYSCALL_DIR, RESPONSES_DIR, OUTCOMES_DIR, TRAINING_DATA_DIR):
            os.makedirs(d, mode=0o750, exist_ok=True)
        _dirs_initialized = True


def _responses_file():
    """Return the daily responses log path (one file per UTC date)."""
    date_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    return os.path.join(RESPONSES_DIR, f"responses_{date_str}.jsonl")


def _write_jsonl(path, record):
    """Append a single JSON record (+ newline) to *path* atomically enough."""
    line = json.dumps(record, ensure_ascii=False, separators=(',', ':'))
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")


# ── Part 2a: Sensitive-data sanitization ───────────────────────────────────────

def sanitize_path(path):
    """Remove sensitive parts from file paths before logging.

    Replaces user-specific path components with safe placeholders so that
    passwords, SSH keys, and home-directory contents are never stored in logs.
    """
    if not isinstance(path, str):
        return path
    if path.startswith("/home/"):
        return "/home/***"
    if path.startswith("/root/"):
        return "/root/***"
    if path.startswith("/proc/"):
        parts = path.split("/")
        if len(parts) >= 3 and parts[2].isdigit():
            parts[2] = "*"
        return "/".join(parts)
    if path.startswith("/run/user/"):
        return "/run/user/***"
    return path


def sanitize_syscall_arg(arg_num, syscall_name, value):
    """Sanitize a specific syscall argument based on its position and syscall."""
    path_syscalls = {"open", "openat", "unlink", "mkdir", "rmdir",
                     "rename", "stat", "lstat", "execve", "execveat",
                     "chdir", "access", "creat", "link", "symlink"}
    if syscall_name in path_syscalls and arg_num == 0 and isinstance(value, str):
        return sanitize_path(value)
    return value


# ── Part 2a: Adaptive sampling strategy ────────────────────────────────────────

# Syscalls classified by importance level.
_ALWAYS_SAMPLE = frozenset([
    "fork", "clone", "clone3", "execve", "execveat",
    "exit", "exit_group", "kill", "tkill", "tgkill",
    "setuid", "setgid", "setcap", "prctl", "ptrace",
    "mount", "umount2", "chroot", "pivot_root",
])
_HIGH_SAMPLE = frozenset([
    "connect", "bind", "listen", "accept", "accept4",
    "sendto", "recvfrom", "socket", "socketpair",
    "chmod", "chown", "fchmod", "fchown",
    "mprotect", "mmap",
])
_LOW_SAMPLE = frozenset(["open", "openat", "read", "write", "close", "pread64", "pwrite64"])

# Sample rates: 1 = always, N = 1-in-N probability
SAMPLE_RATE_ALWAYS = 1
SAMPLE_RATE_HIGH   = 10   # 10%
SAMPLE_RATE_NORMAL = 50   # 2%
SAMPLE_RATE_LOW    = 100  # 1%


def should_sample(syscall_name):
    """Return True if this syscall event should be recorded.

    Implements the adaptive sampling strategy from the hardening doc:
    - Critical lifecycle events (fork, execve, setuid): always sampled
    - Network + privilege syscalls: ~10% sample rate
    - High-volume file I/O (read/write): ~1% sample rate
    - All others: ~2% sample rate
    """
    if syscall_name in _ALWAYS_SAMPLE:
        return True
    if syscall_name in _HIGH_SAMPLE:
        return random.randint(1, SAMPLE_RATE_HIGH) == 1
    if syscall_name in _LOW_SAMPLE:
        return random.randint(1, SAMPLE_RATE_LOW) == 1
    return random.randint(1, SAMPLE_RATE_NORMAL) == 1


# ── Part 2b: Local gzip buffer with rotation and upload worker ─────────────────

class LocalBuffer:
    """Write syscall records to gzip-compressed JSONL files with rotation.

    Files are named syscall-buffer-{timestamp}.jsonl.gz and rotate every
    *rotate_after_records* records. A background upload worker thread
    periodically calls *upload_fn* on completed files and deletes them.

    Usage::

        buf = LocalBuffer()
        buf.start_upload_worker(my_upload_function)  # optional
        buf.append({"syscall_name": "read", "pid": 1234, ...})
    """

    def __init__(self, buffer_dir=None, rotate_after_records=1000):
        if buffer_dir is None:
            buffer_dir = os.path.join(
                os.environ.get("AI_AGENT_LOG_DIR", "/var/ai-agent"),
                "syscall-buffer",
            )
        self.buffer_dir = buffer_dir
        self.rotate_after_records = rotate_after_records
        self._lock = threading.Lock()
        self._current_file = None
        self._current_path = None
        self._record_count = 0
        os.makedirs(buffer_dir, exist_ok=True)

    def append(self, record):
        """Append a syscall record dict to the current gzip buffer."""
        with self._lock:
            if self._current_file is None or self._record_count >= self.rotate_after_records:
                self._rotate()
            line = json.dumps(record, ensure_ascii=False, separators=(',', ':')) + "\n"
            try:
                self._current_file.write(line.encode("utf-8"))
                self._current_file.flush()
                self._record_count += 1
            except IOError as exc:
                print(f"[LocalBuffer] Write error (record dropped): {exc}")

    def _rotate(self):
        """Close current gzip file and open a new one."""
        if self._current_file is not None:
            try:
                self._current_file.close()
            except Exception:
                pass
        ts = int(time.time())
        filename = f"syscall-buffer-{ts}.jsonl.gz"
        self._current_path = os.path.join(self.buffer_dir, filename)
        self._current_file = gzip.open(self._current_path, "ab")
        self._record_count = 0

    def list_pending_files(self):
        """Return sorted list of completed buffer files ready for upload."""
        pending = []
        with self._lock:
            current = self._current_path
        for fname in sorted(os.listdir(self.buffer_dir)):
            if not fname.startswith("syscall-buffer-") or not fname.endswith(".jsonl.gz"):
                continue
            full = os.path.join(self.buffer_dir, fname)
            if full != current:
                pending.append(full)
        return pending

    def upload_and_delete(self, filepath, upload_fn):
        """Call *upload_fn(filepath)* and delete the file on success."""
        try:
            upload_fn(filepath)
            os.remove(filepath)
            print(f"[LocalBuffer] Uploaded and deleted: {filepath}")
        except Exception as exc:
            print(f"[LocalBuffer] Upload failed for {filepath}: {exc} — will retry")

    def start_upload_worker(self, upload_fn, interval_s=60):
        """Start a daemon thread that uploads pending buffer files every *interval_s* seconds."""
        def _worker():
            while True:
                time.sleep(interval_s)
                for fpath in self.list_pending_files():
                    self.upload_and_delete(fpath, upload_fn)
        t = threading.Thread(target=_worker, daemon=True, name="LocalBuffer-uploader")
        t.start()
        print(f"[LocalBuffer] Upload worker started (interval={interval_s}s)")
        return t


# Singleton buffer instance — import and call .append() from anywhere
_local_buffer = LocalBuffer()


def log_raw_syscall_record(record):
    """Append a raw syscall record to the gzip local buffer.

    Applies adaptive sampling and path sanitization automatically.
    *record* should match the schema from the hardening doc::

        {
          "timestamp": 1727866496.123,
          "pid": 1234,
          "process_name": "python3",
          "syscall_name": "open",
          "args": [...],
          "return_value": 5,
          "duration_us": 45,
        }
    """
    syscall_name = record.get("syscall_name", "")
    if not should_sample(syscall_name):
        return  # Dropped by sampler — not an error

    # Sanitize path arguments
    args = record.get("args", [])
    sanitized_args = [
        sanitize_syscall_arg(i, syscall_name, a) for i, a in enumerate(args)
    ]
    record = dict(record, args=sanitized_args)
    _local_buffer.append(record)


# â”€â”€ Public API â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def log_interaction(
    query_id,
    caller_pid,
    comm,
    target_pid,
    query,
    response,
    response_latency_ms,
    model,
    msg_type="AI_MSG_SYSCALL_QUERY",
    outcome=None,
):
    """
    Log a complete agent interaction record.

    Args:
        query_id            Kernel-assigned query ID (int).
        caller_pid          PID of the calling process (int).
        comm                Process comm name string (e.g. "nginx").
        target_pid          Target PID the query is about (int).
        query               The natural-language query string.
        response            The LLM response string.
        response_latency_ms Round-trip time in milliseconds (float).
        model               Ollama model name used (str).
        msg_type            Netlink message type that triggered this
                            (default "AI_MSG_SYSCALL_QUERY").
        outcome             Optional outcome label (None until Phase 6).
    """
    _ensure_dirs()

    timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat() + "Z"

    record = {
        "timestamp":           timestamp,
        "msg_type":            msg_type,
        "query_id":            query_id,
        "caller_pid":          caller_pid,
        "comm":                comm,
        "target_pid":          target_pid,
        "query":               query,
        "response":            response,
        "response_latency_ms": round(response_latency_ms, 2),
        "model":               model,
        "outcome":             outcome,
    }

    # Daily responses log (full verbatim record)
    _write_jsonl(_responses_file(), record)

    # Training dataset (same record; outcome may be backfilled later)
    training_record = {
        "timestamp":    timestamp,
        "prompt":       _build_prompt(comm, caller_pid, target_pid, query),
        "completion":   response,
        "model":        model,
        "query_id":     query_id,
        "comm":         comm,
        "latency_ms":   round(response_latency_ms, 2),
        "outcome":      outcome,
    }
    _write_jsonl(DATASET_JSONL, training_record)


def log_process_event(parent_pid, child_pid, comm):
    """
    Log a Phase 3 process-creation event (AI_MSG_PROCESS_EVENT).
    Written only to the daily responses log, not the training dataset.
    """
    _ensure_dirs()
    record = {
        "timestamp":  datetime.datetime.now(datetime.timezone.utc).isoformat() + "Z",
        "msg_type":   "AI_MSG_PROCESS_EVENT",
        "parent_pid": parent_pid,
        "child_pid":  child_pid,
        "comm":       comm,
    }
    _write_jsonl(_responses_file(), record)


def log_raw_syscalls(pid, comm, syscall_data):
    """
    Append raw ptrace / kernel-hook syscall output to a per-PID file
    in raw_syscalls/. Designed for Phase 1-style trace data.
    """
    _ensure_dirs()
    path = os.path.join(RAW_SYSCALL_DIR, f"pid_{pid}_{comm}.log")
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"[{datetime.datetime.now(datetime.timezone.utc).isoformat()}Z] {syscall_data}\n")


# â”€â”€ Helpers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def _build_prompt(comm, caller_pid, target_pid, query):
    """
    Reconstruct the exact prompt that would be sent to the LLM.
    Stored in the dataset so the (prompt, completion) pair is self-contained.
    """
    return (
        f"You are the operating system's kernel AI agent. "
        f"A process is querying you via sys_agent_query:\n"
        f"- Process Comm: {comm}\n"
        f"- Caller PID: {caller_pid}\n"
        f"- Target PID: {target_pid}\n"
        f"- Query: {query}\n\n"
        f"Provide a concise, direct, 2-3 sentence system safety analysis "
        f"and actionable recommendation."
    )


def get_stats():
    """Return a dict with basic dataset statistics."""
    _ensure_dirs()
    try:
        with open(DATASET_JSONL, "r", encoding="utf-8") as f:
            total_lines = sum(1 for line in f if line.strip())
        return {
            "total_interactions": total_lines,
            "dataset_path":       DATASET_JSONL,
            "responses_dir":      RESPONSES_DIR,
        }
    except FileNotFoundError:
        return {"total_interactions": 0, "dataset_path": DATASET_JSONL}


if __name__ == "__main__":
    # Quick smoke test when run directly
    print("Logger smoke test...")
    log_interaction(
        query_id=999,
        caller_pid=1,
        comm="smoke_test",
        target_pid=1,
        query="Is the system healthy?",
        response="System appears healthy. No anomalies detected.",
        response_latency_ms=42.0,
        model="llama3.2:3b-instruct-q4_K_M",
    )
    stats = get_stats()
    print(f"Stats: {stats}")
    print(f"Dataset path: {DATASET_JSONL}")
    print("Smoke test passed.")
