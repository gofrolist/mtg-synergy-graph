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
  ``non_edh_type`` / ``empty_types`` / ``is_commander``); when no
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

Read-only diagnostic: nothing here mutates either database or the
pinned fixture. CLI wiring, renderers, metric sidecars, history CSV,
and the tiebreaker ablation are later units of the same plan.
"""

from __future__ import annotations

import hashlib
import sqlite3
import sys
from collections.abc import Iterable, Mapping, Sequence, Set
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from mtg_synergy_graph.bench.fixture import PinnedFixture
from mtg_synergy_graph.bench.tensor import compute_config_hash
from mtg_synergy_graph.db import open_db
from mtg_synergy_graph.edhrec_helpers import HIGH_SYNERGY_SECTION
from mtg_synergy_graph.engine import (
    NON_EDH_CARD_TYPES,
    UNRANKED_EDHREC_SENTINEL,
    SynergyEngine,
)
from mtg_synergy_graph.validate import commander_to_slug, edhrec_labels_for_commander

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
REASON_NON_EDH_TYPE = "non_edh_type"
REASON_EMPTY_TYPES = "empty_types"
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


class ForensicsPreconditionError(Exception):
    """A forensics precondition failed — nothing was computed.

    The future ``--forensics`` handler (Unit 4) maps this to exit
    code 2 (usage / stale input), matching the strict-consumer
    failure mode of ``bench/optimize.py`` and the offline-oracle hash
    pattern: no partial report, no history row.
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
    #: True when the commander was skipped (zero graded labels) and
    #: must be excluded from bucket aggregates.
    skipped: bool = False
    skip_reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "bucket_counts", MappingProxyType(dict(self.bucket_counts)))


@dataclass(frozen=True)
class ForensicsReport:
    """Aggregate forensics across the fixture's commanders.

    ``entries`` holds only non-skipped commanders; skipped ones
    (zero graded labels) are listed in ``skipped_commanders`` and
    contribute nothing to ``aggregate_bucket_counts``.
    """

    entries: tuple[CommanderForensics, ...]
    aggregate_bucket_counts: Mapping[str, int]
    total_misses: int
    skipped_commanders: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "aggregate_bucket_counts",
            MappingProxyType(dict(self.aggregate_bucket_counts)),
        )


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
) -> tuple[LabelCard, ...]:
    """Load the tie-closed top-``top_n`` miss universe for a commander.

    Labels come from the canonical validate-path loader
    (:func:`edhrec_labels_for_commander` with ``grade_floor=0.0``);
    HS-section membership is queried separately so every miss carries
    the flag. Returns an empty tuple for zero-label commanders (the
    caller skips them).
    """
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
) -> tuple[RankedCandidate, ...]:
    """One full live ranking for ``commander`` via ``engine.page()``.

    ``limit=1_000_000`` returns the entire filtered ranking, so a
    card absent from the result is precisely "dropped by the legality
    chain or never scored" — the FILTERED/DATA_GAP/NO_RULES side of
    the classifier. Captures ``total_score`` from each
    ``Recommendation`` plus ``cmc``/``edhrec_rank`` from
    ``card_meta`` (the cards-table re-read with engine sentinels).

    The engine's score cache is cleared after extraction: a shared
    engine otherwise retains every commander's full score dict in one
    process — a multi-GB exposure no existing consumer has.
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
    engine._score_cache.clear()
    return ranking


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

    Order (binding, from the plan's Key Technical Decisions):
    ``color_illegal`` → ``not_legal`` → ``non_edh_type`` →
    ``empty_types`` → ``is_commander``. When nothing fails the card
    should have been ranked — ``filter_reason_unknown`` flags a stale
    tensor (rows present for a card the live engine no longer scores).
    """
    if card_row is None:
        return REASON_FILTER_UNKNOWN
    cand_pips = _split_pips(card_row.get("color_identity"))
    if cand_pips - commander_identity:
        return REASON_COLOR_ILLEGAL
    if card_row.get("legal_commander") == 0:
        return REASON_NOT_LEGAL
    card_types = str(card_row.get("card_types") or "").split()
    if any(t in NON_EDH_CARD_TYPES for t in card_types):
        return REASON_NON_EDH_TYPE
    if not card_types:
        return REASON_EMPTY_TYPES
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
    return ForensicsReport(
        entries=tuple(kept),
        aggregate_bucket_counts=counts,
        total_misses=total_misses,
        skipped_commanders=tuple(skipped),
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


def synergy_content_digest(conn: sqlite3.Connection) -> str:
    """Cheap content digest over ``cards`` / ``card_ports``.

    SHA-256 over ``(COUNT(*), MAX(rowid))`` per table — mirrors the
    EDHREC snapshot digest mechanism: stable, cheap, sufficient to
    detect a cardsfolder re-import (which ``compute_config_hash``
    deliberately does NOT cover). Unit 5 records it as a provenance
    column.
    """
    h = hashlib.sha256()
    for table in ("cards", "card_ports"):
        row = conn.execute(f"SELECT COUNT(*), MAX(rowid) FROM {table}").fetchone()  # noqa: S608 — literal table names
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
    """
    pinned = PinnedFixture.load(Path(fixture_path))
    commanders: list[str] = []
    for entry in pinned.entries:
        if not isinstance(entry.commander, str):
            raise ForensicsPreconditionError(
                f"fixture entry {entry.commander!r} is a multi-commander entry: partners not supported in v1"
            )
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
# DB-shell compute function (two-connection pattern, NOT ATTACH)
# ---------------------------------------------------------------------------


def compute_forensics(
    *,
    db_path: Path | str,
    tags_path: Path | str,
    fixture_path: Path | str,
    top_n: int = TOP_N_DEFAULT,
) -> ForensicsReport:
    """Run the full Unit-1 forensics pass over the pinned fixture.

    Two-connection pattern: ``open_db(db_path, create=False)`` for
    synergy.db (gives the ``port_nodes`` view; refuses to materialize
    a missing DB) plus ``sqlite3.connect(tags_path)`` with
    ``row_factory = sqlite3.Row`` for tags.db. One shared
    :class:`SynergyEngine` supplies the live rankings; its score
    cache is cleared after each commander.

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
                for commander in commanders:
                    miss_universe = load_miss_universe(edhrec_conn, commander, top_n=top_n)
                    if not miss_universe:
                        entries.append(classify_commander_misses(commander, (), top_n=top_n))
                        continue

                    ranking = extract_live_ranking(engine, commander, card_meta)
                    tensor_candidates = load_tensor_candidates(conn, commander, config_hash)
                    resolved = {
                        name
                        for name in (normalize_label_name(label.name, known_names) for label in miss_universe)
                        if name is not None
                    }
                    lookup_names = sorted(resolved | {commander})
                    card_rows = load_card_rows(conn, lookup_names)
                    port_stats = load_port_stats(conn, lookup_names)
                    cmdr_row = card_rows.get(commander)
                    commander_identity = _split_pips(cmdr_row.get("color_identity") if cmdr_row else None)

                    entries.append(
                        classify_commander_misses(
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
                    )
                return aggregate_forensics(entries)
            finally:
                engine.close()
        finally:
            edhrec_conn.close()
    finally:
        conn.close()
