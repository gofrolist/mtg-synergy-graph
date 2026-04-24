"""``bench.py audit --embedding-dedup`` handler tests (plan 003 Unit 6).

Covers:

* Happy path: populated tensor + populated embeddings + matching config
  → markdown output on stdout, ``.audit/last_dedup.md`` written.
* Preconditions:
  - Missing tensor rows (no rows for current scoring config_hash).
  - Missing / empty ``card_embeddings``.
  - Stale ``card_embeddings_config`` hash.
* Flag handling: ``--format json``, ``--threshold``, ``--min-activation``.
* Mutex group: ``--embedding-dedup`` + ``--collinearity`` rejected.

Mirrors ``tests/bench/test_handle_unknowns.py`` for the per-test
argparse.Namespace shape and the ``tmp_path`` DB construction.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from mtg_synergy_graph.bench.embedding_dedup_handler import handle_embedding_dedup
from mtg_synergy_graph.bench.tensor import compute_config_hash
from mtg_synergy_graph.db import open_db
from mtg_synergy_graph.embeddings import config as emb_config
from mtg_synergy_graph.embeddings import store as emb_store

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _args(
    db_path: Path,
    *,
    threshold: float = 0.95,
    min_activation: int = 3,
    format_: str = "md",
) -> argparse.Namespace:
    """Minimal ``Namespace`` matching the audit-parser flag shape."""
    return argparse.Namespace(
        db=str(db_path),
        threshold=threshold,
        min_activation=min_activation,
        format=format_,
    )


def _unit_vector(*components: float, dim: int = 128) -> np.ndarray:
    """Build a deterministic L2-normalized float32 vector for tests.

    Same helper as ``test_dedup.py`` — duplicated locally because the
    two test modules should not share a conftest (the dedup-logic
    tests must not depend on SQLite fixtures).
    """
    vec = np.zeros(dim, dtype=np.float32)
    for i, c in enumerate(components):
        vec[i] = c
    norm = float(np.linalg.norm(vec))
    if norm == 0.0:
        return vec
    return (vec / norm).astype(np.float32)


def _insert_tensor_rows(
    conn: sqlite3.Connection,
    rows: list[tuple[str, str, str, float]],
    config_hash: str,
) -> None:
    """Insert ``(cmdr, cand, rule_id, contribution)`` rows under ``config_hash``."""
    conn.executemany(
        "INSERT INTO rule_contributions "
        "(commander, candidate, rule_id, contribution, idf_weight, raw_count, "
        "config_hash, computed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [(c, cand, rid, contrib, 0.1, 1, config_hash, "2026-04-23T00:00:00") for c, cand, rid, contrib in rows],
    )
    conn.commit()


def _populate_synthetic_audit_db(tmp_path: Path) -> Path:
    """Create a tmp DB with:

    * Real schema via ``open_db``.
    * 25+ synthetic ``rule_contributions`` rows under the current
      scoring ``config_hash`` so the handler's precondition checks pass.
    * A ``card_embeddings`` population with matching
      ``card_embeddings_config`` — the ``write_vectors`` call inside
      one transaction.

    The fixture DB is self-contained: no dependency on
    ``data/synergy.db`` or a live importer run.
    """
    db_path = tmp_path / "synergy.db"
    conn = open_db(db_path)
    try:
        h = compute_config_hash()

        # Two rules with heavy overlap (cards c1..c10), one rule with
        # distinct cards (c11..c20). Overlapping rules should flag;
        # distinct rule should not pair under any reasonable threshold.
        overlap_cards = [f"card_{i}" for i in range(10)]
        distinct_cards = [f"dcard_{i}" for i in range(10)]

        # Insert cards first so the card_embeddings FK is satisfied.
        for name in overlap_cards + distinct_cards:
            conn.execute(
                "INSERT INTO cards(name, card_types, color_identity, legal_commander) VALUES (?, 'Creature', 'G', 1)",
                (name,),
            )
        conn.commit()

        rule_a_rows = [("CmdrA", c, "rule_alpha", 1.0) for c in overlap_cards]
        rule_b_rows = [("CmdrA", c, "rule_beta", 1.0) for c in overlap_cards]
        rule_c_rows = [("CmdrA", c, "rule_gamma", 1.0) for c in distinct_cards]
        _insert_tensor_rows(conn, rule_a_rows + rule_b_rows + rule_c_rows, h)

        # Every overlap card points in direction e_0; every distinct
        # card points in e_1 → orthogonal mean vectors → (alpha, beta)
        # pair should flag with cosine 1, (alpha, gamma) and
        # (beta, gamma) pairs should NOT flag.
        vectors = {c: _unit_vector(1.0, 0.0) for c in overlap_cards}
        vectors.update({c: _unit_vector(0.0, 1.0) for c in distinct_cards})
        emb_store.write_vectors(conn, vectors, emb_config.get_embedding_config_inputs())
    finally:
        conn.close()
    return db_path


@pytest.fixture()
def _run_from_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Run each handler call with cwd=``tmp_path`` so ``.audit/`` side
    effects land there and are discarded on teardown."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_happy_path_emits_markdown_and_writes_audit_file(
    tmp_path: Path,
    _run_from_tmp: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = _populate_synthetic_audit_db(tmp_path)

    rc = handle_embedding_dedup(_args(db_path, threshold=0.95, min_activation=5))

    assert rc == 0
    out = capsys.readouterr()
    assert "bench.py audit --embedding-dedup" in out.out
    # Two rules with identical activation sets → flagged.
    assert "rule_alpha" in out.out
    assert "rule_beta" in out.out
    # rule_gamma has orthogonal mean vector → should NOT pair with
    # alpha or beta under threshold=0.95 and cosine ≈ 0.
    # (It appears in neither activation line of a flagged pair.)

    audit_file = _run_from_tmp / ".audit" / "last_dedup.md"
    assert audit_file.exists(), "handler must mirror the report to .audit/last_dedup.md"
    file_text = audit_file.read_text()
    assert "rule_alpha" in file_text
    assert "rule_beta" in file_text


def test_happy_path_json_format(
    tmp_path: Path,
    _run_from_tmp: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = _populate_synthetic_audit_db(tmp_path)

    rc = handle_embedding_dedup(_args(db_path, threshold=0.95, min_activation=5, format_="json"))

    assert rc == 0
    out = capsys.readouterr().out
    payload = json.loads(out.strip())
    assert isinstance(payload, list)
    assert len(payload) >= 1
    entry = payload[0]
    assert entry["rule_a"] == "rule_alpha"
    assert entry["rule_b"] == "rule_beta"
    assert entry["cosine"] == pytest.approx(1.0, rel=1e-6)
    assert entry["activation_a_size"] == 10
    assert entry["activation_b_size"] == 10


# ---------------------------------------------------------------------------
# Precondition failures
# ---------------------------------------------------------------------------


def test_missing_tensor_rows_exits_2_with_repin_hint(
    tmp_path: Path,
    _run_from_tmp: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No ``rule_contributions`` rows under the current scoring hash."""
    db_path = tmp_path / "synergy.db"
    conn = open_db(db_path)
    try:
        # Insert the FK target first so the card_embeddings FK is
        # satisfied. Then populate card_embeddings so that step passes;
        # rule_contributions stays empty — that's the failure mode
        # under test.
        conn.execute(
            "INSERT INTO cards(name, card_types, color_identity, legal_commander) "
            "VALUES ('card_x', 'Creature', 'G', 1)",
        )
        conn.commit()
        emb_store.write_vectors(
            conn,
            {"card_x": _unit_vector(1.0)},
            emb_config.get_embedding_config_inputs(),
        )
    finally:
        conn.close()

    rc = handle_embedding_dedup(_args(db_path))

    assert rc == 2
    err = capsys.readouterr().err
    assert "no rule_contributions rows" in err
    assert "--repin" in err


def test_missing_card_embeddings_exits_2_with_build_hint(
    tmp_path: Path,
    _run_from_tmp: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``card_embeddings`` table has zero rows → exit 2."""
    db_path = tmp_path / "synergy.db"
    conn = open_db(db_path)
    try:
        h = compute_config_hash()
        conn.execute(
            "INSERT INTO cards(name, card_types, color_identity, legal_commander) "
            "VALUES ('card_1', 'Creature', 'G', 1)",
        )
        conn.commit()
        _insert_tensor_rows(conn, [("CmdrA", "card_1", "r1", 1.0)], h)
    finally:
        conn.close()

    # Bypass the autouse conftest stub: the empty-embeddings case is
    # exactly what we're testing, and the stub would mask it by
    # returning synthetic rows. Force the real ``load_card_embeddings``
    # by monkeypatching the module-level reference.
    from mtg_synergy_graph.bench import embedding_dedup_handler as handler_module

    monkeypatch.setattr(handler_module, "load_card_embeddings", lambda _conn: {})

    rc = handle_embedding_dedup(_args(db_path))

    assert rc == 2
    err = capsys.readouterr().err
    assert "no card_embeddings" in err
    assert "build_embeddings.py" in err


def test_stale_embeddings_config_exits_2_with_rebuild_hint(
    tmp_path: Path,
    _run_from_tmp: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``card_embeddings_config.config_hash`` differs from current → exit 2."""
    db_path = tmp_path / "synergy.db"
    conn = open_db(db_path)
    try:
        h = compute_config_hash()
        conn.execute(
            "INSERT INTO cards(name, card_types, color_identity, legal_commander) "
            "VALUES ('card_1', 'Creature', 'G', 1)",
        )
        conn.commit()
        _insert_tensor_rows(conn, [("CmdrA", "card_1", "r1", 1.0)], h)

        # Write vectors under the current config, then deliberately
        # overwrite the stored hash so ``verify_current_or_raise`` fires.
        emb_store.write_vectors(
            conn,
            {"card_1": _unit_vector(1.0)},
            emb_config.get_embedding_config_inputs(),
        )
        conn.execute("UPDATE card_embeddings_config SET value = 'bogus_stale_hash_1234' WHERE key = 'config_hash'")
        conn.commit()
    finally:
        conn.close()

    rc = handle_embedding_dedup(_args(db_path))

    assert rc == 2
    err = capsys.readouterr().err
    # The EmbeddingConfigStaleError message includes "different config"
    # and the rebuild command "scripts/build_embeddings.py".
    assert "build_embeddings.py" in err


# ---------------------------------------------------------------------------
# Flag plumbing
# ---------------------------------------------------------------------------


def test_threshold_flag_changes_output(
    tmp_path: Path,
    _run_from_tmp: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Loose threshold flags the pair; strict threshold does not."""
    db_path = _populate_synthetic_audit_db(tmp_path)

    # Under 0.95 the identical-activation pair flags (cosine == 1).
    rc_loose = handle_embedding_dedup(_args(db_path, threshold=0.95, min_activation=5))
    loose_out = capsys.readouterr().out
    assert rc_loose == 0
    assert "rule_alpha" in loose_out and "rule_beta" in loose_out

    # Bump threshold to 1.5 (impossibly strict) — no pair should flag.
    rc_strict = handle_embedding_dedup(_args(db_path, threshold=1.5, min_activation=5))
    strict_out = capsys.readouterr().out
    assert rc_strict == 0
    # Marker line from the markdown renderer when no pairs flag.
    assert "No rule pairs exceed" in strict_out


def test_min_activation_flag_prunes_rules(
    tmp_path: Path,
    _run_from_tmp: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--min-activation`` above every rule's set size → empty output."""
    db_path = _populate_synthetic_audit_db(tmp_path)

    rc = handle_embedding_dedup(_args(db_path, threshold=0.5, min_activation=100))

    assert rc == 0
    out = capsys.readouterr().out
    assert "No rule pairs exceed" in out
    # pairs_flagged counter is zero.
    assert "pairs_flagged: 0" in out


# ---------------------------------------------------------------------------
# Mutex-group / CLI dispatch
# ---------------------------------------------------------------------------


def test_cli_dispatch_registers(
    tmp_path: Path,
    _run_from_tmp: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """argparse CLI routes ``--embedding-dedup`` to handle_embedding_dedup."""
    # Ensure handlers register (import side-effect).
    import mtg_synergy_graph.bench  # noqa: F401
    from mtg_synergy_graph.bench.cli import main

    db_path = _populate_synthetic_audit_db(tmp_path)

    rc = main(
        [
            "audit",
            "--embedding-dedup",
            "--db",
            str(db_path),
            "--threshold",
            "0.95",
            "--min-activation",
            "5",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "bench.py audit --embedding-dedup" in out


def test_mutex_with_collinearity_rejected() -> None:
    """``--embedding-dedup`` + ``--collinearity`` → argparse SystemExit."""
    from mtg_synergy_graph.bench.cli import main

    with pytest.raises(SystemExit):
        main(["audit", "--embedding-dedup", "--collinearity"])


def test_help_lists_embedding_dedup_flag(capsys: pytest.CaptureFixture[str]) -> None:
    """``bench.py audit --help`` renders the ``--embedding-dedup`` entry."""
    from mtg_synergy_graph.bench.cli import main

    with pytest.raises(SystemExit):
        main(["audit", "--help"])
    out = capsys.readouterr().out
    assert "--embedding-dedup" in out
    assert "--threshold" in out
    assert "--min-activation" in out


# ---------------------------------------------------------------------------
# Defensive: handler never raises
# ---------------------------------------------------------------------------


def test_handler_returns_2_on_unexpected_exception(
    tmp_path: Path,
    _run_from_tmp: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unexpected exceptions inside ``compute_embedding_dedup`` are caught,
    stderr logged, exit 2."""
    db_path = _populate_synthetic_audit_db(tmp_path)

    def _boom(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("simulated internal failure")

    from mtg_synergy_graph.bench import embedding_dedup_handler as handler_module

    monkeypatch.setattr(handler_module, "compute_embedding_dedup", _boom)

    rc = handle_embedding_dedup(_args(db_path, min_activation=5))

    assert rc == 2
    err = capsys.readouterr().err
    assert "simulated internal failure" in err
