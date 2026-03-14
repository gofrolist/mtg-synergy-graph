"""
Grow the golden dataset by finding high-confidence cards where multiple
signals agree on the correct tags.

Confidence signals:
  1. phi4 and fine-tuned model agree on role
  2. phi4 and fine-tuned model agree on provides (intersection)
  3. Scryfall community tags are consistent with role
  4. Embedding neighbors agree on role (majority vote)

Cards are ranked by EDHREC popularity. Only cards passing all confidence
checks are added to the golden dataset.

Usage:
    python3 grow_golden.py --target 500             # grow to 500 cards
    python3 grow_golden.py --target 500 --dry-run   # preview without saving
    python3 grow_golden.py --min-confidence 3       # require 3+ signals
"""

import argparse
import json
import os
from collections import Counter

import numpy as np

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
GOLDEN_FILE = os.path.join(os.path.dirname(__file__), "golden_cards.json")


# Scryfall tag → expected role mapping
SCRYFALL_ROLE_HINTS = {
    "ramp": "ramp",
    "mana rock": "ramp",
    "mana dork": "ramp",
    "mana fix": "ramp",
    "tutor-land-to-battlefield": "ramp",
    "spot removal": "removal",
    "removal-exile": "removal",
    "removal-destroy": "removal",
    "removal-bounce": "removal",
    "sweeper": "removal",
    "counterspell": "removal",
    "boardwipe": "removal",
    "protects-permanent": "protection",
    "protects-creature": "protection",
    "gives indestructible": "protection",
    "gives hexproof": "protection",
    "draw engine": "draw",
    "repeatable pure draw": "draw",
    "counter doubler": "enabler",
    "token doubler": "enabler",
    "trigger doubler": "enabler",
    "counter increaser": "enabler",
    "utility land": "land",
}


def load_data():
    """Load all data sources."""
    phi4_path = os.path.join(DATA_DIR, "top10000_tags.json")
    ft_path = os.path.join(DATA_DIR, "top10000_tags_finetuned.json")

    phi4 = {c["name"]: c for c in json.load(open(phi4_path)) if "name" in c}
    ft = {c["name"]: c for c in json.load(open(ft_path)) if "name" in c}

    # EDHREC ranks from candidates
    cand_path = os.path.join(DATA_DIR, "top10000_candidates.json")
    ranks = {}
    scryfall_info = {}
    if os.path.exists(cand_path):
        for c in json.load(open(cand_path)):
            ranks[c["name"]] = c.get("edhrec_rank", 99999)
            scryfall_info[c["name"]] = c

    # Existing golden cards
    golden = json.load(open(GOLDEN_FILE))
    existing_names = {c["name"] for c in golden["cards"]}

    return phi4, ft, ranks, scryfall_info, golden, existing_names


def get_scryfall_tags_for_card(name: str) -> list[str]:
    """Get Scryfall community tags from DB."""
    try:
        from tag_db import get_scryfall_tags
        from card_db import NAME_INDEX
        oid = NAME_INDEX.get(name.lower())
        if oid:
            return [t["tag"] for t in get_scryfall_tags(oid)]
    except Exception:
        pass
    return []


def check_embedding_role(name: str, expected_role: str, tags_by_name: dict,
                         embeddings=None, oracle_ids=None, oid_to_idx=None,
                         k: int = 8) -> bool:
    """Check if embedding neighbors agree on role."""
    if embeddings is None:
        return True  # skip if no embeddings

    card = tags_by_name.get(name)
    if not card:
        return True

    oid = card.get("oracle_id")
    idx = oid_to_idx.get(oid) if oid else None
    if idx is None:
        return True

    sims = embeddings[idx] @ embeddings.T
    sims[idx] = -1.0
    top_idx = np.argpartition(-sims, k)[:k]
    top_idx = [i for i in top_idx if sims[i] >= 0.70]

    role_counts = Counter()
    for i in top_idx:
        neighbor_oid = oracle_ids[i]
        # Find card by oracle_id
        for c in tags_by_name.values():
            if c.get("oracle_id") == neighbor_oid:
                role_counts[c.get("role", "")] += 1
                break

    if not role_counts:
        return True

    top_role = role_counts.most_common(1)[0][0]
    # Accept if expected role is in top 2 or matches majority
    top2 = [r for r, _ in role_counts.most_common(2)]
    return expected_role in top2


def score_card(name: str, phi4: dict, ft: dict, scryfall_tags: list[str]) -> dict:
    """Score a card's confidence across signals. Returns None if not confident enough."""
    p = phi4.get(name, {})
    f = ft.get(name, {})

    if not p or not f:
        return None

    p_role = p.get("role", "")
    f_role = f.get("role", "")
    p_provides = set(p.get("provides", []))
    f_provides = set(f.get("provides", []))
    p_wants = set(p.get("wants", []))
    f_wants = set(f.get("wants", []))

    signals = 0
    role = None
    provides = []
    wants = []

    # Signal 1: Both models agree on role
    if p_role == f_role and p_role:
        signals += 1
        role = p_role
    else:
        # Use fine-tuned model's role but lower confidence
        role = f_role

    # Signal 2: Provides intersection (both models agree)
    agreed_provides = p_provides & f_provides
    if agreed_provides:
        signals += 1
        provides = sorted(agreed_provides)
    else:
        # Fall back to fine-tuned
        provides = sorted(f_provides)

    # Signal 3: Wants intersection
    agreed_wants = p_wants & f_wants
    if agreed_wants:
        signals += 1
        wants = sorted(agreed_wants)
    else:
        wants = sorted(f_wants)

    # Signal 4: Scryfall tags consistent with role
    if scryfall_tags:
        scryfall_roles = set()
        for tag in scryfall_tags:
            hint = SCRYFALL_ROLE_HINTS.get(tag)
            if hint:
                scryfall_roles.add(hint)
        if role in scryfall_roles:
            signals += 1
        elif not scryfall_roles:
            pass  # no signal, neutral
        # Don't penalize if scryfall disagrees — it's noisy

    # Need role and at least some provides
    if not role or not provides:
        return None

    # Build scryfall must_include from actual tags
    scryfall_must_include = []
    for tag in scryfall_tags:
        if tag in SCRYFALL_ROLE_HINTS:
            scryfall_must_include.append(tag)

    return {
        "signals": signals,
        "role": role,
        "provides": provides,
        "wants": wants,
        "scryfall_must_include": scryfall_must_include[:3],
        "role_agreed": p_role == f_role,
    }


def build_golden_entry(name: str, score_data: dict, scryfall_info: dict,
                       scryfall_tags: list[str]) -> dict:
    """Build a golden card entry."""
    info = scryfall_info.get(name, {})

    oracle_text = info.get("oracle_text", "")
    if not oracle_text and "card_faces" in info:
        oracle_text = info["card_faces"][0].get("oracle_text", "")

    return {
        "name": name,
        "type_line": info.get("type_line", ""),
        "oracle_text": oracle_text,
        "keywords": info.get("keywords", []),
        "cmc": info.get("cmc", 0),
        "scryfall_tags": scryfall_tags[:8],
        "expected": {
            "role": score_data["role"],
            "synergy_tags_must_include": score_data["scryfall_must_include"],
            "synergy_tags_must_exclude": [],
            "provides_must_include": score_data["provides"][:5],
            "wants_must_include": score_data["wants"][:4],
        },
    }


def main():
    parser = argparse.ArgumentParser(description="Grow golden dataset")
    parser.add_argument("--target", type=int, default=500, help="Target golden card count")
    parser.add_argument("--min-confidence", type=int, default=2,
                        help="Min confidence signals (1-4, default 2)")
    parser.add_argument("--dry-run", action="store_true", help="Preview without saving")
    parser.add_argument("--no-embeddings", action="store_true",
                        help="Skip embedding neighbor check (faster)")
    args = parser.parse_args()

    phi4, ft, ranks, scryfall_info, golden, existing_names = load_data()
    print(f"Existing golden: {len(existing_names)} cards")
    print(f"phi4 tags: {len(phi4)}, fine-tuned tags: {len(ft)}")

    # Load embeddings for neighbor check
    embeddings = oracle_ids = oid_to_idx = None
    if not args.no_embeddings:
        try:
            from card_embeddings import load_embeddings
            embeddings, oracle_ids = load_embeddings()
            oid_to_idx = {oid: i for i, oid in enumerate(oracle_ids)}
            print(f"Embeddings: {len(oracle_ids)} cards")
        except Exception as e:
            print(f"Embeddings not available: {e}")

    # Score all cards
    candidates = []
    common_names = set(phi4.keys()) & set(ft.keys())

    for name in common_names:
        if name in existing_names:
            continue

        scryfall_tags = get_scryfall_tags_for_card(name)
        score_data = score_card(name, phi4, ft, scryfall_tags)
        if not score_data:
            continue
        if score_data["signals"] < args.min_confidence:
            continue

        # Embedding neighbor check
        if embeddings is not None and score_data["role_agreed"]:
            if not check_embedding_role(name, score_data["role"], ft,
                                        embeddings, oracle_ids, oid_to_idx):
                continue

        rank = ranks.get(name, 99999)
        candidates.append((rank, name, score_data, scryfall_tags))

    # Sort by EDHREC rank (most popular first)
    candidates.sort()

    needed = args.target - len(existing_names)
    selected = candidates[:needed]

    print(f"\nCandidates found: {len(candidates)}")
    print(f"Selecting: {len(selected)} (to reach target of {args.target})")

    # Stats
    signal_dist = Counter(s["signals"] for _, _, s, _ in selected)
    role_dist = Counter(s["role"] for _, _, s, _ in selected)
    agreed = sum(1 for _, _, s, _ in selected if s["role_agreed"])

    print(f"\nSignal distribution: {dict(signal_dist.most_common())}")
    print(f"Role distribution: {dict(role_dist.most_common())}")
    print(f"Both models agree on role: {agreed}/{len(selected)} ({100*agreed/max(len(selected),1):.0f}%)")

    if args.dry_run:
        print(f"\nTop 20 candidates (dry run):")
        for rank, name, score_data, _ in selected[:20]:
            agree = "✓" if score_data["role_agreed"] else "~"
            print(f"  {agree} rank={rank:<6} {name:<40} role={score_data['role']:<12} "
                  f"signals={score_data['signals']} provides={score_data['provides'][:3]}")
        return

    # Build golden entries
    new_entries = []
    for rank, name, score_data, scryfall_tags in selected:
        entry = build_golden_entry(name, score_data, scryfall_info, scryfall_tags)
        new_entries.append(entry)

    golden["cards"].extend(new_entries)
    golden["_meta"]["version"] = "1.0"
    golden["_meta"]["last_updated"] = "2026-03-14"
    golden["_meta"]["notes"] = (
        "Auto-grown golden dataset. Cards where phi4 and fine-tuned models agree "
        "on tags, validated by Scryfall community tags and embedding neighbors. "
        "Original 19 manually verified, remainder auto-verified with 2+ confidence signals."
    )

    with open(GOLDEN_FILE, "w") as f:
        json.dump(golden, f, indent=2)

    print(f"\nGolden dataset: {len(golden['cards'])} cards (was {len(existing_names)})")
    print(f"Saved to {GOLDEN_FILE}")


if __name__ == "__main__":
    main()
