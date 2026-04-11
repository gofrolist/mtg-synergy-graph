"""Validation harness: NDCG, EDHREC overlap, Golden Set regression (SPEC §10).

Three concerns live here:

* :func:`compute_ndcg` — discounted cumulative gain over a ranking, used as
  the headline metric for §10.4 Golden Set tracking. Supports binary or
  graded relevance labels.
* :func:`compare_to_edhrec` — joins the engine's top-N against the existing
  ``edhrec_card_synergy`` table from ``data/tags.db`` and reports
  Hi-Syn / Top / OnPage / NotEDH counts (matches the columns produced by
  the legacy ``scripts/compare_edhrec.py`` so the LightGBM and deterministic
  baselines are comparable apples-to-apples).
* :func:`bootstrap_golden_set` / :func:`check_golden_set` — round-trip the
  §10.4 regression suite. Bootstrap writes the current top-10 + NDCG@30 for
  each commander; check re-runs the engine and flags rank shifts > the
  ``jitter`` threshold or NDCG drops > the ``ndcg_tolerance``.

The data shapes are deliberately simple JSON so a CI step can `git diff`
the baseline file and the human reviewing the PR can read it.
"""

from __future__ import annotations

import json
import logging
import math
import re
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .engine import RecommendationPage, SynergyEngine

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# §10.1 NDCG metric
# ---------------------------------------------------------------------------


def compute_ndcg(
    predicted: Sequence[str],
    labels: dict[str, float],
    *,
    k: int = 30,
) -> float:
    """Discounted cumulative gain at ``k``, normalised by the ideal ranking.

    ``labels`` maps card name to a non-negative relevance score. Cards not
    present in ``labels`` contribute zero gain. The ideal ranking is the
    top-``k`` of ``labels`` sorted by descending value.

    Returns 0.0 if ``labels`` is empty (no ideal ranking exists).
    """
    if not labels or k <= 0:
        return 0.0

    def _dcg(scores: Iterable[float]) -> float:
        return sum(
            (math.pow(2.0, rel) - 1.0) / math.log2(i + 2)
            for i, rel in enumerate(scores)
            if rel > 0
        )

    pred_scores = [float(labels.get(card, 0.0)) for card in predicted[:k]]
    ideal_scores = sorted((float(v) for v in labels.values() if v > 0), reverse=True)[:k]
    if not ideal_scores:
        return 0.0
    dcg = _dcg(pred_scores)
    idcg = _dcg(ideal_scores)
    return dcg / idcg if idcg > 0 else 0.0


# ---------------------------------------------------------------------------
# §10.1 EDHREC overlap
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EdhrecComparison:
    """Output of :func:`compare_to_edhrec` (matches legacy script columns)."""

    commander:    str
    top_n:        int
    hi_syn_hits:  int   # cards in our top-N that are also in High Synergy Cards
    top_hits:     int   # cards in our top-N that are also in Top Cards
    on_page_hits: int   # cards in our top-N that appear in any EDHREC section
    not_edh:      int   # cards in our top-N that don't appear on EDHREC at all
    hi_syn_size:  int   # size of EDHREC High Synergy Cards section
    top_size:     int   # size of EDHREC Top Cards section
    on_page_size: int   # total cards on the EDHREC page


def commander_to_slug(name: str) -> str:
    """Convert a Forge commander name to the EDHREC slug form.

    EDHREC slugs are lowercase hyphen-separated, with apostrophes and commas
    stripped::

        "Korvold, Fae-Cursed King" → "korvold-fae-cursed-king"
        "Tymna the Weaver"          → "tymna-the-weaver"
    """
    cleaned = re.sub(r"[',]", "", name)
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", cleaned)
    return cleaned.strip("-").lower()


def _fetch_edhrec_sections(
    edhrec_conn: sqlite3.Connection,
    commander_slug: str,
) -> dict[str, set[str]]:
    """Return ``{section: {card_name, ...}}`` for the given commander slug."""
    rows = edhrec_conn.execute(
        "SELECT section, card_name FROM edhrec_card_synergy WHERE commander_slug = ?",
        (commander_slug,),
    ).fetchall()
    out: dict[str, set[str]] = {}
    for section, card_name in rows:
        out.setdefault(section or "", set()).add(card_name)
    return out


def compare_to_edhrec(
    page: RecommendationPage,
    edhrec_conn: sqlite3.Connection,
    *,
    top_n: int = 50,
) -> EdhrecComparison:
    """Compare an engine ``page`` against the EDHREC sections for the same commander."""
    commander_name = " + ".join(page.commander)
    slug = commander_to_slug(page.commander[0])
    sections = _fetch_edhrec_sections(edhrec_conn, slug)
    hi_syn = sections.get("High Synergy Cards", set())
    top = sections.get("Top Cards", set())
    on_page: set[str] = set()
    for cards in sections.values():
        on_page |= cards

    window = [rec.card for rec in page.items[:top_n]]
    return EdhrecComparison(
        commander=commander_name,
        top_n=top_n,
        hi_syn_hits=sum(1 for c in window if c in hi_syn),
        top_hits=sum(1 for c in window if c in top),
        on_page_hits=sum(1 for c in window if c in on_page),
        not_edh=sum(1 for c in window if c not in on_page),
        hi_syn_size=len(hi_syn),
        top_size=len(top),
        on_page_size=len(on_page),
    )


def edhrec_labels_for_commander(
    edhrec_conn: sqlite3.Connection,
    commander_name: str,
    *,
    grade_floor: float = 0.0,
) -> dict[str, float]:
    """Return ``{card_name: synergy_score}`` for use as NDCG labels.

    Cards with ``synergy < grade_floor`` are filtered out so the ideal
    ranking matches the LightGBM baseline (negative-synergy cards are not
    treated as relevant).
    """
    slug = commander_to_slug(commander_name)
    cur = edhrec_conn.execute(
        "SELECT card_name, MAX(synergy) AS synergy "
        "FROM edhrec_card_synergy WHERE commander_slug = ? "
        "GROUP BY card_name",
        (slug,),
    )
    return {
        row["card_name"]: float(row["synergy"])
        for row in cur.fetchall()
        if row["synergy"] is not None and row["synergy"] > grade_floor
    }


# ---------------------------------------------------------------------------
# §10.4 Golden Set
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GoldenSetEntry:
    commander:    str
    top10:        tuple[str, ...]
    ndcg30:       float
    hi_syn_total: int              = 0   # EDHREC high-synergy cards available
    hi_syn_hits:  int              = 0   # how many we ranked in top-30
    on_page_hits: int              = 0   # how many of our top-30 appear anywhere on EDHREC
    edhrec_top10: tuple[str, ...]  = ()  # EDHREC's top-10 high-synergy cards for reference


@dataclass(frozen=True)
class GoldenSetReport:
    entries:        tuple[GoldenSetEntry, ...]            = ()
    drift:          tuple[dict[str, Any], ...]            = ()
    ndcg_drops:     tuple[dict[str, Any], ...]            = ()
    rank_shifts:    tuple[dict[str, Any], ...]            = ()
    aggregate_ndcg: float                                 = 0.0
    baseline_ndcg:  float                                 = 0.0


def _commander_pair(spec: Any) -> list[str]:
    """Accept a string or a list and return the canonical commander list."""
    if isinstance(spec, str):
        return [spec]
    return list(spec)


def _run_one(
    engine: SynergyEngine,
    commander: list[str],
    edhrec_conn: sqlite3.Connection | None,
) -> GoldenSetEntry:
    page = engine.page(commander, offset=0, limit=30)
    top10 = tuple(rec.card for rec in page.items[:10])
    ndcg = 0.0
    hi_syn_total = 0
    hi_syn_hits = 0
    on_page_hits = 0
    edhrec_top10: tuple[str, ...] = ()

    if edhrec_conn is not None:
        labels = edhrec_labels_for_commander(edhrec_conn, commander[0])
        ranking = [rec.card for rec in page.items]
        ndcg = compute_ndcg(ranking, labels, k=30)

        # Hi-syn and on-page tracking
        slug = commander_to_slug(commander[0])
        sections = _fetch_edhrec_sections(edhrec_conn, slug)
        hi_syn = sections.get("High Synergy Cards", set())
        all_on_page: set[str] = set()
        for cards in sections.values():
            all_on_page |= cards

        top30 = set(ranking[:30])
        hi_syn_total = len(hi_syn)
        hi_syn_hits = len(top30 & hi_syn)
        on_page_hits = len(top30 & all_on_page)

        # Top-5 hi-syn by synergy score (for reference)
        hi_syn_rows = edhrec_conn.execute(
            "SELECT card_name, synergy FROM edhrec_card_synergy "
            "WHERE commander_slug = ? AND section = 'High Synergy Cards' "
            "ORDER BY synergy DESC LIMIT 10",
            (slug,),
        ).fetchall()
        edhrec_top10 = tuple(r["card_name"] for r in hi_syn_rows)

    return GoldenSetEntry(
        commander=" + ".join(commander),
        top10=top10,
        ndcg30=round(ndcg, 6),
        hi_syn_total=hi_syn_total,
        hi_syn_hits=hi_syn_hits,
        on_page_hits=on_page_hits,
        edhrec_top10=edhrec_top10,
    )


def bootstrap_golden_set(
    engine: SynergyEngine,
    commanders: Sequence[Any],
    output_path: Path,
    *,
    edhrec_conn: sqlite3.Connection | None = None,
) -> GoldenSetReport:
    """Run the engine on each commander and write the result as a baseline.

    ``commanders`` may contain plain strings or 2-element lists for partner
    pairs. The output JSON has shape::

        {
            "version": 1,
            "entries": [
                {"commander": "...", "top10": [...], "ndcg30": 0.4234},
                ...
            ]
        }
    """
    entries: list[GoldenSetEntry] = []
    for spec in commanders:
        cmdr = _commander_pair(spec)
        try:
            entries.append(_run_one(engine, cmdr, edhrec_conn))
        except ValueError as exc:
            log.warning("skipping %s: %s", cmdr, exc)
            continue

    payload = {
        "version": 1,
        "entries": [asdict(e) for e in entries],
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2, sort_keys=True))

    aggregate = (
        sum(e.ndcg30 for e in entries) / len(entries) if entries else 0.0
    )
    return GoldenSetReport(entries=tuple(entries), aggregate_ndcg=round(aggregate, 6))


def check_golden_set(
    engine: SynergyEngine,
    baseline_path: Path,
    *,
    edhrec_conn: sqlite3.Connection | None = None,
    jitter: int = 5,
    ndcg_tolerance: float = 0.005,
) -> GoldenSetReport:
    """Re-run the engine on the baseline commanders and detect regressions.

    A regression is recorded when:

    * the top-10 has more than ``jitter`` rank shifts compared to baseline
      (set difference, not pure permutation distance), OR
    * the NDCG@30 drops by more than ``ndcg_tolerance`` from the baseline
      value for that commander.

    Aggregate NDCG must also not drop by more than ``ndcg_tolerance`` across
    the whole set.
    """
    payload = json.loads(Path(baseline_path).read_text())
    baseline_entries = {
        entry["commander"]: entry for entry in payload.get("entries", [])
    }

    fresh_entries: list[GoldenSetEntry] = []
    drift: list[dict[str, Any]] = []
    ndcg_drops: list[dict[str, Any]] = []
    rank_shifts: list[dict[str, Any]] = []

    for canonical_name, baseline in baseline_entries.items():
        cmdr = canonical_name.split(" + ")
        try:
            fresh = _run_one(engine, cmdr, edhrec_conn)
        except ValueError as exc:
            drift.append({"commander": canonical_name, "error": str(exc)})
            continue
        fresh_entries.append(fresh)

        baseline_top = set(baseline["top10"])
        fresh_top = set(fresh.top10)
        new_in_top = sorted(fresh_top - baseline_top)
        out_of_top = sorted(baseline_top - fresh_top)
        shift_count = max(len(new_in_top), len(out_of_top))
        if shift_count > jitter:
            rank_shifts.append(
                {
                    "commander":  canonical_name,
                    "added":      new_in_top,
                    "removed":    out_of_top,
                    "shift":      shift_count,
                    "threshold":  jitter,
                }
            )

        baseline_ndcg = float(baseline.get("ndcg30") or 0.0)
        if fresh.ndcg30 + ndcg_tolerance < baseline_ndcg:
            ndcg_drops.append(
                {
                    "commander":  canonical_name,
                    "baseline":   baseline_ndcg,
                    "fresh":      fresh.ndcg30,
                    "delta":      round(fresh.ndcg30 - baseline_ndcg, 6),
                    "tolerance":  ndcg_tolerance,
                }
            )

    aggregate_fresh = (
        sum(e.ndcg30 for e in fresh_entries) / len(fresh_entries)
        if fresh_entries
        else 0.0
    )
    aggregate_baseline = (
        sum(float(b.get("ndcg30") or 0.0) for b in baseline_entries.values())
        / len(baseline_entries)
        if baseline_entries
        else 0.0
    )

    return GoldenSetReport(
        entries=tuple(fresh_entries),
        drift=tuple(drift),
        ndcg_drops=tuple(ndcg_drops),
        rank_shifts=tuple(rank_shifts),
        aggregate_ndcg=round(aggregate_fresh, 6),
        baseline_ndcg=round(aggregate_baseline, 6),
    )


def regression_failed(report: GoldenSetReport, *, ndcg_tolerance: float = 0.005) -> bool:
    """Return True if the report should fail a CI check."""
    if report.rank_shifts or report.ndcg_drops:
        return True
    if report.baseline_ndcg - report.aggregate_ndcg > ndcg_tolerance:
        return True
    return False
