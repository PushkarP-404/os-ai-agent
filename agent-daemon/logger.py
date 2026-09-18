#!/usr/bin/env python3
"""
logger.py — AI-Agent OS Structured Interaction Logger

Writes JSONL records of every agent interaction to /var/ai-agent/.
These records form the raw training data corpus for the Phase 5 LoRA
fine-tuning pipeline.

Directory layout created automatically on first use:
  /var/ai-agent/
  ├── logs/
  │   ├── raw_syscalls/        <- ptrace / kernel-hook raw output (future)
  │   ├── agent_responses/     <- one .jsonl file per day, all LLM responses
  │   └── outcomes/            <- outcome labels (populated in Phase 6)
  └── training_data/
      └── dataset.jsonl        <- (prompt, completion, metadata) tuples
"""

import json
import os
import time
import datetime

# ── Directory layout ─────────────────────────────────────────────────────────
AI_AGENT_BASE     = os.environ.get("AI_AGENT_LOG_DIR", "/var/ai-agent")
RAW_SYSCALL_DIR   = os.path.join(AI_AGENT_BASE, "logs", "raw_syscalls")
RESPONSES_DIR     = os.path.join(AI_AGENT_BASE, "logs", "agent_responses")
OUTCOMES_DIR      = os.path.join(AI_AGENT_BASE, "logs", "outcomes")
TRAINING_DATA_DIR = os.path.join(AI_AGENT_BASE, "training_data")
DATASET_JSONL     = os.path.join(TRAINING_DATA_DIR, "dataset.jsonl")


def _ensure_dirs():
    """Create the /var/ai-agent/ directory tree if it doesn't exist."""
    for d in (RAW_SYSCALL_DIR, RESPONSES_DIR, OUTCOMES_DIR, TRAINING_DATA_DIR):
        os.makedirs(d, mode=0o750, exist_ok=True)


def _responses_file():
    """Return the daily responses log path (one file per UTC date)."""
    date_str = datetime.datetime.utcnow().strftime("%Y-%m-%d")
    return os.path.join(RESPONSES_DIR, f"responses_{date_str}.jsonl")


def _write_jsonl(path, record):
    """Append a single JSON record (+ newline) to *path* atomically enough."""
    line = json.dumps(record, ensure_ascii=False, separators=(',', ':'))
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")


# ── Public API ────────────────────────────────────────────────────────────────

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

    timestamp = datetime.datetime.utcnow().isoformat() + "Z"

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
        "timestamp":  datetime.datetime.utcnow().isoformat() + "Z",
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
        f.write(f"[{datetime.datetime.utcnow().isoformat()}Z] {syscall_data}\n")


# ── Helpers ───────────────────────────────────────────────────────────────────

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
            lines = [l for l in f if l.strip()]
        return {
            "total_interactions": len(lines),
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
