"""Audit report dataclasses + markdown / JSON renderers.

Takes the raw comparison output from :mod:`bench.fixture.assert_identity`
and aggregates it into the shape a human (or automated consumer) needs:
aggregate deltas, per-commander rollups, per-rule rollups, top-30
movement. Unit 5 will layer the rank-shuffle histogram verdict on top.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from typing import Any

from mtg_synergy_graph.bench.fixture import (
    IdentityReport,
    PinnedFixture,
)


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
    per_commander: list[CommanderDelta] = field(default_factory=list)
    per_rule: list[RuleDelta] = field(default_factory=list)

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

    return AuditReport(
        fixture_path=fixture_path,
        pinned_config_hash=pinned.config_hash,
        live_config_hash=live.config_hash,
        aggregate_score_delta=aggregate_score_delta,
        commanders_compared=len(per_commander),
        identity_report=identity,
        per_commander=sorted(per_commander, key=lambda d: abs(d.score_delta_sum), reverse=True),
        per_rule=sorted(per_rule, key=lambda d: abs(d.contribution_delta_sum), reverse=True),
    )


def _commander_delta(pinned: Any, live: Any) -> CommanderDelta:  # FixtureEntry; annotated Any to avoid circular import
    pinned_scores = pinned.scores
    live_scores = live.scores if live is not None else {}

    all_cands = set(pinned_scores) | set(live_scores)
    delta_sum = sum(live_scores.get(c, 0.0) - pinned_scores.get(c, 0.0) for c in all_cands)

    pinned_top30 = _top_n_candidates(pinned_scores, 30)
    live_top30 = _top_n_candidates(live_scores, 30)
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


def _top_n_candidates(scores: dict[str, float], n: int) -> list[str]:
    """Top-N candidate names by score desc, tiebreaking by name for stability."""
    return [name for name, _ in sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))[:n]]


def _build_rule_deltas(pinned_by_cmdr: dict[str, Any], live_by_cmdr: dict[str, Any]) -> list[RuleDelta]:
    """Roll up tensor_rows across (cmdr, cand) grouped by rule_id."""
    per_rule_delta: dict[str, float] = defaultdict(float)
    per_rule_cmdrs: dict[str, set[str]] = defaultdict(set)
    per_rule_cands: dict[str, set[str]] = defaultdict(set)

    all_cmdrs = set(pinned_by_cmdr) | set(live_by_cmdr)
    for cmdr in all_cmdrs:
        pinned_entry = pinned_by_cmdr.get(cmdr)
        live_entry = live_by_cmdr.get(cmdr)
        pinned_tensor = (
            {(r.candidate, r.rule_id): r.contribution for r in pinned_entry.tensor_rows}
            if pinned_entry is not None
            else {}
        )
        live_tensor = (
            {(r.candidate, r.rule_id): r.contribution for r in live_entry.tensor_rows} if live_entry is not None else {}
        )
        all_keys = set(pinned_tensor) | set(live_tensor)
        for cand, rule_id in all_keys:
            p = pinned_tensor.get((cand, rule_id), 0.0)
            q = live_tensor.get((cand, rule_id), 0.0)
            if p != q:
                per_rule_delta[rule_id] += q - p
                per_rule_cmdrs[rule_id].add(cmdr)
                per_rule_cands[rule_id].add(cand)

    return [
        RuleDelta(
            rule_id=rule_id,
            contribution_delta_sum=delta,
            commanders_touched=len(per_rule_cmdrs[rule_id]),
            candidates_touched=len(per_rule_cands[rule_id]),
        )
        for rule_id, delta in per_rule_delta.items()
    ]


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
        "per_commander": [cd.to_dict() for cd in report.per_commander[:50]],
        "per_rule": [rd.to_dict() for rd in report.per_rule[:50]],
        "missing_commanders": report.identity_report.missing_commanders,
    }


def _render_markdown(report: AuditReport) -> str:
    lines: list[str] = []
    lines.append(f"# bench.py audit — {report.fixture_path}")
    lines.append("")

    status_icon = "✓" if report.is_identical else "✗"
    lines.append(f"**Status:** {status_icon} {'identical' if report.is_identical else 'drift detected'}")
    lines.append(
        f"**Config hash:** "
        f"`{report.pinned_config_hash[:12]}...` "
        f"{'== live' if report.config_hash_matches else f'!= `{report.live_config_hash[:12]}...`'}"
    )
    lines.append(f"**Aggregate score Δ:** `{report.aggregate_score_delta:+.6f}`")
    lines.append(f"**Commanders compared:** {report.commanders_compared}")
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
