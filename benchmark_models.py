"""
Benchmark multiple Ollama models against golden cards.

Tests each model on a sample of golden cards and compares function/theme
recall, parse success rate, and inference speed.

Usage:
    python3 benchmark_models.py                          # test all models on 100 cards
    python3 benchmark_models.py --cards 50               # smaller sample
    python3 benchmark_models.py --models qwen3:8b phi4:14b  # specific models
"""

import argparse
import json
import os
import re
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

GOLDEN_FILE = os.path.join(os.path.dirname(__file__), "golden_cards.json")
OLLAMA_URL = "http://localhost:11434/api/chat"

SYSTEM_PROMPT = """You are an MTG card analyst. Analyze the card and return JSON with:
- name: card name
- role: the card's primary function (ramp, draw, removal, protection, enabler, threat, utility, land)
- provides: what this card GIVES to the deck (e.g. card-draw, targeted-removal, counter-placement)
- wants: what conditions make this card BETTER (e.g. creature-death, wide-board, spell-cast)

Select tags from the controlled vocabulary used in training. Return ONLY valid JSON. No explanation."""


def format_card(card: dict) -> str:
    keywords = ", ".join(card.get("keywords", [])) or "none"
    parts = [
        f"Name: {card.get('name', '')}",
        f"Type: {card.get('type_line', '')}",
        f"CMC: {card.get('cmc', 0)}",
        f"Keywords: {keywords}",
        f"Oracle text: {card.get('oracle_text', '')}",
    ]
    if card.get("power") and card.get("toughness"):
        parts.append(f"Power/Toughness: {card['power']}/{card['toughness']}")
    if card.get("loyalty"):
        parts.append(f"Loyalty: {card['loyalty']}")
    return "\n".join(parts)


def call_ollama(prompt: str, model: str) -> tuple[str | None, float]:
    """Call Ollama, return (response, latency_seconds)."""
    payload = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "stream": False,
        "think": False,
        "options": {"temperature": 0.1},
    }).encode()

    req = urllib.request.Request(
        OLLAMA_URL, data=payload,
        headers={"Content-Type": "application/json"},
    )

    t0 = time.time()
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read())
                elapsed = time.time() - t0
                return data["message"]["content"].strip(), elapsed
        except Exception:
            if attempt == 0:
                time.sleep(1)
    return None, time.time() - t0


def parse_response(raw: str) -> dict | None:
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


def eval_card(card: dict, model: str) -> dict:
    prompt = format_card(card)
    raw, latency = call_ollama(prompt, model)

    if not raw:
        return {"parse_fail": True, "latency": latency}

    result = parse_response(raw)
    if not result:
        return {"parse_fail": True, "latency": latency}

    exp = card["expected"]

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
        "latency": latency,
        "prov_predicted": len(r_provides),
        "wants_predicted": len(r_wants),
    }


def benchmark_model(model: str, cards: list[dict]) -> dict:
    """Benchmark a single model. Returns scores dict."""
    print(f"\n  Testing {model}...", flush=True)

    # Warm up
    call_ollama("Name: Sol Ring\nType: Artifact\nCMC: 1\nKeywords: none\nOracle text: {T}: Add {C}{C}.", model)

    results = []
    t0 = time.time()

    # Sequential to get accurate per-card latency
    for i, card in enumerate(cards):
        r = eval_card(card, model)
        results.append(r)
        if (i + 1) % 25 == 0:
            elapsed = time.time() - t0
            print(f"    [{i+1}/{len(cards)}] {elapsed:.0f}s", flush=True)

    total_time = time.time() - t0
    parsed = [r for r in results if not r["parse_fail"]]
    parse_failures = len(results) - len(parsed)

    if not parsed:
        return {
            "model": model, "total": 0, "parse_failures": parse_failures,
            "role_accuracy": 0, "provides_recall": 0, "wants_recall": 0,
            "composite": 0, "avg_latency": 0, "total_time": total_time,
            "avg_provides": 0, "avg_wants": 0,
        }

    role_avg = sum(r["role_match"] for r in parsed) / len(parsed)
    prov_avg = sum(r["prov_recall"] for r in parsed) / len(parsed)
    wants_avg = sum(r["wants_recall"] for r in parsed) / len(parsed)
    latency_avg = sum(r["latency"] for r in parsed) / len(parsed)
    composite = 0.45 * prov_avg + 0.45 * wants_avg + 0.1 * role_avg

    return {
        "model": model,
        "total": len(parsed),
        "parse_failures": parse_failures,
        "role_accuracy": round(role_avg, 4),
        "provides_recall": round(prov_avg, 4),
        "wants_recall": round(wants_avg, 4),
        "composite": round(composite, 4),
        "avg_latency": round(latency_avg, 2),
        "total_time": round(total_time, 1),
        "avg_provides": round(sum(r["prov_predicted"] for r in parsed) / len(parsed), 1),
        "avg_wants": round(sum(r["wants_predicted"] for r in parsed) / len(parsed), 1),
    }


def main():
    parser = argparse.ArgumentParser(description="Benchmark Ollama models on golden cards")
    parser.add_argument("--cards", type=int, default=100, help="Number of golden cards to test")
    parser.add_argument("--models", nargs="*", help="Specific models to test")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    with open(GOLDEN_FILE) as f:
        golden = json.load(f)["cards"]

    # Sample cards
    import random
    random.seed(args.seed)
    sample = random.sample(golden, min(args.cards, len(golden)))
    print(f"Benchmarking on {len(sample)} golden cards")

    # Default models to test
    if args.models:
        models = args.models
    else:
        models = [
            "mtg-eval",         # our fine-tuned Qwen3-4B (2k data, 1 epoch)
            "qwen3:1.7b",       # tiny base
            "granite4",         # IBM granite4
            "lfm25",            # LiquidAI LFM2.5-1.2B-Instruct
            "phi4-mini",        # Microsoft Phi-4 mini (3.8B)
        ]

    # Load previous results to avoid re-running completed models
    results_file = os.path.join(os.path.dirname(__file__), "benchmark_results.json")
    if os.path.exists(results_file):
        with open(results_file) as f:
            all_results = json.load(f)
        done = {r["model"] for r in all_results}
        print(f"  Loaded {len(done)} previous results: {', '.join(sorted(done))}")
    else:
        all_results = []
        done = set()

    for model in models:
        if model in done:
            print(f"\n  Skipping {model} (already benchmarked)")
            continue
        try:
            scores = benchmark_model(model, sample)
            all_results.append(scores)
            # Save after each model so progress is never lost
            with open(results_file, "w") as f:
                json.dump(all_results, f, indent=2)
            print(f"  Saved ({len(all_results)} models so far)")
        except Exception as e:
            print(f"  {model}: FAILED — {e}")

    # Print results table
    print(f"\n{'=' * 100}")
    print(f"BENCHMARK RESULTS ({len(sample)} golden cards)")
    print(f"{'=' * 100}")
    print(f"{'Model':<25} {'Composite':>10} {'Role':>8} {'Provides':>10} {'Wants':>8} "
          f"{'Parse%':>7} {'Latency':>8} {'Time':>7} {'#Prov':>6} {'#Want':>7}")
    print(f"{'-' * 100}")

    for r in sorted(all_results, key=lambda x: -x["composite"]):
        parse_pct = r["total"] / (r["total"] + r["parse_failures"]) * 100 if (r["total"] + r["parse_failures"]) else 0
        print(f"  {r['model']:<23} {r['composite']:>9.1%} {r['role_accuracy']:>7.1%} "
              f"{r['provides_recall']:>9.1%} {r['wants_recall']:>7.1%} "
              f"{parse_pct:>6.0f}% {r['avg_latency']:>7.1f}s {r['total_time']:>6.0f}s "
              f"{r['avg_provides']:>5.1f} {r['avg_wants']:>6.1f}")

    # Final save
    with open(results_file, "w") as f:
        json.dump(all_results, f, indent=2)
    print(f"\nResults saved to {results_file}")


if __name__ == "__main__":
    main()
