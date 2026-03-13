"""
regression_test.py
Validates that tagged cards match golden dataset expectations.

Two-source architecture:
  - synergy_tags: validated against Scryfall tagger community tags
  - role/provides/wants: validated against LLM output

Modes:
  --mode static   : validate against golden_cards.json (no API call)
  --mode scryfall : validate Scryfall tags only (from scryfall_function_tags.json)
  --mode live     : call API for each golden card, compare to expected

Exit codes:
  0 = all tests passed
  1 = some tests failed (shows which tags are wrong)
"""

import json
import sys
import argparse
from pathlib import Path

BASE_DIR = Path(__file__).parent
SCRYFALL_TAGS_FILE = BASE_DIR / "data" / "scryfall_function_tags.json"

# Synonym groups: tags within each group are considered equivalent
TAG_SYNONYMS = [
    # Scryfall vocabulary synonyms
    {"protects-permanent", "protects-creature", "board-protection", "board-wide-protection",
     "gives indestructible", "gives hexproof"},
    {"removal-exile", "exile-removal"},
    {"removal-destroy", "destroy-removal"},
    {"sweeper", "boardwipe", "board-wipe", "mass-removal", "hate-wide"},
    {"mana dork", "mana-dork", "mana-creature"},
    {"mana rock", "mana-rock", "staple-mana-rock", "mana rock with set's mechanic"},
    {"ramp", "tutor-land-to-battlefield", "land-fetch", "land-ramp"},
    {"counter doubler", "counter-doubler", "counter increaser", "counter-multiplier"},
    {"token doubler", "token-doubler", "token-multiplier"},
    {"trigger doubler", "trigger-doubler", "triggered-ability-doubler"},
    {"mana sink", "mana-sink", "mana-outlet", "bottomless-mana-sink"},
    {"move counters", "counter-mover", "counter-movement"},
    {"spot removal", "spot-removal", "single target removal"},
    {"typal-choose", "tribal-choose", "can-become-chosen-type"},
    # LLM provides/wants synonyms
    {"mana-acceleration", "mana-ramp", "colorless-ramp", "colorless-mana-ramp",
     "early-mana-acceleration"},
    {"counter-amplification", "counter-boost", "extra-counters"},
    {"counter-placement", "counter-placement-events", "pp-counter-placement"},
    {"trigger-doubling", "double-triggers", "trigger-amplification"},
]


def _semantic_tag_match(required: str, actual_tags: set[str]) -> bool:
    """Check if a required tag concept is satisfied by any actual tag.

    Matching strategies (in order):
    1. Exact match
    2. Synonym group match
    3. Substring match: required is substring of actual or vice versa
    """
    # 1. Exact match
    if required in actual_tags:
        return True

    # 2. Synonym group match
    for group in TAG_SYNONYMS:
        if required in group:
            if group & actual_tags:  # set intersection
                return True

    # 3. Substring match (e.g., "board-protection" matches "board-wide-protection")
    for actual in actual_tags:
        if required in actual or actual in required:
            return True

    return False


def load_golden() -> list[dict]:
    path = BASE_DIR / "golden_cards.json"
    with open(path) as f:
        data = json.load(f)
    return data["cards"]


def load_tagged(tags_file: str = "card_tags.json") -> dict[str, dict]:
    """Load a tags JSON file, return dict keyed by card name."""
    path = BASE_DIR / tags_file
    if not path.exists():
        return {}
    with open(path) as f:
        cards = json.load(f)
    return {c["name"]: c for c in cards}


def load_scryfall_tags() -> dict[str, list[str]]:
    """Load Scryfall function tags, return dict keyed by card name."""
    if not SCRYFALL_TAGS_FILE.exists():
        return {}
    with open(SCRYFALL_TAGS_FILE) as f:
        items = json.load(f)
    result = {}
    for item in items:
        name = item["name"].split(" // ")[0]  # normalize double-faced names
        result[name] = item.get("scryfall_tags", [])
    return result


# ── VALIDATORS ────────────────────────────────────────────────────────────────

def check_role(actual: dict, expected: dict, card_name: str) -> list[str]:
    failures = []
    expected_role = expected.get("role")
    if not expected_role:
        return failures
    actual_role = actual.get("role")
    # Accept a list of valid roles or a single string
    if isinstance(expected_role, list):
        if actual_role not in expected_role:
            failures.append(
                f"  ROLE: expected one of {expected_role}, got '{actual_role}'"
            )
    elif actual_role != expected_role:
        failures.append(
            f"  ROLE: expected '{expected_role}', got '{actual_role}'"
        )
    return failures


def check_synergy_tags(actual: dict, expected: dict, card_name: str) -> list[str]:
    failures = []
    actual_tags = set(actual.get("synergy_tags", []))

    must_include = expected.get("synergy_tags_must_include", [])
    must_exclude = expected.get("synergy_tags_must_exclude", [])

    for tag in must_include:
        if not _semantic_tag_match(tag, actual_tags):
            failures.append(
                f"  MISSING TAG: '{tag}' (or semantic equivalent) must be in synergy_tags, "
                f"got: {sorted(actual_tags)}"
            )

    for tag in must_exclude:
        # Exclusions use exact match only — we don't want fuzzy false positives
        if tag in actual_tags:
            failures.append(f"  FORBIDDEN TAG: '{tag}' must NOT be in synergy_tags")

    return failures


def check_provides(actual: dict, expected: dict, card_name: str) -> list[str]:
    failures = []
    actual_provides = set(actual.get("provides", []))
    must_include = expected.get("provides_must_include", [])

    for item in must_include:
        if not _semantic_tag_match(item, actual_provides):
            failures.append(f"  MISSING PROVIDE: '{item}' not found in provides: {sorted(actual_provides)}")

    return failures


def check_wants(actual: dict, expected: dict, card_name: str) -> list[str]:
    failures = []
    actual_wants = set(actual.get("wants", []))
    must_include = expected.get("wants_must_include", [])

    for item in must_include:
        if not _semantic_tag_match(item, actual_wants):
            failures.append(f"  MISSING WANT: '{item}' not found in wants: {sorted(actual_wants)}")

    return failures


def validate_card(actual: dict, golden: dict) -> list[str]:
    """Run all checks for a single card. Returns list of failure messages."""
    name = golden["name"]
    exp = golden["expected"]
    failures = []
    failures += check_role(actual, exp, name)
    failures += check_synergy_tags(actual, exp, name)
    failures += check_provides(actual, exp, name)
    failures += check_wants(actual, exp, name)
    return failures


# ── MODES ─────────────────────────────────────────────────────────────────────

def mode_static(tags_file: str = "card_tags.json"):
    """Validate a tags file against golden_cards.json."""
    golden = load_golden()
    tagged = load_tagged(tags_file)

    print("═" * 60)
    print(f"REGRESSION TEST — static mode ({tags_file} vs golden)")
    print("═" * 60)

    passed = 0
    failed = 0
    skipped = 0

    for g in golden:
        name = g["name"]
        if name not in tagged:
            print(f"\n  ⚠  SKIP  {name} — not found in card_tags.json")
            skipped += 1
            continue

        actual = tagged[name]
        failures = validate_card(actual, g)

        if failures:
            failed += 1
            print(f"\n  ✗  FAIL  {name}")
            for f in failures:
                print(f)
        else:
            passed += 1
            print(f"  ✓  PASS  {name}")

    print(f"\n{'═'*60}")
    print(f"Results: {passed} passed, {failed} failed, {skipped} skipped")
    print(f"{'═'*60}")

    return failed == 0


def mode_scryfall():
    """Validate Scryfall tagger tags against golden expectations."""
    golden = load_golden()
    scryfall = load_scryfall_tags()

    if not scryfall:
        print("ERROR: No Scryfall tags found. Run: python3 scryfall_tagger.py")
        return False

    print("═" * 60)
    print("REGRESSION TEST — Scryfall tagger tags vs golden")
    print("═" * 60)

    passed = 0
    failed = 0
    skipped = 0

    for g in golden:
        name = g["name"]
        sf_tags = scryfall.get(name)
        if sf_tags is None:
            print(f"\n  ⚠  SKIP  {name} — not found in Scryfall tags")
            skipped += 1
            continue

        # Build a fake "actual" dict with synergy_tags from Scryfall
        actual = {"synergy_tags": sf_tags}
        exp = g["expected"]
        failures = check_synergy_tags(actual, exp, name)

        if failures:
            failed += 1
            print(f"\n  ✗  FAIL  {name}")
            for f in failures:
                print(f)
        else:
            passed += 1
            print(f"  ✓  PASS  {name} — tags: {sf_tags}")

    print(f"\n{'═'*60}")
    print(f"Results: {passed} passed, {failed} failed, {skipped} skipped")
    print(f"{'═'*60}")

    return failed == 0


def mode_live():
    """Call Claude API for each golden card and validate response."""
    try:
        import requests
        from prompt_builder import build_prompt
        import re
    except ImportError as e:
        print(f"Import error: {e}")
        sys.exit(1)

    golden = load_golden()

    print("═" * 60)
    print("REGRESSION TEST — live mode (API calls)")
    print(f"Testing {len(golden)} golden cards...")
    print("═" * 60)

    passed = 0
    failed = 0
    api_errors = 0

    results_for_review = []

    for g in golden:
        name = g["name"]
        card_input = {
            "name": g["name"],
            "type_line": g["type_line"],
            "oracle_text": g["oracle_text"],
            "keywords": g.get("keywords", []),
            "cmc": g.get("cmc", 0),
        }

        print(f"\n  [{name}] calling API...")
        system, user = build_prompt(card_input)

        response = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"Content-Type": "application/json"},
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 1000,
                "system": system,
                "messages": [{"role": "user", "content": user}],
            },
        )

        if response.status_code != 200:
            print(f"    [API ERROR] {response.status_code}")
            api_errors += 1
            continue

        raw = response.json()["content"][0]["text"].strip()
        raw = re.sub(r"^```json\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)

        try:
            actual = json.loads(raw)
        except json.JSONDecodeError as e:
            print(f"    [JSON ERROR] {e}")
            api_errors += 1
            continue

        failures = validate_card(actual, g)
        results_for_review.append({"card": name, "actual": actual, "failures": failures})

        if failures:
            failed += 1
            print(f"  ✗ FAIL")
            for f in failures:
                print(f"      {f}")
            print(f"  Actual synergy_tags: {actual.get('synergy_tags', [])}")
        else:
            passed += 1
            print(f"  ✓ PASS — synergy_tags: {actual.get('synergy_tags', [])}")

    # Save live results for analysis
    output_path = BASE_DIR / "regression_results.json"
    with open(output_path, "w") as f:
        json.dump(results_for_review, f, indent=2)

    print(f"\n{'═'*60}")
    print(f"Results: {passed} passed, {failed} failed, {api_errors} API errors")
    if failed > 0:
        print(f"→ Add corrections to corrections.json to fix failures")
        print(f"→ Re-run to verify corrections work")
    print(f"Detailed results saved to: regression_results.json")
    print(f"{'═'*60}")

    return failed == 0 and api_errors == 0


# ── CORRECTION HELPER ─────────────────────────────────────────────────────────

def add_correction(wrong: str, correct: str, rule: str, scope: str = "global",
                   card: str = None, example_card: str = None):
    """
    Helper to add a new correction from CLI or script.
    
    Usage:
      python regression_test.py --add-correction \
        --wrong "flash-instant-speed" \
        --correct "reactive-protection" \
        --rule "Flash on protection cards = reactive-protection"
    """
    path = BASE_DIR / "corrections.json"
    with open(path) as f:
        corrections = json.load(f)

    # Generate next ID
    existing_ids = [c.get("id", "c000") for c in corrections]
    max_num = max(int(i[1:]) for i in existing_ids if i.startswith("c")) if existing_ids else 0
    new_id = f"c{max_num+1:03d}"

    new_correction = {
        "id": new_id,
        "scope": scope,
        "rule": rule,
        "wrong": wrong,
        "correct": correct,
    }
    if card:
        new_correction["card"] = card
    if example_card:
        new_correction["example_card"] = example_card

    corrections.append(new_correction)

    with open(path, "w") as f:
        json.dump(corrections, f, indent=2)

    print(f"Added correction {new_id}: '{wrong}' → '{correct}'")
    print(f"Run regression test to verify it fixes the issue.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="MTG Tagger Regression Test")
    parser.add_argument("--mode", choices=["static", "scryfall", "live"], default="static")
    parser.add_argument("--tags-file", type=str, default="card_tags.json",
                        help="Tags JSON file to validate (default: card_tags.json)")
    parser.add_argument("--add-correction", action="store_true")
    parser.add_argument("--wrong", type=str)
    parser.add_argument("--correct", type=str)
    parser.add_argument("--rule", type=str)
    parser.add_argument("--scope", type=str, default="global")
    parser.add_argument("--card", type=str)
    args = parser.parse_args()

    if args.add_correction:
        if not all([args.wrong, args.correct, args.rule]):
            print("--add-correction requires --wrong, --correct, --rule")
            sys.exit(1)
        add_correction(args.wrong, args.correct, args.rule, args.scope, args.card)
    elif args.mode == "static":
        ok = mode_static(args.tags_file)
        sys.exit(0 if ok else 1)
    elif args.mode == "scryfall":
        ok = mode_scryfall()
        sys.exit(0 if ok else 1)
    elif args.mode == "live":
        ok = mode_live()
        sys.exit(0 if ok else 1)
