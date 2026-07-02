"""R0 portfolio-selection simulation harness (plan 2026-07-02-004, Unit 2).

Live-rescoring kill-test instrument for the greedy per-family
diminishing-returns re-ranker. One production scoring pass per
commander (``SynergyEngine.page(limit=1_000_000)`` — the page-side
``to_legacy_buckets()["total"]`` with the full 4-key tiebreak and the
production legality chain), decomposed ONCE via the shared
:func:`mtg_synergy_graph.portfolio.decompose_universal_score` helper;
every (decay form, λ, map granularity, discount base) cell re-assembles
from the cached decompositions without rescoring.

Zero scoring-path changes: the engine is used read-only through its
public ``page()`` API plus two documented private reads
(``_score_cache`` for the finished ``UniversalScore`` objects — the
``bench.forensics.extract_live_ranking`` precedent — and
``_candidate_cache.candidate_rows`` for the exact cmc/edhrec_rank
tiebreak lookups ``page()`` sorts with).

Per-commander identity guarantee: the λ=0 assembly must equal the
engine's actual ``page()`` top-30 bitwise (compared on the exact
recomposed total — :class:`SelfCheckError` on any divergence), and
every decomposed candidate's recomposed total must equal its
``Recommendation.total_score`` bitwise.

Subcommands (thin CLI at ``scripts/portfolio_sim.py``):

* ``bands`` — bootstrap threshold derivation on the 500-cmdr fixture
  commanders' λ=0 per-commander NDCG/gem distributions → NDCG noise
  band, gem non-regression band, R9a baseline predicate pass rates.
* ``share`` — empirical addressable-share readout (origin R7b): the
  fraction of EDHREC-label misses that enter the assembled top-30 at a
  reference decay depth, over the full production ranking (no
  pinned-window censoring).
* ``sweep`` — the R0 grid: decay forms × λ values × map granularities
  (committed map vs an in-memory identity-only variant) × discount-base
  variants (full family synergy vs flat/density share only), with trap
  sidecar, monoculture-23 column, R9a stratified pass rates,
  tie-density/CV sidecar, and a rank_bonus-ablated delta per cell.

Reports go to ``--output`` (default ``.audit/portfolio_sim/`` — the
``.audit/`` tree is gitignored). This instrument NEVER appends to
``.audit/history.csv``.
"""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
import statistics
import sys
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mtg_synergy_graph.bench.hidden_gems import (
    _aggregate_contributions,
    _cohort_median,
    _plausibility_from_maps,
    hidden_gem_hit_rate_for_commander,
)
from mtg_synergy_graph.bench.optimize import load_edhrec_labels
from mtg_synergy_graph.portfolio import (
    FAMILY_MAP,
    FamilyReconciliationError,
    ScoreDecomposition,
    UnmappedRuleFamilyError,
    assemble_portfolio,
    decompose_universal_score,
)
from mtg_synergy_graph.validate import compute_ndcg

if TYPE_CHECKING:
    from mtg_synergy_graph.engine import SynergyEngine
    from mtg_synergy_graph.universal_scorer import UniversalScore

#: Trap commanders (plan Unit 2): scored at EVERY sweep cell. Fragments
#: are resolved against the fixture commander list; absentees are
#: reported, never silently dropped.
TRAP_COMMANDER_FRAGMENTS: tuple[str, ...] = (
    "Magda",
    "Nissa",
    "Camellia",
    "Elenda",
    "Edgar",
    "Krenko",
    "Myrel",
    "Kess",
    "Rionya",
    "Kodama",
    "Marrow-Gnawer",
    "Ghoulcaller Gisa",
)

#: Per-commander NDCG@30 cliff threshold (mirrors
#: ``per_commander_ndcg.PER_COMMANDER_REGRESSION_THRESHOLD``).
CLIFF_THRESHOLD: float = -0.05

#: Monoculture derivation: commanders whose λ=0 top-30 has at least
#: this single-family contribution share (computed live — the sim does
#: not depend on the historical git-tag population).
MONOCULTURE_SHARE_THRESHOLD: float = 0.85

_DEFAULT_FIXTURE_100 = "tests/fixtures/golden_set_run.json"
_DEFAULT_FIXTURE_500 = "tests/fixtures/golden_set_run_500.json"
_DEFAULT_OUTPUT_DIR = ".audit/portfolio_sim"


class PortfolioSimError(RuntimeError):
    """Base error for the portfolio simulation harness."""


class CommanderResolutionError(PortfolioSimError):
    """A commander could not be resolved / produced an empty scored pool."""


class SelfCheckError(PortfolioSimError):
    """The λ=0 identity guarantee (or the recomposition contract) failed."""


# ---------------------------------------------------------------------------
# Per-commander live rescoring + decomposition
# ---------------------------------------------------------------------------


def load_fixture_commanders(path: Path | str) -> list[str]:
    """Read only the commander names from a golden-set fixture JSON.

    Malformed JSON or a wrong entry shape raises a named
    :class:`PortfolioSimError` carrying the fixture path (clean CLI
    error, exit 1) instead of a raw ``json``/``KeyError`` traceback.
    """
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PortfolioSimError(f"fixture {path}: malformed JSON ({exc})") from exc
    try:
        commanders = [e["commander"] for e in data.get("entries", [])]
    except (AttributeError, KeyError, TypeError) as exc:
        raise PortfolioSimError(
            f"fixture {path}: unexpected shape — expected an object with an "
            f"'entries' list of objects each carrying a 'commander' key ({exc!r})"
        ) from exc
    if not commanders:
        raise PortfolioSimError(f"fixture {path} has no commander entries")
    return commanders


def _net_contributions(us: UniversalScore) -> dict[str, float]:
    """Per-rule NET contribution for one candidate.

    Mirrors ``universal_scorer._emit_tensor_rows`` dedup exactly: each
    ``(rule_id, cmdr_event, cand_event, filter_group)`` key contributes
    at most once per direction; anti subtracts; zero-net rules dropped.
    Feeds the hidden-gem plausibility gate with tensor-equivalent rows.
    """
    per_rule: dict[str, float] = {}
    seen_syn: set[tuple[str, str, str, str]] = set()
    seen_anti: set[tuple[str, str, str, str]] = set()
    idf_weights = us.idf_weights
    for c in us.complements:
        key = (c.rule_id, c.cmdr_event, c.cand_event, c.filter_group)
        w = idf_weights.get(key, 1.0)
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
    return {rule_id: net for rule_id, net in per_rule.items() if net != 0.0}


def _identity_family_map(family_map: Mapping[str, str]) -> dict[str, str]:
    """Finer map granularity (R6a): undo the sibling merges by mapping
    every rule_id to itself. Built in-memory; the committed artifact is
    never edited."""
    return {rule_id: rule_id for rule_id in family_map}


def _flood_family(
    decomps: Mapping[str, ScoreDecomposition],
    top_30: Sequence[str],
) -> tuple[str | None, float]:
    """Dominant family + its contribution share over the λ=0 top-30."""
    family_totals: dict[str, float] = {}
    for card in top_30:
        for fam, v in decomps[card].family_syn.items():
            if v > 0:
                family_totals[fam] = family_totals.get(fam, 0.0) + v
    total = sum(family_totals.values())
    if not family_totals or total <= 0:
        return None, 0.0
    fam, top = max(family_totals.items(), key=lambda kv: (kv[1], kv[0]))
    return fam, top / total


@dataclass(frozen=True)
class CommanderSim:
    """One commander's cached scoring pass, ready for cell re-assembly."""

    commander: str
    #: Full production ranking (page order — the λ=0 total order).
    pool_order: tuple[str, ...]
    #: granularity -> {card -> decomposition} for every pool card.
    decomps: Mapping[str, Mapping[str, ScoreDecomposition]]
    cmc_lookup: Mapping[str, float]
    rank_lookup: Mapping[str, int]
    graded_labels: Mapping[str, float]
    edhrec_top_30: frozenset[str] | None
    #: Cards passing the mechanical-plausibility gate (assembly-independent).
    plausible: frozenset[str]
    base_top_30: tuple[str, ...]
    base_ndcg: float
    base_gem_rate: float | None
    base_gems: frozenset[str]
    flood_family: str | None
    flood_share: float
    scoring_seconds: float

    @property
    def monoculture(self) -> bool:
        return self.flood_share >= MONOCULTURE_SHARE_THRESHOLD

    @property
    def has_labels(self) -> bool:
        return bool(self.graded_labels)


def build_commander_sim(
    engine: SynergyEngine,
    edhrec_conn: sqlite3.Connection | None,
    commander: str,
    *,
    granularities: Sequence[str] = ("committed",),
    family_map: Mapping[str, str] | None = None,
    k: int = 30,
) -> CommanderSim:
    """Score one commander live and cache its decomposed pool.

    ``engine`` is annotated as ``SynergyEngine`` but only duck-typed on
    the surface used here (``page``, ``_score_cache``,
    ``_candidate_cache``) so tests can substitute fakes. Raises
    :class:`CommanderResolutionError` for a commander absent from the
    DB or with an empty scored pool (named error, never a silent skip)
    and :class:`SelfCheckError` when the per-card recomposition, the
    per-card family-attribution reconciliation, or the λ=0 assembly
    diverges from the engine's own ``page()`` output.
    """
    fmap = FAMILY_MAP if family_map is None else family_map
    started = time.perf_counter()
    try:
        page = engine.page([commander], offset=0, limit=1_000_000)
    except ValueError as exc:
        raise CommanderResolutionError(f"commander {commander!r} could not be scored: {exc}") from exc
    if not page.items:
        raise CommanderResolutionError(f"commander {commander!r} produced an empty scored pool")
    scoring_seconds = time.perf_counter() - started

    universal = engine._score_cache.get((commander,))
    if universal is None:
        raise PortfolioSimError(
            f"engine score cache missing entry for {commander!r} after page(); engine contract changed?"
        )

    # cmc / edhrec_rank tiebreak lookups, mirroring page() exactly
    # (candidate_rows is the same dict page()'s penalty context reads).
    candidate_cache = engine._candidate_cache
    if candidate_cache is None:
        raise PortfolioSimError(
            f"engine candidate cache not populated after page() for {commander!r}; engine contract changed?"
        )
    cmc_lookup: dict[str, float] = {}
    rank_lookup: dict[str, int] = {}
    for name, row in candidate_cache.candidate_rows.items():
        cmc_lookup[name] = row["cmc"] if row["cmc"] is not None else 99.0
        raw_rank = row.get("edhrec_rank")
        rank_lookup[name] = int(raw_rank) if raw_rank is not None else 10**9

    maps = {"committed": fmap, "identity": _identity_family_map(fmap)}
    decomps: dict[str, dict[str, ScoreDecomposition]] = {}
    for gran in granularities:
        if gran not in maps:
            raise ValueError(f"unknown granularity {gran!r} (known: {sorted(maps)})")
        per_card: dict[str, ScoreDecomposition] = {}
        for rec in page.items:
            try:
                dec = decompose_universal_score(universal[rec.card], maps[gran])
            except FamilyReconciliationError as exc:
                raise SelfCheckError(f"{commander}: family-attribution reconciliation failed: {exc}") from exc
            if dec.total != rec.total_score:
                raise SelfCheckError(
                    f"{commander}: recomposed total for {rec.card!r} is {dec.total!r} "
                    f"but page() reports {rec.total_score!r} (bitwise mismatch — "
                    "decomposition drifted from to_legacy_buckets)"
                )
            per_card[rec.card] = dec
        decomps[gran] = per_card

    pool_order = tuple(rec.card for rec in page.items)
    base_top_30 = pool_order[:k]

    # λ=0 identity self-check against the engine's actual page() order.
    check = assemble_portfolio(
        decomps[granularities[0]],
        lam=0.0,
        decay="exp",
        k=k,
        cmc_lookup=cmc_lookup,
        rank_lookup=rank_lookup,
    )
    if check.cards != base_top_30:
        diff = [(a, b) for a, b in zip(check.cards, base_top_30, strict=False) if a != b][:5]
        raise SelfCheckError(
            f"{commander}: λ=0 assembly diverges from page() top-{k}; first mismatches (assembled, page): {diff}"
        )

    # Tensor-equivalent contributions over ALL scored candidates (the
    # same cohort build_fixture feeds the gem metric) → plausibility set.
    contributions: list[tuple[str, str, float]] = []
    for cand, us in universal.items():
        for rule_id, net in _net_contributions(us).items():
            contributions.append((cand, rule_id, net))
    n_rules_map, totals_map = _aggregate_contributions(contributions)
    median = _cohort_median(totals_map)
    plausible = frozenset(c for c in pool_order if _plausibility_from_maps(c, n_rules_map, totals_map, median))

    graded_labels: dict[str, float] = {}
    edhrec_top_30: frozenset[str] | None = None
    if edhrec_conn is not None:
        labels = load_edhrec_labels(edhrec_conn, commander)
        graded_labels = dict(labels.graded_labels)
        edhrec_top_30 = labels.top_30_set

    base_ndcg = compute_ndcg(list(base_top_30), graded_labels)

    base_gem_rate: float | None = None
    base_gems: frozenset[str] = frozenset()
    if edhrec_top_30 is not None:
        entry = hidden_gem_hit_rate_for_commander(
            commander,
            list(base_top_30),
            set(edhrec_top_30),
            contributions,
        )
        if entry is not None:
            base_gem_rate = entry.rate
            base_gems = frozenset(entry.hidden_cards)
            # Cross-check the incremental plausible-set path used per
            # sweep cell against the canonical helper (drift guard).
            via_sets = (set(base_top_30) - set(edhrec_top_30)) & plausible
            if via_sets != set(base_gems):
                raise SelfCheckError(
                    f"{commander}: plausible-set gem derivation diverged from "
                    f"hidden_gem_hit_rate_for_commander: {sorted(via_sets ^ set(base_gems))}"
                )

    primary = decomps[granularities[0]]
    flood_family, flood_share = _flood_family(primary, base_top_30)

    # Free the engine-side score dict (multi-GB across a 500-cmdr run —
    # the forensics extract_live_ranking precedent).
    engine._score_cache.clear()

    return CommanderSim(
        commander=commander,
        pool_order=pool_order,
        decomps=decomps,
        cmc_lookup=cmc_lookup,
        rank_lookup=rank_lookup,
        graded_labels=graded_labels,
        edhrec_top_30=edhrec_top_30,
        plausible=plausible,
        base_top_30=base_top_30,
        base_ndcg=base_ndcg,
        base_gem_rate=base_gem_rate,
        base_gems=base_gems,
        flood_family=flood_family,
        flood_share=flood_share,
        scoring_seconds=scoring_seconds,
    )


def _iter_sims(
    engine: SynergyEngine,
    edhrec_conn: sqlite3.Connection | None,
    commanders: Sequence[str],
    *,
    granularities: Sequence[str] = ("committed",),
) -> Iterable[CommanderSim]:
    """Yield one :class:`CommanderSim` per commander, printing progress."""
    total = len(commanders)
    for i, commander in enumerate(commanders, start=1):
        sim = build_commander_sim(engine, edhrec_conn, commander, granularities=granularities)
        print(
            f"[{i}/{total}] {commander}: scored in {sim.scoring_seconds:.1f}s "
            f"(pool={len(sim.pool_order)}, λ=0 self-check OK)",
            file=sys.stderr,
        )
        yield sim


# ---------------------------------------------------------------------------
# Metric helpers
# ---------------------------------------------------------------------------


def gem_rate_for_assembly(
    sim: CommanderSim,
    assembled: Sequence[str],
    *,
    k: int = 30,
) -> tuple[float, frozenset[str]] | None:
    """hidden_gem_hit_rate for an assembled top-``k`` (None without EDHREC data).

    ``k`` must match the assembly depth the caller used — the rate
    denominator is the selection size, mirroring
    ``hidden_gem_hit_rate_for_commander``'s ``/30`` at the default.
    """
    if sim.edhrec_top_30 is None:
        return None
    gems = frozenset((set(assembled) - sim.edhrec_top_30) & sim.plausible)
    return len(gems) / k, gems


def gem_quality_predicate(dec: ScoreDecomposition, flood_family: str | None) -> bool | None:
    """R9a mechanical gem-quality predicate for one gem card.

    ``None`` → staple-only / no-rule gem (excluded from the predicate
    denominator, reported separately). Otherwise True iff the card
    draws positive synergy from ≥2 distinct families AND ≥1 of them is
    outside the commander's flood family.
    """
    fams = [fam for fam, v in dec.family_syn.items() if v > 0]
    if not fams:
        return None
    return len(fams) >= 2 and any(fam != flood_family for fam in fams)


def _predicate_counts(
    sim: CommanderSim,
    gems: Iterable[str],
    decomps: Mapping[str, ScoreDecomposition],
) -> tuple[int, int, int]:
    """(n_pass, n_evaluable, n_excluded) over a gem population."""
    n_pass = n_eval = n_excl = 0
    for card in gems:
        verdict = gem_quality_predicate(decomps[card], sim.flood_family)
        if verdict is None:
            n_excl += 1
        else:
            n_eval += 1
            n_pass += int(verdict)
    return n_pass, n_eval, n_excl


def _cv(values: Sequence[float]) -> float:
    """Coefficient of variation (tie-density instrument, Gate-B shape)."""
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    if abs(mean) < 1e-12:
        return 0.0
    return statistics.pstdev(values) / abs(mean)


def bootstrap_band(
    values: Sequence[float],
    *,
    seed: int,
    n_boot: int = 2000,
) -> dict[str, float | int]:
    """Bootstrap the sampling distribution of the mean (fixed seed).

    Per-commander resampling with replacement; returns the observed
    mean, the bootstrap std of the mean, the central 95% interval of
    resampled means, and its half-width — the "noise band" the plan's
    thresholds derive from. Deterministic under a fixed seed.
    """
    if not values:
        return {
            "n": 0,
            "n_boot": n_boot,
            "mean": 0.0,
            "boot_std": 0.0,
            "ci95_low": 0.0,
            "ci95_high": 0.0,
            "half_width": 0.0,
        }
    rng = random.Random(seed)  # noqa: S311 — statistical resampling, not crypto
    n = len(values)
    mean = sum(values) / n
    means: list[float] = []
    for _ in range(n_boot):
        acc = 0.0
        for _ in range(n):
            acc += values[rng.randrange(n)]
        means.append(acc / n)
    means.sort()
    lo = means[int(0.025 * (n_boot - 1))]
    hi = means[int(0.975 * (n_boot - 1))]
    return {
        "n": n,
        "n_boot": n_boot,
        "mean": mean,
        "boot_std": statistics.pstdev(means),
        "ci95_low": lo,
        "ci95_high": hi,
        "half_width": (hi - lo) / 2.0,
    }


def resolve_trap_commanders(commanders: Sequence[str]) -> tuple[dict[str, str], list[str]]:
    """Resolve trap fragments to full names from the fixture list.

    Returns ``(fragment -> full name, absent fragments)``.
    """
    resolved: dict[str, str] = {}
    absent: list[str] = []
    for fragment in TRAP_COMMANDER_FRAGMENTS:
        matches = [c for c in commanders if fragment.lower() in c.lower()]
        if matches:
            resolved[fragment] = sorted(matches)[0]
        else:
            absent.append(fragment)
    return resolved, absent


def _ablate_rank_bonus(decomps: Mapping[str, ScoreDecomposition]) -> dict[str, ScoreDecomposition]:
    """Rank_bonus-ablated copies: subtract rank_bonus from total+residual.

    EDHREC leakage via rank_bonus must not masquerade as mechanical
    NDCG recovery (plan-002 Unit 3 attachment) — the sweep re-runs each
    flagged cell on this ablated surface and reports the delta against
    an equally-ablated λ=0 baseline.
    """
    import dataclasses

    out: dict[str, ScoreDecomposition] = {}
    for name, d in decomps.items():
        if d.rank_bonus:
            out[name] = dataclasses.replace(
                d,
                total=d.total - d.rank_bonus,
                residual=d.residual - d.rank_bonus,
                rank_bonus=0.0,
            )
        else:
            out[name] = d
    return out


# ---------------------------------------------------------------------------
# Subcommand: bands
# ---------------------------------------------------------------------------


def run_bands(
    engine: SynergyEngine,
    edhrec_conn: sqlite3.Connection,
    commanders: Sequence[str],
    *,
    seed: int,
    n_boot: int,
    fixture: str,
) -> dict[str, Any]:
    """Bootstrap threshold derivation (plan Unit 2, flow I1/I2 + R9a floor)."""
    ndcg_values: list[float] = []
    gem_values: list[float] = []
    per_commander: list[dict[str, Any]] = []
    r9a: dict[str, list[int]] = {"monoculture": [0, 0, 0], "other": [0, 0, 0]}
    monoculture_commanders: list[str] = []

    for sim in _iter_sims(engine, edhrec_conn, commanders):
        stratum = "monoculture" if sim.monoculture else "other"
        if sim.monoculture:
            monoculture_commanders.append(sim.commander)
        if sim.has_labels:
            ndcg_values.append(sim.base_ndcg)
        if sim.base_gem_rate is not None:
            gem_values.append(sim.base_gem_rate)
        n_pass, n_eval, n_excl = _predicate_counts(sim, sim.base_gems, sim.decomps["committed"])
        bucket = r9a[stratum]
        bucket[0] += n_pass
        bucket[1] += n_eval
        bucket[2] += n_excl
        per_commander.append(
            {
                "commander": sim.commander,
                "ndcg": sim.base_ndcg,
                "gem_rate": sim.base_gem_rate,
                "monoculture": sim.monoculture,
                "flood_family": sim.flood_family,
                "flood_share": sim.flood_share,
                "pool": len(sim.pool_order),
            }
        )

    def _stratum_report(counts: list[int]) -> dict[str, Any]:
        n_pass, n_eval, n_excl = counts
        return {
            "pass_rate": (n_pass / n_eval) if n_eval else None,
            "n_pass": n_pass,
            "n_evaluable": n_eval,
            "n_excluded_staple_only": n_excl,
        }

    overall = [r9a["monoculture"][i] + r9a["other"][i] for i in range(3)]
    return {
        "instrument": "portfolio_sim bands",
        "fixture": fixture,
        "seed": seed,
        "n_commanders": len(per_commander),
        "ndcg_band": bootstrap_band(ndcg_values, seed=seed, n_boot=n_boot),
        "gem_band": bootstrap_band(gem_values, seed=seed + 1, n_boot=n_boot),
        "r9a_baseline": {
            "monoculture": _stratum_report(r9a["monoculture"]),
            "other": _stratum_report(r9a["other"]),
            "overall": _stratum_report(overall),
        },
        "monoculture_commanders": sorted(monoculture_commanders),
        "per_commander": per_commander,
    }


# ---------------------------------------------------------------------------
# Subcommand: share (R7b)
# ---------------------------------------------------------------------------


def run_share(
    engine: SynergyEngine,
    edhrec_conn: sqlite3.Connection,
    commanders: Sequence[str],
    *,
    lam: float,
    decay: str,
    discount_base: str,
    fixture: str,
) -> dict[str, Any]:
    """Empirical addressable share: misses recovered by assembly at λ_ref."""
    strata = {"diff_family": [0, 0], "same_family": [0, 0], "no_rules": [0, 0]}
    per_commander: list[dict[str, Any]] = []
    skipped: list[str] = []

    for sim in _iter_sims(engine, edhrec_conn, commanders):
        if sim.edhrec_top_30 is None:
            skipped.append(sim.commander)
            continue
        pool_set = set(sim.pool_order)
        misses = sorted((sim.edhrec_top_30 - set(sim.base_top_30)) & pool_set)
        decomps = sim.decomps["committed"]
        assembled = assemble_portfolio(
            decomps,
            lam=lam,
            decay=decay,
            k=30,
            cmc_lookup=sim.cmc_lookup,
            rank_lookup=sim.rank_lookup,
            discount_base=discount_base,
        )
        assembled_set = set(assembled.cards)
        recovered = [m for m in misses if m in assembled_set]
        for miss in misses:
            fams = {f: v for f, v in decomps[miss].family_syn.items() if v > 0}
            if not fams:
                stratum = "no_rules"
            else:
                dominant = max(fams.items(), key=lambda kv: (kv[1], kv[0]))[0]
                stratum = "same_family" if dominant == sim.flood_family else "diff_family"
            strata[stratum][0] += 1
            strata[stratum][1] += int(miss in assembled_set)
        per_commander.append(
            {
                "commander": sim.commander,
                "n_misses": len(misses),
                "n_recovered": len(recovered),
                "recovered": recovered,
                "flood_family": sim.flood_family,
                "monoculture": sim.monoculture,
            }
        )

    total_misses = sum(row["n_misses"] for row in per_commander)
    total_recovered = sum(row["n_recovered"] for row in per_commander)
    return {
        "instrument": "portfolio_sim share (R7b)",
        "fixture": fixture,
        "lam": lam,
        "decay": decay,
        "discount_base": discount_base,
        "n_commanders": len(per_commander),
        "skipped_no_edhrec": skipped,
        "total_misses": total_misses,
        "total_recovered": total_recovered,
        "addressable_share": (total_recovered / total_misses) if total_misses else None,
        "by_stratum": {
            name: {
                "n_misses": counts[0],
                "n_recovered": counts[1],
                "share": (counts[1] / counts[0]) if counts[0] else None,
            }
            for name, counts in strata.items()
        },
        "per_commander": per_commander,
    }


# ---------------------------------------------------------------------------
# Subcommand: sweep
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SweepCell:
    form: str
    lam: float
    granularity: str
    discount_base: str

    @property
    def label(self) -> str:
        return f"{self.form}/λ={self.lam:g}/{self.granularity}/{self.discount_base}"


@dataclass
class _CellAcc:
    """Per-cell accumulator across commanders."""

    ndcg_deltas: list[float] = field(default_factory=list)
    cliffs: list[tuple[str, float]] = field(default_factory=list)
    gem_deltas: list[float] = field(default_factory=list)
    mono_deltas: list[float] = field(default_factory=list)
    trap_deltas: dict[str, float] = field(default_factory=dict)
    cv_values: list[float] = field(default_factory=list)
    ablated_deltas: list[float] = field(default_factory=list)
    r9a_gained: dict[str, list[int]] = field(default_factory=lambda: {"monoculture": [0, 0, 0], "other": [0, 0, 0]})


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def run_sweep(
    engine: SynergyEngine,
    edhrec_conn: sqlite3.Connection,
    commanders: Sequence[str],
    *,
    forms: Sequence[str],
    lams: Sequence[float],
    granularities: Sequence[str],
    bases: Sequence[str],
    fixture: str,
) -> dict[str, Any]:
    """The R0 grid: per-cell aggregate metrics + sidecars (plan Unit 2)."""
    cells = [
        SweepCell(form=form, lam=lam, granularity=gran, discount_base=base)
        for form in forms
        for lam in lams
        for gran in granularities
        for base in bases
    ]
    trap_by_name, trap_absent = resolve_trap_commanders(commanders)
    trap_names = set(trap_by_name.values())

    acc: dict[SweepCell, _CellAcc] = {cell: _CellAcc() for cell in cells}
    base_cvs: list[float] = []
    r9a_baseline: dict[str, list[int]] = {"monoculture": [0, 0, 0], "other": [0, 0, 0]}
    n_scored = 0
    n_with_gem = 0
    monoculture_commanders: list[str] = []
    scoring_seconds: list[float] = []

    build_granularities = tuple(dict.fromkeys(["committed", *granularities]))
    for sim in _iter_sims(engine, edhrec_conn, commanders, granularities=build_granularities):
        n_scored += 1
        scoring_seconds.append(sim.scoring_seconds)
        stratum = "monoculture" if sim.monoculture else "other"
        if sim.monoculture:
            monoculture_commanders.append(sim.commander)
        if sim.base_gem_rate is not None:
            n_with_gem += 1
        base_cvs.append(_cv([sim.decomps["committed"][c].total for c in sim.base_top_30]))
        n_pass, n_eval, n_excl = _predicate_counts(sim, sim.base_gems, sim.decomps["committed"])
        r9a_baseline[stratum][0] += n_pass
        r9a_baseline[stratum][1] += n_eval
        r9a_baseline[stratum][2] += n_excl

        # Ablated surfaces built once per (commander, granularity).
        ablated: dict[str, dict[str, ScoreDecomposition]] = {}
        ablated_base_ndcg: dict[str, float] = {}
        for gran in granularities:
            abl = _ablate_rank_bonus(sim.decomps[gran])
            ablated[gran] = abl
            abl_base = assemble_portfolio(
                abl, lam=0.0, decay="exp", k=30, cmc_lookup=sim.cmc_lookup, rank_lookup=sim.rank_lookup
            )
            ablated_base_ndcg[gran] = compute_ndcg(list(abl_base.cards), dict(sim.graded_labels))

        for cell in cells:
            decomps = sim.decomps[cell.granularity]
            result = assemble_portfolio(
                decomps,
                lam=cell.lam,
                decay=cell.form,
                k=30,
                cmc_lookup=sim.cmc_lookup,
                rank_lookup=sim.rank_lookup,
                discount_base=cell.discount_base,
            )
            cell_acc = acc[cell]
            top = list(result.cards)
            if sim.has_labels:
                ndcg = compute_ndcg(top, dict(sim.graded_labels))
                delta = ndcg - sim.base_ndcg
                cell_acc.ndcg_deltas.append(delta)
                if delta < CLIFF_THRESHOLD:
                    cell_acc.cliffs.append((sim.commander, delta))
                if sim.monoculture:
                    cell_acc.mono_deltas.append(delta)
                if sim.commander in trap_names:
                    cell_acc.trap_deltas[sim.commander] = delta

                abl_result = assemble_portfolio(
                    ablated[cell.granularity],
                    lam=cell.lam,
                    decay=cell.form,
                    k=30,
                    cmc_lookup=sim.cmc_lookup,
                    rank_lookup=sim.rank_lookup,
                    discount_base=cell.discount_base,
                )
                abl_ndcg = compute_ndcg(list(abl_result.cards), dict(sim.graded_labels))
                cell_acc.ablated_deltas.append(abl_ndcg - ablated_base_ndcg[cell.granularity])

            gem = gem_rate_for_assembly(sim, top, k=30)
            if gem is not None and sim.base_gem_rate is not None:
                gem_rate, gems = gem
                cell_acc.gem_deltas.append(gem_rate - sim.base_gem_rate)
                gained = gems - sim.base_gems
                # Predicate decompositions must match the cell's own
                # granularity — the discount that admitted a gained gem
                # was computed on cell.granularity's family vectors, so
                # grading it against the committed map would misreport
                # identity-map cells (correctness fix, review P2).
                g_pass, g_eval, g_excl = _predicate_counts(sim, gained, sim.decomps[cell.granularity])
                bucket = cell_acc.r9a_gained[stratum]
                bucket[0] += g_pass
                bucket[1] += g_eval
                bucket[2] += g_excl

            cell_acc.cv_values.append(_cv([p.effective_score for p in result.picks]))

    def _stratum_rate(counts: list[int]) -> float | None:
        return (counts[0] / counts[1]) if counts[1] else None

    cell_reports: list[dict[str, Any]] = []
    for cell in cells:
        a = acc[cell]
        mean_delta = _mean(a.ndcg_deltas)
        flagged = bool(a.ndcg_deltas) and mean_delta is not None and mean_delta > 0 and not a.cliffs
        cell_reports.append(
            {
                "form": cell.form,
                "lam": cell.lam,
                "granularity": cell.granularity,
                "discount_base": cell.discount_base,
                "n_ndcg": len(a.ndcg_deltas),
                "mean_ndcg_delta": mean_delta,
                "cliff_count": len(a.cliffs),
                "cliffs": sorted(a.cliffs, key=lambda t: t[1])[:10],
                "mean_gem_delta": _mean(a.gem_deltas),
                "monoculture_mean_delta": _mean(a.mono_deltas),
                "trap_deltas": dict(sorted(a.trap_deltas.items())),
                "mean_cv_top30": _mean(a.cv_values),
                "rank_bonus_ablated_mean_delta": _mean(a.ablated_deltas),
                "r9a_gained_pass_rate": {stratum: _stratum_rate(counts) for stratum, counts in a.r9a_gained.items()},
                "r9a_gained_counts": {stratum: list(counts) for stratum, counts in a.r9a_gained.items()},
                "survives": flagged,
            }
        )

    return {
        "instrument": "portfolio_sim sweep",
        "fixture": fixture,
        "n_commanders": n_scored,
        "n_with_gem": n_with_gem,
        "mean_scoring_seconds_per_commander": _mean(scoring_seconds),
        "trap_commanders": trap_by_name,
        "trap_fragments_absent": trap_absent,
        "monoculture_commanders": sorted(monoculture_commanders),
        "monoculture_threshold": MONOCULTURE_SHARE_THRESHOLD,
        "base_mean_cv_top30": _mean(base_cvs),
        "r9a_baseline_pass_rate": {stratum: _stratum_rate(counts) for stratum, counts in r9a_baseline.items()},
        "r9a_baseline_counts": {stratum: list(counts) for stratum, counts in r9a_baseline.items()},
        "cells": cell_reports,
        "survival_predicate": (
            "zero cliffs < -0.05 AND aggregate NDCG delta > 0 "
            "(gem non-regression band applied at Unit 4 against the bands report)"
        ),
    }


def render_sweep_markdown(report: dict[str, Any]) -> str:
    """Compact Markdown table for the sweep report."""
    lines = [
        "# portfolio_sim sweep",
        "",
        f"fixture: {report['fixture']}  |  commanders: {report['n_commanders']}"
        f"  |  base CV(top-30): {report['base_mean_cv_top30']}",
        f"monoculture-{len(report['monoculture_commanders'])} (share >= "
        f"{report['monoculture_threshold']}): {', '.join(report['monoculture_commanders']) or '(none)'}",
    ]
    if report["trap_fragments_absent"]:
        lines.append(f"trap fragments absent from fixture: {', '.join(report['trap_fragments_absent'])}")
    lines += [
        "",
        f"{'cell':<36} {'Δndcg':>9} {'cliffs':>6} {'Δgem':>9} {'monoΔ':>9} {'CV':>7} {'ablΔ':>9} {'ok':>3}",
        f"{'-' * 36} {'-' * 9} {'-' * 6} {'-' * 9} {'-' * 9} {'-' * 7} {'-' * 9} {'-' * 3}",
    ]

    def _f(v: float | None, width: int = 9, spec: str = "+.4f") -> str:
        return format(v, spec).rjust(width) if v is not None else "-".rjust(width)

    for cell in report["cells"]:
        label = f"{cell['form']}/λ={cell['lam']:g}/{cell['granularity']}/{cell['discount_base']}"
        lines.append(
            f"{label:<36} {_f(cell['mean_ndcg_delta'])} {cell['cliff_count']:>6} "
            f"{_f(cell['mean_gem_delta'])} {_f(cell['monoculture_mean_delta'])} "
            f"{_f(cell['mean_cv_top30'], 7, '.3f')} {_f(cell['rank_bonus_ablated_mean_delta'])} "
            f"{'Y' if cell['survives'] else 'n':>3}"
        )

    lines += ["", "## Trap sidecar (NDCG delta per cell)", ""]
    trap_names = sorted({name for cell in report["cells"] for name in cell["trap_deltas"]})
    if trap_names:
        for cell in report["cells"]:
            label = f"{cell['form']}/λ={cell['lam']:g}/{cell['granularity']}/{cell['discount_base']}"
            # Full names on purpose: truncating at the first comma
            # collapsed distinct commanders sharing a given name
            # ("Gisa, Glorious Resurrector" vs "Ghoulcaller Gisa").
            rendered = ", ".join(f"{n}: {cell['trap_deltas'].get(n, float('nan')):+.4f}" for n in trap_names)
            lines.append(f"- {label}: {rendered}")
    else:
        lines.append("(no trap commanders present in this run)")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _add_common_args(p: argparse.ArgumentParser, *, default_fixture: str) -> None:
    p.add_argument("--db", default="data/synergy.db", help="synergy DB path")
    p.add_argument("--edhrec-db", default="data/tags.db", help="EDHREC tags DB path")
    p.add_argument("--fixture", default=default_fixture, help="golden-set fixture (commander names only)")
    p.add_argument("--commanders", type=int, default=None, help="limit to the first N fixture commanders")
    p.add_argument(
        "--commander",
        metavar="NAME",
        action="append",
        default=None,
        help=(
            "restrict to this fixture commander (exact name; repeatable; "
            "mirrors `bench.py audit --commander`); applied before --commanders"
        ),
    )
    p.add_argument("--output", default=_DEFAULT_OUTPUT_DIR, help="report output directory")


_EXIT_CODE_EPILOG = """\
Exit codes (mirrors scripts/bench.py's taxonomy):
  0  Run completed; reports written to --output.
  1  Named harness failure: commander resolution, malformed fixture,
     unmapped rule family, or an invalid parameter (e.g. negative --lam).
  2  Usage / precondition error: missing DB / EDHREC DB / fixture, or a
     --commander name absent from the fixture list.
  3  Self-check failure: the λ=0 identity, the bitwise per-card
     recomposition, or the family-attribution reconciliation diverged.
     Do not trust any report from an exit-3 run.
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="portfolio_sim.py",
        description="R0 portfolio-selection kill-test instrument (plan 2026-07-02-004 Unit 2)",
        epilog=_EXIT_CODE_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_bands = sub.add_parser("bands", help="bootstrap threshold derivation on the 500-cmdr fixture")
    _add_common_args(p_bands, default_fixture=_DEFAULT_FIXTURE_500)
    p_bands.add_argument("--seed", type=int, default=17)
    p_bands.add_argument("--n-boot", type=int, default=2000)

    p_share = sub.add_parser("share", help="empirical addressable-share readout (R7b)")
    _add_common_args(p_share, default_fixture=_DEFAULT_FIXTURE_100)
    p_share.add_argument("--lam", type=float, default=0.5, help="reference decay depth")
    p_share.add_argument("--decay", default="exp", choices=("exp", "harmonic"))
    p_share.add_argument("--discount-base", default="full", choices=("full", "flat"))

    p_sweep = sub.add_parser("sweep", help="full R0 grid with sidecars")
    _add_common_args(p_sweep, default_fixture=_DEFAULT_FIXTURE_100)
    p_sweep.add_argument("--forms", nargs="+", default=["exp", "harmonic"], choices=("exp", "harmonic"))
    p_sweep.add_argument("--lams", nargs="+", type=float, default=[0.1, 0.25, 0.5, 1.0])
    p_sweep.add_argument(
        "--granularities", nargs="+", default=["committed", "identity"], choices=("committed", "identity")
    )
    p_sweep.add_argument("--bases", nargs="+", default=["full", "flat"], choices=("full", "flat"))

    return parser


def _write_report(outdir: Path, name: str, report: dict[str, Any]) -> Path:
    outdir.mkdir(parents=True, exist_ok=True)
    path = outdir / name
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


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
    if args.commander:
        available = set(commanders)
        missing = sorted(name for name in args.commander if name not in available)
        if missing:
            print(
                f"error: --commander name(s) not in fixture {fixture_path}: {missing} "
                "(exact-name match against the fixture commander list)",
                file=sys.stderr,
            )
            return 2
        requested = set(args.commander)
        commanders = [c for c in commanders if c in requested]
    if args.commanders is not None:
        commanders = commanders[: args.commanders]

    # Local import: engine pulls the scoring stack; keep module import light.
    from mtg_synergy_graph.engine import SynergyEngine

    outdir = Path(args.output)
    edhrec_conn = sqlite3.connect(edhrec_path)
    edhrec_conn.row_factory = sqlite3.Row
    engine = SynergyEngine(db_path)
    started = time.perf_counter()
    try:
        if args.command == "bands":
            report = run_bands(
                engine, edhrec_conn, commanders, seed=args.seed, n_boot=args.n_boot, fixture=str(fixture_path)
            )
            path = _write_report(outdir, "bands.json", report)
            nb, gb = report["ndcg_band"], report["gem_band"]
            print(f"bands: n={report['n_commanders']} seed={report['seed']}")
            print(
                f"  NDCG@30  mean={nb['mean']:.4f}  95% CI [{nb['ci95_low']:.4f}, {nb['ci95_high']:.4f}]  "
                f"noise band (half-width) = {nb['half_width']:.4f}"
            )
            print(
                f"  gem rate mean={gb['mean']:.4f}  95% CI [{gb['ci95_low']:.4f}, {gb['ci95_high']:.4f}]  "
                f"non-regression band (half-width) = {gb['half_width']:.4f}"
            )
            for stratum, row in report["r9a_baseline"].items():
                rate = row["pass_rate"]
                print(
                    f"  R9a baseline pass rate [{stratum}]: "
                    f"{'n/a' if rate is None else format(rate, '.4f')} "
                    f"({row['n_pass']}/{row['n_evaluable']}, staple-only excluded: {row['n_excluded_staple_only']})"
                )
            print(f"report: {path}")
        elif args.command == "share":
            report = run_share(
                engine,
                edhrec_conn,
                commanders,
                lam=args.lam,
                decay=args.decay,
                discount_base=args.discount_base,
                fixture=str(fixture_path),
            )
            path = _write_report(outdir, "share.json", report)
            share = report["addressable_share"]
            print(
                f"share (λ={args.lam:g}, {args.decay}, {args.discount_base}): "
                f"{report['total_recovered']}/{report['total_misses']} misses recovered "
                f"= {'n/a' if share is None else format(share, '.4f')}"
            )
            for stratum, row in report["by_stratum"].items():
                s = row["share"]
                print(
                    f"  [{stratum}] {row['n_recovered']}/{row['n_misses']} = {'n/a' if s is None else format(s, '.4f')}"
                )
            print(f"report: {path}")
        else:  # sweep
            report = run_sweep(
                engine,
                edhrec_conn,
                commanders,
                forms=args.forms,
                lams=args.lams,
                granularities=args.granularities,
                bases=args.bases,
                fixture=str(fixture_path),
            )
            path = _write_report(outdir, "sweep.json", report)
            md_path = outdir / "sweep.md"
            md_path.write_text(render_sweep_markdown(report), encoding="utf-8")
            print(render_sweep_markdown(report))
            print(f"reports: {path}, {md_path}")
    except SelfCheckError as exc:
        # bench.py exit-3 analog: a failed self-check means the
        # instrument itself cannot be trusted — distinct from a mere
        # resolution/data failure (exit 1).
        print(f"error: {exc}", file=sys.stderr)
        return 3
    except PortfolioSimError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except (UnmappedRuleFamilyError, ValueError) as exc:
        # Bad user parameters (e.g. negative --lam) or an unmapped
        # rule_id surface as a clean one-line error, never a traceback.
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        engine.close()
        edhrec_conn.close()

    print(f"total wall clock: {time.perf_counter() - started:.1f}s", file=sys.stderr)
    return 0
