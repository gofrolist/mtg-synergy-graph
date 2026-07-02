"""Portfolio-selection support (plan 2026-07-02-004).

Unit 1: the committed ``rule_id -> family`` map artifact
(``src/mtg_synergy_graph/data/family_map.json``) and its strict loader.
The map is the single family authority for the selection layer
(``rules_seed.json``'s per-row ``family`` field is untouched — see the
plan's "Family-map authority" decision).

Unit 2: the shared score-decomposition helper
(:func:`decompose_universal_score`) and the greedy portfolio assembler
(:func:`assemble_portfolio`). The SAME helper pair is consumed by the
R0 simulation (``bench/portfolio_sim.py``), the future live selection
layer (Unit 5), and the R7b addressability readout — no sim/live
arithmetic drift by construction.

DECLINE status (2026-07-02): plan 2026-07-02-004 was DECLINED at the
R0 kill-test (see docs/solutions/best-practices/
portfolio-selection-null-result-2026-07-02.md). The "Unit 5" live
selection layer and "Unit 6" hash wiring referenced in this module are
UNFUNDED and never ran; the map + helpers are retained as inert data
infrastructure (the ``card_hints`` precedent) and as the standing
ranking-transform kill-test substrate (``bench/portfolio_sim.py``).

Scoring-module coupling is kept to *function-local* imports inside
:func:`decompose_universal_score` so the Unit-1 loader stays importable
without pulling the scorer (and so the flag/private symbols are read
live at call time, which keeps monkeypatching in tests effective).

The map artifact stays inert on the scoring path: nothing in
``engine.py`` reads it yet, and it is not folded into
``compute_config_hash`` (Unit 6 — UNFUNDED, see DECLINE note above).
Coverage of the authoring-time rule snapshot is enforced by
``tests/test_family_map.py``, NOT at load time — an unmapped rule_id
must not brick flag-OFF engine load (origin R4); the selection layer
hard-errors (:class:`UnmappedRuleFamilyError`) on unmapped rule_ids it
actually encounters.
"""

from __future__ import annotations

import heapq
import json
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING

from .port_graph._paths import default_seed_path

if TYPE_CHECKING:
    from .universal_scorer import UniversalScore

_FAMILY_MAP_FILENAME = "family_map.json"

#: ``_readme`` is prose for human readers (would have been excluded
#: from the seed digest by the Unit 6 hash wiring — UNFUNDED under the
#: 2026-07-02 DECLINE — mirroring the ``scoring_weights.json`` /
#: ``rules_seed.json`` convention).
_VALID_TOP_LEVEL_KEYS = frozenset({"_readme", "families"})


def load_family_map(path: Path | None = None) -> dict[str, str]:
    """Read the family-map seed JSON into a ``rule_id -> family`` dict.

    ``path`` defaults to the packaged seed
    (``default_seed_path("family_map.json")``); tests pass an explicit
    path to exercise the validation error paths against tmp_path
    artifacts.

    Strict shape validation — missing file, malformed JSON, non-object
    top level, unknown top-level keys, missing ``_readme`` /
    ``families``, non-list-of-strings ``_readme``, non-object
    ``families``, or non-string / empty family values all raise
    ``ValueError`` (never ``assert`` — stripped by ``python -O``).
    Every message names the offending path so the failure is actionable
    without grep.
    """
    p = Path(path) if path is not None else default_seed_path(_FAMILY_MAP_FILENAME)
    try:
        with p.open(encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError as exc:
        raise ValueError(
            f"family_map.json missing at {p}; this file is the single "
            "rule_id->family authority for portfolio selection and must "
            "exist. Restore it from git."
        ) from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"{p}: malformed JSON ({exc})") from exc

    if not isinstance(raw, dict):
        raise ValueError(f"{p}: top level must be a JSON object, got {type(raw).__name__}")

    extra_keys = set(raw) - _VALID_TOP_LEVEL_KEYS
    if extra_keys:
        raise ValueError(f"{p}: unknown top-level keys {sorted(extra_keys)} (valid: {sorted(_VALID_TOP_LEVEL_KEYS)})")
    missing_keys = _VALID_TOP_LEVEL_KEYS - set(raw)
    if missing_keys:
        raise ValueError(f"{p}: missing required top-level keys {sorted(missing_keys)}")

    readme = raw["_readme"]
    if not isinstance(readme, list) or not all(isinstance(line, str) for line in readme):
        raise ValueError(f"{p}: '_readme' must be a list of strings")

    families_raw = raw["families"]
    if not isinstance(families_raw, dict):
        raise ValueError(
            f"{p}: 'families' must be an object mapping rule_id to family, got {type(families_raw).__name__}"
        )

    families: dict[str, str] = {}
    for rule_id, family in families_raw.items():
        if not isinstance(family, str) or not family:
            raise ValueError(
                f"{p}: families.{rule_id}: family must be a non-empty string, got {family!r} ({type(family).__name__})"
            )
        families[rule_id] = family
    return families


#: Loaded once at import, mirroring ``_LOADED_SCORING_WEIGHTS``. A
#: malformed committed artifact fails the import loudly rather than
#: surfacing as silent flag-ON misbehavior later
#: (verify-from-stored-config learning, 2026-04-23).
FAMILY_MAP: dict[str, str] = load_family_map()


# ---------------------------------------------------------------------------
# Unit 2 §1 — shared score decomposition
# ---------------------------------------------------------------------------


class UnmappedRuleFamilyError(ValueError):
    """A complement fired for a rule_id absent from the family map.

    Raised by :func:`decompose_universal_score` — the selection layer's
    hard-error contract for unmapped rule_ids it encounters (origin R4).
    Flag-OFF paths never call the decomposition, so this can never brick
    a plain engine load.
    """


class FamilyReconciliationError(ValueError):
    """Family attribution failed to reconcile with the replayed total.

    Raised by :func:`decompose_universal_score` (via
    :func:`_reconcile_family_attribution`) when ``sum(family_syn)``
    does not match the synergy sum implied by ``(total, residual,
    anti_total, dampener)``, or when a ``family_syn_flat`` share
    exceeds its ``family_syn`` total. The λ=0 self-check in
    ``bench/portfolio_sim.py`` only validates the TOTAL replay; this
    check closes the attribution gap (adversarial P1) — a wrong
    per-family split with a correct total would otherwise pass every
    downstream identity check. Wrapped as ``SelfCheckError`` by the
    sim harness.
    """


def _reconcile_family_attribution(
    card: str,
    family_syn: Mapping[str, float],
    family_syn_flat: Mapping[str, float],
    *,
    total: float,
    anti_total: float,
    residual: float,
    dampener: float,
) -> None:
    """Assert the per-family attribution reconciles with the total.

    The replayed total satisfies ``total == dampener * syn - anti_total
    + residual`` (up to float reassociation), so the implied synergy
    sum is ``(total + anti_total - residual) / dampener`` and must
    match ``sum(family_syn.values())`` within float tolerance. Also
    checks ``family_syn_flat`` is a per-family subset of ``family_syn``
    (the flat share sums a subset of the same weights).
    """
    fam_sum = sum(family_syn.values())
    implied_syn = (total + anti_total - residual) / dampener
    if not math.isclose(fam_sum, implied_syn, rel_tol=1e-9, abs_tol=1e-12):
        raise FamilyReconciliationError(
            f"{card!r}: family attribution does not reconcile: sum(family_syn) = "
            f"{fam_sum!r} but (total + anti_total - residual) / dampener implies "
            f"{implied_syn!r} (total={total!r}, anti_total={anti_total!r}, "
            f"residual={residual!r}, dampener={dampener!r})"
        )
    for fam, flat_v in family_syn_flat.items():
        full_v = family_syn.get(fam)
        if full_v is None or (flat_v > full_v and not math.isclose(flat_v, full_v, rel_tol=1e-9, abs_tol=1e-12)):
            raise FamilyReconciliationError(
                f"{card!r}: family_syn_flat[{fam!r}] = {flat_v!r} is not a subset of "
                f"family_syn[{fam!r}] = {full_v!r} (flat share must never exceed the "
                "full family synergy)"
            )


#: Fallback tiebreak values mirroring ``engine.SynergyEngine.page()``:
#: ``cmc`` defaults to 99.0 and ``edhrec_rank`` to the unranked
#: sentinel (``engine.UNRANKED_EDHREC_SENTINEL == 10**9``). Duplicated
#: here (documented) instead of imported so ``portfolio`` never imports
#: ``engine`` — Unit 5 would have made ``engine`` import this module
#: (UNFUNDED under the 2026-07-02 DECLINE; the no-cycle discipline is
#: kept anyway).
_CMC_FALLBACK: float = 99.0
_RANK_FALLBACK: int = 10**9


@dataclass(frozen=True)
class ScoreDecomposition:
    """Per-candidate decomposition of the PRODUCTION page total.

    Produced by :func:`decompose_universal_score` from a finished
    ``UniversalScore``. The reference total is the page-side
    ``to_legacy_buckets()["total"]`` — the number
    ``SynergyEngine.page()`` sorts by — NOT ``UniversalScore.score``
    (the audit-fixture total, which under the current flag-OFF defaults
    additionally applies the legacy concentration dampener that the
    production total does not).

    Recomposition contract (λ=0 identity): ``total`` is replayed inside
    the helper with the exact operation sequence of
    ``to_legacy_buckets()`` (same dedup, same accumulation order, same
    term grouping), so it equals the production total BITWISE. The
    algebraic identity ``dampener * Σ family_syn − anti_total +
    residual == total`` also holds, but only up to float reassociation
    — order-sensitive comparisons must use ``total``.

    Attributes:
        family_syn: Per-family SYNERGY-ONLY contribution sums,
            undiscounted, grouped by the family map passed to the
            helper. Non-negative by construction on production data.
        family_syn_flat: The flat/density-rule share of ``family_syn``
            (rules in ``universal_scorer._FLAT_COUNT_RULES``) — the R0
            sweep's "discount flat share only" base variant.
        anti_total: Total anti-synergy (positive number; subtracts at
            full value outside the dampened term, mirroring
            ``score()``/``to_legacy_buckets()``).
        residual: staple + breadth bonus + pair bonus + circuit + cmc
            + rank_bonus + embedding — every non-rule term the
            production total adds, undiscounted passthrough.
        dampener: The frozen concentration factor AS APPLIED BY THE
            PRODUCTION TOTAL, computed on the undiscounted syn-only
            per-rule mix. Under the current flag-OFF defaults
            (``_ENABLE_CONCAVE_FAMILY_AGG = False``)
            ``to_legacy_buckets()`` applies NO dampening, so this is
            ``1.0``; under concave-ON it is
            ``_syn_concentration_factor(syn_by_rule, syn)`` exactly as
            the shared choke point computes it. The assembler reapplies
            this frozen factor to the DISCOUNTED synergy sum (semantics
            frozen per plan scope — never recomputed on the discounted
            mix).
        total: The exact production total (bitwise-equal replay of
            ``to_legacy_buckets()["total"]``).
        per_rule_net: Net per-rule contribution (synergy − anti per
            rule_id, zero-net rules dropped) — tensor-row-equivalent,
            mirroring ``_emit_tensor_rows`` dedup. Consumed by the
            hidden-gem plausibility gate in the sim.
        rank_bonus: Copy of ``UniversalScore.rank_bonus`` (already
            inside ``residual``/``total``) so the sweep's
            rank_bonus-ablated sidecar can subtract it without
            re-touching ``UniversalScore`` objects.
    """

    family_syn: Mapping[str, float]
    family_syn_flat: Mapping[str, float]
    anti_total: float
    residual: float
    dampener: float
    total: float
    per_rule_net: Mapping[str, float] = field(default_factory=dict)
    rank_bonus: float = 0.0

    def __post_init__(self) -> None:
        # Freeze the mappings (HiddenGemReport precedent) so the frozen
        # dataclass contract isn't violated by in-place dict mutation.
        for name in ("family_syn", "family_syn_flat", "per_rule_net"):
            object.__setattr__(self, name, MappingProxyType(dict(getattr(self, name))))


def decompose_universal_score(
    us: UniversalScore,
    family_map: Mapping[str, str],
) -> ScoreDecomposition:
    """Decompose a finished ``UniversalScore`` into the plan-pinned parts.

    Mirrors ``UniversalScore.to_legacy_buckets()`` exactly:

    * dedup on ``(rule_id, cmdr_event, cand_event, filter_group,
      direction)`` — each key contributes at most once per direction
      (the same effective dedup ``score()`` applies with its two
      per-direction 4-tuple sets);
    * synergy adds / anti-synergy subtracts the IDF weight
      (``idf_weights`` lookup with 1.0 default);
    * the concentration dampener participates only when
      ``_ENABLE_CONCAVE_FAMILY_AGG`` is ON (production-total
      semantics; flag read live so tests can patch it);
    * residual terms replayed in the exact same order: staple →
      breadth (``0.02 * min(n_rules - 1, 4)``) → pair bonus →
      ``circuit + cmc + rank`` (single expression, preserving float
      association) → embedding.

    Raises :class:`UnmappedRuleFamilyError` when any deduped complement
    (either direction) carries a rule_id absent from ``family_map``.
    """
    # Local import — see module docstring (keeps the Unit-1 loader free
    # of scoring imports; reads flags/private symbols live).
    from . import universal_scorer as _scorer

    idf_weights = us.idf_weights
    seen: set[tuple[str, str, str, str, str]] = set()
    syn = 0.0
    anti = 0.0
    total = 0.0
    syn_by_rule: dict[str, float] = {}
    anti_by_rule: dict[str, float] = {}
    for c in us.complements:
        key = (c.rule_id, c.cmdr_event, c.cand_event, c.filter_group, c.direction)
        if key in seen:
            continue
        seen.add(key)
        w = idf_weights.get((c.rule_id, c.cmdr_event, c.cand_event, c.filter_group), 1.0)
        if c.direction == "anti_synergy":
            anti += w
            total -= w
            anti_by_rule[c.rule_id] = anti_by_rule.get(c.rule_id, 0.0) + w
        else:
            syn += w
            total += w
            syn_by_rule[c.rule_id] = syn_by_rule.get(c.rule_id, 0.0) + w

    # Frozen dampener — production-total semantics. Under flag-OFF
    # defaults ``to_legacy_buckets`` applies NO dampening (the legacy
    # dampener lives only in ``score()``, which the page does not sort
    # by), so the factor is 1.0. Under concave-ON, mirror the shared
    # choke point bitwise, including the ``total -= (1 - factor) * syn``
    # replay form.
    dampener = 1.0
    if _scorer._ENABLE_CONCAVE_FAMILY_AGG:
        dampener = _scorer._syn_concentration_factor(syn_by_rule, syn)
        if dampener != 1.0:
            total -= (1.0 - dampener) * syn

    residual = 0.0
    if us.staple_bonus:
        total += us.staple_bonus
        residual += us.staple_bonus
    n_rules = len(us.distinct_rules)
    if n_rules >= 2:
        breadth = 0.02 * min(n_rules - 1, 4)
        total += breadth
        residual += breadth
    pair = _scorer._compute_pair_bonus(us.distinct_rules)
    total += pair
    residual += pair
    micro = us.circuit_bonus + us.cmc_bonus + us.rank_bonus
    total += micro
    residual += micro
    total += us.embedding_contribution
    residual += us.embedding_contribution

    family_syn: dict[str, float] = {}
    family_syn_flat: dict[str, float] = {}
    flat_rules = _scorer._FLAT_COUNT_RULES
    for rule_id, w in syn_by_rule.items():
        fam = family_map.get(rule_id)
        if fam is None:
            raise UnmappedRuleFamilyError(
                f"rule_id {rule_id!r} fired but has no entry in the family map; "
                "add it to src/mtg_synergy_graph/data/family_map.json (portfolio "
                "selection hard-errors on unmapped rule_ids — origin R4)"
            )
        family_syn[fam] = family_syn.get(fam, 0.0) + w
        if rule_id in flat_rules:
            family_syn_flat[fam] = family_syn_flat.get(fam, 0.0) + w
    for rule_id in anti_by_rule:
        if rule_id not in family_map:
            raise UnmappedRuleFamilyError(
                f"rule_id {rule_id!r} fired (anti_synergy) but has no entry in "
                "the family map; add it to src/mtg_synergy_graph/data/family_map.json"
            )

    per_rule_net: dict[str, float] = {}
    for rule_id in syn_by_rule.keys() | anti_by_rule.keys():
        net = syn_by_rule.get(rule_id, 0.0) - anti_by_rule.get(rule_id, 0.0)
        if net != 0.0:
            per_rule_net[rule_id] = net

    # Attribution self-check (adversarial P1): runs for EVERY
    # decomposition — the λ=0 identity check downstream only validates
    # the total, never the per-family split.
    card = us.complements[0].candidate if us.complements else "<no-complements>"
    _reconcile_family_attribution(
        card,
        family_syn,
        family_syn_flat,
        total=total,
        anti_total=anti,
        residual=residual,
        dampener=dampener,
    )

    return ScoreDecomposition(
        family_syn=family_syn,
        family_syn_flat=family_syn_flat,
        anti_total=anti,
        residual=residual,
        dampener=dampener,
        total=total,
        per_rule_net=per_rule_net,
        rank_bonus=us.rank_bonus,
    )


# ---------------------------------------------------------------------------
# Unit 2 §2 — greedy portfolio assembler
# ---------------------------------------------------------------------------

#: ``g(mass, lam) -> multiplier in (0, 1]``. Must be non-increasing in
#: ``mass`` with ``g(mass, 0) == 1.0`` and ``g(0, lam) == 1.0`` — the
#: lazy-greedy heap relies on effective scores never increasing as
#: family mass accumulates, and the λ=0 identity limit relies on the
#: correction term vanishing exactly.
DecayFn = Callable[[float, float], float]


def _decay_exp(mass: float, lam: float) -> float:
    return math.exp(-lam * mass)


def _decay_harmonic(mass: float, lam: float) -> float:
    return 1.0 / (1.0 + lam * mass)


#: Named decay forms for the R0 sweep. Pluggable: callers may pass any
#: ``DecayFn`` directly to :func:`assemble_portfolio` (probed by
#: :func:`_validate_custom_decay` before use).
DECAY_FORMS: dict[str, DecayFn] = {
    "exp": _decay_exp,
    "harmonic": _decay_harmonic,
}

#: Masses at which a caller-supplied ``DecayFn`` is probed.
_DECAY_PROBE_MASSES: tuple[float, ...] = (0.0, 1.0, 10.0)


def _validate_custom_decay(g: DecayFn, lam: float) -> None:
    """Probe a caller-supplied decay callable — lazy-greedy validity.

    The lazy-greedy heap is only correct when effective scores never
    increase as family mass accumulates, and the λ=0 / zero-mass
    identity limit requires the correction term to vanish exactly.
    Concretely: ``g(0, λ) == 1.0``, values lie in ``(0, 1]``, and
    ``g`` is non-increasing in mass. A three-point probe cannot PROVE
    monotonicity, but it rejects the obviously invalid shapes before
    they silently corrupt an assembly. Shipped named forms skip the
    probe (they satisfy the contract by construction).
    """
    probes = [g(mass, lam) for mass in _DECAY_PROBE_MASSES]
    if probes[0] != 1.0:
        raise ValueError(f"custom decay must satisfy g(0, lam) == 1.0 (identity limit), got {probes[0]!r}")
    for mass, value in zip(_DECAY_PROBE_MASSES, probes, strict=True):
        if not 0.0 < value <= 1.0:
            raise ValueError(f"custom decay values must lie in (0, 1]; g({mass}, {lam}) = {value!r}")
    if probes[0] < probes[1] or probes[1] < probes[2]:
        raise ValueError(
            f"custom decay must be non-increasing in mass (lazy-greedy validity); "
            f"probe values at masses {_DECAY_PROBE_MASSES} were {probes!r}"
        )


#: Which per-family synergy share the discount applies to (R0 sweep
#: discount-base variants): the full family synergy or only its
#: flat/density-rule share.
DISCOUNT_BASES: tuple[str, ...] = ("full", "flat")


@dataclass(frozen=True)
class PortfolioPick:
    """One selected card with the effective score it was picked at.

    The undiscounted production total is available from the caller's
    ``decompositions[card].total`` — not duplicated here.
    """

    card: str
    effective_score: float


@dataclass(frozen=True)
class AssemblyResult:
    """Output of :func:`assemble_portfolio`."""

    picks: tuple[PortfolioPick, ...]
    #: Final accumulated positive family mass after the last pick.
    family_mass: Mapping[str, float]

    def __post_init__(self) -> None:
        object.__setattr__(self, "family_mass", MappingProxyType(dict(self.family_mass)))

    @property
    def cards(self) -> tuple[str, ...]:
        return tuple(p.card for p in self.picks)


def assemble_portfolio(
    decompositions: Mapping[str, ScoreDecomposition],
    *,
    lam: float,
    decay: str | DecayFn = "exp",
    k: int = 30,
    cmc_lookup: Mapping[str, float],
    rank_lookup: Mapping[str, int],
    discount_base: str = "full",
) -> AssemblyResult:
    """Greedy top-``k`` selection with per-family diminishing returns.

    Pure function (no scoring imports, no I/O). For each candidate the
    effective score is::

        eff = total - dampener * Σ_f [syn_f > 0] · syn_f · (1 - g(mass_f, λ))

    which is algebraically ``dampener · Σ_f syn_f · g(mass_f, λ)
    (positive syn discounted, negatives passed through) - anti +
    residual``, but anchored on the exact production ``total`` so that
    at λ=0 (or before any mass accumulates) ``eff == total`` BITWISE —
    the identity-limit guarantee. Anti-synergy and the residual are
    never discounted; the frozen ``dampener`` is reapplied to the
    discounted synergy sum.

    Picks maximise ``eff`` with the production 4-key tiebreak
    ``(-eff, cmc, edhrec_rank, name)`` (caller supplies the lookups;
    missing entries fall back to the ``page()`` sentinels). After each
    pick, ``mass_f += max(syn_f, 0)`` over the discount-base vector of
    the picked card (negative family nets are excluded from mass).

    ``discount_base``: ``"full"`` discounts each family's full synergy
    sum; ``"flat"`` discounts (and accumulates mass from) only the
    flat/density-rule share (``family_syn_flat``).

    λ=0 reproduces the input's production ordering; pools smaller than
    ``k`` select everything. Implemented as lazy greedy (effective
    scores are non-increasing in mass for the shipped decay forms), so
    cost is ~``O(N log N + k · resifts)`` instead of ``O(N · k)``.
    """
    if lam < 0:
        raise ValueError(f"lam must be >= 0, got {lam}")
    if k <= 0:
        raise ValueError(f"k must be > 0, got {k}")
    if discount_base not in DISCOUNT_BASES:
        raise ValueError(f"discount_base must be one of {DISCOUNT_BASES}, got {discount_base!r}")
    if isinstance(decay, str):
        try:
            g = DECAY_FORMS[decay]
        except KeyError:
            raise ValueError(f"unknown decay form {decay!r} (known: {sorted(DECAY_FORMS)})") from None
    else:
        g = decay
        _validate_custom_decay(g, lam)

    def vec(d: ScoreDecomposition) -> Mapping[str, float]:
        return d.family_syn if discount_base == "full" else d.family_syn_flat

    mass: dict[str, float] = {}

    def key_for(name: str) -> tuple[float, float, float, str]:
        d = decompositions[name]
        corr = 0.0
        for fam, v in vec(d).items():
            if v > 0.0:
                m = mass.get(fam, 0.0)
                if m:
                    gf = g(m, lam)
                    if gf != 1.0:
                        corr += v * (1.0 - gf)
        eff = d.total - d.dampener * corr if corr else d.total
        return (
            -eff,
            cmc_lookup.get(name, _CMC_FALLBACK),
            float(rank_lookup.get(name, _RANK_FALLBACK)),
            name,
        )

    # Initial keys: mass is empty, so eff == total for every candidate
    # (no per-family loop needed — key_for short-circuits on m == 0).
    heap: list[tuple[tuple[float, float, float, str], str]] = [(key_for(name), name) for name in decompositions]
    heapq.heapify(heap)

    picks: list[PortfolioPick] = []
    while heap and len(picks) < k:
        stale_key, name = heapq.heappop(heap)
        current_key = key_for(name)
        if current_key != stale_key:
            # Mass grew for one of this card's families since the key
            # was computed — re-sift. Lazy-greedy validity: keys only
            # worsen (eff non-increasing), so a fresh key that still
            # tops the heap is the true argmax.
            heapq.heappush(heap, (current_key, name))
            continue
        d = decompositions[name]
        picks.append(PortfolioPick(card=name, effective_score=-current_key[0]))
        for fam, v in vec(d).items():
            if v > 0.0:
                mass[fam] = mass.get(fam, 0.0) + v

    return AssemblyResult(picks=tuple(picks), family_mass=mass)
