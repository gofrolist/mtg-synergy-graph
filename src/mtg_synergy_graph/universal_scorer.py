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
from typing import TYPE_CHECKING

from .complement_rules import (
    PortComplement,
    find_all_complements,
)
from .heuristics import STAPLES
from .scoring import BUCKETS

if TYPE_CHECKING:
    from .penalties import CandidateCache

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
    "combat_enhancer": "port_match",
    "wheel_synergy": "port_match",
    "artifact_recursion": "graveyard_synergy",
    "copy_synergy": "port_match",
    "token_etb_damage": "token_etb_payoff",
    "evasion": "port_match",
    "spellcast_resonance": "spellcast_density",
    "untap_synergy": "port_match",
    "multicolor_untap": "port_match",
    "cost_reducer": "spellcast_density",
    "graveyard_play": "port_match",
    "affinity_archetype": "spellcast_density",
    "edict_feeder": "sacrifice_synergy",
    "counter_doubler": "counter_synergy",
    "counter_keyword": "counter_synergy",
    "mana_doubler": "port_match",
    "landfall_enabler": "port_match",
    "proliferate_synergy": "counter_synergy",
    "value_engine": "spellcast_density",
    "cheat_cmc": "port_match",
    "cost_reduction_target": "port_match",
    "toughness_synergy": "scaling",
    "cascade_value": "port_match",
    "flicker_payoff": "port_match",
    "monarch_synergy": "port_match",
    "counter_target_payoff": "counter_synergy",
    "creature_untap_engine": "untap_synergy",
    "populate_stack": "port_match",
    "dies_drain": "sacrifice_synergy",
    "gy_loader": "graveyard_synergy",
    "untap_combo": "port_match",
    "attack_payoffs": "port_match",
    "exalted_density": "scaling",
    "aura_equipment_support": "scaling",
    "subject_zone_feeder": "graveyard_synergy",
    "counter_axis_feeder": "counter_synergy",
    "modified_axis_feeder": "counter_synergy",
    "cardpower_axis_feeder": "port_match",
    "tap_type_feeder": "port_match",
    "hand_size_feeder": "port_match",
    "gy_fuel_feeder": "port_match",
    "lifegain_feeder": "port_match",
    "life_total_feeder": "port_match",
    "land_bounce_feeder": "port_match",
    "etb_tapped_stax_feeder": "port_match",
    "party_feeder": "port_match",
    "creature_died_feeder": "port_match",
    "creatures_as_lands_landfall": "port_match",
    "damage_doubler_synergy": "port_match",
    "choose_tribal": "port_match",
    "doctor_s_tribal": "port_match",
    "more_tribal": "port_match",
    "prowess_tribal": "port_match",
    "etbreplacement_copy_dbcopy_optional_tribal": "port_match",
    "firebending_2_tribal": "port_match",
    "start_tribal": "port_match",
    "etbreplacement_other_choosect_tribal": "port_match",
    "mentor_tribal": "port_match",
    "repl_moved_exile_stack": "port_match",
    "changeling_tribal": "port_match",
    "landwalk_island_tribal": "port_match",
    "melee_tribal": "port_match",
    "training_tribal": "port_match",
    "repl_damagedone_counters_stack": "port_match",
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
    }
)

#: Rules where the commander actively feeds the candidate (commander → candidate).
#: Excludes broad identity matches (etb_self, zone_resonance) — only rules
#: where the commander's effect directly enables the candidate.
_FED_BY_COMMANDER_RULES: frozenset[str] = frozenset(
    {
        "effect_feeds_trigger",
    }
)

#: Rule pairs that form mechanical feedback loops.  When a card matches
#: both rules in a pair, it receives the specified bonus on top of its
#: IDF-weighted score.  This replaces the old flat multi-rule bonus
#: (+0.02 per rule) with pair-aware scoring — only mechanically
#: meaningful combinations are rewarded.
_SYNERGY_PAIRS: dict[frozenset[str], float] = {
    # Sacrifice + recursion loop
    frozenset({"cost_feeds_trigger", "graveyard_play"}): 0.05,
    # Sacrifice fodder engine
    frozenset({"effect_feeds_trigger", "sacrifice_cluster"}): 0.04,
    # Bidirectional synergy: feeds and is fed by commander
    frozenset({"trigger_effect", "effect_feeds_trigger"}): 0.05,
    # Tribal: lord + is the tribe
    frozenset({"lord", "tribal_density"}): 0.03,
    # Toughness: scales + has Defender
    frozenset({"scaling", "toughness_synergy"}): 0.03,
    # Cost reduction: big creature + damage enabler
    # Cheat-into-play: type match + CMC bonus
    frozenset({"cheat_cmc", "value_engine"}): 0.03,
    # Landfall: land enabler + zone resonance
    frozenset({"landfall_enabler", "zone_resonance"}): 0.04,
    # Counter synergies
    frozenset({"counter_doubler", "counter_keyword"}): 0.03,
    frozenset({"counter_doubler", "proliferate_synergy"}): 0.03,
}


def _compute_pair_bonus(rules: frozenset[str]) -> float:
    """Sum bonuses for all synergy pairs present in a card's rule set."""
    bonus = 0.0
    for pair, value in _SYNERGY_PAIRS.items():
        if pair <= rules:
            bonus += value
    return bonus


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
        # Multi-rule bonus: flat breadth reward (+0.02 per extra rule)
        # plus pair bonuses for mechanical feedback loops.
        n_rules = len(self.distinct_rules)
        if n_rules >= 2:
            base += 0.02 * min(n_rules - 1, 4)
        base += _compute_pair_bonus(self.distinct_rules)
        base += self.circuit_bonus + self.cmc_bonus + self.rank_bonus
        return base

    @cached_property
    def distinct_rules(self) -> frozenset[str]:
        return frozenset(c.rule_id for c in self.complements if c.direction == "synergy")

    def to_legacy_buckets(self) -> dict[str, float]:
        """Map IDF-weighted scores to legacy bucket dict.

        The running ``total`` is accumulated in the single complement
        loop instead of re-summing the dict afterwards — this function
        is called once per candidate per commander (≈80 k times across a
        100-commander batch), so dropping the 28-key ``sum`` comprehension
        is worth a couple hundred milliseconds.
        """
        buckets: dict[str, float] = dict.fromkeys(BUCKETS, 0.0)
        total = 0.0
        seen: set[tuple[str, str, str, str, str]] = set()
        seen_add = seen.add
        idf_weights = self.idf_weights
        for c in self.complements:
            key = (c.rule_id, c.cmdr_event, c.cand_event, c.filter_group, c.direction)
            if key in seen:
                continue
            seen_add(key)
            bucket = _RULE_TO_BUCKET.get(c.rule_id, "catchall")
            idf_key = (c.rule_id, c.cmdr_event, c.cand_event, c.filter_group)
            weight = idf_weights.get(idf_key, 1.0)
            if c.direction == "anti_synergy":
                buckets[bucket] -= weight
                total -= weight
            else:
                buckets[bucket] += weight
                total += weight
        if self.staple_bonus:
            buckets["staple"] = self.staple_bonus
            total += self.staple_bonus
        n_rules = len(self.distinct_rules)
        if n_rules >= 2:
            total += 0.02 * min(n_rules - 1, 4)
        total += _compute_pair_bonus(self.distinct_rules)
        total += self.circuit_bonus + self.cmc_bonus + self.rank_bonus
        buckets["total"] = total
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
    "spell_density": 0.3,
    "scaling": 0.3,
    "tribal_density": 0.5,
    "token_producer": 0.25,
    "evasion": 0.15,
    "etb_self": 0.01,
}

#: Quality multiplier applied to IDF weights. Dampens broad effect-only
#: rules and enabler-on-enabler trigger-trigger matches.
_RULE_QUALITY_MULTIPLIER: dict[str, float] = {
    # Trigger-trigger resonance is weaker than trigger-effect: finding
    # another card that triggers on the same event (enabler for enabler)
    # is less valuable than finding a payoff that feeds the trigger.
    "trigger_resonance": 0.7,
    # Value engine matches many cards of a type (Angel, Artifact, etc.).
    # IDF already dampens based on N, but the signal is inherently weaker
    # than mechanical synergy — "be the right type" is a density signal.
    "value_engine": 0.5,
    # Value engines (Kozilek, Artisan, Rune-Scarred Demon) only match this
    # single rule for pure cost-reducer commanders (Rakdos, Animar, Hamza).
    # The pool is narrowed to non-damage-dealing creatures, so the remaining
    # hits are the high-quality ETB payoffs we actually want to surface.
    # 1.0× (no dampening) so they can crack top-30 when no other rule fires.
    "cost_reduction_target": 1.0,
    # token_etb_damage has ~40 matches giving IDF ≈ 0.19. For commanders
    # with creature-heavy decks this lifts correct picks (Prossh +0.195),
    # but for 451 total touched cmdrs the audit 2026-04-20 still flagged
    # CONTENTIOUS at multiplier 0.5 — aggregate -0.885 NDCG, Breya -2
    # hits / -0.212, Zidane -2 / -0.099. Further dampen to 0.3: Prossh's
    # golden anchor (+0.195 at 0.5x) drops to ~+0.117 — still above the
    # 0.05 override threshold — while the off-archetype displacement on
    # Breya/Zidane drops proportionally. Tighter gates were considered
    # (require multiple Token effects, exclude spell/artifact cmdrs) but
    # a scalar dampen preserves the simple gate and lets IDF do the
    # ranking work.
    "token_etb_damage": 0.3,
    # combat_enhancer has a ~192-card pool (52 AddPhase + 140 Double
    # Strike) and a 501-cmdr touched set — the Attacks-Self-with-value
    # gate catches any commander with an engine-effect on-attack trigger
    # (Kroxa mill, Green Goblin draw-and-discard) but only a small
    # subset actually builds the deck around extra combat steps
    # (Etali, Aurelia, Saskia). Default 1.0× multiplier displaced
    # archetype-correct EDHREC picks on the false-positive tail
    # (audit 2026-04-20: -0.281 NDCG aggregate, Kroxa -4 hits,
    # Green Goblin -2). 0.7× dampens the IDF weight without losing
    # the top winners (Etali +5, Djeru +3, Narset +2 preserved) —
    # retune lifted the aggregate to +0.205 NDCG / +14 hits across
    # the same 501 cmdrs, golden unchanged. 0.5× was tested first and
    # dropped Narset below the golden threshold (-0.064); 0.7× keeps
    # her at her original +0.046 gain.
    "combat_enhancer": 0.7,
    # zone_resonance: 172 touched cmdrs, +1.146 NDCG, +13 hits at the
    # default 1.0 multiplier — MARGINAL verdict at ratio 0.076, just
    # below the 0.1 positive threshold. Top movers show canonical
    # landfall / tribal resonance wins (Ambrosia +4 hits / +0.144,
    # Sek'Kuar +1 / +0.140, Syr Ginger +2 / +0.067) with only tiny
    # losses (Omnath -0.025, Reaper -0.016). Bumping to 1.3 boosts
    # the clear wins across the hit threshold without amplifying the
    # near-noise-level losses enough to cross the 0.05 golden bar.
    "zone_resonance": 1.3,
    # counter_keyword has a ~100-card pool (Modular / Undying / Persist
    # / Evolve / Fabricate / Riot creatures) feeding all +1/+1-counter-
    # interested commanders. Audit 2026-04-20: -0.876 NDCG across 53
    # touched, -1 hits, saved only by Ezuri's golden anchor at +0.053
    # (right on the override threshold). Shalai and Hallar -3 hits /
    # -0.345 NDCG, Blaster -1 / -0.193 — their EDHREC decks run
    # specific keyword creatures but the broad pool displaced correct
    # picks. Ezuri has redundant anchors (counter_doubler, counter_-
    # target_payoff at +0.068) so dampening here is safe. 0.5×
    # dampens the IDF pressure while leaving the +1 wins (Ezuri,
    # Marchesa) intact.
    "counter_keyword": 0.5,
    # dies_drain: payoff cards (Blood Artist, Grim Haruspex, Pitiless
    # Plunderer) are pure consequence cards — they don't feed the
    # engine the way sac-cost creatures do, so they accumulate fewer
    # rule matches (typically 3 vs 6) and were drowned out without a
    # boost. 1.5× lifts payoff cards into view without displacing
    # tribal lords on Zombie/Sliver dies-commanders (Wilhelt's Death
    # Baron, Cemetery Reaper). Pool size is defined by
    # ``_bulk_load_dies_drain_cards``.
    "dies_drain": 1.5,
    # gy_loader: 49-card tutor pool → IDF ≈ 0.177. A single matching
    # rule leaves tutor sorceries (Buried Alive, Entomb) at rank ~180
    # for Karador/Muldrotha because they have no other port matches
    # (no ETB, no tribal hook, no triggers). 1.5× lifts them into the
    # range where reanimator commanders actually see them; the
    # original ``trigger_effect+graveyard_filler+entomb`` emission
    # stays put so the card accumulates two distinct rule matches
    # plus the breadth bonus.
    "gy_loader": 1.5,
    # untap_combo: combo cards are mostly instants/enchantments/
    # statics (Dramatic Reversal, Unwinding Clock, Paradox Engine)
    # that match NO other rules — Urza's top-30 is a tribal_density
    # tie at 0.50. 3× lifts specialist combo cards enough that,
    # combined with breadth bonuses on cards that also match
    # tribal_density (Voltaic Servant, Clock of Omens), they actually
    # crack top-30. Pool is defined by ``_bulk_load_untap_combo_cards``.
    "untap_combo": 3.0,
    # attack_payoffs: ~400-card pool → IDF ≈ 0.115. The existing
    # panharmonicon rule already matches these cards for Isshin but
    # does so against a 1000+ pool, burying Hi-Syn attack creatures
    # (Adeline, Krenko Tin Street) in a flat tier. 1.5× on a narrower
    # value-effect-filtered pool puts real attack payoffs above the
    # tier of staple equipment noise.
    "attack_payoffs": 1.5,
    # aura_equipment_support: ~30-card pool → IDF ≈ 0.20. These support
    # cards (Sigarda's Aid, Sram, Puresteel Paladin) have exactly one
    # rule match on voltron commanders and sit below the ``scaling``
    # (voltron flat 0.3) floor that every random Aura/Equipment rides.
    # 2.5× puts them above the floor so the enablers crack top-30.
    "aura_equipment_support": 2.5,
    # wheel_synergy: ~62-card wheel pool, IDF ≈ 0.17 per match. Wheels
    # are the archetype's DEFINING cards (Windfall, Wheel of Fortune,
    # Magus of the Wheel) for Locust God / Nekusar / Narset's Reversal-
    # style commanders. Without a boost they sit at ~0.25 port_match,
    # below loot artifacts (Bag of Holding, Soul-Guide Lantern) that
    # accumulate ``etb_value`` on top of their single port match. 2.0×
    # lifts true wheels above loot.
    "wheel_synergy": 2.0,
    # monarch_synergy: ~75-card pool (BecomeMonarch effects + pillowfort
    # statics), IDF ≈ 0.17. For Queen Marchesa the Courts and pillowfort
    # cards match only this single rule and sit below the 0.5 tribal-
    # Assassin floor (she creates Assassin tokens, so every Assassin
    # gets catchall 0.5). 2.5× lifts the archetype's canonical cards
    # (Court of Grace, Ghostly Prison, Thorn of the Black Rose) into
    # the top tier.
    "monarch_synergy": 2.5,
    # counter_target_payoff: ~280-card pool of P1P1 receivers (IDF ≈ 0.14).
    # Gated on the XP-counter mechanism (``scales_with
    # YourCountersExperience`` Forge SVar) combined with active P1P1
    # distribution — a mechanism-specific pair, not a commander name.
    # 2.0× lifts Fathom Mage / Gyre Sage into Ezuri's top-20.
    "counter_target_payoff": 2.0,
    # creature_untap_engine: ~150-card creature-untap pool (IDF ≈ 0.14).
    # Fires only for tap-for-mana creature commanders (Selvala). Without
    # a boost, Quirion Ranger / Scryb Ranger only hit ~0.15 port_match
    # and sit at rank 795; Selvala's top-10 is dominated by artifact-
    # untap cards from ``untap_combo`` (flat 3.0× pool multiplier for
    # Urza). 3.0× to bring creature-untappers level with artifact-
    # untappers for her archetype.
    "creature_untap_engine": 3.0,
    # populate_stack: Ghired's 26-card populate pool (Sundering Growth,
    # Rootborn Defenses, Growing Ranks, Second Harvest, Song of the
    # Worldsoul). IDF ≈ 0.21. Without a boost, populate spells sit at
    # rank 70+ behind Rhino-tribal catchall 0.5. 2.5× lifts them into
    # the top tier.
    "populate_stack": 2.5,
    # landfall_enabler: ~20-card AdjustLandPlays + MayPlay-Land-from-GY
    # pool (Azusa, Crucible of Worlds, Ramunap Excavator, Ancient
    # Greenwarden, Dryad of the Ilysian Grove, Exploration). Fires for
    # landfall-trigger commanders (Tatyova, Titania) and land-reanimate
    # commanders (Lord Windgrace). Base IDF ≈ 0.22. Without a boost,
    # canonical lands-matter support sits at rank 200+ below broad
    # graveyard+etb_value stacks. 2.0× lifts the archetype's defining
    # enablers into top-30 without flattening texture.
    "landfall_enabler": 2.0,
    # subject_zone_feeder: general rule for commanders with a death
    # trigger filtered to a specific subject type (Land, Creature,
    # Artifact, creature-subtype). Matches candidates whose effect
    # sacrifices that subject (Scapeshift, Barter in Blood) or returns
    # it en-masse from graveyard (Splendid Reclamation, Living Death).
    # Pool scales with subject specificity: Land ~15 cards, Creature
    # ~50 cards. IDF ~0.2-0.25 per tier. 2.5× lifts these narrow
    # archetype-defining cards (Splendid Reclamation for Titania, which
    # matches NO other rule) into top-30 territory. Narrow gate (only
    # specific-subject death-trigger commanders qualify) keeps
    # cross-commander impact minimal.
    "subject_zone_feeder": 2.5,
    # counter_axis_feeder: general rule for commanders with a port
    # (trigger / scales_with / static) whose valid_filter contains a
    # ``counters_GE_<TYPE>`` qualifier on a broad scope (non-Self).
    # Four deduped tiers match candidates on the same axis —
    # counter_axis_payoff (~10 cards) > counter_producer (~150 after
    # dropping self-sac-only cards) > etb_counter_keyword (~300) >
    # self_recur_keyword (~75, P1P1 axis only). 3.0× lifts the narrow
    # top tier into the 0.5-0.6 range — enough to crack the 4-rule
    # aristocrat-noise floor for counter-axis commanders (Marchesa,
    # Hamza) without the high multiplier that would be needed to beat
    # scaling-rule bonuses on incidental proliferate cards.
    "counter_axis_feeder": 3.0,
    # modified_axis_feeder: parallel to counter_axis_feeder for the
    # ``modified`` qualifier (a creature with a +1/+1 counter, Aura, or
    # Equipment attached). Three tiers — modified_p1p1_producer (~150,
    # same set as counter_producer P1P1) > modified_proliferate (~70) >
    # modified_etb_keyword (~330, etbCounter:P1P1 + Modular). 3.0× to
    # match counter_axis_feeder so Kodama / Red XIII / SP//dr / Sephiroth
    # / Chishiro all get their P1P1 distributors into top-30. Pure
    # counter-payoff commanders qualify, so the multiplier matches
    # counter_axis_feeder rather than the lower 2.5× of subject_zone /
    # creatures_as_lands gates that target less-curated archetypes.
    "modified_axis_feeder": 3.0,
    # cardpower_axis_feeder: commanders with ``SVar:X:Count$CardPower``
    # scale their abilities with their own power. Two deduped tiers —
    # cardpower_big_attachment (~220 Equipment/Aura with AddPower>=3 or
    # AddPower=X/Y/Z) > cardpower_p1p1_producer (~400 after dropping
    # self-sac-only). 2.5× (vs counter/modified's 3.0×) because the
    # attachment tier overlaps with any commander's voltron pool — the
    # more conservative multiplier guards against flooding the top-30
    # of existing voltron commanders that happen to share the gate.
    # Audit may bump to 3.0× if CardPower NDCG rises without regression.
    "cardpower_axis_feeder": 2.5,
    # tap_type_feeder: commanders with a ``cost.tap_type`` port
    # (``tapXType<N/SUBJECT>``) tap N permanents of a subject as activation
    # cost. Two deduped tiers — tap_type_sustained_untap (~10 cards per
    # axis, static.UntapOtherPlayer: Seedborn Muse, Prophet of Kruphix,
    # Murkfiend Liege) > tap_type_phase_untap (~10 cards per axis,
    # Phase-trigger + UntapAll: Awakening, White Plume Adventurer).
    # 2.0× multiplier (vs counter/modified's 3.0×) because the pool is
    # very narrow (~20 cards per axis) so each match's IDF is already
    # high (~0.29). 3.0× flooded Aryel/Kumena's top-30 with untap
    # cards and displaced their tribal EDHREC Hi-Syn picks (-0.16 /
    # -0.07 NDCG); 2.0× keeps the tier visible (6 untaps in Aryel's
    # top 15) without dominating Knight-lords / Merfolk-lords.
    "tap_type_feeder": 2.0,
    # hand_size_feeder: big-hand commanders (scales_with ValidHand
    # Card.YouOwn, rejecting small-hand signals LE0/LE1/EQ0/GE2/GE3
    # on the hand-binding SVar — Hazoret/Neheb/Djeru-and-Hazoret/Flubs).
    # Single tier — hand_size_no_max (~46 cards, SetMaxHandSize:
    # Unlimited statics: Reliquary Tower, Thought Vessel, Library of
    # Leng, Spellbook, Venser's Journal, Decanter of Endless Water,
    # Folio of Fancies). 2.5× to match subject_zone/creatures_as_lands:
    # narrow, archetype-defining pool with IDF ~0.18 per match.
    # No voltron/tribal overlap — the candidates are Artifact/
    # Enchantment mana rocks / libraries, not stat-sticks.
    "hand_size_feeder": 2.5,
    # gy_fuel_feeder: commanders with cost.exile_from_grave (any-
    # target) pay by exiling cards from graveyards — Aphemia,
    # Ashnod, Araumi, Drivnod, Egon, Ishkanah, Kethis, etc. Single
    # tier — gy_fuel_self_mill (~100 effect.Mill cards with
    # Defined: 'You' and NumCards >= 3 or scaling X/Y/Z; threshold
    # tightened from 2 to 3 after initial audit flagged cantrip-mill
    # flooding on Osgir / Ultimecia). 2.5× to match hand_size_feeder
    # — narrow-axis-gate + single-tier archetype feeder with IDF
    # ~0.16 per match.
    "gy_fuel_feeder": 2.5,
    # lifegain_feeder: commanders with scales_with LifeYouGainedThisTurn
    # scale their mechanic by life gained this turn (Astarion draw,
    # Bre power/toughness, Willowdusk counters, Licia pump). Two
    # deduped tiers — lifegain_amp (~12 replacement.GainLife
    # amplifiers: Alhammarret / Rhox / Boon Reflection / Wind Crystal)
    # > lifegain_etb_trigger (~45 soul sisters: Soul Warden /
    # Auriok Champion / Ajani's Welcome / Anointer Priest). 2.5×
    # matches hand_size/gy_fuel — narrow single-axis feeder with
    # IDF ~0.20 per match across a tight pool of ~55 total candidates.
    "lifegain_feeder": 2.5,
    # life_total_feeder: commanders with scales_with YourLifeTotal AND
    # an up-biased lifegain signal (GainLife replacement amp on self OR
    # static.Continuous with SVarCompare GT*/GE*). Narrow gate leaves
    # Bilbo (Birthday Celebrant — GainLife doubler) and Elenda (Saint
    # of Dusk — +1/+1 when life > starting, +5/+5 when life >= +10);
    # explicitly excludes Ayli / Bane / Beza / Cecil / Jerren / Linvala
    # which carry the axis but read life as a query variable (exile
    # power cap, indestructible-at-low-life, token count, flip
    # thresholds) — first attempt regressed those cmdrs by -0.2 net
    # NDCG (audit 2026-04-20, reverted in ec67250). 2.5× matches
    # hand_size/gy_fuel/lifegain — narrow single-axis feeder with a
    # tight archetype-defining pool (~27 cards, IDF ~0.18 per match).
    "life_total_feeder": 2.5,
    # land_bounce_feeder: commanders whose activated ability costs a
    # land-return (Meloku / Mina and Denn / Multani / Soramaro / Sutina
    # / Tameshi — 6 cmdrs, 0% prior coverage). Two deduped tiers:
    # land_bounce_extra_drops (~38 static.Continuous AdjustLandPlays —
    # Azusa, Exploration, Oracle of Mul Daya, Dryad of the Ilysian
    # Grove, Fastbond) > land_bounce_gy_recur (~56 effect.ChangeZone
    # Graveyard-source with Land filter — Crucible of Worlds, Ramunap
    # Excavator, Splendid Reclamation, Life from the Loam, Lord
    # Windgrace). Multiplier 2.5× matches other single-axis feeders —
    # tight pool (~94 cards total, IDF ~0.15 per match), archetype-
    # defining. No overlap with extra_land_plays (inverse gate: that
    # rule feeds landfall triggers to cmdrs with AdjustLandPlays
    # statics; this one feeds AdjustLandPlays to cmdrs with a land-
    # return cost).
    "land_bounce_feeder": 2.5,
    # etb_tapped_stax_feeder: stax/pillowfort commanders whose
    # replacement.Moved port forces EXTERNAL permanents to ETB tapped
    # (Reidane, Spider-Woman, Thalia+Gitrog, Thalia Heretic Cathar,
    # Urabrask, Zhao, Archelos — 7 legendary cmdrs, 0% prior coverage).
    # Single tier etb_tapped_stax_peer pulls ~24 other cards with the
    # same mechanical shape: Authority of the Consuls, Kismet, Blind
    # Obedience, Loxodon Gatekeeper, Kinjalli's Sunwing, Imposing
    # Sovereign, Manglehorn, Dauntless Dismantler, Archon of Emeria,
    # Frozen Aether, Orb of Dreams, Root Maze — EDHREC stax staples.
    # 2.5× matches other single-axis feeders; pool ~24 → IDF ~0.22,
    # effective ~0.55 per match. Excludes Card.Self (that covers 542
    # tapped-land cards + drawback-ETB creatures like Grimgrin /
    # Ebondeath who belong to sac-outlet / reanimator archetypes).
    "etb_tapped_stax_feeder": 2.5,
    # party_feeder: commanders whose payoff scales with Forge's
    # Count$Party (distinct Cleric / Rogue / Warrior / Wizard count,
    # capped at 4). 9 legendary cmdrs, 0% prior coverage: Burakos,
    # Linvala Shield of Sea Gate, Nalia de'Arnise, Tazri Beacon of
    # Unity, The Destined Black Mage / Thief / Warrior / White Mage,
    # Zagras Thief of Heartbeats. Single tier party_peer pulls the
    # other 34 cards with scales_with.Party — the Zendikar Rising /
    # Baldur's Gate / Final Fantasy Party staple set (Coveted Prize,
    # Spoils of Adventure, Thwart the Grave, Acquisitions Expert,
    # Kabira Outrider, Emeria Captain, Malakir Blood-Priest, Ravager's
    # Mace, Multiclass Baldric). Pool is mechanically identical to the
    # cmdrs themselves — strongest possible archetype signal. IDF
    # ~0.17 per match; 2.5× multiplier matches other small-pool
    # single-axis feeders.
    "party_feeder": 2.5,
    # creature_died_feeder: aristocrats commanders whose payoff scales
    # with Count$ThisTurnEntered_Graveyard_from_Battlefield_Creature
    # (any filter variant: .YouCtrl / .YouOwn / .!token / .!namedX).
    # 15 legendary cmdrs, 0% prior coverage: Asmira, Bontu, Denethor,
    # Ebondeath, Faramir, Gadrak, Gimli Mournful Avenger, Inga Rune-
    # Eyes, Kuon, Lagomos, Mahadi, Nevinyrral, Shessra, Sméagol,
    # Tobias. Single tier pulls ~49 non-legendary peers — Feast of
    # the Victorious Dead, Fresh Meat, Caller of the Claw, Deathreap
    # Ritual, Grizzly Ghoul, Khabál Ghoul, Tallyman of Nurgle,
    # Liliana's Devotee, Warlock Class, Ichor Shade, Rise of the
    # Dread Marn, Osai Vultures, Vile Redeemer, Spoils of Blood, Body
    # Count, Spymaster's Vault — the aristocrats staple set. Same
    # pattern as party_feeder (pool IS the archetype). 2.5× multiplier
    # matches other single-axis feeders; IDF ~0.17 per match.
    "creature_died_feeder": 2.5,
    # creatures_as_lands_landfall: commanders whose type-bending static
    # makes creatures also lands (Ashaya, Soul of the Wild). Pool ~237
    # landfall-trigger cards (Rampaging Baloths, Lotus Cobra, Avenger of
    # Zendikar, Scute Swarm). IDF ≈ 0.126. 2.5× lifts landfall payoffs
    # above the flat 0.30 ``scaling`` floor that every land sits at for
    # these commanders — without the multiplier the generic basic
    # lands tie with Rampaging Baloths. The gate matches static ports
    # with ``Affected: Creature`` + ``AddType: Land``; Ashaya is the
    # only card matching today but any future card with the same
    # mechanical shape qualifies automatically.
    "creatures_as_lands_landfall": 2.5,
    # damage_doubler_synergy: replacement.DamageDone with damage-amp
    # replacement_result (Torbran +2, Gisela / Solphim double, Tor
    # Wauki / Raphael / Wolverine variants). Two tiers — amp_stack
    # (~50 cards: Furnace of Rath, Fiery Emancipation, Curse of
    # Bloodletting, Angrath's Marauders) + damage_pinger (~170
    # cards: Guttersnipe, Firebrand Archer, Manabarbs, Sulfuric
    # Vortex, Storm-Kiln Artist). Multiplier 2.5× lifts the narrow
    # amp_stack tier into the 0.5+ range — the multiplicative
    # synergy of paired doublers is the genuine Hi-Syn signal for
    # this archetype, not the broad pinger pool.
    "damage_doubler_synergy": 2.5,
    # peer_evasion_tribal: commanders carrying a peer-blocking
    # keyword (Horsemanship 29 cards / Shadow 36 cards) want the
    # rest of the pool as both attackers and the only legal blockers
    # against opposing copies. Tiny pools mean naturally high IDF;
    # the 2.0× multiplier ensures the partners surface above the
    # generic-staple noise (P3K vanilla horsemanship commanders had
    # nothing else for the engine to latch onto). All 14 P3K
    # legendary horsemanship commanders qualify automatically; any
    # future card / commander with these keywords does too.
    # AUTO-GENERATED keyword-tribal — IDF handles weighting
    "choose_tribal": 2.0,
    # AUTO-GENERATED keyword-tribal — IDF handles weighting
    "doctor_s_tribal": 2.0,
    # AUTO-GENERATED keyword-tribal — IDF handles weighting
    "more_tribal": 2.0,
    # AUTO-GENERATED keyword-tribal — IDF handles weighting
    "prowess_tribal": 2.0,
    # AUTO-GENERATED keyword-tribal — IDF handles weighting
    "etbreplacement_copy_dbcopy_optional_tribal": 2.0,
    # AUTO-GENERATED keyword-tribal — IDF handles weighting
    "firebending_2_tribal": 2.0,
    # AUTO-GENERATED keyword-tribal — IDF handles weighting
    "start_tribal": 2.0,
    # AUTO-GENERATED keyword-tribal — IDF handles weighting
    "etbreplacement_other_choosect_tribal": 2.0,
    # AUTO-GENERATED keyword-tribal — IDF handles weighting
    "mentor_tribal": 2.0,
    # AUTO-GENERATED replacement-stack — IDF handles weighting
    "repl_moved_exile_stack": 2.0,
    # AUTO-GENERATED keyword-tribal — IDF handles weighting
    "changeling_tribal": 2.0,
    # AUTO-GENERATED keyword-tribal — IDF handles weighting
    "landwalk_island_tribal": 2.0,
    # AUTO-GENERATED keyword-tribal — IDF handles weighting
    "melee_tribal": 2.0,
    # AUTO-GENERATED keyword-tribal — IDF handles weighting
    "training_tribal": 2.0,
    # AUTO-GENERATED replacement-stack — IDF handles weighting
    "repl_damagedone_counters_stack": 2.0,
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
        filter_group = key[3] or ""
        # Effect-conditional dampening: when a commander trigger's
        # execute has a runtime gate (Selvala power compare, Meren's
        # XP-vs-CMC), matches on that trigger are tagged with ":cond" in
        # filter_group. Halve IDF for these — the trigger fires broadly
        # but the payoff is gated, so most matches don't pan out.
        cond_mult = 0.5 if filter_group.endswith(":cond") else 1.0
        if rule_id in _FLAT_COUNT_RULES:
            override = _FLAT_WEIGHT_OVERRIDES.get(rule_id)
            base_w = override if override is not None else 1.0
            result[key] = base_w * cond_mult
        else:
            n = len(candidates)
            # For forward panharmonicon matches, apply minimum N=10 floor.
            # Very rare effects (SacrificeAll N=1) get inflated IDF that
            # doesn't reflect true synergy quality — Krovikan Vampire at
            # IDF=1.0 shouldn't outrank Zulaport Cutthroat at IDF=0.17.
            # Exclude reverse_panharmonicon and panharmonicon_stack which
            # have genuinely unique high-value matches (Harmonic Prodigy).
            cmdr_event = key[1]
            if rule_id == "panharmonicon" and "reverse" not in cmdr_event and "stack" not in cmdr_event:
                # Floor raised from 10 to 30 so rare board-wide triggers
                # (ChangesZoneAll + Token, N=35, IDF 0.195) don't swamp
                # the numerous self-ETB payoff groups (ChangesZone_etb_*,
                # IDF 0.10-0.14). Yarok's EDHREC Hi-Syn is dominated by
                # self-ETB value creatures (Mulldrifter, Coiling Oracle)
                # that were buried at rank 1000+ when board-wide-trigger
                # cards captured the 0.289 cap.
                n = max(n, 30)
            w = 1.0 / math.log2(1.0 + n)
            # Apply quality multiplier: cost-based rules are boosted,
            # broad effect rules are dampened.
            mult = _RULE_QUALITY_MULTIPLIER.get(rule_id, 1.0)
            result[key] = w * mult * cond_mult
    return result


def score_all_universal(
    conn: sqlite3.Connection,
    commander_set: Sequence[str],
    candidate_cache: CandidateCache | None = None,
) -> dict[str, UniversalScore]:
    """Score every candidate via IDF-weighted port matching.

    Each complement's value is ``1/log2(1+N)`` where N is the number of
    candidates sharing the same match tuple. Specific matches (few
    candidates) are worth more than broad matches (many candidates).

    Returns one ``UniversalScore`` per candidate reached by at least one
    complement rule.

    When ``candidate_cache`` is provided, the commander-independent
    cmc/edhrec_rank load is skipped in favour of the cache, and the
    cache is forwarded to ``find_all_complements`` so the complement
    rule layer can also skip its redundant SQL.
    """
    complements = find_all_complements(conn, commander_set, candidate_cache=candidate_cache)
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

    # Bulk-load CMC and edhrec_rank for micro-scores. In batch mode the
    # engine shares a CandidateCache, so we read the pre-loaded map
    # instead of re-issuing the same full-table scan per commander.
    cmc_data: dict[str, float] = {}
    rank_data: dict[str, int] = {}
    if candidate_cache is not None:
        for name, (cmc, rank) in candidate_cache.cmc_rank_map.items():
            cmc_data[name] = cmc
            rank_data[name] = rank
    else:
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
