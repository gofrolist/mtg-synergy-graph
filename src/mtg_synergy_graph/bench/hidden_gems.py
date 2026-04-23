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
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType

#: Warning threshold for aggregate delta regressions (FR4). If the
#: delta between live and pinned aggregate ``hidden_gem_hit_rate`` drops
#: below ``-HIDDEN_GEM_WARN_THRESHOLD``, the audit prints a stderr
#: warning. The commit is NOT gated on this at MVP — see plan 003 FR6
#: escalation criteria: promotion to a commit-gate requires (1) the
#: metric to be tracked for ≥20 commits, (2) human correlation
#: confirming metric drops track subjectively-bad changes, and (3) a
#: false-positive rate below 10% on recent accepted commits. Promotion
#: is itself a separate ce-brainstorm + ce-plan cycle.
HIDDEN_GEM_WARN_THRESHOLD = 0.02


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

    ``per_commander`` is exposed as a ``Mapping`` and wrapped in a
    ``MappingProxyType`` at construction so the ``frozen=True``
    contract isn't silently violated by callers mutating the dict in
    place. Equality comparisons against plain ``dict`` instances still
    work (``MappingProxyType`` compares equal to a dict with the same
    items).
    """

    aggregate: float | None
    per_commander: Mapping[str, HiddenGemEntry]
    skipped_commanders: tuple[str, ...]

    def __post_init__(self) -> None:
        # Freeze the mapping so callers can't mutate ``per_commander``
        # even though the dataclass itself is frozen. We rebuild from a
        # dict so the proxy wraps an independent, caller-unreachable
        # backing dict.
        object.__setattr__(
            self,
            "per_commander",
            MappingProxyType(dict(self.per_commander)),
        )


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


def _plausibility_from_maps(
    cand: str,
    n_rules_map: Mapping[str, int],
    totals_map: Mapping[str, float],
    median: float,
) -> bool:
    """FR2 plausibility gate against pre-aggregated maps + median.

    Internal helper so batched callers can aggregate contributions +
    compute the median once across the whole candidate cohort, then
    evaluate plausibility for each candidate in O(1) per call.

    ``median`` is expected to be the median of strictly-positive totals
    across the commander's cohort; callers pass ``0.0`` when the cohort
    has no positive totals (the median-OR leg then short-circuits to
    False, falling back to the N_rules leg only).
    """
    n_rules_cand = n_rules_map.get(cand, 0)
    if n_rules_cand >= 2:
        return True

    total_cand = totals_map.get(cand, 0.0)
    if total_cand <= 0:
        return False

    if median <= 0:
        # Degenerate: the cohort has no positive totals OR the median
        # collapses to zero. The median-OR leg is vacuous; fall back to
        # the N_rules leg only (which already failed).
        return False

    return total_cand > median


def _cohort_median(totals_map: Mapping[str, float]) -> float:
    """Return the median of strictly-positive totals, or ``0.0`` if none.

    Zero-median fallback is documented in ``plausibility``: an all-zero
    cohort should reject every candidate via the N_rules leg only.
    """
    positive_totals = [v for v in totals_map.values() if v > 0]
    if not positive_totals:
        return 0.0
    return statistics.median(positive_totals)


def plausibility(
    _cmdr: str,
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

    ``_cmdr`` is accepted for API symmetry with future extensions
    (embedding-based plausibility tightening — see plan 003 "Deferred
    to Separate Tasks") but not used by the pure-mechanical gate.

    **Performance note:** this function is ``O(rows)`` per call — it
    aggregates contributions and computes the cohort median fresh every
    invocation. Use :func:`hidden_gem_hit_rate_for_commander` (or the
    internal :func:`_plausibility_from_maps`) when evaluating many
    candidates against the same commander; that path amortizes the
    aggregation to ``O(rows)`` across the whole cohort instead of
    ``O(rows × candidates)``.
    """
    n_rules_map, totals_map = _aggregate_contributions(contributions)
    median = _cohort_median(totals_map)
    return _plausibility_from_maps(cand, n_rules_map, totals_map, median)


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

    # Aggregate contributions + compute the cohort median ONCE so the
    # per-candidate plausibility check is O(1). Prior versions called
    # ``plausibility`` (which re-aggregates internally) once per hidden
    # candidate — O(H × rows) for H hidden candidates. This is now O(rows).
    rows = list(contributions)
    n_rules_map, totals_map = _aggregate_contributions(rows)
    median = _cohort_median(totals_map)

    our_set = set(our_top_30)
    hidden = our_set - edhrec_top_30

    plausible_hidden = sorted(c for c in hidden if _plausibility_from_maps(c, n_rules_map, totals_map, median))

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
