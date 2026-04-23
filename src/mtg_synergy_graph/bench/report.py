"""Audit report dataclasses + markdown / JSON renderers.

Takes the raw comparison output from :mod:`bench.fixture.assert_identity`
and aggregates it into the shape a human (or automated consumer) needs:
aggregate deltas, per-commander rollups, per-rule rollups, top-30
movement. Unit 5 will layer the rank-shuffle histogram verdict on top.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

from mtg_synergy_graph.bench.fixture import (
    IdentityReport,
    PinnedFixture,
)

if TYPE_CHECKING:
    from mtg_synergy_graph.bench.fixture import FixtureEntry
from mtg_synergy_graph.bench.hidden_gems import HIDDEN_GEM_WARN_THRESHOLD
from mtg_synergy_graph.bench.histogram import (
    Bucket,
    Histogram,
    Verdict,
    compute_histogram,
    rollup_to_verdict,
)


def _bucket_for_label(label: str) -> Bucket:
    return Bucket(label)


def _top_n_names(scores: dict[str, float], n: int) -> list[str]:
    """Top-N candidate names by score desc, tiebreaking by name for stability.

    Duplicated (not imported) from histogram.py to avoid a cross-module
    private import; the helper is 1 line and the duplication cost is
    negligible.
    """
    return [name for name, _ in sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))[:n]]


@dataclass(frozen=True)
class CommanderDelta:
    """Summary of how one commander's top-30 + scores moved."""

    commander: str
    score_delta_sum: float  # Σ (live - pinned) across all candidates
    added_to_top30: tuple[str, ...]
    removed_from_top30: tuple[str, ...]
    reordered_in_top30: int  # count of cards whose rank changed without crossing top-30 boundary

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RuleDelta:
    """Summary of how one rule's aggregate contribution moved."""

    rule_id: str
    contribution_delta_sum: float  # Σ (live_contrib - pinned_contrib) across all (cmdr, cand)
    commanders_touched: int
    candidates_touched: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AuditReport:
    """Full audit comparison between pinned baseline and live re-score."""

    fixture_path: str
    pinned_config_hash: str
    live_config_hash: str
    aggregate_score_delta: float
    commanders_compared: int
    identity_report: IdentityReport
    histogram: Histogram = field(default_factory=lambda: Histogram(samples={}))
    verdict: Verdict = Verdict.TRIVIAL
    per_commander: list[CommanderDelta] = field(default_factory=list)
    per_rule: list[RuleDelta] = field(default_factory=list)
    #: Aggregate ``hidden_gem_hit_rate`` over live entries whose
    #: ``legacy`` dict carries the key (i.e., the commander had EDHREC
    #: data at fixture-build time). ``None`` when no entry has the key.
    aggregate_hidden_gem_hit_rate: float | None = None
    #: Same aggregate, computed against the pinned fixture. ``None`` on
    #: an old pin that predates the metric.
    pinned_hidden_gem_hit_rate: float | None = None
    #: ``live - pinned`` when both aggregates are non-None, else None.
    hidden_gem_hit_rate_delta: float | None = None
    #: True iff delta dropped strictly below ``-HIDDEN_GEM_WARN_THRESHOLD``
    #: (FR4). Informational only — does not affect exit code.
    hidden_gem_warning: bool = False
    #: Count of entries contributing to the live aggregate.
    commanders_with_edhrec: int = 0

    @property
    def is_identical(self) -> bool:
        return self.identity_report.is_identical

    @property
    def config_hash_matches(self) -> bool:
        return self.pinned_config_hash == self.live_config_hash

    def to_json(self) -> str:
        return json.dumps(_as_json_dict(self), indent=2, ensure_ascii=False)

    def to_markdown(self) -> str:
        return _render_markdown(self)


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def _aggregate_hidden_gem_from_fixture(
    fixture: PinnedFixture,
) -> tuple[float | None, int]:
    """Compute the arithmetic-mean ``hidden_gem_hit_rate`` across entries.

    Only entries whose ``legacy`` dict carries the
    ``"hidden_gem_hit_rate"`` key contribute — missing-key entries are
    skipped (old fixtures or commanders without EDHREC data). Returns
    ``(None, 0)`` when no entry has the key so callers can distinguish
    "zero rate" from "no data."
    """
    rates: list[float] = []
    for entry in fixture.entries:
        rate = entry.legacy.get("hidden_gem_hit_rate")
        if isinstance(rate, int | float):
            rates.append(float(rate))
    if not rates:
        return None, 0
    return sum(rates) / len(rates), len(rates)


def build_report(
    fixture_path: str,
    pinned: PinnedFixture,
    live: PinnedFixture,
) -> AuditReport:
    """Diff pinned vs live and roll up per-commander + per-rule deltas."""
    identity = pinned.assert_identity(live)

    pinned_by_cmdr = {e.commander: e for e in pinned.entries}
    live_by_cmdr = {e.commander: e for e in live.entries}

    per_commander = [
        _commander_delta(pinned_by_cmdr[c], live_by_cmdr.get(c)) for c in pinned_by_cmdr if c in live_by_cmdr
    ]
    aggregate_score_delta = sum(cd.score_delta_sum for cd in per_commander)

    per_rule = _build_rule_deltas(pinned_by_cmdr, live_by_cmdr)

    histogram = compute_histogram(pinned, live)
    verdict = rollup_to_verdict(histogram, aggregate_score_delta)

    live_gem_mean, live_gem_count = _aggregate_hidden_gem_from_fixture(live)
    pinned_gem_mean, _ = _aggregate_hidden_gem_from_fixture(pinned)
    if live_gem_mean is not None and pinned_gem_mean is not None:
        gem_delta: float | None = live_gem_mean - pinned_gem_mean
    else:
        gem_delta = None
    gem_warning = gem_delta is not None and gem_delta < -HIDDEN_GEM_WARN_THRESHOLD

    return AuditReport(
        fixture_path=fixture_path,
        pinned_config_hash=pinned.config_hash,
        live_config_hash=live.config_hash,
        aggregate_score_delta=aggregate_score_delta,
        commanders_compared=len(per_commander),
        identity_report=identity,
        histogram=histogram,
        verdict=verdict,
        per_commander=sorted(per_commander, key=lambda d: abs(d.score_delta_sum), reverse=True),
        per_rule=sorted(per_rule, key=lambda d: abs(d.contribution_delta_sum), reverse=True),
        aggregate_hidden_gem_hit_rate=live_gem_mean,
        pinned_hidden_gem_hit_rate=pinned_gem_mean,
        hidden_gem_hit_rate_delta=gem_delta,
        hidden_gem_warning=gem_warning,
        commanders_with_edhrec=live_gem_count,
    )


def _commander_delta(pinned: FixtureEntry, live: FixtureEntry | None) -> CommanderDelta:
    pinned_scores = pinned.scores
    live_scores = live.scores if live is not None else {}

    all_cands = set(pinned_scores) | set(live_scores)
    delta_sum = sum(live_scores.get(c, 0.0) - pinned_scores.get(c, 0.0) for c in all_cands)

    pinned_top30 = _top_n_names(pinned_scores, 30)
    live_top30 = _top_n_names(live_scores, 30)
    added = tuple(c for c in live_top30 if c not in pinned_top30)
    removed = tuple(c for c in pinned_top30 if c not in live_top30)

    # Reordering within the top-30: same set, different positions.
    common_top30 = set(pinned_top30) & set(live_top30)
    pinned_ranks = {c: i for i, c in enumerate(pinned_top30)}
    live_ranks = {c: i for i, c in enumerate(live_top30)}
    reordered = sum(1 for c in common_top30 if pinned_ranks[c] != live_ranks[c])

    return CommanderDelta(
        commander=pinned.commander,
        score_delta_sum=delta_sum,
        added_to_top30=added,
        removed_from_top30=removed,
        reordered_in_top30=reordered,
    )


def _build_rule_deltas(
    pinned_by_cmdr: dict[str, FixtureEntry],
    live_by_cmdr: dict[str, FixtureEntry],
) -> list[RuleDelta]:
    """Per-rule rollup lives in SQLite now (see ``bench.rule_ops``).

    The fixture JSON no longer carries tensor rows — they're in the
    ``rule_contributions`` table, reached via ``bench.py audit --rule``.
    Returning an empty list here keeps the ``AuditReport`` shape stable
    while the rollup moves to its proper home in a follow-up.
    """
    return []


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _as_json_dict(report: AuditReport) -> dict[str, Any]:
    return {
        "fixture_path": report.fixture_path,
        "pinned_config_hash": report.pinned_config_hash,
        "live_config_hash": report.live_config_hash,
        "config_hash_matches": report.config_hash_matches,
        "aggregate_score_delta": report.aggregate_score_delta,
        "commanders_compared": report.commanders_compared,
        "is_identical": report.is_identical,
        "verdict": report.verdict.value,
        "histogram": report.histogram.to_dict(),
        "per_commander": [cd.to_dict() for cd in report.per_commander[:50]],
        "per_rule": [rd.to_dict() for rd in report.per_rule[:50]],
        "missing_commanders": report.identity_report.missing_commanders,
        "aggregate_hidden_gem_hit_rate": report.aggregate_hidden_gem_hit_rate,
        "hidden_gem_hit_rate_delta": report.hidden_gem_hit_rate_delta,
        "hidden_gem_warning": report.hidden_gem_warning,
        "commanders_with_edhrec": report.commanders_with_edhrec,
    }


def _format_hidden_gem_line(report: AuditReport) -> str:
    """Format the ``**hidden_gem_hit_rate:**`` markdown backtick content.

    Shapes:
    * ``0.1467 (Δ -0.0034, 97 cmdrs with EDHREC)`` when both aggregates exist.
    * ``0.1467 (Δ —, 97 cmdrs with EDHREC)`` when one side lacks gem data.
    * ``— (no commanders with EDHREC data)`` when neither side has data.
    """
    aggregate = report.aggregate_hidden_gem_hit_rate
    if aggregate is None:
        # No live data — fall through to em-dash sentinel.
        if report.pinned_hidden_gem_hit_rate is None:
            return "— (no commanders with EDHREC data)"
        return f"— (pinned {report.pinned_hidden_gem_hit_rate:.4f}, no live data)"

    delta_str = "—" if report.hidden_gem_hit_rate_delta is None else f"{report.hidden_gem_hit_rate_delta:+.4f}"
    return f"{aggregate:.4f} (Δ {delta_str}, {report.commanders_with_edhrec} cmdrs with EDHREC)"


def _render_markdown(report: AuditReport) -> str:
    lines: list[str] = []
    lines.append(f"# bench.py audit — {report.fixture_path}")
    lines.append("")

    status_icon = "✓" if report.is_identical else "✗"
    lines.append(f"**Status:** {status_icon} {'identical' if report.is_identical else 'drift detected'}")
    lines.append(f"**Verdict:** `{report.verdict.value}`")
    lines.append(
        f"**Config hash:** "
        f"`{report.pinned_config_hash[:12]}...` "
        f"{'== live' if report.config_hash_matches else f'!= `{report.live_config_hash[:12]}...`'}"
    )
    lines.append(f"**Aggregate score Δ:** `{report.aggregate_score_delta:+.6f}`")
    lines.append(f"**hidden_gem_hit_rate:** `{_format_hidden_gem_line(report)}`")
    lines.append(f"**Commanders compared:** {report.commanders_compared}")
    lines.append("")

    lines.append("## Histogram")
    lines.append("")
    lines.append("| Bucket | Count | Sample commanders |")
    lines.append("|---|---:|---|")
    samples = report.histogram.samples
    for bucket, count in (
        ("no_change", report.histogram.no_change),
        ("rank_shuffle_within_top30", report.histogram.rank_shuffle_within_top30),
        ("rank_shuffle_across_top30_boundary", report.histogram.rank_shuffle_across_top30_boundary),
        ("hi_syn_gain", report.histogram.hi_syn_gain),
        ("hi_syn_loss", report.histogram.hi_syn_loss),
    ):
        names = samples.get(_bucket_for_label(bucket), ())
        preview = ", ".join(names[:3]) if names else "—"
        lines.append(f"| {bucket} | {count} | {preview} |")
    lines.append("")

    if report.identity_report.missing_commanders:
        lines.append("## Missing commanders")
        for name in report.identity_report.missing_commanders[:10]:
            lines.append(f"- {name}")
        more = len(report.identity_report.missing_commanders) - 10
        if more > 0:
            lines.append(f"- … and {more} more")
        lines.append("")

    if report.per_commander:
        lines.append("## Per-commander deltas (top 20 by magnitude)")
        lines.append("")
        lines.append("| Commander | Δ score | +top30 | -top30 | reordered |")
        lines.append("|---|---:|---|---|---:|")
        for cd in report.per_commander[:20]:
            added = ", ".join(cd.added_to_top30[:3]) if cd.added_to_top30 else "—"
            removed = ", ".join(cd.removed_from_top30[:3]) if cd.removed_from_top30 else "—"
            lines.append(
                f"| {cd.commander} | {cd.score_delta_sum:+.4f} | {added} | {removed} | {cd.reordered_in_top30} |"
            )
        lines.append("")

    if report.per_rule:
        lines.append("## Per-rule deltas (top 20 by magnitude)")
        lines.append("")
        lines.append("| Rule | Δ contribution | cmdrs | cands |")
        lines.append("|---|---:|---:|---:|")
        for rd in report.per_rule[:20]:
            lines.append(
                f"| {rd.rule_id} | {rd.contribution_delta_sum:+.4f} | {rd.commanders_touched} | {rd.candidates_touched} |"
            )
        lines.append("")

    return "\n".join(lines) + "\n"
