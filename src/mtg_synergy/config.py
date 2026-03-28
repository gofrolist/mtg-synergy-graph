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
