"""Orchestrator for building ``data/forge_oracle.db``.

Pipeline:

1. Walk the Forge precon corpus (``.dck`` files under the configured
   deck directories — by default ``data/forge/forge-gui/res/quest/
   {commanderprecons,precons}/``).
2. For each deck, parse the ``.dck`` file, resolve card names to
   ``oracle_id`` against the local ``cards`` table, then expand each
   card to its ``port_nodes.subkind`` set.
3. Aggregate into a corpus of ``list[frozenset[subkind]]`` decks and
   compute PPMI via ``ppmi.compute_ppmi_table``.
4. Write rows to ``forge_precon_ppmi`` (canonical ordering, threshold
   ``decks_count >= min_decks_count``) under a single transaction on
   a temp DB; atomic-rename into place on success.

Consumers:
- ``scripts/forge_oracle.py build`` is the CLI entry.
- Unit 5 adds the ``OracleConfigInputs`` + hash write at the end of
  ``build`` so strict consumers can refuse-to-run on drift.

Plan: docs/plans/2026-04-23-002-feat-forge-second-oracle-plan.md Unit 4.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from mtg_synergy_graph.forge_oracle import config as fo_config
from mtg_synergy_graph.forge_oracle import dck_parser
from mtg_synergy_graph.forge_oracle import ppmi as ppmi_math

_LOG = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCHEMA_PATH = Path(__file__).parent / "schema.sql"

#: Forge-bundled precon deck directories. Relative to ``data/forge/``.
DEFAULT_DECK_DIRS: tuple[Path, ...] = (
    Path("forge-gui/res/quest/commanderprecons"),
    Path("forge-gui/res/quest/precons"),
)


@dataclass(frozen=True, slots=True)
class IngestStats:
    """Summary of a build run for logging + tests."""

    decks_parsed: int
    decks_with_any_resolved_card: int
    unknown_card_names: int  # names absent from our cards table
    distinct_subkinds: int
    ppmi_rows_written: int


def _read_schema() -> str:
    return _SCHEMA_PATH.read_text(encoding="utf-8")


def _init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(_read_schema())


def _load_name_to_oracle_id(source_conn: sqlite3.Connection) -> dict[str, str]:
    """Map ``cards.name -> cards.oracle_id`` (oracle_id non-null only)."""
    return {
        row[0]: row[1]
        for row in source_conn.execute("SELECT name, oracle_id FROM cards WHERE oracle_id IS NOT NULL").fetchall()
    }


def _load_oracle_id_to_subkinds(
    source_conn: sqlite3.Connection,
) -> dict[str, frozenset[str]]:
    """For each ``oracle_id``, the set of distinct ``port_nodes.subkind``.

    Cards with zero ports get an empty set — their names still contribute
    marginals in the PPMI aggregation but no pairs.
    """
    out: dict[str, set[str]] = {}
    rows = source_conn.execute(
        "SELECT DISTINCT c.oracle_id, pn.subkind "
        "FROM cards c JOIN port_nodes pn ON pn.card_name = c.name "
        "WHERE c.oracle_id IS NOT NULL"
    ).fetchall()
    for oid, subkind in rows:
        out.setdefault(oid, set()).add(subkind)
    return {oid: frozenset(subs) for oid, subs in out.items()}


def _deck_to_subkind_corpus(
    deck_files: Iterable[Path],
    name_to_oid: dict[str, str],
    oid_to_subkinds: dict[str, frozenset[str]],
) -> tuple[list[frozenset[str]], IngestStats]:
    """Transform ``.dck`` files into a corpus of subkind-sets for PPMI.

    One deck → one ``frozenset[subkind]`` = the union of all subkinds
    across the deck's cards. Commander cards are included alongside
    main cards (same treatment).
    """
    corpus: list[frozenset[str]] = []
    decks_parsed = 0
    decks_with_any_resolved = 0
    unknown_names: set[str] = set()
    all_subkinds: set[str] = set()

    for deck_file in deck_files:
        try:
            deck = dck_parser.parse_dck(deck_file)
        except FileNotFoundError:
            _LOG.warning("deck file vanished mid-ingest: %s", deck_file)
            continue
        decks_parsed += 1
        subkinds: set[str] = set()
        resolved_any = False
        for name in dck_parser.iter_deck_names(deck):
            oid = name_to_oid.get(name)
            if oid is None:
                unknown_names.add(name)
                continue
            card_subs = oid_to_subkinds.get(oid)
            if not card_subs:
                resolved_any = True  # card is known, just has no ports
                continue
            resolved_any = True
            subkinds.update(card_subs)
        if resolved_any:
            decks_with_any_resolved += 1
        all_subkinds.update(subkinds)
        if subkinds:
            corpus.append(frozenset(subkinds))

    stats = IngestStats(
        decks_parsed=decks_parsed,
        decks_with_any_resolved_card=decks_with_any_resolved,
        unknown_card_names=len(unknown_names),
        distinct_subkinds=len(all_subkinds),
        ppmi_rows_written=0,  # filled in by caller after write
    )
    return corpus, stats


def _write_ppmi_rows(
    target_conn: sqlite3.Connection,
    table: dict[tuple[str, str], ppmi_math.PpmiEntry],
    *,
    now_iso: str,
) -> int:
    """Insert PPMI rows into ``forge_precon_ppmi``. Returns row count."""
    rows = [(entry.signature_a, entry.signature_b, entry.ppmi, entry.decks_count, now_iso) for entry in table.values()]
    target_conn.executemany(
        "INSERT OR REPLACE INTO forge_precon_ppmi "
        "(port_signature_a, port_signature_b, ppmi, decks_count, last_updated) "
        "VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    return len(rows)


def build_forge_oracle_db(
    *,
    synergy_db_path: Path,
    target_db_path: Path,
    deck_dirs: Iterable[Path] | None = None,
    min_decks_count: int = 3,
    smoothing_k: float = 0.0,
) -> IngestStats:
    """Build ``forge_oracle.db`` from Forge precon decks.

    Writes to a temp file ``<target>.tmp`` first, then atomic-renames
    on success — a build crash never leaves a half-written DB in place.

    ``synergy_db_path`` — our main ``mtg_synergy.db`` (source of
    ``cards`` + ``port_nodes`` view). Must exist; must have
    ``cards.oracle_id`` populated (i.e., imported with ``scryfall_db``).

    ``target_db_path`` — output path, typically ``data/forge_oracle.db``.

    ``deck_dirs`` — iterable of absolute directories to walk; defaults
    to Forge's ``quest/commanderprecons/`` + ``quest/precons/``.
    """
    if not synergy_db_path.is_file():
        raise FileNotFoundError(f"synergy DB not found at {synergy_db_path}")

    deck_dirs = (
        tuple(deck_dirs)
        if deck_dirs is not None
        else tuple((_REPO_ROOT / "data" / "forge" / d) for d in DEFAULT_DECK_DIRS)
    )
    deck_files = [p for directory in deck_dirs for p in dck_parser.iter_deck_files(directory)]
    _LOG.info("discovered %d .dck files across %d directories", len(deck_files), len(deck_dirs))

    source_conn = sqlite3.connect(synergy_db_path)
    try:
        name_to_oid = _load_name_to_oracle_id(source_conn)
        oid_to_subs = _load_oracle_id_to_subkinds(source_conn)
    finally:
        source_conn.close()

    corpus, stats = _deck_to_subkind_corpus(deck_files, name_to_oid, oid_to_subs)
    _LOG.info(
        "corpus: %d decks (%d with resolved cards), %d distinct subkinds, %d unknown names",
        stats.decks_parsed,
        stats.decks_with_any_resolved_card,
        stats.distinct_subkinds,
        stats.unknown_card_names,
    )

    ppmi_table = ppmi_math.compute_ppmi_table(
        corpus,
        min_decks_count=min_decks_count,
        smoothing_k=smoothing_k,
    )
    _LOG.info("computed %d PPMI rows (threshold decks_count>=%d)", len(ppmi_table), min_decks_count)

    # Write to temp file + atomic rename.
    tmp_path = target_db_path.with_suffix(target_db_path.suffix + ".tmp")
    if tmp_path.exists():
        tmp_path.unlink()
    target_db_path.parent.mkdir(parents=True, exist_ok=True)

    now_iso = datetime.now(UTC).isoformat(timespec="seconds")
    target_conn = sqlite3.connect(tmp_path)
    try:
        _init_db(target_conn)
        n_rows = _write_ppmi_rows(target_conn, ppmi_table, now_iso=now_iso)
        # Stamp the config hash so strict consumers can refuse stale DBs.
        config_inputs = fo_config.get_oracle_config_inputs(
            ppmi_smoothing_k=smoothing_k,
            min_decks_count=min_decks_count,
        )
        fo_config.write_oracle_config(target_conn, config_inputs)
        target_conn.execute(
            "INSERT OR REPLACE INTO oracle_config(key, value) VALUES ('built_at', ?)",
            (now_iso,),
        )
        target_conn.commit()
    except Exception:
        target_conn.close()
        # Clean up the temp file without letting a secondary error mask the
        # original write failure. ``missing_ok=True`` + try/except ensures the
        # original exception reaches the caller.
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError as unlink_exc:
            _LOG.warning("failed to clean up tmp file %s: %s", tmp_path, unlink_exc)
        raise
    else:
        target_conn.close()
        tmp_path.replace(target_db_path)

    return IngestStats(
        decks_parsed=stats.decks_parsed,
        decks_with_any_resolved_card=stats.decks_with_any_resolved_card,
        unknown_card_names=stats.unknown_card_names,
        distinct_subkinds=stats.distinct_subkinds,
        ppmi_rows_written=n_rows,
    )
