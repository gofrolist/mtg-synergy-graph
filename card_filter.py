"""
Filter Kyler-relevant cards for LLM tagging.

Source 1 (primary): EDHREC API — curated cards for Kyler, Sigardian Emissary
Source 2 (supplement): Scryfall bulk data — regex filters for cards EDHREC may miss
Source 3 (forced): Current deck cards from main.py

Usage:
    python3 card_filter.py
"""

import json
import os
import re
import sys
import urllib.request

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
SCRYFALL_FILE = os.path.join(DATA_DIR, "oracle_cards.json")
OUTPUT_FILE = os.path.join(DATA_DIR, "kyler_candidates.json")

EDHREC_URL = "https://json.edhrec.com/pages/commanders/kyler-sigardian-emissary.json"
TOTAL_CAP = 500


def _load_deck_names() -> set[str]:
    """Load card names from main.py DECKLIST + DISTRACTORS."""
    sys.path.insert(0, os.path.dirname(__file__))
    from main import DECKLIST, DISTRACTORS
    return set(DECKLIST) | set(DISTRACTORS) | {"Kyler, Sigardian Emissary"}


def get_oracle_text(card: dict) -> str:
    """Get oracle text, handling multi-face cards."""
    text = card.get("oracle_text", "")
    if not text:
        faces = card.get("card_faces", [])
        if faces:
            text = faces[0].get("oracle_text", "")
    return text


def build_scryfall_index(cards: list[dict]) -> dict[str, dict]:
    """Build name -> card dict from Scryfall bulk data."""
    index = {}
    for card in cards:
        oracle_id = card.get("oracle_id")
        if not oracle_id or card.get("layout") == "reversible_card":
            continue
        name = card.get("name", "")
        index[name.lower()] = card
        # Also index face names for MDFCs
        if " // " in name:
            for face in name.split(" // "):
                face = face.strip()
                if face:
                    index[face.lower()] = card
    return index


def fetch_edhrec() -> list[dict]:
    """Fetch EDHREC card data for Kyler."""
    print("Fetching EDHREC data for Kyler...")
    req = urllib.request.Request(EDHREC_URL, headers={"User-Agent": "MTGDeckOptimizer/1.0"})
    try:
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        print(f"  EDHREC fetch failed: {e}")
        return []

    cardlists = data["container"]["json_dict"]["cardlists"]
    results = []
    seen = set()
    for cl in cardlists:
        category = cl.get("tag", "")
        for cv in cl.get("cardviews", []):
            name = cv["name"]
            if name in seen:
                continue
            seen.add(name)
            results.append({
                "name": name,
                "edhrec_synergy": cv.get("synergy", 0),
                "edhrec_inclusion": cv.get("inclusion", 0),
                "edhrec_category": category,
            })

    print(f"  Found {len(results)} unique cards from EDHREC")
    return results


def scryfall_supplement(scryfall_cards: list[dict], already_picked: set[str]) -> dict[str, dict]:
    """Find high-synergy cards from Scryfall that EDHREC might miss (newer cards, niche picks)."""
    result = {}

    for card in scryfall_cards:
        oracle_id = card.get("oracle_id")
        if not oracle_id or oracle_id in already_picked:
            continue
        if card.get("layout") == "reversible_card":
            continue
        if set(card.get("color_identity", [])) > {"G", "W"}:
            continue

        type_line = card.get("type_line", "")
        oracle_text = get_oracle_text(card).lower()

        # Only pick high-value cards EDHREC might miss
        picked = False
        reason = ""

        # Human + counter synergy (the Kyler sweet spot)
        if "Human" in type_line and "+1/+1 counter" in oracle_text:
            picked = True
            reason = "human+counter"

        # Board-wide counter placement
        elif re.search(r"\+1/\+1 counter[s]? on (?:each|all)", oracle_text):
            picked = True
            reason = "board-counter"

        # Counter doublers
        elif re.search(r"twice.*counter|double.*counter|plus one.*counter", oracle_text):
            picked = True
            reason = "counter-doubler"

        # Trigger doublers
        elif re.search(r"trigger.*additional time|additional time", oracle_text):
            picked = True
            reason = "trigger-doubler"

        if picked:
            result[oracle_id] = {
                "name": card.get("name", ""),
                "oracle_id": oracle_id,
                "type_line": type_line,
                "oracle_text": get_oracle_text(card),
                "keywords": card.get("keywords", []),
                "cmc": card.get("cmc", 0),
                "color_identity": card.get("color_identity", []),
                "filter_pass": 2,
                "filter_reason": reason,
            }

    return result


def run():
    # Load Scryfall bulk data
    print("Loading oracle_cards.json...")
    with open(SCRYFALL_FILE) as f:
        scryfall_cards = json.load(f)
    print(f"Loaded {len(scryfall_cards)} cards")
    scryfall_index = build_scryfall_index(scryfall_cards)

    # Source 1: EDHREC
    edhrec_cards = fetch_edhrec()

    # Resolve EDHREC names to Scryfall card data
    candidates = {}
    missing_from_scryfall = []
    for ec in edhrec_cards:
        name = ec["name"]
        sc = scryfall_index.get(name.lower())
        if not sc:
            missing_from_scryfall.append(name)
            continue
        oracle_id = sc.get("oracle_id")
        if not oracle_id:
            continue
        candidates[oracle_id] = {
            "name": sc.get("name", name),
            "oracle_id": oracle_id,
            "type_line": sc.get("type_line", ""),
            "oracle_text": get_oracle_text(sc),
            "keywords": sc.get("keywords", []),
            "cmc": sc.get("cmc", 0),
            "color_identity": sc.get("color_identity", []),
            "filter_pass": 1,
            "filter_reason": "edhrec",
            "edhrec_synergy": ec["edhrec_synergy"],
            "edhrec_inclusion": ec["edhrec_inclusion"],
            "edhrec_category": ec["edhrec_category"],
        }

    print(f"\nSource 1 (EDHREC): {len(candidates)} cards resolved")
    if missing_from_scryfall:
        print(f"  {len(missing_from_scryfall)} EDHREC cards not found in Scryfall: {missing_from_scryfall[:5]}...")

    # Source 2: Scryfall supplement
    print("\nSource 2 (Scryfall supplement)...")
    supplement = scryfall_supplement(scryfall_cards, set(candidates.keys()))
    print(f"  Found {len(supplement)} additional cards")
    reasons = {}
    for c in supplement.values():
        r = c["filter_reason"]
        reasons[r] = reasons.get(r, 0) + 1
    for r, count in sorted(reasons.items()):
        print(f"    {r}: {count}")

    # Source 3: Forced deck cards (guaranteed inclusion)
    deck_names = _load_deck_names()
    deck_name_lower = {n.lower() for n in deck_names}
    deck_forced = {}
    all_picked_ids = set(candidates.keys()) | set(supplement.keys())
    all_picked_names = {c["name"].lower() for c in candidates.values()} | {c["name"].lower() for c in supplement.values()}
    for card in scryfall_cards:
        oracle_id = card.get("oracle_id")
        if not oracle_id:
            continue
        name = card.get("name", "")
        name_lower = name.lower()
        matched = name_lower in deck_name_lower
        if not matched and " // " in name_lower:
            matched = any(face.strip() in deck_name_lower for face in name_lower.split(" // "))
        if matched and oracle_id not in all_picked_ids and name_lower not in all_picked_names:
            deck_forced[oracle_id] = {
                "name": name,
                "oracle_id": oracle_id,
                "type_line": card.get("type_line", ""),
                "oracle_text": get_oracle_text(card),
                "keywords": card.get("keywords", []),
                "cmc": card.get("cmc", 0),
                "color_identity": card.get("color_identity", []),
                "filter_pass": 0,
                "filter_reason": "deck-card",
            }
        # Also force deck cards that ARE in supplement but might get cut by cap
        elif matched and oracle_id in supplement:
            supplement[oracle_id]["filter_pass"] = 0
            supplement[oracle_id]["filter_reason"] = "deck-card"
    if deck_forced:
        print(f"\nSource 3 (deck cards): {len(deck_forced)} forced additions")

    # Merge all sources
    all_candidates = {**candidates, **supplement, **deck_forced}
    candidates_list = list(all_candidates.values())

    # Cap if over limit (prioritize EDHREC + deck cards > supplement)
    if len(candidates_list) > TOTAL_CAP:
        must_keep = [c for c in candidates_list if c["filter_reason"] in ("edhrec", "deck-card")]
        rest = [c for c in candidates_list if c["filter_reason"] not in ("edhrec", "deck-card")]
        remaining = TOTAL_CAP - len(must_keep)
        candidates_list = must_keep + rest[:max(0, remaining)]
        print(f"\nCapped to {len(candidates_list)} (from {len(all_candidates)})")

    print(f"\nTotal candidates: {len(candidates_list)}")

    # Final distribution
    reasons_final = {}
    for c in candidates_list:
        r = c["filter_reason"]
        reasons_final[r] = reasons_final.get(r, 0) + 1
    for r, count in sorted(reasons_final.items(), key=lambda x: -x[1]):
        print(f"  {r}: {count}")

    with open(OUTPUT_FILE, "w") as f:
        json.dump(candidates_list, f, indent=2)
    print(f"\nSaved to {OUTPUT_FILE}")


if __name__ == "__main__":
    run()
