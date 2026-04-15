"""Universal port-complement scorer.

Scores every candidate by counting distinct mechanical interactions with
the commander's ports, weighted by specificity (IDF).  No hand-tuned
weights — specificity is derived from the data: a match that only 3
candidates satisfy is worth more than one 2000 candidates satisfy.
"""

from __future__ import annotations

import math
import sqlite3
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
    "trigger_effect": "port_match",
    "cost_feeds_trigger": "cost_synergy",
    "trigger_resonance": "trigger_resonance",
    "effect_resonance": "effect_resonance",
    "replacement_resonance": "replacement_resonance",
    "replacement_producer": "replacement_producer",
    "replacement_blocks": "replacement",
    "lord": "lord",
    "scaling": "scaling",
    "etb_self": "port_match",
    "spell_density": "spellcast_density",
    "tribal_density": "catchall",
    "sacrifice_cluster": "sacrifice_synergy",
    "zone_resonance": "trigger_resonance",
    "effect_feeds_trigger": "port_match",
    "panharmonicon": "port_match",
    "flicker_synergy": "port_match",
    "cost_payoff": "port_match",
    "opponent_forcing": "opponent_forcing",
    "token_producer": "port_match",
    "voltron": "scaling",
    "etb_sac_target": "etb_value",
    "combat_enhancer": "port_match",
    "wheel_synergy": "port_match",
    "artifact_recursion": "graveyard_synergy",
    "copy_synergy": "port_match",
    "token_sac_chain": "sacrifice_synergy",
    "evasion": "port_match",
    "spellcast_resonance": "spellcast_density",
    "untap_synergy": "port_match",
    "cost_reducer": "spellcast_density",
    "graveyard_play": "port_match",
    "edict_feeder": "sacrifice_synergy",
    "counter_doubler": "counter_synergy",
    "counter_keyword": "counter_synergy",
    "damage_synergy": "port_match",
    "mana_doubler": "port_match",
    "power_matters": "scaling",  # IDF-weighted (not in _FLAT_COUNT_RULES); bucket is display-only
    "landfall_enabler": "port_match",
    "proliferate_synergy": "counter_synergy",
    "value_engine": "spellcast_density",
}

# ---------------------------------------------------------------------------
# §1b  Circuit detection — rule direction classification
# ---------------------------------------------------------------------------

#: Rules where the candidate feeds the commander (candidate → commander).
_FEEDS_COMMANDER_RULES: frozenset[str] = frozenset(
    {
        "cost_feeds_trigger",
        "trigger_effect",
        "sacrifice_cluster",
        "token_sac_chain",
    }
)

#: Rules where the commander actively feeds the candidate (commander → candidate).
#: Excludes broad identity matches (etb_self, zone_resonance) — only rules
#: where the commander's effect directly enables the candidate.
_FED_BY_COMMANDER_RULES: frozenset[str] = frozenset(
    {
        "effect_feeds_trigger",
        "etb_sac_target",
    }
)


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
    # IDF weights per (rule_id, cmdr_event, cand_event, filter_group) — injected by scorer
    idf_weights: dict[tuple[str, str, str, str], float] = field(default_factory=dict)
    circuit_bonus: float = 0.0
    cmc_bonus: float = 0.0
    rank_bonus: float = 0.0

    @cached_property
    def score(self) -> float:
        """IDF-weighted synergy score minus anti-synergy.

        Applies signal concentration dampening: when >70% of a card's
        synergy score comes from a single rule, the score is reduced.
        This prevents broad density axes (etb_self N=17k) from creating
        a high baseline that drowns out differences on secondary axes.
        """
        syn = 0.0
        anti = 0.0
        syn_by_rule: dict[str, float] = {}
        seen_syn: set[tuple[str, str, str, str]] = set()
        seen_anti: set[tuple[str, str, str, str]] = set()
        for c in self.complements:
            key = (c.rule_id, c.cmdr_event, c.cand_event, c.filter_group)
            if c.direction == "synergy":
                if key not in seen_syn:
                    seen_syn.add(key)
                    w = self.idf_weights.get(key, 1.0)
                    syn += w
                    syn_by_rule[c.rule_id] = syn_by_rule.get(c.rule_id, 0.0) + w
            else:
                if key not in seen_anti:
                    seen_anti.add(key)
                    anti += self.idf_weights.get(key, 1.0)

        # Signal concentration dampening: if one rule dominates >70% of
        # the synergy score AND the card matches 2+ distinct keys, compress
        # the total. This prevents broad density axes (etb_self N=17k) from
        # creating a uniform baseline that drowns real score differences.
        # Cards with only 1 complement key are not penalized — single-axis
        # matches are genuine (e.g., a specific lord with one match axis).
        if syn > 0 and len(syn_by_rule) >= 2:
            max_rule_frac = max(syn_by_rule.values()) / syn
            if max_rule_frac > 0.7:
                # Reduce by up to 30% based on how concentrated the signal is.
                # At 70% concentration: no penalty. At 100%: -30%.
                penalty = 0.3 * (max_rule_frac - 0.7) / 0.3
                syn *= 1.0 - penalty

        base = syn - anti + self.staple_bonus
        # Multi-rule bonus: cards matching 3+ distinct rules are
        # genuinely better synergy picks (Pitiless Plunderer matches
        # sacrifice_cluster + trigger_effect + effect_feeds_trigger).
        n_rules = len(self.distinct_rules)
        if n_rules >= 2:
            base += 0.02 * min(n_rules - 1, 4)
        base += self.circuit_bonus + self.cmc_bonus + self.rank_bonus
        return base

    @cached_property
    def distinct_rules(self) -> frozenset[str]:
        return frozenset(c.rule_id for c in self.complements if c.direction == "synergy")

    def to_legacy_buckets(self) -> dict[str, float]:
        """Map IDF-weighted scores to legacy bucket dict."""
        buckets: dict[str, float] = dict.fromkeys(BUCKETS, 0.0)
        buckets["total"] = 0.0
        seen: set[tuple[str, str, str, str, str]] = set()
        for c in self.complements:
            key = (c.rule_id, c.cmdr_event, c.cand_event, c.filter_group, c.direction)
            if key in seen:
                continue
            seen.add(key)
            bucket = _RULE_TO_BUCKET.get(c.rule_id, "catchall")
            idf_key = (c.rule_id, c.cmdr_event, c.cand_event, c.filter_group)
            weight = self.idf_weights.get(idf_key, 1.0)
            if c.direction == "anti_synergy":
                buckets[bucket] -= weight
            else:
                buckets[bucket] += weight
        if self.staple_bonus:
            buckets["staple"] = self.staple_bonus
        buckets["total"] = sum(buckets[b] for b in BUCKETS)
        n_rules = len(self.distinct_rules)
        if n_rules >= 2:
            buckets["total"] += 0.02 * min(n_rules - 1, 4)
        buckets["total"] += self.circuit_bonus + self.cmc_bonus + self.rank_bonus
        return buckets


# ---------------------------------------------------------------------------
# §2  Bulk scorer
# ---------------------------------------------------------------------------


#: Rules where every match counts equally (density rules — the strategy
#: IS having many of this type). IDF would penalize these unfairly.
_FLAT_COUNT_RULES: frozenset[str] = frozenset(
    {
        "spell_density",
        "scaling",
        "etb_self",
        "tribal_density",
        "token_producer",
        "evasion",
    }
)

#: Per-rule flat weight overrides. Density rules use flat weights
#: because matching many candidates IS the strategy (Talrand wants
#: every instant). But weights must stay comparable to IDF-weighted
#: rules (median IDF ≈ 0.09–0.15) so density doesn't drown synergy.
#:
#: etb_self uses size-based dampening (see _compute_idf_weights) rather
#: than a flat override: large Creature groups (N=17k) are dampened so
#: synergy rules can compete, while small groups (Land N=1.1k) keep
#: full weight of 1.0.
_FLAT_WEIGHT_OVERRIDES: dict[str, float] = {
    "tribal_density": 0.5,
    "token_producer": 0.25,
    "evasion": 0.15,
}

#: Quality multiplier applied to IDF weights based on the mechanical
#: nature of the interaction. Broad effect-only matches (Draw,
#: DealDamage) are dampened — they're staple utility rather than synergy.
#: Quality multiplier for IDF weights. Dampens broad effect-only
#: rules and enabler-on-enabler trigger-trigger matches.
_RULE_QUALITY_MULTIPLIER: dict[str, float] = {
    "damage_synergy": 0.5,
    # Trigger-trigger resonance is weaker than trigger-effect: finding
    # another card that triggers on the same event (enabler for enabler)
    # is less valuable than finding a payoff that feeds the trigger.
    "trigger_resonance": 0.7,
    # Value engine matches many cards of a type (Angel, Artifact, etc.).
    # IDF already dampens based on N, but the signal is inherently weaker
    # than mechanical synergy — "be the right type" is a density signal.
    "value_engine": 0.5,
}


def _compute_idf_weights(
    complements: list[PortComplement],
) -> dict[tuple[str, str, str, str], float]:
    """Compute IDF weights: ``1 / log2(1 + N)`` where N is how many
    distinct candidates match each
    ``(rule_id, cmdr_event, cand_event, filter_group)`` tuple.

    The ``filter_group`` dimension segments broad matches by the
    commander's valid_filter context: a commander triggering on
    ``Creature.Goblin.YouCtrl`` computes IDF against only
    Goblin-producing candidates (N≈3, IDF≈0.50), not all creatures
    (N≈2000, IDF≈0.09).

    Density rules (spell_density, scaling) use flat weight per
    match — for these rules, matching many candidates IS the strategy
    (Talrand wants EVERY instant), so IDF would incorrectly penalize.
    """
    freq: dict[tuple[str, str, str, str], set[str]] = defaultdict(set)
    for c in complements:
        key = (c.rule_id, c.cmdr_event, c.cand_event, c.filter_group)
        freq[key].add(c.candidate)

    result: dict[tuple[str, str, str, str], float] = {}
    for key, candidates in freq.items():
        rule_id = key[0]
        if rule_id in _FLAT_COUNT_RULES:
            override = _FLAT_WEIGHT_OVERRIDES.get(rule_id)
            if override is not None:
                result[key] = override
            elif rule_id == "etb_self":
                # etb_self matches every card of a type against the
                # commander's ChangesZone filter. For Creature triggers
                # (N=17k) this drowns out actual synergy picks. Dampen
                # large groups so synergy rules can compete, while
                # small groups (Land N=1.1k) keep full weight.
                n = len(candidates)
                result[key] = 1.0 / math.log2(max(n / 1000, 2.0)) if n > 2000 else 1.0
            else:
                result[key] = 1.0
        else:
            n = len(candidates)
            # For forward panharmonicon matches, apply minimum N=10 floor.
            # Very rare effects (SacrificeAll N=1) get inflated IDF that
            # doesn't reflect true synergy quality — Krovikan Vampire at
            # IDF=1.0 shouldn't outrank Zulaport Cutthroat at IDF=0.17.
            # Exclude reverse_panharmonicon and panharmonicon_stack which
            # have genuinely unique high-value matches (Harmonic Prodigy).
            cmdr_event = key[1]
            if rule_id == "panharmonicon" and n < 10 and "reverse" not in cmdr_event and "stack" not in cmdr_event:
                n = 10
            w = 1.0 / math.log2(1.0 + n)
            # Apply quality multiplier: cost-based rules are boosted,
            # broad effect rules are dampened.
            mult = _RULE_QUALITY_MULTIPLIER.get(rule_id, 1.0)
            result[key] = w * mult
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
    idf = _compute_idf_weights(complements)

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
        cmdr_pips = {t.strip() for t in (cmdr_row["color_identity"] or "").split(",") if t.strip()}
    staple_names: set[str] = set()
    for pip in cmdr_pips | {"C"}:
        staple_names.update(STAPLES.get(pip, ()))

    # Detect circuit candidates (match rules from both directions)
    circuit_candidates: set[str] = set()
    for name, comps in by_candidate.items():
        rules_hit = {c.rule_id for c in comps if c.direction == "synergy"}
        if (rules_hit & _FEEDS_COMMANDER_RULES) and (rules_hit & _FED_BY_COMMANDER_RULES):
            circuit_candidates.add(name)

    # Bulk-load CMC and edhrec_rank for micro-scores
    cmc_data: dict[str, float] = {}
    rank_data: dict[str, int] = {}
    for row in conn.execute("SELECT name, cmc, edhrec_rank FROM cards"):
        cmc_data[row["name"]] = row["cmc"] if row["cmc"] is not None else 99.0
        rank_data[row["name"]] = row["edhrec_rank"] if row["edhrec_rank"] is not None else 99999

    # Build results with IDF and quality dampener injected
    results: dict[str, UniversalScore] = {}
    for name, comps in by_candidate.items():
        bonus = 0.01 if name in staple_names else 0.0
        cmc = cmc_data.get(name, 99.0)
        rank = rank_data.get(name, 99999)
        results[name] = UniversalScore(
            complements=comps,
            staple_bonus=bonus,
            idf_weights=idf,
            circuit_bonus=0.05 if name in circuit_candidates else 0.0,
            cmc_bonus=0.01 * max(0.0, (7.0 - cmc)) / 7.0,
            rank_bonus=0.005 * max(0.0, 1.0 - rank / 30000.0),
        )
    for name in staple_names:
        if name not in results and name not in set(commander_set):
            cmc = cmc_data.get(name, 99.0)
            rank = rank_data.get(name, 99999)
            results[name] = UniversalScore(
                staple_bonus=0.01,
                idf_weights=idf,
                cmc_bonus=0.01 * max(0.0, (7.0 - cmc)) / 7.0,
                rank_bonus=0.005 * max(0.0, 1.0 - rank / 30000.0),
            )

    return results
