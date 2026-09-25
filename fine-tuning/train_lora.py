import os
import json
import torch
from datasets import Dataset
from peft import LoraConfig, get_peft_model, TaskType
from transformers import AutoModelForCausalLM, AutoTokenizer
from trl import SFTTrainer, SFTConfig

# Configuration
BASE_MODEL_ID = "HuggingFaceTB/SmolLM2-135M-Instruct"
DATASET_PATH = "dataset.jsonl"  # Make sure you copy this from the VM first!
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
        target_modules=["q_proj", "v_proj"], # Which layers to target in attention
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

if __name__ == "__main__":
    main()
