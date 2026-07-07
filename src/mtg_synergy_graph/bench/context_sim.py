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
