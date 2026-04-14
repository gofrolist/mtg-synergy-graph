"""Additional unit tests for validate.py — covers missed lines/branches."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from mtg_synergy_graph import (
    GoldenSetEntry,
    GoldenSetReport,
    Recommendation,
    RecommendationPage,
    SynergyEngine,
    bootstrap_golden_set,
    check_golden_set,
    compute_ndcg,
    edhrec_labels_for_commander,
    regression_failed,
)
from mtg_synergy_graph.validate import (
    _commander_pair,
    _fetch_edhrec_sections,
    _max_workers,
    _run_one,
    _run_parallel,
    _worker_run_batch,
)

# ---------------------------------------------------------------------------
# compute_ndcg — edge cases
# ---------------------------------------------------------------------------


def test_ndcg_zero_when_k_is_zero():
    labels = {"a": 1.0}
    assert compute_ndcg(["a"], labels, k=0) == 0.0


def test_ndcg_zero_when_k_is_negative():
    labels = {"a": 1.0}
    assert compute_ndcg(["a"], labels, k=-1) == 0.0


def test_ndcg_all_labels_zero_returns_zero():
    """When all label values are zero, ideal is empty => 0.0."""
    labels = {"a": 0.0, "b": 0.0}
    assert compute_ndcg(["a", "b"], labels, k=2) == 0.0


# ---------------------------------------------------------------------------
# _commander_pair
# ---------------------------------------------------------------------------


def test_commander_pair_string():
    assert _commander_pair("Korvold, Fae-Cursed King") == ["Korvold, Fae-Cursed King"]


def test_commander_pair_list():
    result = _commander_pair(["Tymna the Weaver", "Thrasios, Triton Hero"])
    assert result == ["Tymna the Weaver", "Thrasios, Triton Hero"]


# ---------------------------------------------------------------------------
# _max_workers
# ---------------------------------------------------------------------------


def test_max_workers_returns_positive():
    result = _max_workers()
    assert 1 <= result <= 8


def test_max_workers_capped_at_8():
    with patch("mtg_synergy_graph.validate.os.cpu_count", return_value=32):
        assert _max_workers() == 8


def test_max_workers_fallback_when_none():
    with patch("mtg_synergy_graph.validate.os.cpu_count", return_value=None):
        assert _max_workers() == 4


# ---------------------------------------------------------------------------
# _fetch_edhrec_sections
# ---------------------------------------------------------------------------


@pytest.fixture()
def edhrec_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE edhrec_card_synergy (
            commander_slug TEXT,
            card_name      TEXT,
            synergy        REAL,
            inclusion      INTEGER,
            num_decks      INTEGER,
            section        TEXT
        )
        """
    )
    rows = [
        ("korvold-fae-cursed-king", "Phyrexian Altar", 0.40, 5000, 5000, "High Synergy Cards"),
        ("korvold-fae-cursed-king", "Dockside Extortionist", 0.35, 8000, 8000, "Top Cards"),
        ("korvold-fae-cursed-king", "Sol Ring", 0.05, 30000, 30000, None),
        ("korvold-fae-cursed-king", "Mayhem Devil", 0.49, 14000, 14000, "High Synergy Cards"),
    ]
    conn.executemany(
        "INSERT INTO edhrec_card_synergy VALUES (?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    yield conn
    conn.close()


def test_fetch_edhrec_sections_groups_by_section(edhrec_db):
    sections = _fetch_edhrec_sections(edhrec_db, "korvold-fae-cursed-king")
    assert "High Synergy Cards" in sections
    assert "Phyrexian Altar" in sections["High Synergy Cards"]
    assert "Mayhem Devil" in sections["High Synergy Cards"]
    assert "Top Cards" in sections
    # None section stored as ""
    assert "" in sections
    assert "Sol Ring" in sections[""]


def test_fetch_edhrec_sections_unknown_slug(edhrec_db):
    sections = _fetch_edhrec_sections(edhrec_db, "nonexistent-commander")
    assert sections == {}


# ---------------------------------------------------------------------------
# edhrec_labels_for_commander — grade_floor
# ---------------------------------------------------------------------------


def test_edhrec_labels_grade_floor(edhrec_db):
    """With grade_floor=0.3, only cards with synergy > 0.3 remain."""
    labels = edhrec_labels_for_commander(edhrec_db, "Korvold, Fae-Cursed King", grade_floor=0.3)
    assert "Phyrexian Altar" in labels
    assert "Dockside Extortionist" in labels
    assert "Sol Ring" not in labels  # 0.05 <= 0.3


def test_edhrec_labels_null_synergy(edhrec_db):
    """Cards with NULL synergy are excluded."""
    edhrec_db.execute(
        "INSERT INTO edhrec_card_synergy VALUES (?, ?, ?, ?, ?, ?)",
        ("korvold-fae-cursed-king", "Null Card", None, 100, 100, "Other"),
    )
    edhrec_db.commit()
    labels = edhrec_labels_for_commander(edhrec_db, "Korvold, Fae-Cursed King")
    assert "Null Card" not in labels


# ---------------------------------------------------------------------------
# _run_one — with edhrec_conn
# ---------------------------------------------------------------------------


def _make_page(commander: list[str], cards: list[str]) -> RecommendationPage:
    items = [
        Recommendation(rank=i + 1, card=c, total_score=100 - i, scores={}, contributing_ports=[])
        for i, c in enumerate(cards)
    ]
    return RecommendationPage(
        commander=commander,
        color_identity=["B", "G", "R"],
        total=len(items),
        offset=0,
        limit=30,
        generated_at="2026-01-01T00:00:00",
        forge_version="test",
        spec_version="1.0",
        engine_version="0.1.0",
        items=items,
    )


def test_run_one_without_edhrec():
    """_run_one with edhrec_conn=None should produce zeros for EDHREC fields."""
    engine = MagicMock(spec=SynergyEngine)
    cards = [f"Card {i}" for i in range(30)]
    engine.page.return_value = _make_page(["TestCommander"], cards)

    entry = _run_one(engine, ["TestCommander"], None)
    assert entry.commander == "TestCommander"
    assert entry.ndcg30 == 0.0
    assert entry.hi_syn_total == 0
    assert entry.hi_syn_hits == 0
    assert entry.on_page_hits == 0
    assert entry.edhrec_top10 == ()
    assert len(entry.top10) == 10


def test_run_one_with_edhrec(edhrec_db):
    """_run_one with edhrec_conn should compute NDCG and hi-syn stats."""
    engine = MagicMock(spec=SynergyEngine)
    cards = ["Phyrexian Altar", "Mayhem Devil", "Dockside Extortionist"] + [f"Card {i}" for i in range(27)]
    engine.page.return_value = _make_page(["Korvold, Fae-Cursed King"], cards)

    entry = _run_one(engine, ["Korvold, Fae-Cursed King"], edhrec_db)
    assert entry.commander == "Korvold, Fae-Cursed King"
    assert entry.ndcg30 > 0.0
    assert entry.hi_syn_total == 2
    assert entry.hi_syn_hits == 2  # Phyrexian Altar + Mayhem Devil in top 30
    assert entry.on_page_hits >= 3  # at least the 3 known EDHREC cards
    assert len(entry.edhrec_top10) > 0


# ---------------------------------------------------------------------------
# _worker_run_batch
# ---------------------------------------------------------------------------


def test_worker_run_batch_catches_value_error(tmp_path):
    """If SynergyEngine.page raises ValueError, the batch records the error."""
    # We need a real-ish db_path for SynergyEngine init, so patch the constructor
    with patch("mtg_synergy_graph.validate.SynergyEngine") as MockEngine:
        mock_engine = MagicMock()
        mock_engine.__enter__ = MagicMock(return_value=mock_engine)
        mock_engine.__exit__ = MagicMock(return_value=False)
        mock_engine.page.side_effect = ValueError("Commander not found")
        MockEngine.return_value = mock_engine

        results = _worker_run_batch((tmp_path / "fake.db", None, [["Unknown Commander"]]))
        assert len(results) == 1
        assert "_error" in results[0]
        assert "Commander not found" in results[0]["_error"]


def test_worker_run_batch_with_edhrec(tmp_path, edhrec_db):
    """_worker_run_batch opens its own edhrec connection when path is provided."""
    # Write a temporary edhrec db to disk for the worker
    edhrec_path = tmp_path / "edhrec.db"
    disk_conn = sqlite3.connect(edhrec_path)
    disk_conn.execute(
        """
        CREATE TABLE edhrec_card_synergy (
            commander_slug TEXT,
            card_name TEXT,
            synergy REAL,
            inclusion INTEGER,
            num_decks INTEGER,
            section TEXT
        )
        """
    )
    disk_conn.commit()
    disk_conn.close()

    with patch("mtg_synergy_graph.validate.SynergyEngine") as MockEngine:
        mock_engine = MagicMock()
        mock_engine.__enter__ = MagicMock(return_value=mock_engine)
        mock_engine.__exit__ = MagicMock(return_value=False)
        cards = [f"Card {i}" for i in range(30)]
        mock_engine.page.return_value = _make_page(["TestCmdr"], cards)
        MockEngine.return_value = mock_engine

        results = _worker_run_batch((tmp_path / "fake.db", edhrec_path, [["TestCmdr"]]))
        assert len(results) == 1
        assert "_error" not in results[0]
        assert results[0]["commander"] == "TestCmdr"


# ---------------------------------------------------------------------------
# _run_parallel — single worker path
# ---------------------------------------------------------------------------


def test_run_parallel_single_worker():
    """When n_workers<=1, _run_parallel uses the single-process path."""
    with (
        patch("mtg_synergy_graph.validate._max_workers", return_value=1),
        patch("mtg_synergy_graph.validate.SynergyEngine") as MockEngine,
    ):
        mock_engine = MagicMock()
        mock_engine.__enter__ = MagicMock(return_value=mock_engine)
        mock_engine.__exit__ = MagicMock(return_value=False)
        cards = [f"Card {i}" for i in range(30)]
        mock_engine.page.return_value = _make_page(["Cmdr1"], cards)
        MockEngine.return_value = mock_engine

        entries = _run_parallel(Path("/fake.db"), None, [["Cmdr1"]])
        assert len(entries) == 1
        assert entries[0].commander == "Cmdr1"


def test_run_parallel_single_worker_with_edhrec(tmp_path):
    """Single-worker path with edhrec db."""
    edhrec_path = tmp_path / "edhrec.db"
    disk_conn = sqlite3.connect(edhrec_path)
    disk_conn.execute(
        """
        CREATE TABLE edhrec_card_synergy (
            commander_slug TEXT,
            card_name TEXT,
            synergy REAL,
            inclusion INTEGER,
            num_decks INTEGER,
            section TEXT
        )
        """
    )
    disk_conn.commit()
    disk_conn.close()

    with (
        patch("mtg_synergy_graph.validate._max_workers", return_value=1),
        patch("mtg_synergy_graph.validate.SynergyEngine") as MockEngine,
    ):
        mock_engine = MagicMock()
        mock_engine.__enter__ = MagicMock(return_value=mock_engine)
        mock_engine.__exit__ = MagicMock(return_value=False)
        cards = [f"Card {i}" for i in range(30)]
        mock_engine.page.return_value = _make_page(["Cmdr1"], cards)
        MockEngine.return_value = mock_engine

        entries = _run_parallel(Path("/fake.db"), edhrec_path, [["Cmdr1"]])
        assert len(entries) == 1


def test_run_parallel_single_worker_skips_valueerror():
    """Single-worker path: ValueError is caught and commander is skipped."""
    with (
        patch("mtg_synergy_graph.validate._max_workers", return_value=1),
        patch("mtg_synergy_graph.validate.SynergyEngine") as MockEngine,
    ):
        mock_engine = MagicMock()
        mock_engine.__enter__ = MagicMock(return_value=mock_engine)
        mock_engine.__exit__ = MagicMock(return_value=False)
        mock_engine.page.side_effect = ValueError("not found")
        MockEngine.return_value = mock_engine

        entries = _run_parallel(Path("/fake.db"), None, [["BadCmdr"]])
        assert entries == []


# ---------------------------------------------------------------------------
# _run_parallel — multi-worker path
# ---------------------------------------------------------------------------


def test_run_parallel_multi_worker_handles_errors():
    """Multi-worker path skips entries with _error."""
    fake_results = [
        {
            "commander": "Good",
            "top10": ["a"] * 10,
            "ndcg30": 0.5,
            "hi_syn_total": 0,
            "hi_syn_hits": 0,
            "on_page_hits": 0,
            "edhrec_top10": (),
        },
        {"_error": "not found", "_cmdr": ["Bad"]},
    ]

    with (
        patch("mtg_synergy_graph.validate._max_workers", return_value=4),
        patch("mtg_synergy_graph.validate.ProcessPoolExecutor") as MockPool,
    ):
        mock_pool = MagicMock()
        mock_pool.__enter__ = MagicMock(return_value=mock_pool)
        mock_pool.__exit__ = MagicMock(return_value=False)
        mock_pool.map.return_value = [fake_results]
        MockPool.return_value = mock_pool

        entries = _run_parallel(Path("/fake.db"), None, [["Good"], ["Bad"], ["C1"], ["C2"], ["C3"]])
        assert len(entries) == 1
        assert entries[0].commander == "Good"


# ---------------------------------------------------------------------------
# bootstrap_golden_set — parallel branch & empty entries
# ---------------------------------------------------------------------------


def test_bootstrap_parallel_branch(tmp_path):
    """When parallel=True and >4 commanders, the parallel path is used."""
    commanders = [f"Cmdr{i}" for i in range(6)]
    out = tmp_path / "golden.json"

    entries = [GoldenSetEntry(commander=f"Cmdr{i}", top10=("a",) * 10, ndcg30=0.5) for i in range(6)]

    with patch("mtg_synergy_graph.validate._run_parallel", return_value=entries) as mock_parallel:
        engine = MagicMock(spec=SynergyEngine)
        engine._db_path = Path("/fake.db")
        report = bootstrap_golden_set(engine, commanders, out, parallel=True)
        mock_parallel.assert_called_once()
        assert len(report.entries) == 6
        assert report.aggregate_ndcg == pytest.approx(0.5)


def test_bootstrap_empty_commanders(tmp_path):
    """Empty commander list produces empty report with aggregate_ndcg=0."""
    out = tmp_path / "golden.json"
    engine = MagicMock(spec=SynergyEngine)
    report = bootstrap_golden_set(engine, [], out, parallel=False)
    assert report.entries == ()
    assert report.aggregate_ndcg == 0.0
    payload = json.loads(out.read_text())
    assert payload["entries"] == []


def test_bootstrap_sequential_skips_valueerror(tmp_path):
    """Sequential mode catches ValueError and skips the commander."""
    out = tmp_path / "golden.json"

    with patch("mtg_synergy_graph.validate._run_one", side_effect=ValueError("bad")):
        engine = MagicMock(spec=SynergyEngine)
        report = bootstrap_golden_set(engine, ["BadCmdr"], out, parallel=False)
        assert report.entries == ()


# ---------------------------------------------------------------------------
# check_golden_set — drift, ndcg_drops, parallel
# ---------------------------------------------------------------------------


def test_check_golden_set_detects_ndcg_drop(tmp_path):
    """When fresh NDCG is significantly lower than baseline, ndcg_drops is populated."""
    baseline = {
        "version": 1,
        "entries": [
            {
                "commander": "TestCmdr",
                "top10": [f"Card{i}" for i in range(10)],
                "ndcg30": 0.90,
            }
        ],
    }
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(json.dumps(baseline))

    fresh_entry = GoldenSetEntry(
        commander="TestCmdr",
        top10=tuple(f"Card{i}" for i in range(10)),
        ndcg30=0.50,  # big drop
    )

    with patch("mtg_synergy_graph.validate._run_one", return_value=fresh_entry):
        engine = MagicMock(spec=SynergyEngine)
        report = check_golden_set(engine, baseline_path, parallel=False)

    assert len(report.ndcg_drops) == 1
    assert report.ndcg_drops[0]["commander"] == "TestCmdr"
    assert report.ndcg_drops[0]["delta"] < 0
    assert regression_failed(report)


def test_check_golden_set_drift_missing_commander(tmp_path):
    """When _run_one raises ValueError, the commander appears in drift."""
    baseline = {
        "version": 1,
        "entries": [
            {
                "commander": "MissingCmdr",
                "top10": ["a"] * 10,
                "ndcg30": 0.5,
            }
        ],
    }
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(json.dumps(baseline))

    with patch("mtg_synergy_graph.validate._run_one", side_effect=ValueError("not found")):
        engine = MagicMock(spec=SynergyEngine)
        report = check_golden_set(engine, baseline_path, parallel=False)

    assert len(report.drift) == 1
    assert report.drift[0]["commander"] == "MissingCmdr"
    assert report.drift[0]["error"] == "not in fresh results"


def test_check_golden_set_parallel_branch(tmp_path):
    """check_golden_set uses parallel path when >4 commanders."""
    entries_data = []
    for i in range(6):
        entries_data.append(
            {
                "commander": f"Cmdr{i}",
                "top10": [f"Card{j}" for j in range(10)],
                "ndcg30": 0.5,
            }
        )
    baseline = {"version": 1, "entries": entries_data}
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(json.dumps(baseline))

    fresh_entries = [
        GoldenSetEntry(
            commander=f"Cmdr{i}",
            top10=tuple(f"Card{j}" for j in range(10)),
            ndcg30=0.5,
        )
        for i in range(6)
    ]

    with patch("mtg_synergy_graph.validate._run_parallel", return_value=fresh_entries) as mock_par:
        engine = MagicMock(spec=SynergyEngine)
        engine._db_path = Path("/fake.db")
        report = check_golden_set(engine, baseline_path, parallel=True)
        mock_par.assert_called_once()
    assert report.rank_shifts == ()
    assert report.ndcg_drops == ()


# ---------------------------------------------------------------------------
# regression_failed — more cases
# ---------------------------------------------------------------------------


def test_regression_failed_no_issues():
    report = GoldenSetReport(
        entries=(),
        rank_shifts=(),
        ndcg_drops=(),
        aggregate_ndcg=0.50,
        baseline_ndcg=0.50,
    )
    assert not regression_failed(report)


def test_regression_failed_with_rank_shifts():
    report = GoldenSetReport(
        entries=(),
        rank_shifts=({"commander": "X", "shift": 6},),
        ndcg_drops=(),
        aggregate_ndcg=0.50,
        baseline_ndcg=0.50,
    )
    assert regression_failed(report)


def test_regression_failed_with_ndcg_drops():
    report = GoldenSetReport(
        entries=(),
        rank_shifts=(),
        ndcg_drops=({"commander": "X", "delta": -0.1},),
        aggregate_ndcg=0.50,
        baseline_ndcg=0.50,
    )
    assert regression_failed(report)


def test_regression_failed_aggregate_within_tolerance():
    """Aggregate drop within tolerance should NOT fail."""
    report = GoldenSetReport(
        entries=(),
        rank_shifts=(),
        ndcg_drops=(),
        aggregate_ndcg=0.498,
        baseline_ndcg=0.500,
    )
    assert not regression_failed(report, ndcg_tolerance=0.005)
