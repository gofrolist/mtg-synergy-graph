#!/usr/bin/env python3
"""Export inference artifacts from the full training DB.

Builds a minimal synergy.db for the inference library (no cards table,
no interaction_edges, no training-only data). Also copies the model file
and pre-built edge caches.

Usage:
    python3 scripts/export_inference_db.py                    # Default: data/inference/
    python3 scripts/export_inference_db.py --output /path/to  # Custom output dir

Output:
    <output_dir>/
        synergy.db              (~75 MB) Forge data + strategies + EDHREC
        edge_index_cache.npz    (~270 MB) Raw edge arrays
        edge_adj_cache.npz      (~140 MB) Pre-built adjacency dicts
        fusion_model_forge.lgb  (~45 MB) LightGBM model
"""
import argparse
import os
import shutil
import sqlite3
import sys
import time


_ALLOWED_EXPORT_TABLES = frozenset({
    "forge_abilities", "forge_deck_tags", "forge_name_map",
    "card_strategies", "edhrec_card_synergy",
})


def _copy_table(src: sqlite3.Connection, dst: sqlite3.Connection,
                table: str) -> int:
    """Copy a table from src to dst. Returns row count."""
    if table not in _ALLOWED_EXPORT_TABLES:
        raise ValueError(f"Disallowed table name: {table!r}")

    # Get CREATE TABLE statement (table name already validated against allowlist)
    create_sql = src.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (table,)
    ).fetchone()
    if not create_sql or not create_sql[0]:
        print(f"  WARNING: table '{table}' not found in source DB, skipping")
        return 0

    dst.execute(create_sql[0])

    # Copy data — quote column names for safety even though they're DB-internal
    cols = [r[1] for r in src.execute(f"PRAGMA table_info({table})")]
    col_list = ", ".join(f'"{c}"' for c in cols)
    ph = ", ".join("?" * len(cols))

    count = 0
    batch = []
    for row in src.execute(f"SELECT {col_list} FROM {table}"):
        batch.append(row)
        if len(batch) >= 10000:
            dst.executemany(f"INSERT INTO {table} ({col_list}) VALUES ({ph})", batch)
            count += len(batch)
            batch = []
    if batch:
        dst.executemany(f"INSERT INTO {table} ({col_list}) VALUES ({ph})", batch)
        count += len(batch)

    return count


def _copy_indexes(src: sqlite3.Connection, dst: sqlite3.Connection, table: str):
    """Copy non-autoindex indexes for a table."""
    for row in src.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? "
        "AND sql IS NOT NULL AND name NOT LIKE 'sqlite_autoindex_%'",
        (table,)
    ):
        try:
            dst.execute(row[0])
        except sqlite3.OperationalError:
            pass  # index already exists


def export_inference_db(tags_db_path: str, output_dir: str):
    """Build synergy.db and copy artifacts to output_dir."""
    t0 = time.time()
    os.makedirs(output_dir, exist_ok=True)

    src = sqlite3.connect(tags_db_path)
    dst_path = os.path.join(output_dir, "synergy.db")

    # Remove old export if exists
    if os.path.exists(dst_path):
        os.remove(dst_path)

    dst = sqlite3.connect(dst_path)
    dst.execute("PRAGMA journal_mode=WAL")
    dst.execute("PRAGMA synchronous=NORMAL")

    # Tables to export (inference-only — no cards, no interaction_edges, no combos)
    tables = [
        "forge_abilities",
        "forge_deck_tags",
        "forge_name_map",
        "card_strategies",
        "edhrec_card_synergy",
    ]

    print("Exporting tables to synergy.db:")
    for table in tables:
        count = _copy_table(src, dst, table)
        _copy_indexes(src, dst, table)
        print(f"  {table}: {count:,} rows")

    dst.execute("ANALYZE")
    dst.commit()
    dst.execute("VACUUM")
    dst.close()
    src.close()

    dst_size = os.path.getsize(dst_path) / 1024 / 1024
    print(f"\nsynergy.db: {dst_size:.1f} MB")

    # Copy binary artifacts
    data_dir = os.path.dirname(tags_db_path)
    artifacts = [
        "edge_index_cache.npz",
        "edge_adj_cache.npz",
        "fusion_model_forge.lgb",
    ]

    print("\nCopying artifacts:")
    for artifact in artifacts:
        src_path = os.path.join(data_dir, artifact)
        dst_artifact = os.path.join(output_dir, artifact)
        if os.path.exists(src_path):
            shutil.copy2(src_path, dst_artifact)
            size = os.path.getsize(dst_artifact) / 1024 / 1024
            print(f"  {artifact}: {size:.1f} MB")
        else:
            print(f"  {artifact}: NOT FOUND (run training first)")

    # Total size
    total = sum(
        os.path.getsize(os.path.join(output_dir, f))
        for f in os.listdir(output_dir)
        if os.path.isfile(os.path.join(output_dir, f))
    )
    elapsed = time.time() - t0
    print(f"\nTotal: {total / 1024 / 1024:.1f} MB in {elapsed:.1f}s")
    print(f"Output: {output_dir}")


def main():
    parser = argparse.ArgumentParser(description="Export inference artifacts from training DB")
    parser.add_argument("--output", "-o", default=None,
                        help="Output directory (default: data/inference/)")
    parser.add_argument("--db", default=None,
                        help="Source tags.db path (default: data/tags.db)")
    args = parser.parse_args()

    # Resolve paths
    script_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(script_dir)
    data_dir = os.path.join(project_root, "data")

    db_path = args.db or os.path.join(data_dir, "tags.db")
    output_dir = args.output or os.path.join(data_dir, "inference")

    if not os.path.exists(db_path):
        print(f"ERROR: Source DB not found: {db_path}")
        sys.exit(1)

    export_inference_db(db_path, output_dir)


if __name__ == "__main__":
    main()
