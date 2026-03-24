# Architecture Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restructure the MTG Synergy Graph codebase from 43 flat root-level scripts into a proper Python package with clear module boundaries, centralized configuration, and a unified CLI.

**Architecture:** Extract the monolithic `synergy_graph.py` (4,123 lines) into focused submodules under a new `mtg_synergy/` package. Centralize DB access, configuration constants, and embedding loading. Preserve all existing public APIs by re-exporting from `synergy_graph.py` so external imports and tests continue to work unchanged.

**Tech Stack:** Python 3.12+, SQLite3, NumPy, argparse

**Key constraint:** All 63 existing tests must pass after every task. `synergy_graph.py` remains as a backward-compatible thin wrapper that re-exports from the new package — existing `from synergy_graph import X` imports continue working.

---

## File Structure

```
mtg_synergy/                         # NEW package
├── __init__.py                      # Package marker, version
├── config.py                        # All paths, constants, thresholds
├── db.py                            # Centralized DB connection factory
├── constants.py                     # SEMANTIC_BRIDGES, TRIGGER_EFFECT_BRIDGES, STAPLE_ROLES
├── graph/
│   ├── __init__.py
│   ├── builder.py                   # build_graph(), build_provides_wants_edges(), etc.
│   ├── edges.py                     # build_peer_edges(), build_shared_wants_edges(), build_embedding_edges()
│   └── idf.py                       # _compute_idf()
├── recommend/
│   ├── __init__.py
│   ├── engine.py                    # recommend_cards() core logic
│   ├── swaps.py                     # suggest_swaps(), _classify_card_slot(), show_swaps()
│   ├── affinity.py                  # _compute_commander_affinity()
│   └── display.py                   # recommendation output formatting
├── combos/
│   ├── __init__.py
│   ├── detector.py                  # find_combos(), find_combos_tiered(), find_partial_combos()
│   ├── anti_synergy.py              # find_anti_synergy()
│   └── display.py                   # show_combos(), show_combos_tiered()
├── analysis/
│   ├── __init__.py
│   ├── deck.py                      # show_deck_synergies(), show_card_synergies(), show_deck_analysis()
│   ├── strategy.py                  # compute_strategy_relevance(), _detect_deck_types()
│   ├── validation.py                # validate_against_curated()
│   └── visualization.py             # generate_visualization()
└── cli.py                           # run() — the CLI dispatcher
```

**Files modified:**
- `synergy_graph.py` — gutted to thin re-export wrapper (~50 lines)
- `tag_db.py` — use `mtg_synergy.db` for connection
- `score_synergies.py` — use `mtg_synergy.config` for paths
- `train_tower_model.py` — use `mtg_synergy.config` for paths
- `extract_mechanics.py` — use `mtg_synergy.config` for paths
- `mechanics_matcher.py` — use `mtg_synergy.config` for paths
- `card_embeddings.py` — use `mtg_synergy.config` for paths
- `validate_recommendations.py` — imports from new package

**Files unchanged:** All test files (imports from `synergy_graph` still work via re-exports), deck configs, data pipeline scripts.

---

## Task 1: Create `mtg_synergy/config.py` — Centralized paths and constants

**Files:**
- Create: `mtg_synergy/__init__.py`
- Create: `mtg_synergy/config.py`
- Test: `tests/test_config.py`

- [ ] **Step 1: Create package directory**

```bash
mkdir -p mtg_synergy
```

- [ ] **Step 2: Write the failing test**

```python
# tests/test_config.py
def test_config_paths_exist():
    from mtg_synergy.config import PROJECT_ROOT, DATA_DIR, DB_PATH
    assert PROJECT_ROOT.is_dir()
    assert DATA_DIR.is_dir()

def test_config_constants():
    from mtg_synergy.config import RECOMMENDATION_WEIGHTS
    assert "LLM" in RECOMMENDATION_WEIGHTS
    assert "TOWER" in RECOMMENDATION_WEIGHTS
    assert "RANK_TIEBREAK" in RECOMMENDATION_WEIGHTS
```

- [ ] **Step 3: Run test to verify it fails**

Run: `python3 -m pytest tests/test_config.py -v`
Expected: FAIL with "ModuleNotFoundError"

- [ ] **Step 4: Write `mtg_synergy/__init__.py`**

```python
"""MTG Synergy Graph — card synergy analysis for EDH/Commander."""
```

- [ ] **Step 5: Write `mtg_synergy/config.py`**

```python
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
# LLM score is primary (×1000), tower model sub-tiebreak (×10),
# EDHREC rank micro-tiebreak (×0.1). These multipliers create tiers
# so LLM 8 always beats LLM 7 regardless of tower/rank scores.
RECOMMENDATION_WEIGHTS = {
    "LLM": 1000.0,
    "TOWER": 10.0,
    "RANK_TIEBREAK": 0.1,
    "UNSCORED_LLM_DEFAULT": 2,  # Assumed LLM score for unscored cards
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
    "MIN_INJECTION_SCORE": 1.5,   # Minimum score to inject into candidates
    "MIN_LLM_INJECTION_SCORE": 7, # Minimum LLM score for injection
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
```

- [ ] **Step 6: Run test to verify it passes**

Run: `python3 -m pytest tests/test_config.py -v`
Expected: PASS

- [ ] **Step 7: Run full test suite**

Run: `python3 -m pytest tests/ -v`
Expected: All 63 tests pass (no existing code changed)

- [ ] **Step 8: Commit**

```bash
git add mtg_synergy/ tests/test_config.py
git commit -m "feat: add mtg_synergy package with centralized config"
```

---

## Task 2: Create `mtg_synergy/db.py` — Centralized DB connection factory

**Files:**
- Create: `mtg_synergy/db.py`
- Test: `tests/test_db.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_db.py
import sqlite3

def test_get_connection_returns_connection():
    from mtg_synergy.db import get_connection
    conn = get_connection()
    assert isinstance(conn, sqlite3.Connection)
    # Verify WAL mode is set
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"
    conn.close()

def test_get_connection_custom_path(tmp_path):
    from mtg_synergy.db import get_connection
    db_file = tmp_path / "test.db"
    conn = get_connection(str(db_file))
    conn.execute("CREATE TABLE t (id INTEGER)")
    conn.close()
    assert db_file.exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_db.py -v`
Expected: FAIL

- [ ] **Step 3: Write `mtg_synergy/db.py`**

```python
"""Centralized database connection factory.

All modules that need SQLite access should use get_connection() from here
instead of calling sqlite3.connect() directly.
"""
import sqlite3
from mtg_synergy.config import DB_PATH, DB_PRAGMAS


def get_connection(path: str | None = None) -> sqlite3.Connection:
    """Create a configured SQLite connection.

    Args:
        path: Database file path. Defaults to config.DB_PATH.

    Returns:
        Connection with WAL mode and NORMAL sync enabled.
    """
    db_path = path or str(DB_PATH)
    conn = sqlite3.connect(db_path)
    for pragma, value in DB_PRAGMAS.items():
        conn.execute(f"PRAGMA {pragma}={value}")
    return conn
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_db.py -v`
Expected: PASS

- [ ] **Step 5: Run full test suite**

Run: `python3 -m pytest tests/ -v`
Expected: All 63+ tests pass

- [ ] **Step 6: Commit**

```bash
git add mtg_synergy/db.py tests/test_db.py
git commit -m "feat: add centralized DB connection factory"
```

---

## Task 3: Extract `mtg_synergy/constants.py` — SEMANTIC_BRIDGES and friends

**Files:**
- Create: `mtg_synergy/constants.py`
- Modify: `synergy_graph.py` — replace inline dicts with imports
- Test: existing tests via `python3 -m pytest tests/ -v`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_constants.py
def test_semantic_bridges_loaded():
    from mtg_synergy.constants import SEMANTIC_BRIDGES
    assert isinstance(SEMANTIC_BRIDGES, dict)
    assert len(SEMANTIC_BRIDGES) > 50
    # Spot check key entries
    assert ("counter-placement", "counter-placement-events") in SEMANTIC_BRIDGES
    assert ("token-generation", "creature-etb") in SEMANTIC_BRIDGES

def test_trigger_effect_bridges_loaded():
    from mtg_synergy.constants import TRIGGER_EFFECT_BRIDGES
    assert isinstance(TRIGGER_EFFECT_BRIDGES, dict)
    assert "token-generation" in TRIGGER_EFFECT_BRIDGES

def test_staple_roles_loaded():
    from mtg_synergy.constants import STAPLE_ROLES
    assert "ramp" in STAPLE_ROLES
    assert "draw" in STAPLE_ROLES
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_constants.py -v`
Expected: FAIL

- [ ] **Step 3: Create `mtg_synergy/constants.py`**

Move `SEMANTIC_BRIDGES` (lines 57-562), `TRIGGER_EFFECT_BRIDGES` (lines 567-604), and `STAPLE_ROLES` (line 3734) from `synergy_graph.py` into `mtg_synergy/constants.py`. Also move `_provides_satisfies_want()` (lines 38-52) since it's tightly coupled to SEMANTIC_BRIDGES.

```python
"""Shared constants for the synergy graph system.

SEMANTIC_BRIDGES: Maps (provides_tag, wants_tag) → weight for cross-concept matching.
TRIGGER_EFFECT_BRIDGES: Maps effect_tags → trigger_tags for combo detection.
STAPLE_ROLES: Card roles that should never be cut from a deck.
"""

def _provides_satisfies_want(provide_tag: str, want_tag: str) -> float:
    """Check if a provides tag satisfies a wants tag. Returns weight 0.0-1.0."""
    if provide_tag == want_tag:
        return 1.0
    return SEMANTIC_BRIDGES.get((provide_tag, want_tag), 0.0)


SEMANTIC_BRIDGES = {
    # ... (copy entire dict from synergy_graph.py lines 57-562)
}

TRIGGER_EFFECT_BRIDGES = {
    # ... (copy entire dict from synergy_graph.py lines 567-604)
}

STAPLE_ROLES = {"ramp", "draw", "removal", "protection", "land"}
```

- [ ] **Step 4: Update `synergy_graph.py` — replace constants with imports**

At the top of `synergy_graph.py`, after existing imports, add:
```python
from mtg_synergy.constants import (
    SEMANTIC_BRIDGES, TRIGGER_EFFECT_BRIDGES, STAPLE_ROLES,
    _provides_satisfies_want,
)
```

Delete the inline definitions of `SEMANTIC_BRIDGES` (lines 55-562), `TRIGGER_EFFECT_BRIDGES` (lines 564-604), `STAPLE_ROLES` (line 3734), and `_provides_satisfies_want()` (lines 38-52) from `synergy_graph.py`.

- [ ] **Step 5: Run test to verify constants test passes**

Run: `python3 -m pytest tests/test_constants.py -v`
Expected: PASS

- [ ] **Step 6: Run full test suite**

Run: `python3 -m pytest tests/ -v`
Expected: All 63+ tests pass (imports from `synergy_graph.SEMANTIC_BRIDGES` still work since it's now imported at module level)

- [ ] **Step 7: Verify external imports still work**

```bash
python3 -c "from synergy_graph import SEMANTIC_BRIDGES; print(f'OK: {len(SEMANTIC_BRIDGES)} bridges')"
python3 -c "from mtg_synergy.constants import SEMANTIC_BRIDGES; print(f'OK: {len(SEMANTIC_BRIDGES)} bridges')"
```
Both should print OK.

- [ ] **Step 8: Commit**

```bash
git add mtg_synergy/constants.py synergy_graph.py tests/test_constants.py
git commit -m "refactor: extract SEMANTIC_BRIDGES and constants to mtg_synergy.constants"
```

---

## Task 4: Extract `mtg_synergy/graph/` — Graph building

**Files:**
- Create: `mtg_synergy/graph/__init__.py`
- Create: `mtg_synergy/graph/idf.py`
- Create: `mtg_synergy/graph/edges.py`
- Create: `mtg_synergy/graph/builder.py`
- Modify: `synergy_graph.py` — replace functions with re-exports

- [ ] **Step 1: Create graph package**

```bash
mkdir -p mtg_synergy/graph
```

- [ ] **Step 2: Extract `mtg_synergy/graph/idf.py`**

Move `_compute_idf()` (synergy_graph.py lines 607-643) to `mtg_synergy/graph/idf.py`:

```python
"""IDF (Inverse Document Frequency) computation for tag weighting."""
import math

def compute_idf(cards: list[dict]) -> dict[str, float]:
    # ... (copy function body, rename from _compute_idf to compute_idf)
```

- [ ] **Step 3: Extract `mtg_synergy/graph/edges.py`**

Move these functions from `synergy_graph.py`:
- `build_provides_wants_edges()` (lines 644-755)
- `build_peer_edges()` (lines 756-801)
- `build_shared_wants_edges()` (lines 802-865)
- `build_embedding_edges()` (lines 866-948)

Each function imports what it needs:
```python
"""Edge computation for the synergy graph."""
from collections import defaultdict
from mtg_synergy.constants import SEMANTIC_BRIDGES, _provides_satisfies_want
from mtg_synergy.graph.idf import compute_idf
from mtg_synergy.config import GRAPH

# ... paste functions here, replacing _compute_idf with compute_idf
```

- [ ] **Step 4: Extract `mtg_synergy/graph/builder.py`**

Move `build_graph()` (lines 949-1084) to `mtg_synergy/graph/builder.py`:

```python
"""Main graph builder — composes all edge types into a single graph."""
from mtg_synergy.graph.edges import (
    build_provides_wants_edges,
    build_peer_edges,
    build_shared_wants_edges,
    build_embedding_edges,
)

def build_graph(cards: list[dict], min_score: float = 0.5, deck_oids: set = None) -> dict:
    # ... (copy function body)
```

- [ ] **Step 5: Create `mtg_synergy/graph/__init__.py`**

```python
"""Graph building and edge computation."""
from mtg_synergy.graph.builder import build_graph
from mtg_synergy.graph.edges import (
    build_provides_wants_edges,
    build_peer_edges,
    build_shared_wants_edges,
    build_embedding_edges,
)

__all__ = [
    "build_graph",
    "build_provides_wants_edges",
    "build_peer_edges",
    "build_shared_wants_edges",
    "build_embedding_edges",
]
```

- [ ] **Step 6: Update `synergy_graph.py` — replace with re-exports**

Replace the moved functions with imports:
```python
from mtg_synergy.graph import (
    build_graph, build_provides_wants_edges,
    build_peer_edges, build_shared_wants_edges,
    build_embedding_edges,
)
```

Delete the original function bodies from `synergy_graph.py`.

- [ ] **Step 7: Run full test suite**

Run: `python3 -m pytest tests/ -v`
Expected: All tests pass

- [ ] **Step 8: Smoke test recommendation pipeline**

```bash
python3 synergy_graph.py --deck krenko --recommend 2>&1 | head -20
```
Expected: Same output as before (graph stats, recommendations)

- [ ] **Step 9: Commit**

```bash
git add mtg_synergy/graph/ synergy_graph.py
git commit -m "refactor: extract graph building to mtg_synergy.graph"
```

---

## Task 5: Extract `mtg_synergy/combos/` — Combo detection

**Files:**
- Create: `mtg_synergy/combos/__init__.py`
- Create: `mtg_synergy/combos/detector.py`
- Create: `mtg_synergy/combos/anti_synergy.py`
- Create: `mtg_synergy/combos/display.py`
- Modify: `synergy_graph.py` — replace with re-exports

- [ ] **Step 1: Create combos package**

```bash
mkdir -p mtg_synergy/combos
```

- [ ] **Step 2: Extract `mtg_synergy/combos/detector.py`**

Move these functions from `synergy_graph.py`:
- `find_combos()` (line 2148)
- `_expand_through_bridges()` (line 2230)
- `_combo_potential()` (line 2260)
- `_find_two_card_combos()` (line 2268)
- `_classify_combo()` (line 2348)
- `_find_quad_combos()` (line 2365)
- `find_combos_tiered()` (line 3501)
- `find_partial_combos()` (line 3680)
- `compute_strategy_relevance()` (line 3656)

Import from new locations:
```python
from mtg_synergy.constants import SEMANTIC_BRIDGES, TRIGGER_EFFECT_BRIDGES
from mtg_synergy.config import DB_PATH
```

- [ ] **Step 3: Extract `mtg_synergy/combos/anti_synergy.py`**

Move from `synergy_graph.py`:
- `find_anti_synergy()` (line 3737)
- `STAPLE_ROLES` reference → import from constants

```python
from mtg_synergy.constants import STAPLE_ROLES
```

- [ ] **Step 4: Extract `mtg_synergy/combos/display.py`**

Move from `synergy_graph.py`:
- `show_combos()` (line 2435)
- `show_combos_tiered()` (line 3793)
- `validate_against_curated()` (line 2479)

- [ ] **Step 5: Create `mtg_synergy/combos/__init__.py`**

```python
"""Combo detection: Spellbook confirmed, trigger chains, synergy cycles."""
from mtg_synergy.combos.detector import (
    find_combos, find_combos_tiered, find_partial_combos,
    compute_strategy_relevance,
)
from mtg_synergy.combos.anti_synergy import find_anti_synergy

__all__ = [
    "find_combos", "find_combos_tiered", "find_partial_combos",
    "compute_strategy_relevance", "find_anti_synergy",
]
```

- [ ] **Step 6: Update `synergy_graph.py` with re-exports**

```python
from mtg_synergy.combos import (
    find_combos, find_combos_tiered, find_partial_combos,
    compute_strategy_relevance, find_anti_synergy,
)
from mtg_synergy.combos.display import (
    show_combos, show_combos_tiered, validate_against_curated,
)
```

Delete original function bodies from `synergy_graph.py`.

- [ ] **Step 7: Run full test suite**

Run: `python3 -m pytest tests/ -v`
Expected: All tests pass (tests import from `synergy_graph` which re-exports)

- [ ] **Step 8: Commit**

```bash
git add mtg_synergy/combos/ synergy_graph.py
git commit -m "refactor: extract combo detection to mtg_synergy.combos"
```

---

## Task 6: Extract `mtg_synergy/recommend/` — Recommendation engine

**Files:**
- Create: `mtg_synergy/recommend/__init__.py`
- Create: `mtg_synergy/recommend/engine.py`
- Create: `mtg_synergy/recommend/swaps.py`
- Create: `mtg_synergy/recommend/affinity.py`
- Modify: `synergy_graph.py` — replace with re-exports

- [ ] **Step 1: Create recommend package**

```bash
mkdir -p mtg_synergy/recommend
```

- [ ] **Step 2: Extract `mtg_synergy/recommend/affinity.py`**

Move from `synergy_graph.py`:
- `_compute_commander_affinity()` (line 1718)

- [ ] **Step 3: Extract `mtg_synergy/recommend/engine.py`**

Move from `synergy_graph.py`:
- `recommend_cards()` (line 1180) — the 514-line main function
- `_deck_card_scores()` (line 1694)
- `_candidate_scores()` (line 1821)

Import from new locations:
```python
from mtg_synergy.config import RECOMMENDATION_WEIGHTS, MECHANICS, DATA_DIR
from mtg_synergy.recommend.affinity import _compute_commander_affinity
from mtg_synergy.combos.detector import find_partial_combos
```

- [ ] **Step 4: Extract `mtg_synergy/recommend/swaps.py`**

Move from `synergy_graph.py`:
- `suggest_swaps()` (line 1927)
- `_classify_card_slot()` (line 1854)
- `show_swaps()` (line 2111)

- [ ] **Step 5: Create `mtg_synergy/recommend/__init__.py`**

```python
"""Card recommendation and swap suggestion engine."""
from mtg_synergy.recommend.engine import recommend_cards
from mtg_synergy.recommend.swaps import suggest_swaps, show_swaps

__all__ = ["recommend_cards", "suggest_swaps", "show_swaps"]
```

- [ ] **Step 6: Update `synergy_graph.py` with re-exports**

```python
from mtg_synergy.recommend import recommend_cards, suggest_swaps, show_swaps
from mtg_synergy.recommend.engine import _deck_card_scores, _candidate_scores
from mtg_synergy.recommend.swaps import _classify_card_slot
from mtg_synergy.recommend.affinity import _compute_commander_affinity
```

Delete original function bodies.

- [ ] **Step 7: Run full test suite + smoke test**

Run: `python3 -m pytest tests/ -v`
Run: `python3 synergy_graph.py --deck krenko --recommend 2>&1 | head -20`
Expected: All tests pass, recommendations output works

- [ ] **Step 8: Commit**

```bash
git add mtg_synergy/recommend/ synergy_graph.py
git commit -m "refactor: extract recommendation engine to mtg_synergy.recommend"
```

---

## Task 7: Extract `mtg_synergy/analysis/` — Deck analysis and visualization

**Files:**
- Create: `mtg_synergy/analysis/__init__.py`
- Create: `mtg_synergy/analysis/deck.py`
- Create: `mtg_synergy/analysis/strategy.py`
- Create: `mtg_synergy/analysis/visualization.py`
- Modify: `synergy_graph.py` — replace with re-exports

- [ ] **Step 1: Create analysis package**

```bash
mkdir -p mtg_synergy/analysis
```

- [ ] **Step 2: Extract `mtg_synergy/analysis/deck.py`**

Move from `synergy_graph.py`:
- `show_card_synergies()` (line 1085)
- `show_deck_synergies()` (line 1109)
- `show_deck_analysis()` (line 3833)
- `load_merged()` (line 28)

- [ ] **Step 3: Extract `mtg_synergy/analysis/strategy.py`**

Move from `synergy_graph.py`:
- `_detect_deck_types()` (line 3151)
- `_filter_candidates()` (line 3189)
- `_find_embedding_candidates()` (line 3234)
- `build_from_commander()` (line 3298)

- [ ] **Step 4: Extract `mtg_synergy/analysis/visualization.py`**

Move from `synergy_graph.py`:
- `generate_visualization()` (line 2522) — the large HTML/D3 visualization generator

- [ ] **Step 5: Create `mtg_synergy/analysis/__init__.py`**

```python
"""Deck analysis, strategy detection, and visualization."""
from mtg_synergy.analysis.deck import (
    show_card_synergies, show_deck_synergies, show_deck_analysis, load_merged,
)
from mtg_synergy.analysis.strategy import (
    _detect_deck_types, _filter_candidates, _find_embedding_candidates,
    build_from_commander,
)

__all__ = [
    "show_card_synergies", "show_deck_synergies", "show_deck_analysis",
    "load_merged", "build_from_commander",
]
```

- [ ] **Step 6: Update `synergy_graph.py` with re-exports**

```python
from mtg_synergy.analysis import (
    show_card_synergies, show_deck_synergies, show_deck_analysis,
    load_merged, build_from_commander,
)
from mtg_synergy.analysis.strategy import (
    _detect_deck_types, _filter_candidates, _find_embedding_candidates,
)
from mtg_synergy.analysis.visualization import generate_visualization
```

Delete original function bodies.

- [ ] **Step 7: Run full test suite + smoke test all modes**

```bash
python3 -m pytest tests/ -v
python3 synergy_graph.py --deck krenko --recommend 2>&1 | head -5
python3 synergy_graph.py --deck krenko --combos 2>&1 | head -5
python3 synergy_graph.py --deck krenko --swaps 2>&1 | head -5
python3 synergy_graph.py --deck krenko --deck-view 2>&1 | head -5
```
Expected: All pass, all modes produce output

- [ ] **Step 8: Commit**

```bash
git add mtg_synergy/analysis/ synergy_graph.py
git commit -m "refactor: extract deck analysis and visualization to mtg_synergy.analysis"
```

---

## Task 8: Extract `mtg_synergy/cli.py` — CLI dispatcher

**Files:**
- Create: `mtg_synergy/cli.py`
- Modify: `synergy_graph.py` — thin wrapper

- [ ] **Step 1: Extract `mtg_synergy/cli.py`**

Move the `run()` function (synergy_graph.py lines 3893-4120) to `mtg_synergy/cli.py`.

Update all imports inside `run()` to use the new package:
```python
from mtg_synergy.graph import build_graph
from mtg_synergy.recommend import recommend_cards, suggest_swaps, show_swaps
from mtg_synergy.combos import find_combos, find_combos_tiered
from mtg_synergy.combos.display import show_combos, show_combos_tiered
from mtg_synergy.analysis import (
    show_card_synergies, show_deck_synergies, show_deck_analysis,
    load_merged, build_from_commander,
)
from mtg_synergy.analysis.strategy import (
    _detect_deck_types, _filter_candidates, _find_embedding_candidates,
)
from mtg_synergy.analysis.visualization import generate_visualization
from mtg_synergy.combos.display import validate_against_curated
from mtg_synergy.config import DATA_DIR
```

- [ ] **Step 2: Update `synergy_graph.py` to be a thin wrapper**

`synergy_graph.py` becomes approximately:

```python
"""
MTG Synergy Graph — backward-compatible entry point.

All logic has moved to the mtg_synergy package. This module re-exports
public symbols so existing `from synergy_graph import X` imports work.
"""
# Re-export all public symbols
from mtg_synergy.constants import (
    SEMANTIC_BRIDGES, TRIGGER_EFFECT_BRIDGES, STAPLE_ROLES,
    _provides_satisfies_want,
)
from mtg_synergy.graph import build_graph
from mtg_synergy.recommend import recommend_cards, suggest_swaps, show_swaps
from mtg_synergy.recommend.engine import _deck_card_scores, _candidate_scores
from mtg_synergy.recommend.swaps import _classify_card_slot
from mtg_synergy.recommend.affinity import _compute_commander_affinity
from mtg_synergy.combos import (
    find_combos, find_combos_tiered, find_partial_combos,
    compute_strategy_relevance, find_anti_synergy,
)
from mtg_synergy.combos.display import show_combos, show_combos_tiered, validate_against_curated
from mtg_synergy.analysis import (
    show_card_synergies, show_deck_synergies, show_deck_analysis,
    load_merged, build_from_commander,
)
from mtg_synergy.analysis.strategy import _detect_deck_types, _filter_candidates
from mtg_synergy.analysis.visualization import generate_visualization
from mtg_synergy.cli import run

import os
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")

if __name__ == "__main__":
    run()
```

- [ ] **Step 3: Run full test suite**

Run: `python3 -m pytest tests/ -v`
Expected: All 63+ tests pass

- [ ] **Step 4: Verify all CLI modes work**

```bash
python3 synergy_graph.py --deck krenko --recommend 2>&1 | tail -3
python3 synergy_graph.py --deck krenko --combos 2>&1 | tail -3
python3 synergy_graph.py --deck krenko --swaps 2>&1 | tail -3
python3 synergy_graph.py --deck krenko --deck-view 2>&1 | tail -3
```
Expected: All produce valid output

- [ ] **Step 5: Verify all external imports work**

```bash
python3 -c "from synergy_graph import SEMANTIC_BRIDGES, find_combos_tiered, find_partial_combos, find_anti_synergy, compute_strategy_relevance, STAPLE_ROLES, build_graph, recommend_cards; print('All imports OK')"
```
Expected: "All imports OK"

- [ ] **Step 6: Commit**

```bash
git add mtg_synergy/cli.py synergy_graph.py
git commit -m "refactor: extract CLI to mtg_synergy.cli, synergy_graph.py is now thin re-export wrapper"
```

---

## Task 9: Update external consumers to use new imports

**Files:**
- Modify: `tag_db.py` — import SEMANTIC_BRIDGES from mtg_synergy.constants
- Modify: `validate_recommendations.py` — import from mtg_synergy
- Modify: `train_recommender.py` — import from mtg_synergy.constants
- Modify: `score_synergies.py` — use mtg_synergy.config for paths
- Modify: `train_tower_model.py` — use mtg_synergy.config for paths
- Modify: `extract_mechanics.py` — use mtg_synergy.config for paths

- [ ] **Step 1: Update `tag_db.py`**

Change the lazy import of SEMANTIC_BRIDGES:
```python
# Before (line ~680):
from synergy_graph import SEMANTIC_BRIDGES

# After:
from mtg_synergy.constants import SEMANTIC_BRIDGES
```

- [ ] **Step 2: Update `validate_recommendations.py`**

```python
# Before (line ~87):
from synergy_graph import build_graph, _candidate_scores, _detect_deck_types

# After:
from mtg_synergy.graph import build_graph
from mtg_synergy.recommend.engine import _candidate_scores
from mtg_synergy.analysis.strategy import _detect_deck_types, _filter_candidates
from mtg_synergy.recommend.engine import _deck_card_scores
```

- [ ] **Step 3: Update `train_recommender.py`**

```python
# Before (line 29):
from synergy_graph import SEMANTIC_BRIDGES

# After:
from mtg_synergy.constants import SEMANTIC_BRIDGES
```

- [ ] **Step 4: Update `score_synergies.py`**

```python
# Before (line 39):
DB_PATH = os.path.join(os.path.dirname(__file__), "data", "tags.db")

# After:
from mtg_synergy.config import DB_PATH as _CONFIG_DB_PATH
DB_PATH = str(_CONFIG_DB_PATH)
```

- [ ] **Step 5: Update `train_tower_model.py`**

```python
# Before (lines 22-23):
DB_PATH = os.path.join(os.path.dirname(__file__), "data", "tags.db")
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")

# After:
from mtg_synergy.config import DB_PATH as _CFG_DB, DATA_DIR as _CFG_DATA
DB_PATH = str(_CFG_DB)
DATA_DIR = str(_CFG_DATA)
```

- [ ] **Step 6: Run full test suite**

Run: `python3 -m pytest tests/ -v`
Expected: All tests pass

- [ ] **Step 7: Verify pipelines work**

```bash
python3 -c "import tag_db; print('tag_db OK')"
python3 -c "import score_synergies; print('score_synergies OK')"
python3 -c "import train_tower_model; print('train_tower_model OK')"
python3 synergy_graph.py --deck krenko --recommend 2>&1 | head -5
```
Expected: All OK

- [ ] **Step 8: Commit**

```bash
git add tag_db.py validate_recommendations.py train_recommender.py score_synergies.py train_tower_model.py
git commit -m "refactor: update external consumers to import from mtg_synergy package"
```

---

## Task 10: Final verification and cleanup

**Files:**
- Modify: `CLAUDE.md` — update architecture section
- Run: full pipeline validation

- [ ] **Step 1: Verify line count reduction**

```bash
wc -l synergy_graph.py
# Should be ~50 lines (re-exports only)
```

- [ ] **Step 2: Verify package structure**

```bash
find mtg_synergy -name "*.py" | sort
# Should show all submodules
```

- [ ] **Step 3: Run full test suite one final time**

```bash
python3 -m pytest tests/ -v
```
Expected: All 63+ tests pass

- [ ] **Step 4: Run complete pipeline smoke test**

```bash
python3 synergy_graph.py --deck krenko --recommend 2>&1 | tail -5
python3 synergy_graph.py --deck krenko --combos 2>&1 | tail -5
python3 synergy_graph.py --deck krenko --swaps 2>&1 | tail -5
python3 synergy_graph.py --deck sram --recommend 2>&1 | tail -5
python3 compare_edhrec.py --fast --quiet 2>&1
```
Expected: All produce valid output, EDHREC alignment unchanged

- [ ] **Step 5: Update `CLAUDE.md` architecture section**

Update the Architecture section to reflect the new package structure. Add `mtg_synergy/` to the Key Files table.

- [ ] **Step 6: Final commit**

```bash
git add CLAUDE.md
git commit -m "docs: update CLAUDE.md with new mtg_synergy package architecture"
```

---

## Summary

| Task | What | Lines moved | Risk |
|------|------|-------------|------|
| 1 | `config.py` — paths & thresholds | ~60 new | Low |
| 2 | `db.py` — connection factory | ~25 new | Low |
| 3 | `constants.py` — SEMANTIC_BRIDGES etc. | ~550 moved | Medium (many importers) |
| 4 | `graph/` — graph building | ~440 moved | Medium |
| 5 | `combos/` — combo detection | ~500 moved | Medium |
| 6 | `recommend/` — recommendations | ~700 moved | High (largest function) |
| 7 | `analysis/` — deck analysis & viz | ~1200 moved | Medium |
| 8 | `cli.py` — CLI dispatcher | ~230 moved | Low |
| 9 | External consumers | ~10 changed | Low |
| 10 | Verification & docs | 0 | Low |

After completion: `synergy_graph.py` goes from **4,123 lines → ~50 lines** (re-exports only). All 63+ tests pass. All CLI modes work. All external imports work.
