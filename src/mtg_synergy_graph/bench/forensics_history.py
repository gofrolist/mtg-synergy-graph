"""Append-only ``.audit/forensics_history.csv`` + ``--trend forensics``
(Unit 5 of plan 2026-06-10-001, R7).

Sibling CSV to :mod:`bench.history` with its OWN schema and reader —
:data:`FORENSICS_CSV_FIELDS` is never shared with (or extended from)
``history.CSV_FIELDS``: the audit history reader is strict on its
header, so widening the shared tuple would break every existing
``--trend hidden_gems`` consumer (the breakage documented in the
origin brainstorm). The append/read DISCIPLINE mirrors ``history.py``
exactly: append-only, header-once via ``fh.tell() == 0``, write errors
(``OSError`` / ``csv.Error``) degrade to a stderr warning and never
fail the run, ``fmt_float`` 6-decimal rendering with empty-string for
``None``, malformed-row-tolerant reader.

Row provenance (plan Key Decisions):

* ``config_hash`` — the hash the run's tensor reads were filtered by
  (already validated equal to the live config by the forensics
  preconditions).
* ``fixture_sha256`` — SHA-256 over the pinned fixture FILE BYTES
  (:func:`fixture_file_sha256`), per the verify-from-stored-config
  learning: provenance comes from the stored artifact, not a
  recomputed default.
* ``edhrec_snapshot_digest`` — SHA-256 over ``f"{COUNT(*)}:{MAX(rowid)}"``
  of ``edhrec_card_synergy`` (:func:`edhrec_snapshot_digest`): cheap,
  stable, sufficient to detect a tags.db refresh mid-series.
* ``synergy_db_digest`` — ``forensics.synergy_content_digest`` over
  ``cards`` / ``card_ports`` (detects a cardsfolder re-import, which
  ``compute_config_hash`` deliberately does not cover).
* ``commit_sha`` — via ``history._commit_sha`` (empty string when git
  is unavailable; same sentinel semantics as the audit history).

Metric columns: the five bucket proportions (fractions of total
misses, summing to 1.0; empty when the run had zero misses), the two
canonical-denominator aggregates, and ``gem_rate_forensics``.

``gem_rate_forensics`` is computed IN-RUN per commander via
``hidden_gems.hidden_gem_hit_rate_for_commander`` with the HS-top-30
reference set (``fetch_high_synergy_top_n``, the same convention the
main audit's gem axis uses) over the live top-30 from the forensics
pass plus the commander's tensor contributions. This is DELIBERATELY
the HS reference — NOT R9's all-sections set — so the column stays
comparable with the existing gem-axis series (the two-reference-set
design is documented in the plan's Key Technical Decisions); the
``_forensics`` suffix flags that its top-30 source is the production
``page()`` order, which differs marginally from the audit's pre-filter
top-30. Aggregate = mean over commanders with EDHREC HS data (``None``
entries skipped, matching ``hidden_gems`` conventions); ``None`` (empty
cell) when every commander was skipped.

``--trend forensics`` (handled here, module-owned like
``forensics_report.handle_forensics``): renders the last N rows as
md (default) / json / csv. Rows are grouped by
``(config_hash, edhrec_snapshot_digest)``; when EITHER changes between
consecutive rows the md/json renderers insert an explicit boundary
marker (:data:`BOUNDARY_MARKER`) and NO delta is computed across the
boundary — deltas (``aggregate_ndcg_canonical`` / ``gem_rate_forensics``
vs the previous row) exist only WITHIN a group. Duplicate rows from
repeated identical runs share a group and are accepted as-is (their
deltas are simply 0). The csv format is a raw playback of the stored
rows (header + cells, no delta columns, no marker rows) so it stays
machine-parseable against :data:`FORENSICS_CSV_FIELDS`.

A history row is appended ONLY on full success — the
``handle_forensics`` wiring appends after reconciliation AND rendering
succeed, so a precondition / reconciliation failure (exit 2) leaves
the file untouched.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sqlite3
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from mtg_synergy_graph.bench.forensics import (
    BUCKET_DATA_GAP,
    BUCKET_FILTERED,
    BUCKET_NEAR_MISS,
    BUCKET_NO_RULES,
    BUCKET_OUTRANKED,
    BUCKETS,
    TOP_N_DEFAULT,
    ForensicsReport,
    load_tensor_contributions,
)
from mtg_synergy_graph.bench.hidden_gems import hidden_gem_hit_rate_for_commander
from mtg_synergy_graph.bench.history import _commit_sha, fmt_float
from mtg_synergy_graph.edhrec_helpers import fetch_high_synergy_top_n

#: CSV column order. Must match ``ForensicsHistoryRow`` field order.
#: A SIBLING of ``history.CSV_FIELDS`` — never shared, never extended
#: from it (strict-header readers on both sides).
FORENSICS_CSV_FIELDS: tuple[str, ...] = (
    "timestamp",
    "commit_sha",
    "config_hash",
    "fixture_sha256",
    "edhrec_snapshot_digest",
    "synergy_db_digest",
    "near_miss",
    "outranked",
    "filtered",
    "data_gap",
    "no_rules",
    "aggregate_ndcg_canonical",
    "aggregate_raw_dcg_canonical",
    "gem_rate_forensics",
    "n_commanders",
    "n_skipped",
)

#: Default append target; gitignored via ``.audit/``. Overridable with
#: the ``--forensics-history PATH`` companion flag.
DEFAULT_FORENSICS_HISTORY_PATH = Path(".audit/forensics_history.csv")

#: Marker rendered between trend rows whose ``config_hash`` OR
#: ``edhrec_snapshot_digest`` differ — deltas are never computed across
#: this line (a config change or tags.db refresh invalidates the
#: comparison).
BOUNDARY_MARKER = "— boundary: config_hash / edhrec snapshot changed; deltas reset —"


@dataclass(frozen=True)
class ForensicsHistoryRow:
    """One row of ``.audit/forensics_history.csv``, typed and immutable.

    Bucket-proportion fields are fractions of total misses in
    ``BUCKETS`` order (``near_miss`` + ``outranked`` + ``filtered`` +
    ``data_gap`` + ``no_rules`` = 1.0) and are ``None`` (empty cells)
    when the run classified zero misses. ``gem_rate_forensics`` is
    ``None`` when every commander lacked EDHREC HS reference data.
    """

    timestamp: str
    commit_sha: str
    config_hash: str
    fixture_sha256: str
    edhrec_snapshot_digest: str
    synergy_db_digest: str
    near_miss: float | None
    outranked: float | None
    filtered: float | None
    data_gap: float | None
    no_rules: float | None
    aggregate_ndcg_canonical: float | None
    aggregate_raw_dcg_canonical: float | None
    gem_rate_forensics: float | None
    n_commanders: int
    n_skipped: int


# ---------------------------------------------------------------------------
# Provenance digests
# ---------------------------------------------------------------------------


def fixture_file_sha256(fixture_path: Path | str) -> str:
    """SHA-256 over the pinned fixture's raw file bytes."""
    return hashlib.sha256(Path(fixture_path).read_bytes()).hexdigest()


def edhrec_snapshot_digest(edhrec_conn: sqlite3.Connection) -> str:
    """SHA-256 over ``f"{COUNT(*)}:{MAX(rowid)}"`` of ``edhrec_card_synergy``.

    The plan's Key-Decision derivation: cheap, stable, and sufficient
    to detect a tags.db refresh (any insert moves ``MAX(rowid)`` even
    when a deletion keeps ``COUNT(*)`` constant, and vice versa).
    """
    row = edhrec_conn.execute("SELECT COUNT(*), MAX(rowid) FROM edhrec_card_synergy").fetchone()
    return hashlib.sha256(f"{row[0]}:{row[1]}".encode()).hexdigest()


# ---------------------------------------------------------------------------
# gem_rate_forensics (in-run, HS-top-30 reference — see module docstring)
# ---------------------------------------------------------------------------


def compute_gem_rate_forensics(
    report: ForensicsReport,
    edhrec_conn: sqlite3.Connection,
    conn: sqlite3.Connection,
    config_hash: str,
    *,
    top_n: int = TOP_N_DEFAULT,
) -> float | None:
    """Mean hidden-gem hit rate over the run's live top-30 windows.

    Per commander: ``hidden_gem_hit_rate_for_commander`` with the
    HS-top-30 reference (``fetch_high_synergy_top_n`` — the audit's
    gem-axis convention, deliberately NOT R9's all-sections set), the
    forensics live top-30, and the commander's hash-filtered tensor
    cohort. Commanders without HS data return ``None`` from the gate
    and are skipped (``hidden_gems`` conventions); the aggregate is the
    mean over the rest, or ``None`` when everyone was skipped.
    """
    rates: list[float] = []
    for entry in report.entries:
        hs_top = fetch_high_synergy_top_n(edhrec_conn, entry.commander, limit=top_n)
        contributions = load_tensor_contributions(conn, entry.commander, config_hash)
        gem = hidden_gem_hit_rate_for_commander(entry.commander, entry.live_top_30, hs_top, contributions)
        if gem is not None:
            rates.append(gem.rate)
    if not rates:
        return None
    return sum(rates) / len(rates)


# ---------------------------------------------------------------------------
# Row builder + writer
# ---------------------------------------------------------------------------


def build_history_row(
    report: ForensicsReport,
    *,
    config_hash: str,
    fixture_digest: str,
    snapshot_digest: str,
    synergy_digest: str,
    gem_rate: float | None,
) -> ForensicsHistoryRow:
    """Assemble one history row from a successful forensics run.

    Bucket proportions are fractions of total misses (``None`` for a
    zero-miss run); ``n_commanders`` is the CANONICAL count (classified
    + skipped) and ``n_skipped`` the zero-label subset, matching the
    report's two-denominator discipline.
    """
    total = sum(report.aggregate_bucket_counts.get(bucket, 0) for bucket in BUCKETS)
    if total > 0:
        fraction = {bucket: report.aggregate_bucket_counts.get(bucket, 0) / total for bucket in BUCKETS}
    else:
        fraction = dict.fromkeys(BUCKETS, None)
    return ForensicsHistoryRow(
        timestamp=datetime.now(UTC).isoformat(timespec="seconds"),
        commit_sha=_commit_sha(),
        config_hash=config_hash,
        fixture_sha256=fixture_digest,
        edhrec_snapshot_digest=snapshot_digest,
        synergy_db_digest=synergy_digest,
        near_miss=fraction[BUCKET_NEAR_MISS],
        outranked=fraction[BUCKET_OUTRANKED],
        filtered=fraction[BUCKET_FILTERED],
        data_gap=fraction[BUCKET_DATA_GAP],
        no_rules=fraction[BUCKET_NO_RULES],
        aggregate_ndcg_canonical=report.aggregate_ndcg_canonical,
        aggregate_raw_dcg_canonical=report.aggregate_raw_dcg_canonical,
        gem_rate_forensics=gem_rate,
        n_commanders=len(report.entries) + len(report.skipped_commanders),
        n_skipped=len(report.skipped_commanders),
    )


def _row_to_cells(row: ForensicsHistoryRow) -> list[str]:
    """Render a ``ForensicsHistoryRow`` into its 16 string cells."""
    return [
        row.timestamp,
        row.commit_sha,
        row.config_hash,
        row.fixture_sha256,
        row.edhrec_snapshot_digest,
        row.synergy_db_digest,
        fmt_float(row.near_miss),
        fmt_float(row.outranked),
        fmt_float(row.filtered),
        fmt_float(row.data_gap),
        fmt_float(row.no_rules),
        fmt_float(row.aggregate_ndcg_canonical),
        fmt_float(row.aggregate_raw_dcg_canonical),
        fmt_float(row.gem_rate_forensics),
        str(row.n_commanders),
        str(row.n_skipped),
    ]


def append_forensics_run(
    row: ForensicsHistoryRow,
    path: str | Path = DEFAULT_FORENSICS_HISTORY_PATH,
) -> None:
    """Append one row to the forensics history CSV.

    Mirrors ``history.append_run`` exactly: creates the parent
    directory, emits the header once via the append-mode FD position
    (``fh.tell() == 0`` iff the file was empty / just created — no
    exists/stat race), and degrades ANY write error (``OSError``,
    ``csv.Error``) to a stderr warning — a history write failure must
    never turn a successful forensics run into a failure.
    """
    csv_path = Path(path)
    try:
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        with csv_path.open("a", encoding="utf-8", newline="") as fh:
            pos = fh.tell()
            if pos < 0:  # pragma: no cover — platform-specific fallback (history.py precedent)
                import os as _os

                pos = _os.fstat(fh.fileno()).st_size
            writer = csv.writer(fh)
            if pos == 0:
                writer.writerow(FORENSICS_CSV_FIELDS)
            writer.writerow(_row_to_cells(row))
    except (OSError, csv.Error) as exc:
        print(
            f"bench.py audit --forensics: warning: failed to append history row to "
            f"{csv_path}: {exc.__class__.__name__}: {exc}",
            file=sys.stderr,
        )


# ---------------------------------------------------------------------------
# Reader (malformed-row-tolerant, sibling of history.read_last)
# ---------------------------------------------------------------------------


def read_last_forensics(
    n: int,
    path: str | Path = DEFAULT_FORENSICS_HISTORY_PATH,
) -> list[ForensicsHistoryRow]:
    """Return the last ``n`` rows of ``path`` (chronological order).

    Returns ``[]`` when the file does not exist. Skips malformed rows
    (wrong field count, unparseable numbers) with a stderr warning so a
    single bad append can't break the whole trend command; a header
    mismatch (a foreign CSV at the path) skips the whole file with a
    warning.
    """
    csv_path = Path(path)
    if not csv_path.exists() or n <= 0:
        return []

    rows: list[ForensicsHistoryRow] = []
    total_rows = 0
    skipped_count = 0
    try:
        with csv_path.open(encoding="utf-8", newline="") as fh:
            reader = csv.reader(fh)
            try:
                header = next(reader)
            except StopIteration:
                return []
            if tuple(header) != FORENSICS_CSV_FIELDS:
                print(
                    f"bench.py audit: warning: {csv_path} header mismatch; "
                    f"expected {FORENSICS_CSV_FIELDS}, got {tuple(header)}",
                    file=sys.stderr,
                )
                return []
            for lineno, raw in enumerate(reader, start=2):
                total_rows += 1
                parsed = _parse_row(raw, lineno=lineno, path=csv_path)
                if parsed is None:
                    skipped_count += 1
                else:
                    rows.append(parsed)
    except OSError as exc:
        print(
            f"bench.py audit: warning: failed to read forensics history from {csv_path}: {exc}",
            file=sys.stderr,
        )
        return []

    if skipped_count > 0:
        print(
            f"bench.py audit: warning: skipped {skipped_count} of {total_rows} malformed row(s) in {csv_path}",
            file=sys.stderr,
        )

    if n >= len(rows):
        return rows
    return rows[-n:]


def _parse_row(raw: list[str], *, lineno: int, path: Path) -> ForensicsHistoryRow | None:
    """Convert one raw CSV row; skip + warn on any malformation."""
    if len(raw) != len(FORENSICS_CSV_FIELDS):
        print(
            f"bench.py audit: warning: skipping malformed row {lineno} in "
            f"{path}: expected {len(FORENSICS_CSV_FIELDS)} fields, got {len(raw)}",
            file=sys.stderr,
        )
        return None
    try:
        return ForensicsHistoryRow(
            timestamp=raw[0],
            commit_sha=raw[1],
            config_hash=raw[2],
            fixture_sha256=raw[3],
            edhrec_snapshot_digest=raw[4],
            synergy_db_digest=raw[5],
            near_miss=_parse_float(raw[6]),
            outranked=_parse_float(raw[7]),
            filtered=_parse_float(raw[8]),
            data_gap=_parse_float(raw[9]),
            no_rules=_parse_float(raw[10]),
            aggregate_ndcg_canonical=_parse_float(raw[11]),
            aggregate_raw_dcg_canonical=_parse_float(raw[12]),
            gem_rate_forensics=_parse_float(raw[13]),
            n_commanders=_parse_int(raw[14]),
            n_skipped=_parse_int(raw[15]),
        )
    except ValueError as exc:
        print(
            f"bench.py audit: warning: skipping bad row {lineno} in {path}: {exc}",
            file=sys.stderr,
        )
        return None


def _parse_float(value: str) -> float | None:
    if value == "":
        return None
    return float(value)


def _parse_int(value: str) -> int:
    if value == "":
        return 0
    return int(value)


# ---------------------------------------------------------------------------
# Trend view — (config_hash, snapshot) grouping with boundary markers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ForensicsTrendItem:
    """One rendered trend line: a history row with within-group deltas,
    or a boundary marker (``row is None``).

    Deltas compare against the PREVIOUS row of the same
    ``(config_hash, edhrec_snapshot_digest)`` group; the first row of a
    group (including the first row after a boundary) carries ``None``
    deltas. Duplicate same-config rows are accepted — they simply
    produce 0.0 deltas.
    """

    row: ForensicsHistoryRow | None
    ndcg_delta: float | None
    gem_rate_delta: float | None

    @property
    def is_boundary(self) -> bool:
        return self.row is None


def build_trend_items(rows: Sequence[ForensicsHistoryRow]) -> list[ForensicsTrendItem]:
    """Fold chronological rows into trend items with boundary markers.

    A boundary item is inserted whenever ``config_hash`` OR
    ``edhrec_snapshot_digest`` changes between consecutive rows; the
    delta baseline resets across it (no delta is ever computed across a
    boundary).
    """
    items: list[ForensicsTrendItem] = []
    prev: ForensicsHistoryRow | None = None
    for row in rows:
        if prev is not None and (
            row.config_hash != prev.config_hash or row.edhrec_snapshot_digest != prev.edhrec_snapshot_digest
        ):
            items.append(ForensicsTrendItem(row=None, ndcg_delta=None, gem_rate_delta=None))
            prev = None
        ndcg_delta = (
            row.aggregate_ndcg_canonical - prev.aggregate_ndcg_canonical
            if prev is not None
            and row.aggregate_ndcg_canonical is not None
            and prev.aggregate_ndcg_canonical is not None
            else None
        )
        gem_delta = (
            row.gem_rate_forensics - prev.gem_rate_forensics
            if prev is not None and row.gem_rate_forensics is not None and prev.gem_rate_forensics is not None
            else None
        )
        items.append(ForensicsTrendItem(row=row, ndcg_delta=ndcg_delta, gem_rate_delta=gem_delta))
        prev = row
    return items


_EM_DASH = "—"
_TREND_MD_FIELDS: tuple[str, ...] = (*FORENSICS_CSV_FIELDS, "ndcg_delta", "gem_rate_delta")


def _fmt_delta(value: float | None) -> str:
    """Signed 6-decimal delta cell; ``None`` → em-dash."""
    if value is None:
        return _EM_DASH
    return f"{value:+.6f}"


def _render_trend_forensics_md(items: Sequence[ForensicsTrendItem]) -> str:
    lines: list[str] = []
    lines.append("| " + " | ".join(_TREND_MD_FIELDS) + " |")
    lines.append("|" + "|".join(["---"] * len(_TREND_MD_FIELDS)) + "|")
    for item in items:
        if item.row is None:
            cells = (BOUNDARY_MARKER, *([_EM_DASH] * (len(_TREND_MD_FIELDS) - 1)))
        else:
            cells = (*_row_to_cells(item.row), _fmt_delta(item.ndcg_delta), _fmt_delta(item.gem_rate_delta))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def _render_trend_forensics_json(items: Sequence[ForensicsTrendItem]) -> str:
    payload: list[dict[str, object]] = []
    for item in items:
        if item.row is None:
            payload.append({"boundary": True, "marker": BOUNDARY_MARKER})
            continue
        row = item.row
        payload.append(
            {
                "timestamp": row.timestamp,
                "commit_sha": row.commit_sha,
                "config_hash": row.config_hash,
                "fixture_sha256": row.fixture_sha256,
                "edhrec_snapshot_digest": row.edhrec_snapshot_digest,
                "synergy_db_digest": row.synergy_db_digest,
                "near_miss": row.near_miss,
                "outranked": row.outranked,
                "filtered": row.filtered,
                "data_gap": row.data_gap,
                "no_rules": row.no_rules,
                "aggregate_ndcg_canonical": row.aggregate_ndcg_canonical,
                "aggregate_raw_dcg_canonical": row.aggregate_raw_dcg_canonical,
                "gem_rate_forensics": row.gem_rate_forensics,
                "n_commanders": row.n_commanders,
                "n_skipped": row.n_skipped,
                "ndcg_delta": item.ndcg_delta,
                "gem_rate_delta": item.gem_rate_delta,
            }
        )
    return json.dumps(payload, indent=2, ensure_ascii=False)


def _render_trend_forensics_csv(rows: Sequence[ForensicsHistoryRow]) -> str:
    """Raw playback of the stored rows (no deltas, no boundary rows) so
    the output stays machine-parseable against
    :data:`FORENSICS_CSV_FIELDS` — same convention as
    ``handlers._render_trend_csv`` for the audit history."""
    import io as _io

    buf = _io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(FORENSICS_CSV_FIELDS)
    for row in rows:
        writer.writerow(_row_to_cells(row))
    return buf.getvalue()


def handle_trend_forensics(args: argparse.Namespace) -> int:
    """Handle ``bench.py audit --trend forensics``.

    Reads the last ``args.trend_n`` rows (default 20) from the
    append-only forensics history CSV (``--forensics-history PATH``,
    default ``.audit/forensics_history.csv``) and renders them as
    Markdown (default), JSON, or CSV. md/json group rows by
    ``(config_hash, edhrec_snapshot_digest)`` and insert
    :data:`BOUNDARY_MARKER` rows between groups — deltas are only
    computed within a group; csv is a raw playback (see
    :func:`_render_trend_forensics_csv`). Duplicate same-config rows
    are accepted. Missing history is not an error — advisory stderr,
    exit 0.

    Exit codes:
    * ``0`` — normal (including fresh-checkout / no history).
    """
    history_path = Path(getattr(args, "forensics_history", DEFAULT_FORENSICS_HISTORY_PATH))
    trend_n = int(getattr(args, "trend_n", 20))

    if not history_path.exists():
        print(
            "no forensics history yet — run `bench.py audit --forensics` first",
            file=sys.stderr,
        )
        return 0

    rows = read_last_forensics(trend_n, path=history_path) if trend_n > 0 else []

    fmt = getattr(args, "format", "md")
    if fmt == "json":
        rendered = _render_trend_forensics_json(build_trend_items(rows))
    elif fmt == "csv":
        rendered = _render_trend_forensics_csv(rows)
    else:
        rendered = _render_trend_forensics_md(build_trend_items(rows))

    output_target = getattr(args, "output", None)
    if output_target is None or output_target == "-":
        print(rendered, end="" if rendered.endswith("\n") else "\n")
    else:
        output_path = Path(output_target)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(rendered, encoding="utf-8")
        print(
            f"bench.py audit --trend forensics: report written to {output_path}",
            file=sys.stderr,
        )
    return 0


__all__ = [
    "BOUNDARY_MARKER",
    "DEFAULT_FORENSICS_HISTORY_PATH",
    "FORENSICS_CSV_FIELDS",
    "ForensicsHistoryRow",
    "ForensicsTrendItem",
    "append_forensics_run",
    "build_history_row",
    "build_trend_items",
    "compute_gem_rate_forensics",
    "edhrec_snapshot_digest",
    "fixture_file_sha256",
    "handle_trend_forensics",
    "read_last_forensics",
]
