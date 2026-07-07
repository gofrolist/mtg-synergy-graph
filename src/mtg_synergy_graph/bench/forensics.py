"""Divergence-forensics core — per-miss failure taxonomy (Unit 1 of
plan 2026-06-10-001).

Classifies every (commander, missed EDHREC label card) pair into
exactly one of five mechanical failure buckets:

* ``NEAR_MISS`` — ranked 31..60 in the live full ranking.
* ``OUTRANKED`` — ranked 61+ (sub-tag ``staple_only`` when the tensor
  holds zero rows for the pair: the card's live score came entirely
  from non-rule channels).
* ``FILTERED`` — unranked but the tensor HAS rows: the card was scored
  and then dropped by ``engine.page()``'s legality chain. The reason
  code re-evaluates the engine's exact predicate chain from ``cards``
  rows in documented precedence (``color_illegal`` / ``not_legal`` /
  ``empty_types`` / ``non_edh_type`` / ``is_commander``); when no
  predicate fails, ``filter_reason_unknown`` is a tensor-staleness
  diagnostic counted explicitly.
* ``DATA_GAP`` — unranked, no tensor rows, and the card is absent from
  ``cards`` (``card_absent``, optionally with ``name_unmatched`` when
  name normalization failed), has zero ``port_nodes`` rows
  (``no_ports``), or >50% of its ``port_nodes`` rows are ``UNKNOWN``
  (``unknown_ports``).
* ``NO_RULES`` — unranked, no tensor rows, well-formed port data: the
  vocabulary simply has no rule connecting the pair.

Miss universe per commander = top-30 graded labels by synergy
descending (``grade_floor=0.0``), CLOSED UNDER TIES at the 30th value.
Ranks come from one live ``engine.page(offset=0, limit=1_000_000)``
pass per commander on a shared :class:`SynergyEngine`; "unranked" is
precisely "absent from that filtered full ranking".

The classification core is pure (plain-data inputs — ranking
sequence, tensor-presence set, port-shape map), mirroring
``bench/hidden_gems.py``'s DB-agnostic discipline. DB access is
isolated in the ``load_*`` helpers and the :func:`compute_forensics`
shell, which uses the repo's two-connection pattern (``open_db`` for
synergy.db + ``sqlite3.connect`` for tags.db — never ATTACH).

Metric sidecars (Unit 2 of the same plan) ride on the Unit-1 live
pass — no additional scoring pass:

* Per-commander NDCG@30 (:func:`validate.compute_ndcg` over the live
  ranking, canonical validate-path labels) and raw DCG@30
  (:func:`compute_raw_dcg` — same gains/discounts, no ideal
  normalizer), both stored UNROUNDED on :class:`CommanderForensics`.
* ``aggregate_ndcg_canonical`` — mean over ALL fixture commanders
  with zero-label commanders contributing 0.0 (the validate-path
  golden-set denominator); the exclusion-based per-commander view is
  ``ForensicsReport.entries`` + ``skipped_commanders``.
* Reconciliation assertion (:func:`reconcile_canonical_ndcg`):
  every commander's canonical NDCG is recomputed from an
  INDEPENDENT ``engine.page(offset=0, limit=top_n)`` window
  (:func:`extract_canonical_window` — cache hit on the shared
  engine, so no second scoring pass) and the two canonical
  aggregates must agree within :data:`RECONCILIATION_EPSILON`;
  mismatch raises :class:`ForensicsReconciliationError` naming the
  first divergent commander, before anything is written.
* Sampled independent-engine check
  (:func:`run_independent_engine_check`): the first
  :data:`INDEPENDENT_CHECK_SAMPLE_SIZE` commanders alphabetically
  are re-scored on a SEPARATELY constructed :class:`SynergyEngine`
  (own connection) — the production-faithfulness evidence the
  shared-engine assertion cannot provide. Runs by default inside
  :func:`compute_forensics` (``independent_check=False`` to skip).

Justified-divergence view (Unit 3 of the same plan, R9):

* Divergent picks per commander = live top-30 cards ABSENT from the
  ALL-SECTIONS graded label set (the same
  ``edhrec_labels_for_commander(grade_floor=0.0)`` mapping the miss
  universe is built from). This is DELIBERATELY a different
  reference set from the HS-top-30 one
  ``hidden_gem_hit_rate_for_commander`` uses — the gem axis keeps
  HS-top-30 for series continuity (origin R7); R9 asks "of the
  picks EDHREC lists NOWHERE with positive synergy, how many can we
  mechanically justify?".
* :func:`justified_divergence_for_commander` is a thin wrapper
  applying the SAME plausibility-gate constants as
  ``bench/hidden_gems.py`` (``N_rules_firing >= 2`` OR
  ``total_contribution > cohort median``, identical cohort
  convention: the commander's full tensor-row cohort) to the wider
  reference set. Advisory only — never a gate (FR6 discipline).
* ``listed_nonpositive`` = live top-30 picks present in the
  UNFLOORED label set with ``MAX(synergy) <= 0`` (the floored
  loader excludes them). Counted separately; never subtracts from
  bucket sums or divergence counts. Loaded by
  :func:`load_nonpositive_listed` — the ONLY new DB read Unit 3
  adds, on the existing tags.db connection.
* Gate-margin stratification: bucketed counts of N_rules_firing
  (:data:`N_RULES_BIN_LABELS`) and contribution/median ratio
  (:data:`RATIO_BIN_LABELS`) for the justified picks. Bin edges are
  implementation-chosen (the plan defers them).
* A 100% pass-rate is representable: ``gate_saturated`` is True
  when every divergent pick passed, so the Unit-4 renderer can
  annotate "gate too loose to discriminate".

Tiebreaker ablation (Unit 6 of the same plan, R8 — one-off
measurement mode, ``--forensics --ablate-tiebreak``):

* :func:`compute_tiebreak_ablation` re-sorts each commander's
  captured :class:`RankedCandidate` tuples under two bracketing
  replacement keys — weak ``(-total_score, name)`` and strong
  ``(-total_score, cmc, name)`` — and reports the signed
  production-minus-replacement NDCG@30 deltas plus their range,
  aggregated over the CANONICAL denominator. Pure function of the
  Unit-1 capture; no extra scoring pass, ``engine.page()`` untouched.
* MANDATORY self-check first (:func:`verify_production_sort`):
  re-sorting by the reconstructed production key ``(-total_score,
  cmc, edhrec_rank, name)`` must reproduce the captured rank order
  EXACTLY for every commander; any mismatch raises
  :class:`TiebreakSelfCheckError` (exit-2 mapped — catches
  sentinel/NULL drift) and NO deltas are reported.
* When the upper bound of unearned credit exceeds
  :data:`TIEBREAK_FLAG_THRESHOLD` (0.01),
  :attr:`TiebreakAblation.rule_history_markdown` carries a
  ready-to-paste RULE_HISTORY entry citing the plan — the human
  commits it; the run stays read-only. The forensics history row is
  NOT extended (one-off mode; no schema change).

rank_bonus ablation (Task 1 of plan 2026-07-07-002 — standing
sidecar, computed on EVERY ``--forensics`` run, no flag):

* A 2026-07-07 diagnostic measured that the EDHREC-derived
  ``rank_bonus`` in-score micro-term (``0.005 * max(0.0, 1.0 -
  edhrec_rank / 30000.0)``, ``universal_scorer.py:1114`` /
  ``:1136``) PLUS the sort-key tiebreak together carry -0.0441 of the
  golden-100 mean NDCG@30 (0.2328 raw -> 0.1887 ablated) — contrary to
  the "no EDHREC at inference" claim CLAUDE.md used to make (see
  CLAUDE.md for the corrected wording). :func:`compute_rank_bonus_ablation`
  reports this measurement on every run so future ship/decline
  verdicts can separate mechanical rule signal from this hidden
  credit. Unlike :func:`compute_tiebreak_ablation` (R8, sort-key ONLY,
  one-off ``--ablate-tiebreak`` mode), this sidecar subtracts the
  IN-SCORE ``rank_bonus`` from each candidate's captured total AND
  drops ``edhrec_rank`` from the re-sort key entirely — it measures
  the TOTAL EDHREC-at-inference credit (in-score + in-sort), an upper
  bound of either ablation alone, matching the diagnostic's
  methodology exactly. The bonus itself is KEPT in the score; this is
  a read-side measurement only, never a scoring-path change.

Read-only diagnostic: nothing here mutates either database or the
pinned fixture. CLI wiring, renderers, and the history CSV live in
:mod:`bench.forensics_report` / :mod:`bench.forensics_history`.
"""

from __future__ import annotations

import hashlib
import math
import sqlite3
import sys
from collections.abc import Iterable, Mapping, Sequence, Set
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any

from mtg_synergy_graph.bench.fixture import PinnedFixture
from mtg_synergy_graph.bench.hidden_gems import (
    _aggregate_contributions,
    _cohort_median,
    _plausibility_from_maps,
)
from mtg_synergy_graph.bench.tensor import compute_config_hash
from mtg_synergy_graph.db import open_db
from mtg_synergy_graph.edhrec_helpers import HIGH_SYNERGY_SECTION
from mtg_synergy_graph.engine import (
    NON_EDH_CARD_TYPES,
    UNRANKED_EDHREC_SENTINEL,
    SynergyEngine,
)
from mtg_synergy_graph.validate import commander_to_slug, compute_ndcg, edhrec_labels_for_commander

# ---------------------------------------------------------------------------
# Constants — buckets, reason codes, classification thresholds
# ---------------------------------------------------------------------------

BUCKET_NEAR_MISS = "NEAR_MISS"
BUCKET_OUTRANKED = "OUTRANKED"
BUCKET_FILTERED = "FILTERED"
BUCKET_DATA_GAP = "DATA_GAP"
BUCKET_NO_RULES = "NO_RULES"

#: Canonical bucket ordering for reports + count dicts. Every
#: ``bucket_counts`` mapping in this module carries all five keys.
BUCKETS: tuple[str, ...] = (
    BUCKET_NEAR_MISS,
    BUCKET_OUTRANKED,
    BUCKET_FILTERED,
    BUCKET_DATA_GAP,
    BUCKET_NO_RULES,
)

#: OUTRANKED sub-tag: ranked but zero tensor rows for the pair.
REASON_STAPLE_ONLY = "staple_only"

#: FILTERED reason codes, in the documented re-evaluation precedence.
REASON_COLOR_ILLEGAL = "color_illegal"
REASON_NOT_LEGAL = "not_legal"
REASON_EMPTY_TYPES = "empty_types"
REASON_NON_EDH_TYPE = "non_edh_type"
REASON_IS_COMMANDER = "is_commander"
#: Tensor rows present + legal + unranked: no drop predicate fails.
#: This is a tensor-staleness diagnostic and is counted explicitly.
REASON_FILTER_UNKNOWN = "filter_reason_unknown"

#: DATA_GAP reason codes.
REASON_CARD_ABSENT = "card_absent"
REASON_NO_PORTS = "no_ports"
REASON_UNKNOWN_PORTS = "unknown_ports"

#: Skip reason for commanders with zero graded labels.
SKIP_REASON_ZERO_LABELS = "zero_labels"

#: Miss-universe / live-top window size (R1: top-30 labels, top-30 live).
TOP_N_DEFAULT = 30

#: Upper rank bound (inclusive) for NEAR_MISS; 61+ is OUTRANKED.
NEAR_MISS_MAX_RANK = 60

#: DATA_GAP ``unknown_ports`` threshold: strictly more than this share
#: of a card's ``port_nodes`` rows classified ``UNKNOWN``.
UNKNOWN_PORT_SHARE_THRESHOLD = 0.5

#: ``engine.page()``'s NULL-cmc sentinel, mirrored here for the
#: captured sort-key tuples. The edhrec_rank sentinel is IMPORTED from
#: ``engine`` (``UNRANKED_EDHREC_SENTINEL``), never duplicated.
NULL_CMC_SENTINEL = 99.0

#: Tolerance for the reconciliation assertion and the sampled
#: independent-engine check. Comparisons use UNROUNDED values on both
#: sides (``validate._run_one`` rounds to 6 dp — conventions are
#: never mixed here).
RECONCILIATION_EPSILON = 1e-6

#: How many commanders (first N alphabetically, for determinism) the
#: sampled independent-engine check re-scores on a separately
#: constructed :class:`SynergyEngine`.
INDEPENDENT_CHECK_SAMPLE_SIZE = 5

#: Unit-6 (R8) tiebreaker-ablation flag threshold: when the UPPER
#: BOUND of unearned EDHREC tiebreak credit (the larger of the two
#: production-minus-replacement NDCG deltas) exceeds this, the run
#: emits a ready-to-paste RULE_HISTORY entry flagging sort-key
#: remediation as a next-cycle candidate.
TIEBREAK_FLAG_THRESHOLD = 0.01

#: Plan path cited by the R8 RULE_HISTORY flag entry.
FORENSICS_PLAN_PATH = "docs/plans/2026-06-10-001-feat-divergence-forensics-plan.md"

#: Gate-margin stratification bins for justified divergent picks
#: (Unit 3). IMPLEMENTATION-CHOSEN — the plan defers exact edges.
#: ``N_rules_firing`` bins: a pick justified via the contribution leg
#: can carry 0 or 1 firing rules, hence the ``0-1`` bin alongside the
#: gate's ``>= 2`` leg values.
N_RULES_BIN_LABELS: tuple[str, ...] = ("0-1", "2", "3", "4", "5+")

#: ``total_contribution / cohort_median`` ratio bins (same
#: implementation-chosen caveat). Edges are inclusive on the upper
#: bound: ``<=1``, ``(1, 2]``, ``(2, 5]``, ``> 5``. A zero/negative
#: cohort median makes the ratio undefined — such picks (justified
#: via the N_rules leg only) land in ``<=1``.
RATIO_BIN_LABELS: tuple[str, ...] = ("<=1", "1-2", "2-5", ">5")

#: Em-dash placeholder cell. Defined ONCE here and imported by the
#: renderers in :mod:`bench.forensics_report` / :mod:`bench.forensics_history`.
_EM_DASH = "—"


class ForensicsPreconditionError(Exception):
    """A forensics precondition failed — nothing was computed.

    The future ``--forensics`` handler (Unit 4) maps this to exit
    code 2 (usage / stale input), matching the strict-consumer
    failure mode of ``bench/optimize.py`` and the offline-oracle hash
    pattern: no partial report, no history row.
    """


class ForensicsReconciliationError(Exception):
    """The canonical-NDCG reconciliation (or the sampled
    independent-engine check) failed.

    Raised by :func:`reconcile_canonical_ndcg` when the forensics
    aggregate diverges from the same-run canonical recompute by more
    than :data:`RECONCILIATION_EPSILON`, and by
    :func:`run_independent_engine_check` when a separately
    constructed engine disagrees on a sampled commander. The message
    names the first divergent commander. Callers must write NOTHING
    when this raises (the Unit-4 handler maps it to exit 2: no
    report file, no history row).
    """


# ---------------------------------------------------------------------------
# Frozen data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LabelCard:
    """One graded EDHREC label card inside a commander's miss universe."""

    name: str
    synergy: float
    #: True when the card appears in the EDHREC ``'High Synergy
    #: Cards'`` section for this commander.
    hs_section_member: bool


@dataclass(frozen=True, slots=True)
class RankedCandidate:
    """One candidate from the live full ranking, with its production
    sort-key components captured.

    ``cmc`` and ``edhrec_rank`` are re-read from the ``cards`` table
    with the engine's NULL sentinels applied (NULL cmc → 99.0, NULL
    rank → :data:`UNRANKED_EDHREC_SENTINEL`) because
    ``Recommendation`` carries neither — Unit 6's tiebreaker ablation
    re-sorts these captured tuples and depends on this exact capture.
    """

    name: str
    rank: int
    total_score: float
    cmc: float
    edhrec_rank: int

    @property
    def production_sort_key(self) -> tuple[float, float, int, str]:
        """The exact ``engine.page()`` sort key for this candidate."""
        return (-self.total_score, self.cmc, self.edhrec_rank, self.name)


@dataclass(frozen=True)
class MissRecord:
    """One classified (commander, missed label card) pair."""

    commander: str
    card_name: str
    #: EDHREC synergy score of the label (the miss-universe grade).
    synergy: float
    #: One of :data:`BUCKETS`.
    bucket: str
    #: Reason / sub-tag (FILTERED reason codes, DATA_GAP reason codes,
    #: OUTRANKED ``staple_only``); ``None`` when the bucket carries no
    #: sub-tag (NEAR_MISS, plain OUTRANKED, NO_RULES).
    reason: str | None
    #: Live full-ranking position, or ``None`` when unranked.
    rank: int | None
    #: True when the label sits in EDHREC's High Synergy Cards section.
    hs_section_member: bool
    #: True when name normalization failed to resolve the EDHREC name
    #: to a ``cards.name`` row.
    name_unmatched: bool


@dataclass(frozen=True)
class JustifiedDivergence:
    """Per-commander justified-divergence view (Unit 3, R9).

    Divergent picks = live top-30 cards absent from the ALL-SECTIONS
    graded label set (the ``edhrec_labels_for_commander
    (grade_floor=0.0)`` mapping — NOT the HS-top-30 reference the gem
    axis uses) and not listed at nonpositive synergy. Annotation
    only: these counts never subtract from bucket sums.

    ``n_rules_distribution`` / ``ratio_distribution`` carry every bin
    key from :data:`N_RULES_BIN_LABELS` / :data:`RATIO_BIN_LABELS`
    (zero-filled) and are wrapped in ``MappingProxyType`` at
    construction (same frozen-mapping discipline as
    ``CommanderForensics.bucket_counts``).
    """

    #: Total divergent picks (the gate-pass-rate denominator).
    divergent: int
    justified_divergences: int
    unjustified_divergences: int
    #: ``justified / divergent``; ``None`` when zero divergent picks
    #: (renderers show ``—`` instead of dividing by zero).
    gate_pass_rate: float | None
    #: Live top-30 picks present in the UNFLOORED label set with
    #: ``MAX(synergy) <= 0``. A separate reported count — never
    #: counted divergent, never subtracted from bucket sums.
    listed_nonpositive: int
    #: Divergent picks that passed / failed the plausibility gate,
    #: name-sorted (deterministic; Unit 4 renders these).
    justified_cards: tuple[str, ...]
    unjustified_cards: tuple[str, ...]
    #: Gate-margin stratification over the JUSTIFIED picks only.
    n_rules_distribution: Mapping[str, int]
    ratio_distribution: Mapping[str, int]

    def __post_init__(self) -> None:
        object.__setattr__(self, "n_rules_distribution", MappingProxyType(dict(self.n_rules_distribution)))
        object.__setattr__(self, "ratio_distribution", MappingProxyType(dict(self.ratio_distribution)))

    @property
    def gate_saturated(self) -> bool:
        """True when EVERY divergent pick passed the gate.

        A 100% pass-rate over a nonempty divergent set means the gate
        discriminated nothing for this commander — the Unit-4
        renderer annotates it "gate too loose to discriminate".
        ``False`` when there are zero divergent picks (nothing to
        saturate).
        """
        return self.divergent > 0 and self.justified_divergences == self.divergent


@dataclass(frozen=True)
class CommanderForensics:
    """One commander's classified misses + live-ranking capture.

    ``bucket_counts`` always carries all five bucket keys and is
    wrapped in a ``MappingProxyType`` at construction so the
    ``frozen=True`` contract isn't silently violated by callers
    mutating the dict in place (same discipline as
    ``HiddenGemReport.per_commander``).
    """

    commander: str
    misses: tuple[MissRecord, ...]
    bucket_counts: Mapping[str, int]
    #: Live top-30 candidate names in rank order.
    live_top_30: tuple[str, ...]
    #: Full live ranking with captured sort-key components (Unit 6
    #: re-sorts these; Units 2-3 read the top-30 window).
    ranking: tuple[RankedCandidate, ...]
    #: Per-commander NDCG@30 over the live ranking against canonical
    #: validate-path labels (``grade_floor=0.0``). UNROUNDED — the
    #: reconciliation assertion and the sampled independent-engine
    #: check compare at :data:`RECONCILIATION_EPSILON`; renderers may
    #: round for display. 0.0 for skipped (zero-label) commanders.
    ndcg30: float = 0.0
    #: Per-commander raw DCG@30: same gains (``2^rel − 1``) and
    #: ``log2`` discounts as ``validate.compute_ndcg`` but WITHOUT
    #: dividing by the ideal DCG. UNROUNDED; always >= 0.
    raw_dcg30: float = 0.0
    #: Unit-3 justified-divergence view (R9). ``None`` for skipped
    #: (zero-label) commanders and for entries built by pure-core
    #: callers that did not compute it.
    justified_divergence: JustifiedDivergence | None = None
    #: True when the commander was skipped (zero graded labels) and
    #: must be excluded from bucket aggregates.
    skipped: bool = False
    skip_reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "bucket_counts", MappingProxyType(dict(self.bucket_counts)))


@dataclass(frozen=True)
class ForensicsReport:
    """Aggregate forensics across the fixture's commanders.

    Two denominators, explicitly named (plan Key Decisions):

    * ``aggregate_ndcg_canonical`` / ``aggregate_raw_dcg_canonical``
      — mean over ALL fixture commanders, with zero-label (skipped)
      commanders contributing 0.0. This matches the validate-path
      golden-set aggregate denominator and is the one the
      reconciliation assertion uses.
    * The exclusion-based per-commander view: ``entries`` holds only
      non-skipped commanders; skipped ones (zero graded labels) are
      listed in ``skipped_commanders`` and contribute nothing to
      ``aggregate_bucket_counts`` (bucket proportions use this
      denominator).
    """

    entries: tuple[CommanderForensics, ...]
    aggregate_bucket_counts: Mapping[str, int]
    total_misses: int
    skipped_commanders: tuple[str, ...]
    #: Canonical-denominator mean NDCG@30 (all fixture commanders,
    #: zero-label → 0.0). UNROUNDED.
    aggregate_ndcg_canonical: float = 0.0
    #: Canonical-denominator mean raw DCG@30 (same denominator).
    aggregate_raw_dcg_canonical: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "aggregate_bucket_counts",
            MappingProxyType(dict(self.aggregate_bucket_counts)),
        )


@dataclass(frozen=True)
class ReconciliationPair:
    """One commander's (forensics, canonical-recompute) NDCG pair.

    ``forensics_ndcg`` comes from the Unit-1 full-ranking pass;
    ``recompute_ndcg`` from the independent
    ``engine.page(limit=top_n)`` window. Both UNROUNDED. Zero-label
    commanders appear as ``(0.0, 0.0)`` so the canonical denominator
    (all fixture commanders) is preserved.
    """

    commander: str
    forensics_ndcg: float
    recompute_ndcg: float


# ---------------------------------------------------------------------------
# Name normalization (one boundary, applied at every join)
# ---------------------------------------------------------------------------


def normalize_label_name(label_name: str, known_names: Set[str]) -> str | None:
    """Resolve an EDHREC label name to a ``cards.name`` row.

    v1 contract (fixed so two implementers produce the same
    ``card_absent`` counts): exact match first, then the front face of
    a ``" // "`` split name. Anything else returns ``None`` — the
    caller flags ``name_unmatched`` rather than guessing. Tuning
    beyond this contract (punctuation folding, etc.) is a deferred
    item of the plan.
    """
    if label_name in known_names:
        return label_name
    front = label_name.split(" // ")[0]
    if front != label_name and front in known_names:
        return front
    return None


# ---------------------------------------------------------------------------
# Miss universe (pure core + DB loader)
# ---------------------------------------------------------------------------


def miss_universe_from_labels(
    labels: Mapping[str, float],
    hs_members: Set[str],
    *,
    top_n: int = TOP_N_DEFAULT,
) -> tuple[LabelCard, ...]:
    """Top-``top_n`` label cards by synergy DESC, closed under ties.

    Tie closure: every card whose synergy is >= the ``top_n``-th
    highest value is included, so a synergy tie spanning the boundary
    never arbitrarily drops one tie member. Deterministic ordering:
    synergy descending, then name ascending.
    """
    if top_n <= 0:
        raise ValueError("top_n must be > 0")
    items = sorted(labels.items(), key=lambda kv: (-kv[1], kv[0]))
    if len(items) > top_n:
        threshold = items[top_n - 1][1]
        items = [(name, syn) for name, syn in items if syn >= threshold]
    return tuple(LabelCard(name=name, synergy=syn, hs_section_member=name in hs_members) for name, syn in items)


def load_miss_universe(
    edhrec_conn: sqlite3.Connection,
    commander: str,
    *,
    top_n: int = TOP_N_DEFAULT,
    labels: Mapping[str, float] | None = None,
) -> tuple[LabelCard, ...]:
    """Load the tie-closed top-``top_n`` miss universe for a commander.

    Labels come from the canonical validate-path loader
    (:func:`edhrec_labels_for_commander` with ``grade_floor=0.0``);
    pass ``labels`` to reuse an already-loaded mapping (the Unit-2
    metric sidecars load it once per commander) instead of
    re-querying. HS-section membership is queried separately so every
    miss carries the flag. Returns an empty tuple for zero-label
    commanders (the caller skips them).
    """
    if labels is None:
        labels = edhrec_labels_for_commander(edhrec_conn, commander, grade_floor=0.0)
    if not labels:
        return ()
    slug = commander_to_slug(commander)
    rows = edhrec_conn.execute(
        "SELECT DISTINCT card_name FROM edhrec_card_synergy WHERE commander_slug = ? AND section = ?",
        (slug, HIGH_SYNERGY_SECTION),
    ).fetchall()
    hs_members = {r[0] for r in rows}
    return miss_universe_from_labels(labels, hs_members, top_n=top_n)


def load_nonpositive_listed(
    edhrec_conn: sqlite3.Connection,
    commander: str,
) -> frozenset[str]:
    """Card names listed for ``commander`` at ``MAX(synergy) <= 0``.

    The UNFLOORED membership complement of
    ``edhrec_labels_for_commander(grade_floor=0.0)`` — same query
    shape (``MAX(synergy)`` grouped by card name, all sections) with
    the floor inverted, so the two sets partition the graded
    listings exactly. Names listed only at NULL synergy (ungraded)
    belong to neither set. This is the ONLY new DB read Unit 3 adds,
    and it runs on the existing tags.db connection.
    """
    slug = commander_to_slug(commander)
    rows = edhrec_conn.execute(
        "SELECT card_name, MAX(synergy) AS synergy "
        "FROM edhrec_card_synergy WHERE commander_slug = ? "
        "GROUP BY card_name",
        (slug,),
    ).fetchall()
    return frozenset(row["card_name"] for row in rows if row["synergy"] is not None and float(row["synergy"]) <= 0.0)


# ---------------------------------------------------------------------------
# Live full-ranking pass
# ---------------------------------------------------------------------------


def load_card_meta(conn: sqlite3.Connection) -> dict[str, tuple[float, int]]:
    """Load ``{name: (cmc, edhrec_rank)}`` with engine sentinels applied.

    NULL cmc → :data:`NULL_CMC_SENTINEL` (99.0); NULL edhrec_rank →
    :data:`UNRANKED_EDHREC_SENTINEL`. Loaded once per run and shared
    across every commander's ranking extraction.
    """
    out: dict[str, tuple[float, int]] = {}
    for row in conn.execute("SELECT name, cmc, edhrec_rank FROM cards").fetchall():
        cmc = float(row["cmc"]) if row["cmc"] is not None else NULL_CMC_SENTINEL
        rank = int(row["edhrec_rank"]) if row["edhrec_rank"] is not None else UNRANKED_EDHREC_SENTINEL
        out[row["name"]] = (cmc, rank)
    return out


def extract_live_ranking(
    engine: SynergyEngine,
    commander: str,
    card_meta: Mapping[str, tuple[float, int]],
    *,
    clear_cache: bool = True,
) -> tuple[RankedCandidate, ...]:
    """One full live ranking for ``commander`` via ``engine.page()``.

    ``limit=1_000_000`` returns the entire filtered ranking, so a
    card absent from the result is precisely "dropped by the legality
    chain or never scored" — the FILTERED/DATA_GAP/NO_RULES side of
    the classifier. Captures ``total_score`` from each
    ``Recommendation`` plus ``cmc``/``edhrec_rank`` from
    ``card_meta`` (the cards-table re-read with engine sentinels).

    With ``clear_cache=True`` (default) the engine's score cache is
    cleared after extraction: a shared engine otherwise retains every
    commander's full score dict in one process — a multi-GB exposure
    no existing consumer has. :func:`compute_forensics` passes
    ``clear_cache=False`` so the Unit-2 reconciliation window
    (:func:`extract_canonical_window`) is a cache HIT — never a
    second scoring pass — and clears the cache itself afterwards.
    """
    page = engine.page([commander], offset=0, limit=1_000_000)
    ranking = tuple(
        RankedCandidate(
            name=rec.card,
            rank=rec.rank,
            total_score=rec.total_score,
            cmc=card_meta.get(rec.card, (NULL_CMC_SENTINEL, UNRANKED_EDHREC_SENTINEL))[0],
            edhrec_rank=card_meta.get(rec.card, (NULL_CMC_SENTINEL, UNRANKED_EDHREC_SENTINEL))[1],
        )
        for rec in page.items
    )
    if clear_cache:
        engine._score_cache.clear()
    return ranking


def extract_canonical_window(
    engine: SynergyEngine,
    commander: str,
    *,
    top_n: int = TOP_N_DEFAULT,
) -> tuple[str, ...]:
    """Independent ``engine.page(offset=0, limit=top_n)`` window.

    The Unit-2 reconciliation recompute must use its OWN ``page()``
    invocation — never a slice of the forensics ranking list — so a
    rank-extraction bug in :func:`extract_live_ranking` cannot
    self-validate. On the shared engine this is a cache hit (no
    second scoring pass; R4 holds). Call BEFORE clearing the
    engine's score cache for the commander.
    """
    page = engine.page([commander], offset=0, limit=top_n)
    return tuple(rec.card for rec in page.items)


# ---------------------------------------------------------------------------
# Five-bucket classifier (pure core)
# ---------------------------------------------------------------------------


def _split_pips(color_identity: object) -> frozenset[str]:
    """Parse a ``cards.color_identity`` value the way the engine does."""
    if not isinstance(color_identity, str):
        return frozenset()
    return frozenset(tok.strip() for tok in color_identity.split(",") if tok.strip())


def _first_filter_reason(
    *,
    resolved_name: str | None,
    card_row: Mapping[str, Any] | None,
    commander_identity: frozenset[str],
    commander_names: frozenset[str],
) -> str:
    """First failing predicate of ``page()``'s drop chain, re-evaluated
    from a ``cards`` row in the documented precedence.

    Order (binding): ``color_illegal`` → ``not_legal`` →
    ``empty_types`` → ``non_edh_type`` → ``is_commander``. Empty
    ``card_types`` is checked BEFORE :data:`NON_EDH_CARD_TYPES`
    membership, matching ``engine.page()``'s own drop order
    (``if not cand_types: continue`` comes first). When nothing fails
    the card should have been ranked — ``filter_reason_unknown`` flags
    a stale tensor (rows present for a card the live engine no longer
    scores).
    """
    if card_row is None:
        return REASON_FILTER_UNKNOWN
    cand_pips = _split_pips(card_row.get("color_identity"))
    if cand_pips - commander_identity:
        return REASON_COLOR_ILLEGAL
    if card_row.get("legal_commander") == 0:
        return REASON_NOT_LEGAL
    card_types = str(card_row.get("card_types") or "").split()
    if not card_types:
        return REASON_EMPTY_TYPES
    if any(t in NON_EDH_CARD_TYPES for t in card_types):
        return REASON_NON_EDH_TYPE
    if resolved_name in commander_names:
        return REASON_IS_COMMANDER
    return REASON_FILTER_UNKNOWN


def classify_miss(
    label: LabelCard,
    *,
    commander: str,
    resolved_name: str | None,
    rank: int | None,
    has_tensor_rows: bool,
    card_row: Mapping[str, Any] | None,
    commander_identity: frozenset[str],
    commander_names: frozenset[str],
    port_stats: tuple[int, int] | None,
    top_n: int = TOP_N_DEFAULT,
) -> MissRecord:
    """Classify one miss into exactly one bucket (pure function).

    Precedence (binding): rank 31..60 → NEAR_MISS; rank 61+ →
    OUTRANKED (``staple_only`` sub-tag when no tensor rows); unranked
    with tensor rows → FILTERED (reason from the drop-chain
    re-evaluation); unranked without tensor rows and with broken data
    (absent / portless / majority-UNKNOWN ports) → DATA_GAP; else →
    NO_RULES.

    ``port_stats`` is ``(total_port_nodes_rows, unknown_rows)`` for
    the resolved card, or ``None`` when unavailable.

    Raises ``ValueError`` for ``rank <= top_n`` — label cards inside
    the live top-30 are not misses and must be excluded upstream.
    """
    if rank is not None and rank <= top_n:
        raise ValueError(f"rank {rank} is inside the top-{top_n} window — not a miss; exclude upstream")

    name_unmatched = resolved_name is None

    if rank is not None and rank <= NEAR_MISS_MAX_RANK:
        bucket, reason = BUCKET_NEAR_MISS, None
    elif rank is not None:
        bucket = BUCKET_OUTRANKED
        reason = REASON_STAPLE_ONLY if not has_tensor_rows else None
    elif has_tensor_rows:
        bucket = BUCKET_FILTERED
        reason = _first_filter_reason(
            resolved_name=resolved_name,
            card_row=card_row,
            commander_identity=commander_identity,
            commander_names=commander_names,
        )
    elif resolved_name is None or card_row is None:
        bucket, reason = BUCKET_DATA_GAP, REASON_CARD_ABSENT
    elif port_stats is None or port_stats[0] == 0:
        bucket, reason = BUCKET_DATA_GAP, REASON_NO_PORTS
    elif port_stats[1] / port_stats[0] > UNKNOWN_PORT_SHARE_THRESHOLD:
        bucket, reason = BUCKET_DATA_GAP, REASON_UNKNOWN_PORTS
    else:
        bucket, reason = BUCKET_NO_RULES, None

    return MissRecord(
        commander=commander,
        card_name=label.name,
        synergy=label.synergy,
        bucket=bucket,
        reason=reason,
        rank=rank,
        hs_section_member=label.hs_section_member,
        name_unmatched=name_unmatched,
    )


def classify_commander_misses(
    commander: str,
    miss_universe: Sequence[LabelCard],
    *,
    ranking: Sequence[RankedCandidate] = (),
    known_names: Set[str] = frozenset(),
    tensor_candidates: Set[str] = frozenset(),
    card_rows: Mapping[str, Mapping[str, Any]] | None = None,
    commander_identity: frozenset[str] = frozenset(),
    commander_names: frozenset[str] | None = None,
    port_stats: Mapping[str, tuple[int, int]] | None = None,
    top_n: int = TOP_N_DEFAULT,
) -> CommanderForensics:
    """Classify every miss for one commander (pure orchestration).

    Inputs are plain data: the captured live ranking, the set of
    ``cards.name`` values, the tensor-candidate set for this commander
    at the current config_hash, plain-dict ``cards`` rows, and
    per-card port stats. Label cards whose resolved name sits inside
    the live top-``top_n`` are not misses and are excluded here.

    An empty ``miss_universe`` produces a skipped entry
    (``skip_reason='zero_labels'``) that the aggregator routes to the
    skip list instead of the bucket aggregates.
    """
    card_rows = card_rows or {}
    port_stats = port_stats or {}
    commander_names = commander_names if commander_names is not None else frozenset({commander})

    counts = dict.fromkeys(BUCKETS, 0)
    ranking_tuple = tuple(ranking)
    by_rank = sorted(ranking_tuple, key=lambda rc: rc.rank)
    live_top_30 = tuple(rc.name for rc in by_rank if rc.rank <= top_n)

    if not miss_universe:
        return CommanderForensics(
            commander=commander,
            misses=(),
            bucket_counts=counts,
            live_top_30=live_top_30,
            ranking=ranking_tuple,
            skipped=True,
            skip_reason=SKIP_REASON_ZERO_LABELS,
        )

    rank_by_name = {rc.name: rc.rank for rc in ranking_tuple}
    misses: list[MissRecord] = []
    for label in miss_universe:
        resolved = normalize_label_name(label.name, known_names)
        rank = rank_by_name.get(resolved) if resolved is not None else None
        if rank is not None and rank <= top_n:
            continue  # in our live top-30 → not a miss
        record = classify_miss(
            label,
            commander=commander,
            resolved_name=resolved,
            rank=rank,
            has_tensor_rows=resolved is not None and resolved in tensor_candidates,
            card_row=card_rows.get(resolved) if resolved is not None else None,
            commander_identity=commander_identity,
            commander_names=commander_names,
            port_stats=port_stats.get(resolved) if resolved is not None else None,
            top_n=top_n,
        )
        counts[record.bucket] += 1
        misses.append(record)

    return CommanderForensics(
        commander=commander,
        misses=tuple(misses),
        bucket_counts=counts,
        live_top_30=live_top_30,
        ranking=ranking_tuple,
    )


def aggregate_forensics(entries: Iterable[CommanderForensics]) -> ForensicsReport:
    """Fold per-commander entries into the aggregate report.

    Skipped entries (zero graded labels) land in
    ``skipped_commanders`` and contribute nothing to the aggregate
    bucket counts; zero-miss commanders stay in ``entries`` with
    ``misses=()`` (renderers exclude them from leaderboards).

    ``aggregate_ndcg_canonical`` / ``aggregate_raw_dcg_canonical``
    use the CANONICAL denominator — every fixture commander, with
    skipped (zero-label) commanders contributing 0.0 — matching the
    validate-path golden-set aggregate.
    """
    kept: list[CommanderForensics] = []
    skipped: list[str] = []
    counts = dict.fromkeys(BUCKETS, 0)
    total_misses = 0
    for entry in entries:
        if entry.skipped:
            skipped.append(entry.commander)
            continue
        kept.append(entry)
        for bucket in BUCKETS:
            counts[bucket] += entry.bucket_counts.get(bucket, 0)
        total_misses += len(entry.misses)
    n_canonical = len(kept) + len(skipped)
    aggregate_ndcg = sum(e.ndcg30 for e in kept) / n_canonical if n_canonical else 0.0
    aggregate_raw_dcg = sum(e.raw_dcg30 for e in kept) / n_canonical if n_canonical else 0.0
    return ForensicsReport(
        entries=tuple(kept),
        aggregate_bucket_counts=counts,
        total_misses=total_misses,
        skipped_commanders=tuple(skipped),
        aggregate_ndcg_canonical=aggregate_ndcg,
        aggregate_raw_dcg_canonical=aggregate_raw_dcg,
    )


def bucket_proportions(bucket_counts: Mapping[str, int]) -> dict[str, float] | None:
    """Bucket shares as percentages summing to 100.0.

    Returns ``None`` for a zero-miss count mapping (renderers show
    ``—`` instead of dividing by zero).
    """
    total = sum(bucket_counts.get(b, 0) for b in BUCKETS)
    if total == 0:
        return None
    return {b: 100.0 * bucket_counts.get(b, 0) / total for b in BUCKETS}


# ---------------------------------------------------------------------------
# Justified-divergence view (Unit 3) — pure core
# ---------------------------------------------------------------------------


def _n_rules_bin(n_rules: int) -> str:
    """Stratification bin for a justified pick's N_rules_firing."""
    if n_rules <= 1:
        return "0-1"
    if n_rules >= 5:
        return "5+"
    return str(n_rules)


def _ratio_bin(ratio: float) -> str:
    """Stratification bin for a justified pick's contribution/median ratio."""
    if ratio <= 1.0:
        return "<=1"
    if ratio <= 2.0:
        return "1-2"
    if ratio <= 5.0:
        return "2-5"
    return ">5"


def justified_divergence_for_commander(
    live_top_30: Sequence[str],
    reference_labels: Set[str],
    nonpositive_listed: Set[str],
    contributions: Iterable[tuple[str, str, float]],
) -> JustifiedDivergence:
    """R9 justified-divergence view for one commander (pure function).

    Thin wrapper applying the SAME plausibility-gate constants as
    ``bench/hidden_gems.py`` (``N_rules_firing >= 2`` OR
    ``total_contribution > cohort median``, strict inequality,
    zero-median fallback to the N_rules leg) to the wider
    ALL-SECTIONS reference set. The cohort convention is identical to
    ``hidden_gem_hit_rate_for_commander``: ``contributions`` is the
    commander's FULL tensor-row cohort (``(candidate, rule_id,
    contribution)`` tuples), aggregated once, with the median taken
    over strictly-positive candidate totals. Deliberately does NOT
    reuse ``hidden_gem_hit_rate_for_commander`` — its HS-top-30
    reference answers a different question (module docstring).

    ``reference_labels`` is the floored all-sections label-name set
    (``edhrec_labels_for_commander(grade_floor=0.0)`` keys);
    ``nonpositive_listed`` is the unfloored complement at
    ``MAX(synergy) <= 0`` (:func:`load_nonpositive_listed`). Picks in
    the latter are counted under ``listed_nonpositive`` and are NOT
    divergent. Names listed only at NULL synergy (ungraded) belong to
    neither set and stay divergent.

    Raises ``ValueError`` on duplicate ``live_top_30`` entries (same
    contract as ``hidden_gem_hit_rate_for_commander``).
    """
    if len(set(live_top_30)) != len(live_top_30):
        raise ValueError("live_top_30 must be unique")

    n_rules_map, totals_map = _aggregate_contributions(contributions)
    median = _cohort_median(totals_map)

    listed_nonpositive = sum(1 for pick in live_top_30 if pick not in reference_labels and pick in nonpositive_listed)
    divergent_picks = sorted(
        pick for pick in live_top_30 if pick not in reference_labels and pick not in nonpositive_listed
    )

    justified: list[str] = []
    unjustified: list[str] = []
    n_rules_dist = dict.fromkeys(N_RULES_BIN_LABELS, 0)
    ratio_dist = dict.fromkeys(RATIO_BIN_LABELS, 0)
    for pick in divergent_picks:
        if _plausibility_from_maps(pick, n_rules_map, totals_map, median):
            justified.append(pick)
            n_rules_dist[_n_rules_bin(n_rules_map.get(pick, 0))] += 1
            total = totals_map.get(pick, 0.0)
            ratio = total / median if median > 0 else 0.0
            ratio_dist[_ratio_bin(ratio)] += 1
        else:
            unjustified.append(pick)

    n_divergent = len(divergent_picks)
    return JustifiedDivergence(
        divergent=n_divergent,
        justified_divergences=len(justified),
        unjustified_divergences=len(unjustified),
        gate_pass_rate=len(justified) / n_divergent if n_divergent else None,
        listed_nonpositive=listed_nonpositive,
        justified_cards=tuple(justified),
        unjustified_cards=tuple(unjustified),
        n_rules_distribution=n_rules_dist,
        ratio_distribution=ratio_dist,
    )


# ---------------------------------------------------------------------------
# Metric sidecars (Unit 2) — raw DCG + reconciliation (pure core)
# ---------------------------------------------------------------------------


def compute_raw_dcg(
    predicted: Sequence[str],
    labels: Mapping[str, float],
    *,
    k: int = TOP_N_DEFAULT,
) -> float:
    """Raw (unnormalised) DCG@``k`` over a ranking.

    Mirrors ``validate.compute_ndcg``'s exact conventions — gains
    ``2^rel − 1``, discounts ``log2(i + 2)``, absent label → 0 gain,
    empty ``labels`` or ``k <= 0`` → 0.0 — WITHOUT dividing by the
    ideal DCG, so ``compute_raw_dcg(p, l, k=k) ==
    compute_ndcg(p, l, k=k) * ideal_dcg``. Pure function; always
    >= 0.
    """
    if not labels or k <= 0:
        return 0.0
    pred_scores = [float(labels.get(card, 0.0)) for card in predicted[:k]]
    return sum((math.pow(2.0, rel) - 1.0) / math.log2(i + 2) for i, rel in enumerate(pred_scores) if rel > 0)


def reconcile_canonical_ndcg(
    pairs: Sequence[ReconciliationPair],
    *,
    epsilon: float = RECONCILIATION_EPSILON,
) -> tuple[float, float]:
    """Assert the two canonical NDCG aggregates agree within ``epsilon``.

    Aggregate-to-aggregate comparison over the CANONICAL denominator
    (``pairs`` must contain every fixture commander, zero-label ones
    as ``(0.0, 0.0)``), with UNROUNDED values on both sides. On
    mismatch raises :class:`ForensicsReconciliationError` naming the
    FIRST divergent commander (fixture order; falls back to the
    largest per-commander divergence if no single pair exceeds
    ``epsilon``). Returns ``(forensics_aggregate,
    recompute_aggregate)`` when reconciled.
    """
    if not pairs:
        return (0.0, 0.0)
    n = len(pairs)
    agg_forensics = sum(p.forensics_ndcg for p in pairs) / n
    agg_recompute = sum(p.recompute_ndcg for p in pairs) / n
    if abs(agg_forensics - agg_recompute) <= epsilon:
        return (agg_forensics, agg_recompute)
    divergent = next(
        (p for p in pairs if abs(p.forensics_ndcg - p.recompute_ndcg) > epsilon),
        max(pairs, key=lambda p: abs(p.forensics_ndcg - p.recompute_ndcg)),
    )
    raise ForensicsReconciliationError(
        f"canonical NDCG reconciliation failed: forensics aggregate {agg_forensics!r} vs "
        f"independent page(limit=top_n) recompute {agg_recompute!r} differ by more than "
        f"{epsilon}; first divergent commander: {divergent.commander!r} "
        f"(forensics {divergent.forensics_ndcg!r} vs recompute {divergent.recompute_ndcg!r}). "
        "Nothing was written."
    )


# ---------------------------------------------------------------------------
# Tiebreaker ablation (Unit 6, R8) — pure functions over the Unit-1 capture
# ---------------------------------------------------------------------------


class TiebreakSelfCheckError(ForensicsReconciliationError):
    """The mandatory production-sort-key reconstruction self-check failed.

    Re-sorting a commander's captured :class:`RankedCandidate` tuples
    by the reconstructed production key ``(-total_score, cmc,
    edhrec_rank, name)`` must reproduce the captured rank order
    EXACTLY (``bench/optimize.py::score_commander_from_complements``
    is the sanctioned reconstruction precedent). A mismatch means
    sentinel/NULL drift between the capture and ``engine.page()`` —
    no ablation deltas may be reported. Subclasses
    :class:`ForensicsReconciliationError` so the Unit-4 handler's
    exit-2 mapping (nothing written) applies unchanged.
    """


@dataclass(frozen=True)
class TiebreakAblation:
    """Aggregate R8 ablation result over the canonical denominator.

    All NDCG figures are canonical-denominator means (every fixture
    commander; zero-label / skipped commanders contribute 0.0 on
    every key, so they never move a delta). Deltas are signed in the
    "production minus replacement" direction: a POSITIVE delta means
    the production key (with ``edhrec_rank``) scores higher than the
    replacement — i.e. credit that vanishes when the EDHREC
    tiebreaker is removed.
    """

    #: Canonical denominator (classified + skipped fixture commanders).
    n_commanders: int
    production_ndcg: float
    #: Weak replacement key ``(-total_score, name)``.
    weak_ndcg: float
    #: Strong replacement key ``(-total_score, cmc, name)``.
    strong_ndcg: float
    #: ``production_ndcg - weak_ndcg`` (signed).
    weak_delta: float
    #: ``production_ndcg - strong_ndcg`` (signed).
    strong_delta: float
    #: ISO date stamped into the RULE_HISTORY flag entry (injected by
    #: the caller so this dataclass stays a pure function of inputs).
    run_date: str

    @property
    def delta_range(self) -> tuple[float, float]:
        """The bracketed (min, max) of the two signed deltas."""
        lo, hi = sorted((self.weak_delta, self.strong_delta))
        return (lo, hi)

    @property
    def upper_bound(self) -> float:
        """Upper bound of unearned EDHREC tiebreak credit: the larger
        NDCG drop when ``edhrec_rank`` is removed from the key."""
        return max(self.weak_delta, self.strong_delta)

    @property
    def flagged(self) -> bool:
        """True when the upper bound exceeds :data:`TIEBREAK_FLAG_THRESHOLD`."""
        return self.upper_bound > TIEBREAK_FLAG_THRESHOLD

    @property
    def rule_history_markdown(self) -> str | None:
        """Ready-to-paste RULE_HISTORY entry when :attr:`flagged`.

        ``None`` below the threshold. The HUMAN commits the entry —
        the forensics run itself stays read-only (plan Unit 6).
        """
        if not self.flagged:
            return None
        lo, hi = self.delta_range
        return (
            f"## {self.run_date} — EDHREC tiebreaker credit measured (R8 ablation): "
            "sort-key remediation flagged\n"
            "\n"
            "`bench.py audit --forensics --ablate-tiebreak` re-sorted the captured\n"
            "production rankings under bracketing replacement keys (pure re-sort of\n"
            "the Unit-1 capture; no scoring change, no `engine.page()` change):\n"
            "\n"
            f"- weak key `(-total_score, name)`: delta {self.weak_delta:+.6f}\n"
            f"- strong key `(-total_score, cmc, name)`: delta {self.strong_delta:+.6f}\n"
            f"- bracketed credit range: [{lo:+.6f}, {hi:+.6f}] "
            f"(NDCG@30, canonical denominator, {self.n_commanders} commanders)\n"
            "\n"
            f"Upper bound {self.upper_bound:+.6f} exceeds the "
            f"{TIEBREAK_FLAG_THRESHOLD} threshold — sort-key remediation is a\n"
            "next-cycle candidate (needs its own plan; the production sort key is\n"
            f"unchanged by this measurement). Plan: {FORENSICS_PLAN_PATH}."
        )


def weak_sort_key(candidate: RankedCandidate) -> tuple[float, str]:
    """Weak replacement key ``(-total_score, name)`` (R8 lower bracket)."""
    return (-candidate.total_score, candidate.name)


def strong_sort_key(candidate: RankedCandidate) -> tuple[float, float, str]:
    """Strong replacement key ``(-total_score, cmc, name)`` (R8 upper bracket)."""
    return (-candidate.total_score, candidate.cmc, candidate.name)


def verify_production_sort(
    commander: str,
    ranking: Sequence[RankedCandidate],
) -> tuple[RankedCandidate, ...]:
    """Mandatory R8 self-check: the reconstructed production key must
    reproduce the captured rank order EXACTLY.

    Re-sorts the captured candidates by
    :attr:`RankedCandidate.production_sort_key` and compares against
    the capture ordered by its ``rank`` field. Any mismatch raises
    :class:`TiebreakSelfCheckError` naming the commander and the first
    divergent position — sentinel/NULL drift between the Unit-1
    capture and ``engine.page()`` invalidates every delta, so callers
    must report NO deltas. Returns the captured rank order on success.
    """
    captured = tuple(sorted(ranking, key=lambda rc: rc.rank))
    reconstructed = tuple(sorted(ranking, key=lambda rc: rc.production_sort_key))
    for position, (cap, rec) in enumerate(zip(captured, reconstructed, strict=True), start=1):
        if cap.name != rec.name:
            raise TiebreakSelfCheckError(
                f"tiebreak-ablation self-check failed for {commander!r}: re-sorting the "
                f"captured candidates by the reconstructed production key (-total_score, "
                f"cmc, edhrec_rank, name) diverges from the captured rank order at "
                f"position {position} (captured {cap.name!r}, reconstructed {rec.name!r}). "
                "Sentinel/NULL drift between the capture and engine.page() — no ablation "
                "deltas reported. Nothing was written."
            )
    return captured


def compute_tiebreak_ablation(
    rankings: Mapping[str, Sequence[RankedCandidate]],
    labels_by_commander: Mapping[str, Mapping[str, float]],
    *,
    n_canonical: int,
    run_date: str,
    top_n: int = TOP_N_DEFAULT,
) -> TiebreakAblation:
    """R8 tiebreaker ablation over the captured rankings (pure function).

    ``rankings`` maps each non-skipped commander to its Unit-1
    captured full ranking; ``labels_by_commander`` carries the same
    canonical validate-path labels (``grade_floor=0.0``) the forensics
    run already loaded. ``n_canonical`` is the canonical denominator
    (ALL fixture commanders — commanders absent from ``rankings``,
    i.e. zero-label skips, contribute exactly 0.0 on every key).

    The mandatory self-check (:func:`verify_production_sort`) runs
    over EVERY commander BEFORE any NDCG is computed; a single
    mismatch raises :class:`TiebreakSelfCheckError` and no deltas
    exist. With no score ties anywhere, all three sorts coincide and
    both deltas are exactly 0.0.

    Raises ``ValueError`` when ``n_canonical`` is smaller than the
    number of supplied rankings (the canonical denominator can never
    shrink below the classified set).
    """
    if n_canonical < len(rankings):
        raise ValueError(f"n_canonical ({n_canonical}) < number of rankings ({len(rankings)})")

    # Self-check EVERY commander first — no deltas on any mismatch.
    captured_by_commander = {
        commander: verify_production_sort(commander, rankings[commander]) for commander in sorted(rankings)
    }

    sum_production = sum_weak = sum_strong = 0.0
    for commander in sorted(rankings):
        captured = captured_by_commander[commander]
        labels = dict(labels_by_commander.get(commander, {}))
        production_window = [rc.name for rc in captured[:top_n]]
        weak_window = [rc.name for rc in sorted(captured, key=weak_sort_key)[:top_n]]
        strong_window = [rc.name for rc in sorted(captured, key=strong_sort_key)[:top_n]]
        sum_production += compute_ndcg(production_window, labels, k=top_n)
        sum_weak += compute_ndcg(weak_window, labels, k=top_n)
        sum_strong += compute_ndcg(strong_window, labels, k=top_n)

    if n_canonical:
        agg_production = sum_production / n_canonical
        agg_weak = sum_weak / n_canonical
        agg_strong = sum_strong / n_canonical
    else:
        agg_production = agg_weak = agg_strong = 0.0

    return TiebreakAblation(
        n_commanders=n_canonical,
        production_ndcg=agg_production,
        weak_ndcg=agg_weak,
        strong_ndcg=agg_strong,
        weak_delta=agg_production - agg_weak,
        strong_delta=agg_production - agg_strong,
        run_date=run_date,
    )


# ---------------------------------------------------------------------------
# rank_bonus ablation (Task 1 of plan 2026-07-07-002) — standing sidecar,
# computed on every ``--forensics`` run over the Unit-1 capture
# ---------------------------------------------------------------------------

#: The exact in-score ``rank_bonus`` weight from ``universal_scorer.py``
#: (both call sites, scored + staple-only).
RANK_BONUS_WEIGHT: float = 0.005

#: The exact in-score ``rank_bonus`` rank divisor from ``universal_scorer.py``.
RANK_BONUS_RANK_DIVISOR: float = 30000.0

#: ``universal_scorer.py``'s LOCAL fallback for a candidate absent from
#: its ``rank_data`` mapping (``rank_data.get(name, 99999)`` at both
#: rank_bonus call sites) — distinct from
#: :data:`engine.UNRANKED_EDHREC_SENTINEL` (10**9), the sentinel
#: :func:`load_card_meta` stamps onto a captured
#: :class:`RankedCandidate`. Both sentinels clamp the bonus to 0.0 (a
#: rank this large divided by :data:`RANK_BONUS_RANK_DIVISOR` exceeds
#: 1.0, so the ``max(0.0, ...)`` floor bites either way) — the cap
#: below is documentation of the scorer's actual constant, not a
#: behavior change; it exists so this module's formula matches
#: ``universal_scorer.py`` literally rather than merely producing the
#: same clamped result by coincidence.
RANK_BONUS_SENTINEL_CAP: int = 99999


def rank_bonus_for(edhrec_rank: int) -> float:
    """The exact in-score ``rank_bonus`` for a captured ``edhrec_rank``.

    Mirrors ``universal_scorer.py``'s formula bit-for-bit:
    ``0.005 * max(0.0, 1.0 - min(edhrec_rank, 99999) / 30000.0)``.
    """
    capped = min(edhrec_rank, RANK_BONUS_SENTINEL_CAP)
    return RANK_BONUS_WEIGHT * max(0.0, 1.0 - capped / RANK_BONUS_RANK_DIVISOR)


def rank_bonus_ablated_sort_key(candidate: RankedCandidate) -> tuple[float, float, str]:
    """Ablated re-sort key: subtract :func:`rank_bonus_for` from the
    captured ``total_score`` and DROP ``edhrec_rank`` from the key
    entirely.

    Per Task 1 of plan 2026-07-07-002 this sidecar measures TOTAL
    EDHREC-at-inference credit (in-score bonus + in-sort tiebreak) —
    an upper bound of either ablation alone, and the exact methodology
    behind the 2026-07-07 diagnostic's measured -0.0441 golden-100
    delta. Contrast :func:`strong_sort_key` (R8's tiebreaker-ONLY
    ablation), which leaves the bonus IN ``total_score`` and only
    swaps the replacement sort key.
    """
    adj_total = candidate.total_score - rank_bonus_for(candidate.edhrec_rank)
    return (-adj_total, candidate.cmc, candidate.name)


@dataclass(frozen=True)
class RankBonusAblation:
    """Task 1 (plan 2026-07-07-002) rank_bonus-ablation result.

    Canonical-denominator NDCG@30 means (every fixture commander;
    zero-label/skipped commanders contribute 0.0 to both sides — same
    convention as :class:`TiebreakAblation`).

    ``delta`` is ``ndcg_ablated - ndcg_raw`` — NEGATIVE when the
    rank_bonus is providing unearned credit (matches the diagnostic's
    reported "-0.0441" sign convention exactly; contrast
    :class:`TiebreakAblation`'s production-minus-replacement,
    positive-when-earned convention).
    """

    n_commanders: int
    ndcg_raw: float
    ndcg_ablated: float
    delta: float


def compute_rank_bonus_ablation(
    rankings: Mapping[str, Sequence[RankedCandidate]],
    labels_by_commander: Mapping[str, Mapping[str, float]],
    *,
    n_canonical: int,
    top_n: int = TOP_N_DEFAULT,
) -> RankBonusAblation:
    """Task 1 (plan 2026-07-07-002): total EDHREC-at-inference credit.

    Mirrors :func:`compute_tiebreak_ablation`'s structure and label
    plumbing, but ablates the IN-SCORE ``rank_bonus`` term as well as
    the sort-key tiebreak (see :func:`rank_bonus_ablated_sort_key`) —
    a different, larger measurement than R8's tiebreaker-only
    ablation. Computed on EVERY ``--forensics`` run (no flag): cheap
    re-sort arithmetic over the already-captured Unit-1 rankings, no
    extra scoring pass, ``engine.page()`` untouched.

    ``rankings`` maps each non-skipped commander to its Unit-1
    captured full ranking — already in production rank order, so the
    "raw" window is read directly off ``rc.rank`` with no self-check
    (unlike R8, this sidecar never reconstructs the production sort
    key: only the ABLATED key is a re-sort). ``labels_by_commander``
    carries the same canonical validate-path labels
    (``grade_floor=0.0``) the forensics run already loaded.
    ``n_canonical`` is the canonical denominator (every fixture
    commander; commanders absent from ``rankings`` — zero-label skips
    — contribute 0.0 on both sides).

    Raises ``ValueError`` when ``n_canonical`` is smaller than the
    number of supplied rankings (the canonical denominator can never
    shrink below the classified set).
    """
    if n_canonical < len(rankings):
        raise ValueError(f"n_canonical ({n_canonical}) < number of rankings ({len(rankings)})")

    sum_raw = sum_ablated = 0.0
    for commander in sorted(rankings):
        candidates = tuple(rankings[commander])
        labels = dict(labels_by_commander.get(commander, {}))
        raw_window = [rc.name for rc in sorted(candidates, key=lambda rc: rc.rank)[:top_n]]
        ablated_window = [rc.name for rc in sorted(candidates, key=rank_bonus_ablated_sort_key)[:top_n]]
        sum_raw += compute_ndcg(raw_window, labels, k=top_n)
        sum_ablated += compute_ndcg(ablated_window, labels, k=top_n)

    if n_canonical:
        ndcg_raw = sum_raw / n_canonical
        ndcg_ablated = sum_ablated / n_canonical
    else:
        ndcg_raw = ndcg_ablated = 0.0

    return RankBonusAblation(
        n_commanders=n_canonical,
        ndcg_raw=ndcg_raw,
        ndcg_ablated=ndcg_ablated,
        delta=ndcg_ablated - ndcg_raw,
    )


# ---------------------------------------------------------------------------
# DB loaders (synergy.db side)
# ---------------------------------------------------------------------------


def load_tensor_candidates(
    conn: sqlite3.Connection,
    commander: str,
    config_hash: str,
) -> frozenset[str]:
    """Distinct tensor candidates for ``commander`` at ``config_hash``.

    Hash-filtered like every tensor read (``bench/rule_ops.py``
    style) so stale rows are never silently consumed.
    """
    rows = conn.execute(
        "SELECT DISTINCT candidate FROM rule_contributions WHERE commander = ? AND config_hash = ?",
        (commander, config_hash),
    ).fetchall()
    return frozenset(r[0] for r in rows)


def load_tensor_contributions(
    conn: sqlite3.Connection,
    commander: str,
    config_hash: str,
) -> tuple[tuple[str, str, float], ...]:
    """Full tensor-row cohort for ``commander`` at ``config_hash``.

    ``(candidate, rule_id, contribution)`` tuples in the shape the
    ``hidden_gems`` gate consumes — the Unit-3 justified-divergence
    cohort. Hash-filtered like every tensor read; deterministic
    ordering (aggregation is order-insensitive, but repeat-run
    equality of the report must hold bitwise).

    ``compute_forensics`` derives the Unit-1 tensor-candidate set
    from these rows (one tensor read per commander, not two).
    """
    rows = conn.execute(
        "SELECT candidate, rule_id, contribution FROM rule_contributions "
        "WHERE commander = ? AND config_hash = ? ORDER BY candidate, rule_id, contribution",
        (commander, config_hash),
    ).fetchall()
    return tuple((str(r[0]), str(r[1]), float(r[2])) for r in rows)


def load_card_rows(
    conn: sqlite3.Connection,
    names: Sequence[str],
) -> dict[str, dict[str, Any]]:
    """Plain-dict ``cards`` rows for ``names`` (drop-chain predicates)."""
    if not names:
        return {}
    # Established IN-list expansion pattern (penalties.py / optimize.py):
    # only ? placeholder counts are interpolated; params are always bound.
    rows = conn.execute(
        "SELECT name, color_identity, card_types, legal_commander "  # noqa: S608 — placeholders only
        "FROM cards WHERE name IN ({})".format(",".join("?" * len(names))),
        tuple(names),
    ).fetchall()
    return {r["name"]: dict(r) for r in rows}


def load_port_stats(
    conn: sqlite3.Connection,
    names: Sequence[str],
) -> dict[str, tuple[int, int]]:
    """Per-card ``(total_port_nodes_rows, unknown_rows)`` over the
    ``port_nodes`` view (auto-created by ``open_db``)."""
    if not names:
        return {}
    # Same IN-list expansion as load_card_rows; params are always bound.
    rows = conn.execute(
        "SELECT card_name, COUNT(*) AS n_ports, "  # noqa: S608 — placeholders only
        "SUM(CASE WHEN node_kind = 'UNKNOWN' THEN 1 ELSE 0 END) AS n_unknown "
        "FROM port_nodes WHERE card_name IN ({}) GROUP BY card_name".format(",".join("?" * len(names))),
        tuple(names),
    ).fetchall()
    return {r["card_name"]: (int(r["n_ports"]), int(r["n_unknown"] or 0)) for r in rows}


#: Tables :func:`synergy_content_digest` may interpolate into its SQL.
#: Project convention: SQL fragment interpolation guarded by frozensets
#: + ``ValueError`` (never ``assert`` — stripped by ``python -O``).
_VALID_DIGEST_TABLES: frozenset[str] = frozenset({"cards", "card_ports"})


def synergy_content_digest(
    conn: sqlite3.Connection,
    tables: Sequence[str] = ("cards", "card_ports"),
) -> str:
    """Cheap content digest over ``cards`` / ``card_ports``.

    SHA-256 over ``(COUNT(*), MAX(rowid))`` per table — mirrors the
    EDHREC snapshot digest mechanism: stable, cheap, sufficient to
    detect a cardsfolder re-import (which ``compute_config_hash``
    deliberately does NOT cover). Unit 5 records it as a provenance
    column. ``tables`` must be a subset of
    :data:`_VALID_DIGEST_TABLES`; anything else raises ``ValueError``
    before any SQL interpolation.
    """
    h = hashlib.sha256()
    for table in tables:
        if table not in _VALID_DIGEST_TABLES:
            raise ValueError(f"invalid digest table {table!r}; allowed: {sorted(_VALID_DIGEST_TABLES)}")
        row = conn.execute(f"SELECT COUNT(*), MAX(rowid) FROM {table}").fetchone()  # noqa: S608 — guarded above
        h.update(f"{table}:{row[0]}:{row[1]}|".encode())
    return h.hexdigest()


def emit_freshness_advisory(
    conn: sqlite3.Connection,
    *,
    expected_digest: str | None = None,
) -> str:
    """Advisory-only synergy.db freshness check (never hard-fails).

    With no recorded digest to cross-check against (the v1 tensor
    writer records none), emits a stderr warning saying so; with a
    recorded digest, warns on mismatch. Returns the live digest so
    callers can record it as provenance.
    """
    digest = synergy_content_digest(conn)
    if expected_digest is None:
        print(
            "warning: synergy.db freshness cannot be cross-checked (no content digest "
            "recorded at tensor-write time); forensics assumes the tensor matches the live DB.",
            file=sys.stderr,
        )
    elif digest != expected_digest:
        print(
            f"warning: synergy.db content digest {digest[:12]}... differs from recorded "
            f"{expected_digest[:12]}... — the tensor may be stale relative to a cardsfolder re-import.",
            file=sys.stderr,
        )
    return digest


# ---------------------------------------------------------------------------
# Preconditions + fixture commander list
# ---------------------------------------------------------------------------


def load_fixture_commanders(fixture_path: Path | str) -> list[str]:
    """Commander list from the pinned fixture (single-commander v1).

    Follows ``handlers._load_commanders_from_fixture``, plus the v1
    partner guard: any non-string ``commander`` entry (a partner
    pair) raises :class:`ForensicsPreconditionError` — partners are
    out of scope for forensics v1 and must fail loud, not misclassify.
    Duplicate commander names also raise — duplicates would silently
    double-count every aggregate (bucket sums, canonical denominator).
    """
    pinned = PinnedFixture.load(Path(fixture_path))
    commanders: list[str] = []
    seen: set[str] = set()
    for entry in pinned.entries:
        if not isinstance(entry.commander, str):
            raise ForensicsPreconditionError(
                f"fixture entry {entry.commander!r} is a multi-commander entry: partners not supported in v1"
            )
        if entry.commander in seen:
            raise ForensicsPreconditionError(
                f"fixture entry {entry.commander!r} appears more than once: duplicate "
                "commanders would silently double-count aggregate denominators"
            )
        seen.add(entry.commander)
        commanders.append(entry.commander)
    return commanders


def _check_tensor_populated(conn: sqlite3.Connection, config_hash: str) -> None:
    """Raise unless the tensor has rows at the current config_hash."""
    row = conn.execute(
        "SELECT COUNT(*) FROM rule_contributions WHERE config_hash = ?",
        (config_hash,),
    ).fetchone()
    if int(row[0]) == 0:
        raise ForensicsPreconditionError(
            f"rule_contributions has no rows at the current config_hash "
            f"{config_hash[:12]}... — the persisted tensor is stale or absent. "
            "Re-pin via `bench.py audit --repin --yes` before running forensics."
        )


# ---------------------------------------------------------------------------
# Sampled independent-engine check (Unit 2 production-faithfulness evidence)
# ---------------------------------------------------------------------------


def run_independent_engine_check(
    report: ForensicsReport,
    *,
    db_path: Path | str,
    tags_path: Path | str,
    sample_size: int = INDEPENDENT_CHECK_SAMPLE_SIZE,
    top_n: int = TOP_N_DEFAULT,
    epsilon: float = RECONCILIATION_EPSILON,
) -> tuple[str, ...]:
    """Re-score sampled commanders on a SEPARATE engine and compare NDCG.

    The shared-engine reconciliation is a bookkeeping-consistency
    check; this is the production-faithfulness evidence: the first
    ``sample_size`` non-skipped commanders alphabetically (a
    deterministic sample) are re-scored on a separately constructed
    :class:`SynergyEngine` (own connection, mirroring
    ``validate._run_one``'s canonical pass without its 6-dp
    rounding), and each per-commander NDCG must agree with the
    report's UNROUNDED value within ``epsilon``. Disagreement raises
    :class:`ForensicsReconciliationError` naming the commander.

    Runs by default inside :func:`compute_forensics`
    (``independent_check=False`` to skip — e.g. tests whose
    synthetic DB makes it redundant); the Unit-4 handler can also
    call it directly. Returns the sampled commander names.
    """
    tags_path = Path(tags_path)
    if not tags_path.exists():
        raise ForensicsPreconditionError(
            f"EDHREC DB {tags_path} not found — required for the independent-engine check."
        )
    sampled = tuple(sorted(e.commander for e in report.entries)[:sample_size])
    if not sampled:
        return ()
    by_name = {e.commander: e for e in report.entries}
    try:
        engine = SynergyEngine(Path(db_path))
    except FileNotFoundError as exc:
        raise ForensicsPreconditionError(str(exc)) from exc
    try:
        edhrec_conn = sqlite3.connect(tags_path)
        edhrec_conn.row_factory = sqlite3.Row
        try:
            for commander in sampled:
                labels = edhrec_labels_for_commander(edhrec_conn, commander, grade_floor=0.0)
                window = [rec.card for rec in engine.page([commander], offset=0, limit=top_n).items]
                independent = compute_ndcg(window, labels, k=top_n)
                recorded = by_name[commander].ndcg30
                if abs(independent - recorded) > epsilon:
                    raise ForensicsReconciliationError(
                        f"independent-engine NDCG check failed for {commander!r}: forensics "
                        f"recorded {recorded!r} but a separately constructed SynergyEngine "
                        f"computed {independent!r} (epsilon {epsilon}). Nothing was written."
                    )
        finally:
            edhrec_conn.close()
    finally:
        engine.close()
    return sampled


# ---------------------------------------------------------------------------
# DB-shell compute function (two-connection pattern, NOT ATTACH)
# ---------------------------------------------------------------------------


def compute_forensics(
    *,
    db_path: Path | str,
    tags_path: Path | str,
    fixture_path: Path | str,
    top_n: int = TOP_N_DEFAULT,
    independent_check: bool = True,
) -> ForensicsReport:
    """Run the full forensics pass (Units 1–3) over the pinned fixture.

    Two-connection pattern: ``open_db(db_path, create=False)`` for
    synergy.db (gives the ``port_nodes`` view; refuses to materialize
    a missing DB) plus ``sqlite3.connect(tags_path)`` with
    ``row_factory = sqlite3.Row`` for tags.db. One shared
    :class:`SynergyEngine` supplies the live rankings.

    Per-commander order (load-bearing): ``page(limit=1_000_000)`` →
    extract the forensics ranking; ``page(limit=top_n)`` → the
    independent reconciliation window (cache hit — no second scoring
    pass); THEN clear the engine's score cache. Metric sidecars
    (``ndcg30``, ``raw_dcg30``) are computed from the same pass with
    canonical validate-path labels and stored UNROUNDED.

    After the loop the canonical aggregates must reconcile within
    :data:`RECONCILIATION_EPSILON` (:func:`reconcile_canonical_ndcg`,
    raising :class:`ForensicsReconciliationError` on a planted-bug
    mismatch) and, unless ``independent_check=False``, the sampled
    independent-engine check runs. Nothing is written by this
    function, so a raise means nothing was written.

    All preconditions are checked before anything is computed; any
    failure raises :class:`ForensicsPreconditionError` (the future
    handler's exit-2 path). The synergy.db freshness check is
    advisory only (stderr warning).
    """
    db_path = Path(db_path)
    tags_path = Path(tags_path)
    fixture_path = Path(fixture_path)

    if not fixture_path.exists():
        raise ForensicsPreconditionError(
            f"pinned fixture {fixture_path} not found. Run `bench.py audit --repin --yes` to create one."
        )
    commanders = load_fixture_commanders(fixture_path)
    if not commanders:
        raise ForensicsPreconditionError(f"fixture {fixture_path} has no entries.")
    if not tags_path.exists():
        raise ForensicsPreconditionError(
            f"EDHREC DB {tags_path} not found — required for forensics labels. "
            "Rebuild it from the EDHREC snapshot pipeline before running forensics."
        )

    try:
        conn = open_db(db_path, create=False)
    except FileNotFoundError as exc:
        raise ForensicsPreconditionError(str(exc)) from exc

    try:
        config_hash = compute_config_hash()
        _check_tensor_populated(conn, config_hash)
        emit_freshness_advisory(conn)

        edhrec_conn = sqlite3.connect(tags_path)
        edhrec_conn.row_factory = sqlite3.Row
        try:
            engine = SynergyEngine(db_path)
            try:
                card_meta = load_card_meta(conn)
                known_names = frozenset(card_meta)
                entries: list[CommanderForensics] = []
                recon_pairs: list[ReconciliationPair] = []
                for commander in commanders:
                    labels = edhrec_labels_for_commander(edhrec_conn, commander, grade_floor=0.0)
                    miss_universe = load_miss_universe(edhrec_conn, commander, top_n=top_n, labels=labels)
                    if not miss_universe:
                        entries.append(classify_commander_misses(commander, (), top_n=top_n))
                        # Canonical denominator: zero-label commanders
                        # contribute 0.0 on both sides.
                        recon_pairs.append(ReconciliationPair(commander, 0.0, 0.0))
                        continue

                    # Load-bearing order: full ranking first, then the
                    # independent reconciliation window (cache hit),
                    # then clear the cache for this commander.
                    ranking = extract_live_ranking(engine, commander, card_meta, clear_cache=False)
                    window = extract_canonical_window(engine, commander, top_n=top_n)
                    engine._score_cache.clear()

                    ranking_names = [rc.name for rc in ranking]
                    labels_dict = dict(labels)
                    ndcg30 = compute_ndcg(ranking_names, labels_dict, k=top_n)
                    raw_dcg30 = compute_raw_dcg(ranking_names, labels_dict, k=top_n)
                    recompute_ndcg = compute_ndcg(list(window), labels_dict, k=top_n)
                    recon_pairs.append(ReconciliationPair(commander, ndcg30, recompute_ndcg))

                    # One tensor read per commander: the full cohort
                    # feeds the Unit-3 gate; the candidate set the
                    # Unit-1 classifier needs is derived from it.
                    tensor_rows = load_tensor_contributions(conn, commander, config_hash)
                    tensor_candidates = frozenset(cand for cand, _rule_id, _value in tensor_rows)
                    resolved = {
                        name
                        for name in (normalize_label_name(label.name, known_names) for label in miss_universe)
                        if name is not None
                    }
                    lookup_names = sorted(resolved | {commander})
                    card_rows = load_card_rows(conn, lookup_names)
                    port_stats = load_port_stats(conn, lookup_names)
                    cmdr_row = card_rows.get(commander)
                    if cmdr_row is None:
                        # Advisory only: classification proceeds with an
                        # empty commander identity (every colored card
                        # then re-evaluates as color_illegal).
                        print(
                            f"warning: commander {commander!r} has no cards row — commander "
                            "color identity falls back to empty; classification proceeds.",
                            file=sys.stderr,
                        )
                    commander_identity = _split_pips(cmdr_row.get("color_identity") if cmdr_row else None)

                    entry = classify_commander_misses(
                        commander,
                        miss_universe,
                        ranking=ranking,
                        known_names=known_names,
                        tensor_candidates=tensor_candidates,
                        card_rows=card_rows,
                        commander_identity=commander_identity,
                        commander_names=frozenset({commander}),
                        port_stats=port_stats,
                        top_n=top_n,
                    )
                    # Unit 3 (R9): justified-divergence view over the
                    # ALL-SECTIONS reference set, from already-loaded
                    # data (live top-30, tensor cohort, labels) plus
                    # the one allowed new read on the existing
                    # tags.db connection (unfloored membership).
                    nonpositive_listed = load_nonpositive_listed(edhrec_conn, commander)
                    justified = justified_divergence_for_commander(
                        entry.live_top_30,
                        frozenset(labels_dict),
                        nonpositive_listed,
                        tensor_rows,
                    )
                    entries.append(replace(entry, ndcg30=ndcg30, raw_dcg30=raw_dcg30, justified_divergence=justified))

                # Reconciliation BEFORE the report is handed back:
                # callers write nothing when this raises.
                reconcile_canonical_ndcg(recon_pairs)
                report = aggregate_forensics(entries)
                if independent_check:
                    run_independent_engine_check(
                        report,
                        db_path=db_path,
                        tags_path=tags_path,
                        top_n=top_n,
                    )
                return report
            finally:
                engine.close()
        finally:
            edhrec_conn.close()
    finally:
        conn.close()
