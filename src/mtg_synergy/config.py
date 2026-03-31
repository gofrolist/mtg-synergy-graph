"""Centralized paths, thresholds, and configuration constants."""
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).parent.parent.parent
DATA_DIR = PROJECT_ROOT / "data"
DB_PATH = DATA_DIR / "tags.db"
CARDS_JSON = DATA_DIR / "oracle_cards.json"

# ── Swap suggestion thresholds ────────────────────────────────────────
SWAP = {
    "MIN_MECHANICS_PROTECTION": 2.0,
    "TRIBAL_THRESHOLD": 0.15,
}

# ── DB connection settings ────────────────────────────────────────────
DB_PRAGMAS = {
    "journal_mode": "WAL",
    "synchronous": "NORMAL",
}

# ── Allowed table names for slug resolution (avoids f-string injection) ──
ALLOWED_SLUG_TABLES = frozenset({"edhrec_average_decks", "edhrec_card_synergy"})

# ── Supertypes (not creature subtypes, filtered from type_line parsing) ──
_SUPERTYPES = {"legendary", "basic", "snow", "world", "ongoing"}


def extract_subtypes(type_line: str) -> set[str]:
    """Extract creature/permanent subtypes from a type_line, DFC-aware.

    For DFCs like 'Legendary Creature — God // Legendary Enchantment',
    extracts subtypes from BOTH faces: {'god'}.
    Filters out supertypes, card types, and the '//' separator.
    Returns lowercase set.
    """
    if not type_line or "\u2014" not in type_line:
        return set()
    subtypes = set()
    # Split on '//' for DFCs, process each face independently
    for face in type_line.split("//"):
        face = face.strip()
        if "\u2014" not in face:
            continue
        after_dash = face.split("\u2014")[1].strip()
        for word in after_dash.split():
            w = word.lower().strip()
            if w and w not in _SUPERTYPES:
                subtypes.add(w)
    return subtypes
