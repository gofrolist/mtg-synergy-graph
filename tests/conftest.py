"""Shared fixtures for the mtg_synergy_graph test suite."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import numpy as np
import pytest

# scripts/ is not a package but test files import gap_report / forge_oracle /
# scaffold_rule from it for in-process testing. Centralize the sys.path mutation
# here so individual test modules don't each carry their own copy (plan 002
# code-review finding KP-007).
_SCRIPTS_DIR = str(Path(__file__).resolve().parent.parent / "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from mtg_synergy_graph import parse_card_file  # noqa: E402 — after sys.path mutation

FIXTURES_DIR = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# Forge-Second-Oracle (plan 002) — CI-friendly shim
# ---------------------------------------------------------------------------
#
# forge_oracle tests call ``fo_version.read_current_forge_sha()`` indirectly
# via ``get_oracle_config_inputs()`` from many code paths (build, verify,
# handler, propose-rules). The real function shells out to
# ``git -C data/forge rev-parse HEAD``, which requires the vendored partial
# clone at ``data/forge/`` — present on developer machines (setup per
# ``docs/FORGE_ORACLE.md``) but absent in CI runners (``data/`` is
# ``.gitignore``-ed and the workflow does not clone Forge).
#
# The autouse fixture below detects a missing ``data/forge/.git`` and stubs
# ``read_current_forge_sha`` to return the pinned SHA from
# ``data/forge_oracle/version.txt``. This keeps the core oracle logic
# (config hash, build pipeline, strict-consumer verification) testable
# without the live Forge checkout. When ``forge_dir`` is an explicit path
# other than the default (as in ``test_read_current_forge_sha_raises_on_missing_checkout``),
# the stub delegates to the real function so negative-path tests still
# exercise the real behavior.


@pytest.fixture(autouse=True)
def _stub_forge_sha_when_checkout_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    from mtg_synergy_graph.forge_oracle import version as fo_version

    if (fo_version._FORGE_DIR / ".git").exists():
        # Real checkout present — exercise the real function.
        return

    pinned_sha = fo_version.read_pinned_sha()
    real_read = fo_version.read_current_forge_sha

    def _stub(forge_dir: Path = fo_version._FORGE_DIR) -> str:
        # Preserve negative-path semantics: explicit non-default paths go
        # through the real function so tests targeting error modes still fire.
        if forge_dir != fo_version._FORGE_DIR:
            return real_read(forge_dir)
        return pinned_sha

    monkeypatch.setattr(fo_version, "read_current_forge_sha", _stub)


# ---------------------------------------------------------------------------
# Content embeddings (plan 003) — CI-friendly shim
# ---------------------------------------------------------------------------
#
# ``scripts/build_embeddings.py`` populates ``card_embeddings`` /
# ``card_embeddings_config`` but is NOT invoked by the importer (plan D4:
# users run it manually after cardsfolder refresh). On fresh checkouts /
# CI runners, the real ``data/synergy.db`` either doesn't exist yet or
# has empty embedding tables. Downstream tests that call
# ``load_card_embeddings`` against an in-memory DB without populated
# tables would otherwise get ``{}`` silently; the shim below returns a
# deterministic 5-card synthetic dict instead so consumer tests get
# predictable content.
#
# The shim activates only when the *real* production DB
# (``data/synergy.db``) is missing or has zero rows in
# ``card_embeddings``. When a developer has run
# ``scripts/build_embeddings.py`` locally, the real load path runs —
# production failure modes are never masked. Tests that populate
# ``card_embeddings`` on their own in-memory connection bypass the stub
# via the empty-table guard inside ``_stub`` below.


def _make_stub_embedding_vector(seed: int) -> np.ndarray:
    """Deterministic 128-dim L2-normalized float32 vector for a given seed."""
    rng = np.random.default_rng(seed)
    vec = rng.standard_normal(128).astype(np.float32)
    norm = float(np.linalg.norm(vec))
    if norm == 0.0:
        return vec
    return (vec / norm).astype(np.float32)


_SYNTHETIC_CARD_EMBEDDINGS: dict[str, np.ndarray] = {
    f"__stub_card_{letter}": _make_stub_embedding_vector(seed=seed) for seed, letter in enumerate("abcde", start=1)
}


@pytest.fixture(autouse=True)
def _seed_card_embeddings_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub ``load_card_embeddings`` when the real DB has no vectors.

    Mirrors the ``_stub_forge_sha_when_checkout_absent`` precedent: the
    stub fires only when the real production asset is missing / empty,
    so dev machines that have already run
    ``scripts/build_embeddings.py`` exercise the real path. Tests that
    populate ``card_embeddings`` on their own connection still get the
    real function — the per-call guard inside the stub delegates to the
    real ``load_card_embeddings`` whenever the connection's
    ``card_embeddings`` table has rows.

    Stub contract: consumers that bind ``load_card_embeddings`` at the
    module top (``from mtg_synergy_graph.embeddings.store import
    load_card_embeddings``) will NOT see this stub — the monkeypatch
    replaces the attribute on ``embeddings.store`` and on the
    ``embeddings`` package re-export, but a third top-level binding
    captures the original function at import time. Any new consumer
    must either import via attribute access (``emb_store.load_card_embeddings(...)``)
    OR add a manual ``monkeypatch.setattr`` in its own test.
    """
    real_db = Path(__file__).resolve().parent.parent / "data" / "synergy.db"
    should_stub = True
    if real_db.exists():
        probe: sqlite3.Connection | None = None
        try:
            probe_uri = f"file:{real_db}?mode=ro"
            probe = sqlite3.connect(probe_uri, uri=True)
            row = probe.execute("SELECT COUNT(*) FROM card_embeddings").fetchone()
            if row is not None and row[0] > 0:
                should_stub = False
        except sqlite3.Error:
            # Missing table, corrupt DB, permission issue — treat as
            # absent so the stub activates.
            pass
        finally:
            if probe is not None:
                probe.close()
    if not should_stub:
        return

    from mtg_synergy_graph import embeddings as emb_pkg
    from mtg_synergy_graph.embeddings import store as emb_store

    real_load = emb_store.load_card_embeddings

    def _stub(conn: sqlite3.Connection) -> dict[str, np.ndarray]:
        # Narrow the stub to consumer-shaped tests only: a real
        # inference-path call comes with a full schema (``cards`` +
        # ``card_embeddings`` + friends). Bare-schema test DBs that
        # only set up the two embedding tables (as in
        # ``tests/embeddings/test_store.py``) are exercising the real
        # graceful-fallback contract — skip the stub there.
        try:
            conn.execute("SELECT 1 FROM cards LIMIT 1").fetchone()
        except sqlite3.Error:
            return real_load(conn)

        # If the caller has already populated card_embeddings on this
        # connection, exercise the real loader. The stub only fills the
        # gap for empty-table consumer tests.
        try:
            row = conn.execute("SELECT COUNT(*) FROM card_embeddings").fetchone()
        except sqlite3.Error:
            return real_load(conn)
        if row is not None and row[0] > 0:
            return real_load(conn)
        return {name: vec.copy() for name, vec in _SYNTHETIC_CARD_EMBEDDINGS.items()}

    # Patch on both the defining module and the package-level re-export,
    # because ``from mtg_synergy_graph.embeddings import load_card_embeddings``
    # binds a reference that does NOT track later monkeypatching on
    # ``embeddings.store``.
    monkeypatch.setattr(emb_store, "load_card_embeddings", _stub)
    monkeypatch.setattr(emb_pkg, "load_card_embeddings", _stub)


# ---------------------------------------------------------------------------
# In-memory SQLite schema for complement-rule tests
# ---------------------------------------------------------------------------
#
# Historically each complement-rule test file defined its own SCHEMA
# string + ``_port`` / ``_add_port`` helpers. Copies drifted
# independently: schema additions (e.g., ``counter_type``,
# ``branch_kind``, ``effect_conditional``) had to land in every file.
# The helpers below consolidate one superset schema matching the
# production ``card_ports`` / ``cards`` columns used by any existing
# ``_find_*`` rule query. New tests should prefer ``rules_db`` +
# ``add_port``; older files keep their local copies until touched for
# other reasons.


_RULES_SCHEMA = """\
CREATE TABLE cards (
    name TEXT PRIMARY KEY,
    card_types TEXT,
    types TEXT,
    subtypes TEXT,
    supertypes TEXT,
    keywords TEXT,
    color_identity TEXT,
    cmc INTEGER,
    edhrec_rank INTEGER,
    oracle_id TEXT,
    legal_commander INTEGER DEFAULT 1
);
CREATE TABLE card_ports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    card_name TEXT NOT NULL,
    port_type TEXT NOT NULL,
    event_class TEXT NOT NULL,
    valid_filter TEXT,
    raw_line TEXT,
    zone_origin TEXT,
    zone_destination TEXT,
    counter_type TEXT,
    affected_scope TEXT,
    branch_kind TEXT,
    effect_conditional INTEGER DEFAULT 0,
    replacement_event TEXT,
    replacement_result TEXT,
    execute_ref TEXT
);
"""


@pytest.fixture()
def rules_db():
    """In-memory SQLite connection with the complement-rule schema.

    Schema is the superset of columns touched by any existing
    ``_find_*`` helper. Tests that only need a subset of columns can
    still use this fixture — unused columns default to NULL.
    """
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(_RULES_SCHEMA)
    try:
        yield c
    finally:
        c.close()


def _load(name: str) -> dict:
    return parse_card_file(FIXTURES_DIR / name)


@pytest.fixture(scope="session")
def bloodghast() -> dict:
    return _load("bloodghast.txt")


@pytest.fixture(scope="session")
def cathars_crusade() -> dict:
    return _load("cathars_crusade.txt")


@pytest.fixture(scope="session")
def korvold() -> dict:
    return _load("korvold_fae_cursed_king.txt")


@pytest.fixture(scope="session")
def panharmonicon() -> dict:
    return _load("panharmonicon.txt")


@pytest.fixture(scope="session")
def rhystic_study() -> dict:
    return _load("rhystic_study.txt")


@pytest.fixture(scope="session")
def scute_swarm() -> dict:
    return _load("scute_swarm.txt")
