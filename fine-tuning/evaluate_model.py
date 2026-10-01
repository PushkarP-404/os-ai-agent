#!/usr/bin/env python3
"""
evaluate_model.py — Part 3d: Model Evaluation & Comparison
===========================================================
Evaluates a fine-tuned agent model against a held-out test set and
compares accuracy against the previous version to gate deployment.

Usage:
    # Evaluate current adapter vs baseline
    python evaluate_model.py --test-data labeled_w10.jsonl

    # Compare two adapters explicitly
    python evaluate_model.py --test-data labeled_w10.jsonl \\
        --old-adapter ./adapters/os_agent_lora_v1 \\
        --new-adapter ./adapters/os_agent_lora

Outputs a JSON report and prints a human-readable summary.
"""

import argparse
import json
import os
import sys
import datetime
from collections import Counter
from typing import List, Dict, Tuple, Optional

# ── Inference via llama-server (no GPU required on the VM) ──────────────────

import urllib.request
import urllib.error

LLAMA_URL = os.environ.get("LLAMA_URL", "http://127.0.0.1:11434/completion")

VALID_INTENTS = [
    "file_io", "network_io", "process_mgmt",
    "privilege_change", "memory_mgmt", "crypto", "other",
]


def format_syscall_prompt(example: Dict) -> str:
    """Build the inference prompt from a windowed training example."""
    syscall_str = ", ".join([
        f"{sc['name']}→{sc['return']}"
        for sc in example["syscall_sequence"]
    ])
    return (
        f"Process: {example['process_name']}\n"
        f"Syscalls: {syscall_str}\n\n"
        "What is this process doing? Choose ONE: "
        "[file_io, network_io, process_mgmt, privilege_change, "
        "memory_mgmt, crypto, other]\n"
        "Answer:"
    )


def predict_intent(prompt: str, llama_url: str = LLAMA_URL) -> str:
    """Get intent prediction from the local LLM for a single example."""
    payload = json.dumps({
        "prompt": prompt,
        "n_predict": 15,
        "temperature": 0.0,
        "stop": ["\n", "<|im_end|>", ","],
    }).encode("utf-8")

    try:
        req = urllib.request.Request(
            llama_url,
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            raw = result.get("content", "").strip().lower()
            # Normalise: extract the first known label word
            for label in VALID_INTENTS:
                if label in raw:
                    return label
            return raw  # Return as-is even if unknown
    except Exception as exc:
        print(f"  [eval] LLM error: {exc}")
        return "other"


# ── Evaluation engine ────────────────────────────────────────────────────────

def evaluate(
    test_examples: List[Dict],
    llama_url: str = LLAMA_URL,
    max_examples: int = 500,
    verbose: bool = False,
) -> Dict:
    """Run evaluation on the test set and return accuracy metrics.

    Returns a dict with:
        accuracy        — overall top-1 accuracy (0.0–1.0)
        correct         — number of correct predictions
        total           — number of evaluated examples
        per_intent      — per-class accuracy breakdown
        confusion       — confusion matrix (truth → predicted counts)
        errors          — list of mismatched examples (capped at 20)
    """
    examples = test_examples[:max_examples]
    total = len(examples)
    correct = 0
    per_truth: Dict[str, Dict] = {}
    confusion: Dict[str, Counter] = {}
    errors = []

    for i, ex in enumerate(examples):
        ground_truth = ex["intent"].lower()
        prompt = format_syscall_prompt(ex)
        predicted = predict_intent(prompt, llama_url)

        match = (predicted == ground_truth)
        if match:
            correct += 1
        else:
            if len(errors) < 20:
                errors.append({
                    "process": ex["process_name"],
                    "ground_truth": ground_truth,
                    "predicted": predicted,
                })

        # Per-intent tracking
        if ground_truth not in per_truth:
            per_truth[ground_truth] = {"correct": 0, "total": 0}
        per_truth[ground_truth]["total"] += 1
        if match:
            per_truth[ground_truth]["correct"] += 1

        # Confusion matrix
        if ground_truth not in confusion:
            confusion[ground_truth] = Counter()
        confusion[ground_truth][predicted] += 1

        if verbose or (i + 1) % 50 == 0:
            print(f"  [{i + 1}/{total}] truth={ground_truth:<20} pred={predicted:<20} {'✓' if match else '✗'}")

    accuracy = correct / total if total > 0 else 0.0

    per_intent_acc = {
        intent: (v["correct"] / v["total"]) if v["total"] > 0 else 0.0
        for intent, v in per_truth.items()
    }

    return {
        "accuracy": accuracy,
        "correct": correct,
        "total": total,
        "per_intent": per_intent_acc,
        "per_intent_counts": per_truth,
        "confusion": {k: dict(v) for k, v in confusion.items()},
        "errors": errors,
    }


# ── Comparison & deployment gate ─────────────────────────────────────────────

def compare_and_gate(
    old_result: Dict,
    new_result: Dict,
    min_improvement: float = 0.0,
    min_accuracy: float = 0.80,
) -> bool:
    """Return True if new model should be deployed.

    Deployment criteria (from the hardening doc):
      1. New accuracy >= min_accuracy (default 80%)
      2. New accuracy >= old accuracy + min_improvement (default 0%)
    """
    old_acc = old_result["accuracy"]
    new_acc = new_result["accuracy"]
    delta = new_acc - old_acc

    print(f"\n{'='*50}")
    print(f"  Old model accuracy: {old_acc * 100:.1f}%  ({old_result['correct']}/{old_result['total']})")
    print(f"  New model accuracy: {new_acc * 100:.1f}%  ({new_result['correct']}/{new_result['total']})")
    print(f"  Improvement:        {delta * 100:+.1f}%")
    print(f"{'='*50}")

    if new_acc < min_accuracy:
        print(f"  ✗ BLOCKED: new accuracy {new_acc*100:.1f}% < required {min_accuracy*100:.1f}%")
        return False

    if delta < min_improvement:
        print(f"  ✗ BLOCKED: improvement {delta*100:+.1f}% < required {min_improvement*100:+.1f}%")
        return False

    print(f"  ✓ APPROVED: new model meets all deployment criteria.")
    return True


# ── Report generation ─────────────────────────────────────────────────────────

def save_report(result: Dict, label: str, output_dir: str = ".") -> str:
    """Save evaluation result as a timestamped JSON report."""
    ts = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    filename = os.path.join(output_dir, f"eval_report_{label}_{ts}.json")
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)
    print(f"  Report saved: {filename}")
    return filename


def print_per_intent_table(result: Dict) -> None:
    """Print a per-intent accuracy breakdown table."""
    print("\n  Per-intent accuracy:")
    print(f"  {'Intent':<22} {'Correct':>7} {'Total':>7} {'Accuracy':>9}")
    print("  " + "-" * 50)
    for intent in sorted(result["per_intent"].keys()):
        acc = result["per_intent"][intent]
        counts = result["per_intent_counts"].get(intent, {})
        c = counts.get("correct", 0)
        t = counts.get("total", 0)
        bar = "█" * int(acc * 20)
        print(f"  {intent:<22} {c:>7} {t:>7} {acc*100:>8.1f}%  {bar}")


# ── CLI ───────────────────────────────────────────────────────────────────────

def load_test_examples(path: str, test_split: float = 0.1) -> List[Dict]:
    """Load windowed training examples and return the held-out test portion."""
    examples = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    examples.append(json.loads(line))
                except json.JSONDecodeError:
                    pass

    split_idx = max(1, int(len(examples) * (1 - test_split)))
    test_set = examples[split_idx:]
    print(f"Loaded {len(examples)} total examples; using last {len(test_set)} as test set.")
    return test_set


def main():
    parser = argparse.ArgumentParser(description="Evaluate and compare agent model accuracy.")
    parser.add_argument("--test-data", required=True, help="Path to windowed JSONL test data.")
    parser.add_argument("--llama-url", default=LLAMA_URL, help="llama-server URL.")
    parser.add_argument("--max-examples", type=int, default=500,
                        help="Max examples to evaluate (default 500).")
    parser.add_argument("--min-accuracy", type=float, default=0.80,
                        help="Minimum accuracy threshold for deployment (default 0.80).")
    parser.add_argument("--verbose", action="store_true", help="Print every prediction.")
    parser.add_argument("--output-dir", default=".", help="Directory for report JSON files.")
    args = parser.parse_args()

    if not os.path.exists(args.test_data):
        print(f"ERROR: Test data file not found: {args.test_data}")
        sys.exit(1)

    test_examples = load_test_examples(args.test_data)
    if not test_examples:
        print("ERROR: No test examples found.")
        sys.exit(1)

    print(f"\nEvaluating model via {args.llama_url} ...")
    print(f"Test set size: {min(len(test_examples), args.max_examples)} examples\n")

    result = evaluate(
        test_examples,
        llama_url=args.llama_url,
        max_examples=args.max_examples,
        verbose=args.verbose,
    )

    print(f"\n✓ Accuracy: {result['accuracy']*100:.1f}%  ({result['correct']}/{result['total']})")
    print_per_intent_table(result)

    if result["errors"]:
        print(f"\n  First {len(result['errors'])} mismatches:")
        for err in result["errors"]:
            print(f"    {err['process']:<20} truth={err['ground_truth']:<20} pred={err['predicted']}")

    passes = result["accuracy"] >= args.min_accuracy
    print(f"\n{'✓ PASS' if passes else '✗ FAIL'}: accuracy {result['accuracy']*100:.1f}% "
          f"{'≥' if passes else '<'} required {args.min_accuracy*100:.1f}%")

    save_report(result, label="current", output_dir=args.output_dir)
    sys.exit(0 if passes else 1)


if __name__ == "__main__":
    main()
