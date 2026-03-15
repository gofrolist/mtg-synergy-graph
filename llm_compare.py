"""
Compare local LLM models against GPT-4o on the golden card tagging task.

Tests each model on the 5 golden cards + 5 additional cards, measures:
- Tag accuracy (must_include / must_exclude)
- JSON parse success rate
- Speed (tokens/sec)
- Tag consistency (do related cards share tags?)

Usage:
    python3 llm_compare.py
    python3 llm_compare.py --models qwen3:8b gemma3:12b
"""

import argparse
import json
import os
import re
import time
import urllib.request
import urllib.error

from prompt_builder import build_prompt

MODELS = ["gemma3:12b", "granite4", "lfm25"]
OLLAMA_URL = "http://localhost:11434/api/chat"

# Test cards: 5 golden + 5 extras for consistency check
TEST_CARDS = [
    # Golden cards (have expected tags)
    {
        "name": "Hardened Scales", "type_line": "Enchantment",
        "oracle_text": "If one or more +1/+1 counters would be put on a creature you control, that many plus one +1/+1 counters are put on it instead.",
        "keywords": [], "cmc": 1,
    },
    {
        "name": "Roaming Throne", "type_line": "Artifact Creature — Golem",
        "oracle_text": "Ward {2}\nAs this creature enters, choose a creature type.\nThis creature is the chosen type in addition to its other types.\nIf a triggered ability of a creature you control of the chosen type triggers, it triggers an additional time.",
        "keywords": [], "cmc": 4,
    },
    {
        "name": "Slippery Bogbonder", "type_line": "Creature — Human Scout",
        "oracle_text": "Flash. When Slippery Bogbonder enters, move all counters from target creature onto another target creature. That creature gains hexproof until end of turn.",
        "keywords": ["Flash"], "cmc": 4,
    },
    {
        "name": "Sol Ring", "type_line": "Artifact",
        "oracle_text": "{T}: Add {C}{C}.",
        "keywords": [], "cmc": 1,
    },
    {
        "name": "Gavony Township", "type_line": "Land",
        "oracle_text": "{2}{G}{W}, {T}: Put a +1/+1 counter on each creature you control.",
        "keywords": [], "cmc": 0,
    },
    # Extra cards for consistency check
    {
        "name": "Kyler, Sigardian Emissary",
        "type_line": "Legendary Creature — Human Cleric",
        "oracle_text": "When another Human you control enters, put a +1/+1 counter on each other Human you control. When Kyler, Sigardian Emissary enters, put a +1/+1 counter on it for each other Human you control.",
        "keywords": [], "cmc": 5,
    },
    {
        "name": "Thalia's Lieutenant",
        "type_line": "Creature — Human Soldier",
        "oracle_text": "When Thalia's Lieutenant enters, put a +1/+1 counter on each other Human you control. Whenever another Human enters under your control, put a +1/+1 counter on Thalia's Lieutenant.",
        "keywords": [], "cmc": 2,
    },
    {
        "name": "Doubling Season", "type_line": "Enchantment",
        "oracle_text": "If an effect would create one or more tokens under your control, it creates twice that many of those tokens instead. If an effect would put one or more counters on a permanent you control, it puts twice that many of those counters on that permanent instead.",
        "keywords": [], "cmc": 5,
    },
    {
        "name": "Panharmonicon", "type_line": "Artifact",
        "oracle_text": "If an artifact or creature entering the battlefield causes a triggered ability of a permanent you control to trigger, that ability triggers an additional time.",
        "keywords": [], "cmc": 4,
    },
    {
        "name": "Champion of Lambholt",
        "type_line": "Creature — Human Warrior",
        "oracle_text": "Creatures with power less than Champion of Lambholt's power can't block creatures you control. Whenever another creature enters under your control, put a +1/+1 counter on Champion of Lambholt.",
        "keywords": [], "cmc": 3,
    },
]


def load_golden_expected() -> dict[str, dict]:
    with open("golden_cards.json") as f:
        data = json.load(f)
    return {c["name"]: c["expected"] for c in data["cards"]}


def load_gpt4o_tags() -> dict[str, dict]:
    """Load GPT-4o results for comparison."""
    if not os.path.exists("data/kyler_tags.json"):
        return {}
    with open("data/kyler_tags.json") as f:
        tags = json.load(f)
    return {t["name"]: t for t in tags}


def call_ollama(model: str, system: str, user: str) -> tuple[str | None, float]:
    """Call Ollama API. Returns (response_text, duration_seconds)."""
    payload = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": False,
        "options": {"temperature": 0.1},
    }).encode()

    req = urllib.request.Request(
        OLLAMA_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
    )

    start = time.time()
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            data = json.loads(resp.read())
            elapsed = time.time() - start
            return data["message"]["content"].strip(), elapsed
    except Exception as e:
        elapsed = time.time() - start
        print(f"    [ERROR] {e}")
        return None, elapsed


def parse_json(raw: str) -> dict | None:
    """Try to extract JSON from model response."""
    # Strip markdown fences
    raw = re.sub(r"^```json\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    # Strip thinking tags (qwen3)
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL)
    raw = raw.strip()

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Try to find JSON object in the response
        match = re.search(r"\{[\s\S]*\}", raw)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
    return None


def score_golden(tagged: dict, expected: dict) -> dict:
    """Score a tagged card against golden expected values."""
    t_provides = set(tagged.get("provides", []))
    e_provides = set(expected.get("provides", []))
    t_wants = set(tagged.get("wants", []))
    e_wants = set(expected.get("wants", []))

    p_hits = len(t_provides & e_provides)
    w_hits = len(t_wants & e_wants)
    total_required = len(e_provides) + len(e_wants)
    role_match = tagged.get("role", "") == expected.get("role", "")

    return {
        "hits": p_hits + w_hits,
        "total_required": total_required,
        "missing_provides": list(e_provides - t_provides),
        "missing_wants": list(e_wants - t_wants),
        "role_match": role_match,
        "pass": total_required == 0 or (p_hits + w_hits) / total_required >= 0.5,
    }


def run():
    parser = argparse.ArgumentParser()
    parser.add_argument("--models", nargs="+", default=MODELS)
    args = parser.parse_args()

    golden = load_golden_expected()
    gpt4o_tags = load_gpt4o_tags()

    print("=" * 70)
    print("LLM COMPARISON: Local Models vs GPT-4o on MTG Card Tagging")
    print("=" * 70)
    print(f"Models: {args.models}")
    print(f"Test cards: {len(TEST_CARDS)} ({len(golden)} golden)")
    print()

    results = {}

    for model in args.models:
        print(f"\n{'─'*70}")
        print(f"MODEL: {model}")
        print(f"{'─'*70}")

        model_results = {
            "json_success": 0,
            "json_fail": 0,
            "golden_pass": 0,
            "golden_fail": 0,
            "golden_hits": 0,
            "golden_total": 0,
            "total_time": 0,
            "cards": {},
        }

        for i, card in enumerate(TEST_CARDS, 1):
            name = card["name"]
            print(f"\n  [{i}/{len(TEST_CARDS)}] {name}...", end=" ", flush=True)

            system, user = build_prompt(card)
            raw, elapsed = call_ollama(model, system, user)
            model_results["total_time"] += elapsed

            if not raw:
                print(f"ERROR ({elapsed:.1f}s)")
                model_results["json_fail"] += 1
                continue

            tagged = parse_json(raw)
            if not tagged:
                print(f"JSON FAIL ({elapsed:.1f}s)")
                print(f"    Raw: {raw[:150]}...")
                model_results["json_fail"] += 1
                continue

            model_results["json_success"] += 1
            role = tagged.get("role", "?")
            provides = tagged.get("provides", [])
            print(f"OK ({elapsed:.1f}s) role={role} provides={provides[:4]}")

            # Score against golden if available
            if name in golden:
                score = score_golden(tagged, golden[name])
                model_results["golden_hits"] += score["hits"]
                model_results["golden_total"] += score["total_required"]
                if score["pass"]:
                    model_results["golden_pass"] += 1
                    print(f"    GOLDEN: PASS ({score['hits']}/{score['total_required']} tags)")
                else:
                    model_results["golden_fail"] += 1
                    print(f"    GOLDEN: FAIL — missing={score['missing']} unwanted={score['unwanted']}")

            model_results["cards"][name] = tagged

        results[model] = model_results

    # ── COMPARISON TABLE ──
    print(f"\n\n{'='*70}")
    print("COMPARISON SUMMARY")
    print(f"{'='*70}")

    # Add GPT-4o results if available
    if gpt4o_tags:
        gpt4o_results = {
            "json_success": 0, "json_fail": 0,
            "golden_pass": 0, "golden_fail": 0,
            "golden_hits": 0, "golden_total": 0,
            "total_time": 0,
        }
        for card in TEST_CARDS:
            name = card["name"]
            tagged = gpt4o_tags.get(name)
            if tagged:
                gpt4o_results["json_success"] += 1
                if name in golden:
                    score = score_golden(tagged, golden[name])
                    gpt4o_results["golden_hits"] += score["hits"]
                    gpt4o_results["golden_total"] += score["total_required"]
                    if score["pass"]:
                        gpt4o_results["golden_pass"] += 1
                    else:
                        gpt4o_results["golden_fail"] += 1
        results["gpt-4o (reference)"] = gpt4o_results

    header = f"{'Model':<25} {'JSON OK':>8} {'Golden':>8} {'Tag Acc':>8} {'Time':>8}"
    print(header)
    print("─" * len(header))

    for model, r in results.items():
        total = r["json_success"] + r["json_fail"]
        json_rate = f"{r['json_success']}/{total}"
        golden_rate = f"{r['golden_pass']}/{r['golden_pass']+r['golden_fail']}"
        tag_acc = f"{r['golden_hits']}/{r['golden_total']}" if r["golden_total"] > 0 else "n/a"
        time_str = f"{r['total_time']:.0f}s" if r["total_time"] > 0 else "API"
        print(f"  {model:<23} {json_rate:>8} {golden_rate:>8} {tag_acc:>8} {time_str:>8}")

    print(f"\nJSON OK = parsed valid JSON | Golden = passed golden card checks")
    print(f"Tag Acc = required tags found / total required | Time = total for {len(TEST_CARDS)} cards")


if __name__ == "__main__":
    run()
