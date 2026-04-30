"""Coordinate Ascent weight optimizer for ``_RULE_QUALITY_MULTIPLIER``.

Foothold M1 of the tensor-driven weight optimizer plan
(``docs/plans/2026-04-26-001-feat-tensor-weight-optimizer-plan.md``).

This module is built incrementally across the plan's units; each unit
appends its layer to this file:

* **Unit 1 — Composite objective + random split + EDHREC label loader.**
  Pure helper functions over commander lists and EDHREC label dicts.
  Independent of which rules fire.
* **Unit 2 — Cached-complements scorer.** Production-faithful per-grid-cell
  scoring using cached ``find_all_complements`` + re-IDF + ``UniversalScore``.
* **Unit 3 — Coordinate Ascent driver.** The optimizer's main loop:
  alphabetical sweep over rule keys, multiplicative grid, accept/reject
  gates, cumulative-drift revert, planted-perturbation self-test.
* **Unit 4 — CLI handler.** Wires ``bench.py audit --optimize`` into the
  existing dispatcher; tensor-staleness precondition.
* **Unit 5 — Output artifacts.** ``.audit/optimize_proposal.json`` +
  ``.audit/optimize_history.csv`` writers.

The optimizer never mutates ``data/scoring_weights.json`` — it emits a
candidate diff for human review. Application is human-driven via
``bench.py audit --repin --yes``.
"""

from __future__ import annotations

import random
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from mtg_synergy_graph.complement_rules import PortComplement
    from mtg_synergy_graph.universal_scorer import CandidateCache

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
    color_buckets: Mapping[str, dict[str, int]]


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
        placeholders = ",".join("?" * len(commanders))
        query = f"SELECT name, color_identity FROM cards WHERE name IN ({placeholders})"  # noqa: S608
        for row in conn.execute(query, list(commanders)).fetchall():
            # Row may be a sqlite3.Row (dict-like) or a plain tuple; index
            # by position so both work.
            color_by_cmdr[row[0]] = row[1]

    buckets: dict[str, dict[str, int]] = {b: {"train": 0, "held": 0} for b in _COLOR_BUCKETS}
    for name in train:
        buckets[_color_bucket(color_by_cmdr.get(name))]["train"] += 1
    for name in held:
        buckets[_color_bucket(color_by_cmdr.get(name))]["held"] += 1

    # Drop empty buckets so the report stays focused.
    non_empty = {k: v for k, v in buckets.items() if v["train"] or v["held"]}

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


def _build_contributions(
    results: Mapping[str, object],
) -> tuple[tuple[str, str, float], ...]:
    """Fold per-candidate ``UniversalScore`` results into per-(cand, rule) contributions.

    Mirrors the dedup logic in ``universal_scorer._emit_tensor_rows``:
    each ``(rule_id, cmdr_event, cand_event, filter_group)`` key
    contributes at most once per direction. Anti-synergy contributes
    negatively. Zero-contribution rows (synergy fully cancelled by anti)
    are dropped.
    """
    out: list[tuple[str, str, float]] = []
    for cand_name, us in results.items():
        per_rule: dict[str, float] = {}
        seen_syn: set[tuple[str, str, str, str]] = set()
        seen_anti: set[tuple[str, str, str, str]] = set()
        for c in us.complements:  # type: ignore[attr-defined]
            key = (c.rule_id, c.cmdr_event, c.cand_event, c.filter_group)
            w = us.idf_weights.get(key, 1.0)  # type: ignore[attr-defined]
            if c.direction == "synergy":
                if key in seen_syn:
                    continue
                seen_syn.add(key)
                per_rule[c.rule_id] = per_rule.get(c.rule_id, 0.0) + w
            else:
                if key in seen_anti:
                    continue
                seen_anti.add(key)
                per_rule[c.rule_id] = per_rule.get(c.rule_id, 0.0) - w
        for rid, contrib in per_rule.items():
            if contrib != 0.0:
                out.append((cand_name, rid, contrib))
    return tuple(out)


def score_commander_from_complements(
    conn: sqlite3.Connection,
    commander: str,
    complements: list[PortComplement],
    *,
    candidate_cache: CandidateCache | None = None,
) -> CommanderScoreResult:
    """Score one commander given precomputed complements.

    Reads the live ``_RULE_QUALITY_MULTIPLIER`` and ``_FLAT_WEIGHT_OVERRIDES``
    globals — the caller is responsible for patching them via
    ``.clear() + .update(weights)`` in a ``try/finally`` BEFORE this call
    if grid-cell weights differ from baseline. ``_score_from_complements``
    handles the staple/circuit/cmc/rank/embedding side-channels exactly
    as ``score_all_universal`` does.

    The returned ``score_by_candidate`` and ``top_30`` use
    ``to_legacy_buckets()["total"]`` — the production sort key from
    ``engine.SynergyEngine.page()`` — so the optimizer's view matches
    what ``recommend.py`` displays.
    """
    from mtg_synergy_graph.universal_scorer import _score_from_complements

    # Mirrors engine.SynergyEngine.page() at engine.py:378 — sentinel for
    # commanders/cards without an EDHREC rank, sorted last after ranked cards.
    _UNRANKED = 10**9

    results = _score_from_complements(
        conn,
        [commander],
        complements,
        candidate_cache=candidate_cache,
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
            rank_lookup[row["name"]] = row["edhrec_rank"] if row["edhrec_rank"] is not None else _UNRANKED

    score_by_candidate: dict[str, float] = {}
    sortable: list[tuple[str, float]] = []
    for name, us in results.items():
        total = us.to_legacy_buckets()["total"]
        score_by_candidate[name] = total
        sortable.append((name, total))

    sortable.sort(
        key=lambda r: (
            -r[1],
            cmc_lookup.get(r[0], 99.0),
            rank_lookup.get(r[0], _UNRANKED),
            r[0],
        )
    )
    top_30 = tuple(name for name, _ in sortable[:30])

    contributions = _build_contributions(results)

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
    color_buckets: Mapping[str, dict[str, int]]
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
) -> CompositeObjective:
    """Score a commander split with the given weights; return composite objective.

    Patches ``_RULE_QUALITY_MULTIPLIER`` for the duration of the call. The cache
    arguments are populated lazily on first miss; subsequent calls reuse them
    so ``find_all_complements`` and ``load_edhrec_labels`` each run once per
    commander per optimizer run.
    """
    from mtg_synergy_graph import universal_scorer
    from mtg_synergy_graph.bench.hidden_gems import (
        hidden_gem_hit_rate_for_commander,
    )
    from mtg_synergy_graph.complement_rules import find_all_complements
    from mtg_synergy_graph.validate import compute_ndcg

    baseline = dict(universal_scorer._RULE_QUALITY_MULTIPLIER)
    try:
        universal_scorer._RULE_QUALITY_MULTIPLIER.clear()
        universal_scorer._RULE_QUALITY_MULTIPLIER.update(weights)

        per_ndcg: dict[str, float] = {}
        per_gem: dict[str, float | None] = {}
        for cmdr in commanders:
            if cmdr not in complements_cache:
                complements_cache[cmdr] = find_all_complements(conn, [cmdr])
            comps = complements_cache[cmdr]
            if cmdr not in labels_cache:
                labels_cache[cmdr] = load_edhrec_labels(edhrec_conn, cmdr)
            labels = labels_cache[cmdr]

            result = score_commander_from_complements(conn, cmdr, comps)
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
    finally:
        universal_scorer._RULE_QUALITY_MULTIPLIER.clear()
        universal_scorer._RULE_QUALITY_MULTIPLIER.update(baseline)

    return composite_objective(per_ndcg, per_gem, alpha)


def _planted_perturbation_self_test(
    conn: sqlite3.Connection,
    edhrec_conn: sqlite3.Connection,
    baseline_weights: Mapping[str, float],
    train: Sequence[str],
    *,
    config: OptimizerConfig,
    complements_cache: dict[str, list[PortComplement]],
    labels_cache: dict[str, EdhrecLabels],
) -> None:
    """Plant a 2.0x perturbation on a random rule; assert recovery on the grid.

    Sweeps ``config.grid`` from the planted state and verifies the optimizer's
    chosen multiplier lands within ``self_test_recovery_tolerance`` of the
    baseline value. The grid step ``0.5x`` against a ``2.0x`` plant lands
    exactly on baseline; ±5% tolerance accommodates float noise but rejects
    "couldn't recover at all" failures.

    Raises:
        OptimizerSelfTestFailed: optimizer could not recover the baseline.
    """
    rule_id = random.Random(config.self_test_seed).choice(list(baseline_weights.keys()))  # noqa: S311
    baseline_value = baseline_weights[rule_id]
    planted_value = _clamp(
        baseline_value * config.self_test_planted_mult,
        config.clamp_min,
        config.clamp_max,
    )

    if planted_value == baseline_value:
        # Plant collapsed to baseline due to clamp — pick a different rule.
        # Practically impossible at default config; bail with a clear message.
        raise OptimizerSelfTestFailed(
            f"self-test plant for {rule_id!r} collapsed to baseline under clamp; "
            f"baseline={baseline_value}, planted_target={baseline_value * config.self_test_planted_mult}, "
            f"clamp=({config.clamp_min}, {config.clamp_max})"
        )

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
    ``time.time``.
    """
    import time as time_module

    from mtg_synergy_graph import universal_scorer

    config = config or OptimizerConfig()
    clock: Callable[[], float] = time_now if time_now is not None else time_module.time
    start_time = clock()

    baseline_weights = dict(universal_scorer._RULE_QUALITY_MULTIPLIER)

    # Identify dead keys: entries in the JSON config whose rule_id isn't reachable
    # via any helper / declarative rule. We can't introspect the registries here
    # without circular imports; instead, we treat any rule that produces zero
    # complements across ALL commanders as "dead" and report it. This is a strict
    # subset of true dead keys but doesn't crash the optimizer.
    dead_keys: list[str] = []

    # Build the split. Random_split returns frozen tuples; we keep them.
    split = random_split(
        commanders,
        conn,
        train_ratio=config.train_ratio,
        seed=config.split_seed,
    )

    complements_cache: dict[str, list[PortComplement]] = {}
    labels_cache: dict[str, EdhrecLabels] = {}

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
    )
    baseline_held = _score_split(
        conn,
        edhrec_conn,
        split.held,
        baseline_weights,
        complements_cache=complements_cache,
        labels_cache=labels_cache,
        alpha=config.alpha,
    )

    # Identify dead keys after baseline scoring populates the complements cache.
    firing_rules: set[str] = set()
    for comps in complements_cache.values():
        for c in comps:
            firing_rules.add(c.rule_id)
    for rule_id in baseline_weights:
        if rule_id not in firing_rules:
            dead_keys.append(rule_id)

    current_weights = dict(baseline_weights)
    current_train_composite = baseline_train.composite
    current_held_composite = baseline_held.composite
    current_train_ndcg = baseline_train.mean_ndcg
    current_held_ndcg = baseline_held.mean_ndcg
    current_train_gem = baseline_train.gem_rate
    current_held_gem = baseline_held.gem_rate

    history: list[OptimizerStep] = []
    n_steps_accepted = 0
    n_steps_rejected = 0
    partial_sweep = False
    n_iterations = 0

    rule_keys = sorted(k for k in baseline_weights if k not in dead_keys)

    for sweep_n in range(1, config.max_sweeps + 1):
        n_iterations = sweep_n
        sweep_start_weights = dict(current_weights)
        sweep_accepted_steps: list[int] = []
        sweep_had_accept = False

        for rule_id in rule_keys:
            if clock() - start_time > config.wall_clock_seconds:
                partial_sweep = True
                break

            old_value = current_weights[rule_id]
            best_train: CompositeObjective | None = None
            best_value = old_value
            best_mult = 1.0

            for mult in config.grid:
                new_value = _clamp(old_value * mult, config.clamp_min, config.clamp_max)
                if new_value == old_value:
                    continue
                candidate_weights = {**current_weights, rule_id: new_value}
                train_obj = _score_split(
                    conn,
                    edhrec_conn,
                    split.train,
                    candidate_weights,
                    complements_cache=complements_cache,
                    labels_cache=labels_cache,
                    alpha=config.alpha,
                )
                if best_train is None or train_obj.composite > best_train.composite:
                    best_train = train_obj
                    best_value = new_value
                    best_mult = mult

            # If no perturbation strictly beat the current train composite, skip.
            if best_train is None or best_train.composite <= current_train_composite:
                continue

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
                    import sys

                    print(
                        f"[optimize] warning: accepted step on {rule_id} dropped train gem_rate "
                        f"by {current_train_gem - best_train.gem_rate:.4f} (composite improved)",
                        file=sys.stderr,
                    )

                current_weights[rule_id] = best_value
                current_train_composite = best_train.composite
                current_held_composite = held_obj.composite
                current_train_ndcg = best_train.mean_ndcg
                current_held_ndcg = held_obj.mean_ndcg
                current_train_gem = best_train.gem_rate
                current_held_gem = held_obj.gem_rate
                n_steps_accepted += 1
                sweep_accepted_steps.append(len(history) - 1)
                sweep_had_accept = True
            else:
                n_steps_rejected += 1
            _ = best_mult  # retained in case future telemetry wants it

        # End of sweep: cumulative-drift check (only if we didn't already abort).
        if partial_sweep:
            break

        held_drift = current_held_composite - baseline_held.composite
        if held_drift < -config.eps_cumulative:
            # Revert this sweep's accepts and terminate.
            current_weights = sweep_start_weights
            # Mark the sweep's accepted steps as reverted in history. We append
            # a synthetic "revert" step so the history CSV records the rollback.
            for idx in sweep_accepted_steps:
                step = history[idx]
                # Replace with a copy that's now "rejected" with reason cumulative_drift_revert.
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
            partial_sweep = True
            # Reset current metrics to baseline since we reverted.
            current_train_composite = baseline_train.composite
            current_held_composite = baseline_held.composite
            current_train_ndcg = baseline_train.mean_ndcg
            current_held_ndcg = baseline_held.mean_ndcg
            current_train_gem = baseline_train.gem_rate
            current_held_gem = baseline_held.gem_rate
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
    ``compute_config_hash``, restores, and returns the hash. The
    patch+hash+restore happens in a single ``try/finally`` so dict
    identity is preserved on every exit path. Mirrors the established
    test-fixture pattern (``tests/test_scoring_weights.py:309-324``).
    """
    from mtg_synergy_graph import universal_scorer
    from mtg_synergy_graph.bench.tensor import compute_config_hash

    saved = dict(universal_scorer._RULE_QUALITY_MULTIPLIER)
    try:
        universal_scorer._RULE_QUALITY_MULTIPLIER.clear()
        universal_scorer._RULE_QUALITY_MULTIPLIER.update(proposed_weights)
        return compute_config_hash()
    finally:
        universal_scorer._RULE_QUALITY_MULTIPLIER.clear()
        universal_scorer._RULE_QUALITY_MULTIPLIER.update(saved)


def _diff_value(baseline: float, final: float) -> bool:
    """Return True iff baseline and final differ (bitwise float comparison)."""
    return baseline != final


def write_proposal_json(
    result: OptimizerResult,
    path: str | Path | None = None,
) -> dict:
    """Write the optimizer's proposal artifact to ``.audit/optimize_proposal.json``.

    Returns the dict that was serialized (useful for tests / inspection).
    Per FR6: ``per_rule_diffs`` includes only rules whose value actually
    changed; ``proposed_config_hash`` flips relative to baseline iff any
    weight changed; ``dead_keys`` non-empty signals a stale
    ``data/scoring_weights.json`` entry.

    The ``comment`` fields in ``data/scoring_weights.json`` are never
    read or emitted by this function — humans applying the diff cannot
    accidentally clobber them.
    """
    import json as _json
    from pathlib import Path as _Path

    target_path = _Path(path) if path is not None else _Path(_PROPOSAL_DEFAULT_PATH)

    from mtg_synergy_graph.bench.tensor import compute_config_hash

    baseline_hash = compute_config_hash()
    proposed_hash = _proposed_config_hash(result.final_weights)

    per_rule_diffs: list[dict] = []
    for rule_id, baseline_value in result.baseline_weights.items():
        final_value = result.final_weights[rule_id]
        if not _diff_value(baseline_value, final_value):
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

    payload: dict = {
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
        "color_buckets": dict(result.color_buckets),
    }

    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(_json.dumps(payload, indent=2, sort_keys=False), encoding="utf-8")
    return payload


def append_optimize_history_rows(
    result: OptimizerResult,
    run_id: str,
    path: str | Path | None = None,
) -> None:
    """Append one CSV row per attempted step to ``.audit/optimize_history.csv``.

    Mirrors :func:`bench.history.append_run` exactly:
    ``mkdir parents=True, exist_ok=True``, open ``"a" newline=""``, write
    the header on ``tell() == 0``, wrap in try/except OSError/csv.Error
    with a stderr-warn-and-continue degradation. Exceptions during write
    must never abort the optimizer's primary output.
    """
    import csv as _csv
    import sys as _sys
    from datetime import UTC, datetime
    from pathlib import Path as _Path

    target_path = _Path(path) if path is not None else _Path(_HISTORY_DEFAULT_PATH)
    timestamp = datetime.now(UTC).isoformat(timespec="seconds")

    try:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        with target_path.open("a", encoding="utf-8", newline="") as fh:
            pos = fh.tell()
            if pos < 0:
                import os as _os

                pos = _os.fstat(fh.fileno()).st_size
            write_header = pos == 0
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
