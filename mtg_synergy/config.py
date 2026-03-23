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

# ── Recommendation scoring weights ────────────────────────────────────
RECOMMENDATION_WEIGHTS = {
    "LLM": 1000.0,
    "TOWER": 10.0,
    "RANK_TIEBREAK": 0.1,
    "UNSCORED_LLM_DEFAULT": 2,
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
