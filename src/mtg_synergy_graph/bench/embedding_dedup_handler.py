"""``bench.py audit --embedding-dedup`` handler (plan 003 Unit 6).

Glues ``embeddings.dedup.compute_embedding_dedup`` to:

* the persisted ``rule_contributions`` tensor under the current
  scoring ``config_hash``,
* ``card_embeddings`` via
  ``embeddings.store.load_card_embeddings`` + the strict
  ``embeddings.config.verify_current_or_raise`` guard, and
* a markdown / JSON renderer that writes to ``.audit/last_dedup.md``
  as a side effect (matches ``--collinearity`` conventions).

Exit-code discipline follows plan D7 (offline-strict diagnostic):
* 0 — success; report on stdout + file.
* 2 — precondition missing (no tensor rows / no embeddings / stale
  embedding config). Every 2-exit path prints an actionable rebuild
  hint to stderr.

The handler never raises — any unexpected error falls into a
top-level ``except Exception`` that logs to stderr and returns 2.
This keeps the CLI from emitting a traceback on an audit machine.

Plan: docs/plans/2026-04-23-003-feat-content-embeddings-fallback-plan.md Unit 6.
"""

from __future__ import annotations

import argparse
import json as _json
import sys
from pathlib import Path

from mtg_synergy_graph.bench.tensor import compute_config_hash
from mtg_synergy_graph.db import open_db
from mtg_synergy_graph.embeddings.config import (
    EmbeddingConfigMissingError,
    EmbeddingConfigStaleError,
    get_embedding_config_inputs,
    verify_current_or_raise,
)
from mtg_synergy_graph.embeddings.dedup import DedupPair, compute_embedding_dedup
from mtg_synergy_graph.embeddings.store import load_card_embeddings

#: Relative path to the side-effect markdown mirror of the report. We
#: always write a markdown copy (even when ``--format json`` is chosen
#: for stdout) to match ``--collinearity``'s ``.audit/last.md``
#: convention: humans skim ``.audit/`` after each run, not stdout
#: captures.
_AUDIT_SIDE_EFFECT_PATH: str = ".audit/last_dedup.md"


def _render_markdown(pairs: list[DedupPair], threshold: float, min_activation: int) -> str:
    """Human-readable markdown table matching the ``--collinearity`` style."""
    lines: list[str] = []
    lines.append("# bench.py audit --embedding-dedup")
    lines.append(f"threshold: {threshold:.3f}")
    lines.append(f"min_activation: {min_activation}")
    lines.append(f"pairs_flagged: {len(pairs)}")
    lines.append("")

    if not pairs:
        lines.append(f"No rule pairs exceed cosine > {threshold:.3f}.")
        return "\n".join(lines) + "\n"

    header = f"{'rule_a':<40} {'rule_b':<40} {'cosine':>8} {'act_a':>6} {'act_b':>6}"
    lines.append(header)
    lines.append("-" * len(header))
    for pair in pairs:
        lines.append(
            f"{pair.rule_a[:40]:<40} {pair.rule_b[:40]:<40} "
            f"{pair.cosine:>8.4f} {pair.activation_a_size:>6d} {pair.activation_b_size:>6d}"
        )
    return "\n".join(lines) + "\n"


def _render_json(pairs: list[DedupPair]) -> str:
    """Structured JSON output mirroring ``handle_collinearity``."""
    payload = [
        {
            "rule_a": pair.rule_a,
            "rule_b": pair.rule_b,
            "cosine": pair.cosine,
            "activation_a_size": pair.activation_a_size,
            "activation_b_size": pair.activation_b_size,
        }
        for pair in pairs
    ]
    return _json.dumps(payload, indent=2, ensure_ascii=False)


def _write_side_effect(markdown: str) -> None:
    """Mirror the markdown report to ``.audit/last_dedup.md``.

    Creates the parent directory if missing so a fresh checkout
    without a prior audit run doesn't crash here. Failures to write
    (e.g., permissions) propagate up to the top-level exception
    handler in :func:`handle_embedding_dedup`.
    """
    path = Path(_AUDIT_SIDE_EFFECT_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(markdown, encoding="utf-8")


def handle_embedding_dedup(args: argparse.Namespace) -> int:
    """Flag rule pairs whose candidate-activation sets are near-parallel
    in embedding space.

    Exit codes:

    * ``0`` — success. Report rendered on stdout + mirrored to
      ``.audit/last_dedup.md``.
    * ``2`` — precondition missing or unexpected failure. Every ``2``
      path prints an actionable stderr hint.

    Preconditions (checked in order):

    1. ``rule_contributions`` has rows under the current scoring
       ``config_hash`` — rebuild via ``bench.py audit --repin --yes``.
    2. ``card_embeddings`` is populated — rebuild via
       ``scripts/build_embeddings.py``.
    3. ``card_embeddings_config`` hash matches the current embedding
       config (strict: stale hash fails loud, per plan D7).
    """
    threshold: float = float(getattr(args, "threshold", 0.95))
    min_activation: int = int(getattr(args, "min_activation", 20))

    try:
        conn = open_db(args.db)
    except Exception as exc:  # pragma: no cover — DB open failure is exceptional
        print(f"error: could not open DB at {args.db!r}: {exc}", file=sys.stderr)
        return 2

    try:
        try:
            # Precondition 1: tensor rows for current scoring config.
            scoring_hash = compute_config_hash()
            tensor_row_count = conn.execute(
                "SELECT COUNT(*) FROM rule_contributions WHERE config_hash = ?",
                (scoring_hash,),
            ).fetchone()[0]
            if tensor_row_count == 0:
                print(
                    "error: no rule_contributions rows for current scoring "
                    f"config_hash={scoring_hash[:12]}.... "
                    "Rebuild with `uv run scripts/bench.py audit --repin --yes`.",
                    file=sys.stderr,
                )
                return 2

            # Precondition 2: embeddings present.
            vectors = load_card_embeddings(conn)
            if not vectors:
                print(
                    "error: no card_embeddings available. Rebuild with `uv run scripts/build_embeddings.py`.",
                    file=sys.stderr,
                )
                return 2

            # Precondition 3: embedding config hash matches current.
            try:
                verify_current_or_raise(conn, get_embedding_config_inputs())
            except (EmbeddingConfigStaleError, EmbeddingConfigMissingError) as exc:
                # Both error messages already contain the rebuild
                # command; printing them verbatim keeps the CLI hint
                # in one place (the config module).
                print(f"error: {exc}", file=sys.stderr)
                return 2

            # Tensor rows — commander/candidate/rule_id/contribution,
            # ``contribution > 0`` so we only count positive firings.
            # Matches the FR5 spec wording ("flags rule pairs whose
            # candidate-activation sets are near-parallel in embedding
            # space") — a rule that fires a zero-or-negative
            # contribution is "not really covering the candidate".
            rows = conn.execute(
                "SELECT commander, candidate, rule_id, contribution "
                "FROM rule_contributions "
                "WHERE config_hash = ? AND contribution > 0",
                (scoring_hash,),
            ).fetchall()

            # The cursor returns Row objects; convert to plain tuples
            # so ``compute_embedding_dedup``'s type signature matches
            # its unit tests (Sequence[tuple[...]]).
            tensor_rows: list[tuple[str, str, str, float]] = [
                (r["commander"], r["candidate"], r["rule_id"], float(r["contribution"])) for r in rows
            ]
        finally:
            conn.close()
    except Exception as exc:
        print(
            f"error: unexpected failure while preparing dedup inputs: {exc}",
            file=sys.stderr,
        )
        return 2

    try:
        pairs = compute_embedding_dedup(
            tensor_rows,
            vectors,
            threshold=threshold,
            min_activation=min_activation,
        )

        # Always render the markdown report (even when the user
        # requested JSON on stdout) so ``.audit/last_dedup.md`` is a
        # durable human-readable record. Matches the side-effect
        # discipline in ``handle_collinearity``.
        markdown = _render_markdown(pairs, threshold=threshold, min_activation=min_activation)
        _write_side_effect(markdown)

        fmt = getattr(args, "format", "md")
        rendered = _render_json(pairs) if fmt == "json" else markdown
        # stdout for the rendered output; stderr for confirmations.
        print(rendered)
        print(
            f"bench.py audit --embedding-dedup: report written to {_AUDIT_SIDE_EFFECT_PATH}",
            file=sys.stderr,
        )
        return 0
    except Exception as exc:
        print(
            f"error: unexpected failure during dedup rendering: {exc}",
            file=sys.stderr,
        )
        return 2
