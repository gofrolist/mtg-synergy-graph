"""
Card database — loads from Scryfall oracle_cards.json bulk data.

CARD_DB: dict keyed by oracle_id (UUID) → card data dict
NAME_INDEX: dict keyed by lowercase card name → oracle_id

Each card entry: {name, oracle_id, cmc, type_line, oracle_text, keywords, color_identity}
"""

import json
import os
import sys


DATA_FILE = os.path.join(os.path.dirname(__file__), "data", "oracle_cards.json")


def _load_cards():
    if not os.path.exists(DATA_FILE):
        print(
            f"ERROR: {DATA_FILE} not found.\n"
            f"Run 'python download_cards.py' first to download Scryfall bulk data.",
            file=sys.stderr,
        )
        sys.exit(1)

    with open(DATA_FILE, "r") as f:
        raw_cards = json.load(f)

    card_db = {}
    name_index = {}

    for card in raw_cards:
        # Skip reversible_card layout — no top-level oracle_id
        if card.get("layout") == "reversible_card":
            continue

        oracle_id = card.get("oracle_id")
        if not oracle_id:
            continue

        # For multi-face cards, combine oracle text from faces
        if "card_faces" in card:
            oracle_text = card["card_faces"][0].get("oracle_text", "")
        else:
            oracle_text = card.get("oracle_text", "")

        entry = {
            "name": card.get("name", ""),
            "oracle_id": oracle_id,
            "cmc": card.get("cmc", 0),
            "type_line": card.get("type_line", ""),
            "oracle_text": oracle_text,
            "keywords": card.get("keywords", []),
            "color_identity": card.get("color_identity", []),
        }

        card_db[oracle_id] = entry
        full_name = entry["name"].lower()
        name_index[full_name] = oracle_id

        # Index each face name for multi-face cards (MDFC, split, adventure, etc.)
        if " // " in full_name:
            for face_name in full_name.split(" // "):
                face_name = face_name.strip()
                if face_name and face_name not in name_index:
                    name_index[face_name] = oracle_id

    return card_db, name_index


CARD_DB, NAME_INDEX = _load_cards()
