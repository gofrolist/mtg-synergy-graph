"""
Fine-tune a small LLM on MTG card tagging using unsloth + LoRA.

Trains on 9.5k card examples to produce a fast, task-specific tagger.
Output: GGUF model importable into Ollama.

Usage:
    python3 finetune.py                              # default: Qwen2.5-3B, 3 epochs
    python3 finetune.py --model unsloth/Qwen2.5-7B   # larger base model
    python3 finetune.py --epochs 5 --lr 2e-4          # custom hyperparams
    python3 finetune.py --export-only                 # export existing checkpoint to GGUF

Requirements:
    conda activate finetune
    # unsloth + dependencies installed via pip
"""

import argparse
import json
import os

# Disable torch.compile (requires nvcc which may not be available)
os.environ["TORCHDYNAMO_DISABLE"] = "1"

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
TRAIN_FILE = os.path.join(DATA_DIR, "train.jsonl")
VAL_FILE = os.path.join(DATA_DIR, "val.jsonl")
OUTPUT_DIR = os.environ.get("FINETUNE_OUTPUT_DIR",
                             os.path.join(os.path.dirname(__file__), "model_output"))


def load_dataset(path: str):
    """Load JSONL dataset."""
    from datasets import Dataset

    examples = []
    with open(path) as f:
        for line in f:
            examples.append(json.loads(line))
    return Dataset.from_list(examples)


def format_chat(example):
    """Format a chat example for the tokenizer."""
    return {"messages": example["messages"]}


def train(
    model_name: str = "unsloth/Qwen2.5-3B-Instruct",
    epochs: int = 3,
    lr: float = 2e-4,
    batch_size: int = 4,
    max_seq_length: int = 2048,
    lora_rank: int = 32,
):
    from unsloth import FastLanguageModel
    from trl import SFTTrainer
    from transformers import TrainingArguments
    from unsloth.chat_templates import get_chat_template

    print(f"Loading base model: {model_name}")
    model, tokenizer = FastLanguageModel.from_pretrained(
        model_name=model_name,
        max_seq_length=max_seq_length,
        dtype=None,  # auto-detect
        load_in_4bit=True,
    )

    # Apply chat template
    model_lower = model_name.lower()
    if "qwen3" in model_lower or "qwen-3" in model_lower:
        template = "qwen-2.5"  # Qwen3 uses same <|im_start|> template
    elif "qwen2" in model_lower or "qwen-2" in model_lower:
        template = "qwen-2.5"
    else:
        template = "chatml"
    tokenizer = get_chat_template(tokenizer, chat_template=template)

    # Add LoRA adapters
    model = FastLanguageModel.get_peft_model(
        model,
        r=lora_rank,
        target_modules=[
            "q_proj", "k_proj", "v_proj", "o_proj",
            "gate_proj", "up_proj", "down_proj",
        ],
        lora_alpha=lora_rank,
        lora_dropout=0,
        bias="none",
        use_gradient_checkpointing="unsloth",
        random_state=42,
    )

    # Load datasets
    print("Loading training data...")
    train_dataset = load_dataset(TRAIN_FILE)
    val_dataset = load_dataset(VAL_FILE)
    print(f"Train: {len(train_dataset)}, Val: {len(val_dataset)}")

    # Format for trainer
    def formatting_func(examples):
        texts = []
        for msgs in examples["messages"]:
            text = tokenizer.apply_chat_template(msgs, tokenize=False, add_generation_prompt=False)
            texts.append(text)
        return {"text": texts}

    train_dataset = train_dataset.map(formatting_func, batched=True, remove_columns=train_dataset.column_names)
    val_dataset = val_dataset.map(formatting_func, batched=True, remove_columns=val_dataset.column_names)

    # Training args
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    training_args = TrainingArguments(
        output_dir=OUTPUT_DIR,
        per_device_train_batch_size=batch_size,
        gradient_accumulation_steps=4,
        num_train_epochs=epochs,
        learning_rate=lr,
        bf16=True,
        logging_steps=25,
        eval_strategy="steps",
        eval_steps=100,
        save_steps=500,
        save_total_limit=2,
        warmup_steps=50,
        weight_decay=0.01,
        lr_scheduler_type="cosine",
        seed=42,
        report_to="none",
    )

    # Trainer
    trainer = SFTTrainer(
        model=model,
        tokenizer=tokenizer,
        train_dataset=train_dataset,
        eval_dataset=val_dataset,
        args=training_args,
        dataset_text_field="text",
        max_seq_length=max_seq_length,
        packing=True,
    )

    # Check for existing checkpoint to resume from
    import glob
    checkpoints = sorted(glob.glob(os.path.join(OUTPUT_DIR, "checkpoint-*")))
    resume_from = checkpoints[-1] if checkpoints else None

    print(f"\nStarting training: {epochs} epochs, lr={lr}, batch={batch_size}")
    print(f"LoRA rank: {lora_rank}, max_seq: {max_seq_length}")
    if resume_from:
        print(f"Resuming from {resume_from}")
    trainer.train(resume_from_checkpoint=resume_from)

    # Save
    print(f"\nSaving model to {OUTPUT_DIR}")
    model.save_pretrained(OUTPUT_DIR)
    tokenizer.save_pretrained(OUTPUT_DIR)

    return model, tokenizer


def export_gguf(model=None, tokenizer=None, model_name: str = "unsloth/Qwen2.5-3B-Instruct", quant: str = "q4_k_m"):
    """Export fine-tuned model to GGUF for Ollama."""
    if model is None:
        from unsloth import FastLanguageModel
        print(f"Loading fine-tuned model from {OUTPUT_DIR}")
        model, tokenizer = FastLanguageModel.from_pretrained(
            model_name=OUTPUT_DIR,
            max_seq_length=2048,
            dtype=None,
            load_in_4bit=True,
        )

    gguf_dir = os.path.join(OUTPUT_DIR, "gguf")
    os.makedirs(gguf_dir, exist_ok=True)

    print(f"Exporting to GGUF ({quant})...")
    model.save_pretrained_gguf(
        gguf_dir,
        tokenizer,
        quantization_method=quant,
    )

    # Find the GGUF file
    gguf_files = [f for f in os.listdir(gguf_dir) if f.endswith(".gguf")]
    if gguf_files:
        gguf_path = os.path.join(gguf_dir, gguf_files[0])
        print(f"\nGGUF model: {gguf_path}")
        size_mb = os.path.getsize(gguf_path) / 1024 / 1024
        print(f"Size: {size_mb:.0f}MB")

        # Generate Ollama Modelfile
        modelfile = os.path.join(gguf_dir, "Modelfile")
        with open(modelfile, "w") as f:
            f.write(f'FROM ./{gguf_files[0]}\n')
            f.write('PARAMETER temperature 0.1\n')
            f.write('PARAMETER num_ctx 2048\n')
            f.write(f'SYSTEM """{SYSTEM_PROMPT}"""\n')
        print(f"Modelfile: {modelfile}")
        print(f"\nTo import into Ollama:")
        print(f"  cd {gguf_dir}")
        print(f"  ollama create mtg-tagger -f Modelfile")
        print(f"\nThen use: python3 batch_tagger.py --provider ollama --model mtg-tagger")

    return gguf_dir


SYSTEM_PROMPT = """You are an MTG card analyst. Analyze the card and return JSON with:
- role: one of enabler, threat, draw, removal, ramp, utility, protection, land
- provides: what this card gives (kebab-case tags, e.g. token-generation, counter-placement)
- wants: what conditions make this card better (e.g. creature-etb, attack-events)

Return ONLY valid JSON. No explanation."""


def main():
    parser = argparse.ArgumentParser(description="Fine-tune MTG card tagger")
    parser.add_argument("--model", default="unsloth/Qwen3-4B-Instruct-2507-unsloth-bnb-4bit",
                        help="Base model (default: Qwen3-4B-Instruct)")
    parser.add_argument("--epochs", type=int, default=3, help="Training epochs")
    parser.add_argument("--lr", type=float, default=2e-4, help="Learning rate")
    parser.add_argument("--batch-size", type=int, default=4, help="Batch size")
    parser.add_argument("--lora-rank", type=int, default=32, help="LoRA rank")
    parser.add_argument("--export-only", action="store_true",
                        help="Export existing checkpoint to GGUF without training")
    parser.add_argument("--quant", default="q4_k_m",
                        help="GGUF quantization (default: q4_k_m)")
    args = parser.parse_args()

    if args.export_only:
        export_gguf(model_name=args.model, quant=args.quant)
    else:
        model, tokenizer = train(
            model_name=args.model,
            epochs=args.epochs,
            lr=args.lr,
            batch_size=args.batch_size,
            lora_rank=args.lora_rank,
        )
        export_gguf(model, tokenizer, quant=args.quant)


if __name__ == "__main__":
    main()
