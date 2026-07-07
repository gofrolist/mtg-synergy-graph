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

import argparse
import sqlite3
import sys
import time
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..complement_rules import PortComplement, find_all_complements
from ..universal_scorer import _compute_idf_weights
from ..validate import compute_ndcg
from ._sim_shared import UNRANKED, add_common_sim_args, parse_grid_cells, render_bands_markdown, write_text
from .hidden_gems import _cohort_median, _plausibility_from_maps
from .optimize import load_edhrec_labels
from .portfolio_sim import (
    PortfolioSimError,
    SelfCheckError,
    _write_report,
    bootstrap_band,
    load_fixture_commanders,
)

if TYPE_CHECKING:
    from ..engine import SynergyEngine


def select_context(
    pool_order: Sequence[str],
    n_rules_map: Mapping[str, int],
    k: int,
) -> tuple[str, ...]:
    """Top-``k`` pass-1 candidates with >=1 distinct rule (pool order).

    Staple-only / rule-free candidates are excluded: they carry no
    mechanical signature for the context pass to match against.
    ``k <= 0`` returns an empty tuple — without this guard the
    ``len(out) == k`` break condition is never true for ``k <= 0`` and
    the loop falls through, silently returning every eligible
    candidate instead of none.
    """
    if k <= 0:
        return ()
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


def _plausible_set(n_rules_map: Mapping[str, int], base_totals: Mapping[str, float]) -> frozenset[str]:
    """Mechanical-plausibility gate over ``base_totals`` (hidden_gems.py FR2 criteria).

    Delegates to the canonical helpers in ``bench.hidden_gems`` (``_cohort_median`` /
    ``_plausibility_from_maps``) so the FORMULA cannot drift — but the INPUTS differ
    from the audit gate: this instrument feeds ``page()`` total scores (which include
    staple/circuit/cmc/rank bonuses) and per-candidate distinct-rule counts, while
    the audit / portfolio_sim feed tensor rule-net contribution totals. The resulting
    gem rate is therefore INSTRUMENT-LOCAL and must not be compared to the audit's
    ``hidden_gem_hit_rate`` or to quality_sim/portfolio_sim gem bands (empirically:
    golden-500 gem half-width 0.0235 here vs 0.0355 there on the same fixture). Gate
    any decision on THIS instrument's own pinned band.
    """
    median = _cohort_median(base_totals)
    return frozenset(name for name in base_totals if _plausibility_from_maps(name, n_rules_map, base_totals, median))


@dataclass(frozen=True)
class ContextCell:
    k_context: int
    w_ctx: float


@dataclass
class ContextSim:
    """One commander's cached pass-1 state, ready for cheap cell re-assembly."""

    commander: str
    base_totals: dict[str, float]  # page() total per scored candidate
    base_top_30: tuple[str, ...]
    pool_order: tuple[str, ...]
    legal_pool: frozenset[str]  # engine.legal_cards() — new entrants gate
    context_max: tuple[str, ...]  # top K_MAX rule-covered candidates
    ctx_scores: dict[str, dict[str, float]]
    cmc_lookup: dict[str, float]
    rank_lookup: dict[str, int]
    graded_labels: dict[str, float]
    edhrec_top_30: frozenset[str] | None
    #: EDHREC labels with NO pass-1 score — the in-instrument NO_RULES proxy.
    zero_score_labels: frozenset[str]
    #: Mechanical-plausibility gate over ``base_totals`` (hidden_gems.py
    #: criteria, computed against this commander's own cohort): >=2
    #: distinct synergy rules OR a pass-1 total above the cohort median.
    #: Trailing default keeps existing positional/keyword constructor
    #: call sites (tests) working unchanged.
    plausible: frozenset[str] = frozenset()


def _rank_top30(sim: ContextSim, totals: Mapping[str, float]) -> tuple[str, ...]:
    """Shared 4-key sort (-total, cmc, edhrec_rank, name) -> top-30 names."""
    ranked = sorted(
        totals,
        key=lambda c: (
            -totals[c],
            sim.cmc_lookup.get(c, 99.0),
            sim.rank_lookup.get(c, UNRANKED),
            c,
        ),
    )
    return tuple(ranked[:30])


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
    return _rank_top30(sim, totals)


def assemble_whitelist(sim: ContextSim, wl: Mapping[str, float], b: float) -> tuple[str, ...]:
    """Top-30 with whitelist members bumped by a flat bonus ``b`` (G4 comparator).

    Same legal-pool gating and commander exclusion as :func:`assemble_cell`,
    but substitutes a flat per-member bonus for the context term — this is
    "the predicate as a disguised whitelist a rule could hardcode" baseline
    against which a real mechanism's context-term gain must be judged.
    """
    totals = dict(sim.base_totals)
    for cand in wl:
        if cand == sim.commander or cand not in sim.legal_pool:
            continue
        totals[cand] = totals.get(cand, 0.0) + b
    return _rank_top30(sim, totals)


#: Per-engine cache of the shared cmc/edhrec_rank lookups derived from
#: ``engine._candidate_cache.candidate_rows`` (~32k rows, engine-lifetime
#: constant). Keyed by ``id(candidate_cache)`` — valid for one process
#: (same single-run assumption as ``_CTX_SCORE_CACHE`` above); avoids
#: every :class:`ContextSim` in a run retaining its own ~2.5GB rebuilt
#: copy of the same two dicts.
_ENGINE_LOOKUPS_CACHE: dict[int, tuple[dict[str, float], dict[str, int]]] = {}


def _engine_lookups(engine) -> tuple[dict[str, float], dict[str, int]]:
    """Shared ``(cmc_lookup, rank_lookup)`` for one engine, computed once.

    Callers may hold onto the returned dicts and pass them back into
    later :func:`build_context_sim` calls — they are read-only from
    every call site (``.get()`` only, see ``_rank_top30``), so sharing
    the same dict references across sims is safe.
    """
    key = id(engine._candidate_cache)
    cached = _ENGINE_LOOKUPS_CACHE.get(key)
    if cached is not None:
        return cached
    cmc_lookup: dict[str, float] = {}
    rank_lookup: dict[str, int] = {}
    for name, row in engine._candidate_cache.candidate_rows.items():
        cmc_lookup[name] = row["cmc"] if row["cmc"] is not None else 99.0
        raw = row.get("edhrec_rank")
        rank_lookup[name] = int(raw) if raw is not None else UNRANKED
    result = (cmc_lookup, rank_lookup)
    _ENGINE_LOOKUPS_CACHE[key] = result
    return result


def build_context_sim(
    engine,
    edhrec_conn,
    commander: str,
    *,
    k_max: int = 30,
    cmc_lookup: dict[str, float] | None = None,
    rank_lookup: dict[str, int] | None = None,
) -> ContextSim:
    """Score one commander live, cache context scores, self-check w=0.

    Mirrors ``portfolio_sim.build_commander_sim``'s engine duck-typing
    (``page``, ``legal_cards``, ``_score_cache``, ``_candidate_cache``,
    ``_conn``). Raises ``SelfCheckError`` when the w=0 assembly diverges
    from ``page()``'s own top-30.

    ``cmc_lookup``/``rank_lookup`` default to ``None``, in which case
    they are derived (and memoized) via :func:`_engine_lookups`. Pass
    them explicitly (as ``run_bands`` does, computing them once and
    sharing across every commander) to avoid every :class:`ContextSim`
    retaining its own rebuilt copy.
    """
    page = engine.page([commander], offset=0, limit=1_000_000)
    universal = engine._score_cache[(commander,)]
    base_totals = {rec.card: rec.total_score for rec in page.items}
    pool_order = tuple(rec.card for rec in page.items)

    n_rules_map = {name: len(us.distinct_rules) for name, us in universal.items()}

    # Free the engine-side score dict (multi-GB across a many-commander
    # run) now that everything this function needs from it (n_rules_map)
    # has been derived — the portfolio_sim.build_commander_sim precedent
    # (portfolio_sim.py, "Free the engine-side score dict").
    engine._score_cache.clear()

    if cmc_lookup is None or rank_lookup is None:
        cmc_lookup, rank_lookup = _engine_lookups(engine)

    context_max = select_context(pool_order, n_rules_map, k_max)
    ctx_scores = {
        c: context_scores_for_card(engine._conn, c, candidate_cache=engine._candidate_cache) for c in context_max
    }
    legal_pool = frozenset(engine.legal_cards([commander]))

    plausible = _plausible_set(n_rules_map, base_totals)

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
        plausible=plausible,
    )
    check = assemble_cell(sim, ContextCell(0, 0.0))
    if check != sim.base_top_30:
        diff = [(a, b) for a, b in zip(check, sim.base_top_30, strict=False) if a != b][:5]
        raise SelfCheckError(f"{commander}: w=0 assembly diverges from page(); first mismatches: {diff}")
    return sim


# ---------------------------------------------------------------------------
# Metrics: NDCG + gem-rate deltas
# ---------------------------------------------------------------------------


def gem_rate_for_context(
    sim: ContextSim,
    assembled: Sequence[str],
    *,
    k: int = 30,
) -> tuple[float, frozenset[str]] | None:
    """hidden-gem rate for an assembled top-``k`` (None without EDHREC data).

    Mirrors ``portfolio_sim.gem_rate_for_assembly``'s formula against
    :class:`ContextSim`'s own fields: ``(assembled - edhrec_top_30) &
    plausible``, rate = ``len(gems) / k``.
    """
    if sim.edhrec_top_30 is None:
        return None
    gems = frozenset((set(assembled) - sim.edhrec_top_30) & sim.plausible)
    return len(gems) / k, gems


# ---------------------------------------------------------------------------
# Whitelist-equivalence baseline (G4 comparator, plan 2026-07-06-001 Task 4)
# ---------------------------------------------------------------------------


def payoff_subtypes(conn: sqlite3.Connection, commander: str) -> frozenset[str]:
    """The commander's subtype-keyed death-payoff subtypes.

    Re-derives the per-commander subtype the same way
    ``cohorts.subtype_death_payoff`` selects cohort members, so the
    whitelist baseline is exactly "the predicate as a rule". Delegates to
    the shared ``death_payoff`` implementation (the same one the
    ``subtype_supply`` scoring rule uses) instead of re-deriving the
    death-trigger scan locally.
    """
    from ..death_payoff import payoff_subtypes_from_ports
    from ..graph_engine import load_ports_for_set

    return frozenset(payoff_subtypes_from_ports(conn, load_ports_for_set(conn, [commander])))


def whitelist_scores(conn: sqlite3.Connection, commander: str) -> dict[str, float]:
    """Flat 1.0 for every subtype body or subtype-token producer.

    The G4 comparator: a disguised whitelist a rule could hardcode.
    Scaled by the bonus grid (:data:`B_GRID`) at assembly time (same slot
    as ``w_ctx``).

    Subtype BODIES are matched against ``cards.subtypes`` — a
    space-separated token column (see ``importer._split_types``) — with
    exact per-token membership, built as a single subs->names index
    scanning ``cards`` once per call. ``cards.card_types`` holds only
    top-level types (Creature/Artifact/...) and never a subtype, and an
    unanchored SQL ``LIKE`` would substring-match e.g. subtype 'Rat'
    against 'Pirate'; both were bugs in the previous
    ``card_types LIKE '%<subtype>%'`` query.
    """
    subs = payoff_subtypes(conn, commander)
    names: set[str] = set()
    if subs:
        for name, subtypes in conn.execute(
            "SELECT name, subtypes FROM cards WHERE subtypes IS NOT NULL AND subtypes != ''"
        ):
            if subs & set(subtypes.split()):
                names.add(name)
    for s in subs:
        names.update(
            n
            for (n,) in conn.execute(
                "SELECT p.card_name FROM card_ports p "
                "JOIN port_attributes a ON a.port_id = p.id "
                "WHERE a.attr_kind = 'token_subtype' AND a.attr_value = ?",
                (s,),
            )
        )
    names.discard(commander)
    return dict.fromkeys(names, 1.0)


def outlet_whitelist_scores(conn: sqlite3.Connection, commander: str) -> dict[str, float]:
    """Flat 1.0 for every sac-outlet body, gated on the outlet-cohort port shape.

    The G4-style comparator for ``death_outlet_feeder`` (Task 6's rule, not
    yet built) — a disguised whitelist a rule could hardcode. Gate: the
    commander's ports must satisfy ``death_payoff.has_changeszone_death_payoff``
    (a non-self-only ChangesZone/ChangesZoneAll death trigger) AND carry no
    ``Sacrificed``/``SacrificedOnce`` trigger port. The second conjunct is
    NOT part of ``has_changeszone_death_payoff`` itself (its docstring is
    explicit that it "deliberately does NOT check for Sacrificed ...
    triggers") — it is composed here so this gate matches
    ``bench.cohorts.outlet_direction_death_payoff``'s port-level semantics
    exactly (those commanders are already served by the existing
    ``cost_feeds_trigger`` arm). Ungated commanders get ``{}`` (their delta
    is 0 by construction, same convention as :func:`whitelist_scores`).

    Unlike ``whitelist_scores``/``payoff_subtypes``, this does not re-derive
    the cohort predicate's ``subtype_death_payoff``-overlap exclusion — that
    exclusion is a set-level "which cohort claims this commander first"
    tiebreak applied once at cohort-enumeration time (see
    ``outlet_direction_death_payoff``'s docstring), not a property of the
    commander's own ports, and every commander this function is called
    against (the outlet fixture) has already had that tiebreak applied at
    fixture-build time.
    """
    from ..death_payoff import has_changeszone_death_payoff
    from ..graph_engine import load_ports_for_set

    cmdr_ports = load_ports_for_set(conn, [commander])
    if not has_changeszone_death_payoff(cmdr_ports):
        return {}
    for p in cmdr_ports:
        if (p.get("port_type") or "").strip() != "trigger":
            continue
        if (p.get("event_class") or "").strip() in {"Sacrificed", "SacrificedOnce"}:
            return {}

    names = {
        n
        for (n,) in conn.execute(
            "SELECT DISTINCT card_name FROM card_ports WHERE port_type='cost' AND event_class='sacrifice'"
        )
    }
    names.discard(commander)
    return dict.fromkeys(names, 1.0)


def run_whitelist_baseline(
    sims: Sequence[ContextSim],
    conn: sqlite3.Connection,
    *,
    whitelist_fn: Callable[[sqlite3.Connection, str], dict[str, float]] = whitelist_scores,
) -> dict[str, Any]:
    """Whitelist-equivalence baseline (G4 comparator) over :data:`B_GRID`.

    For each graded commander, ``whitelist_fn`` is built once (a
    design-time query re-deriving the cohort predicate), then re-assembled
    per bonus with :func:`assemble_whitelist` — same legal-pool gating and
    metric shape as ``run_sweep``'s cells. Commanders for whom ``whitelist_fn``
    returns ``{}`` get an empty whitelist (their delta is 0 by construction);
    ``n_commanders_with_whitelist`` counts the rest so the readout stays
    honest about how many commanders the comparator actually touches.

    ``whitelist_fn`` defaults to :func:`whitelist_scores` (the subtype-body
    comparator, plan 2026-07-06-001 Task 4) — existing call sites and CLI
    behavior are unchanged. Pass :func:`outlet_whitelist_scores` for the
    outlet-cohort comparator (plan 2026-07-07-002 Task 5).
    """
    graded_sims = [sim for sim in sims if sim.graded_labels]

    base_ndcg: dict[str, float] = {
        sim.commander: compute_ndcg(list(sim.base_top_30), sim.graded_labels) for sim in graded_sims
    }
    base_gem: dict[str, float] = {}
    for sim in graded_sims:
        gem = gem_rate_for_context(sim, sim.base_top_30, k=30)
        if gem is not None:
            base_gem[sim.commander] = gem[0]

    wl_by_commander: dict[str, dict[str, float]] = {}
    n_with_whitelist = 0
    for sim in graded_sims:
        wl = whitelist_fn(conn, sim.commander)
        wl_by_commander[sim.commander] = wl
        if wl:
            n_with_whitelist += 1

    out: list[dict[str, Any]] = []
    for b in B_GRID:
        deltas: list[tuple[str, float]] = []
        cliff = 0
        reach = 0
        gem_deltas: list[float] = []
        for sim in graded_sims:
            top = assemble_whitelist(sim, wl_by_commander[sim.commander], b)
            d = compute_ndcg(list(top), sim.graded_labels) - base_ndcg[sim.commander]
            deltas.append((sim.commander, d))
            if d < -0.05:
                cliff += 1
            reach += len(sim.zero_score_labels & set(top))
            if sim.commander in base_gem:
                gem = gem_rate_for_context(sim, top, k=30)
                if gem is not None:
                    gem_deltas.append(gem[0] - base_gem[sim.commander])
        mean = sum(d for _, d in deltas) / max(len(deltas), 1)
        mean_gem_delta = (sum(gem_deltas) / len(gem_deltas)) if gem_deltas else None
        out.append(
            {
                "bonus": b,
                "mean_ndcg_delta": mean,
                "cliffs": cliff,
                "reach": reach,
                "gem_delta": mean_gem_delta,
                "per_commander": dict(deltas),
            }
        )
    return {"cells": out, "n_commanders_with_whitelist": n_with_whitelist}


# ---------------------------------------------------------------------------
# Subcommand: bands
# ---------------------------------------------------------------------------

#: Grid of context-pool depths (`k_context`) and context-term weights
#: (`w_ctx`) swept by ``run_sweep``'s default cell set.
K_GRID: tuple[int, ...] = (10, 20, 30)
W_GRID: tuple[float, ...] = (0.1, 0.25, 0.5)

#: Flat per-member bonus grid for the whitelist-equivalence baseline (G4
#: comparator, plan 2026-07-06-001 Task 4) — matched to W_GRID's scale.
B_GRID: tuple[float, ...] = (0.1, 0.25, 0.5)


def run_bands(
    engine: SynergyEngine,
    edhrec_conn: sqlite3.Connection,
    commanders: Sequence[str],
    *,
    seed: int = 17,
    k_max: int = 30,
) -> tuple[dict[str, Any], list[ContextSim]]:
    """w=0 baseline distribution -> bootstrap noise bands (pin BEFORE sweep).

    Returns ``(report, sims)`` — the cached :class:`ContextSim` list is
    reused by ``run_sweep`` in the same process so a ``sweep`` CLI
    invocation only scores each commander once.

    Note: the report's ``per_commander_ndcg`` is built by tracking
    ``(commander, ndcg)`` pairs directly in the scoring loop rather than
    zipping ``sims`` against ``ndcgs`` post-hoc — commanders without
    graded EDHREC labels are skipped from ``ndcgs`` but still appear in
    ``sims``, so a positional zip would silently mis-pair names against
    the wrong NDCG values whenever any commander in the batch lacks
    labels.
    """
    ndcgs: list[float] = []
    gem_rates: list[float] = []
    per_commander_ndcg: dict[str, float] = {}
    sims: list[ContextSim] = []
    total = len(commanders)
    # Computed once (on the first commander, since engine._candidate_cache
    # is only populated after the first page() call) and shared by every
    # later build_context_sim call, instead of each ContextSim rebuilding
    # and retaining its own ~2.5GB copy of the same two dicts.
    cmc_lookup: dict[str, float] | None = None
    rank_lookup: dict[str, int] | None = None
    for i, commander in enumerate(commanders, start=1):
        sim = build_context_sim(
            engine, edhrec_conn, commander, k_max=k_max, cmc_lookup=cmc_lookup, rank_lookup=rank_lookup
        )
        if cmc_lookup is None:
            cmc_lookup, rank_lookup = sim.cmc_lookup, sim.rank_lookup
        sims.append(sim)
        print(
            f"[{i}/{total}] {commander}: pool={len(sim.pool_order)} (w=0 self-check OK)",
            file=sys.stderr,
        )
        if sim.graded_labels:
            ndcg = compute_ndcg(list(sim.base_top_30), sim.graded_labels)
            ndcgs.append(ndcg)
            per_commander_ndcg[sim.commander] = ndcg
            gem = gem_rate_for_context(sim, sim.base_top_30, k=30)
            if gem is not None:
                gem_rates.append(gem[0])
    return {
        "n_commanders": len(sims),
        "ndcg_band": bootstrap_band(ndcgs, seed=seed),
        "gem_band": bootstrap_band(gem_rates, seed=seed + 1),
        "per_commander_ndcg": per_commander_ndcg,
    }, sims


# ---------------------------------------------------------------------------
# Subcommand: sweep
# ---------------------------------------------------------------------------


def run_sweep(
    sims: Sequence[ContextSim],
    *,
    cells: Sequence[ContextCell] | None = None,
) -> dict[str, Any]:
    """Re-assemble every cell from cached sims; per-cell metrics + traps.

    Commanders with no graded EDHREC labels are skipped from every
    per-cell aggregate (ndcg delta, cliffs, reach, gem delta) — mirrors
    ``run_bands``'s same skip.
    """
    cells = list(cells) if cells else [ContextCell(k, w) for k in K_GRID for w in W_GRID]
    graded_sims = [sim for sim in sims if sim.graded_labels]

    base_ndcg: dict[str, float] = {
        sim.commander: compute_ndcg(list(sim.base_top_30), sim.graded_labels) for sim in graded_sims
    }
    base_gem: dict[str, float] = {}
    for sim in graded_sims:
        gem = gem_rate_for_context(sim, sim.base_top_30, k=30)
        if gem is not None:
            base_gem[sim.commander] = gem[0]

    out: list[dict[str, Any]] = []
    for cell in cells:
        deltas: list[tuple[str, float]] = []
        cliff = 0
        reach = 0
        gem_deltas: list[float] = []
        for sim in graded_sims:
            top = assemble_cell(sim, cell)
            d = compute_ndcg(list(top), sim.graded_labels) - base_ndcg[sim.commander]
            deltas.append((sim.commander, d))
            if d < -0.05:
                cliff += 1
            reach += len(sim.zero_score_labels & set(top))
            if sim.commander in base_gem:
                gem = gem_rate_for_context(sim, top, k=30)
                if gem is not None:
                    gem_deltas.append(gem[0] - base_gem[sim.commander])
        mean = sum(d for _, d in deltas) / max(len(deltas), 1)
        mean_gem_delta = (sum(gem_deltas) / len(gem_deltas)) if gem_deltas else None
        out.append(
            {
                "k_context": cell.k_context,
                "w_ctx": cell.w_ctx,
                "mean_ndcg_delta": mean,
                "cliffs": cliff,
                "reach": reach,
                "mean_gem_delta": mean_gem_delta,
                "per_commander": dict(deltas),
            }
        )
    return {"cells": out}


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------


def _render_gates_markdown(
    report: dict[str, Any],
    *,
    h_cohort: float | None,
    h_500: float | None,
    g_500: float | None,
) -> str:
    """Rendered comparison only — no routing/decision logic here.

    G1: mean_ndcg_delta > h_cohort (cohort-fixture noise band).
    G2: mean_ndcg_delta > -h_500 AND zero cliffs AND mean_gem_delta >= -g_500
        (500-cmdr no-regression band on both axes). ``mean_gem_delta is
        None`` (no gem evidence for this cell) renders 'n/a', not an
        unqualified 'Y' — a genuine core/gem failure still renders 'n'
        even when gem evidence is missing.
    G3: reach >= 100 (NO_RULES-population reachability floor).
    """
    lines = [
        "## Decision gates",
        "",
        f"pinned: h_cohort={h_cohort}  h_500={h_500}  g_500={g_500}",
        "",
        f"{'k':>4} {'w':>6} {'G1':>4} {'G2':>4} {'G3':>4}",
        f"{'-' * 4} {'-' * 6} {'-' * 4} {'-' * 4} {'-' * 4}",
    ]
    for cell in report["cells"]:
        g1 = "-" if h_cohort is None else ("Y" if cell["mean_ndcg_delta"] > h_cohort else "n")
        if h_500 is None or g_500 is None:
            g2 = "-"
        else:
            gem = cell["mean_gem_delta"]
            gem_known = gem is not None
            gem_ok = gem_known and gem >= -g_500
            core_ok = cell["mean_ndcg_delta"] > -h_500 and cell["cliffs"] == 0
            if not core_ok or (gem_known and not gem_ok):
                g2 = "n"
            elif not gem_known:
                g2 = "n/a"
            else:
                g2 = "Y"
        g3 = "Y" if cell["reach"] >= 100 else "n"
        lines.append(f"{cell['k_context']:>4} {cell['w_ctx']:>6.2f} {g1:>4} {g2:>4} {g3:>4}")
    lines += [
        "",
        "G2 'n/a' = mean_gem_delta is None (no gem evidence this cell) but core "
        "(ndcg/cliffs) checks passed -- would otherwise be an unqualified pass.",
    ]
    return "\n".join(lines) + "\n"


def _render_sweep_markdown(report: dict[str, Any], *, gates: str | None = None) -> str:
    lines = [
        "# context_sim sweep",
        "",
        f"fixture: {report.get('fixture', '?')}  |  commanders: {report.get('n_commanders', '?')}",
        "",
        f"{'k':>4} {'w':>6} {'mean_ndcg_delta':>16} {'cliffs':>7} {'reach':>7} {'mean_gem_delta':>15}",
        f"{'-' * 4} {'-' * 6} {'-' * 16} {'-' * 7} {'-' * 7} {'-' * 15}",
    ]
    for cell in report["cells"]:
        gem = cell["mean_gem_delta"]
        gem_str = f"{gem:+.4f}" if gem is not None else "-"
        lines.append(
            f"{cell['k_context']:>4} {cell['w_ctx']:>6.2f} {cell['mean_ndcg_delta']:>+16.4f} "
            f"{cell['cliffs']:>7} {cell['reach']:>7} {gem_str:>15}"
        )
    if gates is not None:
        lines += ["", gates]
    return "\n".join(lines) + "\n"


def _render_whitelist_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# context_sim whitelist baseline (G4 comparator)",
        "",
        f"fixture: {report.get('fixture', '?')}  |  commanders: {report.get('n_commanders', '?')}  |  "
        f"with whitelist: {report.get('n_commanders_with_whitelist', '?')}",
        "",
        f"{'bonus':>6} {'mean_ndcg_delta':>16} {'cliffs':>7} {'reach':>7} {'gem_delta':>10}",
        f"{'-' * 6} {'-' * 16} {'-' * 7} {'-' * 7} {'-' * 10}",
    ]
    for cell in report["cells"]:
        gem = cell["gem_delta"]
        gem_str = f"{gem:+.4f}" if gem is not None else "-"
        lines.append(
            f"{cell['bonus']:>6.2f} {cell['mean_ndcg_delta']:>+16.4f} "
            f"{cell['cliffs']:>7} {cell['reach']:>7} {gem_str:>10}"
        )
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

_DEFAULT_FIXTURE = "tests/fixtures/golden_set_archetype_payoff.json"
_DEFAULT_OUTPUT_DIR = ".audit/context_sim"


class ContextSimError(RuntimeError):
    """Base error for the context_sim CLI (bad --cells syntax, etc.)."""


def _parse_cells(raw: str) -> list[ContextCell]:
    """Parse ``"K,W;K,W"`` into a :class:`ContextCell` list."""

    def make_cell(part: str) -> ContextCell:
        k_str, w_str = part.split(",")
        return ContextCell(int(k_str.strip()), float(w_str.strip()))

    return parse_grid_cells(raw, make_cell=make_cell, expected="K,W", error_cls=ContextSimError)


def _add_common_args(p: argparse.ArgumentParser) -> None:
    add_common_sim_args(
        p,
        default_fixture=_DEFAULT_FIXTURE,
        default_output_dir=_DEFAULT_OUTPUT_DIR,
        include_k_max=True,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="context_sim.py",
        description="Deck-context two-pass kill-test instrument (plan 2026-07-06-001)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_bands = sub.add_parser("bands", help="w=0 baseline distribution -> bootstrap noise bands")
    _add_common_args(p_bands)

    p_sweep = sub.add_parser("sweep", help="K_GRID x W_GRID cell sweep (or --cells) over the cached sims")
    _add_common_args(p_sweep)
    p_sweep.add_argument(
        "--cells",
        default=None,
        help='semicolon-separated "k_context,w_ctx" pairs, e.g. "10,0.1;20,0.25" (default: full K_GRID x W_GRID)',
    )
    p_sweep.add_argument(
        "--whitelist-baseline",
        action="store_true",
        help="run the whitelist-equivalence baseline (G4 comparator) over B_GRID instead of the K_GRID x W_GRID sweep",
    )
    p_sweep.add_argument(
        "--whitelist-baseline-outlet",
        action="store_true",
        help=(
            "like --whitelist-baseline, but uses outlet_whitelist_scores (the "
            "outlet-cohort G4 comparator, plan 2026-07-07-002 Task 5) instead of "
            "the subtype-body whitelist_scores comparator"
        ),
    )
    p_sweep.add_argument("--h-cohort", type=float, default=None, help="pinned cohort NDCG band half-width (gate G1)")
    p_sweep.add_argument("--h-500", type=float, default=None, help="pinned 500-cmdr NDCG band half-width (gate G2)")
    p_sweep.add_argument("--g-500", type=float, default=None, help="pinned 500-cmdr gem band half-width (gate G2)")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    db_path = Path(args.db)
    edhrec_path = Path(args.edhrec_db)
    fixture_path = Path(args.fixture)
    for path, what in ((db_path, "synergy DB"), (edhrec_path, "EDHREC DB"), (fixture_path, "fixture")):
        if not path.exists():
            print(f"error: {what} {path} not found", file=sys.stderr)
            return 2

    try:
        commanders = load_fixture_commanders(fixture_path)
    except PortfolioSimError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.limit_commanders is not None:
        commanders = commanders[: args.limit_commanders]

    # Local import: engine pulls the scoring stack; keep module import light.
    from ..engine import SynergyEngine

    outdir = Path(args.output_dir)
    edhrec_conn = sqlite3.connect(edhrec_path)
    edhrec_conn.row_factory = sqlite3.Row
    engine = SynergyEngine(db_path)
    started = time.perf_counter()
    try:
        report, sims = run_bands(engine, edhrec_conn, commanders, seed=args.seed, k_max=args.k_max)
        report["fixture"] = str(fixture_path)
        bands_path = _write_report(outdir, "bands.json", report)
        write_text(outdir, "bands.md", render_bands_markdown(report, title="context_sim bands"))
        nb, gb = report["ndcg_band"], report["gem_band"]
        print(f"bands: n={report['n_commanders']} seed={args.seed}")
        print(
            f"  NDCG@30  mean={nb['mean']:.4f}  95% CI [{nb['ci95_low']:.4f}, {nb['ci95_high']:.4f}]  "
            f"noise band (half-width) = {nb['half_width']:.4f}"
        )
        print(
            f"  gem rate mean={gb['mean']:.4f}  95% CI [{gb['ci95_low']:.4f}, {gb['ci95_high']:.4f}]  "
            f"non-regression band (half-width) = {gb['half_width']:.4f}"
        )
        print(f"report: {bands_path}")

        if args.command == "sweep" and (args.whitelist_baseline or args.whitelist_baseline_outlet):
            wl_fn = outlet_whitelist_scores if args.whitelist_baseline_outlet else whitelist_scores
            wl_report = run_whitelist_baseline(sims, engine._conn, whitelist_fn=wl_fn)
            wl_report["fixture"] = str(fixture_path)
            wl_report["n_commanders"] = report["n_commanders"]
            wl_path = _write_report(outdir, "whitelist.json", wl_report)
            wl_md = _render_whitelist_markdown(wl_report)
            wl_md_path = write_text(outdir, "whitelist.md", wl_md)
            print(wl_md)
            print(f"reports: {wl_path}, {wl_md_path}")
        elif args.command == "sweep":
            cells = _parse_cells(args.cells) if args.cells else None
            sweep_report = run_sweep(sims, cells=cells)
            sweep_report["fixture"] = str(fixture_path)
            sweep_report["n_commanders"] = report["n_commanders"]
            gates_md = None
            if args.h_cohort is not None or args.h_500 is not None or args.g_500 is not None:
                gates_md = _render_gates_markdown(
                    sweep_report, h_cohort=args.h_cohort, h_500=args.h_500, g_500=args.g_500
                )
            sweep_path = _write_report(outdir, "sweep.json", sweep_report)
            md = _render_sweep_markdown(sweep_report, gates=gates_md)
            md_path = write_text(outdir, "sweep.md", md)
            print(md)
            print(f"reports: {sweep_path}, {md_path}")
    except SelfCheckError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3
    except (PortfolioSimError, ContextSimError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        engine.close()
        edhrec_conn.close()

    print(f"total wall clock: {time.perf_counter() - started:.1f}s", file=sys.stderr)
    return 0
