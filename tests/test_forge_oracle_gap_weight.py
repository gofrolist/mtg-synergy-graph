"""Tests for ``forge_oracle.gap_weight`` — the ``gap_report.py`` signal loader.

Plan: docs/plans/2026-04-23-002-feat-forge-second-oracle-plan.md Unit 6.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from mtg_synergy_graph.forge_oracle import gap_weight


def _make_sidecar(
    tmp_path: Path,
    rows: list[tuple[str, str, float, int]],
    *,
    config_hash: str | None = None,
) -> Path:
    """Make a minimal forge_oracle.db with the given PPMI rows."""
    path = tmp_path / "forge_oracle.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE forge_precon_ppmi ("
        "  port_signature_a TEXT, port_signature_b TEXT, "
        "  ppmi REAL, decks_count INTEGER, last_updated TEXT,"
        "  PRIMARY KEY (port_signature_a, port_signature_b));"
        "CREATE TABLE oracle_config (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
    )
    conn.executemany(
        "INSERT INTO forge_precon_ppmi VALUES (?, ?, ?, ?, '2026-04-23')",
        rows,
    )
    if config_hash is not None:
        conn.execute("INSERT INTO oracle_config(key, value) VALUES ('config_hash', ?)", (config_hash,))
    conn.commit()
    conn.close()
    return path


# ---------------------------------------------------------------------------
# load_forge_signals — happy path + fallback
# ---------------------------------------------------------------------------


def test_load_empty_when_path_missing(tmp_path: Path) -> None:
    """Missing sidecar → empty dict (graceful fallback per plan)."""
    sigs = gap_weight.load_forge_signals(tmp_path / "nonexistent.db")
    assert sigs == {}


def test_load_empty_when_table_has_no_rows(tmp_path: Path) -> None:
    sidecar = _make_sidecar(tmp_path, [])
    assert gap_weight.load_forge_signals(sidecar) == {}


def test_load_maps_subkinds_in_range(tmp_path: Path) -> None:
    """All returned weights land in [0.5, 1.5] per the plan."""
    rows = [
        ("cost.sacrifice", "trigger.ChangesZone", 3.0, 5),
        ("cost.sacrifice", "effect.Token", 1.5, 4),
        ("effect.Token", "trigger.ChangesZone", 0.8, 3),
    ]
    sigs = gap_weight.load_forge_signals(_make_sidecar(tmp_path, rows))

    assert "cost.sacrifice" in sigs
    assert "trigger.ChangesZone" in sigs
    assert "effect.Token" in sigs
    for subkind, weight in sigs.items():
        assert 0.5 <= weight <= 1.5, f"{subkind} = {weight} out of range"


def test_load_higher_ppmi_yields_higher_weight(tmp_path: Path) -> None:
    rows = [
        ("high_sig", "partner_x", 3.0, 10),
        ("high_sig", "partner_y", 3.0, 10),
        ("low_sig", "partner_x", 0.1, 3),
    ]
    sigs = gap_weight.load_forge_signals(_make_sidecar(tmp_path, rows))
    assert sigs["high_sig"] > sigs["low_sig"]


def test_load_zero_ppmi_yields_fallback_weight(tmp_path: Path) -> None:
    """Subkinds with only zero-PPMI rows get weight 1.0 (no boost, no dampen)."""
    rows = [
        ("only_zero", "p1", 0.0, 3),
        ("only_zero", "p2", 0.0, 3),
    ]
    sigs = gap_weight.load_forge_signals(_make_sidecar(tmp_path, rows))
    # The subkind "only_zero" may be absent entirely (filtered) or land at 1.0.
    # Either way it must not penalize or boost.
    assert sigs.get("only_zero", 1.0) == pytest.approx(1.0)


def test_load_uses_max_across_partners(tmp_path: Path) -> None:
    """A subkind's weight reflects its strongest pair, not the mean."""
    rows = [
        ("strong", "p1", 0.1, 3),
        ("strong", "p2", 0.1, 3),
        ("strong", "p3", 5.0, 5),  # one strong partner
    ]
    sigs = gap_weight.load_forge_signals(_make_sidecar(tmp_path, rows))
    # Without max semantics, the mean PPMI of "strong" is ~1.7; with max, it's 5.0.
    # The weight should be at/near the 1.5 ceiling if max is being used.
    assert sigs["strong"] == pytest.approx(1.5, abs=0.01)


def test_load_handles_db_corruption_gracefully(tmp_path: Path) -> None:
    """A broken DB → fallback to empty dict, no exception."""
    bad_path = tmp_path / "forge_oracle.db"
    bad_path.write_bytes(b"not a sqlite database at all")
    sigs = gap_weight.load_forge_signals(bad_path)
    assert sigs == {}


def test_load_handles_missing_table_gracefully(tmp_path: Path) -> None:
    """DB exists but has no forge_precon_ppmi table → empty dict."""
    path = tmp_path / "forge_oracle.db"
    conn = sqlite3.connect(path)
    conn.executescript("CREATE TABLE unrelated (x TEXT);")
    conn.commit()
    conn.close()
    assert gap_weight.load_forge_signals(path) == {}


def test_load_subkind_reads_both_pair_sides(tmp_path: Path) -> None:
    """A subkind can appear as signature_a OR signature_b; both sides count."""
    # Canonical storage is ordered a < b. Rows use lexicographically-first
    # signature as port_signature_a. Target "aaa.a" can appear as A in one
    # pair and implicitly reach other partners through its own row presence.
    canonical = [
        ("aaa.a", "zzz.z", 2.0, 3),
        ("aaa.a", "bbb.b", 2.0, 3),
    ]
    sigs = gap_weight.load_forge_signals(_make_sidecar(tmp_path, canonical))
    assert "aaa.a" in sigs
    assert "zzz.z" in sigs
    assert "bbb.b" in sigs


# ---------------------------------------------------------------------------
# forge_weight_for_signature — used by gap_report
# ---------------------------------------------------------------------------


def test_weight_for_signature_returns_default_for_unknown() -> None:
    signals: dict[str, float] = {"cost.sacrifice": 1.3}
    assert gap_weight.forge_weight_for_signature(("effect", "Token", ""), signals) == pytest.approx(1.0)


def test_weight_for_signature_composes_subkind_from_pt_ev() -> None:
    signals = {"cost.sacrifice": 1.3}
    assert gap_weight.forge_weight_for_signature(("cost", "sacrifice", ""), signals) == pytest.approx(1.3)


def test_weight_for_signature_ignores_sub_discriminator() -> None:
    """The PPMI subkind collapses over the sub_discriminator; gap_report's
    3-tuple signatures ``(pt, ev, sub)`` all with the same ``(pt, ev)``
    share the same forge weight."""
    signals = {"trigger.ChangesZone": 1.4}
    w_etb = gap_weight.forge_weight_for_signature(("trigger", "ChangesZone", "Battlefield"), signals)
    w_ltb = gap_weight.forge_weight_for_signature(("trigger", "ChangesZone", "Exile"), signals)
    assert w_etb == pytest.approx(1.4)
    assert w_ltb == pytest.approx(1.4)
    assert w_etb == w_ltb
