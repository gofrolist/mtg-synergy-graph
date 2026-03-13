"""
Rule-based pre-tagger — deterministic tagging from oracle text patterns.

Tags cards using regex pattern matching on oracle text, keywords, and type line.
Handles ~40-60% of common provides/wants tags instantly (no LLM needed).
Cards with low-confidence tags are flagged for LLM review.

Usage:
    python3 rule_tagger.py --check              # show coverage stats
    python3 rule_tagger.py --tag FILE            # tag candidates, output JSON
    python3 rule_tagger.py --compare             # compare rule tags vs LLM tags

Architecture:
    rule_tagger.py tags cards first (instant, free)
    batch_tagger.py handles remaining/complex cards (LLM, slow)
    tag_db.py imports the merged result
"""

import argparse
import json
import os
import re

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
SCRYFALL_FILE = os.path.join(DATA_DIR, "oracle_cards.json")


def get_oracle_text(card: dict) -> str:
    text = card.get("oracle_text", "")
    if not text:
        faces = card.get("card_faces", [])
        if faces:
            text = " // ".join(f.get("oracle_text", "") for f in faces)
    return text


def get_type_line(card: dict) -> str:
    return card.get("type_line", "")


# ── Pattern definitions ──
# Each rule: (compiled_regex, field_to_search, tag, tag_field)
# field_to_search: "text", "type", "keywords"
# tag_field: "provides", "wants", "role"

def _build_rules():
    """Build all tagging rules. Returns list of (regex, field, tag, tag_field, weight)."""
    rules = []

    def p(pattern, field, tag, tag_field, weight=1.0):
        rules.append((re.compile(pattern, re.IGNORECASE), field, tag, tag_field, weight))

    # Two tiers of rules:
    #   High-precision (>70%): used as ground truth, LLM must keep these
    #   Medium-precision (40-70%): suggested to LLM as hints
    # Weight encodes precision tier: 1.0 = high, 0.5 = medium

    # ── ROLE rules ──
    p(r"(destroy|exile) target (creature|permanent|artifact|enchantment|planeswalker)", "text", "removal", "role", 0.8)
    p(r"target (creature|permanent) gets -\d+/-\d+", "text", "removal", "role", 0.8)
    p(r"counter target spell", "text", "removal", "role", 0.8)
    p(r"\{T\}: Add \{", "text", "ramp", "role", 0.7)

    # ── PROVIDES: Tokens (90%) ──
    p(r"create .* \d+/\d+ .* (creature )?token", "text", "token-generation", "provides", 1.0)
    p(r"create .* (Treasure|Food|Clue|Blood|Map) token", "text", "token-generation", "provides", 1.0)

    # ── PROVIDES: Life (79%) ──
    p(r"you gain \d+ life", "text", "life-gain", "provides", 1.0)
    p(r"you gain life equal to", "text", "life-gain", "provides", 1.0)

    # ── PROVIDES: Mana (70%) ──
    p(r"add .* mana of any (color|type)", "text", "mana-flexibility", "provides", 1.0)
    p(r"\{T\}: Add \{C\}\{C\}", "text", "mana-acceleration", "provides", 1.0)
    p(r"\{T\}: Add \{[WUBRG]\}", "text", "mana-acceleration", "provides", 0.5)

    # ── PROVIDES: Damage (67%) ──
    p(r"deals? \d+ damage to (any target|target (creature|player|opponent))", "text", "damage-dealing", "provides", 1.0)
    p(r"deals? damage .* equal to", "text", "damage-dealing", "provides", 0.5)

    # ── PROVIDES: Removal (63%) ──
    p(r"(destroy|exile) target (creature|permanent)", "text", "targeted-removal", "provides", 1.0)
    p(r"(destroy|exile) target (artifact|enchantment)", "text", "targeted-removal", "provides", 1.0)
    p(r"target (creature|permanent) gets -\d+/-\d+", "text", "targeted-removal", "provides", 1.0)
    p(r"(destroy|exile) all (creatures|nonland permanents|permanents)", "text", "board-wipe", "provides", 1.0)
    p(r"counter target spell", "text", "targeted-removal", "provides", 0.5)

    # ── PROVIDES: Life drain ──
    p(r"each opponent loses \d+ life", "text", "life-drain", "provides", 1.0)
    p(r"deals? .* damage to each opponent", "text", "life-drain", "provides", 0.5)

    # ── PROVIDES: Counters ──
    p(r"put a \+1/\+1 counter on", "text", "counter-placement", "provides", 0.5)
    p(r"put \+1/\+1 counters on each", "text", "board-wide-counter-placement", "provides", 1.0)
    p(r"(that many plus one|twice that many|double the number of) .* counter", "text", "counter-amplification", "provides", 1.0)

    # ── PROVIDES: Draw ──
    p(r"draw (two|three|\d+) cards", "text", "card-draw", "provides", 0.5)
    p(r"whenever .* you draw a card", "text", "card-draw-payoff", "provides", 0.5)

    # ── PROVIDES: Graveyard ──
    p(r"return .* from (your|a) graveyard to (the battlefield|your hand)", "text", "graveyard-recursion", "provides", 0.5)

    # ── PROVIDES: Other ──
    p(r"gains? haste", "text", "haste-grant", "provides", 0.5)
    p(r"untap (target|all|each) (creature|permanent)", "text", "untap", "provides", 0.5)
    p(r"\{Q\}", "text", "untap", "provides", 1.0)
    p(r"search your library for .* (land|basic)", "text", "land-search", "provides", 0.5)
    p(r"trigger(s|ed)? .* additional time", "text", "trigger-doubling", "provides", 1.0)

    # ── WANTS (high precision only) ──
    p(r"whenever .* deals? combat damage", "text", "combat-events", "wants", 1.0)
    p(r"whenever an opponent casts", "text", "opponent-spell-casting", "wants", 1.0)
    p(r"whenever you gain life", "text", "life-gain-events", "wants", 1.0)
    p(r"whenever .* you control attacks", "text", "attack-events", "wants", 0.5)
    p(r"whenever .* (cast|play) a .* spell", "text", "spell-casting", "wants", 0.5)

    return rules


RULES = _build_rules()


def tag_card(card: dict) -> dict:
    """Apply rule-based tags to a single card. Returns tagged dict."""
    text = get_oracle_text(card)
    type_line = get_type_line(card)
    keywords = card.get("keywords", [])

    provides = set()
    wants = set()
    roles = {}  # role -> max weight

    for regex, field, tag, tag_field, weight in RULES:
        if field == "text":
            source = text
        elif field == "type":
            source = type_line
        elif field == "keywords":
            source = " ".join(keywords)
        else:
            continue

        if not source:
            continue

        if regex.search(source):
            if tag_field == "provides":
                provides.add(tag)
            elif tag_field == "wants":
                wants.add(tag)
            elif tag_field == "role":
                roles[tag] = max(roles.get(tag, 0), weight)

    # Pick highest-weight role
    role = max(roles, key=roles.get) if roles else ""

    # Confidence: how much did rules cover? 2+ tags = usable as base for LLM
    total_tags = len(provides) + len(wants)
    confidence = min(1.0, total_tags / 3)  # 3+ tags = high confidence

    return {
        "name": card.get("name", ""),
        "oracle_id": card.get("oracle_id", ""),
        "role": role,
        "provides": sorted(provides),
        "wants": sorted(wants),
        "confidence": round(confidence, 2),
        "source": "rules",
    }


def tag_cards(cards: list[dict]) -> tuple[list[dict], list[dict]]:
    """Tag a list of cards. Returns (high_confidence, low_confidence)."""
    high = []
    low = []
    for card in cards:
        tagged = tag_card(card)
        if tagged["confidence"] >= 0.5 and tagged["role"]:
            high.append(tagged)
        else:
            low.append(tagged)
    return high, low


def compare_with_llm(limit: int = 100):
    """Compare rule-based tags against LLM tags for accuracy."""
    with open(os.path.join(DATA_DIR, "top10000_tags.json")) as f:
        llm_tags = {c["oracle_id"]: c for c in json.load(f) if "oracle_id" in c}

    with open(SCRYFALL_FILE) as f:
        scryfall = json.load(f)

    # Match by oracle_id
    compared = 0
    role_match = 0
    provides_tp = provides_fp = provides_fn = 0
    wants_tp = wants_fp = wants_fn = 0

    for card in scryfall:
        oid = card.get("oracle_id")
        if not oid or oid not in llm_tags:
            continue

        rule_tagged = tag_card(card)
        llm = llm_tags[oid]

        if not rule_tagged["provides"] and not rule_tagged["wants"]:
            continue

        compared += 1
        if compared > limit:
            break

        # Role
        if rule_tagged["role"] == llm.get("role", ""):
            role_match += 1

        # Provides precision/recall
        rule_p = set(rule_tagged["provides"])
        llm_p = set(llm.get("provides", []))
        provides_tp += len(rule_p & llm_p)
        provides_fp += len(rule_p - llm_p)
        provides_fn += len(llm_p - rule_p)

        # Wants precision/recall
        rule_w = set(rule_tagged["wants"])
        llm_w = set(llm.get("wants", []))
        wants_tp += len(rule_w & llm_w)
        wants_fp += len(rule_w - llm_w)
        wants_fn += len(llm_w - rule_w)

    print(f"Compared {compared} cards (rule-tagger vs phi4:14b)")
    print(f"\nRole agreement: {role_match}/{compared} ({100*role_match/compared:.0f}%)")

    p_prec = provides_tp / (provides_tp + provides_fp) if (provides_tp + provides_fp) else 0
    p_rec = provides_tp / (provides_tp + provides_fn) if (provides_tp + provides_fn) else 0
    print(f"\nProvides tags:")
    print(f"  Precision: {p_prec:.0%} ({provides_tp} correct, {provides_fp} false positives)")
    print(f"  Recall:    {p_rec:.0%} ({provides_tp} found, {provides_fn} missed)")

    w_prec = wants_tp / (wants_tp + wants_fp) if (wants_tp + wants_fp) else 0
    w_rec = wants_tp / (wants_tp + wants_fn) if (wants_tp + wants_fn) else 0
    print(f"\nWants tags:")
    print(f"  Precision: {w_prec:.0%} ({wants_tp} correct, {wants_fp} false positives)")
    print(f"  Recall:    {w_rec:.0%} ({wants_tp} found, {wants_fn} missed)")


def check_coverage():
    """Show what percentage of Scryfall cards can be tagged by rules."""
    with open(SCRYFALL_FILE) as f:
        scryfall = json.load(f)

    total = 0
    high_conf = 0
    low_conf = 0
    no_tags = 0

    for card in scryfall:
        if not get_oracle_text(card) and "Land" not in get_type_line(card):
            continue
        layout = card.get("layout", "")
        if layout in ("token", "emblem", "art_series"):
            continue

        total += 1
        tagged = tag_card(card)
        if tagged["confidence"] >= 0.5 and tagged["role"]:
            high_conf += 1
        elif tagged["provides"] or tagged["wants"]:
            low_conf += 1
        else:
            no_tags += 1

    print(f"Total taggable cards: {total}")
    print(f"  High confidence (skip LLM): {high_conf} ({100*high_conf/total:.0f}%)")
    print(f"  Low confidence (LLM needed): {low_conf} ({100*low_conf/total:.0f}%)")
    print(f"  No rule matches (LLM needed): {no_tags} ({100*no_tags/total:.0f}%)")
    print(f"\nLLM calls saved: ~{100*high_conf/total:.0f}%")


def main():
    parser = argparse.ArgumentParser(description="Rule-based card pre-tagger")
    parser.add_argument("--check", action="store_true", help="Show coverage stats")
    parser.add_argument("--compare", action="store_true",
                        help="Compare rule tags vs LLM tags for accuracy")
    parser.add_argument("--compare-limit", type=int, default=500,
                        help="Number of cards to compare (default: 500)")
    parser.add_argument("--tag", type=str, metavar="FILE",
                        help="Tag candidates from JSON file")
    parser.add_argument("--output", type=str, help="Output file for tagged cards")
    parser.add_argument("--card", type=str, help="Tag a single card by name (for debugging)")
    args = parser.parse_args()

    if args.check:
        check_coverage()
    elif args.compare:
        compare_with_llm(args.compare_limit)
    elif args.card:
        with open(SCRYFALL_FILE) as f:
            scryfall = json.load(f)
        for card in scryfall:
            if card.get("name", "").lower() == args.card.lower():
                tagged = tag_card(card)
                print(json.dumps(tagged, indent=2))
                break
        else:
            print(f"Card not found: {args.card}")
    elif args.tag:
        with open(args.tag) as f:
            candidates = json.load(f)
        high, low = tag_cards(candidates)
        output = args.output or args.tag.replace("_candidates", "_rule_tags")
        with open(output, "w") as f:
            json.dump(high, f, indent=2)
        print(f"Tagged {len(high)} cards (high confidence) → {output}")
        print(f"Remaining {len(low)} cards need LLM tagging")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
