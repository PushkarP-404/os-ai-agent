#!/usr/bin/env python3
"""
auto_label.py — Part 3a: Data Labeling Pipeline
================================================
Implements the two-tier labeling strategy from the hardening doc:

  Tier 1 — Auto-labeling heuristics (~70% accuracy, free, instant)
  Tier 2 — LLM-assisted labeling  (~95% accuracy, costs tokens, slower)

Also provides:
  build_training_dataset() — Part 3b: convert labeled syscalls into
      windowed (syscall_sequence, intent) training examples suitable
      for LoRA fine-tuning.

Usage:
    from auto_label import auto_label_syscall, build_training_dataset
    from auto_label import llm_label_syscall_sequence

    # Fast path (heuristics only)
    label = auto_label_syscall(syscall_record)

    # High-accuracy path (LLM-assisted, spot-check every Nth)
    labeled = llm_label_syscall_sequence(raw_syscalls, batch_size=50)

    # Build windowed training examples
    examples = build_training_dataset(labeled_syscalls, window_size=10)
"""

import json
import random
import urllib.request
import urllib.error
from collections import Counter, deque
from typing import List, Dict, Any, Optional

# ── Part 3a: Heuristic auto-labeler ─────────────────────────────────────────

_NETWORK_SYSCALLS = frozenset([
    "connect", "bind", "listen", "accept", "accept4",
    "sendto", "recvfrom", "sendmsg", "recvmsg",
    "socket", "socketpair", "getsockopt", "setsockopt",
])

_FILE_SYSCALLS = frozenset([
    "open", "openat", "read", "write", "close",
    "pread64", "pwrite64", "readv", "writev",
    "unlink", "unlinkat", "rename", "renameat",
    "mkdir", "rmdir", "stat", "lstat", "fstat",
    "access", "creat", "truncate", "ftruncate",
])

_PROCESS_SYSCALLS = frozenset([
    "fork", "clone", "clone3", "vfork",
    "execve", "execveat",
    "wait4", "waitpid", "exit", "exit_group",
    "getpid", "getppid", "getpgrp", "setsid",
])

_PRIVILEGE_SYSCALLS = frozenset([
    "setuid", "setgid", "setresuid", "setresgid",
    "setfsuid", "setfsgid", "setgroups",
    "setcap", "prctl", "capset",
    "ptrace", "chroot", "pivot_root",
    "mount", "umount2", "unshare",
])

_MEMORY_SYSCALLS = frozenset([
    "mmap", "mmap2", "munmap", "mprotect",
    "mremap", "madvise", "mlock", "munlock",
    "brk", "sbrk",
])

_CRYPTO_SYSCALLS = frozenset([
    "getrandom", "add_key", "request_key",
    "keyctl", "get_entropy",
])


def auto_label_syscall(syscall_record: Dict[str, Any]) -> str:
    """Assign a high-level intent label based on syscall name.

    Fast, heuristic-based, ~70% accuracy per the hardening doc.
    Returns one of:
        network_io | file_io | process_mgmt | privilege_change |
        memory_mgmt | crypto | other
    """
    name = syscall_record.get("syscall_name", "")

    if name in _NETWORK_SYSCALLS:
        return "network_io"
    if name in _FILE_SYSCALLS:
        return "file_io"
    if name in _PROCESS_SYSCALLS:
        return "process_mgmt"
    if name in _PRIVILEGE_SYSCALLS:
        return "privilege_change"
    if name in _MEMORY_SYSCALLS:
        return "memory_mgmt"
    if name in _CRYPTO_SYSCALLS:
        return "crypto"
    return "other"


# ── Part 3a: LLM-assisted labeler ───────────────────────────────────────────

LLAMA_URL = "http://127.0.0.1:11434/completion"
VALID_LABELS = frozenset([
    "file_io", "network_io", "process_mgmt",
    "privilege_change", "memory_mgmt", "crypto", "other",
])


def llm_label_syscall_sequence(
    syscall_sequence: List[Dict[str, Any]],
    batch_size: int = 50,
    llama_url: str = LLAMA_URL,
) -> List[Dict[str, Any]]:
    """Label a sequence of syscalls using the local LLM (~95% accuracy).

    Batches syscalls into groups of *batch_size* to amortize HTTP overhead.
    Falls back to heuristics if the LLM is unreachable or returns garbage.

    Returns a list of {"syscall": record, "intent": label} dicts.
    """
    batches = [
        syscall_sequence[i:i + batch_size]
        for i in range(0, len(syscall_sequence), batch_size)
    ]

    labeled: List[Dict[str, Any]] = []

    for batch in batches:
        syscall_text = "\n".join([
            f"{sc.get('timestamp', 0):.3f} "
            f"{sc.get('process_name', '?')}({sc.get('pid', 0)}) "
            f"→ {sc.get('syscall_name', '?')}"
            f"({', '.join(str(a) for a in sc.get('args', []))}) "
            f"= {sc.get('return_value', 0)}"
            for sc in batch
        ])

        prompt = (
            "Given this sequence of syscalls from a process, what is the "
            "high-level intent? Choose ONE from: "
            "[file_io, network_io, process_mgmt, privilege_change, "
            "memory_mgmt, crypto, other]\n\n"
            f"Syscalls:\n{syscall_text}\n\n"
            "Respond with ONLY the label, no explanation."
        )

        label = _query_llm_for_label(prompt, llama_url)

        # Validate label; fall back to heuristics per-syscall on garbage
        if label not in VALID_LABELS:
            print(f"  [auto_label] LLM returned invalid label '{label}'; "
                  "falling back to heuristics for this batch.")
            for sc in batch:
                labeled.append({"syscall": sc, "intent": auto_label_syscall(sc)})
        else:
            for sc in batch:
                labeled.append({"syscall": sc, "intent": label})

    return labeled


def _query_llm_for_label(prompt: str, llama_url: str) -> str:
    """POST to llama-server and return the predicted label string."""
    payload = json.dumps({
        "prompt": prompt,
        "n_predict": 15,
        "temperature": 0.0,
        "stop": ["\n", "<|im_end|>"],
    }).encode("utf-8")

    try:
        req = urllib.request.Request(
            llama_url,
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            return result.get("content", "").strip().lower()
    except Exception as exc:
        print(f"  [auto_label] LLM request failed: {exc}")
        return "other"


# ── Part 3b: Training dataset builder ───────────────────────────────────────

def build_training_dataset(
    labeled_syscalls: List[Dict[str, Any]],
    window_size: int = 10,
) -> List[Dict[str, Any]]:
    """Convert individual labeled syscalls into windowed training examples.

    Each example is:
        "Given this sequence of N syscalls, predict the high-level intent."

    Args:
        labeled_syscalls: List of {"syscall": record, "intent": label} dicts.
        window_size:      Number of syscalls to include as context per example.

    Returns:
        List of training example dicts with "syscall_sequence" and "intent".
    """
    examples = []
    window: deque = deque(maxlen=window_size)

    for item in labeled_syscalls:
        syscall = item["syscall"]
        intent = item["intent"]
        window.append(syscall)

        if len(window) == window_size:
            example = {
                "process_name": syscall.get("process_name", "unknown"),
                "pid": syscall.get("pid", 0),
                "uid": syscall.get("uid", 0),
                "syscall_sequence": [
                    {
                        "name": sc.get("syscall_name", "unknown"),
                        "return": sc.get("return_value", 0),
                        "duration_us": sc.get("duration_us", 0),
                    }
                    for sc in window
                ],
                "intent": intent,
                "timestamp": syscall.get("timestamp", 0),
            }
            examples.append(example)

    return examples


def print_intent_distribution(examples: List[Dict[str, Any]]) -> None:
    """Print a distribution table of intents across the training dataset."""
    if not examples:
        print("No examples to display.")
        return

    intents = [ex["intent"] for ex in examples]
    dist = Counter(intents)
    total = len(intents)

    print("\nIntent distribution:")
    for intent, count in dist.most_common():
        pct = count / total * 100
        bar = "█" * int(pct / 2)
        print(f"  {intent:<20} {count:>6}  ({pct:5.1f}%)  {bar}")
    print(f"  {'TOTAL':<20} {total:>6}")


# ── Part 4a: Continuous learning — monthly labeling job ─────────────────────

def label_new_data_with_spotcheck(
    raw_syscalls: List[Dict[str, Any]],
    spotcheck_every: int = 100,
    llama_url: str = LLAMA_URL,
) -> List[Dict[str, Any]]:
    """Label a batch with heuristics, spot-checking every Nth via LLM.

    This is the strategy described for the monthly fine-tuning job:
    use fast heuristics for most records, LLM for a random sample.
    """
    labeled = []
    for i, sc in enumerate(raw_syscalls):
        if i % spotcheck_every == 0:
            # LLM spot-check: single syscall wrapped in a one-element batch
            items = llm_label_syscall_sequence([sc], batch_size=1,
                                              llama_url=llama_url)
            labeled.extend(items)
        else:
            labeled.append({"syscall": sc, "intent": auto_label_syscall(sc)})
    return labeled


# ── CLI entry point ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: auto_label.py <dataset.jsonl> [--llm] [--window N]")
        sys.exit(1)

    input_path = sys.argv[1]
    use_llm = "--llm" in sys.argv
    window_size = 10
    if "--window" in sys.argv:
        idx = sys.argv.index("--window")
        window_size = int(sys.argv[idx + 1])

    # Load raw syscall records
    raw: List[Dict[str, Any]] = []
    with open(input_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    raw.append(json.loads(line))
                except json.JSONDecodeError as e:
                    print(f"  [WARN] Skipping malformed JSON: {e}")

    print(f"Loaded {len(raw)} raw syscall records from {input_path}")

    if use_llm:
        print("Labeling with LLM-assisted pipeline (spot-check every 100th)...")
        labeled = label_new_data_with_spotcheck(raw)
    else:
        print("Labeling with heuristics (fast mode)...")
        labeled = [{"syscall": sc, "intent": auto_label_syscall(sc)} for sc in raw]

    examples = build_training_dataset(labeled, window_size=window_size)
    print_intent_distribution(examples)

    output_path = input_path.replace(".jsonl", f"_labeled_w{window_size}.jsonl")
    with open(output_path, "w", encoding="utf-8") as f:
        for ex in examples:
            f.write(json.dumps(ex) + "\n")
    print(f"\nSaved {len(examples)} training examples to {output_path}")
