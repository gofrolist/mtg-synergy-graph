"""``bench.py audit --forensics`` — report rendering + CLI handler
(Unit 4 of plan 2026-06-10-001).

Module-owned handler following the ``per_commander_ndcg.py`` precedent:
the classification/metric/justified core lives in
:mod:`bench.forensics` (Units 1–3); this module adds the read-side
enrichment (tensor-derived displacer profiles, OUTRANKED rule-family
attribution, NO_RULES port-shape drill-down), the pure markdown/JSON
renderer pair, and :func:`handle_forensics`.

Rule-family grouping (documented choice, R2): the complement-rules
registry (``complement_rules/registry.py``) exposes NO family
attribute on rules — ``RULE_GATES`` is a flat ``(rule_id,
predicate)`` registry. Per the plan's documented fallback, grouping is
by rule_id, refined only by the three families cleanly derivable from
the repo's naming conventions (the declarative seed + CLAUDE.md rule
catalogue): ``*_tribal`` (peer-tribal keyword family), ``*_feeder``
(gated axis-feeders), ``repl_*`` (replacement-stack family). See
:func:`rule_family`.

Exit codes (plan Unit 4): ``0`` for a successful run — findings are
not errors; ``2`` on usage / stale input
(:class:`ForensicsPreconditionError`) or a failed reconciliation
(:class:`ForensicsReconciliationError`), with NOTHING written. Exit 1
(drift) is reserved for the main audit verdict and never emitted here.

Output discipline: default output goes to ``.audit/forensics.{md,json}``
via the ``_write_default_output`` degrade pattern (OSError → stderr
warning, never a failure — the report has already been printed to
stdout); ``--output PATH`` writes there instead and skips ``.audit/``.

Read-only diagnostic: nothing here mutates either database or the
pinned fixture. On full success (preconditions + reconciliation +
render all passed) the handler appends one provenance-stamped row to
the forensics history CSV (Unit 5, :mod:`bench.forensics_history` —
default ``.audit/forensics_history.csv``, ``--forensics-history PATH``
to override); the append itself degrades to a stderr warning on write
failure and never changes the exit code.

Tiebreaker ablation (Unit 6, R8 — one-off measurement mode): with the
``--ablate-tiebreak`` companion flag the handler additionally runs
:func:`bench.forensics.compute_tiebreak_ablation` over the
already-captured rankings + labels (no extra scoring pass) and the
renderers append a "Tiebreaker ablation (R8)" section (md + json) —
ONLY when the flag is set; absent otherwise. The mandatory
production-key self-check failing
(:class:`bench.forensics.TiebreakSelfCheckError`) maps to exit 2
BEFORE anything is written (no report, no history row). The forensics
history row is NOT extended (one-off mode; no schema change).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mtg_synergy_graph.bench.forensics import (
    _EM_DASH,
    BUCKET_NO_RULES,
    BUCKET_OUTRANKED,
    BUCKETS,
    N_RULES_BIN_LABELS,
    RATIO_BIN_LABELS,
    TIEBREAK_FLAG_THRESHOLD,
    CommanderForensics,
    ForensicsPreconditionError,
    ForensicsReconciliationError,
    ForensicsReport,
    RankBonusAblation,
    TiebreakAblation,
    TiebreakSelfCheckError,
    bucket_proportions,
    compute_forensics,
    compute_rank_bonus_ablation,
    compute_tiebreak_ablation,
    load_tensor_contributions,
    synergy_content_digest,
)
from mtg_synergy_graph.bench.forensics_history import (
    DEFAULT_FORENSICS_HISTORY_PATH,
    append_forensics_run,
    build_history_row,
    compute_gem_rate_forensics,
    edhrec_snapshot_digest,
    fixture_file_sha256,
)
from mtg_synergy_graph.bench.tensor import compute_config_hash
from mtg_synergy_graph.db import open_db
from mtg_synergy_graph.validate import edhrec_labels_for_commander

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Golden-set bubble caveat (R2 header requirement; rule-quality-gates
#: learning: tensor views are golden-set-bubble-bound and the report
#: must state the boundary).
GOLDEN_SET_BUBBLE_CAVEAT = (
    "Golden-set bubble: tensor-derived sections (OUTRANKED rule families, "
    "displacer profiles, tensor-candidate counts) only see this fixture's "
    "commanders and the rules that fired on them at the current "
    "config_hash; commanders and rules outside the golden set are "
    "invisible to those views."
)

#: R9 aggregate-saturation annotation, rendered verbatim when every
#: divergent pick across the run passed the plausibility gate.
GATE_SATURATED_ANNOTATION = "gate too loose to discriminate — this is a finding about the gate"

#: Markdown header of the Unit-6 (R8) ablation section — present in
#: the report ONLY when ``--ablate-tiebreak`` was passed.
TIEBREAK_SECTION_HEADER = "## Tiebreaker ablation (R8)"

#: OUTRANKED rank-quantile bands (R2). OUTRANKED misses always carry a
#: LIVE rank >= 61 (ranks 31..60 are NEAR_MISS at the default top_n).
OUTRANKED_RANK_BANDS: tuple[tuple[str, int, int | None], ...] = (
    ("61-100", 61, 100),
    ("101-500", 101, 500),
    (">500", 501, None),
)

#: Markdown truncation limits (JSON carries the full lists).
DISPLACER_TOP_FAMILIES_MD = 3
OUTRANKED_TOP_FAMILIES_MD = 10
NO_RULES_TOP_SHAPES_MD = 20


def rule_family(rule_id: str) -> str:
    """Map a rule_id to its reporting family.

    The registry has no family attribute, so this is the plan's
    documented fallback (group by rule_id) refined by the three
    naming-convention families that ARE cleanly derivable:

    * ``repl_*``     → ``replacement_stack`` (declarative replacement family)
    * ``*_tribal``   → ``tribal`` (the 16-rule peer-tribal declarative family)
    * ``*_feeder``   → ``axis_feeder`` (gated axis-feeder rules)

    Everything else groups as itself.
    """
    if rule_id.startswith("repl_"):
        return "replacement_stack"
    if rule_id.endswith("_tribal"):
        return "tribal"
    if rule_id.endswith("_feeder"):
        return "axis_feeder"
    return rule_id


# ---------------------------------------------------------------------------
# Enrichment data model (frozen) + DB loader
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CommanderEnrichment:
    """Tensor-derived per-commander enrichment for the renderers."""

    commander: str
    #: Distinct tensor candidates at the current config_hash — the
    #: per-commander tensor-coverage caveat column (R2 leaderboard).
    tensor_candidate_count: int
    #: Rule-family contribution shares of the commander's live top-30
    #: (positive contributions only), sorted ``(-share, family)``.
    displacer_family_shares: tuple[tuple[str, float], ...]


@dataclass(frozen=True)
class ForensicsRenderData:
    """Everything the pure renderers need, computed once."""

    report: ForensicsReport
    config_hash: str
    fixture_path: str
    enrichments: tuple[CommanderEnrichment, ...]
    outranked_rank_quantiles: tuple[tuple[str, int], ...]
    #: ``(family, total_contribution)`` over OUTRANKED missed cards'
    #: tensor rows, sorted ``(-contribution, family)``.
    outranked_family_contributions: tuple[tuple[str, float], ...]
    #: Aggregate mean family share across commanders with a non-empty
    #: displacer profile, sorted ``(-share, family)``.
    aggregate_displacer_shares: tuple[tuple[str, float], ...]
    #: Frequency-ranked ``(node_kind, subkind, n_cards)`` shapes shared
    #: across all NO_RULES cards (R3).
    no_rules_port_shapes: tuple[tuple[str, str, int], ...]
    #: Unit-6 (R8) tiebreaker ablation. ``None`` unless the run was
    #: invoked with ``--ablate-tiebreak`` — the renderers emit the
    #: ablation section ONLY when this is set.
    ablation: TiebreakAblation | None = None
    #: Task 1 (plan 2026-07-07-002) rank_bonus ablation — computed on
    #: EVERY ``--forensics`` run (no flag). ``None`` only in tests that
    #: build :class:`ForensicsRenderData` directly without it; a real
    #: run via :func:`handle_forensics` always sets this.
    rank_bonus_ablation: RankBonusAblation | None = None


def load_port_shape_counts(
    conn: sqlite3.Connection,
    names: Sequence[str],
) -> tuple[tuple[str, str, int], ...]:
    """Frequency-ranked ``(node_kind, subkind, n_cards)`` over the
    ``port_nodes`` view for ``names`` (R3 drill-down).

    ``n_cards`` counts DISTINCT card names per shape so a card matched
    under both its label name and its front face counts once.
    """
    if not names:
        return ()
    # Established IN-list expansion pattern (load_card_rows /
    # load_port_stats): only ? placeholder counts are interpolated.
    rows = conn.execute(
        "SELECT node_kind, subkind, COUNT(DISTINCT card_name) AS n_cards "  # noqa: S608 — placeholders only
        "FROM port_nodes WHERE card_name IN ({}) "
        "GROUP BY node_kind, subkind "
        "ORDER BY n_cards DESC, node_kind ASC, subkind ASC".format(",".join("?" * len(names))),
        tuple(names),
    ).fetchall()
    return tuple((str(r["node_kind"]), str(r["subkind"] or ""), int(r["n_cards"])) for r in rows)


# ---------------------------------------------------------------------------
# Pure aggregation helpers
# ---------------------------------------------------------------------------


def _miss_name_candidates(card_name: str) -> tuple[str, ...]:
    """Candidate ``cards.name`` keys for a label name — the same v1
    normalization contract as ``forensics.normalize_label_name`` (exact,
    then front face of a ``" // "`` split)."""
    front = card_name.split(" // ")[0]
    return (card_name,) if front == card_name else (card_name, front)


def aggregate_reason_counts(entries: Iterable[CommanderForensics]) -> dict[str, dict[str, int]]:
    """``{bucket: {reason: count}}`` across all classified misses.

    Buckets without sub-tagged misses map to empty dicts; reason keys
    are sorted at render time for determinism.
    """
    out: dict[str, dict[str, int]] = {bucket: {} for bucket in BUCKETS}
    for entry in entries:
        for miss in entry.misses:
            if miss.reason is None:
                continue
            bucket_reasons = out[miss.bucket]
            bucket_reasons[miss.reason] = bucket_reasons.get(miss.reason, 0) + 1
    return out


def count_name_unmatched(entries: Iterable[CommanderForensics]) -> int:
    """Total misses whose EDHREC name failed normalization."""
    return sum(1 for entry in entries for miss in entry.misses if miss.name_unmatched)


def outranked_rank_quantiles(entries: Iterable[CommanderForensics]) -> tuple[tuple[str, int], ...]:
    """OUTRANKED miss counts per :data:`OUTRANKED_RANK_BANDS` band."""
    counts = {label: 0 for label, _lo, _hi in OUTRANKED_RANK_BANDS}
    for entry in entries:
        for miss in entry.misses:
            if miss.bucket != BUCKET_OUTRANKED or miss.rank is None:
                continue
            for label, lo, hi in OUTRANKED_RANK_BANDS:
                if miss.rank >= lo and (hi is None or miss.rank <= hi):
                    counts[label] += 1
                    break
    return tuple((label, counts[label]) for label, _lo, _hi in OUTRANKED_RANK_BANDS)


def displacer_family_shares(
    tensor_rows: Sequence[tuple[str, str, float]],
    live_top_30: Sequence[str],
) -> tuple[tuple[str, float], ...]:
    """Rule-family contribution shares of one commander's live top-30.

    Families are aggregated over the tensor rows whose candidate sits
    in the live top-30; families with non-positive totals are dropped
    (anti-synergy rows would otherwise produce negative shares) and
    shares are normalised over the remaining positive mass.
    """
    top = set(live_top_30)
    fam_totals: dict[str, float] = {}
    for candidate, rule_id, contribution in tensor_rows:
        if candidate not in top:
            continue
        family = rule_family(rule_id)
        fam_totals[family] = fam_totals.get(family, 0.0) + contribution
    positive = {family: total for family, total in fam_totals.items() if total > 0.0}
    grand_total = sum(positive.values())
    if grand_total <= 0.0:
        return ()
    shares = [(family, total / grand_total) for family, total in positive.items()]
    return tuple(sorted(shares, key=lambda item: (-item[1], item[0])))


def aggregate_displacer_profile(
    profiles: Sequence[tuple[tuple[str, float], ...]],
) -> tuple[tuple[str, float], ...]:
    """Mean family share across commanders with a non-empty profile.

    Missing families contribute 0.0 to a commander's share vector, so
    the denominator is the count of non-empty profiles for every family.
    """
    nonempty = [profile for profile in profiles if profile]
    if not nonempty:
        return ()
    sums: dict[str, float] = {}
    for profile in nonempty:
        for family, share in profile:
            sums[family] = sums.get(family, 0.0) + share
    n = len(nonempty)
    means = [(family, total / n) for family, total in sums.items()]
    return tuple(sorted(means, key=lambda item: (-item[1], item[0])))


def outranked_family_contributions(
    entries: Iterable[CommanderForensics],
    tensor_by_commander: Mapping[str, Sequence[tuple[str, str, float]]],
) -> tuple[tuple[str, float], ...]:
    """Total tensor contribution per rule family over OUTRANKED missed
    cards (R2 'dominant rule-family' breakdown).

    For each OUTRANKED miss the card's tensor rows are looked up under
    its label name, falling back to its front face (the v1
    normalization contract). Misses with zero tensor rows
    (``staple_only``) contribute nothing — that absence is already a
    reported sub-count.
    """
    fam_totals: dict[str, float] = {}
    for entry in entries:
        rows = tensor_by_commander.get(entry.commander, ())
        by_candidate: dict[str, dict[str, float]] = {}
        for candidate, rule_id, contribution in rows:
            family = rule_family(rule_id)
            cand_fams = by_candidate.setdefault(candidate, {})
            cand_fams[family] = cand_fams.get(family, 0.0) + contribution
        for miss in entry.misses:
            if miss.bucket != BUCKET_OUTRANKED:
                continue
            for name in _miss_name_candidates(miss.card_name):
                cand_fams = by_candidate.get(name)
                if cand_fams is not None:
                    for family, total in cand_fams.items():
                        fam_totals[family] = fam_totals.get(family, 0.0) + total
                    break
    items = [(family, total) for family, total in fam_totals.items()]
    return tuple(sorted(items, key=lambda item: (-item[1], item[0])))


@dataclass(frozen=True)
class JustifiedAggregate:
    """Run-level R9 rollup across all commanders carrying the view."""

    divergent: int
    justified: int
    unjustified: int
    listed_nonpositive: int
    pass_rate: float | None
    gate_saturated: bool
    n_rules_distribution: tuple[tuple[str, int], ...]
    ratio_distribution: tuple[tuple[str, int], ...]
    #: Name-sorted concatenation of the per-commander card lists
    #: (duplicates retained when a card diverges for several
    #: commanders, so lengths always match the counts above).
    justified_cards: tuple[str, ...] = ()
    unjustified_cards: tuple[str, ...] = ()


def aggregate_justified(report: ForensicsReport) -> JustifiedAggregate:
    """Sum the per-commander justified-divergence views (R9).

    ``gate_saturated`` is True when the overall pass-rate is 1.0 over a
    nonempty divergent set — equivalent to every commander with
    divergent picks being individually saturated.
    """
    views = [e.justified_divergence for e in report.entries if e.justified_divergence is not None]
    divergent = sum(v.divergent for v in views)
    justified = sum(v.justified_divergences for v in views)
    unjustified = sum(v.unjustified_divergences for v in views)
    listed_nonpositive = sum(v.listed_nonpositive for v in views)
    n_rules = {label: 0 for label in N_RULES_BIN_LABELS}
    ratios = {label: 0 for label in RATIO_BIN_LABELS}
    for view in views:
        for label in N_RULES_BIN_LABELS:
            n_rules[label] += view.n_rules_distribution.get(label, 0)
        for label in RATIO_BIN_LABELS:
            ratios[label] += view.ratio_distribution.get(label, 0)
    return JustifiedAggregate(
        divergent=divergent,
        justified=justified,
        unjustified=unjustified,
        listed_nonpositive=listed_nonpositive,
        pass_rate=justified / divergent if divergent else None,
        gate_saturated=divergent > 0 and justified == divergent,
        n_rules_distribution=tuple((label, n_rules[label]) for label in N_RULES_BIN_LABELS),
        ratio_distribution=tuple((label, ratios[label]) for label in RATIO_BIN_LABELS),
        justified_cards=tuple(sorted(card for v in views for card in v.justified_cards)),
        unjustified_cards=tuple(sorted(card for v in views for card in v.unjustified_cards)),
    )


def _leaderboard_entries(report: ForensicsReport) -> list[tuple[float, CommanderForensics]]:
    """``(missed_synergy_weight, entry)`` rows for the worst-divergence
    leaderboard: misses weighted by graded synergy, descending;
    zero-miss commanders excluded; name as deterministic tiebreaker."""
    rows = [(sum(miss.synergy for miss in entry.misses), entry) for entry in report.entries if entry.misses]
    return sorted(rows, key=lambda item: (-item[0], item[1].commander))


# ---------------------------------------------------------------------------
# Render-data shell (the only DB-touching function in this module)
# ---------------------------------------------------------------------------


def build_render_data(
    report: ForensicsReport,
    conn: sqlite3.Connection,
    *,
    config_hash: str,
    fixture_path: str,
) -> ForensicsRenderData:
    """Enrich a :class:`ForensicsReport` with the tensor / port_nodes
    reads the R2/R3 sections need.

    One ``load_tensor_contributions`` read per commander (hash-filtered
    like every tensor read) plus one batched ``port_nodes`` query for
    the NO_RULES drill-down — no live re-scoring (R4 holds).
    """
    enrichments: list[CommanderEnrichment] = []
    profiles: list[tuple[tuple[str, float], ...]] = []
    tensor_by_commander: dict[str, tuple[tuple[str, str, float], ...]] = {}
    for entry in report.entries:
        rows = load_tensor_contributions(conn, entry.commander, config_hash)
        tensor_by_commander[entry.commander] = rows
        shares = displacer_family_shares(rows, entry.live_top_30)
        profiles.append(shares)
        enrichments.append(
            CommanderEnrichment(
                commander=entry.commander,
                tensor_candidate_count=len({candidate for candidate, _rule, _value in rows}),
                displacer_family_shares=shares,
            )
        )

    no_rules_names = sorted(
        {
            name
            for entry in report.entries
            for miss in entry.misses
            if miss.bucket == BUCKET_NO_RULES
            for name in _miss_name_candidates(miss.card_name)
        }
    )
    return ForensicsRenderData(
        report=report,
        config_hash=config_hash,
        fixture_path=fixture_path,
        enrichments=tuple(enrichments),
        outranked_rank_quantiles=outranked_rank_quantiles(report.entries),
        outranked_family_contributions=outranked_family_contributions(report.entries, tensor_by_commander),
        aggregate_displacer_shares=aggregate_displacer_profile(profiles),
        no_rules_port_shapes=load_port_shape_counts(conn, no_rules_names),
    )


# ---------------------------------------------------------------------------
# Renderers (pure md/json pair)
# ---------------------------------------------------------------------------


def _fmt_share(share: float) -> str:
    return f"{100.0 * share:.1f}%"


def _fmt_reason_cell(reasons: Mapping[str, int]) -> str:
    if not reasons:
        return _EM_DASH
    return ", ".join(f"{reason}: {count}" for reason, count in sorted(reasons.items()))


def _fmt_divergent_cell(entry: CommanderForensics) -> str:
    view = entry.justified_divergence
    if view is None:
        return _EM_DASH
    return f"{view.divergent} ({view.justified_divergences} justified)"


def render_forensics_markdown(data: ForensicsRenderData) -> str:
    """Render the full forensics report as Markdown (pure function)."""
    report = data.report
    n_classified = len(report.entries)
    n_skipped = len(report.skipped_commanders)
    reason_counts = aggregate_reason_counts(report.entries)
    proportions = bucket_proportions(report.aggregate_bucket_counts)
    n_unmatched = count_name_unmatched(report.entries)
    justified = aggregate_justified(report)
    enrichment_by_commander = {e.commander: e for e in data.enrichments}

    lines: list[str] = []
    lines.append("# bench.py audit --forensics")
    lines.append("")
    lines.append(f"config_hash: {data.config_hash[:12]}...")
    lines.append(f"fixture: {data.fixture_path}")
    lines.append("")
    lines.append(f"> {GOLDEN_SET_BUBBLE_CAVEAT}")
    lines.append("")
    lines.append(
        f"Aggregate NDCG@30 (canonical denominator: all {n_classified + n_skipped} fixture "
        f"commanders, zero-label commanders contribute 0.0): {report.aggregate_ndcg_canonical:.6f}"
    )
    lines.append(f"Aggregate raw DCG@30 (same canonical denominator): {report.aggregate_raw_dcg_canonical:.6f}")
    if data.rank_bonus_ablation is not None:
        rba = data.rank_bonus_ablation
        lines.append(
            f"rank_bonus-ablated NDCG@30: {rba.ndcg_ablated:.4f} (raw {rba.ndcg_raw:.4f}, "
            f"delta {rba.delta:+.4f}) — EDHREC-at-inference credit"
        )
    lines.append(
        f"Commanders: {n_classified + n_skipped} total = {n_classified} classified + {n_skipped} skipped (zero labels)"
    )
    lines.append(
        f"Total misses: {report.total_misses} (bucket proportions use the exclusion-based "
        f"denominator: {n_classified} classified commanders)"
    )
    if n_unmatched:
        lines.append(f"Misses with unmatched EDHREC names (name_unmatched): {n_unmatched}")
    if report.skipped_commanders:
        lines.append(f"Skipped commanders: {', '.join(report.skipped_commanders)}")
    lines.append("")

    # -- Aggregate bucket proportions ------------------------------------
    lines.append("## Aggregate bucket proportions")
    lines.append("")
    lines.append("| bucket | count | share | reason sub-counts |")
    lines.append("|--------|------:|------:|-------------------|")
    for bucket in BUCKETS:
        count = report.aggregate_bucket_counts.get(bucket, 0)
        # bucket_proportions() already returns percentages (0-100); do not
        # route through _fmt_share, which expects a 0-1 fraction.
        share = f"{proportions[bucket]:.1f}%" if proportions is not None else _EM_DASH
        lines.append(f"| {bucket} | {count} | {share} | {_fmt_reason_cell(reason_counts[bucket])} |")
    lines.append("")

    # -- Worst-divergence leaderboard ------------------------------------
    lines.append("## Worst-divergence commanders")
    lines.append("")
    lines.append(
        "Misses weighted by graded synergy (sum of missed synergy per commander), "
        "descending. Zero-miss commanders excluded. `tensor cands` is the "
        "per-commander tensor-coverage caveat (golden-set bubble)."
    )
    lines.append("")
    leaderboard = _leaderboard_entries(report)
    if not leaderboard:
        lines.append(f"{_EM_DASH} (no misses anywhere)")
    else:
        lines.append(
            "| commander | missed synergy | misses | NEAR_MISS | OUTRANKED | FILTERED "
            "| DATA_GAP | NO_RULES | divergent (justified) | ndcg30 | tensor cands |"
        )
        lines.append(
            "|-----------|---------------:|-------:|----------:|----------:|---------:|---------:|---------:|---|------:|------:|"
        )
        for weight, entry in leaderboard:
            enrichment = enrichment_by_commander.get(entry.commander)
            tensor_cands = str(enrichment.tensor_candidate_count) if enrichment is not None else _EM_DASH
            bucket_cells = " | ".join(str(entry.bucket_counts.get(bucket, 0)) for bucket in BUCKETS)
            lines.append(
                f"| {entry.commander} | {weight:.2f} | {len(entry.misses)} | {bucket_cells} "
                f"| {_fmt_divergent_cell(entry)} | {entry.ndcg30:.4f} | {tensor_cands} |"
            )
    lines.append("")

    # -- OUTRANKED breakdown ----------------------------------------------
    lines.append("## OUTRANKED breakdown")
    lines.append("")
    lines.append("Rank quantiles (live full-ranking position):")
    lines.append("")
    lines.append("| rank band | count |")
    lines.append("|-----------|------:|")
    for label, count in data.outranked_rank_quantiles:
        lines.append(f"| {label} | {count} |")
    lines.append("")
    lines.append(
        "Dominant rule families of OUTRANKED missed cards' tensor contributions "
        "(family = rule_id grouping; the registry exposes no family attribute — "
        "only `repl_*` / `*_tribal` / `*_feeder` naming families are derivable):"
    )
    lines.append("")
    if not data.outranked_family_contributions:
        lines.append(f"{_EM_DASH} (no OUTRANKED misses with tensor rows)")
    else:
        total = sum(value for _family, value in data.outranked_family_contributions)
        lines.append("| family | total contribution | share |")
        lines.append("|--------|-------------------:|------:|")
        for family, value in data.outranked_family_contributions[:OUTRANKED_TOP_FAMILIES_MD]:
            share = _fmt_share(value / total) if total > 0 else _EM_DASH
            lines.append(f"| {family} | {value:.4f} | {share} |")
    lines.append("")

    # -- Displacer profiles -------------------------------------------------
    lines.append("## Displacer profiles")
    lines.append("")
    lines.append(
        "Rule-family contribution shares of each commander's live top-30 "
        "(tensor rows at the current config_hash; positive contributions only)."
    )
    lines.append("")
    profiled = [e for e in data.enrichments if e.displacer_family_shares]
    if not profiled:
        lines.append(f"{_EM_DASH} (no tensor rows intersect any live top-30)")
    else:
        lines.append("| commander | top families (share) |")
        lines.append("|-----------|----------------------|")
        for enrichment in profiled:
            cell = ", ".join(
                f"{family} ({_fmt_share(share)})"
                for family, share in enrichment.displacer_family_shares[:DISPLACER_TOP_FAMILIES_MD]
            )
            lines.append(f"| {enrichment.commander} | {cell} |")
    lines.append("")
    lines.append("Aggregate mean-share profile (across commanders with a non-empty profile):")
    lines.append("")
    if not data.aggregate_displacer_shares:
        lines.append(f"{_EM_DASH} (no profiles)")
    else:
        lines.append("| family | mean share |")
        lines.append("|--------|-----------:|")
        for family, share in data.aggregate_displacer_shares:
            lines.append(f"| {family} | {_fmt_share(share)} |")
    lines.append("")

    # -- NO_RULES drill-down -------------------------------------------------
    lines.append("## NO_RULES port shapes")
    lines.append("")
    lines.append("Frequency-ranked shared (node_kind, subkind) port shapes across all NO_RULES cards.")
    lines.append("")
    if not data.no_rules_port_shapes:
        lines.append(f"{_EM_DASH} (no NO_RULES misses)")
    else:
        lines.append("| node_kind | subkind | cards |")
        lines.append("|-----------|---------|------:|")
        for node_kind, subkind, n_cards in data.no_rules_port_shapes[:NO_RULES_TOP_SHAPES_MD]:
            lines.append(f"| {node_kind} | {subkind or _EM_DASH} | {n_cards} |")
    lines.append("")

    # -- R9 justified divergence ----------------------------------------------
    lines.append("## Justified divergence (R9)")
    lines.append("")
    lines.append(
        "Reference set: all-sections graded labels (grade_floor=0.0) — deliberately "
        "wider than the gem axis's HS-top-30 reference (they answer different questions)."
    )
    lines.append("")
    lines.append(f"divergent picks: {justified.divergent}")
    lines.append(f"justified: {justified.justified}")
    lines.append(f"unjustified: {justified.unjustified}")
    pass_rate = f"{justified.pass_rate:.4f}" if justified.pass_rate is not None else _EM_DASH
    lines.append(f"gate pass-rate: {pass_rate}")
    lines.append(f"listed_nonpositive: {justified.listed_nonpositive}")
    if justified.gate_saturated:
        lines.append("")
        lines.append(f"**{GATE_SATURATED_ANNOTATION}**")
    lines.append("")
    lines.append("N_rules_firing distribution (justified picks):")
    lines.append("")
    lines.append("| bin | count |")
    lines.append("|-----|------:|")
    for label, count in justified.n_rules_distribution:
        lines.append(f"| {label} | {count} |")
    lines.append("")
    lines.append("contribution/median ratio distribution (justified picks):")
    lines.append("")
    lines.append("| bin | count |")
    lines.append("|-----|------:|")
    for label, count in justified.ratio_distribution:
        lines.append(f"| {label} | {count} |")

    # -- Tiebreaker ablation (R8, only with --ablate-tiebreak) -----------------
    if data.ablation is not None:
        lines.extend(_render_ablation_markdown(data.ablation))
    return "\n".join(lines) + "\n"


def _render_ablation_markdown(ablation: TiebreakAblation) -> list[str]:
    """Markdown lines for the Unit-6 (R8) ablation section (pure)."""
    lo, hi = ablation.delta_range
    lines: list[str] = []
    lines.append("")
    lines.append(TIEBREAK_SECTION_HEADER)
    lines.append("")
    lines.append(
        "Bracketing re-sorts of the captured production rankings (pure re-sort "
        "of the Unit-1 capture — no extra scoring pass; `engine.page()` "
        "untouched). Deltas are signed production-minus-replacement NDCG@30 over "
        f"the canonical denominator ({ablation.n_commanders} commanders, "
        "zero-label → 0.0). The production-key reconstruction self-check "
        "passed on every commander."
    )
    lines.append("")
    lines.append("| key | NDCG@30 | delta vs production |")
    lines.append("|-----|--------:|--------------------:|")
    lines.append(
        f"| production `(-total_score, cmc, edhrec_rank, name)` | {ablation.production_ndcg:.6f} | {_EM_DASH} |"
    )
    lines.append(f"| strong `(-total_score, cmc, name)` | {ablation.strong_ndcg:.6f} | {ablation.strong_delta:+.6f} |")
    lines.append(f"| weak `(-total_score, name)` | {ablation.weak_ndcg:.6f} | {ablation.weak_delta:+.6f} |")
    lines.append("")
    lines.append(f"delta range: [{lo:+.6f}, {hi:+.6f}]")
    lines.append(
        f"upper bound of unearned EDHREC tiebreak credit: {ablation.upper_bound:+.6f} "
        f"(flag threshold {TIEBREAK_FLAG_THRESHOLD})"
    )
    entry = ablation.rule_history_markdown
    if entry is not None:
        lines.append("")
        lines.append(
            f"**Upper bound exceeds {TIEBREAK_FLAG_THRESHOLD} — ready-to-paste "
            "RULE_HISTORY entry below (commit it manually; this run stays read-only):**"
        )
        lines.append("")
        lines.extend(entry.splitlines())
    return lines


def render_forensics_json(data: ForensicsRenderData) -> str:
    """Render the same report as JSON (key order is insertion order —
    deterministic for identical inputs, mirroring every count the
    markdown view shows)."""
    report = data.report
    reason_counts = aggregate_reason_counts(report.entries)
    proportions = bucket_proportions(report.aggregate_bucket_counts)
    justified = aggregate_justified(report)
    enrichment_by_commander = {e.commander: e for e in data.enrichments}

    leaderboard: list[dict[str, Any]] = []
    for weight, entry in _leaderboard_entries(report):
        enrichment = enrichment_by_commander.get(entry.commander)
        view = entry.justified_divergence
        leaderboard.append(
            {
                "commander": entry.commander,
                "missed_synergy": weight,
                "misses": len(entry.misses),
                "bucket_counts": {bucket: entry.bucket_counts.get(bucket, 0) for bucket in BUCKETS},
                "divergent": view.divergent if view is not None else None,
                "justified": view.justified_divergences if view is not None else None,
                "ndcg30": entry.ndcg30,
                "raw_dcg30": entry.raw_dcg30,
                "tensor_candidates": (enrichment.tensor_candidate_count if enrichment is not None else None),
            }
        )

    payload: dict[str, Any] = {
        "config_hash": data.config_hash,
        "fixture": data.fixture_path,
        "caveat": GOLDEN_SET_BUBBLE_CAVEAT,
        "aggregate": {
            "ndcg30_canonical": report.aggregate_ndcg_canonical,
            "raw_dcg30_canonical": report.aggregate_raw_dcg_canonical,
            "n_commanders": len(report.entries) + len(report.skipped_commanders),
            "n_classified": len(report.entries),
            "n_skipped": len(report.skipped_commanders),
            "total_misses": report.total_misses,
            "name_unmatched_misses": count_name_unmatched(report.entries),
        },
        "skipped_commanders": list(report.skipped_commanders),
        "bucket_counts": {bucket: report.aggregate_bucket_counts.get(bucket, 0) for bucket in BUCKETS},
        "bucket_proportions": (
            {bucket: proportions[bucket] for bucket in BUCKETS} if proportions is not None else None
        ),
        "reason_counts": {bucket: dict(sorted(reason_counts[bucket].items())) for bucket in BUCKETS},
        "leaderboard": leaderboard,
        "outranked": {
            "rank_quantiles": {label: count for label, count in data.outranked_rank_quantiles},
            "rule_families": [
                {"family": family, "total_contribution": value} for family, value in data.outranked_family_contributions
            ],
        },
        "displacers": {
            "per_commander": [
                {
                    "commander": enrichment.commander,
                    "tensor_candidates": enrichment.tensor_candidate_count,
                    "families": [
                        {"family": family, "share": share} for family, share in enrichment.displacer_family_shares
                    ],
                }
                for enrichment in data.enrichments
            ],
            "aggregate_mean_share": [
                {"family": family, "mean_share": share} for family, share in data.aggregate_displacer_shares
            ],
        },
        "no_rules_port_shapes": [
            {"node_kind": node_kind, "subkind": subkind, "cards": n_cards}
            for node_kind, subkind, n_cards in data.no_rules_port_shapes
        ],
        "justified_divergence": {
            "divergent": justified.divergent,
            "justified": justified.justified,
            "unjustified": justified.unjustified,
            "justified_cards": list(justified.justified_cards),
            "unjustified_cards": list(justified.unjustified_cards),
            "gate_pass_rate": justified.pass_rate,
            "listed_nonpositive": justified.listed_nonpositive,
            "gate_saturated": justified.gate_saturated,
            "gate_saturated_annotation": (GATE_SATURATED_ANNOTATION if justified.gate_saturated else None),
            "n_rules_distribution": {label: count for label, count in justified.n_rules_distribution},
            "ratio_distribution": {label: count for label, count in justified.ratio_distribution},
        },
    }
    # Task 1 (plan 2026-07-07-002): computed on EVERY --forensics run
    # (no flag) — absent only when the caller never set it (renderer
    # unit tests that build ForensicsRenderData directly).
    if data.rank_bonus_ablation is not None:
        rba = data.rank_bonus_ablation
        payload["rank_bonus_ablation"] = {
            "n_commanders": rba.n_commanders,
            "ndcg_raw": rba.ndcg_raw,
            "ndcg_ablated": rba.ndcg_ablated,
            "delta": rba.delta,
        }

    # Unit 6 (R8): the ablation block exists ONLY when --ablate-tiebreak
    # was passed — absent otherwise (no null placeholder).
    if data.ablation is not None:
        ablation = data.ablation
        lo, hi = ablation.delta_range
        payload["tiebreak_ablation"] = {
            "n_commanders": ablation.n_commanders,
            "production_ndcg30": ablation.production_ndcg,
            "strong_ndcg30": ablation.strong_ndcg,
            "weak_ndcg30": ablation.weak_ndcg,
            "strong_delta": ablation.strong_delta,
            "weak_delta": ablation.weak_delta,
            "delta_range": [lo, hi],
            "upper_bound": ablation.upper_bound,
            "flag_threshold": TIEBREAK_FLAG_THRESHOLD,
            "flagged": ablation.flagged,
            "rule_history_entry": ablation.rule_history_markdown,
        }
    return json.dumps(payload, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# CLI handler
# ---------------------------------------------------------------------------


def _write_default_forensics_output(rendered: str, fmt: str) -> None:
    """Persist the rendered report to ``.audit/forensics.{md,json}``.

    Mirrors ``audit._write_default_output``: a read-only ``.audit/``
    (or any other OSError during mkdir / write) must NOT fail the run —
    the report has already been printed to stdout. Degrade to a stderr
    warning and return.
    """
    default_dir = Path(".audit")
    suffix = "json" if fmt == "json" else "md"
    target = default_dir / f"forensics.{suffix}"
    try:
        default_dir.mkdir(exist_ok=True)
        target.write_text(rendered, encoding="utf-8")
    except OSError as exc:
        print(
            f"bench.py audit --forensics: warning: could not write {target}: {exc.__class__.__name__}: {exc}",
            file=sys.stderr,
        )


def handle_forensics(args: argparse.Namespace) -> int:
    """Handle ``bench.py audit --forensics``.

    Runs the Units 1–3 forensics pass over the pinned fixture, enriches
    it with the tensor / port-shape reads, and emits the markdown
    (default) or JSON report. Default output also lands at
    ``.audit/forensics.{md,json}``; ``--output PATH`` writes there
    instead and skips ``.audit/``.

    Exit codes:
    * ``0`` — report emitted (findings are not errors; read-only run).
    * ``2`` — precondition or reconciliation failure
      (missing fixture / tags.db / DB, stale tensor, partner fixture
      entries, NDCG reconciliation mismatch). NOTHING is written.

    Exit 1 is reserved for the main audit's drift verdict and is never
    emitted by this mode.

    On full success ONLY — after reconciliation (inside
    ``compute_forensics``) and rendering both succeeded — one
    provenance-stamped row is appended to the forensics history CSV
    (Unit 5). The append degrades to a stderr warning on write failure
    (``append_forensics_run``'s contract) and never changes the exit
    code; an exit-2 path above never reaches it, so a failed run leaves
    the history file untouched.
    """
    db_path = Path(args.db)
    tags_path = Path(getattr(args, "edhrec_db", "data/tags.db"))
    fixture_path = Path(args.fixture)

    try:
        report = compute_forensics(
            db_path=db_path,
            tags_path=tags_path,
            fixture_path=fixture_path,
        )
    except (ForensicsPreconditionError, ForensicsReconciliationError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    # Enrichment reads (tensor + port_nodes) on a fresh read-side
    # connection; the DB demonstrably exists (compute_forensics opened
    # it), so create=False cannot raise here. The Unit-5 history-row
    # inputs (provenance digests + in-run gem rate) are gathered on the
    # same connections while they're open; the append itself happens
    # only after rendering succeeds below. The label reload is used
    # unconditionally by the Task 1 rank_bonus-ablation sidecar (every
    # run) and, with --ablate-tiebreak, also by the Unit-6 tiebreaker
    # ablation.
    ablate_tiebreak = bool(getattr(args, "ablate_tiebreak", False))
    config_hash = compute_config_hash()
    conn = open_db(db_path, create=False)
    try:
        data = build_render_data(
            report,
            conn,
            config_hash=config_hash,
            fixture_path=str(fixture_path),
        )
        synergy_digest = synergy_content_digest(conn)
        edhrec_conn = sqlite3.connect(tags_path)
        edhrec_conn.row_factory = sqlite3.Row
        try:
            snapshot_digest = edhrec_snapshot_digest(edhrec_conn)
            gem_rate = compute_gem_rate_forensics(report, edhrec_conn, conn, config_hash)
            # PR #103 review (F8): compute_forensics already loaded every
            # commander's labels once and carries them on the report; reuse
            # that instead of re-querying tags.db per entry. Fall back to
            # the old re-query ONLY if the field is empty (backward compat
            # for a report built by a caller that never populated it).
            if report.labels_by_commander:
                labels_by_commander: dict[str, dict[str, float]] = {
                    commander: dict(labels) for commander, labels in report.labels_by_commander.items()
                }
            else:
                labels_by_commander = {
                    entry.commander: edhrec_labels_for_commander(edhrec_conn, entry.commander, grade_floor=0.0)
                    for entry in report.entries
                }
        finally:
            edhrec_conn.close()
    finally:
        conn.close()

    # Task 1 (plan 2026-07-07-002): cheap re-sort arithmetic over the
    # already-captured Unit-1 rankings — no extra scoring pass, no
    # flag. Computed on EVERY --forensics run, unlike the one-off R8
    # tiebreaker ablation below.
    rank_bonus_ablation = compute_rank_bonus_ablation(
        {entry.commander: entry.ranking for entry in report.entries},
        labels_by_commander,
        n_canonical=len(report.entries) + len(report.skipped_commanders),
    )
    data = replace(data, rank_bonus_ablation=rank_bonus_ablation)

    if ablate_tiebreak:
        # Unit 6 (R8): pure re-sort of the already-captured rankings +
        # labels — no extra scoring pass. The mandatory production-key
        # self-check failing maps to exit 2 BEFORE anything is written
        # (no report file, no history row, no deltas). One-off mode:
        # the history row schema is NOT extended.
        try:
            ablation = compute_tiebreak_ablation(
                {entry.commander: entry.ranking for entry in report.entries},
                labels_by_commander,
                n_canonical=len(report.entries) + len(report.skipped_commanders),
                run_date=datetime.now(UTC).date().isoformat(),
            )
        except TiebreakSelfCheckError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        data = replace(data, ablation=ablation)

    fmt = getattr(args, "format", "md")
    rendered = render_forensics_json(data) if fmt == "json" else render_forensics_markdown(data)

    output_target = getattr(args, "output", None)
    if output_target is None or output_target == "-":
        _write_default_forensics_output(rendered, fmt)
        print(rendered, end="" if rendered.endswith("\n") else "\n")
    else:
        output_path = Path(output_target)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(rendered, encoding="utf-8")
        print(
            f"bench.py audit --forensics: report written to {output_path}",
            file=sys.stderr,
        )

    # Unit 5: history row, appended only now that reconciliation +
    # rendering succeeded. Provenance per the verify-from-stored-config
    # learning: fixture file hash + the config_hash the tensor reads
    # were filtered by.
    history_row = build_history_row(
        report,
        config_hash=config_hash,
        fixture_digest=fixture_file_sha256(fixture_path),
        snapshot_digest=snapshot_digest,
        synergy_digest=synergy_digest,
        gem_rate=gem_rate,
    )
    append_forensics_run(
        history_row,
        path=Path(getattr(args, "forensics_history", DEFAULT_FORENSICS_HISTORY_PATH)),
    )
    return 0
