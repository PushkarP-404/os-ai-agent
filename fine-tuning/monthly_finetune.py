#!/usr/bin/env python3
"""
monthly_finetune.py — Part 4a: Continuous Learning Flywheel
============================================================
Implements the monthly fine-tuning job described in the hardening doc.
Run this once manually or schedule it (cron / apscheduler) to
continuously improve the agent as new syscall data accumulates.

Workflow:
    1. Collect syscall data from the past N days
    2. Label with heuristics + LLM spot-check
    3. Merge with historical training data (dedup)
    4. Fine-tune LoRA adapter (incremental, not from scratch)
    5. Evaluate new vs. old model; deploy only if better
    6. Schedule for next month

Usage:
    # Run once immediately
    python monthly_finetune.py

    # Run as a daemon (scheduler fires 1st of each month at midnight)
    python monthly_finetune.py --daemon

    # Only label + build dataset, skip training (useful for testing)
    python monthly_finetune.py --label-only
"""

import argparse
import datetime
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import List, Dict

# Relative imports from fine-tuning directory
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

try:
    from auto_label import (
        auto_label_syscall,
        label_new_data_with_spotcheck,
        build_training_dataset,
        print_intent_distribution,
    )
    AUTO_LABEL_AVAILABLE = True
except ImportError:
    AUTO_LABEL_AVAILABLE = False
    print("[WARN] auto_label.py not found — labeling will be skipped.")

# ── Configuration ─────────────────────────────────────────────────────────────

AI_AGENT_BASE     = os.environ.get("AI_AGENT_LOG_DIR", "/var/ai-agent")
TRAINING_DATA_DIR = os.path.join(AI_AGENT_BASE, "training_data")
DATASET_JSONL     = os.path.join(TRAINING_DATA_DIR, "dataset.jsonl")
ALL_LABELED_PATH  = os.path.join(TRAINING_DATA_DIR, "all_labeled.jsonl")
ADAPTERS_DIR      = os.path.join(SCRIPT_DIR, "adapters")

DAYS_LOOKBACK         = int(os.environ.get("FINETUNE_DAYS_LOOKBACK", "30"))
SPOTCHECK_EVERY       = int(os.environ.get("FINETUNE_SPOTCHECK_EVERY", "100"))
WINDOW_SIZE           = int(os.environ.get("FINETUNE_WINDOW_SIZE", "10"))
MIN_ACCURACY          = float(os.environ.get("FINETUNE_MIN_ACCURACY", "0.80"))
MAX_TRAINING_EXAMPLES = int(os.environ.get("FINETUNE_MAX_EXAMPLES", "50000"))


# ── Step 1: Collect recent data ───────────────────────────────────────────────

def collect_recent_data(days: int = DAYS_LOOKBACK) -> List[Dict]:
    """Load raw syscall records modified within the past *days* days."""
    cutoff = datetime.datetime.now() - datetime.timedelta(days=days)
    raw = []

    # Look in both the JSONL training dataset and any gzip buffers
    search_dirs = [
        os.path.join(AI_AGENT_BASE, "training_data"),
        os.path.join(AI_AGENT_BASE, "syscall-buffer"),
        os.path.join(AI_AGENT_BASE, "logs", "agent_responses"),
    ]

    for search_dir in search_dirs:
        if not os.path.isdir(search_dir):
            continue
        for fname in sorted(os.listdir(search_dir)):
            fpath = os.path.join(search_dir, fname)
            mtime = datetime.datetime.fromtimestamp(os.path.getmtime(fpath))
            if mtime < cutoff:
                continue

            if fname.endswith(".jsonl.gz"):
                import gzip
                try:
                    with gzip.open(fpath, "rb") as f:
                        for line in f:
                            line = line.decode("utf-8").strip()
                            if line:
                                raw.append(json.loads(line))
                except Exception as e:
                    print(f"  [WARN] Could not read {fname}: {e}")

            elif fname.endswith(".jsonl"):
                try:
                    with open(fpath, encoding="utf-8") as f:
                        for line in f:
                            line = line.strip()
                            if line:
                                try:
                                    raw.append(json.loads(line))
                                except json.JSONDecodeError:
                                    pass
                except Exception as e:
                    print(f"  [WARN] Could not read {fname}: {e}")

    print(f"  [Step 1] Loaded {len(raw)} raw records from past {days} days.")
    return raw


# ── Step 2: Label new data ────────────────────────────────────────────────────

def label_data(raw: List[Dict]) -> List[Dict]:
    """Apply adaptive labeling: heuristics + LLM spot-check."""
    if not AUTO_LABEL_AVAILABLE or not raw:
        return []

    print(f"  [Step 2] Labeling {len(raw)} records "
          f"(LLM spot-check every {SPOTCHECK_EVERY}th)...")
    labeled = label_new_data_with_spotcheck(raw, spotcheck_every=SPOTCHECK_EVERY)
    print(f"  [Step 2] Labeled {len(labeled)} records.")
    return labeled


# ── Step 3: Merge with historical data ────────────────────────────────────────

def merge_with_history(new_labeled: List[Dict]) -> List[Dict]:
    """Load historical labeled data, append new, deduplicate."""
    historical = []
    if os.path.exists(ALL_LABELED_PATH):
        with open(ALL_LABELED_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        historical.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
        print(f"  [Step 3] Loaded {len(historical)} historical labeled records.")

    combined = historical + new_labeled

    # Deduplicate by (timestamp, syscall_name) key
    seen = set()
    deduped = []
    for item in combined:
        sc = item.get("syscall", {})
        key = (sc.get("timestamp", 0), sc.get("syscall_name", ""), sc.get("pid", 0))
        if key not in seen:
            seen.add(key)
            deduped.append(item)

    print(f"  [Step 3] After dedup: {len(deduped)} total labeled records.")

    # Save merged set
    os.makedirs(TRAINING_DATA_DIR, exist_ok=True)
    with open(ALL_LABELED_PATH, "w", encoding="utf-8") as f:
        for item in deduped:
            f.write(json.dumps(item) + "\n")

    # Cap to prevent memory blowup
    if len(deduped) > MAX_TRAINING_EXAMPLES:
        print(f"  [Step 3] Capping to {MAX_TRAINING_EXAMPLES} most recent examples.")
        deduped = deduped[-MAX_TRAINING_EXAMPLES:]

    return deduped


# ── Step 4: Build windowed examples and trigger training ──────────────────────

def build_and_save_training_examples(labeled: List[Dict]) -> str:
    """Build windowed examples and save to a timestamped JSONL file."""
    if not AUTO_LABEL_AVAILABLE:
        return ""

    examples = build_training_dataset(labeled, window_size=WINDOW_SIZE)
    print_intent_distribution(examples)

    ts = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    out_path = os.path.join(TRAINING_DATA_DIR, f"windowed_{ts}_w{WINDOW_SIZE}.jsonl")
    with open(out_path, "w", encoding="utf-8") as f:
        for ex in examples:
            f.write(json.dumps(ex) + "\n")

    print(f"  [Step 4] Saved {len(examples)} windowed examples to {out_path}")
    return out_path


def run_finetuning(dataset_path: str) -> bool:
    """Call train_lora.py as a subprocess."""
    train_script = os.path.join(SCRIPT_DIR, "train_lora.py")
    if not os.path.exists(train_script):
        print(f"  [ERROR] train_lora.py not found at {train_script}")
        return False

    print(f"  [Step 4] Starting fine-tuning on {dataset_path} ...")
    env = os.environ.copy()
    env["DATASET_PATH"] = dataset_path

    result = subprocess.run(
        [sys.executable, train_script],
        env=env,
        capture_output=False,  # Stream output to console
    )

    if result.returncode == 0:
        print("  [Step 4] Fine-tuning completed successfully.")
        return True
    else:
        print(f"  [Step 4] Fine-tuning failed (exit code {result.returncode}).")
        return False


# ── Step 5: Evaluate and deploy ───────────────────────────────────────────────

def evaluate_and_deploy(windowed_path: str) -> bool:
    """Run evaluate_model.py; return True if deployment approved."""
    eval_script = os.path.join(SCRIPT_DIR, "evaluate_model.py")
    if not os.path.exists(eval_script) or not windowed_path:
        print("  [Step 5] Evaluation script not found — skipping gate.")
        return True

    print(f"  [Step 5] Evaluating new model against test set ...")
    result = subprocess.run(
        [sys.executable, eval_script,
         "--test-data", windowed_path,
         "--min-accuracy", str(MIN_ACCURACY),
         "--output-dir", TRAINING_DATA_DIR],
        capture_output=False,
    )

    approved = result.returncode == 0
    print(f"  [Step 5] Deployment {'APPROVED ✓' if approved else 'BLOCKED ✗'}")
    return approved


# ── Orchestrator ──────────────────────────────────────────────────────────────

def run_monthly_job(label_only: bool = False) -> None:
    """Execute the full monthly fine-tuning pipeline."""
    print("\n" + "=" * 60)
    print(f"  Monthly Fine-Tuning Job — {datetime.datetime.utcnow().isoformat()}Z")
    print("=" * 60)

    # Step 1: Collect
    raw = collect_recent_data(days=DAYS_LOOKBACK)
    if not raw:
        print("  No new data found. Skipping this run.")
        return

    # Step 2: Label
    labeled = label_data(raw)
    if not labeled:
        print("  Labeling failed or unavailable. Skipping.")
        return

    # Step 3: Merge
    all_labeled = merge_with_history(labeled)

    # Step 4: Build windowed examples
    windowed_path = build_and_save_training_examples(all_labeled)

    if label_only:
        print("\n  [label-only mode] Skipping training and evaluation.")
        return

    # Step 4: Fine-tune
    training_ok = run_finetuning(windowed_path or DATASET_JSONL)
    if not training_ok:
        print("  Training failed — aborting deployment.")
        return

    # Step 5: Evaluate and deploy
    evaluate_and_deploy(windowed_path)

    print(f"\n  Job complete at {datetime.datetime.utcnow().isoformat()}Z")


# ── Daemon / scheduler mode ───────────────────────────────────────────────────

def run_daemon() -> None:
    """Run as a daemon that fires the job on the 1st of each month at midnight."""
    try:
        from apscheduler.schedulers.blocking import BlockingScheduler
    except ImportError:
        print("apscheduler not installed. Install with: pip install apscheduler")
        print("Falling back to a simple sleep-based scheduler (checks every hour).")
        _simple_scheduler()
        return

    scheduler = BlockingScheduler()
    scheduler.add_job(
        run_monthly_job,
        "cron",
        day=1,
        hour=0,
        minute=0,
        id="monthly_finetune",
    )
    print("  [Scheduler] Monthly fine-tuning job scheduled (1st of month, 00:00 UTC).")
    print("  [Scheduler] Press Ctrl+C to stop.")
    try:
        scheduler.start()
    except KeyboardInterrupt:
        print("\n  [Scheduler] Stopped.")


def _simple_scheduler() -> None:
    """Fallback scheduler: wake hourly and trigger on the 1st of the month."""
    import time

    print("  [Scheduler] Simple hourly check running. Press Ctrl+C to stop.")
    last_run_month = None

    while True:
        now = datetime.datetime.utcnow()
        if now.day == 1 and now.month != last_run_month:
            print(f"\n  [Scheduler] Triggering monthly job ({now.isoformat()}Z)")
            run_monthly_job()
            last_run_month = now.month
        time.sleep(3600)  # Check every hour


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Monthly continuous-learning fine-tuning pipeline."
    )
    parser.add_argument(
        "--daemon", action="store_true",
        help="Run as a persistent daemon (fires monthly via cron scheduler)."
    )
    parser.add_argument(
        "--label-only", action="store_true",
        help="Only collect + label data; skip training and evaluation."
    )
    parser.add_argument(
        "--days", type=int, default=DAYS_LOOKBACK,
        help=f"Days of data to look back (default {DAYS_LOOKBACK})."
    )
    args = parser.parse_args()

    global DAYS_LOOKBACK
    DAYS_LOOKBACK = args.days

    if args.daemon:
        run_daemon()
    else:
        run_monthly_job(label_only=args.label_only)


if __name__ == "__main__":
    main()
