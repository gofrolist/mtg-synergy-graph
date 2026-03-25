"""Centralized paths, thresholds, and configuration constants.

All magic numbers and path definitions live here. Other modules import
from config instead of defining their own DATA_DIR / DB_PATH.
"""
import os
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).parent.parent
DATA_DIR = PROJECT_ROOT / "data"
DB_PATH = DATA_DIR / "tags.db"
CARDS_JSON = DATA_DIR / "oracle_cards.json"
EMBEDDINGS_NPY = DATA_DIR / "card2vec_embeddings.npy"
EMBEDDINGS_INDEX = DATA_DIR / "card2vec_index.json"
TOWER_MODEL_PATH = DATA_DIR / "tower_model.npz"

# ── Dynamic scoring weights (feature-based, computed at recommendation time) ─
SCORING_WEIGHTS = {
    "TOWER": 0.0,               # Disabled — trained on biased LLM data
    "MECHANICS": 2.0,           # Structured interaction scoring (trigger→effect chains + timing)
    "CMDR_TAG_OVERLAP": 3.0,    # Card provides↔wants with commander
    "DECK_TAG_OVERLAP": 1.5,    # Card provides↔wants with entire deck
    "STRATEGY": 2.0,            # Matching deck's detected strategies (tag-based)
    "STRATEGY_KEYWORD": 4.0,    # Oracle text matches strategy keywords (e.g. "+1/+1 counter" for counter decks)
    "TRIBAL_BONUS": 4.0,        # Creature matches deck's dominant type
    "TRIBAL_PENALTY": -3.0,     # Creature DOESN'T match (tribal decks only)
    "RANK": 0.2,                # Global card quality (EDHREC rank)
    "EDHREC_SYNERGY": 3.0,      # Per-commander EDHREC tiebreaker
    "CAUSAL": 2.0,              # Causal interaction graph (trigger/feeds edges)
    "EDHREC_INJECTION_THRESHOLD": 0.25,
}

# ── Legacy scoring weights (kept for backward compat with score_synergies.py) ─
RECOMMENDATION_WEIGHTS = {
    "LLM": 1000.0, "EDHREC_SYNERGY": 200.0, "OVERLAP": 20.0,
    "TOWER": 10.0, "RANK_TIEBREAK": 0.1, "UNSCORED_LLM_DEFAULT": 2,
    "EDHREC_INJECTION_THRESHOLD": 0.25,
}

# ── Graph building thresholds ─────────────────────────────────────────
GRAPH = {
    "EMBEDDING_MIN_SIMILARITY": 0.75,
    "MIN_EDGE_SCORE": 0.5,
    "MIN_PEER_SHARED_TAGS": 2,
    "MIN_SHARED_WANTS": 2,
    "COMMANDER_EDGE_MULTIPLIER": 5.0,
}

# ── Mechanics matching ────────────────────────────────────────────────
MECHANICS = {
    "MIN_INJECTION_SCORE": 1.5,
    "MIN_LLM_INJECTION_SCORE": 7,
}

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
