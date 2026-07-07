---
title: "feat: Structural gap remediation — deck-context second pass + magnitude quality prior"
type: feat
status: declined
date: 2026-07-06
origin: forensics readout 2026-07-06 (.audit/forensics.md, config_hash 34a9d110…) + architecture review in-session
---

# feat: Structural Gap Remediation Implementation Plan

> **DECISION 2026-07-06: Phase A (deck-context second pass) DECLINED at the
> Stage-1 kill test (Task 5).** Every cell of the 3×3 grid failed every gate.
> Cohort fixture (n=33): best cell mean ΔNDCG −0.0019 vs G1 bar +0.0567; all
> cells negative; reach 0. Golden-100: mean Δ −0.0230..−0.0478, 19–35 cliffs
> (<−0.05) per 100 commanders vs G5 bar of zero, reach 0–5 vs G3 floor 100;
> traps cliff (Kess −0.31, Edgar −0.18 at k=10/w=0.5). G4: the disguised
> whitelist beats the mechanism by a wide margin (decision-time readout
> +0.0531 @1 cliff / +0.0697 @6 cliffs vs mechanism best −0.0019; PR #101
> review later found the comparator's body query hit the wrong column —
> corrected full-whitelist numbers +0.0147/+0.0376/+0.0523, still strictly
> dominant, DECLINE unaffected; see the null-result doc's CORRECTION) — the
> mechanism is strictly dominated by the predicate it needed to beat. Instrument internally
> validated: the whitelist's positive deltas flow through the SAME assembly
> path, so the mechanism's negatives are real, and the w=0 self-check passed
> on all 633 sims. Root cause (scale diagnostic, Slimefoot): the mean-of-IDF-
> sums context term is flood-shaped — ~22k candidates receive it, generic
> breadth accumulates the largest terms (top ctx 0.417 > #30 base total
> 0.275), while zero-score labels max at 0.08, unreachable at any safe w.
> Tasks 6–8 skipped per pinned routing; zero scoring-path changes; pins
> untouched. Phase C proceeds independently. Evidence:
> docs/solutions/best-practices/deck-context-null-result-2026-07-06.md

> **DECISION 2026-07-06: Phase C (magnitude quality prior) DECLINED at the
> golden-100 screen (Task 11).** All 9 cells (q × r0) negative: mean ΔNDCG
> −0.0228..−0.0430 with 17–35 cliffs (<−0.05) per 100 commanders; traps cliff
> (Kess −0.145..−0.42, Edgar −0.10..−0.15 across cells). The Phase C gate
> (golden-500 Δ ≥ +0.0136 AND zero cliffs) is unreachable — no cell advanced
> to Stage 2. Gates were pinned first (H_500q=0.0136, G_500q=0.0355; q=0
> band cross-validates both sibling instruments). Task 11 integration branch
> skipped; zero scoring-path changes; pins untouched. All three OUTRANKED
> lever classes are now measured: reweighting (plan 002/003), re-ranking
> (plan 004), and a new-information quality prior (this cycle). Evidence:
> docs/solutions/best-practices/quality-rate-prior-null-result-2026-07-06.md


> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Attack the two structural failure modes the forensics taxonomy proves rule-authoring cannot reach — NO_RULES (43.0% of misses: archetype synergy invisible to commander-pairwise matching) via a deck-context second scoring pass, and OUTRANKED (45.6%: no card-quality signal) via a magnitude-derived rate prior — each behind a committed kill-test instrument with pinned gates before any scoring-path change.

**Architecture:** Two independent kill-test-first cycles in one program. Phase A builds `context_sim`, a portfolio_sim-style offline instrument that adds a second scoring pass: the commander's top-K pass-1 candidates become a *context pool*, and every legal card earns an IDF-weighted context contribution from its mechanical matches against that pool — this is how "Slimefoot → all Saprolings" becomes scoreable without a commander-pairwise rule. Phase B (conditional on Phase A's pinned routing) integrates the winner flag-gated into `engine.page()`. Phase C runs a cheap, honest kill test on a deterministic effect-per-mana quality multiplier built from the already-populated `card_ports.amount` column, targeting OUTRANKED with an explicit expected-loss framing (two prior levers on this bucket were DECLINED).

**Tech Stack:** Python 3.12, sqlite3, existing bench machinery (`portfolio_sim.bootstrap_band`, `build_commander_sim`, `load_edhrec_labels`, `compute_ndcg`), pytest, uv.

## Global Constraints

- **Zero scoring-path changes until a kill-test PASS.** Phases A and C are read-side instruments; `bench.py audit --expect-identity` must PASS after every task in those phases.
- **Every score-affecting flag/knob registers in `ScoringConfigInputs`** (`universal_scorer.get_scoring_config_inputs`) so `compute_config_hash` catches drift. Flags default OFF, bitwise-identical off (canonical shape: `tests/test_universal_scorer_concave_agg.py`).
- **Gates pinned BEFORE sweeps.** Noise bands come from THIS instrument's own w=0 distribution (bands are instrument-specific — the `--per-commander-ndcg` band 0.1436±0.0448 and the `portfolio_sim bands` band 0.2858±0.0567 are different instruments and MUST NOT be borrowed).
- **Cohort gain is necessary-but-not-sufficient** (plan 2026-07-03-001): any SHIP must also clear whole-fixture no-regression AND beat the whitelist-equivalence baseline.
- **Tests never touch `data/synergy.db` paths for writes**; tmp_path only. Live-DB integration tests use the existing skip-guard pattern (`pytest.mark.skipif(not Path("data/synergy.db").exists(), ...)`).
- **Partner commander pairs are EXEMPT** from both mechanisms this cycle (flag-OFF path), mirroring plan 2026-07-02-004.
- All commands via `uv run`. Commit per task; pre-commit bench hook runs advisorily on scoring-path edits.

## Problem Frame

From `.audit/forensics.md` (100-cmdr golden set, NDCG@30 0.2336, 2,646 misses):

| bucket | share | status |
|--------|------:|--------|
| OUTRANKED | 45.6% | OPEN — lift-normalization DECLINED, portfolio-selection DECLINED at R0 |
| NO_RULES | 43.0% | OPEN — resource-flow demand DECLINED at Stage 0; re-framed as an **archetype-payoff-detection gap** (unreachable cards are on-theme archetype synergies: Slimefoot→Saprolings, Yawgmoth→undying/aristocrats, Araumi→reanimation, Gitrog→lands) |
| NEAR_MISS | 7.3% | serviced by the existing gap_report → scaffold loop |
| DATA_GAP | 4.2% | deferred (`--unknowns` sweep) |

**Dead axes (do not re-propose):** pointwise transforms of existing contributions (lift normalization); list-level per-family diminishing returns (portfolio selection — "most addressable misses sit far below the marginal flood member"); cost→supply resource-flow pairing (share 0.083 vs 0.25 bar).

**Why deck-context is not a dead axis:** all three DECLINEs *re-weighted or re-ranked existing pairwise signal*. The context pass creates **new matches** (candidate ↔ pool) and reaches candidates with zero pass-1 score — the exact population the 005 decline proved unreachable by supply pairing. The archetype-payoff cohort fixture (plan 2026-07-03-001, this branch) exists precisely to adjudicate this mechanism undiluted.

**Why the quality prior is not a dead axis (but is a long shot):** it injects *new information* (`card_ports.amount` magnitudes — 14,402 populated effect/static rows — plus repeatability shape and cmc), not a transform of existing contributions. Expected-loss framing: cheap instrument reusing `build_commander_sim`, hard exit at the pinned gate.

## Decision Gates (pinned routing — record verbatim in reports)

**Phase A → Phase B (deck-context SHIP-candidate)** — ∃ sweep cell such that ALL of:
- **G1 (target):** cohort-fixture mean NDCG@30 delta ≥ `H_cohort` (this instrument's bootstrap half-width, Task 3 bands run, seed 17).
- **G2 (no-regression):** golden-500 aggregate NDCG delta ≥ `−H_500`; zero per-commander cliffs < −0.05; gem-rate delta ≥ `−G_500` (pinned gem band, Task 3).
- **G3 (reach):** ≥ 100 zero-pass-1-score EDHREC labels enter top-30 across the golden-100 fixture (comparable to the 1,137 NO_RULES universe; same floor as plan 005).
- **G4 (whitelist-equivalence):** the cell's cohort mean delta strictly exceeds the BEST whitelist-baseline cohort delta (Task 4). If it can't beat the whitelist, it IS the whitelist → DECLINE.
- **G5 (traps):** trap-commander sidecar (Kess, Dissident Mage / Edgar Markov / Magda, Brazen Outlaw / Talrand, Sky Summoner) shows no per-commander delta < −0.05 in the cell.

Any gate fails for every cell → **DECLINE**: write the null-result doc (Task 5 step 6), skip Tasks 6–8, ship the instrument as standing infra.

**Phase C routing (quality prior)** — SHIP-candidate iff ∃ cell: golden-500 aggregate NDCG delta ≥ `+H_500q` (must IMPROVE above noise — OUTRANKED recovery is EDHREC-measured by definition) AND zero cliffs < −0.05 AND gem delta ≥ `−G_500q` AND trap sidecar clean. Else DECLINE + null-result doc, skip Task 11.

## File Structure

```
src/mtg_synergy_graph/bench/context_sim.py    # Phase A instrument core (new)
scripts/context_sim.py                        # Phase A CLI wrapper (new)
tests/bench/test_context_sim.py               # Phase A pure-function + smoke tests (new)
src/mtg_synergy_graph/deck_context.py         # Phase B flag + shared assembly math (new, Task 6)
tests/test_deck_context_flag.py               # Phase B flag-gate identity tests (new)
src/mtg_synergy_graph/quality.py              # Phase C rate signal (new)
src/mtg_synergy_graph/bench/quality_sim.py    # Phase C instrument (new)
scripts/quality_sim.py                        # Phase C CLI wrapper (new)
tests/test_quality.py                         # Phase C tests (new)
src/mtg_synergy_graph/universal_scorer.py     # Phase B/C: ScoringConfigInputs fields (modify)
src/mtg_synergy_graph/engine.py               # Phase B: page()/score_one integration (modify)
.audit/context_sim/                           # Phase A reports (gitignored)
.audit/quality_sim/                           # Phase C reports (gitignored)
```

---

## Phase A — Deck-context kill-test instrument (zero scoring-path changes)

### Task 1: `context_sim` core — context selection + per-context-card scoring

**Files:**
- Create: `src/mtg_synergy_graph/bench/context_sim.py`
- Test: `tests/bench/test_context_sim.py`

**Interfaces:**
- Consumes: `PortComplement`, `find_all_complements` from `mtg_synergy_graph.complement_rules`; `_compute_idf_weights` from `mtg_synergy_graph.universal_scorer`.
- Produces: `select_context(pool_order, n_rules_map, k) -> tuple[str, ...]`; `aggregate_context_scores(comps, idf, ctx_card) -> dict[str, float]`; `context_scores_for_card(conn, ctx_card, *, candidate_cache=None) -> dict[str, float]` with module-level cache `_CTX_SCORE_CACHE`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/bench/test_context_sim.py
"""Pure-function tests for the deck-context kill-test instrument (plan 2026-07-06-001)."""

from mtg_synergy_graph.bench.context_sim import (
    aggregate_context_scores,
    select_context,
)
from mtg_synergy_graph.complement_rules import PortComplement


def _comp(cand, rule="trigger_effect", direction="synergy", cmdr_ev="Sacrificed", cand_ev="Token"):
    return PortComplement(
        rule_id=rule, direction=direction, candidate=cand,
        cmdr_event=cmdr_ev, cand_event=cand_ev, filter_group="",
    )


def test_select_context_skips_zero_rule_candidates_and_caps_at_k():
    pool = ("A", "B", "C", "D")
    n_rules = {"A": 2, "B": 0, "C": 1, "D": 3}
    assert select_context(pool, n_rules, k=2) == ("A", "C")


def test_select_context_short_pool_returns_all_eligible():
    assert select_context(("A",), {"A": 1}, k=5) == ("A",)


def test_aggregate_dedups_on_idf_key_and_sums_weights():
    comps = [_comp("X"), _comp("X"), _comp("X", cand_ev="Treasure")]
    idf = {
        ("trigger_effect", "Sacrificed", "Token", ""): 0.5,
        ("trigger_effect", "Sacrificed", "Treasure", ""): 0.25,
    }
    out = aggregate_context_scores(comps, idf, ctx_card="CTX")
    assert out == {"X": 0.75}  # duplicate key counted once


def test_aggregate_excludes_anti_synergy_and_self():
    comps = [_comp("X", direction="anti_synergy"), _comp("CTX")]
    out = aggregate_context_scores(comps, {}, ctx_card="CTX")
    assert out == {}
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/bench/test_context_sim.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mtg_synergy_graph.bench.context_sim'`

- [ ] **Step 3: Implement the module**

```python
# src/mtg_synergy_graph/bench/context_sim.py
"""Deck-context two-pass kill-test instrument (plan 2026-07-06-001, Phase A).

Zero scoring-path impact. Pass 1 is the production ranking; the top-K
rule-covered candidates become the *context pool*; pass 2 awards every
legal card an IDF-weighted synergy contribution from its mechanical
matches against each context card (same complement rules, same IDF
form, context card standing in the commander slot). Candidates with
zero pass-1 score become reachable — the NO_RULES archetype-payoff
population (see plan 2026-07-02-005 DECLINE Correction).
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from collections.abc import Mapping, Sequence

from ..complement_rules import PortComplement, find_all_complements
from ..universal_scorer import _compute_idf_weights


def select_context(
    pool_order: Sequence[str],
    n_rules_map: Mapping[str, int],
    k: int,
) -> tuple[str, ...]:
    """Top-``k`` pass-1 candidates with >=1 distinct rule (pool order).

    Staple-only / rule-free candidates are excluded: they carry no
    mechanical signature for the context pass to match against.
    """
    out: list[str] = []
    for name in pool_order:
        if n_rules_map.get(name, 0) >= 1:
            out.append(name)
            if len(out) == k:
                break
    return tuple(out)


def aggregate_context_scores(
    comps: Sequence[PortComplement],
    idf: Mapping[tuple[str, str, str, str], float],
    ctx_card: str,
) -> dict[str, float]:
    """Per-candidate IDF-weighted synergy sum vs one context card.

    Mirrors ``UniversalScore.score`` dedup (one contribution per
    ``(rule_id, cmdr_event, cand_event, filter_group)`` key per
    candidate), synergy direction only, no staple/circuit/cmc/rank
    bonuses — the context term is a pure mechanical-match signal.
    Self-pairs (candidate == ctx_card) are dropped.
    """
    out: dict[str, float] = defaultdict(float)
    seen: set[tuple[str, str, str, str, str]] = set()
    for c in comps:
        if c.direction != "synergy" or c.candidate == ctx_card:
            continue
        key = (c.rule_id, c.cmdr_event, c.cand_event, c.filter_group)
        dkey = (c.candidate, *key)
        if dkey in seen:
            continue
        seen.add(dkey)
        out[c.candidate] += idf.get(key, 1.0)
    return dict(out)


#: Global cache: ctx card name -> {candidate -> context score}. Context
#: cards repeat heavily across commanders (staColor archetype overlap),
#: so this is the instrument's main wall-clock lever. Keyed by name only
#: — valid for one DB/config; the CLI process is single-run.
_CTX_SCORE_CACHE: dict[str, dict[str, float]] = {}


def context_scores_for_card(
    conn: sqlite3.Connection,
    ctx_card: str,
    *,
    candidate_cache=None,
) -> dict[str, float]:
    """Cached per-context-card scoring pass (one full complement walk)."""
    cached = _CTX_SCORE_CACHE.get(ctx_card)
    if cached is not None:
        return cached
    comps = find_all_complements(conn, [ctx_card], candidate_cache=candidate_cache)
    idf = _compute_idf_weights(comps)
    scores = aggregate_context_scores(comps, idf, ctx_card)
    _CTX_SCORE_CACHE[ctx_card] = scores
    return scores
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/bench/test_context_sim.py -v`
Expected: 4 PASS

- [ ] **Step 5: Identity guard + commit**

Run: `uv run scripts/bench.py audit --expect-identity` — Expected: PASS (no scoring-path change).

```bash
git add src/mtg_synergy_graph/bench/context_sim.py tests/bench/test_context_sim.py
git commit -m "feat(bench): context_sim core — context pool + per-context-card scoring (plan 2026-07-06-001 Task 1)"
```

### Task 2: `ContextSim` build + cell assembly + w=0 self-check

**Files:**
- Modify: `src/mtg_synergy_graph/bench/context_sim.py`
- Test: `tests/bench/test_context_sim.py`

**Interfaces:**
- Consumes: `SynergyEngine` (duck-typed: `page`, `legal_cards`, `_score_cache`, `_candidate_cache`, `_conn`); `load_edhrec_labels` from `mtg_synergy_graph.bench.optimize`; `SelfCheckError` from `mtg_synergy_graph.bench.portfolio_sim`.
- Produces: `ContextCell(k_context: int, w_ctx: float)`; `ContextSim` dataclass; `assemble_cell(sim, cell) -> tuple[str, ...]` (top-30); `build_context_sim(engine, edhrec_conn, commander, *, k_max=30) -> ContextSim`.

- [ ] **Step 1: Write the failing tests** (append to `tests/bench/test_context_sim.py`)

```python
from mtg_synergy_graph.bench.context_sim import ContextCell, ContextSim, assemble_cell


def _sim(**over):
    base = dict(
        commander="Cmdr",
        base_totals={"A": 3.0, "B": 2.0, "C": 1.0},
        base_top_30=("A", "B", "C"),
        pool_order=("A", "B", "C"),
        legal_pool=frozenset({"A", "B", "C", "NEW", "ILLEGAL_ELSEWHERE"}),
        context_max=("A", "B"),
        ctx_scores={"A": {"NEW": 4.0, "OFFCOLOR": 9.0}, "B": {"C": 4.0}},
        cmc_lookup={}, rank_lookup={},
        graded_labels={}, edhrec_top_30=None,
        zero_score_labels=frozenset(),
    )
    base.update(over)
    return ContextSim(**base)


def test_w0_cell_is_identity():
    sim = _sim()
    assert assemble_cell(sim, ContextCell(k_context=0, w_ctx=0.0)) == ("A", "B", "C")


def test_new_entrant_scores_and_illegal_excluded():
    sim = _sim()
    top = assemble_cell(sim, ContextCell(k_context=2, w_ctx=1.0))
    # NEW gets 1.0 * (4.0/2) = 2.0; C gets 1.0 + 4.0/2 = 3.0
    assert top == ("A", "C", "B", "NEW")
    assert "OFFCOLOR" not in top  # not in legal_pool


def test_commander_never_enters():
    sim = _sim(ctx_scores={"A": {"Cmdr": 99.0}, "B": {}})
    assert "Cmdr" not in assemble_cell(sim, ContextCell(2, 1.0))
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/bench/test_context_sim.py -v` — Expected: new tests FAIL with `ImportError: cannot import name 'ContextCell'`

- [ ] **Step 3: Implement** (append to `context_sim.py`)

```python
import time
from dataclasses import dataclass

from ..validate import compute_ndcg
from .optimize import load_edhrec_labels
from .portfolio_sim import SelfCheckError

_UNRANKED = 10**9


@dataclass(frozen=True)
class ContextCell:
    k_context: int
    w_ctx: float


@dataclass
class ContextSim:
    """One commander's cached pass-1 state, ready for cheap cell re-assembly."""

    commander: str
    base_totals: dict[str, float]        # page() total per scored candidate
    base_top_30: tuple[str, ...]
    pool_order: tuple[str, ...]
    legal_pool: frozenset[str]           # engine.legal_cards() — new entrants gate
    context_max: tuple[str, ...]         # top K_MAX rule-covered candidates
    ctx_scores: dict[str, dict[str, float]]
    cmc_lookup: dict[str, float]
    rank_lookup: dict[str, int]
    graded_labels: dict[str, float]
    edhrec_top_30: frozenset[str] | None
    #: EDHREC labels with NO pass-1 score — the in-instrument NO_RULES proxy.
    zero_score_labels: frozenset[str]


def assemble_cell(sim: ContextSim, cell: ContextCell) -> tuple[str, ...]:
    """Two-pass top-30 for one grid cell. w_ctx=0 is bitwise pass-1."""
    totals = dict(sim.base_totals)
    if cell.w_ctx > 0.0 and cell.k_context > 0:
        ctx = sim.context_max[: cell.k_context]
        agg: dict[str, float] = defaultdict(float)
        for ctx_card in ctx:
            for cand, s in sim.ctx_scores.get(ctx_card, {}).items():
                agg[cand] += s
        n = max(len(ctx), 1)
        for cand, s in agg.items():
            if cand == sim.commander or cand not in sim.legal_pool:
                continue
            totals[cand] = totals.get(cand, 0.0) + cell.w_ctx * (s / n)
    ranked = sorted(
        totals,
        key=lambda c: (
            -totals[c],
            sim.cmc_lookup.get(c, 99.0),
            sim.rank_lookup.get(c, _UNRANKED),
            c,
        ),
    )
    return tuple(ranked[:30])


def build_context_sim(engine, edhrec_conn, commander: str, *, k_max: int = 30) -> ContextSim:
    """Score one commander live, cache context scores, self-check w=0.

    Mirrors ``portfolio_sim.build_commander_sim``'s engine duck-typing
    (``page``, ``legal_cards``, ``_score_cache``, ``_candidate_cache``,
    ``_conn``). Raises ``SelfCheckError`` when the w=0 assembly diverges
    from ``page()``'s own top-30.
    """
    page = engine.page([commander], offset=0, limit=1_000_000)
    universal = engine._score_cache[(commander,)]
    base_totals = {rec.card: rec.total_score for rec in page.items}
    pool_order = tuple(rec.card for rec in page.items)

    cmc_lookup: dict[str, float] = {}
    rank_lookup: dict[str, int] = {}
    for name, row in engine._candidate_cache.candidate_rows.items():
        cmc_lookup[name] = row["cmc"] if row["cmc"] is not None else 99.0
        raw = row.get("edhrec_rank")
        rank_lookup[name] = int(raw) if raw is not None else _UNRANKED

    n_rules_map = {name: len(us.distinct_rules) for name, us in universal.items()}
    context_max = select_context(pool_order, n_rules_map, k_max)
    ctx_scores = {
        c: context_scores_for_card(engine._conn, c, candidate_cache=engine._candidate_cache)
        for c in context_max
    }
    legal_pool = frozenset(engine.legal_cards([commander]))

    graded_labels: dict[str, float] = {}
    edhrec_top_30: frozenset[str] | None = None
    if edhrec_conn is not None:
        labels = load_edhrec_labels(edhrec_conn, commander)
        graded_labels = dict(labels.graded_labels)
        edhrec_top_30 = labels.top_30_set
    zero_score_labels = frozenset(n for n in graded_labels if n not in base_totals)

    sim = ContextSim(
        commander=commander,
        base_totals=base_totals,
        base_top_30=pool_order[:30],
        pool_order=pool_order,
        legal_pool=legal_pool,
        context_max=context_max,
        ctx_scores=ctx_scores,
        cmc_lookup=cmc_lookup,
        rank_lookup=rank_lookup,
        graded_labels=graded_labels,
        edhrec_top_30=edhrec_top_30,
        zero_score_labels=zero_score_labels,
    )
    check = assemble_cell(sim, ContextCell(0, 0.0))
    if check != sim.base_top_30:
        diff = [(a, b) for a, b in zip(check, sim.base_top_30, strict=False) if a != b][:5]
        raise SelfCheckError(f"{commander}: w=0 assembly diverges from page(); first mismatches: {diff}")
    return sim
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/bench/test_context_sim.py -v` — Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat(bench): ContextSim build + cell assembly + w=0 self-check (plan 2026-07-06-001 Task 2)"
```

### Task 3: CLI (`bands` + `sweep`) and pinned noise bands

**Files:**
- Create: `scripts/context_sim.py`
- Modify: `src/mtg_synergy_graph/bench/context_sim.py`
- Test: `tests/bench/test_context_sim.py`

**Interfaces:**
- Consumes: `bootstrap_band`, `load_fixture_commanders`, `gem_rate_for_assembly` patterns from `mtg_synergy_graph.bench.portfolio_sim`; `compute_ndcg` from `mtg_synergy_graph.validate`.
- Produces: `run_bands(...) -> dict`, `run_sweep(...) -> dict`, `main(argv) -> int`; reports under `.audit/context_sim/`.

- [ ] **Step 1: Write the failing CLI test** (append)

```python
import json
from pathlib import Path

import pytest

_DB = Path("data/synergy.db")


def test_build_parser_has_subcommands():
    from mtg_synergy_graph.bench.context_sim import build_parser
    p = build_parser()
    args = p.parse_args(["bands", "--fixture", "tests/fixtures/golden_set_archetype_payoff.json"])
    assert args.command == "bands"


@pytest.mark.skipif(not _DB.exists(), reason="requires built data/synergy.db")
def test_bands_smoke_two_commanders(tmp_path):
    from mtg_synergy_graph.bench.context_sim import main
    rc = main([
        "bands",
        "--fixture", "tests/fixtures/golden_set_archetype_payoff.json",
        "--limit-commanders", "2",
        "--output-dir", str(tmp_path),
    ])
    assert rc == 0
    report = json.loads((tmp_path / "bands.json").read_text())
    assert "ndcg_band" in report and report["n_commanders"] == 2
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/bench/test_context_sim.py -k "parser or bands_smoke" -v` — Expected: FAIL (`build_parser` missing)

- [ ] **Step 3: Implement `run_bands`, `run_sweep`, `build_parser`, `main`**

Mirror `portfolio_sim.py`'s CLI skeleton (argparse subcommands, `--db data/synergy.db`, `--edhrec-db data/tags.db`, `--fixture`, `--output-dir .audit/context_sim`, `--limit-commanders N` for smoke runs, JSON + markdown report writer). Core logic:

```python
K_GRID = (10, 20, 30)
W_GRID = (0.1, 0.25, 0.5)


def run_bands(engine, edhrec_conn, commanders, *, seed=17, k_max=30):
    """w=0 baseline distribution -> bootstrap noise bands (pin BEFORE sweep)."""
    from .portfolio_sim import bootstrap_band
    ndcgs, gems = [], []
    sims = []
    for cmdr in commanders:
        sim = build_context_sim(engine, edhrec_conn, cmdr, k_max=k_max)
        sims.append(sim)
        if sim.graded_labels:
            ndcgs.append(compute_ndcg(list(sim.base_top_30), sim.graded_labels))
    return {
        "n_commanders": len(sims),
        "ndcg_band": bootstrap_band(ndcgs, seed=seed),
        "per_commander_ndcg": dict(zip((s.commander for s in sims), ndcgs, strict=False)),
    }, sims


def run_sweep(sims, *, cells=None):
    """Re-assemble every cell from cached sims; per-cell metrics + traps."""
    cells = cells or [ContextCell(k, w) for k in K_GRID for w in W_GRID]
    out = []
    for cell in cells:
        deltas, cliff, reach = [], 0, 0
        for sim in sims:
            if not sim.graded_labels:
                continue
            top = assemble_cell(sim, cell)
            d = compute_ndcg(list(top), sim.graded_labels) - compute_ndcg(list(sim.base_top_30), sim.graded_labels)
            deltas.append((sim.commander, d))
            if d < -0.05:
                cliff += 1
            reach += len(sim.zero_score_labels & set(top))
        mean = sum(d for _, d in deltas) / max(len(deltas), 1)
        out.append({
            "k_context": cell.k_context, "w_ctx": cell.w_ctx,
            "mean_ndcg_delta": mean, "cliffs": cliff, "reach": reach,
            "per_commander": dict(deltas),
        })
    return {"cells": out}
```

`main()` wires: open DBs (read-only intent; `SynergyEngine(db_path)`), load fixture commanders, `run_bands` → write `bands.json`/`bands.md`, and for `sweep`: reuse the sims from a bands pass in the same process, write `sweep.json`/`sweep.md` with the Decision Gates table (G1–G5) rendered against the pinned band values passed via `--h-cohort`/`--h-500`/`--g-500` args. Gem deltas per cell via the `gem_rate_for_assembly` pattern (port it to `ContextSim` fields — same formula: `(set(top) − edhrec_top_30) ∩ plausible`; for the instrument use plausible = candidates with ≥2 distinct rules OR above-median pass-1 total, matching `bench/hidden_gems.py` criteria; import its helper if importable rather than re-deriving).

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/bench/test_context_sim.py -v` — Expected: all PASS (smoke test may take ~1–2 min; it builds 2 live sims)

- [ ] **Step 5: Create the CLI wrapper**

```python
#!/usr/bin/env python3
# scripts/context_sim.py
"""CLI wrapper for the deck-context kill-test instrument (plan 2026-07-06-001)."""
import sys

from mtg_synergy_graph.bench.context_sim import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
```

- [ ] **Step 6: Pin the bands (RECORD THE NUMBERS)**

```bash
uv run python scripts/context_sim.py bands --fixture tests/fixtures/golden_set_archetype_payoff.json
uv run python scripts/context_sim.py bands --fixture tests/fixtures/golden_set_run_500.json
```

Record `H_cohort` (cohort half-width), `H_500`, `G_500` (gem band) into `.audit/context_sim/PINNED_GATES.md` AND into this plan file under Decision Gates. These are pinned before any sweep cell is looked at. Expected wall clock: cohort ~10 min, 500-cmdr up to ~1.5 h on first run (context-card cache is cold; it warms across commanders).

- [ ] **Step 7: Identity guard + commit**

Run: `uv run scripts/bench.py audit --expect-identity` — Expected: PASS.

```bash
git add -A && git commit -m "feat(bench): context_sim CLI, bands + sweep, pinned noise bands (plan 2026-07-06-001 Task 3)"
```

### Task 4: Whitelist-equivalence baseline (G4 comparator)

**Files:**
- Modify: `src/mtg_synergy_graph/bench/context_sim.py`
- Test: `tests/bench/test_context_sim.py`

**Interfaces:**
- Consumes: `_is_death_event`, `_token_subtype_vocab`, `_valid_filter_subtype_tokens` from `mtg_synergy_graph.bench.cohorts` (read-side instrument importing read-side privates — the portfolio_sim/engine._score_cache precedent).
- Produces: `payoff_subtypes(conn, commander) -> frozenset[str]`; `whitelist_scores(conn, commander) -> dict[str, float]`; `sweep --whitelist-baseline` mode over bonus grid `B_GRID = (0.1, 0.25, 0.5)` (matched to `W_GRID` scale).

- [ ] **Step 1: Write the failing tests** (append; live-DB, skip-guarded)

```python
@pytest.mark.skipif(not _DB.exists(), reason="requires built data/synergy.db")
def test_payoff_subtypes_slimefoot():
    import sqlite3
    from mtg_synergy_graph.bench.context_sim import payoff_subtypes
    conn = sqlite3.connect(_DB)
    conn.row_factory = sqlite3.Row
    subs = payoff_subtypes(conn, "Slimefoot, the Stowaway")
    assert "Saproling" in subs


@pytest.mark.skipif(not _DB.exists(), reason="requires built data/synergy.db")
def test_whitelist_scores_cover_subtype_bodies_and_producers():
    import sqlite3
    from mtg_synergy_graph.bench.context_sim import whitelist_scores
    conn = sqlite3.connect(_DB)
    conn.row_factory = sqlite3.Row
    wl = whitelist_scores(conn, "Slimefoot, the Stowaway")
    assert wl  # non-empty for a cohort commander
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/bench/test_context_sim.py -k whitelist -v` — Expected: FAIL (imports missing)

- [ ] **Step 3: Implement**

```python
def payoff_subtypes(conn: sqlite3.Connection, commander: str) -> frozenset[str]:
    """The commander's subtype-keyed death-payoff subtypes.

    Re-derives the per-commander subtype the same way
    ``cohorts.subtype_death_payoff`` selects cohort members, so the
    whitelist baseline is exactly "the predicate as a rule".
    """
    from .cohorts import _is_death_event, _token_subtype_vocab, _valid_filter_subtype_tokens

    vocab = _token_subtype_vocab(conn)
    subs: set[str] = set()
    rows = conn.execute(
        "SELECT event_class, valid_filter, zone_origin, zone_destination "
        "FROM card_ports WHERE card_name = ? AND port_type = 'trigger'",
        (commander,),
    )
    for event_class, valid_filter, zo, zd in rows:
        if not _is_death_event(event_class, zo, zd):
            continue
        subs.update(t for t in _valid_filter_subtype_tokens(valid_filter or "") if t in vocab)
    return frozenset(subs)


def whitelist_scores(conn: sqlite3.Connection, commander: str) -> dict[str, float]:
    """Flat 1.0 for every subtype body or subtype-token producer.

    The G4 comparator: a disguised whitelist a rule could hardcode.
    Scaled by the bonus grid at assembly time (same slot as w_ctx).
    """
    subs = payoff_subtypes(conn, commander)
    names: set[str] = set()
    for s in subs:
        names.update(
            n for (n,) in conn.execute("SELECT name FROM cards WHERE card_types LIKE ?", (f"%{s}%",))
        )
        names.update(
            n for (n,) in conn.execute(
                "SELECT p.card_name FROM card_ports p "
                "JOIN port_attributes a ON a.port_id = p.id "
                "WHERE a.attr_kind = 'token_subtype' AND a.attr_value = ?",
                (s,),
            )
        )
    names.discard(commander)
    return dict.fromkeys(names, 1.0)
```

Wire `--whitelist-baseline` into `run_sweep`: for each bonus `b` in `B_GRID`, assemble with `totals[cand] += b` for whitelist members (legal-pool-gated, same as context entrants) and report cohort mean delta per `b`. The BEST whitelist cohort delta is the G4 bar.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/bench/test_context_sim.py -v` — Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat(bench): whitelist-equivalence baseline for G4 (plan 2026-07-06-001 Task 4)"
```

### Task 5: Staged sweep + routing decision

**Files:**
- Modify: `.audit/context_sim/` outputs; this plan file (decision record); possibly `docs/solutions/best-practices/` (DECLINE branch)

- [ ] **Step 1: Stage 1 sweep — cohort fixture + golden-100**

```bash
uv run python scripts/context_sim.py sweep --fixture tests/fixtures/golden_set_archetype_payoff.json
uv run python scripts/context_sim.py sweep --fixture tests/fixtures/golden_set_run.json
uv run python scripts/context_sim.py sweep --fixture tests/fixtures/golden_set_archetype_payoff.json --whitelist-baseline
```

Evaluate G1 (cohort delta vs `H_cohort`), G3 (reach ≥ 100 on golden-100), G4 (vs best whitelist delta), G5 (traps — golden-100 contains Talrand; resolve the other three via `portfolio_sim.resolve_trap_commanders` against the 500 fixture in Stage 2). Cells failing G1/G3/G4 here are dead — do not carry to Stage 2.

- [ ] **Step 2: Stage 2 — survivors only, golden-500**

```bash
uv run python scripts/context_sim.py sweep --fixture tests/fixtures/golden_set_run_500.json --cells "K,W;K,W"
```

(Add a `--cells` arg parsing `k,w` pairs so only survivors pay the 500-cmdr cost.) Evaluate G2 + G5.

- [ ] **Step 3: Record the routing decision**

Append a `## DECISION` block at the top of this plan (mirror plan 2026-07-02-004's format): every gate's measured value vs its pinned bar, per cell, and the verdict — **SHIP-candidate (→ Phase B)** or **DECLINED**.

- [ ] **Step 4 (DECLINE branch only): null-result doc**

Create `docs/solutions/best-practices/deck-context-null-result-2026-07-06.md` with YAML frontmatter (`module: universal_scorer`, `tags: [deck-context, two-pass, kill-test, null-result, no-rules]`, `problem_type: null-result`, `applies_when` including "considering any deck-level / second-pass context mechanism"), the measured gate table, and the honest interpretation. Skip Tasks 6–8; proceed to Task 9. Update plan frontmatter `status: declined`.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat(bench): context_sim sweep results + routing decision (plan 2026-07-06-001 Task 5)"
```

---

## Phase B — Conditional integration (ONLY on Phase A SHIP-candidate)

### Task 6: `deck_context` module — flag, shared math, config-hash registration

**Files:**
- Create: `src/mtg_synergy_graph/deck_context.py`
- Modify: `src/mtg_synergy_graph/universal_scorer.py` (ScoringConfigInputs), `src/mtg_synergy_graph/bench/context_sim.py` (import shared math)
- Test: `tests/test_deck_context_flag.py`

**Interfaces:**
- Produces: `_ENABLE_DECK_CONTEXT: bool = False`; `_CTX_K: int`; `_CTX_W: float` (pinned from the winning cell); `context_totals(base_totals, ctx_scores, ctx, w, *, legal, commander) -> dict[str, float]` — the assembly math, moved here so instrument and production share one source of truth; `ScoringConfigInputs` gains `enable_deck_context: bool`, `ctx_k: int`, `ctx_w: float`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_deck_context_flag.py
"""Flag-gate identity tests (canonical shape: test_universal_scorer_concave_agg.py)."""

from mtg_synergy_graph import deck_context
from mtg_synergy_graph.universal_scorer import get_scoring_config_inputs


def test_flag_default_off():
    assert deck_context._ENABLE_DECK_CONTEXT is False


def test_config_inputs_expose_deck_context_fields():
    cfg = get_scoring_config_inputs()
    assert cfg.enable_deck_context is False
    assert isinstance(cfg.ctx_k, int)
    assert isinstance(cfg.ctx_w, float)


def test_context_totals_off_is_identity():
    base = {"A": 1.0}
    out = deck_context.context_totals(
        base, ctx_scores={"A": {"B": 5.0}}, ctx=("A",), w=0.0,
        legal=frozenset({"A", "B"}), commander="C",
    )
    assert out == base
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/test_deck_context_flag.py -v` → FAIL (module missing)

- [ ] **Step 3: Implement**

`deck_context.py`: module docstring citing this plan + the Phase A decision block; the three module constants (values from the winning cell); `context_totals` extracted verbatim from `context_sim.assemble_cell`'s inner block (returning a NEW dict; pure). Refactor `assemble_cell` to call it. In `universal_scorer.py`: add the three fields to `ScoringConfigInputs` with a `#: Plan 2026-07-06-001 Phase B` comment block (pattern: the `enable_concave_family_agg` field) and wire them in `get_scoring_config_inputs()` via a local `from . import deck_context` import.

- [ ] **Step 4: Run** — `uv run pytest tests/test_deck_context_flag.py tests/bench/test_context_sim.py tests/test_scoring_weights.py -v` → PASS; then full `uv run pytest tests/ -x -q` → PASS; `uv run scripts/bench.py audit --expect-identity` → PASS (flag off).

- [ ] **Step 5: Commit** — `git commit -m "feat(scoring): deck_context flag module, OFF-identical, hash-registered (plan 2026-07-06-001 Task 6)"`

### Task 7: `page()`/`score_one` integration + eval-path routing + explain + optimizer guard

**Files:**
- Modify: `src/mtg_synergy_graph/engine.py` (`page()` between sort and slice — the plan-004 hook point at `engine.py:413-423`), `src/mtg_synergy_graph/deck_context.py`, `src/mtg_synergy_graph/bench/fixture.py` (fixture-build scoring path), `src/mtg_synergy_graph/bench/optimize.py` (guard)
- Test: `tests/test_deck_context_flag.py`

- [ ] **Step 1: Write the failing tests**

```python
import pytest
from pathlib import Path

_DB = Path("data/synergy.db")


@pytest.mark.skipif(not _DB.exists(), reason="requires built data/synergy.db")
def test_page_flag_on_reranks_and_bucket_invariant(monkeypatch):
    from mtg_synergy_graph import deck_context
    from mtg_synergy_graph.engine import SynergyEngine
    monkeypatch.setattr(deck_context, "_ENABLE_DECK_CONTEXT", True)
    with SynergyEngine(db_path=_DB) as eng:
        page = eng.page("Slimefoot, the Stowaway", limit=30)
    for rec in page.items:
        named = sum(v for k, v in rec.scores.items() if k != "total")
        assert abs(named - rec.scores["total"]) < 1e-9  # sum(buckets) == total


@pytest.mark.skipif(not _DB.exists(), reason="requires built data/synergy.db")
def test_optimize_refuses_with_flag_on(monkeypatch):
    from mtg_synergy_graph import deck_context
    from mtg_synergy_graph.bench.optimize import guard_deck_context_flag
    monkeypatch.setattr(deck_context, "_ENABLE_DECK_CONTEXT", True)
    with pytest.raises(RuntimeError, match="deck-context"):
        guard_deck_context_flag()
```

- [ ] **Step 2: Run to verify failure** — expected FAIL (`guard_deck_context_flag` missing; bucket invariant fails without a `deck_context` bucket)

- [ ] **Step 3: Implement**

In `deck_context.py` add the production entry point:

```python
def page_context_totals(conn, commander, base_totals, legal, *, candidate_cache, n_rules_map):
    """Production two-pass totals + per-candidate context bucket values.

    Returns (totals, ctx_bucket) where ctx_bucket[cand] is the additive
    context term (0.0 absent). New entrants (no pass-1 score) appear in
    totals with score == their context term.
    """
    from .bench.context_sim import context_scores_for_card, select_context

    pool_order = sorted(base_totals, key=lambda c: -base_totals[c])
    ctx = select_context(pool_order, n_rules_map, _CTX_K)
    ctx_scores = {c: context_scores_for_card(conn, c, candidate_cache=candidate_cache) for c in ctx}
    totals = context_totals(base_totals, ctx_scores=ctx_scores, ctx=ctx, w=_CTX_W, legal=legal, commander=commander)
    ctx_bucket = {c: totals.get(c, 0.0) - base_totals.get(c, 0.0) for c in totals}
    return totals, ctx_bucket
```

In `engine.page()` after the `ranked.sort(...)` block (engine.py:413-420), flag-gated and partner-exempt (`len(cmdr_set) == 1`): call `page_context_totals`, rebuild `ranked` from the new totals (new entrants get `empty_buckets()` + `scores["deck_context"] = term` + `scores["total"] = term`; existing entries get `buckets["deck_context"] = term`, `buckets["total"] += term`), re-sort with the same 4-key. Add `"deck_context"` to `BUCKETS` in `scoring.py` (keeps the sum invariant). `score_one` routes through the same helper when the flag is on. `_render_explanation` gains: `if scores.get("deck_context", 0): lines.append(f"{card} synergizes with the deck's top-{_CTX_K} context pool (+{scores['deck_context']:.2f}).")`. Eval-path: in `bench/fixture.py`, at the point where per-commander scores are ranked into `FixtureEntry.scores` (grep `def build_fixture` and its sort), apply the same flag-gated helper so pinned fixtures see what users see (plan-004 R12 discipline). In `bench/optimize.py` add and call at driver entry:

```python
def guard_deck_context_flag() -> None:
    from .. import deck_context
    if deck_context._ENABLE_DECK_CONTEXT:
        raise RuntimeError(
            "--optimize refuses to run while the deck-context flag is ON: "
            "the optimizer's pointwise objective does not see the context pass "
            "(plan 2026-07-06-001 Task 7; precedent plan 2026-07-02-004 Unit 7)"
        )
```

- [ ] **Step 4: Run** — `uv run pytest tests/test_deck_context_flag.py -v` → PASS; full suite `uv run pytest tests/ -q` → PASS; `uv run scripts/bench.py audit --expect-identity` → PASS (flag still off).

- [ ] **Step 5: Commit** — `git commit -m "feat(engine): flag-gated deck-context second pass in page()/eval path (plan 2026-07-06-001 Task 7)"`

### Task 8: Flip + evidence package + re-pin + docs

- [ ] **Step 1: Flip** `_ENABLE_DECK_CONTEXT = True` (one-line edit, cite the decision block).
- [ ] **Step 2: Evidence** — run and save outputs:

```bash
uv run scripts/bench.py audit                      # expect POSITIVE/ACCEPTABLE verdict; hash flipped
uv run scripts/bench.py audit --per-commander-ndcg --fixture tests/fixtures/golden_set_archetype_payoff.json
uv run scripts/bench.py audit --forensics          # NO_RULES share must drop; record before/after
```

Cohort readout must clear `H_cohort` ON THIS instrument's band too (recompute the reporter band per the CLAUDE.md NOISE BAND note — reporter and page-ranking bands differ).

- [ ] **Step 3: Re-pin** all committed fixtures:

```bash
uv run scripts/bench.py audit --repin --yes
uv run scripts/bench.py audit --repin --yes --fixture tests/fixtures/golden_set_run_500.json
uv run scripts/bench.py audit --repin --yes --fixture tests/fixtures/golden_set_archetype_payoff.json
uv run pytest tests/bench/test_fixture_freshness.py -v
```

- [ ] **Step 4: Docs** — `docs/RULE_HISTORY.md` dated entry (verdict, cohort delta, forensics before/after); CLAUDE.md: add `scripts/context_sim.py` commands + flag note; update the NDCG figure in Project Overview.
- [ ] **Step 5: Commit** — `git commit -m "feat(scoring): enable deck-context second pass; re-pin fixtures (plan 2026-07-06-001 Task 8)"`

---

## Phase C — Magnitude quality prior kill test (independent of A/B; runs even on Phase A DECLINE)

### Task 9: `quality.py` — deterministic effect-per-mana rate signal

**Files:**
- Create: `src/mtg_synergy_graph/quality.py`
- Test: `tests/test_quality.py`

**Interfaces:**
- Produces: `_amount_value(amount: str) -> float`; `rate_signal(conn) -> dict[str, float]` (card name → rate ≥ 0); `quality_multiplier(rate, *, q, r0) -> float` = `1.0 + q * tanh(rate / r0)`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_quality.py
import math
import sqlite3

from mtg_synergy_graph.quality import _amount_value, quality_multiplier, rate_signal


def test_amount_value_mapping():
    assert _amount_value("3") == 3.0
    assert _amount_value("X") == 2.5
    assert _amount_value("All") == 4.0
    assert _amount_value("SVarWeird") == 1.0
    assert _amount_value("99") == 6.0   # capped
    assert _amount_value("-1") == 0.0   # floored


def test_quality_multiplier_bounded():
    assert quality_multiplier(0.0, q=0.2, r0=2.0) == 1.0
    assert quality_multiplier(1e9, q=0.2, r0=2.0) < 1.2 + 1e-9


def test_rate_signal_from_synthetic_db(tmp_path):
    conn = sqlite3.connect(tmp_path / "t.db")
    conn.execute("CREATE TABLE cards (name TEXT, cmc REAL)")
    conn.execute("CREATE TABLE card_ports (card_name TEXT, port_type TEXT, amount TEXT)")
    conn.execute("INSERT INTO cards VALUES ('Engine', 2.0), ('OneShot', 2.0)")
    conn.executemany(
        "INSERT INTO card_ports VALUES (?, ?, ?)",
        [("Engine", "trigger", ""), ("Engine", "effect", "2"),
         ("OneShot", "effect", "2")],
    )
    rates = rate_signal(conn)
    assert math.isclose(rates["Engine"], 1.0)     # 1.0 * 2 / 2
    assert math.isclose(rates["OneShot"], 0.5)    # 0.5 * 2 / 2 (no engine shape)
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/test_quality.py -v` → FAIL (module missing)

- [ ] **Step 3: Implement**

```python
# src/mtg_synergy_graph/quality.py
"""Deterministic effect-per-mana rate signal (plan 2026-07-06-001 Phase C).

Built entirely from Forge-extracted data already in synergy.db:
``card_ports.amount`` magnitudes (effect/static rows), an engine-shape
marker (any trigger or activation-cost port -> repeatable), and cmc.
No EDHREC, no popularity, no hand-curated card list. Design-time
kill-test only until the Phase C gate passes.
"""

from __future__ import annotations

import math
import sqlite3
from collections import defaultdict

#: Cards with any of these ports have a repeatable engine shape; pure
#: one-shot spells get half weight — a Divination is worth less per
#: mana than a "draw each turn" engine at the same printed amount.
_ENGINE_MARKER_PORTS = frozenset({"trigger", "cost"})
_AMOUNT_BEARING_PORTS = frozenset({"effect", "static"})
_ONE_SHOT_WEIGHT = 0.5
_VARIABLE_AMOUNT_VALUE = 2.5   # X/Y/Z — scales with investment
_ALL_AMOUNT_VALUE = 4.0        # "All" — board-scope effects
_AMOUNT_CAP = 6.0
_DEFAULT_CMC = 4.0


def _amount_value(amount: str) -> float:
    if amount in ("X", "Y", "Z"):
        return _VARIABLE_AMOUNT_VALUE
    if amount == "All":
        return _ALL_AMOUNT_VALUE
    try:
        v = float(amount)
    except ValueError:
        return 1.0
    return min(max(v, 0.0), _AMOUNT_CAP)


def quality_multiplier(rate: float, *, q: float, r0: float) -> float:
    """Bounded multiplicative prior: 1.0 at rate 0, asymptote 1+q."""
    return 1.0 + q * math.tanh(rate / r0)


def rate_signal(conn: sqlite3.Connection) -> dict[str, float]:
    output: dict[str, float] = defaultdict(float)
    engine_shape: set[str] = set()
    for name, ptype, amount in conn.execute("SELECT card_name, port_type, amount FROM card_ports"):
        if ptype in _ENGINE_MARKER_PORTS:
            engine_shape.add(name)
        if ptype in _AMOUNT_BEARING_PORTS and amount:
            output[name] += _amount_value(amount)
    cmc = {n: (c if c is not None else _DEFAULT_CMC) for n, c in conn.execute("SELECT name, cmc FROM cards")}
    return {
        name: (1.0 if name in engine_shape else _ONE_SHOT_WEIGHT) * out / max(cmc.get(name, _DEFAULT_CMC), 1.0)
        for name, out in output.items()
    }
```

- [ ] **Step 4: Run** — `uv run pytest tests/test_quality.py -v` → PASS. `uv run scripts/bench.py audit --expect-identity` → PASS.
- [ ] **Step 5: Commit** — `git commit -m "feat(scoring): quality.py rate signal, design-time only (plan 2026-07-06-001 Task 9)"`

### Task 10: `quality_sim` instrument + pinned gates + sweep

**Files:**
- Create: `src/mtg_synergy_graph/bench/quality_sim.py`, `scripts/quality_sim.py`
- Test: `tests/test_quality.py`

**Interfaces:**
- Consumes: `build_commander_sim`, `bootstrap_band`, `load_fixture_commanders`, `gem_rate_for_assembly`, `resolve_trap_commanders` from `mtg_synergy_graph.bench.portfolio_sim`; `rate_signal`, `quality_multiplier` from `mtg_synergy_graph.quality`; `compute_ndcg` from `mtg_synergy_graph.validate`.
- Produces: `assemble_quality(sim, rates, *, q, r0) -> tuple[str, ...]`; `run_bands` / `run_sweep` / `main` (same CLI shape as context_sim); reports under `.audit/quality_sim/`. Grid: `q ∈ (0.05, 0.1, 0.2)` × `r0 ∈ (1.0, 2.0, 4.0)`.

- [ ] **Step 1: Write the failing test**

```python
def test_assemble_quality_q0_is_identity_and_reorders_at_q():
    from mtg_synergy_graph.bench.quality_sim import assemble_quality

    class FakeSim:
        commander = "C"
        pool_order = ("A", "B")
        base_top_30 = ("A", "B")
        base_totals = {"A": 1.00, "B": 0.99}
        cmc_lookup = {}
        rank_lookup = {}

    rates = {"B": 10.0, "A": 0.0}
    assert assemble_quality(FakeSim(), rates, q=0.0, r0=2.0) == ("A", "B")
    assert assemble_quality(FakeSim(), rates, q=0.2, r0=2.0) == ("B", "A")
```

- [ ] **Step 2: Run to verify failure** — FAIL (module missing)

- [ ] **Step 3: Implement**

`assemble_quality`: `totals = {c: sim.base_totals[c] * quality_multiplier(rates.get(c, 0.0), q=q, r0=r0) for c in sim.base_totals}`, production 4-key sort, top-30. Note the multiplier applies to the *pool ranking only* (no new entrants — this is an OUTRANKED lever; NO_RULES stays Phase A's job). `build` path wraps `portfolio_sim.build_commander_sim` (it already carries `base_top_30`, `graded_labels`, gem plumbing, λ=0 self-check). `run_bands`: q=0 distribution → pin `H_500q`, `G_500q`. `run_sweep`: 9 cells on golden-100 first; survivors re-run on golden-500; per-cell mean NDCG delta, cliffs, gem delta, trap sidecar (`resolve_trap_commanders`). CLI wrapper identical in shape to `scripts/context_sim.py`.

- [ ] **Step 4: Run** — `uv run pytest tests/test_quality.py -v` → PASS; `--expect-identity` → PASS.

- [ ] **Step 5: Bands then sweep (pinned order)**

```bash
uv run python scripts/quality_sim.py bands --fixture tests/fixtures/golden_set_run_500.json   # pin H_500q, G_500q FIRST
uv run python scripts/quality_sim.py sweep --fixture tests/fixtures/golden_set_run.json
uv run python scripts/quality_sim.py sweep --fixture tests/fixtures/golden_set_run_500.json --cells "..."  # survivors
```

- [ ] **Step 6: Commit** — `git commit -m "feat(bench): quality_sim kill-test instrument + sweep (plan 2026-07-06-001 Task 10)"`

### Task 11: Phase C routing — integrate or decline

- [ ] **Step 1: Evaluate the pinned Phase C gate** (Decision Gates section) against the sweep report; append the `## DECISION` block to this plan.

- [ ] **Step 2 (DECLINE branch):** write `docs/solutions/best-practices/quality-rate-prior-null-result-2026-07-06.md` (frontmatter `tags: [quality-prior, magnitude, outranked, kill-test, null-result]`, `applies_when` including "considering any card-quality / rate / power-level prior on the OUTRANKED bucket") recording that all three OUTRANKED levers (reweight, rerank, new-information prior) are now measured. Stop here.

- [ ] **Step 3 (SHIP branch):** integrate via the flag playbook, mirroring Task 6/7 exactly:
  - `universal_scorer.py`: `_ENABLE_QUALITY_RATE: bool = False`, `_QUALITY_Q: float`, `_QUALITY_R0: float` (winning cell); `UniversalScore` gains field `quality_mult: float = 1.0`; `score` returns `base * self.quality_mult`; `to_legacy_buckets` multiplies `total` and adds bucket `"quality_adjust" = total_before * (quality_mult - 1.0)` to keep the sum invariant; `score_from_complements` computes `rates = rate_signal(conn)` once per call (module-level cache keyed by `id(conn)` is NOT acceptable — pass via `candidate_cache` extension or a `functools.lru_cache` on a `db_fingerprint`; follow the `load_card_embeddings_verified` per-call-load precedent) and sets `quality_mult = quality_multiplier(rates.get(name, 0.0), q=_QUALITY_Q, r0=_QUALITY_R0)` when the flag is on.
  - `ScoringConfigInputs` += `enable_quality_rate: bool`, `quality_q: float`, `quality_r0: float`.
  - Flag-gate identity test file `tests/test_quality_rate_flag.py` (same shape as Task 6 tests).
  - Flip + evidence (`audit`, `--forensics` OUTRANKED before/after) + re-pin all three fixtures + docs — same steps as Task 8.

- [ ] **Step 4: Commit** — `git commit -m "feat(scoring): quality-rate prior routing decision (plan 2026-07-06-001 Task 11)"`

---

## Deferred to Separate Cycles (record; do not start)

- **Forge goldfish simulation as design-time oracle** — Forge plays games; simulate commander+candidate shells, measure engine throughput deltas, calibrate `quality.py` rates and context weights empirically. Needs its own `ce-brainstorm` (JVM harness, determinism, cost).
- **MayPlay / projection vocabulary** — 671 `MayPlay` statics (Gravecrawler et al.) project to `static/Continuous`, invisible to recursion-shaped matching; a `port_nodes` vocabulary expansion candidate. Run `bench.py audit --unknowns` first; fold into the next vocabulary cycle.
- **DATA_GAP sweep** — 95 `unknown_ports` misses; same `--unknowns` cycle.
- **Optimizer objective alignment** with any shipped flag (the Task 7 guard forces this before the next `--optimize` run).
- **Partner-pair support** for both mechanisms.

## Self-Review Notes

- Spec coverage: NO_RULES → Tasks 1–8; OUTRANKED → Tasks 9–11; whitelist-equivalence obligation from plan 2026-07-03-001 → Task 4 + G4; instrument-own noise bands → Task 3/10 bands-before-sweep ordering; dead-axes avoidance → Problem Frame table + G-gates.
- Type consistency: `ContextCell`/`ContextSim`/`assemble_cell` names match across Tasks 1–7; `context_totals` extraction (Task 6) is the single shared-math source; `quality_multiplier(rate, *, q, r0)` signature consistent across Tasks 9–11.
- Known softness (deliberate): Task 7's `bench/fixture.py` hook and Task 3's markdown-report rendering direct the engineer to a named grep target and a named template file (`portfolio_sim.py`) rather than inlining ~200 lines of report plumbing; the behavior contracts (identity tests, invariants) pin correctness.
