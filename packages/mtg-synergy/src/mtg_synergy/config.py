"""Centralized paths, thresholds, and configuration constants."""
import os
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────


def _find_data_dir() -> Path:
    """Find the data/ directory by walking up from this module.

    Works in monorepo (packages/mtg-synergy/src/mtg_synergy/) and standalone.
    """
    p = Path(__file__).resolve().parent
    for _ in range(8):  # max depth
        candidate = p / "data"
        if candidate.is_dir():
            return candidate
        p = p.parent
    # Fallback: relative to file (original behavior)
    return Path(__file__).parent.parent.parent / "data"

DATA_DIR = Path(os.environ.get("MTG_SYNERGY_DATA_DIR", _find_data_dir()))
DB_PATH = Path(os.environ.get("MTG_SYNERGY_DB_PATH", DATA_DIR / "tags.db"))
CARDS_JSON = DATA_DIR / "oracle_cards.json"

# Inference artifact directory (model, edge caches).
# Defaults to DATA_DIR. Set MTG_SYNERGY_ARTIFACT_DIR for separate deployment.
ARTIFACT_DIR = Path(os.environ.get("MTG_SYNERGY_ARTIFACT_DIR", DATA_DIR))

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
