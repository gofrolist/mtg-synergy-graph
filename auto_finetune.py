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
- function: what the card DOES mechanically (e.g. draw-engine, spot-removal, token-generator, sacrifice-outlet)
- themes: EDH deck archetypes this card fits (e.g. aristocrats, tokens, storm, voltron)
- provides: what this card GIVES to the board (e.g. card-draw, targeted-removal, counter-placement)
- wants: what conditions make this card BETTER (e.g. creature-death, wide-board, spell-cast)

Select tags from the controlled vocabulary used in training. Return ONLY valid JSON. No explanation."""


# ── Experiment Configurations ──

EXPERIMENTS = [
    # === Qwen 3.5 models (newest architecture) ===

    # Qwen3.5-4B: direct upgrade from Qwen3-4B
    {"name": "qwen35-4b-r32-lr2e4-ep2",
     "model": "unsloth/Qwen3.5-4B", "lora_rank": 32, "lr": 2e-4, "epochs": 2, "batch_size": 4},

    # Qwen3.5-35B-A3B MoE: 35B knowledge, 3B active params
    {"name": "qwen35-35b-a3b-r32-lr2e4-ep2",
     "model": "unsloth/Qwen3.5-35B-A3B", "lora_rank": 32, "lr": 2e-4, "epochs": 2, "batch_size": 4},

    # Qwen3.5-2B: fast iteration
    {"name": "qwen35-2b-r32-lr2e4-ep2",
     "model": "unsloth/Qwen3.5-2B", "lora_rank": 32, "lr": 2e-4, "epochs": 2, "batch_size": 4},

    # Qwen3.5-0.8B: minimum viable model
    {"name": "qwen35-08b-r32-lr2e4-ep2",
     "model": "unsloth/Qwen3.5-0.8B", "lora_rank": 32, "lr": 2e-4, "epochs": 2, "batch_size": 4},

    # === Qwen 3 baseline (retrain with fixed data) ===

    # Qwen3-4B: same as current model, fresh training data
    {"name": "qwen3-4b-r32-lr2e4-ep2",
     "model": "unsloth/Qwen3-4B-Instruct-2507-unsloth-bnb-4bit", "lora_rank": 32, "lr": 2e-4, "epochs": 2, "batch_size": 4},

    # === Hyperparameter variants on best Qwen3.5 ===

    # Qwen3.5-4B with lower LR
    {"name": "qwen35-4b-r32-lr1e4-ep2",
     "model": "unsloth/Qwen3.5-4B", "lora_rank": 32, "lr": 1e-4, "epochs": 2, "batch_size": 4},

    # Qwen3.5-4B with higher rank
    {"name": "qwen35-4b-r64-lr2e4-ep2",
     "model": "unsloth/Qwen3.5-4B", "lora_rank": 64, "lr": 2e-4, "epochs": 2, "batch_size": 4},

    # Qwen3.5-4B with 3 epochs (overfitting check)
    {"name": "qwen35-4b-r32-lr2e4-ep3",
     "model": "unsloth/Qwen3.5-4B", "lora_rank": 32, "lr": 2e-4, "epochs": 3, "batch_size": 4},

    # === Other architectures ===

    # Phi-4-mini (3.8B, Microsoft, different architecture)
    {"name": "phi4-mini-r32-lr2e4-ep2",
     "model": "unsloth/Phi-4-mini-instruct", "lora_rank": 32, "lr": 2e-4, "epochs": 2, "batch_size": 4},
]


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
            env=env, capture_output=True, text=True, timeout=1800,
        )
        if "MERGE_OK" not in result.stdout:
            print(f"  Merge FAILED", flush=True)
            print(f"  stderr: {result.stderr[-300:]}", flush=True)
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
        env=env, capture_output=True, text=True, timeout=1800,
    )

    elapsed = time.time() - t0

    if result.returncode != 0 or not os.path.exists(gguf_file):
        print(f"  GGUF conversion FAILED ({elapsed/60:.1f}m)")
        print(f"  stderr: {result.stderr[-300:]}", flush=True)
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

    # Function recall: what fraction of expected functions did the model produce?
    r_funcs = set(result.get("function", []))
    g_funcs = set(exp.get("function", []))
    func_recall = (len(r_funcs & g_funcs) / len(g_funcs)) if g_funcs else 1.0

    # Theme recall: what fraction of expected themes did the model produce?
    r_themes = set(result.get("themes", []))
    g_themes = set(exp.get("themes", []))
    theme_recall = (len(r_themes & g_themes) / len(g_themes)) if g_themes else 1.0

    # High-synergy theme recall (bonus signal — these are the most important themes)
    g_high = set(exp.get("high_synergy_themes", []))
    high_recall = (len(r_themes & g_high) / len(g_high)) if g_high else 1.0

    # Provides recall (may be empty in golden set)
    r_provides = set(result.get("provides", []))
    g_provides = set(exp.get("provides_must_include", []))
    prov_recall = (len(r_provides & g_provides) / len(g_provides)) if g_provides else 1.0

    # Wants recall (may be empty in golden set)
    r_wants = set(result.get("wants", []))
    g_wants = set(exp.get("wants_must_include", []))
    wants_recall = (len(r_wants & g_wants) / len(g_wants)) if g_wants else 1.0

    return {
        "parse_fail": False,
        "func_recall": func_recall,
        "theme_recall": theme_recall,
        "high_recall": high_recall,
        "prov_recall": prov_recall,
        "wants_recall": wants_recall,
        "name": g["name"],
        "func_error": None if func_recall == 1.0 else f"{g['name']}: got {sorted(r_funcs)[:4]} expected {sorted(g_funcs)[:4]}",
    }


def evaluate_model(model_name: str, max_workers: int = 4) -> dict:
    """Evaluate a model against the golden dataset. Returns detailed scores."""
    golden = json.load(open(GOLDEN_FILE))["cards"]

    total = 0
    parse_failures = 0
    func_recalls = []
    theme_recalls = []
    high_recalls = []
    prov_recalls = []
    wants_recalls = []
    func_errors = []

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
                func_recalls.append(r["func_recall"])
                theme_recalls.append(r["theme_recall"])
                high_recalls.append(r["high_recall"])
                prov_recalls.append(r["prov_recall"])
                wants_recalls.append(r["wants_recall"])
                if r["func_error"]:
                    func_errors.append(r["func_error"])

            if done % 100 == 0:
                elapsed = time.time() - t0
                print(f"    [{done}/{len(golden)}] {elapsed:.0f}s", flush=True)

    elapsed = time.time() - t0

    func_avg = sum(func_recalls) / max(total, 1)
    theme_avg = sum(theme_recalls) / max(total, 1)
    high_avg = sum(high_recalls) / max(total, 1)
    prov_avg = sum(prov_recalls) / max(total, 1)
    wants_avg = sum(wants_recalls) / max(total, 1)

    # Composite: weight function and themes higher (they have ground truth)
    composite = 0.35 * func_avg + 0.35 * theme_avg + 0.15 * prov_avg + 0.15 * wants_avg

    scores = {
        "total": total,
        "parse_failures": parse_failures,
        "function_recall": round(func_avg, 4),
        "theme_recall": round(theme_avg, 4),
        "high_synergy_recall": round(high_avg, 4),
        "provides_recall": round(prov_avg, 4),
        "wants_recall": round(wants_avg, 4),
        "composite_score": round(composite, 4),
        "eval_time_s": round(elapsed, 1),
        "func_errors": func_errors[:10],
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

    # Step 1: Fine-tune
    exp_output = run_finetune(config)
    if not exp_output:
        return None

    # Step 2: Export GGUF
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
    print(f"    Function:   {scores['function_recall']:.1%}", flush=True)
    print(f"    Themes:     {scores['theme_recall']:.1%} (high-syn: {scores['high_synergy_recall']:.1%})", flush=True)
    print(f"    Provides:   {scores['provides_recall']:.1%}", flush=True)
    print(f"    Wants:      {scores['wants_recall']:.1%}", flush=True)
    print(f"    Parse fail: {scores['parse_failures']}", flush=True)
    print(f"    Total time: {scores['total_time_m']}m", flush=True)

    if scores["func_errors"]:
        print(f"    Sample errors: {scores['func_errors'][:5]}", flush=True)

    # Cleanup: remove Ollama model and intermediate merge files to save disk
    subprocess.run(["ollama", "rm", ollama_name], capture_output=True)
    gguf_dir = os.path.join(exp_output, "gguf")
    if os.path.isdir(gguf_dir):
        shutil.rmtree(gguf_dir)

    return scores


def main():
    parser = argparse.ArgumentParser(description="Automated fine-tuning experiments")
    parser.add_argument("--experiments", type=int, help="Max experiments to run")
    parser.add_argument("--dry-run", action="store_true", help="Show plan without running")
    parser.add_argument("--eval-only", type=str, help="Evaluate an existing Ollama model")
    parser.add_argument("--skip", type=str, nargs="*", help="Experiment names to skip")
    args = parser.parse_args()

    if args.eval_only:
        print(f"Evaluating model: {args.eval_only}", flush=True)
        scores = evaluate_model(args.eval_only)
        print(f"\nComposite:  {scores['composite_score']:.1%}", flush=True)
        print(f"Function:   {scores['function_recall']:.1%}", flush=True)
        print(f"Themes:     {scores['theme_recall']:.1%} (high-syn: {scores['high_synergy_recall']:.1%})", flush=True)
        print(f"Provides:   {scores['provides_recall']:.1%}", flush=True)
        print(f"Wants:      {scores['wants_recall']:.1%}", flush=True)
        print(f"Parse fail: {scores['parse_failures']}", flush=True)
        if scores["func_errors"]:
            print(f"\nFunction errors:", flush=True)
            for err in scores["func_errors"]:
                print(f"  {err}", flush=True)
        return

    experiments = EXPERIMENTS
    if args.experiments:
        experiments = experiments[:args.experiments]

    skip = set(args.skip or [])
    existing = load_results()
    already_done = {r["experiment"] for r in existing}

    # Filter out already-done and skipped experiments
    todo = [e for e in experiments if e["name"] not in already_done and e["name"] not in skip]

    print(f"Experiment plan: {len(todo)} experiments")
    print(f"  Already done: {len(already_done)}")
    print(f"  Skipped: {len(skip)}")

    for i, config in enumerate(todo, 1):
        est_time = config["epochs"] * 15 + 10  # rough estimate: 15m/epoch + 10m overhead
        print(f"  {i}. {config['name']} (~{est_time}m)")
        if args.dry_run:
            print(f"     model={config['model']}, rank={config['lora_rank']}, "
                  f"lr={config['lr']}, epochs={config['epochs']}", flush=True)

    if args.dry_run:
        return

    # Run experiments
    results = existing.copy()
    best_score = max((r["composite_score"] for r in results), default=0)

    for i, config in enumerate(todo, 1):
        print(f"\n[{i}/{len(todo)}]", end="")

        scores = run_experiment(config)
        if scores:
            results.append(scores)
            save_results(results)

            if scores["composite_score"] > best_score:
                best_score = scores["composite_score"]
                print(f"  ★ NEW BEST: {scores['composite_score']:.1%}", flush=True)
            else:
                print(f"  Current best: {best_score:.1%}", flush=True)

    # Summary
    print(f"\n{'═' * 70}", flush=True)
    print(f"EXPERIMENT SUMMARY", flush=True)
    print(f"{'═' * 70}", flush=True)
    results.sort(key=lambda r: r.get("composite_score", 0), reverse=True)
    print(f"\n{'Experiment':<35} {'Composite':>10} {'Function':>10} {'Themes':>8} {'Provides':>10} {'Wants':>8} {'Time':>8}", flush=True)
    print(f"{'─' * 95}", flush=True)
    for r in results:
        print(f"  {r['experiment']:<33} {r.get('composite_score', 0):>9.1%} "
              f"{r.get('function_recall', 0):>9.1%} {r.get('theme_recall', 0):>7.1%} "
              f"{r.get('provides_recall', 0):>9.1%} {r.get('wants_recall', 0):>7.1%} "
              f"{r.get('total_time_m', 0):>6.1f}m", flush=True)

    if results:
        best = results[0]
        print(f"\n  Best: {best['experiment']} ({best['composite_score']:.1%})")


if __name__ == "__main__":
    main()
