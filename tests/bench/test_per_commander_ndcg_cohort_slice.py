"""Cohort-restricted NDCG slice in ``--per-commander-ndcg`` (plan 2026-07-03-001 Unit 3).

The reporter gains an ``in-cohort`` / ``rest`` partition keyed on archetype-payoff
cohort membership, mirroring the existing identity-size slice. Membership is read
from the fixture's ``cohort_members`` snapshot when present (reproducible from the
pin) and falls back to a live, explicitly non-reproducible computation otherwise.
Synthetic ``tmp_path`` DBs only (conftest guard).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from mtg_synergy_graph.bench import per_commander_ndcg as pcn
from mtg_synergy_graph.bench.per_commander_ndcg import (
    PerCommanderNdcgRow,
    compute_cohort_slices,
    handle_per_commander_ndcg,
    render_per_commander_ndcg_markdown,
)
from mtg_synergy_graph.db import open_db


def _row(commander: str, pinned: float, live: float) -> PerCommanderNdcgRow:
    return PerCommanderNdcgRow(commander=commander, pinned_ndcg=pinned, live_ndcg=live, delta=live - pinned)


# ---------------------------------------------------------------------------
# compute_cohort_slices
# ---------------------------------------------------------------------------


def test_partitions_into_in_cohort_and_rest() -> None:
    """Happy path: 3 of 5 commanders in cohort → in-cohort n=3, rest n=2."""
    rows = [
        _row("A", 0.2, 0.3),
        _row("B", 0.4, 0.5),
        _row("C", 0.1, 0.1),
        _row("D", 0.6, 0.5),
        _row("E", 0.2, 0.4),
    ]
    slices = compute_cohort_slices(rows, {"A", "B", "C"})
    by_name = {s.slice_name: s for s in slices}
    assert by_name["in-cohort"].n == 3
    assert by_name["rest"].n == 2
    # in-cohort mean delta = mean(0.1, 0.1, 0.0) = 0.0666...
    assert by_name["in-cohort"].mean_delta == pytest.approx((0.1 + 0.1 + 0.0) / 3)
    assert by_name["rest"].mean_delta == pytest.approx((-0.1 + 0.2) / 2)


def test_in_cohort_row_is_first() -> None:
    """Stable order: in-cohort always renders before rest."""
    slices = compute_cohort_slices([_row("A", 0.1, 0.2)], {"A"})
    assert [s.slice_name for s in slices] == ["in-cohort", "rest"]


def test_zero_cohort_members_renders_zero_count_row() -> None:
    """Edge: no cohort members → in-cohort row present with n=0, no div-by-zero."""
    rows = [_row("A", 0.1, 0.2), _row("B", 0.3, 0.3)]
    slices = compute_cohort_slices(rows, set())
    by_name = {s.slice_name: s for s in slices}
    assert by_name["in-cohort"].n == 0
    assert by_name["in-cohort"].mean_delta == 0.0  # guarded, not a crash
    assert by_name["rest"].n == 2


def test_all_in_cohort_leaves_rest_empty() -> None:
    """Edge: every commander in cohort → rest n=0 handled."""
    rows = [_row("A", 0.1, 0.2), _row("B", 0.3, 0.4)]
    slices = compute_cohort_slices(rows, {"A", "B"})
    by_name = {s.slice_name: s for s in slices}
    assert by_name["in-cohort"].n == 2
    assert by_name["rest"].n == 0
    assert by_name["rest"].mean_delta == 0.0


# ---------------------------------------------------------------------------
# render
# ---------------------------------------------------------------------------


def test_render_includes_cohort_block_below_identity() -> None:
    """Integration: cohort block renders after the identity-slice block."""
    rows = [_row("A", 0.1, 0.2), _row("B", 0.3, 0.3)]
    out = render_per_commander_ndcg_markdown(
        rows,
        fixture_path="f.json",
        config_hash="deadbeef0000",
        identity_class_by_commander={"A": "mono", "B": "2-color"},
        cohort_names={"A"},
        cohort_from_snapshot=True,
    )
    assert "Cohort slices" in out
    assert out.index("Identity-size slices") < out.index("Cohort slices")
    assert "in-cohort" in out


def test_render_labels_live_membership_non_reproducible() -> None:
    """A live (non-snapshot) cohort block is explicitly labeled non-reproducible."""
    rows = [_row("A", 0.1, 0.2)]
    out = render_per_commander_ndcg_markdown(
        rows,
        fixture_path="f.json",
        config_hash="deadbeef0000",
        cohort_names={"A"},
        cohort_from_snapshot=False,
    )
    assert "not pin-reproducible" in out.lower() or "live" in out.lower()


def test_render_omits_cohort_block_when_no_cohort_names() -> None:
    """Backwards compat: no cohort_names → no cohort block (existing callers)."""
    rows = [_row("A", 0.1, 0.2)]
    out = render_per_commander_ndcg_markdown(rows, fixture_path="f.json", config_hash="x")
    assert "Cohort slices" not in out


# ---------------------------------------------------------------------------
# handler: snapshot-first, live fallback
# ---------------------------------------------------------------------------


@pytest.fixture()
def handler_dbs(tmp_path: Path) -> Iterator[tuple[Path, Path]]:
    """A synergy DB + EDHREC DB sufficient for the handler to run."""
    synergy = tmp_path / "synergy.db"
    conn = open_db(synergy)
    conn.execute(
        "INSERT INTO cards (name, card_types, subtypes, cmc, color_identity, edhrec_rank, legal_commander) "
        "VALUES ('Alpha', 'Creature', '', 3, 'B', 100, 1)"
    )
    conn.execute(
        "INSERT INTO cards (name, card_types, subtypes, cmc, color_identity, edhrec_rank, legal_commander) "
        "VALUES ('Beta', 'Creature', '', 3, 'B', 200, 1)"
    )
    conn.commit()
    conn.close()

    edhrec = tmp_path / "tags.db"
    econn = sqlite3.connect(edhrec)
    econn.execute("CREATE TABLE edhrec_card_synergy (commander_slug TEXT, card_name TEXT, section TEXT, synergy REAL)")
    econn.commit()
    econn.close()
    yield synergy, edhrec


def _write_fixture(path: Path, commanders: list[str], cohort_members: list[str] | None) -> None:
    payload: dict = {
        "schema_version": 2,
        "config_hash": "x",
        "created_at": "t",
        "entries": [{"commander": c, "scores": {}} for c in commanders],
    }
    if cohort_members is not None:
        payload["cohort_members"] = cohort_members
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_handler_uses_snapshot_without_requerying(
    handler_dbs: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Integration: a fixture with cohort_members uses the snapshot, not a live query."""
    synergy, edhrec = handler_dbs
    fixture = tmp_path / "cohort.json"
    _write_fixture(fixture, ["Alpha", "Beta"], cohort_members=["Alpha"])

    def _boom(_conn: sqlite3.Connection) -> set[str]:  # pragma: no cover - must not run
        raise AssertionError("live cohort computed despite snapshot present")

    monkeypatch.setattr(pcn, "archetype_payoff_cohort", _boom)
    out = tmp_path / "report.md"
    args = argparse.Namespace(db=str(synergy), edhrec_db=str(edhrec), fixture=str(fixture), output=str(out))
    assert handle_per_commander_ndcg(args) == 0
    text = out.read_text()
    assert "Cohort slices" in text


def test_handler_falls_back_to_live_for_untagged_fixture(
    handler_dbs: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Integration: an untagged fixture computes live membership, labeled non-reproducible."""
    synergy, edhrec = handler_dbs
    fixture = tmp_path / "untagged.json"
    _write_fixture(fixture, ["Alpha", "Beta"], cohort_members=None)

    monkeypatch.setattr(pcn, "archetype_payoff_cohort", lambda _conn: {"Alpha"})
    out = tmp_path / "report.md"
    args = argparse.Namespace(db=str(synergy), edhrec_db=str(edhrec), fixture=str(fixture), output=str(out))
    assert handle_per_commander_ndcg(args) == 0
    text = out.read_text().lower()
    assert "cohort slices" in text
    assert "not pin-reproducible" in text or "live" in text
