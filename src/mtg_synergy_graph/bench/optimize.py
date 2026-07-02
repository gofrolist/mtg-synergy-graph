"""Coordinate Ascent weight optimizer for ``_RULE_QUALITY_MULTIPLIER``.

Foothold M1 of the tensor-driven weight optimizer plan
(``docs/plans/2026-04-26-001-feat-tensor-weight-optimizer-plan.md``).

This module is organized by plan unit. Sections appear in source order;
Unit 5 precedes Unit 4 because the CLI handler (Unit 4) consumes the
output writers (Unit 5):

* **Unit 1 — Composite objective + random split + EDHREC label loader.**
  Pure helper functions over commander lists and EDHREC label dicts.
  Independent of which rules fire.
* **Unit 2 — Cached-complements scorer.** Production-faithful per-grid-cell
  scoring using cached ``find_all_complements`` + re-IDF + ``UniversalScore``.
* **Unit 3 — Coordinate Ascent driver.** The optimizer's main loop:
  alphabetical sweep over rule keys, multiplicative grid, accept/reject
  gates, cumulative-drift revert, planted-perturbation self-test.
* **Unit 5 — Output artifacts.** ``.audit/optimize_proposal.json`` +
  ``.audit/optimize_history.csv`` writers. Defined before Unit 4 so the
  CLI handler can call them without forward-reference shenanigans.
* **Unit 4 — CLI handler.** Wires ``bench.py audit --optimize`` into the
  existing dispatcher; tensor-staleness precondition.

The optimizer never mutates ``src/mtg_synergy_graph/data/scoring_weights.json`` — it emits a
candidate diff for human review. Application is human-driven via
``bench.py audit --repin --yes``.
"""

from __future__ import annotations

import argparse
import logging
import random
import sqlite3
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING

from mtg_synergy_graph.complement_rules import find_all_complements
from mtg_synergy_graph.engine import UNRANKED_EDHREC_SENTINEL
from mtg_synergy_graph.penalties import build_candidate_cache
from mtg_synergy_graph.universal_scorer import (
    _compute_idf_basis,
    _compute_pair_bonus,
    patched_rule_quality_multiplier,
    score_from_complements,
)
from mtg_synergy_graph.validate import compute_ndcg

_logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from mtg_synergy_graph.complement_rules import PortComplement
    from mtg_synergy_graph.universal_scorer import (
        CandidateCache,
        IdfBasis,
        UniversalScore,
    )

# ---------------------------------------------------------------------------
# Unit 1: Composite α-blended objective
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CompositeObjective:
    """Composite α-blended objective value + per-axis breakdown.

    ``composite = α · mean_ndcg + (1 - α) · gem_rate`` over commanders that
    contribute to each axis. ``gem_rate`` is ``None`` when no commander in
    the input set has EDHREC data; in that case the gem term contributes
    zero and the composite collapses to ``α · mean_ndcg``.

    Attributes:
        composite: Final blended score in [0, 1].
        mean_ndcg: Arithmetic mean per-commander nDCG@30 over the input
            commanders. Always populated.
        gem_rate: Mean ``hidden_gem_hit_rate`` over commanders with
            EDHREC data; ``None`` when no commander has data.
        n_commanders: Total commanders in the input set.
        n_commanders_with_gem: Subset of ``n_commanders`` for whom the
            gem metric was computable (EDHREC data present).
    """

    composite: float
    mean_ndcg: float
    gem_rate: float | None
    n_commanders: int
    n_commanders_with_gem: int


def composite_objective(
    per_commander_ndcg: Mapping[str, float],
    per_commander_gem: Mapping[str, float | None],
    alpha: float,
) -> CompositeObjective:
    """Blend per-commander nDCG and hidden-gem rate into a composite score.

    The two input mappings need not have identical keys: a commander
    appearing only in ``per_commander_ndcg`` contributes to the nDCG mean
    but not the gem mean. A commander whose gem entry is ``None``
    (EDHREC data absent) is also excluded from the gem mean.

    Raises:
        ValueError: ``alpha`` is outside ``[0, 1]``.
    """
    if not 0.0 <= alpha <= 1.0:
        raise ValueError(f"alpha must be in [0, 1], got {alpha}")

    n_commanders = len(per_commander_ndcg)
    if n_commanders == 0:
        return CompositeObjective(
            composite=0.0,
            mean_ndcg=0.0,
            gem_rate=None,
            n_commanders=0,
            n_commanders_with_gem=0,
        )

    mean_ndcg = sum(per_commander_ndcg.values()) / n_commanders

    gem_values = [v for v in per_commander_gem.values() if v is not None]
    n_with_gem = len(gem_values)
    if n_with_gem == 0:
        gem_rate: float | None = None
        composite = alpha * mean_ndcg
    else:
        gem_rate = sum(gem_values) / n_with_gem
        composite = alpha * mean_ndcg + (1.0 - alpha) * gem_rate

    return CompositeObjective(
        composite=composite,
        mean_ndcg=mean_ndcg,
        gem_rate=gem_rate,
        n_commanders=n_commanders,
        n_commanders_with_gem=n_with_gem,
    )


# ---------------------------------------------------------------------------
# Unit 1: Random fixed-seed train/held split
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SplitResult:
    """Train/held commander split + per-color-identity bucket counts.

    The plan revision dropped archetype stratification (no data source
    in ``data/tags.db``); this random split + post-hoc color-identity
    reporting is the substitute. Bucket counts let the human reviewer
    spot obvious skew in the proposal output. Stratification is NOT
    enforced — the buckets are reported, not gated.
    """

    train: tuple[str, ...]
    held: tuple[str, ...]
    color_buckets: Mapping[str, Mapping[str, int]]


_COLOR_BUCKETS = ("colorless", "mono", "2c", "3c+")


def _color_bucket(color_identity: str | None) -> str:
    """Map ``cards.color_identity`` to a coarse bucket.

    Recognised forms: ``""``/``None`` → ``colorless``, ``"R"`` →
    ``mono``, ``"R,U"`` → ``2c``, ``"R,U,B"`` and wider → ``3c+``.
    """
    if not color_identity:
        return "colorless"
    colors = [c.strip() for c in color_identity.split(",") if c.strip()]
    n = len(colors)
    if n == 0:
        return "colorless"
    if n == 1:
        return "mono"
    if n == 2:
        return "2c"
    return "3c+"


def random_split(
    commanders: Sequence[str],
    conn: sqlite3.Connection | None,
    *,
    train_ratio: float = 0.8,
    seed: int,
) -> SplitResult:
    """Deterministic random 80/20 split with color-identity bucket reporting.

    Uses ``random.Random(seed).shuffle`` for determinism. The conn argument
    is read-only — it queries ``cards.color_identity`` for the bucket
    report. When ``conn`` is ``None`` all commanders fall into the
    ``colorless`` bucket (the report is informational only, so this is
    safe for unit tests that don't need real DB seeding).

    Raises:
        ValueError: ``train_ratio`` is outside ``(0, 1)``.
    """
    if not 0.0 < train_ratio < 1.0:
        raise ValueError(f"train_ratio must be in (0, 1), got {train_ratio}")

    rng = random.Random(seed)  # noqa: S311 — determinism, not crypto
    shuffled = list(commanders)
    rng.shuffle(shuffled)
    cut = round(len(shuffled) * train_ratio)
    train = tuple(shuffled[:cut])
    held = tuple(shuffled[cut:])

    color_by_cmdr: dict[str, str | None] = {}
    if conn is not None and commanders:
        # Mirrors the established placeholder pattern in penalties.py:
        # split SELECT / FROM / WHERE across adjacent string literals so
        # ruff S608 doesn't fire on the .format(?,?,...) IN-list expansion.
        rows = conn.execute(
            "SELECT name, color_identity FROM cards WHERE name IN ({})".format(",".join("?" * len(commanders))),
            tuple(commanders),
        ).fetchall()
        for row in rows:
            # Row may be a sqlite3.Row (dict-like) or a plain tuple; index
            # by position so both work.
            color_by_cmdr[row[0]] = row[1]

    buckets: dict[str, dict[str, int]] = {b: {"train": 0, "held": 0} for b in _COLOR_BUCKETS}
    for name in train:
        buckets[_color_bucket(color_by_cmdr.get(name))]["train"] += 1
    for name in held:
        buckets[_color_bucket(color_by_cmdr.get(name))]["held"] += 1

    # Drop empty buckets so the report stays focused. Wrap each inner dict in
    # MappingProxyType so SplitResult.color_buckets is fully immutable through
    # both layers (the outer Mapping[str, ...] alone wouldn't prevent
    # `result.color_buckets["mono"]["train"] = 99`).
    non_empty = {k: MappingProxyType(v) for k, v in buckets.items() if v["train"] or v["held"]}

    return SplitResult(
        train=train,
        held=held,
        color_buckets=MappingProxyType(non_empty),
    )


# ---------------------------------------------------------------------------
# Unit 1: EDHREC label loader
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EdhrecLabels:
    """Per-commander EDHREC inputs for the composite objective.

    ``graded_labels`` follows ``weight_grid_search.py``'s shape: 3.0 for
    cards in the ``High Synergy Cards`` section, 1.0 for cards in any
    other on-page section, 0 (absent) elsewhere. Used by ``compute_ndcg``.

    ``top_30_set`` is the EDHREC top-30 high-synergy-cards set used as
    the ``edhrec_top_30`` argument to ``hidden_gem_hit_rate_for_commander``.
    ``None`` when EDHREC has no high-synergy data for this commander —
    the documented "skip gem metric" sentinel.
    """

    graded_labels: Mapping[str, float]
    top_30_set: frozenset[str] | None


def load_edhrec_labels(
    edhrec_conn: sqlite3.Connection,
    commander: str,
) -> EdhrecLabels:
    """Build graded labels + top-30 set for one commander from EDHREC.

    Reads the ``edhrec_card_synergy`` table. The slug-conversion is
    handled internally by the helpers we delegate to. Local imports
    avoid pulling ``validate`` (and its eager ``engine`` import) into
    the module-load path.
    """
    from mtg_synergy_graph.edhrec_helpers import fetch_high_synergy_top_n
    from mtg_synergy_graph.validate import _fetch_edhrec_sections, commander_to_slug

    slug = commander_to_slug(commander)
    sections = _fetch_edhrec_sections(edhrec_conn, slug)
    hi_syn = sections.get("High Synergy Cards", set())
    on_page: set[str] = set()
    for cards in sections.values():
        on_page |= cards

    graded: dict[str, float] = {}
    for card in hi_syn:
        graded[card] = 3.0
    for card in on_page - hi_syn:
        graded[card] = 1.0

    top_30 = fetch_high_synergy_top_n(edhrec_conn, commander, limit=30)
    return EdhrecLabels(
        graded_labels=MappingProxyType(graded),
        top_30_set=frozenset(top_30) if top_30 is not None else None,
    )


# ---------------------------------------------------------------------------
# Unit 2: Cached-complements scorer (re-IDF + production-faithful re-score)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CommanderScoreResult:
    """Top-30 ranking + auxiliary outputs for one commander under specific weights.

    Produced by :func:`score_commander_from_complements`. The score values
    in ``score_by_candidate`` are ``UniversalScore.to_legacy_buckets()["total"]``
    — the same field ``engine.SynergyEngine.page()`` sorts by in production,
    so optimizer rankings match what ``recommend.py`` returns.

    Attributes:
        top_30: Top-30 candidate names in production sort order
            ``(-total, cmc, edhrec_rank, name)``.
        score_by_candidate: Frozen mapping of every reached candidate to
            its production-faithful score.
        contributions: Tuple of ``(candidate, rule_id, contribution)``
            tuples in the shape that ``hidden_gem_hit_rate_for_commander``
            expects. One entry per ``(candidate, rule_id)`` pair with a
            non-zero net IDF-weighted contribution. Mirrors the dedup
            rules in ``UniversalScore.score`` and ``_emit_tensor_rows``.
    """

    top_30: tuple[str, ...]
    score_by_candidate: Mapping[str, float]
    contributions: tuple[tuple[str, str, float], ...]


def _fast_total_and_contribs(
    score_obj: UniversalScore,
) -> tuple[float, dict[str, float]]:
    """Compute total + per-rule contributions in a single walk over complements.

    Fused version of the previous two-pass pattern (``_fast_total`` followed
    by ``_build_contributions``). Both passes deduped the same complements
    against the same key tuple — this fuses them so the per-candidate work
    is O(complements) once instead of twice.

    Per the 2026-04-30 cProfile run
    (``docs/solutions/best-practices/optimizer-perf-profile-2026-04-30.md``),
    ``_build_contributions`` was 9.9% of total run time on its own walk;
    fusing eliminates the redundant pass. Bitwise-identical to the legacy
    ``_fast_total`` + ``_build_contributions`` pair, verified by the
    ``test_production_faithful_top_30_at_baseline`` fidelity test plus
    ``bench.py audit --expect-identity`` over the 100-commander golden set.

    Returns:
        ``(total, per_rule)``:
        * ``total``: the sort key produced by
          ``UniversalScore.to_legacy_buckets()["total"]``.
        * ``per_rule``: ``rule_id -> net IDF-weighted contribution``,
          synergy positive and anti-synergy negative, each
          ``(rule_id, cmdr_event, cand_event, filter_group, direction)``
          deduped exactly once. Zero-net entries are NOT dropped here —
          the caller filters them when assembling the contributions tuple.
    """
    total = 0.0
    seen: set[tuple[str, str, str, str, str]] = set()
    distinct_rules: set[str] = set()
    per_rule: dict[str, float] = {}
    idf_weights = score_obj.idf_weights
    for c in score_obj.complements:
        key = (c.rule_id, c.cmdr_event, c.cand_event, c.filter_group, c.direction)
        if key in seen:
            continue
        seen.add(key)
        weight = idf_weights.get((c.rule_id, c.cmdr_event, c.cand_event, c.filter_group), 1.0)
        if c.direction == "anti_synergy":
            total -= weight
            per_rule[c.rule_id] = per_rule.get(c.rule_id, 0.0) - weight
        else:
            total += weight
            distinct_rules.add(c.rule_id)
            per_rule[c.rule_id] = per_rule.get(c.rule_id, 0.0) + weight

    total += score_obj.staple_bonus
    n_rules = len(distinct_rules)
    if n_rules >= 2:
        total += 0.02 * min(n_rules - 1, 4)
    total += _compute_pair_bonus(frozenset(distinct_rules))
    total += score_obj.circuit_bonus + score_obj.cmc_bonus + score_obj.rank_bonus
    total += score_obj.embedding_contribution
    return total, per_rule


def _fast_total(score_obj: UniversalScore) -> float:
    """Compute ``UniversalScore.to_legacy_buckets()["total"]`` without the bucket dict.

    Thin wrapper kept for the bitwise-fidelity test
    (``test_production_faithful_top_30_at_baseline``) and any future caller
    that needs only the scalar total. The hot path inside
    ``score_commander_from_complements`` calls
    :func:`_fast_total_and_contribs` directly to fuse the per-rule
    contribution walk into the same pass.
    """
    return _fast_total_and_contribs(score_obj)[0]


def score_commander_from_complements(
    conn: sqlite3.Connection,
    commander: str,
    complements: Sequence[PortComplement],
    *,
    candidate_cache: CandidateCache | None = None,
    idf_basis: IdfBasis | None = None,
) -> CommanderScoreResult:
    """Score one commander given precomputed complements.

    Reads the live ``_RULE_QUALITY_MULTIPLIER`` and ``_FLAT_WEIGHT_OVERRIDES``
    globals — the caller is responsible for patching them via
    ``.clear() + .update(weights)`` in a ``try/finally`` BEFORE this call
    if grid-cell weights differ from baseline. ``score_from_complements``
    handles the staple/circuit/cmc/rank/embedding side-channels exactly
    as ``score_all_universal`` does.

    When ``idf_basis`` is supplied, the inner ``_compute_idf_weights`` walk
    is replaced with a cheap ``_RULE_QUALITY_MULTIPLIER`` lookup over the
    cached basis. The basis is weight-independent (see :class:`IdfBasis`),
    so the optimizer can build it once per commander and reuse across
    every grid cell.

    The returned ``score_by_candidate`` and ``top_30`` use
    ``to_legacy_buckets()["total"]`` — the production sort key from
    ``engine.SynergyEngine.page()`` — so the optimizer's view matches
    what ``recommend.py`` displays.
    """
    results = score_from_complements(
        conn,
        [commander],
        complements,
        candidate_cache=candidate_cache,
        idf_basis=idf_basis,
    )

    # Production sort key: (-total, cmc_bonus_inverse_proxy, edhrec_rank, name).
    # cmc_bonus and rank_bonus are already folded into total via to_legacy_buckets,
    # so we recover the underlying cmc and rank from the bonus fields. Since the
    # bonus formulas are deterministic (cmc_bonus = 0.01 * max(0, (7 - cmc)/7)),
    # the inverse map is unique up to ties — and ties tie-break to name anyway.
    # For clean fidelity we read cmc/rank from the conn or candidate_cache
    # directly, mirroring engine.SynergyEngine.page().
    if candidate_cache is not None:
        cmc_lookup = {n: cmc for n, (cmc, _) in candidate_cache.cmc_rank_map.items()}
        rank_lookup = {n: rank for n, (_, rank) in candidate_cache.cmc_rank_map.items()}
    else:
        cmc_lookup = {}
        rank_lookup = {}
        for row in conn.execute("SELECT name, cmc, edhrec_rank FROM cards"):
            cmc_lookup[row["name"]] = row["cmc"] if row["cmc"] is not None else 99.0
            rank_lookup[row["name"]] = (
                row["edhrec_rank"] if row["edhrec_rank"] is not None else UNRANKED_EDHREC_SENTINEL
            )

    score_by_candidate: dict[str, float] = {}
    sortable: list[tuple[str, float]] = []
    contributions_list: list[tuple[str, str, float]] = []
    for name, us in results.items():
        # Hot path: fused walk produces both the production-faithful total
        # AND the per-rule contributions in one pass over complements (was
        # two: _fast_total + _build_contributions). 9.9% of pre-fuse total
        # run time was the second walk per the 2026-04-30 cProfile run.
        total, per_rule = _fast_total_and_contribs(us)
        score_by_candidate[name] = total
        sortable.append((name, total))
        for rule_id, contrib in per_rule.items():
            if contrib != 0.0:
                contributions_list.append((name, rule_id, contrib))

    sortable.sort(
        key=lambda r: (
            -r[1],
            cmc_lookup.get(r[0], 99.0),
            rank_lookup.get(r[0], UNRANKED_EDHREC_SENTINEL),
            r[0],
        )
    )
    top_30 = tuple(name for name, _ in sortable[:30])

    contributions = tuple(contributions_list)

    return CommanderScoreResult(
        top_30=top_30,
        score_by_candidate=MappingProxyType(score_by_candidate),
        contributions=contributions,
    )


# ---------------------------------------------------------------------------
# Unit 3: Coordinate Ascent driver
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OptimizerConfig:
    """Configuration for ``run_optimizer``.

    All fields are keyword-only via the dataclass + the call sites that
    construct it. Defaults are the brainstorm/plan-revisions M1 values.
    """

    alpha: float = 0.5
    grid: tuple[float, ...] = (0.5, 0.75, 1.25, 1.5, 2.0)
    clamp_min: float = 0.01
    clamp_max: float = 5.0
    max_sweeps: int = 5
    wall_clock_seconds: float = 300.0
    eps_step: float = 0.005
    eps_cumulative: float = 0.005
    train_ratio: float = 0.8
    split_seed: int = 42
    self_test_seed: int = 7
    run_self_test: bool = True
    self_test_planted_mult: float = 2.0
    self_test_recovery_tolerance: float = 0.05  # ±5% of baseline value


@dataclass(frozen=True)
class OptimizerStep:
    """One attempted ``(rule_id, perturbation)`` step.

    Logged for every grid evaluation (best-perturbation-per-rule), accepted
    or rejected. ``reject_reason`` is empty when ``accepted`` is True.
    """

    sweep_n: int
    rule_id: str
    old_value: float
    new_value: float
    train_composite: float
    held_composite: float
    train_ndcg: float
    held_ndcg: float
    train_gem: float | None
    held_gem: float | None
    accepted: bool
    reject_reason: str


@dataclass(frozen=True)
class OptimizerResult:
    """Outcome of ``run_optimizer`` — feeds Unit 5's proposal artifact."""

    baseline_weights: Mapping[str, float]
    final_weights: Mapping[str, float]
    history: tuple[OptimizerStep, ...]
    n_iterations: int
    n_steps_accepted: int
    n_steps_rejected: int
    partial_sweep: bool
    dead_keys: tuple[str, ...]
    train_split: tuple[str, ...]
    held_split: tuple[str, ...]
    color_buckets: Mapping[str, Mapping[str, int]]
    train_composite_baseline: float
    held_composite_baseline: float
    train_ndcg_baseline: float
    held_ndcg_baseline: float
    train_gem_baseline: float | None
    held_gem_baseline: float | None
    train_composite_final: float
    held_composite_final: float
    train_ndcg_final: float
    held_ndcg_final: float
    train_gem_final: float | None
    held_gem_final: float | None


class OptimizerSelfTestFailed(RuntimeError):
    """Planted-perturbation self-test could not recover the baseline weight.

    The diagnostic message names the rule, the planted value, the recovered
    value, and the gate that prevented recovery (train, held-out, or
    convergence). When raised, the optimizer aborts before producing a
    proposal — calibration must be fixed before trusting any output.

    Invariant: this is raised ONLY by ``_planted_perturbation_self_test``,
    which runs once before the sweep loop begins. The ``handle_optimize``
    catch path (rc=3) relies on this — adding a sweep-phase raise site
    would route mid-run failures through rc=3 incorrectly.
    """


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _score_split(
    conn: sqlite3.Connection,
    edhrec_conn: sqlite3.Connection,
    commanders: Sequence[str],
    weights: Mapping[str, float],
    *,
    complements_cache: dict[str, list[PortComplement]],
    labels_cache: dict[str, EdhrecLabels],
    alpha: float,
    candidate_cache: CandidateCache | None = None,
    idf_basis_cache: dict[str, IdfBasis] | None = None,
) -> CompositeObjective:
    """Score a commander split with the given weights; return composite objective.

    Patches ``_RULE_QUALITY_MULTIPLIER`` for the duration of the call. The cache
    arguments are populated lazily on first miss; subsequent calls reuse them
    so ``find_all_complements`` and ``load_edhrec_labels`` each run once per
    commander per optimizer run.

    ``candidate_cache`` is the commander-independent cmc/edhrec_rank cache
    from :func:`penalties.build_candidate_cache`. Built once at run_optimizer
    entry and threaded through; without it, each grid-cell evaluation re-issues
    a full ``SELECT name, cmc, edhrec_rank FROM cards`` table scan, costing
    ~5x wall-clock on a 100-commander × 53-key × 5-grid sweep.

    ``idf_basis_cache`` is the per-commander IDF basis (frequency counts +
    log2 + cond_mult, all weight-independent). Populated lazily on first miss
    alongside ``complements_cache``; reused across every grid cell so the
    inner ``_compute_idf_weights`` walk is replaced by a per-key
    ``_RULE_QUALITY_MULTIPLIER`` lookup.
    """
    from mtg_synergy_graph.bench.hidden_gems import (
        hidden_gem_hit_rate_for_commander,
    )

    with patched_rule_quality_multiplier(weights):
        per_ndcg: dict[str, float] = {}
        per_gem: dict[str, float | None] = {}
        for cmdr in commanders:
            if cmdr not in complements_cache:
                complements_cache[cmdr] = find_all_complements(conn, [cmdr], candidate_cache=candidate_cache)
            comps = complements_cache[cmdr]
            if cmdr not in labels_cache:
                labels_cache[cmdr] = load_edhrec_labels(edhrec_conn, cmdr)
            labels = labels_cache[cmdr]

            # Lazy-fill the IDF basis for this commander on first miss. The
            # basis is weight-independent, so subsequent grid cells skip the
            # frequency-counting walk entirely.
            cmdr_basis: IdfBasis | None = None
            if idf_basis_cache is not None:
                if cmdr not in idf_basis_cache:
                    idf_basis_cache[cmdr] = _compute_idf_basis(comps)
                cmdr_basis = idf_basis_cache[cmdr]

            result = score_commander_from_complements(
                conn, cmdr, comps, candidate_cache=candidate_cache, idf_basis=cmdr_basis
            )
            ndcg = compute_ndcg(list(result.top_30), dict(labels.graded_labels))
            per_ndcg[cmdr] = ndcg

            if labels.top_30_set is not None:
                gem_entry = hidden_gem_hit_rate_for_commander(
                    cmdr,
                    list(result.top_30),
                    set(labels.top_30_set),
                    list(result.contributions),
                )
                per_gem[cmdr] = gem_entry.rate if gem_entry is not None else None
            else:
                per_gem[cmdr] = None

    return composite_objective(per_ndcg, per_gem, alpha)


def _warn_missing_edhrec_labels(labels_cache: Mapping[str, EdhrecLabels]) -> None:
    """Emit one aggregate warning if any commanders lack EDHREC top-30 data.

    Slug-mismatch / EDHREC-empty diagnostic. After both baseline scoring
    calls populate ``labels_cache`` for every commander, count those whose
    ``top_30_set`` is None (no high-synergy data — typically a slug mismatch
    or a brand-new commander not yet on EDHREC). If a meaningful fraction
    of the catalogue lacks data, the gem axis silently degrades to zero
    contribution. Warn once with the aggregate count instead of spamming
    per-commander.
    """
    missing_gem = [c for c, lbls in labels_cache.items() if lbls.top_30_set is None]
    if not missing_gem:
        return
    n_total = len(labels_cache)
    pct = (len(missing_gem) / n_total) * 100 if n_total else 0.0
    _logger.warning(
        "EDHREC labels missing for %d/%d commanders (%.1f%%) — gem axis will contribute nothing for these. Sample: %s",
        len(missing_gem),
        n_total,
        pct,
        ", ".join(sorted(missing_gem)[:5]) + ("..." if len(missing_gem) > 5 else ""),
    )


def _detect_dead_keys(
    complements_cache: Mapping[str, list[PortComplement]],
    baseline_weights: Mapping[str, float],
) -> list[str]:
    """Return rule_ids in baseline_weights that fire on zero commanders.

    Iterates the complements_cache populated by baseline scoring and
    collects every rule_id that appeared. Any baseline-weights key NOT in
    that set is "dead" (no candidate matched it across the entire input
    catalogue). Reported in the proposal artifact so reviewers can prune
    truly-unused entries from ``src/mtg_synergy_graph/data/scoring_weights.json``.
    """
    firing_rules: set[str] = set()
    for comps in complements_cache.values():
        for c in comps:
            firing_rules.add(c.rule_id)
    return [rule_id for rule_id in baseline_weights if rule_id not in firing_rules]


def _apply_drift_revert(
    *,
    history: list[OptimizerStep],
    accepted_step_indices: list[int],
    n_steps_accepted: int,
    n_steps_rejected: int,
) -> tuple[int, int]:
    """Cumulative-drift revert: mark every accepted step as reverted, return updated counters.

    Mutates ``history`` (replaces each accepted ``OptimizerStep`` with a copy
    flagged ``accepted=False, reject_reason="cumulative_drift_revert"``) and
    clears ``accepted_step_indices``. Returns the corrected
    ``(n_steps_accepted, n_steps_rejected)`` so the caller can update its own
    counters and reset metrics atomically.

    Used at two trip points: mid-sweep (after every accept) and sweep-boundary
    backstop. Both call sites must reset metrics to baseline values themselves —
    this helper only owns the history bookkeeping.
    """
    for idx in accepted_step_indices:
        step = history[idx]
        history[idx] = OptimizerStep(
            sweep_n=step.sweep_n,
            rule_id=step.rule_id,
            old_value=step.old_value,
            new_value=step.new_value,
            train_composite=step.train_composite,
            held_composite=step.held_composite,
            train_ndcg=step.train_ndcg,
            held_ndcg=step.held_ndcg,
            train_gem=step.train_gem,
            held_gem=step.held_gem,
            accepted=False,
            reject_reason="cumulative_drift_revert",
        )
        n_steps_accepted -= 1
        n_steps_rejected += 1
    accepted_step_indices.clear()
    return n_steps_accepted, n_steps_rejected


def _grid_search_best_perturbation(
    rule_id: str,
    current_weights: Mapping[str, float],
    *,
    conn: sqlite3.Connection,
    edhrec_conn: sqlite3.Connection,
    train: Sequence[str],
    config: OptimizerConfig,
    complements_cache: dict[str, list[PortComplement]],
    labels_cache: dict[str, EdhrecLabels],
    candidate_cache: CandidateCache | None,
    idf_basis_cache: dict[str, IdfBasis],
    current_train_composite: float,
) -> tuple[CompositeObjective, float] | None:
    """Sweep ``config.grid`` for one rule; return ``(best_train_obj, best_value)`` or None.

    Inner step of the Coordinate Ascent driver: for a fixed rule_id and
    the current weights vector, evaluate every grid multiplier on the
    train split and return the perturbation with the highest train
    composite — but only if it strictly improves on
    ``current_train_composite``. Returns ``None`` if no perturbation
    beat the current state (the optimizer should skip to the next rule).

    Pure with respect to optimizer state — does not mutate
    ``current_weights`` or any cache; the caller decides whether to
    accept the returned perturbation based on the held-out re-eval.
    """
    old_value = current_weights[rule_id]
    best_train: CompositeObjective | None = None
    best_value = old_value

    for mult in config.grid:
        new_value = _clamp(old_value * mult, config.clamp_min, config.clamp_max)
        if new_value == old_value:
            continue
        candidate_weights = {**current_weights, rule_id: new_value}
        train_obj = _score_split(
            conn,
            edhrec_conn,
            train,
            candidate_weights,
            complements_cache=complements_cache,
            labels_cache=labels_cache,
            alpha=config.alpha,
            candidate_cache=candidate_cache,
            idf_basis_cache=idf_basis_cache,
        )
        if best_train is None or train_obj.composite > best_train.composite:
            best_train = train_obj
            best_value = new_value

    if best_train is None or best_train.composite <= current_train_composite:
        return None
    return best_train, best_value


def _select_self_test_plant(
    baseline_weights: Mapping[str, float],
    *,
    seed: int,
    planted_mult: float,
    clamp_min: float,
    clamp_max: float,
    excluded: Collection[str] = (),
) -> tuple[str, float]:
    """Pick a (rule_id, planted_value) pair that doesn't collapse to baseline under clamp.

    Tries rules in deterministic seed-shuffled order. For each rule, tries the
    UP direction first (``baseline × planted_mult``); if that clamps back to
    baseline (rule already at ``clamp_max``), tries the DOWN direction
    (``baseline / planted_mult``). The 0.5×/2.0× grid recovers either way —
    a 2× plant lands exactly back on baseline at grid 0.5×, and a 0.5× plant
    lands exactly on baseline at grid 2.0×.

    ``excluded`` skips rules that the caller knows can't produce a recoverable
    signal — typically dead-keys (rules that fire on zero commanders in the
    train split). Without this filter, the selector would pick a dead rule
    and the grid search would produce identical composite scores at every
    cell, raising a false ``OptimizerSelfTestFailed``.

    Returns the first rule with non-trivial planting room. Raises only if EVERY
    non-excluded rule has zero room in both directions (clamp_min == clamp_max
    case, or the entire catalogue was excluded — both indicate the caller's
    catalogue or filter is misconfigured).

    Raises:
        OptimizerSelfTestFailed: no eligible rule has planting room.
    """
    rng = random.Random(seed)  # noqa: S311 — determinism, not crypto
    excluded_set = frozenset(excluded)
    candidates = [k for k in baseline_weights if k not in excluded_set]
    rng.shuffle(candidates)

    for rule_id in candidates:
        baseline_value = baseline_weights[rule_id]
        # Try UP direction first (the original 2.0× plant).
        up_value = _clamp(baseline_value * planted_mult, clamp_min, clamp_max)
        if up_value != baseline_value:
            return rule_id, up_value
        # UP collapsed (baseline at clamp_max). Try DOWN: baseline / planted_mult.
        # The grid recovers symmetrically — 0.5× plant + 2.0× grid step = baseline.
        down_value = _clamp(baseline_value / planted_mult, clamp_min, clamp_max)
        if down_value != baseline_value:
            return rule_id, down_value

    if excluded_set:
        raise OptimizerSelfTestFailed(
            f"self-test could not find ANY non-excluded rule with planting room: "
            f"{len(excluded_set)} of {len(baseline_weights)} rule(s) excluded as dead-keys, "
            f"and every remaining rule collapses under both x{planted_mult} (UP) and "
            f"/{planted_mult} (DOWN) clamping (clamp_min={clamp_min}, clamp_max={clamp_max}). "
            "The catalogue may be misconfigured, or the train split is too narrow."
        )
    raise OptimizerSelfTestFailed(
        f"self-test could not find ANY rule with planting room: every rule's "
        f"baseline collapses under both x{planted_mult} (UP) and /{planted_mult} (DOWN) "
        f"clamping (clamp_min={clamp_min}, clamp_max={clamp_max}). "
        "The catalogue may be misconfigured."
    )


def _planted_perturbation_self_test(
    conn: sqlite3.Connection,
    edhrec_conn: sqlite3.Connection,
    baseline_weights: Mapping[str, float],
    train: Sequence[str],
    *,
    config: OptimizerConfig,
    complements_cache: dict[str, list[PortComplement]],
    labels_cache: dict[str, EdhrecLabels],
    candidate_cache: CandidateCache | None = None,
    idf_basis_cache: dict[str, IdfBasis] | None = None,
) -> None:
    """Plant a 2.0x perturbation on a rule; assert recovery on the grid.

    Picks the rule via :func:`_select_self_test_plant`, which rotates through
    rules in deterministic shuffled order until one has planting room (handles
    the case where the seed-picked rule's baseline is already at ``clamp_max``).

    Sweeps ``config.grid`` from the planted state and verifies the optimizer's
    chosen multiplier lands within ``self_test_recovery_tolerance`` of the
    baseline value. The grid step ``0.5x`` against a ``2.0x`` plant (or
    ``2.0x`` against a ``0.5x`` plant) lands exactly on baseline; ±5% tolerance
    accommodates float noise but rejects "couldn't recover at all" failures.

    Raises:
        OptimizerSelfTestFailed: optimizer could not recover the baseline,
            or no rule has planting room.
    """
    # Pre-warm the train commanders' complements_cache so we can detect
    # dead keys (rules that fire on zero train commanders) and exclude them
    # from plant selection. Without this filter, the seed-shuffled selector
    # would happily pick a rule that fires nowhere on the train split — the
    # grid search then produces identical composite scores at every cell,
    # raising a false-positive ``OptimizerSelfTestFailed`` on a structural
    # property of the rule catalogue rather than a real calibration bug.
    for cmdr in train:
        if cmdr not in complements_cache:
            complements_cache[cmdr] = find_all_complements(conn, [cmdr], candidate_cache=candidate_cache)
    train_only_cache = {cmdr: complements_cache[cmdr] for cmdr in train}
    dead_keys = frozenset(_detect_dead_keys(train_only_cache, baseline_weights))

    rule_id, planted_value = _select_self_test_plant(
        baseline_weights,
        seed=config.self_test_seed,
        planted_mult=config.self_test_planted_mult,
        clamp_min=config.clamp_min,
        clamp_max=config.clamp_max,
        excluded=dead_keys,
    )
    baseline_value = baseline_weights[rule_id]
    planted_weights = {**baseline_weights, rule_id: planted_value}

    # Sweep the grid from the planted state; track the best train composite.
    best_value = planted_value
    best_composite = _score_split(
        conn,
        edhrec_conn,
        train,
        planted_weights,
        complements_cache=complements_cache,
        labels_cache=labels_cache,
        alpha=config.alpha,
        candidate_cache=candidate_cache,
        idf_basis_cache=idf_basis_cache,
    ).composite

    for mult in config.grid:
        candidate_value = _clamp(planted_value * mult, config.clamp_min, config.clamp_max)
        if candidate_value == planted_value:
            continue
        candidate_weights = {**planted_weights, rule_id: candidate_value}
        obj = _score_split(
            conn,
            edhrec_conn,
            train,
            candidate_weights,
            complements_cache=complements_cache,
            labels_cache=labels_cache,
            alpha=config.alpha,
            candidate_cache=candidate_cache,
            idf_basis_cache=idf_basis_cache,
        )
        if obj.composite > best_composite:
            best_composite = obj.composite
            best_value = candidate_value

    tolerance = max(baseline_value * config.self_test_recovery_tolerance, 1e-9)
    if abs(best_value - baseline_value) > tolerance:
        raise OptimizerSelfTestFailed(
            f"self-test could not recover baseline for {rule_id!r}: "
            f"baseline={baseline_value:.6f}, planted={planted_value:.6f}, "
            f"recovered={best_value:.6f}, tolerance=±{tolerance:.6f}. "
            "Either the gates are mis-calibrated (eps_step too tight, alpha skew) "
            "or the rule's contribution is too small for the train split to detect "
            "a 2x perturbation."
        )


def run_optimizer(
    conn: sqlite3.Connection,
    edhrec_conn: sqlite3.Connection,
    commanders: Sequence[str],
    *,
    config: OptimizerConfig | None = None,
    time_now: Callable[[], float] | None = None,
) -> OptimizerResult:
    """Coordinate Ascent driver. Returns the optimization result + history.

    Reads ``_RULE_QUALITY_MULTIPLIER`` at entry to snapshot the baseline.
    Patches the global within ``_score_split`` per evaluation; restores
    via try/finally on every exit path so dict identity is preserved.

    The ``time_now`` argument is a test seam: pass a callable returning
    monotonically advancing seconds (e.g., ``itertools.count(start, step).__next__``)
    to deterministically test wall-clock termination. Default ``None`` uses
    ``time.monotonic`` so an NTP step correction cannot disable the
    wall-clock gate by moving the clock backwards.
    """
    import time as time_module

    from mtg_synergy_graph import universal_scorer

    config = config or OptimizerConfig()
    clock: Callable[[], float] = time_now if time_now is not None else time_module.monotonic
    start_time = clock()

    baseline_weights = dict(universal_scorer._RULE_QUALITY_MULTIPLIER)

    # Build the commander-independent candidate cache ONCE per run. Without this,
    # every grid-cell evaluation re-issues `SELECT name, cmc, edhrec_rank FROM
    # cards` (full ~32k-row scan) inside score_commander_from_complements +
    # score_from_complements. With ~26k grid evaluations per sweep, that's
    # ~850M row reads → the bottleneck. The cache is reused across the full run.
    candidate_cache = build_candidate_cache(conn)

    # Build the split. Random_split returns frozen tuples; we keep them.
    split = random_split(
        commanders,
        conn,
        train_ratio=config.train_ratio,
        seed=config.split_seed,
    )

    complements_cache: dict[str, list[PortComplement]] = {}
    labels_cache: dict[str, EdhrecLabels] = {}
    # Per-commander IDF basis cache. Lazily filled on first miss inside
    # _score_split. Reused across every grid cell to skip the O(complements)
    # frequency-counting walk in _compute_idf_weights — only the per-key
    # _RULE_QUALITY_MULTIPLIER lookup runs per cell.
    idf_basis_cache: dict[str, IdfBasis] = {}

    # Run self-test BEFORE measuring baseline — both for sequencing clarity and
    # because the self-test mutates only its own internal state via try/finally.
    if config.run_self_test:
        _planted_perturbation_self_test(
            conn,
            edhrec_conn,
            baseline_weights,
            split.train,
            config=config,
            complements_cache=complements_cache,
            labels_cache=labels_cache,
            candidate_cache=candidate_cache,
            idf_basis_cache=idf_basis_cache,
        )

    # Baseline objectives. After this call, complements/labels caches are warm
    # for every train + held commander.
    baseline_train = _score_split(
        conn,
        edhrec_conn,
        split.train,
        baseline_weights,
        complements_cache=complements_cache,
        labels_cache=labels_cache,
        alpha=config.alpha,
        candidate_cache=candidate_cache,
        idf_basis_cache=idf_basis_cache,
    )
    baseline_held = _score_split(
        conn,
        edhrec_conn,
        split.held,
        baseline_weights,
        complements_cache=complements_cache,
        labels_cache=labels_cache,
        alpha=config.alpha,
        candidate_cache=candidate_cache,
        idf_basis_cache=idf_basis_cache,
    )

    # Slug-mismatch / EDHREC-empty diagnostic + dead-key detection.
    _warn_missing_edhrec_labels(labels_cache)
    dead_keys = _detect_dead_keys(complements_cache, baseline_weights)

    current_weights = dict(baseline_weights)
    current_train_composite = baseline_train.composite
    current_held_composite = baseline_held.composite
    current_train_ndcg = baseline_train.mean_ndcg
    current_held_ndcg = baseline_held.mean_ndcg
    current_train_gem = baseline_train.gem_rate
    current_held_gem = baseline_held.gem_rate

    history: list[OptimizerStep] = []
    accepted_step_indices: list[int] = []
    n_steps_accepted = 0
    n_steps_rejected = 0
    partial_sweep = False
    n_iterations = 0

    rule_keys = sorted(k for k in baseline_weights if k not in dead_keys)

    for sweep_n in range(1, config.max_sweeps + 1):
        n_iterations = sweep_n
        sweep_had_accept = False

        for rule_id in rule_keys:
            if clock() - start_time > config.wall_clock_seconds:
                partial_sweep = True
                break

            old_value = current_weights[rule_id]
            grid_result = _grid_search_best_perturbation(
                rule_id,
                current_weights,
                conn=conn,
                edhrec_conn=edhrec_conn,
                train=split.train,
                config=config,
                complements_cache=complements_cache,
                labels_cache=labels_cache,
                candidate_cache=candidate_cache,
                idf_basis_cache=idf_basis_cache,
                current_train_composite=current_train_composite,
            )
            if grid_result is None:
                # No grid cell beat the current train composite — skip this rule.
                continue
            best_train, best_value = grid_result

            # Re-evaluate held with the candidate weights (the best perturbation).
            candidate_weights = {**current_weights, rule_id: best_value}
            held_obj = _score_split(
                conn,
                edhrec_conn,
                split.held,
                candidate_weights,
                complements_cache=complements_cache,
                labels_cache=labels_cache,
                alpha=config.alpha,
                candidate_cache=candidate_cache,
                idf_basis_cache=idf_basis_cache,
            )

            held_delta = held_obj.composite - current_held_composite
            accepted = held_delta >= -config.eps_step
            reject_reason = "" if accepted else "held_out_eps"

            step = OptimizerStep(
                sweep_n=sweep_n,
                rule_id=rule_id,
                old_value=old_value,
                new_value=best_value,
                train_composite=best_train.composite,
                held_composite=held_obj.composite,
                train_ndcg=best_train.mean_ndcg,
                held_ndcg=held_obj.mean_ndcg,
                train_gem=best_train.gem_rate,
                held_gem=held_obj.gem_rate,
                accepted=accepted,
                reject_reason=reject_reason,
            )
            history.append(step)

            if accepted:
                # Per-axis regression warning (FR3): emit stderr if either
                # axis dropped > 0.005 even though composite passed.
                if (
                    best_train.gem_rate is not None
                    and current_train_gem is not None
                    and current_train_gem - best_train.gem_rate > 0.005
                ):
                    _logger.warning(
                        "accepted step on %s dropped train gem_rate by %.4f (composite improved)",
                        rule_id,
                        current_train_gem - best_train.gem_rate,
                    )

                current_weights[rule_id] = best_value
                current_train_composite = best_train.composite
                current_held_composite = held_obj.composite
                current_train_ndcg = best_train.mean_ndcg
                current_held_ndcg = held_obj.mean_ndcg
                current_train_gem = best_train.gem_rate
                current_held_gem = held_obj.gem_rate
                n_steps_accepted += 1
                accepted_step_indices.append(len(history) - 1)
                sweep_had_accept = True

                # Mid-sweep cumulative-drift check. Trip earlier if accepts have
                # already drifted held below the cumulative gate. Without this,
                # a wall-clock-aborted sweep would skip the drift check entirely
                # (the boundary check at the end of sweep_n only fires when the
                # sweep completes), and intra-sweep drift escapes detection.
                if current_held_composite - baseline_held.composite < -config.eps_cumulative:
                    n_steps_accepted, n_steps_rejected = _apply_drift_revert(
                        history=history,
                        accepted_step_indices=accepted_step_indices,
                        n_steps_accepted=n_steps_accepted,
                        n_steps_rejected=n_steps_rejected,
                    )
                    current_weights = dict(baseline_weights)
                    current_train_composite = baseline_train.composite
                    current_held_composite = baseline_held.composite
                    current_train_ndcg = baseline_train.mean_ndcg
                    current_held_ndcg = baseline_held.mean_ndcg
                    current_train_gem = baseline_train.gem_rate
                    current_held_gem = baseline_held.gem_rate
                    partial_sweep = True
                    break
            else:
                n_steps_rejected += 1

        # End of sweep. If wall-clock or mid-sweep drift already aborted, exit.
        if partial_sweep:
            break

        # Sweep-boundary drift check is now a backstop — most drift trips will
        # fire mid-sweep above. Kept here for defense-in-depth and so the trip
        # condition is identical at both call sites.
        if current_held_composite - baseline_held.composite < -config.eps_cumulative:
            n_steps_accepted, n_steps_rejected = _apply_drift_revert(
                history=history,
                accepted_step_indices=accepted_step_indices,
                n_steps_accepted=n_steps_accepted,
                n_steps_rejected=n_steps_rejected,
            )
            current_weights = dict(baseline_weights)
            current_train_composite = baseline_train.composite
            current_held_composite = baseline_held.composite
            current_train_ndcg = baseline_train.mean_ndcg
            current_held_ndcg = baseline_held.mean_ndcg
            current_train_gem = baseline_train.gem_rate
            current_held_gem = baseline_held.gem_rate
            partial_sweep = True
            break

        # Convergence: no perturbation accepted this sweep.
        if not sweep_had_accept:
            break

    return OptimizerResult(
        baseline_weights=MappingProxyType(baseline_weights),
        final_weights=MappingProxyType(current_weights),
        history=tuple(history),
        n_iterations=n_iterations,
        n_steps_accepted=n_steps_accepted,
        n_steps_rejected=n_steps_rejected,
        partial_sweep=partial_sweep,
        dead_keys=tuple(dead_keys),
        train_split=split.train,
        held_split=split.held,
        color_buckets=split.color_buckets,
        train_composite_baseline=baseline_train.composite,
        held_composite_baseline=baseline_held.composite,
        train_ndcg_baseline=baseline_train.mean_ndcg,
        held_ndcg_baseline=baseline_held.mean_ndcg,
        train_gem_baseline=baseline_train.gem_rate,
        held_gem_baseline=baseline_held.gem_rate,
        train_composite_final=current_train_composite,
        held_composite_final=current_held_composite,
        train_ndcg_final=current_train_ndcg,
        held_ndcg_final=current_held_ndcg,
        train_gem_final=current_train_gem,
        held_gem_final=current_held_gem,
    )


# ---------------------------------------------------------------------------
# Unit 5: Output artifacts (proposal JSON + history CSV)
# ---------------------------------------------------------------------------


_PROPOSAL_DEFAULT_PATH = ".audit/optimize_proposal.json"
_HISTORY_DEFAULT_PATH = ".audit/optimize_history.csv"

#: Schema version for the proposal JSON artifact. Bump on any breaking
#: change to the payload shape (renamed/removed fields, changed semantics)
#: so agents diffing proposals across code versions can detect drift.
PROPOSAL_SCHEMA_VERSION = 1

#: Schema version for the ``--format json`` run-summary printed to stdout
#: by ``handle_optimize``. Independent from PROPOSAL_SCHEMA_VERSION because
#: the summary covers run-level metadata (rc context, paths, deltas) while
#: the proposal covers the candidate-diff payload. Bump on any breaking
#: change to the summary shape.
OPTIMIZE_SUMMARY_SCHEMA_VERSION = 1

#: CSV column order for ``optimize_history.csv``. Bumping or reordering
#: requires updating the reader symmetrically.
OPTIMIZE_HISTORY_FIELDS: tuple[str, ...] = (
    "timestamp",
    "run_id",
    "sweep_n",
    "rule_id",
    "old_value",
    "new_value",
    "train_composite",
    "held_composite",
    "train_ndcg",
    "held_ndcg",
    "train_gem",
    "held_gem",
    "accepted",
    "reject_reason",
)


def _proposed_config_hash(proposed_weights: Mapping[str, float]) -> str:
    """Compute the config hash that the proposed weights would produce.

    Patches ``_RULE_QUALITY_MULTIPLIER`` to the proposed values, calls
    ``compute_config_hash``, and restores via the
    :func:`~mtg_synergy_graph.universal_scorer.patched_rule_quality_multiplier`
    context manager. Dict identity preserved on every exit path
    (including exceptions raised inside ``compute_config_hash``).
    """
    from mtg_synergy_graph.bench.tensor import compute_config_hash

    with patched_rule_quality_multiplier(proposed_weights):
        return compute_config_hash()


def write_proposal_json(
    result: OptimizerResult,
    path: str | Path | None = None,
) -> dict[str, object]:
    """Write the optimizer's proposal artifact to ``.audit/optimize_proposal.json``.

    Returns the dict that was serialized (useful for tests / inspection).
    Per FR6: ``per_rule_diffs`` includes only rules whose value actually
    changed; ``proposed_config_hash`` flips relative to baseline iff any
    weight changed; ``dead_keys`` non-empty signals a stale
    ``src/mtg_synergy_graph/data/scoring_weights.json`` entry.

    If ``target_path`` already exists, a stderr warning is emitted before
    overwriting — the file is still overwritten (non-blocking) so CI runs
    don't stall, but the human reviewer sees that a prior proposal was
    replaced.

    The ``comment`` fields in ``src/mtg_synergy_graph/data/scoring_weights.json`` are never
    read or emitted by this function — humans applying the diff cannot
    accidentally clobber them.
    """
    import json as _json
    from pathlib import Path as _Path

    target_path = _Path(path) if path is not None else _Path(_PROPOSAL_DEFAULT_PATH)

    # Hash the baseline_weights snapshot symmetrically with proposed_hash so
    # both fields describe the same source-of-truth dict. Using live
    # compute_config_hash() here would silently drift if the global was
    # mutated between run_optimizer() return and write_proposal_json() entry.
    baseline_hash = _proposed_config_hash(result.baseline_weights)
    proposed_hash = _proposed_config_hash(result.final_weights)

    per_rule_diffs: list[dict] = []
    for rule_id, baseline_value in result.baseline_weights.items():
        final_value = result.final_weights[rule_id]
        if baseline_value == final_value:
            continue
        # Find the accepted-iteration step that produced the final value (if any).
        accepted_iter: int | None = None
        for step in result.history:
            if step.rule_id == rule_id and step.accepted and step.new_value == final_value:
                accepted_iter = step.sweep_n
                break
        per_rule_diffs.append(
            {
                "rule_id": rule_id,
                "old_value": baseline_value,
                "new_value": final_value,
                "accepted_iteration": accepted_iter,
            }
        )

    payload: dict[str, object] = {
        "schema_version": PROPOSAL_SCHEMA_VERSION,
        "baseline_config_hash": baseline_hash,
        "proposed_config_hash": proposed_hash,
        "per_rule_diffs": per_rule_diffs,
        "aggregate_train_composite_delta": (result.train_composite_final - result.train_composite_baseline),
        "aggregate_held_composite_delta": (result.held_composite_final - result.held_composite_baseline),
        "train_ndcg_baseline": result.train_ndcg_baseline,
        "train_ndcg_final": result.train_ndcg_final,
        "held_ndcg_baseline": result.held_ndcg_baseline,
        "held_ndcg_final": result.held_ndcg_final,
        "gem_rate_train_baseline": result.train_gem_baseline,
        "gem_rate_train_final": result.train_gem_final,
        "gem_rate_held_baseline": result.held_gem_baseline,
        "gem_rate_held_final": result.held_gem_final,
        "n_iterations": result.n_iterations,
        "n_steps_accepted": result.n_steps_accepted,
        "n_steps_rejected": result.n_steps_rejected,
        "partial_sweep": result.partial_sweep,
        "dead_keys": list(result.dead_keys),
        "train_split_size": len(result.train_split),
        "held_split_size": len(result.held_split),
        # color_buckets has nested MappingProxyType (outer + inner) for
        # frozen-dataclass immutability. json.dumps doesn't know how to
        # encode mappingproxy — flatten both layers to plain dicts.
        "color_buckets": {k: dict(v) for k, v in result.color_buckets.items()},
    }

    target_path.parent.mkdir(parents=True, exist_ok=True)
    if target_path.exists():
        _logger.warning("overwriting existing proposal at %s", target_path)
    target_path.write_text(_json.dumps(payload, indent=2, sort_keys=False), encoding="utf-8")
    return payload


def append_optimize_history_rows(
    result: OptimizerResult,
    run_id: str,
    path: str | Path | None = None,
) -> None:
    """Append one CSV row per attempted step to ``.audit/optimize_history.csv``.

    Header detection: check the file's on-disk size BEFORE opening in append
    mode. ``fh.tell()`` returns 0 on Linux ext4/xfs/tmpfs even on a non-empty
    file opened ``'a'`` (the seek-to-end happens at first write, not open),
    so a tell-based check would re-write the header on every invocation.

    Wraps in try/except OSError/csv.Error with stderr-warn-and-continue
    degradation. Exceptions during write must never abort the optimizer's
    primary output.
    """
    import csv as _csv
    import sys as _sys
    from datetime import UTC, datetime
    from pathlib import Path as _Path

    target_path = _Path(path) if path is not None else _Path(_HISTORY_DEFAULT_PATH)
    timestamp = datetime.now(UTC).isoformat(timespec="seconds")

    try:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        write_header = not target_path.exists() or target_path.stat().st_size == 0
        with target_path.open("a", encoding="utf-8", newline="") as fh:
            writer = _csv.writer(fh)
            if write_header:
                writer.writerow(OPTIMIZE_HISTORY_FIELDS)
            for step in result.history:
                writer.writerow(_history_row_for_step(step, timestamp, run_id))
    except (OSError, _csv.Error) as exc:
        print(
            f"bench.py audit --optimize: warning: failed to append history rows "
            f"to {target_path}: {exc.__class__.__name__}: {exc}",
            file=_sys.stderr,
        )


def _history_row_for_step(step: OptimizerStep, timestamp: str, run_id: str) -> list[str]:
    """Render one ``OptimizerStep`` into the CSV's 14 string cells."""
    return [
        timestamp,
        run_id,
        str(step.sweep_n),
        step.rule_id,
        f"{step.old_value:.6f}",
        f"{step.new_value:.6f}",
        f"{step.train_composite:.6f}",
        f"{step.held_composite:.6f}",
        f"{step.train_ndcg:.6f}",
        f"{step.held_ndcg:.6f}",
        "" if step.train_gem is None else f"{step.train_gem:.6f}",
        "" if step.held_gem is None else f"{step.held_gem:.6f}",
        "true" if step.accepted else "false",
        step.reject_reason,
    ]


# ---------------------------------------------------------------------------
# Unit 4: CLI handler
# ---------------------------------------------------------------------------


#: Minimum commander count for a non-degenerate train/held split.
#: At ``train_ratio=0.8`` (default), ``round(n * 0.8)`` produces ``held=0``
#: when ``n <= 2`` (round(0.8)=1, round(1.6)=2). At ``n=3`` the split is
#: 2 train + 1 held — the smallest input that doesn't trivially pass the
#: held-eps gate. Below this, cross-validation is silently nullified.
#: The pinned golden set has 100 commanders; this floor is a usability
#: guardrail for CLI invocations against a synthetic or hand-built fixture.
_MIN_COMMANDERS_FOR_SPLIT = 3

#: Canonical 100-cmdr fixture path — the default for ``--fixture``.
#: ``--optimize`` swaps to ``_OPTIMIZE_DEFAULT_FIXTURE`` when the user
#: accepted this default, since 100 commanders is too few for trustworthy
#: gradient signal (the held-out delta correlates poorly with train delta;
#: rules slam to clamp_max). Documented in the docs/solutions/best-practices
#: optimizer-fixture-size note.
_CANONICAL_FIXTURE = "tests/fixtures/golden_set_run.json"

#: 500-cmdr fixture path used as the optimize-mode default. Built by
#: ``scripts/bootstrap_golden_set_500.py``. Regenerated after each
#: cardsfolder import or scoring config change.
_OPTIMIZE_DEFAULT_FIXTURE = "tests/fixtures/golden_set_run_500.json"


def handle_optimize(args: argparse.Namespace) -> int:
    """Handle ``bench.py audit --optimize``.

    Reads commanders from the pinned fixture, opens both DBs, runs the
    Coordinate Ascent driver (Unit 3), emits the proposal artifact + history
    rows (Unit 5), and returns an exit code:

    - 0: success — proposal written (may be empty if no improvement found).
    - 1: driver-internal exception (DB I/O, JSON write failure, etc).
    - 2: usage / config error — missing fixture, stale tensor, fixture too small.
    - 3: planted-perturbation self-test failed (calibration issue).

    The split between 1 and 3 lets CI consumers distinguish "optimizer
    untrustworthy on this catalogue" from "infra problem."
    """
    import sys as _sys
    import uuid

    from mtg_synergy_graph.bench.fixture import PinnedFixture
    from mtg_synergy_graph.bench.tensor import compute_config_hash
    from mtg_synergy_graph.db import open_db

    fixture_path = args.fixture
    db_path = args.db
    edhrec_db_path = args.edhrec_db

    # Default-redirect: swap the 100-cmdr canonical for the 500-cmdr fixture
    # when the user accepted the global default. The 100-cmdr fixture is too
    # small to produce trustworthy optimizer proposals — held-delta correlates
    # poorly with train-delta, multiple rules slam to clamp_max, and ~25 of
    # 60+ rules are dead-keys. Users who explicitly pass --fixture get exactly
    # what they asked for. The redirect is silent on success and noisy when
    # the 500 fixture is missing (so a CI runner without the bootstrap data
    # falls back gracefully instead of failing on a missing path).
    if fixture_path == _CANONICAL_FIXTURE:
        from pathlib import Path as _Path

        if _Path(_OPTIMIZE_DEFAULT_FIXTURE).exists():
            print(
                f"--optimize: using {_OPTIMIZE_DEFAULT_FIXTURE} (default for --optimize). "
                f"Pass --fixture {_CANONICAL_FIXTURE} to use the 100-cmdr canonical instead.",
                file=_sys.stderr,
            )
            fixture_path = _OPTIMIZE_DEFAULT_FIXTURE

    fixture = PinnedFixture.load(fixture_path)
    if not fixture.entries:
        print(
            f"error: fixture at {fixture_path} is empty; --optimize needs at least one commander to score.",
            file=_sys.stderr,
        )
        return 2

    if len(fixture.entries) < _MIN_COMMANDERS_FOR_SPLIT:
        print(
            f"error: fixture at {fixture_path} has {len(fixture.entries)} commanders; "
            f"--optimize requires at least {_MIN_COMMANDERS_FOR_SPLIT} to produce a "
            "non-degenerate train/held split.",
            file=_sys.stderr,
        )
        return 2

    # Tensor-staleness precondition (FR4c): refuse to run if the persisted
    # tensor was built under a different config than the current code state.
    # The pinned fixture's config_hash is the authoritative reference.
    current_hash = compute_config_hash()
    if fixture.config_hash and current_hash != fixture.config_hash:
        print(
            f"error: tensor stale (current config_hash={current_hash[:12]}... "
            f"vs fixture {fixture.config_hash[:12]}...). Run "
            "`bench.py audit --repin --yes` first.",
            file=_sys.stderr,
        )
        return 2

    # All OptimizerConfig fields exposed as CLI flags. Defaults come from
    # OptimizerConfig itself (this is just a forwarding constructor); change
    # defaults THERE not in cli.py, otherwise the two drift.
    config = OptimizerConfig(
        alpha=args.alpha,
        grid=tuple(args.grid),
        clamp_min=args.clamp_min,
        clamp_max=args.clamp_max,
        max_sweeps=args.max_sweeps,
        wall_clock_seconds=args.wall_clock_seconds,
        eps_step=args.eps_step,
        eps_cumulative=args.eps_cumulative,
        train_ratio=args.train_ratio,
        split_seed=args.seed,
        self_test_seed=args.self_test_seed,
        run_self_test=not args.no_self_test,
    )
    commanders = [entry.commander for entry in fixture.entries]
    run_id = uuid.uuid4().hex[:12]

    conn = open_db(db_path)
    try:
        edhrec_conn = open_db(edhrec_db_path)
    except Exception:
        # If the second open fails, the first conn would otherwise leak —
        # WAL-mode SQLite holds locks until close. Close it before re-raising.
        conn.close()
        raise

    try:
        try:
            result = run_optimizer(conn, edhrec_conn, commanders, config=config)
        except OptimizerSelfTestFailed as exc:
            print(f"error: optimizer self-test failed: {exc}", file=_sys.stderr)
            return 3
    finally:
        conn.close()
        edhrec_conn.close()

    try:
        write_proposal_json(result, path=args.proposal_path)
        append_optimize_history_rows(result, run_id=run_id, path=args.optimize_history)
    except OSError as exc:
        # Proposal/history write failure: surface as documented rc=1
        # ("driver-internal exception: DB I/O, JSON write failure, etc").
        print(
            f"error: failed to write optimizer artifacts: {exc.__class__.__name__}: {exc}",
            file=_sys.stderr,
        )
        return 1

    train_delta = result.train_composite_final - result.train_composite_baseline
    held_delta = result.held_composite_final - result.held_composite_baseline

    if getattr(args, "format", "md") == "json":
        # Machine-readable summary on stdout for agent-driven sweep pipelines.
        # The proposal JSON itself is at args.proposal_path; this is the
        # run-summary so an agent can branch on rc + summary without re-reading
        # the proposal file just to know "did anything change."
        import json as _json

        summary = {
            "schema_version": OPTIMIZE_SUMMARY_SCHEMA_VERSION,
            "run_id": run_id,
            "n_iterations": result.n_iterations,
            "n_steps_accepted": result.n_steps_accepted,
            "n_steps_rejected": result.n_steps_rejected,
            "partial_sweep": result.partial_sweep,
            "train_composite_baseline": result.train_composite_baseline,
            "train_composite_final": result.train_composite_final,
            "train_composite_delta": train_delta,
            "held_composite_baseline": result.held_composite_baseline,
            "held_composite_final": result.held_composite_final,
            "held_composite_delta": held_delta,
            "proposal_path": str(args.proposal_path),
            "history_path": str(args.optimize_history),
            "dead_keys_count": len(result.dead_keys),
        }
        print(_json.dumps(summary))
    else:
        # Default human-readable summary on stderr, unchanged from prior behavior.
        if result.n_steps_accepted == 0:
            print(
                f"no improvement found (n_iterations={result.n_iterations}, "
                f"partial_sweep={result.partial_sweep}). Proposal written to "
                f"{args.proposal_path} for inspection.",
                file=_sys.stderr,
            )
        else:
            print(
                f"optimizer accepted {result.n_steps_accepted} step(s) over "
                f"{result.n_iterations} sweep(s). "
                f"train composite Δ={train_delta:+.4f}, held Δ={held_delta:+.4f}. "
                f"Proposal written to {args.proposal_path}.",
                file=_sys.stderr,
            )

    return 0
