"""Unit 3 tests — scripts/build_embeddings.py end-to-end pipeline.

Tests the build script by invoking ``main()`` directly (no subprocess)
so coverage is collected and failures surface inline. A separate
integration test exercises the ``__main__`` entry-point via subprocess
to prove the standalone invocation contract.

The test DB is a 50-card fixture built via ``open_db()`` (real schema)
+ inline ``INSERT`` statements — ensures ``card_embeddings`` +
``card_embeddings_config`` are created by the schema, not hand-rolled.

Covers:

* 50 rows populated by a single run; ``config_hash`` present.
* Post-build ``verify_current_or_raise`` passes.
* Re-run replaces (not appends) — row count stays at 50.
* Same inputs → same ``config_hash`` (deterministic).
* Different ``--min-df`` → different ``config_hash``.
* Stdout summary line contains expected fields (via ``capsys``).
* Subprocess ``--help`` invocation succeeds.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import build_embeddings  # type: ignore[import-not-found]
import pytest

from mtg_synergy_graph.db import open_db
from mtg_synergy_graph.embeddings import (
    compute_embedding_hash,
    get_embedding_config_inputs,
    verify_current_or_raise,
)

# ``build_embeddings`` is importable because ``tests/conftest.py`` inserts
# ``scripts/`` on ``sys.path`` for in-process testing of script entry-points.

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "build_embeddings.py"


def _seed_50_card_fixture(db_path: Path) -> None:
    """Populate ``db_path`` with 50 diverse cards via the real schema.

    Applies the full ``schema.sql`` via ``open_db`` so
    ``card_embeddings`` + ``card_embeddings_config`` exist before the
    build script runs. Seeds a spread of ``card_ports`` +
    ``port_attributes`` rows so TF-IDF has a non-trivial vocabulary.
    """
    conn = open_db(db_path)
    try:
        port_types = ("trigger", "effect", "keyword")
        event_classes = ("ETB", "SpellCast", "Draw", "Sacrifice", "Attack")
        keywords_pool = (
            ["Flying"],
            ["Haste"],
            ["Trample"],
            ["Flying", "Vigilance"],
            ["Deathtouch"],
        )

        with conn:
            for i in range(50):
                name = f"Card_{i:03d}"
                kws = keywords_pool[i % len(keywords_pool)]
                conn.execute(
                    "INSERT INTO cards (name, keywords) VALUES (?, ?)",
                    (name, json.dumps(kws)),
                )
                # One port per card; vary port_type + event_class so
                # TF-IDF has tokens with df >= 2 that survive pruning.
                port_type = port_types[i % len(port_types)]
                event_class = event_classes[i % len(event_classes)]
                cur = conn.execute(
                    "INSERT INTO card_ports "
                    "(card_name, port_type, event_class, zone_origin, "
                    "zone_destination, counter_type, branch_kind) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        name,
                        port_type,
                        event_class,
                        "Hand" if i % 2 == 0 else None,
                        "Battlefield" if i % 3 == 0 else None,
                        "P1P1" if i % 5 == 0 else None,
                        "root" if i % 7 == 0 else None,
                    ),
                )
                port_id = int(cur.lastrowid or 0)
                # Add a couple of port_attributes per card so the attr:
                # token family populates the vocabulary.
                conn.execute(
                    "INSERT INTO port_attributes (port_id, attr_kind, attr_value) VALUES (?, ?, ?)",
                    (port_id, "type", "Creature" if i % 2 == 0 else "Instant"),
                )
                conn.execute(
                    "INSERT INTO port_attributes (port_id, attr_kind, attr_value) VALUES (?, ?, ?)",
                    (port_id, "subtype", "Goblin" if i % 3 == 0 else "Elf"),
                )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Happy-path scenarios
# ---------------------------------------------------------------------------


def test_build_populates_50_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "synergy.db"
    _seed_50_card_fixture(db_path)

    rc = build_embeddings.main(["--db", str(db_path)])
    assert rc == 0

    conn = open_db(db_path)
    try:
        count = conn.execute("SELECT COUNT(*) FROM card_embeddings").fetchone()[0]
        assert count == 50
    finally:
        conn.close()


def test_build_writes_config_hash_and_per_field_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "synergy.db"
    _seed_50_card_fixture(db_path)

    rc = build_embeddings.main(["--db", str(db_path)])
    assert rc == 0

    conn = open_db(db_path)
    try:
        rows = dict(conn.execute("SELECT key, value FROM card_embeddings_config").fetchall())
        assert "config_hash" in rows
        assert len(rows["config_hash"]) == 64
        # Per-field diagnostics present.
        from mtg_synergy_graph.port_graph import vocabulary as port_vocab

        assert rows["token_format_version"] == "v1"  # noqa: S105 — version tag
        assert rows["svd_dims"] == "128"
        assert rows["min_df"] == "2"
        assert rows["vectorizer_version"] == "1"
        # port_signature_version tracks VOCAB_VERSION (FU follow-on) so a
        # vocabulary bump invalidates stored embeddings.
        assert rows["port_signature_version"] == port_vocab.VOCAB_VERSION
        assert "built_at" in rows
    finally:
        conn.close()


def test_build_post_state_verifies_current(tmp_path: Path) -> None:
    db_path = tmp_path / "synergy.db"
    _seed_50_card_fixture(db_path)

    rc = build_embeddings.main(["--db", str(db_path)])
    assert rc == 0

    conn = open_db(db_path)
    try:
        # No exception = verification passed.
        inputs = get_embedding_config_inputs()
        verify_current_or_raise(conn, inputs)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Re-run / determinism / sensitivity
# ---------------------------------------------------------------------------


def test_rerun_overwrites_rows_not_appends(tmp_path: Path) -> None:
    db_path = tmp_path / "synergy.db"
    _seed_50_card_fixture(db_path)

    assert build_embeddings.main(["--db", str(db_path)]) == 0
    assert build_embeddings.main(["--db", str(db_path)]) == 0

    conn = open_db(db_path)
    try:
        count = conn.execute("SELECT COUNT(*) FROM card_embeddings").fetchone()[0]
        assert count == 50

        # Config keys exist exactly once each — no duplicates from the
        # second run layering on top of the first.
        keys = [row[0] for row in conn.execute("SELECT key FROM card_embeddings_config").fetchall()]
        assert len(keys) == len(set(keys))
    finally:
        conn.close()


def test_rerun_same_inputs_produces_same_config_hash(tmp_path: Path) -> None:
    db_path = tmp_path / "synergy.db"
    _seed_50_card_fixture(db_path)

    assert build_embeddings.main(["--db", str(db_path)]) == 0
    conn = open_db(db_path)
    try:
        hash_1 = conn.execute("SELECT value FROM card_embeddings_config WHERE key = 'config_hash'").fetchone()[0]
    finally:
        conn.close()

    assert build_embeddings.main(["--db", str(db_path)]) == 0
    conn = open_db(db_path)
    try:
        hash_2 = conn.execute("SELECT value FROM card_embeddings_config WHERE key = 'config_hash'").fetchone()[0]
    finally:
        conn.close()

    assert hash_1 == hash_2


def test_different_min_df_produces_different_config_hash(tmp_path: Path) -> None:
    db_path = tmp_path / "synergy.db"
    _seed_50_card_fixture(db_path)

    assert build_embeddings.main(["--db", str(db_path), "--min-df", "2"]) == 0
    conn = open_db(db_path)
    try:
        hash_min_df_2 = conn.execute("SELECT value FROM card_embeddings_config WHERE key = 'config_hash'").fetchone()[0]
    finally:
        conn.close()

    assert build_embeddings.main(["--db", str(db_path), "--min-df", "3"]) == 0
    conn = open_db(db_path)
    try:
        hash_min_df_3 = conn.execute("SELECT value FROM card_embeddings_config WHERE key = 'config_hash'").fetchone()[0]
    finally:
        conn.close()

    assert hash_min_df_2 != hash_min_df_3


def test_config_hash_matches_computed_from_inputs(tmp_path: Path) -> None:
    db_path = tmp_path / "synergy.db"
    _seed_50_card_fixture(db_path)

    assert build_embeddings.main(["--db", str(db_path), "--min-df", "3", "--svd-dims", "64"]) == 0

    expected_inputs = get_embedding_config_inputs()._replace(
        min_df=3,
        svd_dims=64,
    )
    expected_hash = compute_embedding_hash(expected_inputs)

    conn = open_db(db_path)
    try:
        stored = conn.execute("SELECT value FROM card_embeddings_config WHERE key = 'config_hash'").fetchone()[0]
    finally:
        conn.close()

    assert stored == expected_hash


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_build_on_small_fixture_db_is_no_op_friendly(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """DB with fewer than 10 cards prints summary without warnings."""
    db_path = tmp_path / "tiny.db"
    conn = open_db(db_path)
    try:
        with conn:
            for i in range(3):
                conn.execute(
                    "INSERT INTO cards (name, keywords) VALUES (?, ?)",
                    (f"Tiny_{i}", json.dumps(["Flying"])),
                )
                conn.execute(
                    "INSERT INTO card_ports (card_name, port_type, event_class) VALUES (?, ?, ?)",
                    (f"Tiny_{i}", "trigger", "ETB"),
                )
    finally:
        conn.close()

    rc = build_embeddings.main(["--db", str(db_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "embeddings: built" in out
    # No "warning" token in the summary path.
    assert "warning" not in out.lower()


def test_build_missing_db_returns_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    missing = tmp_path / "does_not_exist.db"
    rc = build_embeddings.main(["--db", str(missing)])
    assert rc == 2
    err = capsys.readouterr().err
    assert "does not exist" in err


def test_stdout_summary_contains_expected_fields(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "synergy.db"
    _seed_50_card_fixture(db_path)

    rc = build_embeddings.main(["--db", str(db_path)])
    assert rc == 0

    out = capsys.readouterr().out
    assert "built 50 vectors" in out
    assert "x 128 dims" in out
    assert "vocabulary=" in out
    assert "config_hash=" in out
    assert "wall=" in out
    assert out.rstrip().endswith("s")


# ---------------------------------------------------------------------------
# Subprocess integration test — proves ``__main__`` entry-point works
# ---------------------------------------------------------------------------


def test_help_invocation_succeeds() -> None:
    """``python scripts/build_embeddings.py --help`` exits 0 with usage text."""
    result = subprocess.run(  # noqa: S603 — hard-coded interpreter + script path
        [sys.executable, str(_SCRIPT_PATH), "--help"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "--db" in result.stdout
    assert "--min-df" in result.stdout
    assert "--svd-dims" in result.stdout


def test_subprocess_invocation_populates_rows(tmp_path: Path) -> None:
    """End-to-end via subprocess — the standalone CLI contract."""
    db_path = tmp_path / "synergy.db"
    _seed_50_card_fixture(db_path)

    result = subprocess.run(  # noqa: S603 — hard-coded interpreter + script path
        [sys.executable, str(_SCRIPT_PATH), "--db", str(db_path)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "built 50 vectors" in result.stdout

    conn = open_db(db_path)
    try:
        count = conn.execute("SELECT COUNT(*) FROM card_embeddings").fetchone()[0]
        assert count == 50
    finally:
        conn.close()
