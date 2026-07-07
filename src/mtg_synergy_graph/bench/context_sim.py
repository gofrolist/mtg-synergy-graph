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
from dataclasses import dataclass

from ..complement_rules import PortComplement, find_all_complements
from ..universal_scorer import _compute_idf_weights
from .optimize import load_edhrec_labels
from .portfolio_sim import SelfCheckError


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


_UNRANKED = 10**9


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
        c: context_scores_for_card(engine._conn, c, candidate_cache=engine._candidate_cache) for c in context_max
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
