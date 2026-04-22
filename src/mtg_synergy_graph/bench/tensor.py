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
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from types import TracebackType

from mtg_synergy_graph.complement_rules.core import COMPLEMENT_RULES
from mtg_synergy_graph.universal_scorer import (
    _FLAT_WEIGHT_OVERRIDES,
    _RULE_QUALITY_MULTIPLIER,
    TensorSink,
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

    Changes to the registered rule set, ``_RULE_QUALITY_MULTIPLIER``, or
    ``_FLAT_WEIGHT_OVERRIDES`` all flip the hash. Stale tensor rows
    retain the old hash, so queries can filter by the current hash and
    refuse to read a pre-change tensor.

    Tensor-agnostic inputs (IDF formula shape, bonus constants, the
    scoring function structure itself) are NOT in the hash — those are
    code changes that refactors catch via ``bench.py audit
    --expect-identity`` instead.
    """
    h = hashlib.sha256()
    # Registered rule ids, in registration order converted to sorted.
    rule_ids = sorted(rule.rule_id for rule in COMPLEMENT_RULES)
    h.update(b"rule_ids:")
    h.update(repr(rule_ids).encode("utf-8"))
    h.update(b"|quality:")
    h.update(repr(sorted(_RULE_QUALITY_MULTIPLIER.items())).encode("utf-8"))
    h.update(b"|flat:")
    h.update(repr(sorted(_FLAT_WEIGHT_OVERRIDES.items())).encode("utf-8"))
    return h.hexdigest()


@dataclass(frozen=True)
class _Row:
    commander: str
    candidate: str
    rule_id: str
    contribution: float
    idf_weight: float
    raw_count: int


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
        self._buffer: list[_Row] = []
        self._rows_written = 0
        self._closed = False

    @property
    def sink(self) -> TensorSink:
        """A ``TensorSink`` callable bound to this writer."""
        return self._record

    def _record(
        self,
        commander: str,
        candidate: str,
        rule_id: str,
        contribution: float,
        idf_weight: float,
        raw_count: int,
    ) -> None:
        if self._closed:
            raise RuntimeError("cannot record into a closed TensorWriter")
        self._buffer.append(_Row(commander, candidate, rule_id, contribution, idf_weight, raw_count))
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
