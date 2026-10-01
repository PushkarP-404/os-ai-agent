import os
import json
import torch
from datasets import Dataset
from peft import LoraConfig, get_peft_model, TaskType, PeftModel
from transformers import AutoModelForCausalLM, AutoTokenizer
from trl import SFTTrainer, SFTConfig

# Configuration
BASE_MODEL_ID = "HuggingFaceTB/SmolLM2-135M-Instruct"
DATASET_PATH = os.environ.get("DATASET_PATH", "dataset.jsonl")  # Override via env var
OUTPUT_DIR = "./adapters/os_agent_lora"

def load_and_format_dataset(file_path):
    """
    Reads the OS-Agent JSONL log dataset and formats it into prompts
    suitable for fine-tuning.
    """
    formatted_data = {"text": []}
    
    with open(file_path, "r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
                
            entry = json.loads(line)
            prompt = entry.get("prompt", "")
            completion = entry.get("completion", "")
            
            # Format: Instruction (prompt) -> Response (completion)
            # We use ChatML or standard instruction format depending on the base model.
            # SmolLM2 uses standard instruction formats.
            text = f"<|im_start|>user\n{prompt}<|im_end|>\n<|im_start|>assistant\n{completion}<|im_end|>"
            formatted_data["text"].append(text)
            
    return Dataset.from_dict(formatted_data)

def main():
    print(f"Loading dataset from {DATASET_PATH}...")
    if not os.path.exists(DATASET_PATH):
        print(f"ERROR: Dataset not found at {DATASET_PATH}.")
        print("Please pull it from the VM using:")
        print("scp -P 2222 root@127.0.0.1:/var/ai-agent/training_data/dataset.jsonl .")
        return

    dataset = load_and_format_dataset(DATASET_PATH)
    print(f"Loaded {len(dataset)} training examples.")

    print(f"Loading base model: {BASE_MODEL_ID}...")
    tokenizer = AutoTokenizer.from_pretrained(BASE_MODEL_ID)
    tokenizer.pad_token = tokenizer.eos_token

    # Load model (we load the full precision or fp16 version, not the GGUF)
    # The GGUF is only for inference in the VM. We need the original for training.
    model = AutoModelForCausalLM.from_pretrained(
        BASE_MODEL_ID, 
        device_map="auto",
        torch_dtype=torch.float16 if torch.cuda.is_available() else torch.float32
    )

    # Configure LoRA (Low-Rank Adaptation)
    # This tells the trainer to freeze the base model and only train a tiny set of new weights.
    lora_config = LoraConfig(
        r=8,                     # Rank of the adapter (size)
        lora_alpha=16,           # Scaling factor
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"], # Extended target modules for better convergence
        lora_dropout=0.05,
        bias="none",
        task_type=TaskType.CAUSAL_LM
    )

    print("Configuring training arguments...")
    training_args = SFTConfig(
        output_dir=OUTPUT_DIR,
        dataset_text_field="text",
        max_length=512,
        per_device_train_batch_size=2,
        gradient_accumulation_steps=4,
        learning_rate=2e-4,
        logging_steps=5,
        max_steps=50,             # Just 50 steps for a quick prototype fine-tune
        save_steps=25,
        optim="adamw_torch",
        remove_unused_columns=False,
        use_cpu=not torch.cuda.is_available(),
    )

    print("Initializing LoRA Trainer...")
    trainer = SFTTrainer(
        model=model,
        train_dataset=dataset,
        peft_config=lora_config,
        processing_class=tokenizer,
        args=training_args,
    )

    print("Starting fine-tuning...")
    trainer.train()

    print(f"Training complete! Saving LoRA adapter to {OUTPUT_DIR}...")
    trainer.model.save_pretrained(OUTPUT_DIR)
    tokenizer.save_pretrained(OUTPUT_DIR)

    print("\nNext steps:")
    print("1. Convert this LoRA adapter back to GGUF format.")
    print("2. Mount it in the VM alongside the base model.")
    print("3. Tell llama-server to load the adapter dynamically!")


# ── Part 4b: Per-process-type multi-adapter training ─────────────────────────

PROCESS_TYPES_TO_TRAIN = ["nginx", "python3", "postgres", "redis", "node", "sh"]


def multi_adapter_train(
    all_examples_path: str,
    min_examples: int = 100,
    base_model_id: str = BASE_MODEL_ID,
):
    """Train a separate LoRA adapter for each known process type.

    Each adapter specialises the base model for the syscall patterns of a
    specific process (nginx, postgres, etc.), yielding higher accuracy for
    that workload versus the generic adapter.

    Adapters are saved to ./adapters/<process_name>_lora/
    """
    print(f"\n{'='*60}")
    print("  Part 4b: Multi-Adapter Training")
    print(f"{'='*60}")

    # Load all windowed examples
    all_examples = []
    with open(all_examples_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    all_examples.append(json.loads(line))
                except json.JSONDecodeError:
                    pass

    print(f"  Loaded {len(all_examples)} total examples from {all_examples_path}")

    tokenizer = AutoTokenizer.from_pretrained(base_model_id)
    tokenizer.pad_token = tokenizer.eos_token

    for process_name in PROCESS_TYPES_TO_TRAIN:
        process_data = [
            ex for ex in all_examples
            if ex.get("process_name", "") == process_name
        ]

        if len(process_data) < min_examples:
            print(f"  Skipping {process_name:<12} — only {len(process_data)} examples "
                  f"(need ≥ {min_examples})")
            continue

        print(f"\n  Training adapter for '{process_name}' "
              f"({len(process_data)} examples) ...")

        # Format examples for this process type
        formatted = []
        for ex in process_data:
            sc_str = ", ".join([f"{s['name']}→{s['return']}" for s in ex["syscall_sequence"]])
            text = (
                f"<|im_start|>user\n"
                f"Process: {ex['process_name']}\nSyscalls: {sc_str}\n"
                f"What is this process doing?<|im_end|>\n"
                f"<|im_start|>assistant\n{ex['intent']}<|im_end|>"
            )
            formatted.append({"text": text})

        dataset = Dataset.from_dict({"text": [f["text"] for f in formatted]})

        model = AutoModelForCausalLM.from_pretrained(
            base_model_id,
            device_map="auto",
            torch_dtype=torch.float16 if torch.cuda.is_available() else torch.float32,
        )

        lora_config = LoraConfig(
            r=8, lora_alpha=16,
            target_modules=["q_proj", "k_proj", "v_proj", "o_proj",
                            "gate_proj", "up_proj", "down_proj"],
            lora_dropout=0.05, bias="none",
            task_type=TaskType.CAUSAL_LM,
        )

        adapter_dir = os.path.join("./adapters", f"{process_name}_lora")
        args = SFTConfig(
            output_dir=adapter_dir,
            dataset_text_field="text",
            max_length=512,
            per_device_train_batch_size=2,
            gradient_accumulation_steps=4,
            learning_rate=2e-4,
            max_steps=50,
            logging_steps=10,
            optim="adamw_torch",
            remove_unused_columns=False,
            use_cpu=not torch.cuda.is_available(),
        )

        trainer = SFTTrainer(
            model=model,
            train_dataset=dataset,
            peft_config=lora_config,
            processing_class=tokenizer,
            args=args,
        )

        trainer.train()
        trainer.model.save_pretrained(adapter_dir)
        tokenizer.save_pretrained(adapter_dir)
        print(f"  ✓ Saved adapter for '{process_name}' → {adapter_dir}")

        # Free memory before next adapter
        del model, trainer
        if torch.cuda.is_available():
            torch.cuda.empty_cache()


# ── Part 4b: Adapter selector (inference) ─────────────────────────────────────

_loaded_models: dict = {}


def load_adapter(process_name: str, base_model_id: str = BASE_MODEL_ID):
    """Load the best available adapter for *process_name* (with caching).

    Prefers the process-specific adapter; falls back to the generic one.
    """
    if process_name in _loaded_models:
        return _loaded_models[process_name]

    specific_path = os.path.join("./adapters", f"{process_name}_lora")
    generic_path  = os.path.join("./adapters", "os_agent_lora")

    adapter_path = specific_path if os.path.isdir(specific_path) else generic_path

    if not os.path.isdir(adapter_path):
        raise FileNotFoundError(f"No adapter found at {adapter_path}")

    print(f"[load_adapter] Loading {adapter_path} for '{process_name}'")
    tokenizer = AutoTokenizer.from_pretrained(base_model_id)
    tokenizer.pad_token = tokenizer.eos_token

    base = AutoModelForCausalLM.from_pretrained(
        base_model_id,
        device_map="auto",
        torch_dtype=torch.float16 if torch.cuda.is_available() else torch.float32,
    )
    model = PeftModel.from_pretrained(base, adapter_path)
    model.eval()

    _loaded_models[process_name] = (model, tokenizer)
    return model, tokenizer


def query_with_adapter(process_name: str, syscall_sequence: list, max_new_tokens: int = 15) -> str:
    """Run inference using the best adapter for *process_name*.

    Returns the predicted intent string.
    """
    model, tokenizer = load_adapter(process_name)
    sc_str = ", ".join([f"{s['name']}→{s.get('return', 0)}" for s in syscall_sequence])
    prompt = (
        f"<|im_start|>user\n"
        f"Process: {process_name}\nSyscalls: {sc_str}\n"
        f"What is this process doing?<|im_end|>\n"
        f"<|im_start|>assistant\n"
    )
    inputs = tokenizer(prompt, return_tensors="pt").to(model.device)
    with torch.no_grad():
        outputs = model.generate(**inputs, max_new_tokens=max_new_tokens, do_sample=False)
    decoded = tokenizer.decode(outputs[0], skip_special_tokens=True)
    # Extract only the assistant's answer after the prompt
    parts = decoded.split("assistant\n", 1)
    return parts[-1].strip().split("\n")[0].lower() if len(parts) > 1 else decoded.strip()


if __name__ == "__main__":
    main()
