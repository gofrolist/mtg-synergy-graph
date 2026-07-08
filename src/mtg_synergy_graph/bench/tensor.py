"""Rule-contribution tensor I/O.

Wraps the persistence layer around the ``tensor_sink`` hook on
``score_all_universal``. One ``TensorWriter`` session collects rows
across one or more commander scorings, batches them into SQLite, and
tags every row with the same ``config_hash`` so stale rows are never
silently read when rules or multipliers change.

See ``docs/plans/2026-04-22-001-feat-unified-eval-harness-plan.md``
FR1 / FR2 for the contract.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Sequence
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from types import TracebackType

from mtg_synergy_graph.complement_rules.core import COMPLEMENT_RULES
from mtg_synergy_graph.universal_scorer import (
    TensorRow,
    TensorSink,
    get_scoring_config_inputs,
)

#: Flush rows to SQLite in chunks of this size to avoid transaction overhead.
_BATCH_SIZE = 10_000

_INSERT_SQL = (
    "INSERT OR REPLACE INTO rule_contributions "
    "(commander, candidate, rule_id, contribution, idf_weight, raw_count, "
    "config_hash, computed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
)


def compute_config_hash() -> str:
    """Hex SHA-256 over the current scoring config.

    Changes to the registered rule set, ``_RULE_QUALITY_MULTIPLIER``,
    ``_FLAT_WEIGHT_OVERRIDES``, or ``_SYNERGY_PAIRS`` all flip the hash.
    Stale tensor rows retain the old hash, so queries can filter by the
    current hash and refuse to read a pre-change tensor.

    ``_RULE_QUALITY_MULTIPLIER`` and ``_FLAT_WEIGHT_OVERRIDES`` are
    loaded from ``src/mtg_synergy_graph/data/scoring_weights.json`` at module import. Editing
    a ``value`` in that file flips the hash; editing a ``comment``
    does not (the comment field is metadata for human readers and is
    intentionally excluded from the hash input set).

    NOT in the hash (because either constant-in-function or captured
    elsewhere): the IDF formula shape, the 70% concentration-dampening
    threshold, multi-rule-bonus coefficients, circuit/cmc/rank bonus
    coefficients, and the scoring-function structure itself. These are
    code changes that refactors catch via ``bench.py audit
    --expect-identity`` instead of via hash invalidation.
    """
    h = hashlib.sha256()
    # Registered rule ids, in registration order converted to sorted.
    rule_ids = sorted(rule.rule_id for rule in COMPLEMENT_RULES)
    h.update(b"rule_ids:")
    h.update(repr(rule_ids).encode("utf-8"))

    # Read the scoring-config inputs through the public accessor so a
    # rename / split of the underlying private dicts is visible here
    # instead of silently producing stale hashes.
    cfg = get_scoring_config_inputs()
    h.update(b"|quality:")
    h.update(repr(sorted(cfg.rule_quality_multiplier.items())).encode("utf-8"))
    h.update(b"|flat:")
    h.update(repr(sorted(cfg.flat_weight_overrides.items())).encode("utf-8"))
    # Pair bonuses fire inside ``score()`` after the per-rule sum, so
    # adding / removing / retuning a pair flips downstream scores even
    # when per-rule IDF weights are unchanged.
    h.update(b"|pairs:")
    h.update(repr(sorted((sorted(pair), weight) for pair, weight in cfg.synergy_pairs.items())).encode("utf-8"))
    # Plan 2026-04-23-001 Unit 6: pathway feature flag must flip the
    # hash so flag=True vs flag=False runs cannot compare against
    # each other's pinned tensor.
    h.update(b"|pathway:")
    h.update(repr(cfg.enable_pathway_rules).encode("utf-8"))
    # Plan 2026-04-23-003 Unit 7: embedding-contribution flip flag,
    # scaling weight, decay rate, and vectorizer version — each of
    # these changes every candidate's ``score`` when the gate is
    # open, so each must invalidate the pinned tensor.
    h.update(b"|embedding_enable:")
    h.update(repr(cfg.enable_embedding_contribution).encode("utf-8"))
    h.update(b"|embedding_w:")
    h.update(repr(cfg.embedding_w).encode("utf-8"))
    h.update(b"|embedding_k:")
    h.update(repr(cfg.embedding_k).encode("utf-8"))
    h.update(b"|vectorizer_version:")
    h.update(repr(cfg.vectorizer_version).encode("utf-8"))
    # 2026-06-09 audit follow-up: seed JSONs and the STAPLES dict all
    # change live scores when edited, so each must invalidate the
    # pinned tensor (previously none of the three was hashed and an
    # edit left stale tensors silently readable).
    h.update(b"|event_match_seed:")
    h.update(cfg.event_match_seed_digest.encode("utf-8"))
    h.update(b"|declarative_rules:")
    h.update(cfg.declarative_rules_digest.encode("utf-8"))
    h.update(b"|staples:")
    h.update(repr(sorted((pip, tuple(names)) for pip, names in cfg.staples.items())).encode("utf-8"))
    # Plan 2026-07-02-002 Unit 4: concave family-aggregation flip flag
    # changes both totals' dampening semantics — flag=True vs False
    # runs must not compare against each other's pinned tensor.
    h.update(b"|concave_family_agg:")
    h.update(repr(cfg.enable_concave_family_agg).encode("utf-8"))
    # Plan 2026-07-02-002 Unit 5: tribal payoff/body tier flag reroutes
    # body emissions to tribal_body — flag flips invalidate the tensor.
    h.update(b"|tribal_payoff_tier:")
    h.update(repr(cfg.enable_tribal_payoff_tier).encode("utf-8"))
    # Plan 2026-07-02-002 Unit 6: pool-scaled flat weights flag + floor.
    h.update(b"|pool_scaled_flat:")
    h.update(repr(cfg.enable_pool_scaled_flat_weights).encode("utf-8"))
    h.update(b"|pool_scale_floor:")
    h.update(repr(cfg.pool_scale_floor).encode("utf-8"))
    # Plan 2026-07-07-001 review follow-up (F1): the subtype-supply rule
    # gate flag was previously hash-blind -- a flip could silently read a
    # stale pinned tensor. Fold it in like every other rule-gating flag.
    h.update(b"|subtype_supply_enable:")
    h.update(repr(cfg.enable_subtype_supply).encode("utf-8"))
    return h.hexdigest()


#: SQLite caps bound variables per statement (999 on the historical
#: default build). Chunk the ``commander IN (...)`` eviction well under
#: that so an arbitrarily large fixture can never overflow it.
_EVICT_CHUNK = 400


def evict_fixture_rows(
    conn: sqlite3.Connection,
    config_hash: str,
    commanders: Sequence[str],
    *,
    chunk_size: int = _EVICT_CHUNK,
) -> int:
    """Delete ``rule_contributions`` rows for ``commanders`` at ``config_hash``.

    This is the additive-repin eviction (see
    ``docs/solutions/best-practices/tensor-single-owner-slot-2026-07-08.md``).
    A ``--repin`` scopes deletion to the re-pinned fixture's commanders
    instead of blanket-deleting the whole ``config_hash``, so broad
    (golden-100/500) and cohort (archetype/outlet) fixtures COEXIST at
    the same config_hash rather than evicting one another from a single
    slot. Semantics of the two row classes:

    * The re-pinned commanders: every row (all candidates, all rules) is
      cleared here, then ``TensorWriter`` repopulates only the rules that
      still fire -- so a rule that stopped firing leaves no orphan row.
    * Every OTHER commander at this config_hash: untouched. Scores are
      ``config_hash``-deterministic, so a commander shared by two fixtures
      has identical rows regardless of which fixture last pinned it; the
      ``INSERT OR REPLACE`` PK ``(commander, candidate, rule_id,
      config_hash)`` set-dedups on overlap.

    Aggregate readers that query ``WHERE config_hash = ?`` WITHOUT a
    commander filter (``summarize_rule_contributions``,
    ``compute_collinearity``, embedding-dedup, demand-coverage pool
    counts) therefore now span the UNION of pinned fixtures rather than
    the last-pinned one. That is a well-defined, monotonic population
    (no double-counting) -- intended, not a regression.

    Chunks the ``IN`` clause to stay under SQLite's bound-variable limit.
    Returns the number of rows deleted. ``chunk_size <= 0`` raises
    ``ValueError`` (a zero/negative chunk would loop forever).
    """
    if chunk_size <= 0:
        raise ValueError(f"chunk_size must be >= 1; got {chunk_size}")
    total = 0
    for start in range(0, len(commanders), chunk_size):
        batch = commanders[start : start + chunk_size]
        if not batch:
            continue
        placeholders = ",".join("?" * len(batch))
        cur = conn.execute(
            f"DELETE FROM rule_contributions WHERE config_hash = ? AND commander IN ({placeholders})",
            (config_hash, *batch),
        )
        total += cur.rowcount
    return total


class TensorWriter(AbstractContextManager["TensorWriter"]):
    """Batch writer that persists tensor rows under a single config_hash.

    Use as a context manager::

        with TensorWriter(conn) as writer:
            score_all_universal(conn, ["Korvold..."], tensor_sink=writer.sink)

    The writer buffers rows in memory and flushes in chunks of
    ``_BATCH_SIZE``. On context exit, any remaining rows are flushed
    and the transaction is committed. If an exception propagates,
    buffered rows are discarded — partial tensors never land.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        config_hash: str | None = None,
        computed_at: str | None = None,
    ) -> None:
        self._conn = conn
        self.config_hash = config_hash or compute_config_hash()
        self.computed_at = computed_at or datetime.now(UTC).isoformat(timespec="seconds")
        self._buffer: list[TensorRow] = []
        self._rows_written = 0
        self._closed = False

    @property
    def sink(self) -> TensorSink:
        """A ``TensorSink`` callable bound to this writer."""
        return self._record

    def _record(self, row: TensorRow) -> None:
        if self._closed:
            raise RuntimeError("cannot record into a closed TensorWriter")
        self._buffer.append(row)
        if len(self._buffer) >= _BATCH_SIZE:
            self._flush()

    def _flush(self) -> None:
        if not self._buffer:
            return
        rows = [
            (
                r.commander,
                r.candidate,
                r.rule_id,
                r.contribution,
                r.idf_weight,
                r.raw_count,
                self.config_hash,
                self.computed_at,
            )
            for r in self._buffer
        ]
        self._conn.executemany(_INSERT_SQL, rows)
        self._rows_written += len(rows)
        self._buffer.clear()

    @property
    def rows_written(self) -> int:
        """Total rows flushed to SQLite so far (excludes buffered rows)."""
        return self._rows_written

    def close(self) -> None:
        """Flush + commit any pending rows. Idempotent."""
        if self._closed:
            return
        self._flush()
        self._conn.commit()
        self._closed = True

    def __enter__(self) -> TensorWriter:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        if exc_type is None:
            self.close()
        else:
            # Discard any buffered rows; do not commit a partial tensor.
            self._buffer.clear()
            self._closed = True
