"""
Automated fine-tuning experiment runner (autoresearch-style).

Runs multiple fine-tuning experiments with different hyperparameters,
evaluates each against the golden dataset, and keeps the best model.

Each experiment cycle:
  1. Fine-tune with given hyperparameters
  2. Export to GGUF → import into Ollama
  3. Retag golden cards with the new model
  4. Score against golden expectations (role + provides + wants accuracy)
  5. Log results, keep best

Inspired by github.com/karpathy/autoresearch — fixed budget per experiment,
single evaluation metric, automated keep/revert.

Usage:
    python3 auto_finetune.py                          # run all experiments
    python3 auto_finetune.py --experiments 5           # limit to 5 experiments
    python3 auto_finetune.py --dry-run                 # show experiment plan
    python3 auto_finetune.py --eval-only mtg-tagger    # evaluate existing model

Requires: conda activate finetune (unsloth environment)
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
GOLDEN_FILE = os.path.join(os.path.dirname(__file__), "golden_cards.json")
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "model_output")
RESULTS_FILE = os.path.join(os.path.dirname(__file__), "experiment_results.json")

OLLAMA_URL = "http://localhost:11434/api/chat"

# Python for fine-tuning (uv venv with unsloth + torch cu130)
TRAIN_PYTHON = os.path.join(os.path.dirname(__file__), ".venv", "bin", "python3")
CUDA_LIB = os.path.join(
    os.path.dirname(__file__), ".venv", "lib", "python3.12",
    "site-packages", "nvidia", "cu13", "lib"
)

SYSTEM_PROMPT = """You are an MTG card analyst. Analyze the card and return JSON with:
- name: card name
- role: the card's primary function (ramp, draw, removal, protection, enabler, threat, utility, land)
- provides: what this card GIVES to the deck (e.g. card-draw, targeted-removal, counter-placement)
- wants: what conditions make this card BETTER (e.g. creature-death, wide-board, spell-cast)

Select tags from the controlled vocabulary used in training. Return ONLY valid JSON. No explanation."""


# ── Experiment Configurations ──

BASE_MODEL = "unsloth/Qwen3-4B-Instruct-2507-unsloth-bnb-4bit"

# Iterative experiment grid — each iteration tries a different setting.
# The loop cycles through these indefinitely, appending iteration number.
ITERATION_GRID = [
    # Iteration 1: baseline with clean data
    {"suffix": "baseline", "lora_rank": 32, "lr": 2e-4, "epochs": 2, "batch_size": 4,
     "filter_to_registry": False},
    # Iteration 2: filter training data to registry tags only
    {"suffix": "registry-only", "lora_rank": 32, "lr": 2e-4, "epochs": 2, "batch_size": 4,
     "filter_to_registry": True},
    # Iteration 3: registry tags + more epochs
    {"suffix": "registry-ep3", "lora_rank": 32, "lr": 2e-4, "epochs": 3, "batch_size": 4,
     "filter_to_registry": True},
    # Iteration 4: registry tags + higher LoRA rank
    {"suffix": "registry-r64", "lora_rank": 64, "lr": 2e-4, "epochs": 2, "batch_size": 4,
     "filter_to_registry": True},
    # Iteration 5: registry tags + lower LR
    {"suffix": "registry-lr1e4", "lora_rank": 32, "lr": 1e-4, "epochs": 2, "batch_size": 4,
     "filter_to_registry": True},
    # Iteration 6: registry tags + higher rank + 3 epochs
    {"suffix": "registry-r64-ep3", "lora_rank": 64, "lr": 2e-4, "epochs": 3, "batch_size": 4,
     "filter_to_registry": True},
    # Iteration 7: registry tags + lower LR + 3 epochs
    {"suffix": "registry-lr1e4-ep3", "lora_rank": 32, "lr": 1e-4, "epochs": 3, "batch_size": 4,
     "filter_to_registry": True},
    # Iteration 8: registry tags + high rank + low LR
    {"suffix": "registry-r64-lr1e4", "lora_rank": 64, "lr": 1e-4, "epochs": 2, "batch_size": 4,
     "filter_to_registry": True},
]


# ── Training Data Filtering ──

def filter_training_data_to_registry():
    """Rebuild train.jsonl with only registry-approved tags. Returns filtered path."""
    registry_file = os.path.join(os.path.dirname(__file__), "synergy_tag_registry.json")
    with open(registry_file) as f:
        registry = json.load(f)
    valid_provides = set(registry["provides"]["tags"])
    valid_wants = set(registry["wants"]["tags"])

    train_path = os.path.join(DATA_DIR, "train.jsonl")
    filtered_path = os.path.join(DATA_DIR, "train_filtered.jsonl")

    kept = 0
    dropped_tags = 0
    with open(train_path) as fin, open(filtered_path, "w") as fout:
        for line in fin:
            ex = json.loads(line)
            # Parse the assistant response to filter tags
            assistant_msg = ex["messages"][2]["content"]
            resp = json.loads(assistant_msg)

            orig_p = len(resp.get("provides", []))
            orig_w = len(resp.get("wants", []))
            resp["provides"] = [t for t in resp.get("provides", []) if t in valid_provides]
            resp["wants"] = [t for t in resp.get("wants", []) if t in valid_wants]
            dropped_tags += (orig_p - len(resp["provides"])) + (orig_w - len(resp["wants"]))

            # Skip examples with no tags after filtering
            if not resp["provides"] and not resp["wants"]:
                continue

            ex["messages"][2]["content"] = json.dumps(resp, separators=(",", ":"))
            fout.write(json.dumps(ex, ensure_ascii=False) + "\n")
            kept += 1

    print(f"  Filtered training data: {kept} examples (dropped {dropped_tags} non-registry tags)")
    return filtered_path


# ── Training ──

def run_finetune(config: dict) -> str:
    """Run fine-tuning with given config. Returns path to output directory."""
    exp_output = os.path.join(OUTPUT_DIR, config["name"])
    os.makedirs(exp_output, exist_ok=True)

    # Skip training if adapter already exists (resume from previous run)
    adapter_path = os.path.join(exp_output, "adapter_model.safetensors")
    if os.path.exists(adapter_path):
        size_mb = os.path.getsize(adapter_path) / (1024 * 1024)
        print(f"  Adapter already exists ({size_mb:.0f}MB), skipping training")
        return exp_output

    cmd = [
        TRAIN_PYTHON, "finetune.py",
        "--model", config["model"],
        "--epochs", str(config["epochs"]),
        "--lr", str(config["lr"]),
        "--batch-size", str(config["batch_size"]),
        "--lora-rank", str(config["lora_rank"]),
    ]

    # Patch finetune.py to use experiment-specific output dir
    env = os.environ.copy()
    env["FINETUNE_OUTPUT_DIR"] = exp_output
    env["UNSLOTH_SKIP_TORCHVISION_CHECK"] = "1"
    env["LD_LIBRARY_PATH"] = CUDA_LIB + ":" + env.get("LD_LIBRARY_PATH", "")

    print(f"  Training: {config['model']} (rank={config['lora_rank']}, "
          f"lr={config['lr']}, ep={config['epochs']}, batch={config['batch_size']})",
          flush=True)
    t0 = time.time()

    # Stream output so progress is visible
    process = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True)
    for line in process.stdout:
        line = line.strip()
        # Show key progress lines (loss, eval, saving)
        if any(k in line for k in ["loss", "eval_loss", "Saving", "Training",
                                    "epochs", "Loading", "Epoch"]):
            print(f"    {line}", flush=True)
    process.wait()
    elapsed = time.time() - t0

    if process.returncode != 0:
        print(f"  Training FAILED ({elapsed/60:.1f}m)", flush=True)
        return None

    print(f"  Training complete ({elapsed/60:.1f}m)", flush=True)
    return exp_output


def export_gguf(exp_output: str, config: dict) -> str:
    """Export model to GGUF via merge + Python converter. Returns path to GGUF file."""
    gguf_dir = os.path.join(exp_output, "gguf")
    gguf_file = os.path.join(gguf_dir, "model.gguf")

    # Check if already exported
    if os.path.exists(gguf_file):
        print(f"  GGUF already exists: {gguf_file}", flush=True)
        return gguf_file

    t0 = time.time()
    env = os.environ.copy()
    env["UNSLOTH_SKIP_TORCHVISION_CHECK"] = "1"
    env["LD_LIBRARY_PATH"] = CUDA_LIB + ":" + env.get("LD_LIBRARY_PATH", "")

    # Step 1: Merge LoRA into base model (saves merged safetensors)
    # Check if already merged from a previous attempt
    merged_files = [f for f in os.listdir(gguf_dir) if f.endswith(".safetensors")] if os.path.exists(gguf_dir) else []
    if not merged_files:
        print(f"  Merging LoRA weights...", flush=True)
        merge_script = f"""
import os
os.environ["TORCHDYNAMO_DISABLE"] = "1"
os.environ["UNSLOTH_SKIP_TORCHVISION_CHECK"] = "1"
from unsloth import FastLanguageModel
model, tokenizer = FastLanguageModel.from_pretrained(
    model_name="{exp_output}",
    max_seq_length=2048, dtype=None, load_in_4bit=True,
)
model.save_pretrained_merged("{gguf_dir}", tokenizer, save_method="merged_16bit")
print("MERGE_OK", flush=True)
"""
        result = subprocess.run(
            [TRAIN_PYTHON, "-c", merge_script],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            text=True, timeout=1800,
        )
        if result.returncode != 0:
            print(f"  Merge FAILED", flush=True)
            print(f"  stderr: {result.stderr[-500:]}", flush=True)
            return None
        # Verify merge produced safetensors
        merged_files = [f for f in os.listdir(gguf_dir) if f.endswith(".safetensors")]
        if not merged_files:
            print(f"  Merge produced no safetensors", flush=True)
            return None

    # Step 2: Convert merged safetensors to GGUF via Python converter
    converter = os.path.expanduser("~/.unsloth/llama.cpp/convert_hf_to_gguf.py")
    if not os.path.exists(converter):
        print(f"  Converter not found: {converter}", flush=True)
        return None

    print(f"  Converting to GGUF (q8_0)...", flush=True)
    result = subprocess.run(
        [TRAIN_PYTHON, converter, gguf_dir,
         "--outfile", gguf_file, "--outtype", "q8_0"],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        text=True, timeout=1800,
    )

    elapsed = time.time() - t0

    if result.returncode != 0 or not os.path.exists(gguf_file):
        print(f"  GGUF conversion FAILED ({elapsed/60:.1f}m)")
        print(f"  stderr: {result.stderr[-500:]}", flush=True)
        return None

    size_mb = os.path.getsize(gguf_file) / (1024 * 1024)
    print(f"  GGUF exported: {size_mb:.0f}MB ({elapsed/60:.1f}m)")
    return gguf_file


def import_to_ollama(gguf_path: str, model_name: str, config: dict) -> bool:
    """Import GGUF into Ollama."""
    gguf_dir = os.path.dirname(gguf_path)
    modelfile = os.path.join(gguf_dir, "Modelfile")

    # Use absolute path in Modelfile with model-appropriate template.
    abs_gguf = os.path.abspath(gguf_path)
    model_name_lower = config.get("model", "").lower()

    with open(modelfile, "w") as f:
        f.write(f'FROM {abs_gguf}\n')

        if "phi" in model_name_lower:
            # Phi-4 uses <|system|>/<|user|>/<|assistant|> format
            f.write('TEMPLATE """{{- if .System }}<|system|>\n{{ .System }}<|end|>\n{{ end }}')
            f.write('{{- range .Messages }}{{- if eq .Role "user" }}<|user|>\n{{ .Content }}<|end|>\n{{ end }}')
            f.write('{{- if eq .Role "assistant" }}<|assistant|>\n{{ .Content }}<|end|>\n{{ end }}{{- end }}')
            f.write('<|assistant|>\n"""\n')
            f.write('PARAMETER stop <|end|>\n')
        else:
            # Qwen/chatml: no <|im_start|>think to prevent thinking mode
            f.write('TEMPLATE """{{- if .System }}<|im_start|>system\n')
            f.write('{{ .System }}<|im_end|>\n')
            f.write('{{ end }}{{- range .Messages }}{{- if eq .Role "user" }}<|im_start|>user\n')
            f.write('{{ .Content }}<|im_end|>\n')
            f.write('{{ end }}{{- if eq .Role "assistant" }}<|im_start|>assistant\n')
            f.write('{{ .Content }}<|im_end|>\n')
            f.write('{{ end }}{{- end }}<|im_start|>assistant\n')
            f.write('"""\n')
            f.write('PARAMETER stop <|im_end|>\n')

        f.write('PARAMETER num_ctx 1024\n')
        f.write('PARAMETER temperature 0.1\n')
        f.write(f'SYSTEM """{SYSTEM_PROMPT}"""\n')

    print(f"  Importing to Ollama as '{model_name}'...", flush=True)
    result = subprocess.run(
        ["ollama", "create", model_name, "-f", modelfile],
        capture_output=True, text=True, timeout=300,
    )

    if result.returncode != 0:
        print(f"  Ollama import FAILED: {result.stderr[:200]}", flush=True)
        return False

    print(f"  Ollama import OK", flush=True)
    return True


# ── Evaluation ──

def format_card_prompt(card: dict) -> str:
    """Format card for evaluation (matches training format)."""
    keywords = ", ".join(card.get("keywords", [])) or "none"
    parts = [
        f"Name: {card.get('name', '')}",
        f"Type: {card.get('type_line', '')}",
        f"CMC: {card.get('cmc', 0)}",
        f"Keywords: {keywords}",
        f"Oracle text: {card.get('oracle_text', '')}",
    ]
    power = card.get("power")
    toughness = card.get("toughness")
    if power is not None and toughness is not None:
        parts.append(f"Power/Toughness: {power}/{toughness}")
    loyalty = card.get("loyalty")
    if loyalty is not None:
        parts.append(f"Loyalty: {loyalty}")
    return "\n".join(parts)


def call_ollama(prompt: str, model: str, retries: int = 2) -> str | None:
    """Call Ollama API for a single card with retry."""
    payload = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "stream": False,
        "options": {"temperature": 0.1},
    }).encode()

    req = urllib.request.Request(
        OLLAMA_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
    )

    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read())
                return data["message"]["content"].strip()
        except Exception:
            if attempt < retries:
                time.sleep(1)
    return None


def parse_response(raw: str) -> dict | None:
    """Parse JSON from model response."""
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL)
    raw = re.sub(r"^```json\s*", "", raw.strip())
    raw = re.sub(r"\s*```$", "", raw)
    try:
        result = json.loads(raw)
        if isinstance(result, list):
            result = result[0]
        return result if isinstance(result, dict) else None
    except json.JSONDecodeError:
        return None


def _eval_single_card(g: dict, model_name: str) -> dict:
    """Evaluate a single golden card. Returns result dict for aggregation."""
    prompt = format_card_prompt(g)
    raw = call_ollama(prompt, model_name)

    if not raw:
        return {"parse_fail": True}

    result = parse_response(raw)
    if not result:
        return {"parse_fail": True}

    exp = g["expected"]

    # Role match
    r_role = result.get("role", "")
    g_role = exp.get("role", "")
    role_match = 1.0 if r_role == g_role else 0.0

    # Provides recall
    r_provides = set(result.get("provides", []))
    g_provides = set(exp.get("provides", []))
    prov_recall = (len(r_provides & g_provides) / len(g_provides)) if g_provides else 1.0

    # Wants recall
    r_wants = set(result.get("wants", []))
    g_wants = set(exp.get("wants", []))
    wants_recall = (len(r_wants & g_wants) / len(g_wants)) if g_wants else 1.0

    return {
        "parse_fail": False,
        "role_match": role_match,
        "prov_recall": prov_recall,
        "wants_recall": wants_recall,
        "name": g["name"],
        "error": None if prov_recall == 1.0 else f"{g['name']}: got {sorted(r_provides)[:4]} expected {sorted(g_provides)[:4]}",
    }


def evaluate_model(model_name: str, max_workers: int = 4) -> dict:
    """Evaluate a model against the golden dataset. Returns detailed scores."""
    golden = json.load(open(GOLDEN_FILE))["cards"]

    total = 0
    parse_failures = 0
    role_matches = []
    prov_recalls = []
    wants_recalls = []
    errors = []

    t0 = time.time()
    done = 0

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_eval_single_card, g, model_name): g for g in golden}
        for future in as_completed(futures):
            r = future.result()
            done += 1

            if r["parse_fail"]:
                parse_failures += 1
            else:
                total += 1
                role_matches.append(r["role_match"])
                prov_recalls.append(r["prov_recall"])
                wants_recalls.append(r["wants_recall"])
                if r["error"]:
                    errors.append(r["error"])

            if done % 100 == 0:
                elapsed = time.time() - t0
                print(f"    [{done}/{len(golden)}] {elapsed:.0f}s", flush=True)

    elapsed = time.time() - t0

    role_avg = sum(role_matches) / max(total, 1)
    prov_avg = sum(prov_recalls) / max(total, 1)
    wants_avg = sum(wants_recalls) / max(total, 1)

    # Composite: provides/wants are the core value, role is secondary
    composite = 0.45 * prov_avg + 0.45 * wants_avg + 0.1 * role_avg

    scores = {
        "total": total,
        "parse_failures": parse_failures,
        "role_accuracy": round(role_avg, 4),
        "provides_recall": round(prov_avg, 4),
        "wants_recall": round(wants_avg, 4),
        "composite_score": round(composite, 4),
        "eval_time_s": round(elapsed, 1),
        "errors": errors[:10],
    }

    return scores


# ── Experiment Runner ──

def load_results() -> list[dict]:
    """Load previous experiment results."""
    if os.path.exists(RESULTS_FILE):
        with open(RESULTS_FILE) as f:
            return json.load(f)
    return []


def save_results(results: list[dict]):
    """Save experiment results."""
    with open(RESULTS_FILE, "w") as f:
        json.dump(results, f, indent=2)


def run_experiment(config: dict) -> dict | None:
    """Run a single experiment end-to-end. Returns scores or None on failure."""
    exp_name = config["name"]
    ollama_name = f"mtg-exp-{exp_name}"

    print(f"\n{'═' * 70}", flush=True)
    print(f"EXPERIMENT: {exp_name}", flush=True)
    print(f"  model={config['model']}", flush=True)
    print(f"  lora_rank={config['lora_rank']}, lr={config['lr']}, "
          f"epochs={config['epochs']}, batch={config['batch_size']}", flush=True)
    print(f"{'═' * 70}", flush=True)

    t0 = time.time()

    # Unload any Ollama models to free VRAM for training/merging
    subprocess.run(["ollama", "stop", "--all"], capture_output=True, timeout=30)
    time.sleep(2)

    # Step 1: Fine-tune
    exp_output = run_finetune(config)
    if not exp_output:
        return None

    # Step 2: Export GGUF (unload Ollama again to free VRAM for merge)
    subprocess.run(["ollama", "stop", "--all"], capture_output=True, timeout=30)
    time.sleep(2)
    gguf_path = export_gguf(exp_output, config)
    if not gguf_path:
        return None

    # Step 3: Import to Ollama
    if not import_to_ollama(gguf_path, ollama_name, config):
        return None

    # Step 4: Warm up model (first inference is slow due to model loading)
    print(f"  Warming up model...", flush=True)
    for attempt in range(3):
        result = call_ollama("Name: Sol Ring\nType: Artifact\nCMC: 1\nKeywords: none\nOracle text: {T}: Add {C}{C}.", ollama_name)
        if result:
            break
        time.sleep(5)

    # Step 5: Evaluate
    golden_count = len(json.load(open(GOLDEN_FILE))["cards"])
    print(f"  Evaluating against {golden_count} golden cards...", flush=True)
    scores = evaluate_model(ollama_name)

    total_time = time.time() - t0
    scores["experiment"] = exp_name
    scores["config"] = config
    scores["total_time_m"] = round(total_time / 60, 1)

    print(f"\n  RESULTS: {exp_name}", flush=True)
    print(f"    Composite:  {scores['composite_score']:.1%}", flush=True)
    print(f"    Role:       {scores['role_accuracy']:.1%}", flush=True)
    print(f"    Provides:   {scores['provides_recall']:.1%}", flush=True)
    print(f"    Wants:      {scores['wants_recall']:.1%}", flush=True)
    print(f"    Parse fail: {scores['parse_failures']}", flush=True)
    print(f"    Total time: {scores['total_time_m']}m", flush=True)

    if scores["errors"]:
        print(f"    Sample errors: {scores['errors'][:5]}", flush=True)

    # Cleanup: remove Ollama model and intermediate merge files to save disk
    subprocess.run(["ollama", "rm", ollama_name], capture_output=True)
    gguf_dir = os.path.join(exp_output, "gguf")
    if os.path.isdir(gguf_dir):
        shutil.rmtree(gguf_dir)

    return scores


def print_summary(results: list[dict]):
    """Print experiment results table."""
    print(f"\n{'═' * 85}", flush=True)
    print(f"EXPERIMENT SUMMARY", flush=True)
    print(f"{'═' * 85}", flush=True)
    sorted_results = sorted(results, key=lambda r: r.get("composite_score", 0), reverse=True)
    print(f"\n{'Experiment':<35} {'Composite':>10} {'Role':>8} {'Provides':>10} {'Wants':>8} {'Time':>8}", flush=True)
    print(f"{'─' * 85}", flush=True)
    for r in sorted_results:
        print(f"  {r['experiment']:<33} {r.get('composite_score', 0):>9.1%} "
              f"{r.get('role_accuracy', 0):>7.1%} "
              f"{r.get('provides_recall', 0):>9.1%} {r.get('wants_recall', 0):>7.1%} "
              f"{r.get('total_time_m', 0):>6.1f}m", flush=True)

    if sorted_results:
        best = sorted_results[0]
        print(f"\n  Best: {best['experiment']} ({best['composite_score']:.1%})")


def main():
    parser = argparse.ArgumentParser(description="Automated fine-tuning experiments")
    parser.add_argument("--max-iterations", type=int, help="Max iterations (default: infinite)")
    parser.add_argument("--dry-run", action="store_true", help="Show plan without running")
    parser.add_argument("--eval-only", type=str, help="Evaluate an existing Ollama model")
    args = parser.parse_args()

    if args.eval_only:
        print(f"Evaluating model: {args.eval_only}", flush=True)
        scores = evaluate_model(args.eval_only)
        print(f"\nComposite:  {scores['composite_score']:.1%}", flush=True)
        print(f"Role:       {scores['role_accuracy']:.1%}", flush=True)
        print(f"Provides:   {scores['provides_recall']:.1%}", flush=True)
        print(f"Wants:      {scores['wants_recall']:.1%}", flush=True)
        print(f"Parse fail: {scores['parse_failures']}", flush=True)
        if scores["errors"]:
            print(f"\nErrors:", flush=True)
            for err in scores["errors"]:
                print(f"  {err}", flush=True)
        return

    # Load existing results and determine where to resume
    results = load_results()
    already_done = {r["experiment"] for r in results}
    best_score = max((r["composite_score"] for r in results), default=0)

    # Build experiment list — cycle through ITERATION_GRID indefinitely
    grid_size = len(ITERATION_GRID)
    iteration = 0
    experiments_run = 0
    max_iterations = args.max_iterations

    # Backup original train.jsonl path
    train_orig = os.path.join(DATA_DIR, "train.jsonl")
    train_filtered = os.path.join(DATA_DIR, "train_filtered.jsonl")

    print(f"Auto fine-tune loop starting", flush=True)
    print(f"  Grid size: {grid_size} configs per cycle", flush=True)
    print(f"  Already done: {len(already_done)}", flush=True)
    print(f"  Best score so far: {best_score:.1%}", flush=True)
    if max_iterations:
        print(f"  Max iterations: {max_iterations}", flush=True)
    else:
        print(f"  Running indefinitely (Ctrl+C to stop)", flush=True)

    if args.dry_run:
        print(f"\nExperiment plan (first cycle):")
        for i, grid_cfg in enumerate(ITERATION_GRID):
            name = f"iter-{i+1}-{grid_cfg['suffix']}"
            status = "SKIP (done)" if name in already_done else "TODO"
            print(f"  {i+1}. {name} [{status}]")
            print(f"     rank={grid_cfg['lora_rank']}, lr={grid_cfg['lr']}, "
                  f"ep={grid_cfg['epochs']}, filter={grid_cfg['filter_to_registry']}")
        return

    try:
        while True:
            grid_cfg = ITERATION_GRID[iteration % grid_size]
            cycle = iteration // grid_size + 1
            step = iteration % grid_size + 1
            exp_name = f"iter-{iteration+1}-{grid_cfg['suffix']}"

            if max_iterations and experiments_run >= max_iterations:
                print(f"\nReached max iterations ({max_iterations})", flush=True)
                break

            # Skip already completed
            if exp_name in already_done:
                print(f"  Skipping {exp_name} (already done)", flush=True)
                iteration += 1
                continue

            # Prepare training data
            if grid_cfg["filter_to_registry"]:
                print(f"\n  Filtering training data to registry tags...", flush=True)
                filter_training_data_to_registry()
                # Swap train.jsonl with filtered version
                import shutil as _sh
                _sh.copy2(train_orig, train_orig + ".bak")
                _sh.copy2(train_filtered, train_orig)

            # Build experiment config
            config = {
                "name": exp_name,
                "model": BASE_MODEL,
                "lora_rank": grid_cfg["lora_rank"],
                "lr": grid_cfg["lr"],
                "epochs": grid_cfg["epochs"],
                "batch_size": grid_cfg["batch_size"],
            }

            print(f"\n[Cycle {cycle}, Step {step}/{grid_size}]", flush=True)

            # Run experiment
            scores = run_experiment(config)

            # Restore original train.jsonl if we swapped it
            if grid_cfg["filter_to_registry"] and os.path.exists(train_orig + ".bak"):
                _sh.copy2(train_orig + ".bak", train_orig)
                os.remove(train_orig + ".bak")

            if scores:
                results.append(scores)
                save_results(results)
                already_done.add(exp_name)
                experiments_run += 1

                if scores["composite_score"] > best_score:
                    best_score = scores["composite_score"]
                    print(f"  ★ NEW BEST: {scores['composite_score']:.1%}", flush=True)
                else:
                    print(f"  Current best: {best_score:.1%}", flush=True)

                # Print summary every cycle
                if step == grid_size:
                    print_summary(results)

            iteration += 1

    except KeyboardInterrupt:
        print(f"\n\nStopped by user after {experiments_run} experiments.", flush=True)

    # Final summary
    print_summary(results)


if __name__ == "__main__":
    main()
