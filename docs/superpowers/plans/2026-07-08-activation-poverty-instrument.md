# Activation-Poverty Instrument Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a standing read-side instrument that measures per-commander coverage (`earned_top30`) across the full legal commander universe, so coverage-oriented rule work can be gated on a non-EDHREC-NDCG metric.

**Architecture:** A pure metric core (`bench/coverage.py`) turns an `engine.page()` result into three coverage scalars. A `toughness_payoff` cohort predicate (in the existing `bench/cohorts.py`) names the follow-on rule's target cohort. A CLI module (`bench/coverage_report.py` + `scripts/coverage_report.py` thin wrapper) provides `census` (full-universe → pinned `.audit/coverage/baseline.json`), `queue` (poverty ranking), and `gate` (cohort + stratified control Δ) modes. Zero scoring-path mutation.

**Tech Stack:** Python 3.13, sqlite3, `uv run`, pytest (TDD), argparse. No new dependencies.

## Global Constraints

- **Zero scoring-path changes.** `uv run scripts/bench.py audit --expect-identity` must stay bitwise-identical before and after this work. Nothing under `complement_rules/`, `universal_scorer.py`, `graph_engine.py`, `embeddings/`, or `src/mtg_synergy_graph/data/scoring_weights.json` may change.
- **Read-side instrument.** New code reads the DB and calls `engine.page()`; it never writes to `data/synergy.db` or mutates engine state beyond the existing `engine._score_cache.clear()` pattern.
- **No literal project-relative DB path in tests.** Never pass `db="data/synergy.db"` (or any repo-root-relative path) to code reaching `open_db()`/`sqlite3.connect()`. Use `tmp_path`. (CLAUDE.md → Conventions; a session-autouse fixture fails the run if a `*.db` file appears at the repo root.)
- **"Earned" definition (single source of truth).** A candidate is *earned* iff its `Recommendation.scores` dict has ≥1 key outside `_NON_RULE_BUCKET_KEYS = frozenset({"staple", "embedding", "concentration_dampen", "total"})` with a non-zero value. NOTE — this refines the spec's "subtract staple + rank_bonus" wording: `rank_bonus`/`cmc_bonus`/`circuit_bonus` are folded into `total` but are **not** exposed as `scores` buckets (only `embedding` is), so a synergy-bucket-presence test is strictly the mechanical-rule signal the spec intends and needs no numeric subtraction. See `universal_scorer.py:588-604`.
- **Metric names.** `earned_top30` (primary), `n_scored_cands`, `n_synergy_buckets`. The spec listed `n_rules_firing`; true distinct-rule count is not exposed on `Recommendation` (only bucket-level), and recovering it needs a second `find_all_complements` pass that would double the ~1h census cost. `n_synergy_buckets` (distinct non-zero synergy-bucket kinds) is the faithful, single-pass proxy. Flagged for user review.
- **Config-hash stamping.** `baseline.json` is stamped with `mtg_synergy_graph.bench.tensor.compute_config_hash()`; `gate`/`queue` refuse a baseline whose stamp ≠ live hash (message: re-run `census`).
- **Commit style.** End commit messages with the two trailers used across this repo (`Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>` and the `Claude-Session:` line). The pre-commit pytest hook currently has a pre-existing parallel-xdist flake (Task 7 fixes it); until Task 7 lands, if the hook fails ONLY on `test_korvold_finds_phyrexian_altar`, re-run to confirm it's the flake before proceeding.

**Key real signatures this plan builds on (verified against the tree):**
- `SynergyEngine.page(commander: str | Sequence[str], offset=0, limit=...) -> RecommendationPage`; `.items: list[Recommendation]`.
- `Recommendation`: `rank: int`, `card: str`, `total_score: float`, `scores: dict[str, float]`, `contributing_ports: list[ContributingPort]` (`engine.py:78`).
- `mtg_synergy_graph.bench.forensics.load_card_meta(conn) -> dict[str, tuple[float, int]]` (name → (cmc, edhrec_rank)).
- `mtg_synergy_graph.bench.tensor.compute_config_hash() -> str`.
- Cohort predicate template: `subtype_death_payoff(conn) -> set[str]` in `bench/cohorts.py`.
- Standing-instrument script pattern: `scripts/demand_coverage.py` is a 15-line wrapper calling `mtg_synergy_graph.bench.demand_coverage.main`.
- Engine construction in tests: see `tests/bench/` for the populated-DB fixture pattern (e.g. `tests/bench/test_forensics*.py`).

---

### Task 1: Metric core — `bench/coverage.py`

**Files:**
- Create: `src/mtg_synergy_graph/bench/coverage.py`
- Test: `tests/bench/test_coverage_metric.py`

**Interfaces:**
- Consumes: `mtg_synergy_graph.engine.Recommendation` (has `.scores: dict[str, float]`).
- Produces:
  - `_NON_RULE_BUCKET_KEYS: frozenset[str]` = `{"staple", "embedding", "concentration_dampen", "total"}`
  - `is_earned(rec: Recommendation) -> bool`
  - `@dataclass(frozen=True) CoverageMetrics` with fields `earned_top30: int`, `n_scored_cands: int`, `n_synergy_buckets: int`
  - `compute_coverage(items: Sequence[Recommendation], *, top_n: int = 30) -> CoverageMetrics`

- [ ] **Step 1: Write the failing test**

```python
# tests/bench/test_coverage_metric.py
from dataclasses import dataclass, field

from mtg_synergy_graph.bench.coverage import (
    CoverageMetrics,
    compute_coverage,
    is_earned,
)


@dataclass(frozen=True)
class _Rec:
    """Minimal Recommendation stand-in: compute_coverage only reads .scores."""

    scores: dict[str, float] = field(default_factory=dict)


def _staple_only():
    # Pure staple + additive terms only (rank/cmc/circuit are NOT in scores).
    return _Rec(scores={"staple": 0.01, "total": 0.037})


def _earned(bucket="port_match", val=1.5):
    return _Rec(scores={bucket: val, "staple": 0.01, "total": 1.55})


def test_is_earned_false_for_staple_only():
    assert is_earned(_staple_only()) is False


def test_is_earned_true_for_synergy_bucket():
    assert is_earned(_earned()) is True


def test_is_earned_false_when_only_non_rule_keys_present():
    assert is_earned(_Rec(scores={"embedding": 0.4, "total": 0.4})) is False


def test_is_earned_true_for_negative_anti_synergy_bucket():
    # A firing anti-synergy rule still means "the engine said something".
    assert is_earned(_Rec(scores={"port_match": -0.5, "total": -0.5})) is True


def test_earned_top30_counts_only_top_n():
    items = [_earned() for _ in range(5)] + [_staple_only() for _ in range(40)]
    m = compute_coverage(items, top_n=30)
    assert m.earned_top30 == 5
    assert m.n_scored_cands == 5


def test_earned_top30_caps_at_top_n_window():
    # 35 earned; only the first 30 count toward earned_top30, all 35 toward pool.
    items = [_earned() for _ in range(35)]
    m = compute_coverage(items, top_n=30)
    assert m.earned_top30 == 30
    assert m.n_scored_cands == 35


def test_n_synergy_buckets_distinct_nonzero():
    items = [
        _earned(bucket="port_match"),
        _earned(bucket="cost_synergy"),
        _earned(bucket="port_match"),
        _staple_only(),
    ]
    m = compute_coverage(items, top_n=30)
    assert m.n_synergy_buckets == 2


def test_empty_page_all_zero():
    m = compute_coverage([], top_n=30)
    assert m == CoverageMetrics(earned_top30=0, n_scored_cands=0, n_synergy_buckets=0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/bench/test_coverage_metric.py -o addopts="" -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'mtg_synergy_graph.bench.coverage'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/mtg_synergy_graph/bench/coverage.py
"""Per-commander activation-poverty metric (coverage instrument).

Zero scoring-path impact. Turns an ``engine.page()`` result into three
coverage scalars. ``earned_top30`` is the headline: of the surfaced top-30,
how many earned real mechanical credit (>=1 non-zero synergy bucket) rather
than a flat staple bonus. See
``docs/superpowers/specs/2026-07-08-activation-poverty-instrument-design.md``.

"Earned" is defined by synergy-bucket PRESENCE, not by numeric subtraction:
``rank_bonus``/``cmc_bonus``/``circuit_bonus`` are folded into ``total`` but
never exposed as ``scores`` keys (only ``embedding`` is), so any ``scores``
key outside ``_NON_RULE_BUCKET_KEYS`` is a fired complement rule. See
``universal_scorer.py`` (``to_legacy_buckets``).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

#: Keys that appear in ``Recommendation.scores`` but are NOT complement-rule
#: contributions. Everything else is a synergy/anti-synergy bucket.
_NON_RULE_BUCKET_KEYS: frozenset[str] = frozenset(
    {"staple", "embedding", "concentration_dampen", "total"}
)


def _synergy_buckets(scores: dict[str, float]) -> dict[str, float]:
    return {
        k: v
        for k, v in scores.items()
        if k not in _NON_RULE_BUCKET_KEYS and v != 0.0
    }


def is_earned(rec) -> bool:
    """True iff at least one complement-rule bucket fired for this candidate."""
    return bool(_synergy_buckets(rec.scores))


@dataclass(frozen=True)
class CoverageMetrics:
    earned_top30: int
    n_scored_cands: int
    n_synergy_buckets: int


def compute_coverage(items: Sequence, *, top_n: int = 30) -> CoverageMetrics:
    """Coverage scalars for one commander's full ranking.

    ``items`` is an ``engine.page(limit=large).items`` list (rank-ordered).
    """
    earned_top30 = sum(1 for rec in items[:top_n] if is_earned(rec))
    n_scored = 0
    bucket_kinds: set[str] = set()
    for rec in items:
        buckets = _synergy_buckets(rec.scores)
        if buckets:
            n_scored += 1
            bucket_kinds.update(buckets)
    return CoverageMetrics(
        earned_top30=earned_top30,
        n_scored_cands=n_scored,
        n_synergy_buckets=len(bucket_kinds),
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/bench/test_coverage_metric.py -o addopts="" -q`
Expected: PASS (8 passed)

- [ ] **Step 5: Commit**

```bash
git add src/mtg_synergy_graph/bench/coverage.py tests/bench/test_coverage_metric.py
git commit -m "$(cat <<'EOF'
feat(bench): activation-poverty metric core (earned_top30)

Pure read-side metric turning an engine.page() result into
earned_top30 / n_scored_cands / n_synergy_buckets. "Earned" =
presence of a non-zero synergy bucket (rank/cmc/circuit are not in
scores, so no subtraction needed). Zero scoring-path impact.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01GbVUPULJeQAGdUBy4M75m3
EOF
)"
```

---

### Task 2: `toughness_payoff` cohort predicate

**Files:**
- Modify: `src/mtg_synergy_graph/bench/cohorts.py` (add one function, append nothing to any shared union tuple)
- Test: `tests/bench/test_toughness_cohort.py`

**Interfaces:**
- Consumes: `sqlite3.Connection` to a populated DB.
- Produces: `toughness_payoff(conn: sqlite3.Connection) -> set[str]`

**Design notes (verified counts against `data/synergy.db`):**
- Precise signal: a `scales_with` port whose `event_class = 'CardToughness'` OR whose `scaling_expression LIKE '%CardToughness%'` → Phenax, Tanazir Quandrix, A-Tanazir Quandrix, Arwen (Weaver of Hope), Vhal, Betor, Orysa, The Pride of Hull Clade (~8).
- Plus an explicit small set for toughness-as-combat-damage / defender-draw commanders whose toughness use is a different port shape: `_TOUGHNESS_COMBAT_COMMANDERS = frozenset({"Doran, the Siege Tower", "Arcades, the Strategist", "High Alert"})`. (`High Alert` is not a legendary creature; it is included only if it survives the legal-commander/legendary-creature join — it will NOT, and that is fine; the frozenset documents intent and the join filters it. See the exclusion test.)
- Must NOT match the ~300 cards that merely mention "Toughness" in a buff/P-T `raw_line` — the SQL keys on `event_class`/`scaling_expression`, never `raw_line LIKE '%Toughness%'`.
- Follow the `subtype_death_payoff` shape: filter `legal_commander = 1 AND supertypes LIKE '%Legendary%' AND card_types LIKE '%Creature%'`. Do NOT append to `_COHORT_PREDICATES` / any shared union (that would mutate the pinned archetype-payoff fixture's `cohort_members` snapshot — see the `outlet_direction_death_payoff` docstring warning). This predicate stands alone for the coverage instrument.

- [ ] **Step 1: Write the failing test**

```python
# tests/bench/test_toughness_cohort.py
import pytest

from mtg_synergy_graph.bench.cohorts import toughness_payoff

# Reuse whatever populated-DB fixture the other bench cohort tests use.
# If tests/bench/conftest.py exposes `synergy_db` / `populated_db`, depend on it.


def test_includes_known_toughness_payoff_commanders(synergy_db):
    cohort = toughness_payoff(synergy_db)
    assert "Phenax, God of Deception" in cohort
    assert "Tanazir Quandrix" in cohort
    assert "Arwen, Weaver of Hope" in cohort


def test_excludes_pure_pt_buff_noise(synergy_db):
    cohort = toughness_payoff(synergy_db)
    # A vanilla/buff card that references toughness only in a pump line.
    assert "Giant Growth" not in cohort
    # Non-creature enchantment must be filtered by the legendary-creature join.
    assert "High Alert" not in cohort


def test_all_members_are_legal_legendary_creatures(synergy_db):
    cohort = toughness_payoff(synergy_db)
    assert cohort  # non-empty
    placeholders = ",".join("?" * len(cohort))
    rows = synergy_db.execute(
        f"SELECT name FROM cards WHERE name IN ({placeholders}) "
        "AND legal_commander = 1 AND supertypes LIKE '%Legendary%' "
        "AND card_types LIKE '%Creature%'",
        tuple(cohort),
    ).fetchall()
    assert len(rows) == len(cohort)


def test_cohort_is_a_real_cohort_not_singleton(synergy_db):
    # Guards against a disguised whitelist: must be a genuine multi-member set.
    assert len(toughness_payoff(synergy_db)) >= 6
```

If `tests/bench/` has no shared `synergy_db` fixture, add one to `tests/bench/conftest.py` that opens the real `data/synergy.db` read-only and `pytest.skip`s when it is absent (mirror the skip-guard used by `tests/bench/test_forensics*.py`). Do NOT hardcode `db="data/synergy.db"` into engine-construction code paths; the fixture reads the file directly for the predicate, which only needs a connection.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/bench/test_toughness_cohort.py -o addopts="" -q`
Expected: FAIL — `ImportError: cannot import name 'toughness_payoff'`

- [ ] **Step 3: Write minimal implementation**

```python
# append to src/mtg_synergy_graph/bench/cohorts.py

#: Toughness-payoff commanders whose toughness use is a non-``CardToughness``
#: port shape (toughness-as-combat-damage / defender-draw). Documented intent;
#: the legal-legendary-creature join drops any non-qualifying entry (e.g. the
#: enchantment ``High Alert``).
_TOUGHNESS_COMBAT_COMMANDERS: frozenset[str] = frozenset(
    {"Doran, the Siege Tower", "Arcades, the Strategist", "High Alert"}
)


def toughness_payoff(conn: sqlite3.Connection) -> set[str]:
    """Legal legendary-creature commanders whose payoff scales off toughness.

    Precise signal: a ``scales_with`` port with ``event_class='CardToughness'``
    or ``scaling_expression LIKE '%CardToughness%'`` (Phenax, Tanazir, Arwen,
    Vhal, Betor, Orysa, The Pride of Hull Clade), UNION an explicit
    combat/defender set (:data:`_TOUGHNESS_COMBAT_COMMANDERS`). Keys on port
    shape, never ``raw_line LIKE '%Toughness%'`` (which matches ~300 buff/P-T
    noise cards). Names the follow-on toughness-payoff rule's target cohort for
    the coverage instrument; deliberately NOT part of any shared cohort union.
    """
    rows = conn.execute(
        "SELECT DISTINCT p.card_name "
        "FROM card_ports p "
        "JOIN cards c ON c.name = p.card_name "
        "WHERE (p.event_class = 'CardToughness' "
        "       OR p.scaling_expression LIKE '%CardToughness%') "
        "AND c.legal_commander = 1 "
        "AND c.supertypes LIKE '%Legendary%' "
        "AND c.card_types LIKE '%Creature%'"
    )
    cohort: set[str] = {name for (name,) in rows}

    if _TOUGHNESS_COMBAT_COMMANDERS:
        placeholders = ",".join("?" * len(_TOUGHNESS_COMBAT_COMMANDERS))
        extra = conn.execute(
            f"SELECT name FROM cards WHERE name IN ({placeholders}) "
            "AND legal_commander = 1 AND supertypes LIKE '%Legendary%' "
            "AND card_types LIKE '%Creature%'",
            tuple(sorted(_TOUGHNESS_COMBAT_COMMANDERS)),
        )
        cohort.update(name for (name,) in extra)
    return cohort
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/bench/test_toughness_cohort.py -o addopts="" -q`
Expected: PASS (4 passed). If `Arwen, Weaver of Hope` is absent in the current DB build, adjust the assertion to another confirmed member from the verified list (Tanazir Quandrix, The Pride of Hull Clade) — do not weaken the cohort-size guard.

- [ ] **Step 5: Commit**

```bash
git add src/mtg_synergy_graph/bench/cohorts.py tests/bench/test_toughness_cohort.py tests/bench/conftest.py
git commit -m "$(cat <<'EOF'
feat(bench): toughness_payoff cohort predicate

~12-commander cohort (scales_with CardToughness + explicit
Doran/Arcades combat set) keyed on port shape, not raw_line noise.
Stands alone for the coverage instrument; not appended to any shared
cohort union. Zero scoring-path impact.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01GbVUPULJeQAGdUBy4M75m3
EOF
)"
```

---

### Task 3: Census mode + `baseline.json` (config-hash stamped)

**Files:**
- Create: `src/mtg_synergy_graph/bench/coverage_report.py`
- Test: `tests/bench/test_coverage_report_census.py`

**Interfaces:**
- Consumes: `compute_coverage` (Task 1); `SynergyEngine`; `load_card_meta`; `compute_config_hash`.
- Produces:
  - `legal_commander_names(conn) -> list[str]` (sorted; legal legendary creatures)
  - `commander_coverage(engine, commander) -> CoverageMetrics` (one `page(limit=1_000_000)` pass, clears `engine._score_cache` after)
  - `run_census(engine, conn, *, commanders=None) -> dict[str, CoverageMetrics]`
  - `write_baseline(path, metrics_by_cmdr, *, config_hash) -> None` (JSON: `{"config_hash": ..., "generated_metric_version": 1, "commanders": {name: {earned_top30, n_scored_cands, n_synergy_buckets}}}`)
  - `read_baseline(path) -> tuple[str, dict[str, CoverageMetrics]]` (returns `(config_hash, metrics)`)

- [ ] **Step 1: Write the failing test**

```python
# tests/bench/test_coverage_report_census.py
import json

from mtg_synergy_graph.bench.coverage import CoverageMetrics
from mtg_synergy_graph.bench.coverage_report import (
    read_baseline,
    write_baseline,
)


def test_baseline_roundtrip(tmp_path):
    path = tmp_path / "baseline.json"
    metrics = {
        "Phenax, God of Deception": CoverageMetrics(0, 0, 0),
        "Korvold, Fae-Cursed King": CoverageMetrics(24, 130, 5),
    }
    write_baseline(path, metrics, config_hash="deadbeef")
    cfg, back = read_baseline(path)
    assert cfg == "deadbeef"
    assert back == metrics


def test_baseline_json_shape(tmp_path):
    path = tmp_path / "baseline.json"
    write_baseline(path, {"X": CoverageMetrics(1, 2, 3)}, config_hash="abc")
    doc = json.loads(path.read_text())
    assert doc["config_hash"] == "abc"
    assert doc["commanders"]["X"] == {
        "earned_top30": 1,
        "n_scored_cands": 2,
        "n_synergy_buckets": 3,
    }
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/bench/test_coverage_report_census.py -o addopts="" -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'mtg_synergy_graph.bench.coverage_report'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/mtg_synergy_graph/bench/coverage_report.py
"""Activation-poverty census / queue / gate (coverage instrument CLI core).

Zero scoring-path impact. ``census`` runs the whole legal commander universe
through ``engine.page()`` and pins a config-hash-stamped baseline; ``queue``
ranks commanders by ``earned_top30`` (the coverage successor to gap_report);
``gate`` measures a cohort's ``earned_top30`` lift vs baseline against a
stratified control sample. See
``docs/superpowers/specs/2026-07-08-activation-poverty-instrument-design.md``.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from mtg_synergy_graph.bench.coverage import CoverageMetrics, compute_coverage
from mtg_synergy_graph.engine import SynergyEngine

_METRIC_VERSION = 1


def legal_commander_names(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM cards "
        "WHERE legal_commander = 1 "
        "AND supertypes LIKE '%Legendary%' "
        "AND card_types LIKE '%Creature%' "
        "ORDER BY name"
    )
    return [name for (name,) in rows]


def commander_coverage(engine: SynergyEngine, commander: str) -> CoverageMetrics:
    page = engine.page([commander], offset=0, limit=1_000_000)
    metrics = compute_coverage(page.items, top_n=30)
    engine._score_cache.clear()  # bound memory across a ~2000-commander loop
    return metrics


def run_census(
    engine: SynergyEngine,
    conn: sqlite3.Connection,
    *,
    commanders: list[str] | None = None,
) -> dict[str, CoverageMetrics]:
    names = commanders if commanders is not None else legal_commander_names(conn)
    return {name: commander_coverage(engine, name) for name in names}


def write_baseline(
    path: str | Path,
    metrics_by_cmdr: dict[str, CoverageMetrics],
    *,
    config_hash: str,
) -> None:
    doc = {
        "config_hash": config_hash,
        "generated_metric_version": _METRIC_VERSION,
        "commanders": {
            name: {
                "earned_top30": m.earned_top30,
                "n_scored_cands": m.n_scored_cands,
                "n_synergy_buckets": m.n_synergy_buckets,
            }
            for name, m in sorted(metrics_by_cmdr.items())
        },
    }
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")


def read_baseline(
    path: str | Path,
) -> tuple[str, dict[str, CoverageMetrics]]:
    doc = json.loads(Path(path).read_text())
    metrics = {
        name: CoverageMetrics(
            earned_top30=v["earned_top30"],
            n_scored_cands=v["n_scored_cands"],
            n_synergy_buckets=v["n_synergy_buckets"],
        )
        for name, v in doc["commanders"].items()
    }
    return doc["config_hash"], metrics
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/bench/test_coverage_report_census.py -o addopts="" -q`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add src/mtg_synergy_graph/bench/coverage_report.py tests/bench/test_coverage_report_census.py
git commit -m "$(cat <<'EOF'
feat(bench): coverage census + config-hash-stamped baseline

run_census() runs the legal commander universe through engine.page()
and pins .audit/coverage/baseline.json (stamped with compute_config_hash).
read/write_baseline round-trip. Zero scoring-path impact.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01GbVUPULJeQAGdUBy4M75m3
EOF
)"
```

---

### Task 4: Queue ranking + stratified control sampler

**Files:**
- Modify: `src/mtg_synergy_graph/bench/coverage_report.py` (add `poverty_queue`, `stratified_control`)
- Test: `tests/bench/test_coverage_queue.py`

**Interfaces:**
- Consumes: `read_baseline` output (`dict[str, CoverageMetrics]`).
- Produces:
  - `poverty_queue(metrics: dict[str, CoverageMetrics]) -> list[tuple[str, int]]` — `(name, earned_top30)` ascending by `earned_top30`, then by name for determinism.
  - `stratified_control(metrics, *, exclude: set[str], size: int = 200, seed: int = 17) -> list[str]` — deterministic sample spanning the `earned_top30` distribution (decile strata) of `metrics` minus `exclude`.

- [ ] **Step 1: Write the failing test**

```python
# tests/bench/test_coverage_queue.py
from mtg_synergy_graph.bench.coverage import CoverageMetrics
from mtg_synergy_graph.bench.coverage_report import (
    poverty_queue,
    stratified_control,
)


def _metrics(n):
    return {f"C{i:03d}": CoverageMetrics(i % 31, i, i % 8) for i in range(n)}


def test_poverty_queue_ascending_by_earned():
    m = {
        "A": CoverageMetrics(5, 10, 2),
        "B": CoverageMetrics(0, 0, 0),
        "C": CoverageMetrics(5, 3, 1),
    }
    q = poverty_queue(m)
    assert q[0] == ("B", 0)
    # Tie at earned=5 broken by name.
    assert q[1] == ("A", 5)
    assert q[2] == ("C", 5)


def test_stratified_control_deterministic_and_excludes_cohort():
    m = _metrics(500)
    cohort = {"C000", "C001", "C002"}
    a = stratified_control(m, exclude=cohort, size=200, seed=17)
    b = stratified_control(m, exclude=cohort, size=200, seed=17)
    assert a == b  # deterministic
    assert len(a) == 200
    assert not (set(a) & cohort)  # excludes cohort


def test_stratified_control_caps_at_available():
    m = _metrics(50)
    ctrl = stratified_control(m, exclude=set(), size=200, seed=17)
    assert len(ctrl) == 50  # cannot exceed the pool
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/bench/test_coverage_queue.py -o addopts="" -q`
Expected: FAIL — `ImportError: cannot import name 'poverty_queue'`

- [ ] **Step 3: Write minimal implementation**

```python
# append to src/mtg_synergy_graph/bench/coverage_report.py
import random


def poverty_queue(
    metrics: dict[str, CoverageMetrics],
) -> list[tuple[str, int]]:
    return sorted(
        ((name, m.earned_top30) for name, m in metrics.items()),
        key=lambda t: (t[1], t[0]),
    )


def stratified_control(
    metrics: dict[str, CoverageMetrics],
    *,
    exclude: set[str],
    size: int = 200,
    seed: int = 17,
) -> list[str]:
    """Deterministic sample spanning the earned_top30 distribution.

    Buckets the eligible pool into 10 strata by earned_top30, then draws
    proportionally (seeded) so the control spans poverty-poor to
    poverty-rich commanders — a regression anywhere is visible.
    """
    pool = sorted(name for name in metrics if name not in exclude)
    if len(pool) <= size:
        return pool

    rng = random.Random(seed)
    strata: dict[int, list[str]] = {}
    for name in pool:
        band = min(metrics[name].earned_top30 // 3, 9)  # 0..9 (earned 0..30)
        strata.setdefault(band, []).append(name)

    picked: list[str] = []
    per = max(1, size // max(1, len(strata)))
    for band in sorted(strata):
        members = sorted(strata[band])
        rng.shuffle(members)
        picked.extend(members[:per])

    # Top up / trim deterministically to exactly `size`.
    if len(picked) < size:
        remaining = sorted(set(pool) - set(picked))
        rng.shuffle(remaining)
        picked.extend(remaining[: size - len(picked)])
    return sorted(picked[:size])
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/bench/test_coverage_queue.py -o addopts="" -q`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add src/mtg_synergy_graph/bench/coverage_report.py tests/bench/test_coverage_queue.py
git commit -m "$(cat <<'EOF'
feat(bench): poverty queue + stratified control sampler

poverty_queue ranks commanders ascending by earned_top30 (coverage
successor to gap_report); stratified_control draws a deterministic,
distribution-spanning 200-commander regression sample. Zero
scoring-path impact.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01GbVUPULJeQAGdUBy4M75m3
EOF
)"
```

---

### Task 5: Gate mode (cohort Δ vs baseline + control Δ distribution)

**Files:**
- Modify: `src/mtg_synergy_graph/bench/coverage_report.py` (add `run_gate`, `GateResult`)
- Test: `tests/bench/test_coverage_gate.py`

**Interfaces:**
- Consumes: `read_baseline`, `run_census` (reused to score a name subset live), `stratified_control`.
- Produces:
  - `@dataclass(frozen=True) GateResult` with `cohort_delta_mean: float`, `cohort_deltas: dict[str, int]`, `control_delta_mean: float`, `control_deltas: dict[str, int]`, `stale_baseline: bool`.
  - `run_gate(engine, conn, baseline_path, cohort_names, *, live_config_hash, control_size=200, seed=17) -> GateResult` — reads baseline; if its stamp ≠ `live_config_hash`, returns `GateResult(..., stale_baseline=True)` without scoring; else live-scores cohort + control and computes per-commander `earned_top30` deltas vs baseline.

- [ ] **Step 1: Write the failing test**

```python
# tests/bench/test_coverage_gate.py
from mtg_synergy_graph.bench.coverage import CoverageMetrics
from mtg_synergy_graph.bench.coverage_report import (
    GateResult,
    _compute_deltas,  # pure helper, unit-tested without an engine
    write_baseline,
)


def test_compute_deltas_vs_baseline():
    baseline = {
        "A": CoverageMetrics(0, 0, 0),
        "B": CoverageMetrics(10, 50, 3),
    }
    live = {
        "A": CoverageMetrics(7, 40, 2),  # +7
        "B": CoverageMetrics(9, 48, 3),  # -1 regression
    }
    deltas, mean = _compute_deltas(live, baseline)
    assert deltas == {"A": 7, "B": -1}
    assert mean == 3.0


def test_gate_flags_stale_baseline(tmp_path):
    path = tmp_path / "baseline.json"
    write_baseline(path, {"A": CoverageMetrics(0, 0, 0)}, config_hash="OLD")
    # run_gate is called with a mismatching live hash -> stale, no scoring.
    from mtg_synergy_graph.bench.coverage_report import run_gate

    res = run_gate(
        engine=None,
        conn=None,
        baseline_path=path,
        cohort_names=["A"],
        live_config_hash="NEW",
    )
    assert res.stale_baseline is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/bench/test_coverage_gate.py -o addopts="" -q`
Expected: FAIL — `ImportError: cannot import name 'GateResult'`

- [ ] **Step 3: Write minimal implementation**

```python
# append to src/mtg_synergy_graph/bench/coverage_report.py
from dataclasses import dataclass, field


def _compute_deltas(
    live: dict[str, CoverageMetrics],
    baseline: dict[str, CoverageMetrics],
) -> tuple[dict[str, int], float]:
    deltas = {
        name: m.earned_top30 - baseline[name].earned_top30
        for name, m in live.items()
        if name in baseline
    }
    mean = sum(deltas.values()) / len(deltas) if deltas else 0.0
    return deltas, mean


@dataclass(frozen=True)
class GateResult:
    cohort_delta_mean: float = 0.0
    cohort_deltas: dict[str, int] = field(default_factory=dict)
    control_delta_mean: float = 0.0
    control_deltas: dict[str, int] = field(default_factory=dict)
    stale_baseline: bool = False


def run_gate(
    engine,
    conn,
    baseline_path,
    cohort_names,
    *,
    live_config_hash: str,
    control_size: int = 200,
    seed: int = 17,
) -> GateResult:
    baseline_hash, baseline = read_baseline(baseline_path)
    if baseline_hash != live_config_hash:
        return GateResult(stale_baseline=True)

    cohort_set = set(cohort_names)
    control = stratified_control(
        baseline, exclude=cohort_set, size=control_size, seed=seed
    )
    live_cohort = run_census(engine, conn, commanders=sorted(cohort_set))
    live_control = run_census(engine, conn, commanders=control)

    cohort_deltas, cohort_mean = _compute_deltas(live_cohort, baseline)
    control_deltas, control_mean = _compute_deltas(live_control, baseline)
    return GateResult(
        cohort_delta_mean=cohort_mean,
        cohort_deltas=cohort_deltas,
        control_delta_mean=control_mean,
        control_deltas=control_deltas,
        stale_baseline=False,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/bench/test_coverage_gate.py -o addopts="" -q`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add src/mtg_synergy_graph/bench/coverage_report.py tests/bench/test_coverage_gate.py
git commit -m "$(cat <<'EOF'
feat(bench): coverage gate (cohort delta + control distribution)

run_gate live-scores a cohort + stratified control and reports
per-commander earned_top30 deltas vs the pinned baseline; refuses a
stale (config-hash-mismatched) baseline without scoring. Zero
scoring-path impact.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01GbVUPULJeQAGdUBy4M75m3
EOF
)"
```

---

### Task 6: CLI wiring + script + `--expect-identity` guard

**Files:**
- Modify: `src/mtg_synergy_graph/bench/coverage_report.py` (add `main(argv=None) -> int` with argparse: `census`/`queue`/`gate`)
- Create: `scripts/coverage_report.py` (thin wrapper, mirrors `scripts/demand_coverage.py`)
- Test: `tests/bench/test_coverage_cli.py`
- Test: `tests/bench/test_coverage_zero_scoring_path.py`

**Interfaces:**
- Consumes: everything above; builds a `SynergyEngine` and opens the DB via the project's standard `open_db(..., create=False)` path (copy the engine/DB-open pattern from `bench/demand_coverage.py` or `bench/forensics.py` — do NOT invent a new DB-open).
- Produces: `main(argv: list[str] | None = None) -> int`. Output dir default `.audit/coverage/`, baseline default `.audit/coverage/baseline.json`. `gate` prints cohort Δ mean, control Δ mean, and the full control Δ distribution (min / count of negatives / each negative name).

- [ ] **Step 1: Write the failing test**

```python
# tests/bench/test_coverage_cli.py
from mtg_synergy_graph.bench.coverage_report import main


def test_queue_reads_baseline_and_prints(tmp_path, capsys):
    from mtg_synergy_graph.bench.coverage import CoverageMetrics
    from mtg_synergy_graph.bench.coverage_report import write_baseline

    bp = tmp_path / "baseline.json"
    write_baseline(
        bp,
        {"Zzz": CoverageMetrics(20, 100, 5), "Aaa": CoverageMetrics(0, 0, 0)},
        config_hash="abc",
    )
    rc = main(["queue", "--baseline", str(bp), "--top", "2"])
    out = capsys.readouterr().out
    assert rc == 0
    # Poorest first.
    assert out.index("Aaa") < out.index("Zzz")


def test_gate_reports_stale_baseline(tmp_path, capsys):
    from mtg_synergy_graph.bench.coverage import CoverageMetrics
    from mtg_synergy_graph.bench.coverage_report import write_baseline

    bp = tmp_path / "baseline.json"
    write_baseline(bp, {"A": CoverageMetrics(0, 0, 0)}, config_hash="OLD")
    # Force a mismatching live hash via the documented override flag.
    rc = main(["gate", "--baseline", str(bp), "--cohort", "toughness_payoff",
               "--force-config-hash", "NEW"])
    out = capsys.readouterr().out
    assert "stale" in out.lower()
    assert rc != 0  # stale baseline is a non-zero exit
```

`census`/live `gate` need a real DB and are covered by an integration-style test that `pytest.skip`s when `data/synergy.db` is absent — add `test_census_smoke` guarded that way (score just `["Phenax, God of Deception"]` via `--commander` to keep it fast), asserting Phenax's `earned_top30 == 0`.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/bench/test_coverage_cli.py -o addopts="" -q`
Expected: FAIL — `TypeError`/`SystemExit` (argparse subcommands not implemented).

- [ ] **Step 3: Write minimal implementation**

Implement `main(argv)` with an argparse subparser for `census` (flags: `--out`, `--commander` repeatable for a subset run), `queue` (`--baseline`, `--top`, `--format {text,csv}`), and `gate` (`--baseline`, `--cohort {toughness_payoff}`, `--control-size`, `--seed`, hidden `--force-config-hash` for tests). `census` and live `gate` build the engine via the same `open_db(..., create=False)` + `SynergyEngine(...)` pattern used in `bench/forensics.py`; `queue` and stale-`gate` touch no engine. Resolve the cohort name through a small dispatch: `{"toughness_payoff": toughness_payoff}` imported from `bench.cohorts`. `gate` returns `1` when `stale_baseline`. Write the thin `scripts/coverage_report.py`:

```python
#!/usr/bin/env python3
"""Activation-poverty instrument CLI (coverage census / queue / gate).

Thin wrapper around :mod:`mtg_synergy_graph.bench.coverage_report`. See that
module and
``docs/superpowers/specs/2026-07-08-activation-poverty-instrument-design.md``.
"""

from __future__ import annotations

import sys

from mtg_synergy_graph.bench.coverage_report import main

if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Write the zero-scoring-path guard test**

```python
# tests/bench/test_coverage_zero_scoring_path.py
"""The coverage instrument must not perturb scoring: config hash is stable."""

from mtg_synergy_graph.bench.tensor import compute_config_hash


def test_config_hash_unchanged_by_import():
    # Importing the instrument must not mutate any scoring-config module state.
    before = compute_config_hash()
    import mtg_synergy_graph.bench.coverage  # noqa: F401
    import mtg_synergy_graph.bench.coverage_report  # noqa: F401

    assert compute_config_hash() == before
```

- [ ] **Step 5: Run tests + the real identity gate**

Run: `uv run pytest tests/bench/test_coverage_cli.py tests/bench/test_coverage_zero_scoring_path.py -o addopts="" -q`
Expected: PASS.
Then run the authoritative scoring-invariance gate:
Run: `uv run scripts/bench.py audit --expect-identity`
Expected: `✓ identical` / verdict `TRIVIAL` (or the repo's identity-pass message). If it reports any non-identity, STOP — the instrument touched the scoring path and the task is not done.

- [ ] **Step 6: Commit**

```bash
git add src/mtg_synergy_graph/bench/coverage_report.py scripts/coverage_report.py tests/bench/test_coverage_cli.py tests/bench/test_coverage_zero_scoring_path.py
git commit -m "$(cat <<'EOF'
feat(bench): coverage_report CLI (census/queue/gate) + identity guard

argparse CLI + scripts/coverage_report.py wrapper (mirrors
demand_coverage). gate exits non-zero on a stale baseline. Adds a
config-hash-stability test; bench.py audit --expect-identity stays
bitwise-identical. Zero scoring-path impact.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01GbVUPULJeQAGdUBy4M75m3
EOF
)"
```

---

### Task 7: Fix the pre-existing xdist-ordering flake

**Files:**
- Modify: `tests/test_complement_rules.py` (the `populated_db` fixture and/or `test_korvold_finds_phyrexian_altar`)
- Investigate: `src/mtg_synergy_graph/penalties.py:782` (`_bulk_load_candidate_attr_rows` leaks an unclosed sqlite connection — the `ResourceWarning` seen in the failing run).

**Context:** `test_korvold_finds_phyrexian_altar` passes in isolation (`uv run pytest "tests/test_complement_rules.py::TestFindAllComplements::test_korvold_finds_phyrexian_altar" -o addopts="" -q` → 1 passed) but fails under the parallel pre-commit run, returning `find_all_complements(...) == set()`. The co-occurring `ResourceWarning: unclosed database in penalties.py:782` points at cross-test DB-connection contamination (CLAUDE.md documents this DB-poisoning class). Use systematic-debugging: reproduce under xdist first, find the shared state, fix the leak — do NOT just mark the test `xfail`.

- [ ] **Step 1: Reproduce under parallel execution**

Run: `uv run pytest tests/test_complement_rules.py -n auto -o addopts="-n auto" -q` (or the repo's default parallel invocation)
Expected: intermittently FAIL on `test_korvold_finds_phyrexian_altar` with `assert 'Phyrexian Altar' in set()`. If it does not reproduce, run the full `uv run pytest tests/` as the hook does. Record the reproduction command.

- [ ] **Step 2: Identify the shared state (write the diagnosis as a failing assertion)**

Inspect `penalties.py:_bulk_load_candidate_attr_rows` (line ~782): confirm it opens a sqlite connection it never closes. Determine whether that connection (or a module-level cache it populates) is keyed by a path that collides across xdist workers or leaks a half-populated cache when a sibling test constructs an engine on a different DB. Add a focused test that fails deterministically by exercising the leak — e.g. constructing two engines on different DBs in one process and asserting `find_all_complements` for Korvold on the second is non-empty.

- [ ] **Step 3: Fix the leak / contamination**

Close the connection (context-manager or explicit `.close()`) in `_bulk_load_candidate_attr_rows`, and/or key any module-level cache it feeds by the DB path so a second engine cannot read a sibling's rows. Minimal change; do not refactor unrelated code.

- [ ] **Step 4: Verify the fix**

Run: `uv run pytest tests/test_complement_rules.py::TestFindAllComplements::test_korvold_finds_phyrexian_altar -o addopts="" -q` → PASS.
Run the parallel reproduction from Step 1 five times → PASS every time.
Run: `uv run pytest tests/ ` (full suite, the hook's invocation) → 0 failed, no `ResourceWarning: unclosed database` from `penalties.py:782`.

- [ ] **Step 5: Commit**

```bash
git add tests/test_complement_rules.py src/mtg_synergy_graph/penalties.py
git commit -m "$(cat <<'EOF'
fix(tests): close leaked sqlite conn in _bulk_load_candidate_attr_rows

test_korvold_finds_phyrexian_altar flaked under parallel xdist
(find_all_complements returned empty) due to an unclosed connection in
penalties.py:_bulk_load_candidate_attr_rows contaminating sibling
tests. Close the connection; passes in isolation and under repeated
parallel runs.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01GbVUPULJeQAGdUBy4M75m3
EOF
)"
```

---

## After all tasks: run the census + read the queue

Not a code task — the first operational use, run once the instrument is green:

```bash
uv run scripts/coverage_report.py census                    # ~1h, pins .audit/coverage/baseline.json
uv run scripts/coverage_report.py queue --top 40            # the poverty ranking; Phenax should sit near the bottom
```

Confirm Phenax's `earned_top30 == 0` in the queue (validates the metric against the known-poor case). This baseline + the `toughness_payoff` cohort become the gate for the **follow-on toughness-payoff rule spec**, which pre-registers its `earned_top30`-lift bar against the observed distribution.

## Self-Review

- **Spec coverage:** metric core (Task 1 ✓), `toughness_payoff` cohort (Task 2 ✓), census + baseline (Task 3 ✓), queue (Task 4 ✓), gate + control (Tasks 4–5 ✓), CLI/script + zero-scoring-path guard (Task 6 ✓), flake fix (Task 7 ✓). Success criteria 1–4 from the spec are exercised by Task 6's smoke test + the post-task census run.
- **Deviations flagged for user:** (a) primary "earned" defined by synergy-bucket presence rather than numeric staple/rank_bonus subtraction — equivalent-or-stricter given the real `scores` model; (b) secondary metric named `n_synergy_buckets` (single-pass proxy) instead of the spec's `n_rules_firing` (would need a cost-doubling second pass). Both in Global Constraints.
- **Type consistency:** `CoverageMetrics(earned_top30, n_scored_cands, n_synergy_buckets)` used identically in Tasks 1, 3, 4, 5. `read_baseline` returns `(config_hash, metrics)` and is consumed that way in Task 5. `GateResult.stale_baseline` set in Task 5, asserted in Tasks 5–6.
- **Placeholder scan:** no TBD/TODO; every code step shows complete code; the one investigation step (Task 7 Step 2) is a debugging action, not a code placeholder.
