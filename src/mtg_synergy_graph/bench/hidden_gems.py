"""Hidden-gem metric core (Unit 1 of plan 003).

Pure-function substrate for the ``hidden_gem_hit_rate`` axis of
``bench.py audit``. Takes pre-shaped tensor rows and computes:

* ``plausibility(cmdr, cand, contributions)`` — the FR2 mechanical
  plausibility gate (``N_rules_firing >= 2`` OR
  ``total_contribution > median``, strict inequality, zero-median
  fallback to the N_rules leg).
* ``hidden_gem_hit_rate_for_commander(our_top_30, edhrec_top_30,
  contributions)`` — fraction of our top-30 that (a) EDHREC's top-30
  did not list and (b) passes the plausibility gate. Returns ``None``
  when ``edhrec_top_30 is None`` (no EDHREC data → skip).
* ``aggregate_hidden_gem_hit_rate(entries)`` — mean over commanders
  with entries; records skipped commanders.

The module is DB-agnostic: callers (Unit 2) shape sqlite rows from
``rule_contributions`` into ``(candidate, rule_id, contribution)``
tuples and pass them in. No sqlite import here.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

#: Warning threshold for aggregate delta regressions (FR4). If the
#: delta between live and pinned aggregate ``hidden_gem_hit_rate`` drops
#: below ``-_HIDDEN_GEM_WARN_THRESHOLD``, the audit prints a stderr
#: warning. The commit is NOT gated on this at MVP — see plan 003 FR6
#: escalation criteria: promotion to a commit-gate requires (1) the
#: metric to be tracked for ≥20 commits, (2) human correlation
#: confirming metric drops track subjectively-bad changes, and (3) a
#: false-positive rate below 10% on recent accepted commits. Promotion
#: is itself a separate ce-brainstorm + ce-plan cycle.
_HIDDEN_GEM_WARN_THRESHOLD = 0.02


@dataclass(frozen=True)
class HiddenGemEntry:
    """One commander's hidden-gem metric value + the specific cards."""

    commander: str
    rate: float
    hidden_cards: tuple[str, ...]


@dataclass(frozen=True)
class HiddenGemReport:
    """Aggregate hidden-gem metric across commanders.

    ``aggregate`` is None when no commander had EDHREC data (everyone
    was skipped). ``skipped_commanders`` carries the names of
    commanders whose per-commander computation returned ``None``
    (``edhrec_top_30 is None``).
    """

    aggregate: float | None
    per_commander: dict[str, HiddenGemEntry]
    skipped_commanders: tuple[str, ...]


def _aggregate_contributions(
    contributions: Iterable[tuple[str, str, float]],
) -> tuple[dict[str, int], dict[str, float]]:
    """Fold tensor rows into per-candidate (n_rules_firing, total).

    Only contributions strictly greater than zero count — a rule that
    reduces the score shouldn't make the card "more plausible."
    """
    n_rules: dict[str, int] = defaultdict(int)
    totals: dict[str, float] = defaultdict(float)
    for candidate, _rule_id, contribution in contributions:
        if contribution > 0:
            n_rules[candidate] += 1
            totals[candidate] += contribution
    return dict(n_rules), dict(totals)


def plausibility(
    cmdr: str,
    cand: str,
    contributions: Iterable[tuple[str, str, float]],
) -> bool:
    """FR2 mechanical plausibility gate for one ``(cmdr, cand)``.

    Returns True iff
    ``N_rules_firing(cmdr, cand) >= 2`` OR
    ``total_contribution(cmdr, cand) > median_contribution(cmdr)``
    (strict inequality).

    When the per-commander median is 0 (all candidate totals are 0),
    the median-OR leg is vacuous — strict ``> 0`` would flag every
    non-zero candidate regardless of specificity. We short-circuit to
    the N_rules leg only in that case.

    ``cmdr`` is accepted for API symmetry with future extensions
    (embedding-based plausibility tightening — see plan 003 "Deferred
    to Separate Tasks") but not used by the pure-mechanical gate.
    """
    # ``cmdr`` is reserved for future per-commander tuning (FR6
    # escalation path); silence unused-argument without per-line noqa.
    del cmdr
    n_rules_map, totals_map = _aggregate_contributions(contributions)
    n_rules_cand = n_rules_map.get(cand, 0)

    if n_rules_cand >= 2:
        return True

    total_cand = totals_map.get(cand, 0.0)
    if total_cand <= 0:
        return False

    # Median is computed across candidates with any positive total.
    # Empty or all-zero cohort → median fallback rejects everything.
    positive_totals = [v for v in totals_map.values() if v > 0]
    if not positive_totals:
        return False

    median = statistics.median(positive_totals)
    if median <= 0:
        # Degenerate: the median of the positive set somehow collapses
        # to zero. Fall back to N_rules leg only.
        return False

    return total_cand > median


def hidden_gem_hit_rate_for_commander(
    commander: str,
    our_top_30: Sequence[str],
    edhrec_top_30: set[str] | None,
    contributions: Iterable[tuple[str, str, float]],
) -> HiddenGemEntry | None:
    """FR1 per-commander metric.

    Returns None when ``edhrec_top_30 is None`` — "no EDHREC data,
    skip." Aggregator treats these as skipped commanders, not as
    zero-rate contributors.

    Input ordering of ``our_top_30`` does not affect the output. The
    function rejects duplicates with ``ValueError("our_top_30 must be
    unique")`` since duplicates would double-count against the 30-card
    denominator.
    """
    if len(set(our_top_30)) != len(our_top_30):
        raise ValueError("our_top_30 must be unique")

    if edhrec_top_30 is None:
        return None

    # Materialize once so we can iterate twice (plausibility check).
    rows = list(contributions)

    our_set = set(our_top_30)
    hidden = our_set - edhrec_top_30

    plausible_hidden = sorted(c for c in hidden if plausibility(commander, c, rows))

    rate = len(plausible_hidden) / 30
    return HiddenGemEntry(
        commander=commander,
        rate=rate,
        hidden_cards=tuple(plausible_hidden),
    )


def aggregate_hidden_gem_hit_rate(
    entries: Iterable[tuple[str, HiddenGemEntry | None]],
) -> HiddenGemReport:
    """Combine per-commander entries into aggregate report.

    Each input tuple is ``(commander_name, entry_or_None)``. ``None``
    entries move the commander into ``skipped_commanders``; real
    entries contribute their ``rate`` to the arithmetic-mean
    ``aggregate``. Empty input and all-None input both produce
    ``aggregate = None``.
    """
    per_commander: dict[str, HiddenGemEntry] = {}
    skipped: list[str] = []

    for name, entry in entries:
        if entry is None:
            skipped.append(name)
        else:
            per_commander[name] = entry

    if not per_commander:
        aggregate: float | None = None
    else:
        aggregate = sum(e.rate for e in per_commander.values()) / len(per_commander)

    return HiddenGemReport(
        aggregate=aggregate,
        per_commander=per_commander,
        skipped_commanders=tuple(skipped),
    )
