#!/usr/bin/env python3
"""Build the ``card_embeddings`` table from structured per-card features.

Standalone user-invoked build step (plan 2026-04-23-003 Unit 3). Mirrors
``scripts/build_graph_cache.py`` — the importer does NOT invoke this;
users run it manually after each Forge cardsfolder refresh (or when the
vectorizer version is bumped).

Pipeline:

1. ``open_db(db_path)`` to apply the v1.2.2 schema (creates
   ``card_embeddings`` + ``card_embeddings_config`` if absent).
2. ``extract_card_tokens(conn)`` — structured feature bag per card.
3. ``compute_tfidf(corpus, min_df=...)`` — TF-IDF over the corpus.
4. ``truncated_svd(tfidf, k=svd_dims)`` — project to fixed dim + L2.
5. ``write_vectors(conn, vectors, inputs)`` — one atomic transaction
   that wipes and re-populates both embedding tables.

Usage:

    uv run scripts/build_embeddings.py
    uv run scripts/build_embeddings.py --db data/synergy.db
    uv run scripts/build_embeddings.py --min-df 3 --svd-dims 64
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

# scripts/ is not a package; the sys.path shim mirrors the precedent from
# other scripts in this directory so ``from mtg_synergy_graph...`` imports
# resolve when invoked as ``uv run scripts/build_embeddings.py``.
_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from mtg_synergy_graph.db import open_db  # noqa: E402 — after sys.path shim
from mtg_synergy_graph.embeddings import (  # noqa: E402 — after sys.path shim
    compute_tfidf,
    extract_card_tokens,
    get_embedding_config_inputs,
    truncated_svd,
    write_vectors,
)
from mtg_synergy_graph.embeddings.config import (  # noqa: E402 — after sys.path shim
    _DEFAULT_MIN_DF,
    _DEFAULT_SVD_DIMS,
)


def _build_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        type=Path,
        default=Path("data/synergy.db"),
        help="Path to the synergy.db. Default: data/synergy.db",
    )
    parser.add_argument(
        "--min-df",
        type=int,
        default=_DEFAULT_MIN_DF,
        help=f"Minimum document frequency for TF-IDF pruning. Default: {_DEFAULT_MIN_DF}",
    )
    parser.add_argument(
        "--svd-dims",
        type=int,
        default=_DEFAULT_SVD_DIMS,
        help=f"Target dimensionality after truncated SVD. Default: {_DEFAULT_SVD_DIMS}",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_argparser()
    args = parser.parse_args(argv)

    if not args.db.exists():
        print(f"error: {args.db} does not exist", file=sys.stderr)
        return 2

    start = time.perf_counter()
    try:
        conn = open_db(args.db)
        try:
            corpus = extract_card_tokens(conn)
            tfidf_result = compute_tfidf(corpus, min_df=args.min_df)
            projected = truncated_svd(tfidf_result.tfidf_matrix, k=args.svd_dims)

            # Pair each row with its card name. ``card_names`` is sorted
            # ascending and matches the row order of ``projected`` by
            # construction (see ``TfidfResult`` in embeddings.vectorizer).
            vectors = {name: projected[i] for i, name in enumerate(tfidf_result.card_names)}

            # Start from the ambient defaults, then override the two
            # CLI-tunable knobs so the config hash reflects the actual
            # build inputs (not the defaults).
            inputs = get_embedding_config_inputs()._replace(
                min_df=args.min_df,
                svd_dims=args.svd_dims,
            )
            write_vectors(conn, vectors, inputs)

            elapsed = time.perf_counter() - start
            config_hash_row = conn.execute(
                "SELECT value FROM card_embeddings_config WHERE key = 'config_hash'"
            ).fetchone()
            config_hash = config_hash_row[0] if config_hash_row else "?"
            print(
                f"embeddings: built {len(vectors)} vectors "
                f"x {args.svd_dims} dims, "
                f"vocabulary={len(tfidf_result.vocabulary)}, "
                f"config_hash={config_hash[:8]}..., "
                f"wall={elapsed:.2f}s"
            )
        finally:
            conn.close()
    except (SystemExit, KeyboardInterrupt):
        raise
    except Exception as exc:
        print(f"error: build_embeddings failed: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
