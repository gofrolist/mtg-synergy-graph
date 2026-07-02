"""Universal port-complement scorer.

Scores every candidate by counting distinct mechanical interactions with
the commander's ports, weighted by specificity (IDF).  No hand-tuned
weights — specificity is derived from the data: a match that only 3
candidates satisfy is worth more than one 2000 candidates satisfy.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import sqlite3
from collections import defaultdict
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import cached_property, lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple, Protocol

from .complement_rules import (
    PortComplement,
    find_all_complements,
)
from .heuristics import STAPLES
from .scoring import BUCKETS

if TYPE_CHECKING:
    from .penalties import CandidateCache

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TensorRow:
    """One per-(commander, candidate, rule_id) contribution row.

    Passed to the ``TensorSink`` hook wired by ``bench.py``; the
    interchange type between scoring and persistence. Fields are
    accessed by name in every caller; positional access is avoided
    so a future field addition is non-breaking.
    """

    commander: str
    candidate: str
    rule_id: str
    contribution: float
    idf_weight: float
    raw_count: int


class TensorSink(Protocol):
    """Callable that receives one ``TensorRow`` per emission.

    Used by ``score_all_universal(tensor_sink=...)`` to externalize
    the per-(cmdr, cand, rule_id) contribution tensor without the
    scorer needing to know how it's persisted. See
    ``docs/plans/2026-04-22-001-feat-unified-eval-harness-plan.md``
    FR2 for the persistence contract.
    """

    def __call__(self, row: TensorRow) -> None: ...


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
    "land_to_gy_synergy": "port_match",
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
    "cascade_tribal": "port_match",
    # Plan 2026-04-23-001 depth-2 self-bridging pathway.
    "self_bridging_cascade": "port_match",
    # Plan 2026-05-19 Prepared / AlternateMode:Prepare mechanic capture.
    "prepared_mechanic": "port_match",
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


@lru_cache(maxsize=4096)
def _compute_pair_bonus(rules: frozenset[str]) -> float:
    """Sum bonuses for all synergy pairs present in a card's rule set.

    Cached because the bench optimizer calls this 43.85M+ times per sweep
    (once per candidate per grid cell) — see the 2026-04-30 cProfile run
    in ``docs/solutions/best-practices/optimizer-perf-profile-2026-04-30.md``.
    The function is pure and the input is hashable, so caching is safe.

    Cache invalidation contract: if a test or migration mutates
    ``_SYNERGY_PAIRS`` at runtime, call ``_compute_pair_bonus.cache_clear()``
    in test setup/teardown. The module-level dict is not currently mutated
    by any non-test code path; callers that monkeypatch ``_SYNERGY_PAIRS``
    must clear the cache explicitly.
    """
    bonus = 0.0
    for pair, value in _SYNERGY_PAIRS.items():
        if pair <= rules:
            bonus += value
    return bonus


# ---------------------------------------------------------------------------
# §1c  Public scoring-config accessor
# ---------------------------------------------------------------------------


class ScoringConfigInputs(NamedTuple):
    """Public view over the module-private scoring-config dicts.

    Exposed so ``bench.tensor.compute_config_hash`` (and any future
    tooling) can observe what fingerprints tensor staleness without
    importing the underscore-private symbols directly. Rename the
    underlying dicts freely — consumers read them through this
    NamedTuple.

    The returned dicts are *live references*, not copies — tests that
    use ``unittest.mock.patch.dict`` on the private names still mutate
    what this function returns, which is the correct behavior for a
    config-fingerprint API.
    """

    rule_quality_multiplier: dict[str, float]
    flat_weight_overrides: dict[str, float]
    synergy_pairs: dict[frozenset[str], float]
    #: Plan 2026-04-23-001 Unit 6: flipping
    #: ``complement_rules.pathway._ENABLE_PATHWAY_RULES`` invalidates
    #: the persisted audit tensor. Exposing it here means
    #: ``compute_config_hash`` catches the flip without a second
    #: check-site.
    enable_pathway_rules: bool
    #: Plan 2026-04-23-003 Unit 7: the embedding-contribution gate
    #: (``embeddings.contribution._ENABLE_EMBEDDING_CONTRIBUTION``).
    #: Flipping this field invalidates the pinned tensor via
    #: ``compute_config_hash`` because the hash input set changes.
    #: Scores are only affected when ``embedding_w`` is also non-zero
    #: — with ``_EMBEDDING_W=0.0`` (current default), the short-circuit
    #: in ``embedding_contribution()`` returns ``0.0`` regardless of
    #: this flag.
    enable_embedding_contribution: bool
    #: Plan 2026-04-23-003 Unit 7: the embedding-contribution weight
    #: ``_EMBEDDING_W``. Tuning it changes scores — catch drift here.
    embedding_w: float
    #: Plan 2026-04-23-003 Unit 7: the embedding-contribution decay
    #: rate ``_EMBEDDING_K``. Tuning it changes scores — catch drift
    #: here.
    embedding_k: float
    #: Plan 2026-04-23-003 Unit 7: vectorizer version read from
    #: ``embeddings.config.get_embedding_config_inputs()``. A rebuild
    #: under a new token grammar bumps this and invalidates the tensor
    #: even when the flip flag + w + k are unchanged.
    vectorizer_version: int
    #: 2026-06-09 audit follow-up: the two committed seed JSONs
    #: directly determine scores (``event_match_seed.json`` builds the
    #: core rules' event_pairs; ``rules_seed.json`` predicate bodies
    #: compile to the interpreter's SQL/gates) but were absent from
    #: the hash — editing either left pinned tensors/fixtures silently
    #: "fresh". Digests cover the functional rows only, so edits to
    #: ``_readme`` / grammar prose do not flip the hash (mirroring the
    #: value-vs-comment discipline of ``scoring_weights.json``).
    event_match_seed_digest: str
    declarative_rules_digest: str
    #: The hand-curated ``heuristics.STAPLES`` dict both adds
    #: ``staple_bonus`` and injects staple-only candidates into
    #: results — editing it changes live scores, so per this
    #: accessor's own docstring rule it must invalidate the tensor.
    staples: dict[str, tuple[str, ...]]


def _seed_digest(filename: str, functional_keys: tuple[str, ...]) -> str:
    """SHA-256 over the functional content of a committed seed JSON.

    Only ``functional_keys`` participate — top-level documentation
    blocks (``_readme``, ``_match_qualities``, ``_predicate_grammar``)
    are excluded so prose edits do not invalidate pinned tensors.
    """
    # Local import: _paths is a port_graph leaf module; importing it at
    # module scope would couple the scorer's import to port_graph.
    from .port_graph._paths import default_seed_path

    data = json.loads(default_seed_path(filename).read_text(encoding="utf-8"))
    functional = {k: data.get(k) for k in functional_keys}
    canonical = json.dumps(functional, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def get_scoring_config_inputs() -> ScoringConfigInputs:
    """Return the scoring-config inputs that affect ``score()`` output.

    Intended consumer: ``bench.tensor.compute_config_hash``. If you
    tune ``score()`` with a new module-global dict or flag, add it
    here too — otherwise tensor staleness will go undetected.
    """
    # Local import: complement_rules imports universal_scorer for
    # _RULE_TO_BUCKET at module import time, so pulling pathway here
    # at function-call time avoids the cycle. Same rationale for the
    # embeddings modules — they import numpy + sqlite3 and have no
    # reason to be pulled in at scorer-module import.
    from .complement_rules import pathway
    from .embeddings import contribution as emb_contribution
    from .embeddings.config import get_embedding_config_inputs as _get_emb_cfg

    return ScoringConfigInputs(
        rule_quality_multiplier=_RULE_QUALITY_MULTIPLIER,
        flat_weight_overrides=_FLAT_WEIGHT_OVERRIDES,
        synergy_pairs=_SYNERGY_PAIRS,
        enable_pathway_rules=pathway._ENABLE_PATHWAY_RULES,
        enable_embedding_contribution=emb_contribution._ENABLE_EMBEDDING_CONTRIBUTION,
        embedding_w=emb_contribution._EMBEDDING_W,
        embedding_k=emb_contribution._EMBEDDING_K,
        vectorizer_version=_get_emb_cfg().vectorizer_version,
        event_match_seed_digest=_seed_digest("event_match_seed.json", ("event_match_map", "cost_feeds_trigger")),
        declarative_rules_digest=_seed_digest("rules_seed.json", ("rules",)),
        staples=STAPLES,
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
    #: Plan 2026-04-23-003 Unit 7: deterministic content-embedding
    #: contribution. When ``_ENABLE_EMBEDDING_CONTRIBUTION`` is False
    #: (module default), this stays ``0.0`` for every candidate —
    #: ``score`` is bitwise-identical to pre-plan baseline. When the
    #: audit-gated flip lands, this carries
    #: ``w * exp(-k * n_rules) * cosine(v_cand, v_cmdr)``.
    embedding_contribution: float = 0.0

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
        base += self.embedding_contribution
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
        total += self.embedding_contribution
        # Expose embedding contribution under a named bucket key so the
        # ``sum(named_bucket_values) == total`` invariant holds when the
        # flag is on. Alongside ``circuit_bonus``/``cmc_bonus``/
        # ``rank_bonus`` — these are additive non-rule terms.
        buckets["embedding"] = self.embedding_contribution
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

#: Path to the committed JSON sidecar holding the float values for
#: ``_FLAT_WEIGHT_OVERRIDES`` and ``_RULE_QUALITY_MULTIPLIER``.
#: Packaged inside the module (``src/mtg_synergy_graph/data/``) since
#: 2026-06-09 so installed wheels are importable — consumers like
#: mtg-edh-builder install this package from GitHub-release wheels.
_SCORING_WEIGHTS_PATH = Path(__file__).resolve().parent / "data" / "scoring_weights.json"

_VALID_SECTIONS = frozenset({"rule_quality_multiplier", "flat_weight_overrides"})
_VALID_ENTRY_FIELDS = frozenset({"value", "comment"})


class ScoringWeights(NamedTuple):
    """Structured return for ``_load_scoring_weights``.

    The two dicts are bound at module scope to ``_RULE_QUALITY_MULTIPLIER``
    and ``_FLAT_WEIGHT_OVERRIDES`` immediately after load. Test fixtures
    that mutate these module-level dicts in place (via ``.clear()`` +
    ``.update()``) preserve the dict identity that ``get_scoring_config_inputs()``
    returns — a property the hash-invariant tests rely on.
    """

    rule_quality_multiplier: dict[str, float]
    flat_weight_overrides: dict[str, float]


def _is_numeric_non_bool(x: object) -> bool:
    """True for int / float, False for bool (which is an int subclass)."""
    return not isinstance(x, bool) and isinstance(x, (int, float))


def _load_scoring_weights() -> ScoringWeights:
    """Read ``src/mtg_synergy_graph/data/scoring_weights.json`` into the two scoring-weight dicts.

    Returns a ``ScoringWeights`` NamedTuple with both sections.

    Strict shape validation: missing JSON file, malformed JSON, unknown
    top-level keys, missing required top-level sections, unknown per-entry
    fields, missing ``value``/``comment``, non-numeric ``value`` (bool
    explicitly rejected since it is an ``int`` subclass), or non-string
    ``comment`` all raise ``ValueError`` at module import. Every error
    message names the JSON file path so the failure is actionable
    without grep. Registry-membership (no dead rule_ids) is checked
    separately by ``tests/test_scoring_weights.py`` to avoid coupling
    the production import path to plugin load order.

    The ``comment`` field is intentionally consumed only for shape
    validation — it is not exposed at runtime, not folded into
    ``compute_config_hash``, and exists only on disk for human readers
    and ``jq``/grep queries.
    """
    try:
        with _SCORING_WEIGHTS_PATH.open(encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError as exc:
        raise ValueError(
            f"scoring_weights.json missing at {_SCORING_WEIGHTS_PATH}; "
            "this file is the source-of-truth for _RULE_QUALITY_MULTIPLIER + "
            "_FLAT_WEIGHT_OVERRIDES and must exist for scoring to function. "
            "Restore from git or check the .gitignore allowlist."
        ) from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"{_SCORING_WEIGHTS_PATH}: malformed JSON ({exc})") from exc

    extra_sections = set(raw) - _VALID_SECTIONS
    if extra_sections:
        raise ValueError(
            f"{_SCORING_WEIGHTS_PATH}: unknown top-level sections "
            f"{sorted(extra_sections)} (valid: {sorted(_VALID_SECTIONS)})"
        )
    missing_sections = _VALID_SECTIONS - set(raw)
    if missing_sections:
        raise ValueError(f"{_SCORING_WEIGHTS_PATH}: missing required top-level sections {sorted(missing_sections)}")

    sections: dict[str, dict[str, float]] = {}
    for section in _VALID_SECTIONS:
        section_raw = raw[section]
        out: dict[str, float] = {}
        for key, entry in section_raw.items():
            if not isinstance(entry, dict):
                raise ValueError(
                    f"{_SCORING_WEIGHTS_PATH}: {section}.{key}: entry must be an object, got {type(entry).__name__}"
                )
            extra_fields = set(entry) - _VALID_ENTRY_FIELDS
            if extra_fields:
                raise ValueError(f"{_SCORING_WEIGHTS_PATH}: {section}.{key}: unknown fields {sorted(extra_fields)}")
            if "value" not in entry:
                raise ValueError(f"{_SCORING_WEIGHTS_PATH}: {section}.{key}: missing required 'value'")
            if "comment" not in entry:
                raise ValueError(f"{_SCORING_WEIGHTS_PATH}: {section}.{key}: missing required 'comment'")
            value = entry["value"]
            if not _is_numeric_non_bool(value):
                raise ValueError(
                    f"{_SCORING_WEIGHTS_PATH}: {section}.{key}: 'value' must be a number, got {type(value).__name__}"
                )
            if not isinstance(entry["comment"], str):
                raise ValueError(
                    f"{_SCORING_WEIGHTS_PATH}: {section}.{key}: 'comment' must be a "
                    f"string, got {type(entry['comment']).__name__}"
                )
            out[key] = float(value)
        sections[section] = out

    return ScoringWeights(
        rule_quality_multiplier=sections["rule_quality_multiplier"],
        flat_weight_overrides=sections["flat_weight_overrides"],
    )


_LOADED_SCORING_WEIGHTS = _load_scoring_weights()


#: Per-rule flat weight overrides. Density rules use flat weights
#: because matching many candidates IS the strategy (Talrand wants
#: every instant). But weights must stay comparable to IDF-weighted
#: rules (median IDF ≈ 0.09–0.15) so density doesn't drown synergy.
#:
#: etb_self uses size-based dampening (see _compute_idf_weights) rather
#: than a flat override: large Creature groups (N=17k) are dampened so
#: synergy rules can compete, while small groups (Land N=1.1k) keep
#: full weight of 1.0.
#:
#: Source-of-truth: ``src/mtg_synergy_graph/data/scoring_weights.json``. Per-key tuning
#: history lives in commit messages and ``docs/RULE_HISTORY.md``.
_FLAT_WEIGHT_OVERRIDES: dict[str, float] = _LOADED_SCORING_WEIGHTS.flat_weight_overrides

#: Quality multiplier applied to IDF weights. Dampens broad effect-only
#: rules and enabler-on-enabler trigger-trigger matches.
#:
#: Source-of-truth: ``src/mtg_synergy_graph/data/scoring_weights.json``. Per-key tuning
#: history lives in commit messages and ``docs/RULE_HISTORY.md``.
_RULE_QUALITY_MULTIPLIER: dict[str, float] = _LOADED_SCORING_WEIGHTS.rule_quality_multiplier


@contextmanager
def patched_rule_quality_multiplier(weights: Mapping[str, float]) -> Iterator[None]:
    """Temporarily replace ``_RULE_QUALITY_MULTIPLIER`` with ``weights``; restore on exit.

    The bench optimizer scores grid cells by patching the global
    ``_RULE_QUALITY_MULTIPLIER`` dict, calling
    :func:`score_from_complements`, and restoring the baseline. The
    ``.clear() + .update()`` pattern preserves the dict's identity (so
    callers holding a reference to the global keep seeing live values).

    Identity preservation matters because:

    * ``compute_config_hash`` and ``ScoringConfigInputs`` read the
      module-level global by attribute access; rebinding to a new dict
      would silently break observers.
    * Test fixtures patch the global to install synthetic weights and
      rely on the same dict object surviving across the fixture's yield.

    The context manager consolidates the four-line ``baseline = dict(...);
    try: clear+update; finally: clear+update`` pattern previously
    duplicated across ``_score_split``, ``_proposed_config_hash``, the
    ``patched_baseline_weights`` test fixture, and several individual
    tests. One source of truth means future refactors (e.g., a future
    atomic single-key swap) only touch this function.

    Usage::

        with patched_rule_quality_multiplier(weights):
            score_from_complements(conn, [cmdr], complements)

    Restores even if the body raises (try/finally semantics).
    """
    baseline = dict(_RULE_QUALITY_MULTIPLIER)
    try:
        _RULE_QUALITY_MULTIPLIER.clear()
        _RULE_QUALITY_MULTIPLIER.update(weights)
        yield
    finally:
        _RULE_QUALITY_MULTIPLIER.clear()
        _RULE_QUALITY_MULTIPLIER.update(baseline)


#: Plan 2026-07-02-001 (color-conditioned IDF probe). When True, the IDF
#: denominator N for non-flat keys counts only candidates in the
#: commander's color-identity-legal pool (``_color_legal_pool``) instead
#: of the color-unfiltered matched set. Default False — the flag-off
#: path is bitwise-identical to the pre-probe scorer (proven by
#: ``bench.py audit --expect-identity``). Flip procedure (plan Unit 4):
#: set True AND register the value in ``ScoringConfigInputs`` so
#: ``compute_config_hash`` flips. Tests toggle via
#: ``mock.patch.object(universal_scorer, "_ENABLE_COLOR_CONDITIONED_IDF", True)``
#: — the flag is read at call time inside ``maybe_color_legal_pool``,
#: never snapshotted by a ``from``-import.
_ENABLE_COLOR_CONDITIONED_IDF: bool = False


def _color_legal_pool(
    conn: sqlite3.Connection,
    commander_set: Sequence[str],
    candidate_cache: CandidateCache | None = None,
) -> frozenset[str]:
    """Card names legal in the commander's color-identity pool.

    Mirrors the ``engine.page()`` / ``legal_cards()`` predicate exactly
    (all three conditions conjoined): candidate colour identity ⊆ the
    identity UNION over the full ``commander_set``, AND a non-empty
    top-level type not in ``NON_EDH_CARD_TYPES``, AND
    ``legal_commander != 0``. The queried commanders themselves are
    excluded, mirroring ``page()``. Parity with ``legal_cards()`` is
    locked by ``tests/test_universal_scorer_color_idf.py`` — the R2a
    orphan policy is only sound while the pool ⊇ every ranked candidate.

    Raises ``ValueError`` on an unknown commander. Never silently
    returns an empty pool for a known commander set — an empty pool
    would orphan every key and reproduce unconditioned behaviour,
    masking the probe.
    """
    # Local import: engine.py imports this module at top level.
    from .engine import NON_EDH_CARD_TYPES

    names = list(commander_set)
    placeholders = ",".join("?" * len(names))
    cmdr_rows = conn.execute(
        f"SELECT name, color_identity FROM cards WHERE name IN ({placeholders})",
        tuple(names),
    ).fetchall()
    found = {r["name"] for r in cmdr_rows}
    missing = [n for n in names if n not in found]
    if missing:
        raise ValueError(f"unknown commander(s) for color-legal pool: {missing!r}")
    identity: set[str] = set()
    for r in cmdr_rows:
        identity.update(t.strip() for t in (r["color_identity"] or "").split(",") if t.strip())

    cmdr_names = set(names)
    pool: set[str] = set()
    if candidate_cache is not None:
        for name, row in candidate_cache.candidate_rows.items():
            if name in cmdr_names:
                continue
            cand_types = (row["card_types"] or "").split()
            if not cand_types or any(t in NON_EDH_CARD_TYPES for t in cand_types):
                continue
            if row.get("legal_commander") == 0:
                continue
            pips = {t.strip() for t in (row["color_identity"] or "").split(",") if t.strip()}
            if not (pips - identity):
                pool.add(name)
        return frozenset(pool)

    # SQL fallback (no cache — e.g. one-off score_one calls). Same
    # back-compat legal_commander shim as engine.legal_cards().
    has_legal = any(r[1] == "legal_commander" for r in conn.execute("PRAGMA table_info(cards)").fetchall())
    legal_expr = "legal_commander" if has_legal else "1 AS legal_commander"
    cur = conn.execute(
        f"SELECT name, color_identity, card_types, {legal_expr} FROM cards WHERE name NOT IN ({placeholders})",
        tuple(names),
    )
    for r in cur.fetchall():
        cand_types = (r["card_types"] or "").split()
        if not cand_types or any(t in NON_EDH_CARD_TYPES for t in cand_types):
            continue
        if r["legal_commander"] == 0:
            continue
        pips = {t.strip() for t in (r["color_identity"] or "").split(",") if t.strip()}
        if not (pips - identity):
            pool.add(r["name"])
    return frozenset(pool)


def maybe_color_legal_pool(
    conn: sqlite3.Connection,
    commander_set: Sequence[str],
    candidate_cache: CandidateCache | None = None,
) -> frozenset[str] | None:
    """Single flag-read point for color-conditioned IDF.

    Returns ``None`` when ``_ENABLE_COLOR_CONDITIONED_IDF`` is off.
    Both the scorer (``score_from_complements``) and the optimizer
    basis fill site (``bench/optimize.py::_score_split``) route through
    here, so one ``mock.patch`` of the flag on this module flips every
    call site together — no import-time snapshot can diverge.
    """
    if not _ENABLE_COLOR_CONDITIONED_IDF:
        return None
    return _color_legal_pool(conn, commander_set, candidate_cache)


@dataclass(frozen=True)
class IdfBasis:
    """Per-commander IDF basis: weight-independent components of ``_compute_idf_weights``.

    Splits the IDF computation into two parts:

    * **Weight-independent** (this dataclass): frequency counts, ``1/log2(1+N)``,
      ``cond_mult``, and the flat-rule overrides. Cacheable per commander.
    * **Weight-dependent** (:func:`_idf_weights_from_basis`): a single
      ``_RULE_QUALITY_MULTIPLIER`` lookup per non-flat key.

    The bench optimizer caches one ``IdfBasis`` per commander alongside its
    ``complements_cache`` and reuses it across every grid-cell evaluation.
    Per-cell cost drops from ``O(complements)`` frequency counting to
    ``O(unique_keys)`` multiplier lookup.

    Cache invalidation contract: ``IdfBasis`` depends on the complements list
    AND on ``_FLAT_WEIGHT_OVERRIDES`` (which the M1 optimizer does not sweep)
    AND on the static dampening logic (``_FLAT_COUNT_RULES``, panharmonicon
    floor, ``cond_mult``). It does NOT depend on ``_RULE_QUALITY_MULTIPLIER``.
    A future optimizer variant that sweeps ``_FLAT_WEIGHT_OVERRIDES`` MUST
    rebuild the basis on every override change.

    Attributes:
        flat_weights: Final IDF weights for ``_FLAT_COUNT_RULES`` keys
            (already include ``base_w * cond_mult``; no per-cell work).
        base_idf_non_flat: Pre-multiplier IDF weights for non-flat keys —
            ``(1/log2(1+N)) * cond_mult``. Multiply by
            ``_RULE_QUALITY_MULTIPLIER[rule_id]`` per cell.
    """

    flat_weights: Mapping[tuple[str, str, str, str], float]
    base_idf_non_flat: Mapping[tuple[str, str, str, str], float]


def _compute_idf_basis(
    complements: Sequence[PortComplement],
    legal_pool: frozenset[str] | None = None,
) -> IdfBasis:
    """Build the weight-independent IDF basis for one commander's complements.

    See :class:`IdfBasis` for the cache invariant. Mirrors the frequency
    counting + ``cond_mult`` + flat-override + log2 logic from
    :func:`_compute_idf_weights`, but stops short of applying
    ``_RULE_QUALITY_MULTIPLIER``.

    When ``legal_pool`` is given (plan 2026-07-02-001, color-conditioned
    IDF), non-flat N counts only candidates inside the pool. A key with
    zero in-pool matchers keeps its unconditioned N (R2a orphan policy:
    such keys cannot touch any ranked candidate — the pool ⊇ the ranked
    legal set — and falling back avoids both ``log2(1)`` division blowup
    and the implicit weight-1.0 default). ``None`` reproduces the
    unconditioned basis bitwise. NOTE the cache contract: a cached basis
    additionally depends on the pool it was built with — the optimizer's
    per-commander cache stays valid because the pool is a pure function
    of the commander set and DB snapshot.
    """
    freq: dict[tuple[str, str, str, str], set[str]] = defaultdict(set)
    for c in complements:
        key = (c.rule_id, c.cmdr_event, c.cand_event, c.filter_group)
        freq[key].add(c.candidate)

    flat_weights: dict[tuple[str, str, str, str], float] = {}
    base_idf_non_flat: dict[tuple[str, str, str, str], float] = {}
    for key, candidates in freq.items():
        rule_id = key[0]
        filter_group = key[3] or ""
        # Effect-conditional dampening: see _compute_idf_weights docstring.
        cond_mult = 0.5 if filter_group.endswith(":cond") else 1.0
        if rule_id in _FLAT_COUNT_RULES:
            override = _FLAT_WEIGHT_OVERRIDES.get(rule_id)
            base_w = override if override is not None else 1.0
            flat_weights[key] = base_w * cond_mult
        else:
            n = len(candidates)
            if legal_pool is not None:
                n_legal = len(candidates & legal_pool)
                if n_legal > 0:
                    n = n_legal
            cmdr_event = key[1]
            if rule_id == "panharmonicon" and "reverse" not in cmdr_event and "stack" not in cmdr_event:
                n = max(n, 30)
            base_idf_non_flat[key] = (1.0 / math.log2(1.0 + n)) * cond_mult
    return IdfBasis(
        flat_weights=flat_weights,
        base_idf_non_flat=base_idf_non_flat,
    )


def _idf_weights_from_basis(basis: IdfBasis) -> dict[tuple[str, str, str, str], float]:
    """Apply current ``_RULE_QUALITY_MULTIPLIER`` to a cached :class:`IdfBasis`.

    The hot-path inner-loop equivalent of :func:`_compute_idf_weights` when
    the caller has already paid the frequency-counting cost. ``flat_weights``
    pass through unchanged; non-flat keys get the per-rule multiplier.
    """
    result: dict[tuple[str, str, str, str], float] = dict(basis.flat_weights)
    for key, base_w in basis.base_idf_non_flat.items():
        rule_id = key[0]
        mult = _RULE_QUALITY_MULTIPLIER.get(rule_id, 1.0)
        result[key] = base_w * mult
    return result


def _compute_idf_weights(
    complements: Sequence[PortComplement],
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

    Refactored into :func:`_compute_idf_basis` + :func:`_idf_weights_from_basis`
    so the bench optimizer can cache the basis once per commander and only pay
    the cheap multiplier-lookup per grid cell. This wrapper is the legacy
    one-shot path used by ``score_all_universal``; bitwise-identical output
    to pre-refactor.
    """
    return _idf_weights_from_basis(_compute_idf_basis(complements))


def score_all_universal(
    conn: sqlite3.Connection,
    commander_set: Sequence[str],
    candidate_cache: CandidateCache | None = None,
    *,
    tensor_sink: TensorSink | None = None,
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

    When ``tensor_sink`` is provided, it is called once per
    ``(candidate, rule_id)`` pair after scoring completes with the
    net IDF-weighted contribution, the per-key IDF weight, and the
    raw count of distinct complement keys that fired. Default
    ``None`` keeps inference paths bitwise-identical — see the R7
    identity invariant in the plan.
    """
    complements = find_all_complements(conn, commander_set, candidate_cache=candidate_cache)
    return score_from_complements(
        conn,
        commander_set,
        complements,
        candidate_cache=candidate_cache,
        tensor_sink=tensor_sink,
    )


def score_from_complements(
    conn: sqlite3.Connection,
    commander_set: Sequence[str],
    complements: Sequence[PortComplement],
    *,
    candidate_cache: CandidateCache | None = None,
    tensor_sink: TensorSink | None = None,
    idf_basis: IdfBasis | None = None,
) -> dict[str, UniversalScore]:
    """Build per-candidate ``UniversalScore`` results from precomputed complements.

    Public counterpart to :func:`score_all_universal`: skip the
    ``find_all_complements`` step when the caller already has the
    complements list in hand. The bench optimizer
    (``bench/optimize.py``) caches complements per commander and calls
    this directly per grid cell with patched ``_RULE_QUALITY_MULTIPLIER``.

    Reads ``_RULE_QUALITY_MULTIPLIER``, ``_FLAT_WEIGHT_OVERRIDES``, and
    the embedding/staple/circuit/cmc/rank side-channels exactly as
    ``score_all_universal`` does.

    When ``idf_basis`` is supplied, skip the per-call frequency-counting walk
    and apply the current ``_RULE_QUALITY_MULTIPLIER`` to the cached basis
    instead. The optimizer caches one basis per commander (see
    :class:`IdfBasis`); production scoring leaves it ``None`` and recomputes.

    Refactor invariant: ``score_all_universal`` is a thin wrapper that calls
    ``find_all_complements`` then this helper; behavior is bitwise-identical
    to pre-extraction. Verified by the existing
    ``tests/bench/test_universal_scorer_identity.py`` suite and
    ``bench.py audit --expect-identity``.
    """
    if idf_basis is not None:
        # Optimizer path: the pool (if any) is already baked into the
        # cached basis at its fill site (bench/optimize.py).
        idf = _idf_weights_from_basis(idf_basis)
    else:
        _pool = maybe_color_legal_pool(conn, commander_set, candidate_cache)
        idf = _idf_weights_from_basis(_compute_idf_basis(complements, legal_pool=_pool))

    # Group complements by candidate
    by_candidate: dict[str, list[PortComplement]] = defaultdict(list)
    for c in complements:
        by_candidate[c.candidate].append(c)

    # Plan 2026-04-23-003 Unit 7 — embedding contribution wire.
    # Resolve the commander target + per-card vector lookup ONCE per
    # scoring call. When the flag is off (module default), both land on
    # empty / None and the per-candidate call in the results loop below
    # short-circuits to 0.0 — the scorer remains bitwise-identical to
    # pre-plan baseline. Local imports keep the embeddings subpackage
    # off the scorer's hot import path.
    from .embeddings.contribution import (
        _ENABLE_EMBEDDING_CONTRIBUTION,
        embedding_contribution,
        load_card_embeddings_verified,
    )

    if _ENABLE_EMBEDDING_CONTRIBUTION:
        # numpy-dependent import deferred behind the flag: the base
        # install (no [graph] extra) must be able to score rules-only.
        from .embeddings.commander_target import build_commander_target_vector

        _emb_vectors = load_card_embeddings_verified(conn)
        try:
            _emb_cmdr_target = (
                build_commander_target_vector(
                    tuple(commander_set),
                    _emb_vectors,
                    edhrec_conn=None,
                )
                if _emb_vectors
                else None
            )
        except Exception as exc:
            # Reliability R-004: the scoring init path must never raise.
            # Any failure in commander-target composition degrades to
            # "no embedding contribution for this run" so the rule-based
            # scorer continues unaffected.
            logger.warning(
                "build_commander_target_vector failed; disabling embedding contribution for this run: %s",
                exc,
            )
            _emb_cmdr_target = None
    else:
        _emb_vectors = {}
        _emb_cmdr_target = None

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
        # Plan 2026-04-23-003 Unit 7 — n_rules is the count of distinct
        # rules firing positively for this candidate. Matches
        # ``UniversalScore.distinct_rules`` by construction, but
        # inlined here because ``distinct_rules`` is a
        # ``@cached_property`` on the object we're about to build.
        n_rules_syn = len({c.rule_id for c in comps if c.direction == "synergy"})
        emb_contrib = embedding_contribution(
            candidate_name=name,
            n_rules=n_rules_syn,
            v_cmdr=_emb_cmdr_target,
            vectors=_emb_vectors,
        )
        results[name] = UniversalScore(
            complements=comps,
            staple_bonus=bonus,
            idf_weights=idf,
            circuit_bonus=0.05 if name in circuit_candidates else 0.0,
            cmc_bonus=0.01 * max(0.0, (7.0 - cmc)) / 7.0,
            rank_bonus=0.005 * max(0.0, 1.0 - rank / 30000.0),
            embedding_contribution=emb_contrib,
        )
    for name in staple_names:
        if name not in results and name not in set(commander_set):
            cmc = cmc_data.get(name, 99.0)
            rank = rank_data.get(name, 99999)
            # Staple-only candidates never accumulate complements, so
            # ``n_rules=0`` — the embedding term (if flag on) is the
            # undampened ``w_emb * cosine``. This is the intended
            # behavior: cold-start / unrepresented-rule candidates are
            # exactly where the embedding fallback is supposed to fire.
            emb_contrib = embedding_contribution(
                candidate_name=name,
                n_rules=0,
                v_cmdr=_emb_cmdr_target,
                vectors=_emb_vectors,
            )
            results[name] = UniversalScore(
                staple_bonus=0.01,
                idf_weights=idf,
                cmc_bonus=0.01 * max(0.0, (7.0 - cmc)) / 7.0,
                rank_bonus=0.005 * max(0.0, 1.0 - rank / 30000.0),
                embedding_contribution=emb_contrib,
            )

    if tensor_sink is not None:
        if len(commander_set) != 1:
            raise ValueError(
                f"tensor_sink requires a single-commander call; got {len(commander_set)} commanders in commander_set"
            )
        _emit_tensor_rows(commander_set[0], results, tensor_sink)

    return results


def _emit_tensor_rows(
    commander: str,
    results: dict[str, UniversalScore],
    sink: TensorSink,
) -> None:
    """Emit one sink call per (candidate, rule_id) with the net contribution.

    Mirrors the dedup rules in ``UniversalScore.score()``: each
    ``(rule_id, cmdr_event, cand_event, filter_group)`` key contributes
    at most once per direction, so a candidate with two redundant
    trigger_effect rows yields exactly one synergy contribution. Anti-
    synergy contributes with a negative sign. Zero-contribution
    (synergy fully cancelled by anti) rows are dropped so consumers
    don't store noise.

    Contributions emitted here are **pre-dampening** — the sum over
    (rule_id) does NOT equal ``UniversalScore.score()`` for candidates
    where one rule dominates >70% of the synergy total, because
    ``score()`` applies a concentration penalty on top. ``bench.py
    audit --rule RULE_ID`` surfaces these raw per-rule sums directly,
    which is the right answer for the question "how much did this rule
    contribute?" but only an approximation of the answer to "what would
    the score be without this rule?" (the naive answer assumes the
    concentration structure stays stable). If that distinction matters,
    re-score without the rule; the tensor is a fast O(1) lookup, the
    ground truth is the scorer.
    """
    for cand, score_obj in results.items():
        per_rule_contrib: dict[str, float] = defaultdict(float)
        per_rule_max_idf: dict[str, float] = defaultdict(float)
        per_rule_count: dict[str, int] = defaultdict(int)
        seen_syn: set[tuple[str, str, str, str]] = set()
        seen_anti: set[tuple[str, str, str, str]] = set()
        for c in score_obj.complements:
            key = (c.rule_id, c.cmdr_event, c.cand_event, c.filter_group)
            w = score_obj.idf_weights.get(key, 1.0)
            if c.direction == "synergy":
                if key in seen_syn:
                    continue
                seen_syn.add(key)
                per_rule_contrib[c.rule_id] += w
            else:
                if key in seen_anti:
                    continue
                seen_anti.add(key)
                per_rule_contrib[c.rule_id] -= w
            per_rule_count[c.rule_id] += 1
            if w > per_rule_max_idf[c.rule_id]:
                per_rule_max_idf[c.rule_id] = w
        for rule_id, contrib in per_rule_contrib.items():
            if contrib == 0.0:
                continue
            sink(
                TensorRow(
                    commander=commander,
                    candidate=cand,
                    rule_id=rule_id,
                    contribution=contrib,
                    idf_weight=per_rule_max_idf[rule_id],
                    raw_count=per_rule_count[rule_id],
                )
            )
