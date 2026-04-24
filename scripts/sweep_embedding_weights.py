#!/usr/bin/env python3
"""Grid-search ``(w_emb, k)`` for the embedding-contribution flip decision.

Phase C of the activate-content-embeddings rollout. Scores the 100
golden-set commanders under each ``(w, k)`` cell and reports two
signals:

- **aggregate_hidden_gem_hit_rate** — the stated FR7 target. Higher =
  more mechanically-plausible picks that EDHREC's top-30 did not list.
- **aggregate_score_delta** — sum of absolute ``score_live -
  score_pinned`` across the top-100 pinned candidates per commander.
  A regression signal: near-zero means the embedding term barely
  perturbed the rule-based ranking; large values mean the embedding
  term dominated (typically a sign that ``w_emb`` is too large).

The winning cell maximizes ``aggregate_hidden_gem_hit_rate`` while
keeping ``aggregate_score_delta`` within a tolerance the caller picks
by eye. This script does NOT pick for you — it prints the table and
lets the human / orchestrator decide.

Does NOT commit, re-pin, or mutate ``data/synergy.db``. Reads:
``data/synergy.db``, ``data/tags.db``, and the pinned fixture under
``tests/fixtures/golden_set_run.json``.

Usage::

    uv run scripts/sweep_embedding_weights.py
    uv run scripts/sweep_embedding_weights.py --w-grid 0.05,0.1,0.2 --k-grid 0.6,0.8,1.0
    uv run scripts/sweep_embedding_weights.py --cells 0.1:0.8,0.2:0.8  # one-off cells
    uv run scripts/sweep_embedding_weights.py --commanders 20  # smaller golden set for quick iteration
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

import numpy as np

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from mtg_synergy_graph.bench.fixture import PinnedFixture, build_fixture  # noqa: E402
from mtg_synergy_graph.db import open_db  # noqa: E402
from mtg_synergy_graph.embeddings import commander_target as emb_cmdr_target  # noqa: E402
from mtg_synergy_graph.embeddings import contribution as emb_contribution  # noqa: E402


def _parse_grid(value: str) -> list[float]:
    """Parse ``'0.05,0.1,0.2'`` → ``[0.05, 0.1, 0.2]``."""
    return [float(x.strip()) for x in value.split(",") if x.strip()]


def _parse_cells(value: str) -> list[tuple[float, float]]:
    """Parse ``'0.1:0.8,0.2:0.8'`` → ``[(0.1, 0.8), (0.2, 0.8)]``."""
    cells: list[tuple[float, float]] = []
    for pair in value.split(","):
        w, k = pair.split(":")
        cells.append((float(w.strip()), float(k.strip())))
    return cells


def _aggregate_score_delta(live: PinnedFixture, pinned: PinnedFixture) -> float:
    """Sum of absolute score deltas across every pinned candidate.

    Walks the pinned fixture's top-100 per commander and subtracts the
    live score for that candidate. Candidates absent from the live
    fixture's top-100 are treated as ``0.0`` (they fell off the page);
    this slightly over-counts but gives a consistent upper bound.
    """
    pinned_by_cmdr = {e.commander: e for e in pinned.entries}
    live_by_cmdr = {e.commander: e for e in live.entries}
    total = 0.0
    for cmdr, pinned_entry in pinned_by_cmdr.items():
        live_entry = live_by_cmdr.get(cmdr)
        live_scores = live_entry.scores if live_entry is not None else {}
        for cand, pinned_score in pinned_entry.scores.items():
            live_score = live_scores.get(cand, 0.0)
            total += abs(live_score - pinned_score)
    return total


def _aggregate_hit_rate(live: PinnedFixture) -> tuple[float, int]:
    """Return ``(mean_hit_rate, n_measured)``; commanders without EDHREC data skipped."""
    rates: list[float] = []
    for entry in live.entries:
        rate = entry.legacy.get("hidden_gem_hit_rate")
        if rate is not None:
            rates.append(float(rate))
    if not rates:
        return 0.0, 0
    return sum(rates) / len(rates), len(rates)


def _install_vector_cache(vectors: dict[str, np.ndarray]) -> None:
    """Monkey-patch ``load_card_embeddings_verified`` to return a pre-loaded dict.

    Avoids re-reading the ``card_embeddings`` table once per commander
    per cell (100 × N_cells full-table scans otherwise). The replacement
    function ignores its ``conn`` argument and returns the pre-loaded
    dict. Caller responsibility: restore the original before exit.
    """

    def _cached_load(_conn: sqlite3.Connection) -> dict[str, np.ndarray]:
        return vectors

    emb_contribution.load_card_embeddings_verified = _cached_load  # type: ignore[assignment]

    # The scorer imports ``load_card_embeddings_verified`` lazily inside
    # ``score_all_universal`` via ``from .embeddings.contribution import
    # load_card_embeddings_verified``. Since each call re-executes that
    # binding at function scope, the monkeypatch on the module attribute
    # is picked up on every invocation.


def _score_one_cell(
    conn: sqlite3.Connection,
    edhrec_conn: sqlite3.Connection,
    commanders: list[str],
    pinned: PinnedFixture,
    w_emb: float,
    k: float,
) -> tuple[float, float, int, float]:
    """Score all ``commanders`` under ``(w_emb, k)``; return metrics.

    Returns ``(aggregate_hit_rate, aggregate_score_delta, n_measured,
    wall_time_seconds)``.
    """
    emb_contribution._ENABLE_EMBEDDING_CONTRIBUTION = True
    emb_contribution._EMBEDDING_W = w_emb
    emb_contribution._EMBEDDING_K = k

    # Clear the commander-target cache between cells so ``build_commander_target_vector``
    # re-composes under the new constants. Vectors themselves are stable across cells
    # (same DB), so the vector cache installed earlier stays valid.
    emb_cmdr_target.clear_cache()

    t0 = time.perf_counter()
    live = build_fixture(
        conn,
        commanders,
        existing=pinned,
        tensor_writer=None,
        edhrec_conn=edhrec_conn,
    )
    wall = time.perf_counter() - t0

    hit_rate, n_measured = _aggregate_hit_rate(live)
    score_delta = _aggregate_score_delta(live, pinned)
    return hit_rate, score_delta, n_measured, wall


def _build_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        type=Path,
        default=Path("data/synergy.db"),
        help="Path to synergy.db. Default: data/synergy.db",
    )
    parser.add_argument(
        "--edhrec-db",
        type=Path,
        default=Path("data/tags.db"),
        help="Path to EDHREC DB. Default: data/tags.db",
    )
    parser.add_argument(
        "--fixture",
        type=Path,
        default=Path("tests/fixtures/golden_set_run.json"),
        help="Path to pinned fixture. Default: tests/fixtures/golden_set_run.json",
    )
    parser.add_argument(
        "--w-grid",
        type=_parse_grid,
        default=[0.05, 0.1, 0.2],
        help="Comma-separated w_emb values. Default: 0.05,0.1,0.2",
    )
    parser.add_argument(
        "--k-grid",
        type=_parse_grid,
        default=[0.6, 0.8, 1.0],
        help="Comma-separated k values. Default: 0.6,0.8,1.0",
    )
    parser.add_argument(
        "--cells",
        type=_parse_cells,
        default=None,
        help="One-off (w:k) cells, comma-separated (e.g. 0.1:0.8,0.2:0.8). Overrides --w-grid/--k-grid.",
    )
    parser.add_argument(
        "--commanders",
        type=int,
        default=None,
        help="Subset to the first N commanders of the fixture for quick iteration.",
    )
    parser.add_argument(
        "--include-baseline",
        action="store_true",
        help="Also score a flag-off baseline cell for reference.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_argparser()
    args = parser.parse_args(argv)

    if not args.db.exists():
        print(f"error: --db path does not exist: {args.db}", file=sys.stderr)
        return 2
    if not args.edhrec_db.exists():
        print(f"error: --edhrec-db path does not exist: {args.edhrec_db}", file=sys.stderr)
        return 2
    if not args.fixture.exists():
        print(f"error: --fixture path does not exist: {args.fixture}", file=sys.stderr)
        return 2

    pinned = PinnedFixture.load(args.fixture)
    commanders = [e.commander for e in pinned.entries]
    if args.commanders is not None:
        commanders = commanders[: args.commanders]
        # Reduce pinned to the same subset so the score-delta comparison is apples-to-apples.
        pinned = PinnedFixture(
            config_hash=pinned.config_hash,
            created_at=pinned.created_at,
            schema_version=pinned.schema_version,
            entries=[e for e in pinned.entries if e.commander in set(commanders)],
        )

    conn = open_db(args.db)
    edhrec_conn = sqlite3.connect(args.edhrec_db)

    # Pre-load embedding vectors once; monkey-patch the loader to
    # return them so every per-commander scorer avoids a full-table
    # scan on ``card_embeddings``.
    print("[sweep] loading card_embeddings...", flush=True)
    from mtg_synergy_graph.embeddings.store import load_card_embeddings

    t_load = time.perf_counter()
    vectors = load_card_embeddings(conn)
    load_wall = time.perf_counter() - t_load
    print(f"[sweep] loaded {len(vectors)} vectors in {load_wall:.1f}s", flush=True)

    _install_vector_cache(vectors)

    cells: list[tuple[float, float]] = (
        args.cells if args.cells is not None else [(w, k) for w in args.w_grid for k in args.k_grid]
    )

    if args.include_baseline:
        baseline_k = args.k_grid[0] if args.k_grid else 0.8
        cells = [(0.0, baseline_k), *cells]

    print(f"[sweep] {len(commanders)} commanders x {len(cells)} cells", flush=True)
    print(f"[sweep] fixture config_hash: {pinned.config_hash[:12]}...", flush=True)
    print()
    print(f"{'w_emb':>8}  {'k':>6}  {'hit_rate':>10}  {'n':>4}  {'score_delta':>14}  {'wall(s)':>8}")
    print(f"{'-' * 8:>8}  {'-' * 6:>6}  {'-' * 10:>10}  {'-' * 4:>4}  {'-' * 14:>14}  {'-' * 8:>8}")

    results: list[tuple[float, float, float, int, float, float]] = []
    for w_emb, k in cells:
        if w_emb == 0.0:
            # Baseline: flag-off-equivalent.
            emb_contribution._ENABLE_EMBEDDING_CONTRIBUTION = False
        hit_rate, score_delta, n_measured, wall = _score_one_cell(
            conn,
            edhrec_conn,
            commanders,
            pinned,
            w_emb,
            k,
        )
        # Restore flag-on for subsequent cells (if baseline was first).
        emb_contribution._ENABLE_EMBEDDING_CONTRIBUTION = True
        results.append((w_emb, k, hit_rate, n_measured, score_delta, wall))
        print(
            f"{w_emb:>8.3f}  {k:>6.2f}  {hit_rate:>10.4f}  {n_measured:>4d}  {score_delta:>14.4f}  {wall:>8.1f}",
            flush=True,
        )

    print()
    pin_hit_rate, pin_n = _aggregate_hit_rate(pinned)
    print(f"[sweep] pinned fixture (flag-off): hit_rate={pin_hit_rate:.4f} n={pin_n}")

    flag_on_results = [r for r in results if r[0] != 0.0]
    if flag_on_results:
        # Advisory winner: highest hit_rate; ties broken by smallest score_delta.
        best = max(flag_on_results, key=lambda r: (r[2], -r[4]))
        print(
            f"[sweep] advisory winner: w={best[0]:.3f} k={best[1]:.2f} "
            f"hit_rate={best[2]:.4f} (pinned={pin_hit_rate:.4f}, "
            f"delta={best[2] - pin_hit_rate:+.4f}), score_delta={best[4]:.4f}"
        )

    conn.close()
    edhrec_conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
