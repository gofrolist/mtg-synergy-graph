"""Universal port-complement scorer.

Scores every candidate by counting distinct mechanical interactions with
the commander's ports, weighted by specificity (IDF).  No hand-tuned
weights — specificity is derived from the data: a match that only 3
candidates satisfy is worth more than one 2000 candidates satisfy.
"""

from __future__ import annotations

import math
import sqlite3
import types
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from functools import cached_property

from .complement_rules import (
    PortComplement,
    find_all_complements,
)
from .heuristics import STAPLES
from .scoring import BUCKETS


# ---------------------------------------------------------------------------
# §1  UniversalScore — per-candidate result
# ---------------------------------------------------------------------------

# Map rule_id → legacy bucket name for backward compatibility
_RULE_TO_BUCKET: dict[str, str] = {
    "trigger_effect":        "port_match",
    "cost_feeds_trigger":    "cost_synergy",
    "trigger_resonance":     "trigger_resonance",
    "effect_resonance":      "effect_resonance",
    "replacement_resonance": "replacement_resonance",
    "replacement_producer":  "replacement_producer",
    "replacement_blocks":    "replacement",
    "lord":                  "lord",
    "scaling":               "scaling",
    "etb_self":              "port_match",
    "spell_density":         "spellcast_density",
    "tribal_density":        "catchall",
    "sacrifice_cluster":     "sacrifice_synergy",
    "zone_resonance":        "trigger_resonance",
    "effect_feeds_trigger":  "port_match",
    "panharmonicon":         "port_match",
    "flicker_synergy":       "port_match",
    "cost_payoff":           "port_match",
    "opponent_forcing":      "opponent_forcing",
    "token_producer":        "port_match",
    "voltron":               "scaling",
    "etb_sac_target":        "etb_value",
    "combat_enhancer":       "port_match",
    "wheel_synergy":         "port_match",
    "artifact_recursion":    "graveyard_synergy",
    "copy_synergy":          "port_match",
    "token_sac_chain":       "sacrifice_synergy",
    "evasion":               "port_match",
}


@dataclass
class UniversalScore:
    """Scoring result for one candidate under the universal port matcher.

    Score is IDF-weighted: each complement's value is ``1/log2(1+N)``
    where N is the number of candidates that match the same
    ``(rule_id, cmdr_event, cand_event)`` tuple.  Specific matches
    (Saproling lord: N=3, IDF=0.50) contribute more than broad matches
    (sacrifice cost: N=2000, IDF=0.09).

    No hand-tuned weights — specificity is derived from the data.
    """

    complements: list[PortComplement] = field(default_factory=list)
    staple_bonus: float = 0.0
    # IDF weights per (rule_id, cmdr_event, cand_event) — injected by scorer
    idf_weights: dict[tuple[str, str, str], float] = field(default_factory=dict)

    @cached_property
    def score(self) -> float:
        """IDF-weighted synergy score minus anti-synergy."""
        syn = 0.0
        anti = 0.0
        seen_syn: set[tuple[str, str, str]] = set()
        seen_anti: set[tuple[str, str, str]] = set()
        for c in self.complements:
            key = (c.rule_id, c.cmdr_event, c.cand_event)
            if c.direction == "synergy":
                if key not in seen_syn:
                    seen_syn.add(key)
                    syn += self.idf_weights.get(key, 1.0)
            else:
                if key not in seen_anti:
                    seen_anti.add(key)
                    anti += self.idf_weights.get(key, 1.0)
        base = syn - anti + self.staple_bonus
        # Multi-rule bonus: cards matching 3+ distinct rules are
        # genuinely better synergy picks (Pitiless Plunderer matches
        # sacrifice_cluster + trigger_effect + effect_feeds_trigger).
        n_rules = len(self.distinct_rules)
        if n_rules >= 3:
            base += 0.05 * min(n_rules - 2, 3)
        return base

    @cached_property
    def distinct_rules(self) -> frozenset[str]:
        return frozenset(
            c.rule_id for c in self.complements if c.direction == "synergy"
        )

    def to_legacy_buckets(self) -> dict[str, float]:
        """Map IDF-weighted scores to legacy bucket dict."""
        buckets: dict[str, float] = dict.fromkeys(BUCKETS, 0.0)
        buckets["total"] = 0.0
        seen: set[tuple[str, str, str, str]] = set()
        for c in self.complements:
            key = (c.rule_id, c.cmdr_event, c.cand_event, c.direction)
            if key in seen:
                continue
            seen.add(key)
            bucket = _RULE_TO_BUCKET.get(c.rule_id, "catchall")
            idf_key = (c.rule_id, c.cmdr_event, c.cand_event)
            weight = self.idf_weights.get(idf_key, 1.0)
            if c.direction == "anti_synergy":
                buckets[bucket] -= weight
            else:
                buckets[bucket] += weight
        if self.staple_bonus:
            buckets["staple"] = self.staple_bonus
        buckets["total"] = sum(buckets[b] for b in BUCKETS)
        return buckets


# ---------------------------------------------------------------------------
# §2  Bulk scorer
# ---------------------------------------------------------------------------


#: Rules where every match counts equally (density rules — the strategy
#: IS having many of this type). IDF would penalize these unfairly.
_FLAT_COUNT_RULES: frozenset[str] = frozenset({
    "spell_density",
    "scaling",
    "etb_self",
    "tribal_density",
    "token_producer",
    "evasion",
})

#: Per-rule flat weight overrides. Tribal density uses a lower weight
#: (0.5) because random tribal creatures (Bog Rats, Robber Fly) at 1.0
#: drown out actual synergy cards. At 0.5, a tribal creature scores
#: 0.5 while a lord scores 0.5 + lord_IDF ≈ 0.74 — proper discrimination.
_FLAT_WEIGHT_OVERRIDES: types.MappingProxyType[str, float] = types.MappingProxyType({
    "tribal_density": 0.5,
    "token_producer": 0.25,
    "evasion": 0.15,
})


def _computeidf_weights(
    complements: list[PortComplement],
) -> dict[tuple[str, str, str], float]:
    """Compute IDF weights: ``1 / log2(1 + N)`` where N is how many
    distinct candidates match each ``(rule_id, cmdr_event, cand_event)``
    tuple.

    Specific matches (Saproling lord: N=3) get IDF ~0.50.
    Broad matches (sacrifice cost: N=2000) get IDF ~0.09.

    Density rules (spell_density, scaling) use flat weight of 1.0 per
    match — for these rules, matching many candidates IS the strategy
    (Talrand wants EVERY instant), so IDF would incorrectly penalize.
    """
    freq: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    for c in complements:
        key = (c.rule_id, c.cmdr_event, c.cand_event)
        freq[key].add(c.candidate)
    result: dict[tuple[str, str, str], float] = {}
    for key, candidates in freq.items():
        rule_id = key[0]
        if rule_id in _FLAT_COUNT_RULES:
            result[key] = _FLAT_WEIGHT_OVERRIDES.get(rule_id, 1.0)
        else:
            result[key] = 1.0 / math.log2(1.0 + len(candidates))
    return result


def score_all_universal(
    conn: sqlite3.Connection,
    commander_set: Sequence[str],
) -> dict[str, UniversalScore]:
    """Score every candidate via IDF-weighted port matching.

    Each complement's value is ``1/log2(1+N)`` where N is the number of
    candidates sharing the same match tuple. Specific matches (few
    candidates) are worth more than broad matches (many candidates).

    Returns one ``UniversalScore`` per candidate reached by at least one
    complement rule.
    """
    complements = find_all_complements(conn, commander_set)
    idf = _computeidf_weights(complements)

    # Group complements by candidate
    by_candidate: dict[str, list[PortComplement]] = defaultdict(list)
    for c in complements:
        by_candidate[c.candidate].append(c)

    # Compute staple bonuses
    cmdr_row = conn.execute(
        "SELECT color_identity FROM cards WHERE name = ?",
        (commander_set[0],),
    ).fetchone()
    cmdr_pips: set[str] = set()
    if cmdr_row:
        cmdr_pips = {
            t.strip() for t in (cmdr_row["color_identity"] or "").split(",")
            if t.strip()
        }
    staple_names: set[str] = set()
    for pip in cmdr_pips | {"C"}:
        staple_names.update(STAPLES.get(pip, ()))

    # Build results with IDF and quality dampener injected
    results: dict[str, UniversalScore] = {}
    for name, comps in by_candidate.items():
        bonus = 0.01 if name in staple_names else 0.0
        results[name] = UniversalScore(
            complements=comps, staple_bonus=bonus, idf_weights=idf,
        )
    for name in staple_names:
        if name not in results and name not in set(commander_set):
            results[name] = UniversalScore(staple_bonus=0.01, idf_weights=idf)

    return results
