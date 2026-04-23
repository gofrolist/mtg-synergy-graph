"""``bench.py audit --inspect-gems`` handler tests (Unit 4 of plan 003).

Per-commander diff of hidden-gem sets between pinned and live. Tests
use synthetic in-memory ``PinnedFixture`` instances and monkeypatch
``build_fixture`` in the handler module so the tests run in ~milliseconds
and avoid touching the real ``data/synergy.db`` + ``data/tags.db``.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

import pytest

from mtg_synergy_graph.bench import handlers
from mtg_synergy_graph.bench.fixture import (
    FixtureEntry,
    PinnedFixture,
)
from mtg_synergy_graph.bench.handlers import handle_inspect_gems

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _args(
    fixture: Path,
    db: Path | str = "data/synergy.db",
    *,
    edhrec_db: str | None = "data/tags.db",
    commander: str | None = None,
    fmt: str = "md",
    output: str | None = None,
    limit: int = 100,
) -> argparse.Namespace:
    return argparse.Namespace(
        db=str(db),
        edhrec_db=edhrec_db,
        fixture=str(fixture),
        commander=commander,
        format=fmt,
        output=output,
        limit=limit,
    )


def _write_fixture(path: Path, fixture: PinnedFixture) -> None:
    fixture.write(path)


def _make_fixture(entries: list[FixtureEntry], *, config_hash: str = "x") -> PinnedFixture:
    return PinnedFixture(config_hash=config_hash, created_at="t", entries=entries)


@pytest.fixture()
def _stub_dbs(tmp_path: Path) -> tuple[Path, Path]:
    """Create empty stub DB files so existence checks pass. The real
    ``build_fixture`` is monkeypatched per test so these contents are
    never read."""
    synergy = tmp_path / "synergy.db"
    synergy.touch()
    edhrec = tmp_path / "tags.db"
    edhrec.touch()
    return synergy, edhrec


def _patch_build_fixture(monkeypatch: pytest.MonkeyPatch, live: PinnedFixture) -> None:
    """Replace ``build_fixture`` in the handler module with a stub that
    returns the given live fixture regardless of input."""

    def _stub(conn, commanders, existing=None, tensor_writer=None, edhrec_conn=None):  # type: ignore[no-untyped-def]
        return live

    monkeypatch.setattr(handlers, "build_fixture", _stub)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_happy_path_renders_delta_rows_sorted_by_magnitude(
    tmp_path: Path,
    _stub_dbs: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Two commanders, each with lost/gained gems — output lists them in
    |Δ| desc order and names lost/gained cards."""
    synergy, edhrec = _stub_dbs

    pinned = _make_fixture(
        [
            FixtureEntry(
                commander="Animar",
                legacy={
                    "hidden_gem_hit_rate": 0.20,
                    "hidden_cards": ["Spellbinder", "Morophon"],
                },
            ),
            FixtureEntry(
                commander="Yuriko",
                legacy={
                    "hidden_gem_hit_rate": 0.10,
                    "hidden_cards": ["Agent of Treachery"],
                },
            ),
        ]
    )
    fixture_path = tmp_path / "baseline.json"
    _write_fixture(fixture_path, pinned)

    live = _make_fixture(
        [
            FixtureEntry(
                commander="Animar",
                legacy={
                    # Dropped by |Δ|=0.10 (3/30 to 0/30 ≈ -0.10 — use ~0.1333)
                    "hidden_gem_hit_rate": 0.10,
                    # Lost Morophon; no gains.
                    "hidden_cards": ["Spellbinder"],
                },
            ),
            FixtureEntry(
                commander="Yuriko",
                legacy={
                    "hidden_gem_hit_rate": 0.13,  # small positive Δ
                    "hidden_cards": ["Agent of Treachery", "Curtains' Call"],
                },
            ),
        ]
    )
    _patch_build_fixture(monkeypatch, live)

    rc = handle_inspect_gems(_args(fixture_path, synergy, edhrec_db=str(edhrec)))
    assert rc == 0
    out = capsys.readouterr().out
    assert "bench.py audit --inspect-gems" in out
    # Both commanders appear.
    assert "Animar" in out
    assert "Yuriko" in out
    # Animar should sort first (|Δ|=0.10 > |Δ|=0.03).
    animar_pos = out.index("Animar")
    yuriko_pos = out.index("Yuriko")
    assert animar_pos < yuriko_pos
    # Lost/gained names rendered.
    assert "Morophon" in out
    assert "Curtains' Call" in out


def test_rate_delta_rendered_as_integer_count_out_of_30(
    tmp_path: Path,
    _stub_dbs: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Δ column shows ``round(rate_delta * 30)`` — more intuitive than a
    float since the metric is a count out of 30."""
    synergy, edhrec = _stub_dbs
    pinned = _make_fixture(
        [
            FixtureEntry(
                commander="CmdrA",
                legacy={
                    "hidden_gem_hit_rate": 2.0 / 30.0,
                    "hidden_cards": ["X", "Y"],
                },
            ),
        ]
    )
    fixture_path = tmp_path / "baseline.json"
    _write_fixture(fixture_path, pinned)

    live = _make_fixture(
        [
            FixtureEntry(
                commander="CmdrA",
                legacy={
                    "hidden_gem_hit_rate": 0.0,
                    "hidden_cards": [],
                },
            ),
        ]
    )
    _patch_build_fixture(monkeypatch, live)

    rc = handle_inspect_gems(_args(fixture_path, synergy, edhrec_db=str(edhrec)))
    assert rc == 0
    out = capsys.readouterr().out
    # Δ=-2 (lost 2 gems out of 30)
    assert "-2" in out


# ---------------------------------------------------------------------------
# Edge: no baseline gem data
# ---------------------------------------------------------------------------


def test_empty_pinned_no_baseline_message(
    tmp_path: Path,
    _stub_dbs: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Pinned has no ``hidden_gem_hit_rate`` anywhere → handler emits
    ``no baseline gems to compare`` and exits 0."""
    synergy, edhrec = _stub_dbs
    pinned = _make_fixture(
        [
            FixtureEntry(commander="A", legacy={"ndcg30": 0.5}),
            FixtureEntry(commander="B", legacy={}),
        ]
    )
    fixture_path = tmp_path / "baseline.json"
    _write_fixture(fixture_path, pinned)

    live = _make_fixture(
        [
            FixtureEntry(commander="A", legacy={}),
            FixtureEntry(commander="B", legacy={}),
        ]
    )
    _patch_build_fixture(monkeypatch, live)

    rc = handle_inspect_gems(_args(fixture_path, synergy, edhrec_db=str(edhrec)))
    assert rc == 0
    combined = capsys.readouterr()
    assert "no baseline gems to compare" in (combined.out + combined.err).lower()


# ---------------------------------------------------------------------------
# Edge: --commander filter
# ---------------------------------------------------------------------------


def test_commander_filter_selects_one_row(
    tmp_path: Path,
    _stub_dbs: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--commander NAME`` filters to one row."""
    synergy, edhrec = _stub_dbs
    pinned = _make_fixture(
        [
            FixtureEntry(
                commander="KeepMe",
                legacy={"hidden_gem_hit_rate": 0.20, "hidden_cards": ["A", "B"]},
            ),
            FixtureEntry(
                commander="DropMe",
                legacy={"hidden_gem_hit_rate": 0.30, "hidden_cards": ["C", "D"]},
            ),
        ]
    )
    fixture_path = tmp_path / "baseline.json"
    _write_fixture(fixture_path, pinned)

    live = _make_fixture(
        [
            FixtureEntry(
                commander="KeepMe",
                legacy={"hidden_gem_hit_rate": 0.10, "hidden_cards": ["A"]},
            ),
            FixtureEntry(
                commander="DropMe",
                legacy={"hidden_gem_hit_rate": 0.05, "hidden_cards": []},
            ),
        ]
    )
    _patch_build_fixture(monkeypatch, live)

    rc = handle_inspect_gems(_args(fixture_path, synergy, edhrec_db=str(edhrec), commander="KeepMe"))
    assert rc == 0
    out = capsys.readouterr().out
    assert "KeepMe" in out
    assert "DropMe" not in out


def test_commander_not_in_fixture_exits_2(
    tmp_path: Path,
    _stub_dbs: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--commander NameNotInFixture`` → exit 2, actionable stderr."""
    synergy, edhrec = _stub_dbs
    pinned = _make_fixture(
        [
            FixtureEntry(
                commander="RealCommander",
                legacy={"hidden_gem_hit_rate": 0.1, "hidden_cards": ["X"]},
            ),
        ]
    )
    fixture_path = tmp_path / "baseline.json"
    _write_fixture(fixture_path, pinned)

    live = _make_fixture(
        [
            FixtureEntry(
                commander="RealCommander",
                legacy={"hidden_gem_hit_rate": 0.1, "hidden_cards": ["X"]},
            ),
        ]
    )
    _patch_build_fixture(monkeypatch, live)

    rc = handle_inspect_gems(
        _args(fixture_path, synergy, edhrec_db=str(edhrec), commander="Ghost Commander"),
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "not in fixture" in err.lower()


# ---------------------------------------------------------------------------
# Edge: --limit
# ---------------------------------------------------------------------------


def test_aggregate_computed_over_full_set_not_truncated_by_limit(
    tmp_path: Path,
    _stub_dbs: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--limit`` truncates the rendered table but MUST NOT shrink the
    input to the aggregate-Δ calculation.

    With 10 commanders whose rate_deltas span a wide range, rendering
    ``--limit 3`` should still report the aggregate over all 10. If
    the aggregate is computed post-limit, only the three extreme
    movers drive it — hiding whether the broader set moved positively
    or negatively.
    """
    synergy, edhrec = _stub_dbs

    # Build 10 commanders with known deltas. Pinned rate is constant so
    # the delta on each equals live - pinned.
    #   - 7 commanders drop by 0.01 (well below the extreme)
    #   - 3 commanders drop by 0.30 (the extreme movers)
    # Aggregate over all 10: (7 * -0.01 + 3 * -0.30) / 10 = -0.097
    # Aggregate over top 3 (|Δ|=0.30 each): -0.30
    pinned_entries: list[FixtureEntry] = []
    live_entries: list[FixtureEntry] = []
    for idx in range(10):
        pinned_rate = 0.5
        # Δ = -0.30 for the 3 extreme movers, -0.01 for the rest.
        live_rate = 0.20 if idx < 3 else 0.49
        name = f"Cmdr{idx:02d}"
        pinned_entries.append(
            FixtureEntry(
                commander=name,
                legacy={"hidden_gem_hit_rate": pinned_rate, "hidden_cards": ["P"]},
            )
        )
        live_entries.append(
            FixtureEntry(
                commander=name,
                legacy={"hidden_gem_hit_rate": live_rate, "hidden_cards": ["L"]},
            )
        )

    pinned = _make_fixture(pinned_entries)
    fixture_path = tmp_path / "baseline.json"
    _write_fixture(fixture_path, pinned)

    live = _make_fixture(live_entries)
    _patch_build_fixture(monkeypatch, live)

    rc = handle_inspect_gems(_args(fixture_path, synergy, edhrec_db=str(edhrec), limit=3))
    assert rc == 0
    out = capsys.readouterr().out

    # Aggregate over all 10 = -0.0970. Over the truncated top-3 it
    # would be -0.3000. Accept either sign formatting but require the
    # 10-row aggregate, not the 3-row one.
    assert "-0.0970" in out, (
        f"Aggregate should reflect all 10 commanders, not just the top-3 by magnitude. Output was:\n{out}"
    )
    assert "-0.3000" not in out


def test_limit_zero_yields_empty_table(
    tmp_path: Path,
    _stub_dbs: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--limit 0`` → table header only, exit 0."""
    synergy, edhrec = _stub_dbs
    pinned = _make_fixture(
        [
            FixtureEntry(
                commander="A",
                legacy={"hidden_gem_hit_rate": 0.1, "hidden_cards": ["X"]},
            ),
        ]
    )
    fixture_path = tmp_path / "baseline.json"
    _write_fixture(fixture_path, pinned)

    live = _make_fixture(
        [
            FixtureEntry(commander="A", legacy={"hidden_gem_hit_rate": 0.0, "hidden_cards": []}),
        ]
    )
    _patch_build_fixture(monkeypatch, live)

    rc = handle_inspect_gems(_args(fixture_path, synergy, edhrec_db=str(edhrec), limit=0))
    assert rc == 0
    out = capsys.readouterr().out
    # Full table separator row must be present (render still emits the
    # header + separator even with --limit 0).
    separator = "|-----------|--:|-----------|-------------|"
    assert separator in out
    # No data rows follow the separator — everything after it should
    # be whitespace. Previously the test was
    # ``assert 'A' not in out.split(sep)[-1] if sep in out else True``
    # which was vacuously true when the separator wasn't present (the
    # exact bug P2 fix #23 targeted); this rewrite asserts behavior
    # directly.
    after_separator = out.split(separator, 1)[1].strip()
    assert after_separator == ""


# ---------------------------------------------------------------------------
# Error: missing fixture
# ---------------------------------------------------------------------------


def test_fixture_missing_exits_2(
    tmp_path: Path,
    _stub_dbs: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Fixture file doesn't exist → exit 2, message consistent with handle_audit."""
    synergy, edhrec = _stub_dbs
    rc = handle_inspect_gems(_args(tmp_path / "does-not-exist.json", synergy, edhrec_db=str(edhrec)))
    assert rc == 2
    err = capsys.readouterr().err
    assert "not found" in err.lower()


# ---------------------------------------------------------------------------
# Error: missing EDHREC DB
# ---------------------------------------------------------------------------


def test_edhrec_db_missing_exits_2(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Fixture exists but edhrec DB missing → exit 2."""
    synergy = tmp_path / "synergy.db"
    synergy.touch()

    pinned = _make_fixture(
        [
            FixtureEntry(
                commander="A",
                legacy={"hidden_gem_hit_rate": 0.1, "hidden_cards": ["X"]},
            ),
        ]
    )
    fixture_path = tmp_path / "baseline.json"
    _write_fixture(fixture_path, pinned)

    rc = handle_inspect_gems(
        _args(fixture_path, synergy, edhrec_db=str(tmp_path / "missing.db")),
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "edhrec" in err.lower()


def test_edhrec_db_unusable_surfaces_actionable_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Corrupt / schema-less EDHREC DB → exit 2 with actionable stderr.

    Simulates the "sqlite3.DatabaseError from build_fixture" path: a
    real file exists at the path but build_fixture raises when it
    tries to query it. Should surface as exit 2 with the
    "Pass --edhrec-db PATH..." guidance (the same surface as a missing
    path), not an unhandled traceback.
    """
    synergy = tmp_path / "synergy.db"
    synergy.touch()
    edhrec = tmp_path / "tags.db"
    # Create a real SQLite DB so the path exists and is openable,
    # but without the expected table — the helper raises
    # OperationalError (subclass of DatabaseError).
    conn = sqlite3.connect(edhrec)
    conn.commit()
    conn.close()

    pinned = _make_fixture(
        [
            FixtureEntry(
                commander="A",
                legacy={"hidden_gem_hit_rate": 0.1, "hidden_cards": ["X"]},
            ),
        ]
    )
    fixture_path = tmp_path / "baseline.json"
    _write_fixture(fixture_path, pinned)

    # Monkeypatch build_fixture to raise DatabaseError directly — this
    # is the error surface we need to handle.
    def _raise(conn, commanders, existing=None, tensor_writer=None, edhrec_conn=None):  # type: ignore[no-untyped-def]
        import sqlite3 as _sql

        raise _sql.OperationalError("no such table: edhrec_card_synergy")

    monkeypatch.setattr(handlers, "build_fixture", _raise)

    rc = handle_inspect_gems(_args(fixture_path, synergy, edhrec_db=str(edhrec)))
    assert rc == 2
    err = capsys.readouterr().err
    assert "edhrec" in err.lower()
    assert "unusable" in err.lower() or "pass --edhrec-db" in err.lower()


# ---------------------------------------------------------------------------
# CLI dispatch
# ---------------------------------------------------------------------------


def test_cli_dispatch_routes_to_handle_inspect_gems(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``main(["audit", "--inspect-gems", ...])`` routes via register()."""
    import mtg_synergy_graph.bench  # ensure handlers register  # noqa: F401
    from mtg_synergy_graph.bench.cli import main

    synergy = tmp_path / "synergy.db"
    synergy.touch()
    edhrec = tmp_path / "tags.db"
    edhrec.touch()

    pinned = _make_fixture(
        [
            FixtureEntry(
                commander="A",
                legacy={"hidden_gem_hit_rate": 0.1, "hidden_cards": ["X"]},
            ),
        ]
    )
    fixture_path = tmp_path / "baseline.json"
    _write_fixture(fixture_path, pinned)

    # Monkeypatch so CLI routing is exercised but we don't need a real DB.
    live = _make_fixture(
        [
            FixtureEntry(
                commander="A",
                legacy={"hidden_gem_hit_rate": 0.0, "hidden_cards": []},
            ),
        ]
    )
    _patch_build_fixture(monkeypatch, live)

    rc = main(
        [
            "audit",
            "--inspect-gems",
            "--db",
            str(synergy),
            "--edhrec-db",
            str(edhrec),
            "--fixture",
            str(fixture_path),
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "bench.py audit --inspect-gems" in out


def test_inspect_gems_mutually_exclusive_with_other_modes() -> None:
    """``--inspect-gems`` is in the mode mutex group → combining with
    ``--expect-identity`` raises SystemExit from argparse."""
    from mtg_synergy_graph.bench.cli import main

    with pytest.raises(SystemExit):
        main(["audit", "--inspect-gems", "--expect-identity"])


# ---------------------------------------------------------------------------
# --format json
# ---------------------------------------------------------------------------


def test_json_format_valid_and_contains_aggregate(
    tmp_path: Path,
    _stub_dbs: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--format json`` emits parseable JSON with ``rows`` + aggregate."""
    synergy, edhrec = _stub_dbs
    pinned = _make_fixture(
        [
            FixtureEntry(
                commander="A",
                legacy={"hidden_gem_hit_rate": 0.2, "hidden_cards": ["X", "Y"]},
            ),
        ]
    )
    fixture_path = tmp_path / "baseline.json"
    _write_fixture(fixture_path, pinned)

    live = _make_fixture(
        [
            FixtureEntry(
                commander="A",
                legacy={"hidden_gem_hit_rate": 0.1, "hidden_cards": ["X"]},
            ),
        ]
    )
    _patch_build_fixture(monkeypatch, live)

    rc = handle_inspect_gems(_args(fixture_path, synergy, edhrec_db=str(edhrec), fmt="json"))
    assert rc == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert "aggregate_rate_delta" in payload
    assert "n_commanders" in payload
    assert "rows" in payload
    assert isinstance(payload["rows"], list)
    assert payload["rows"][0]["commander"] == "A"
    assert "Y" in payload["rows"][0]["lost"]


# ---------------------------------------------------------------------------
# --output PATH
# ---------------------------------------------------------------------------


def test_output_writes_to_file_with_stderr_confirmation(
    tmp_path: Path,
    _stub_dbs: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--output PATH`` writes rendered content to file; stderr carries
    the confirmation; stdout is empty."""
    synergy, edhrec = _stub_dbs
    pinned = _make_fixture(
        [
            FixtureEntry(
                commander="A",
                legacy={"hidden_gem_hit_rate": 0.2, "hidden_cards": ["X", "Y"]},
            ),
        ]
    )
    fixture_path = tmp_path / "baseline.json"
    _write_fixture(fixture_path, pinned)

    live = _make_fixture(
        [
            FixtureEntry(
                commander="A",
                legacy={"hidden_gem_hit_rate": 0.1, "hidden_cards": ["X"]},
            ),
        ]
    )
    _patch_build_fixture(monkeypatch, live)

    out_path = tmp_path / "gems.md"
    rc = handle_inspect_gems(
        _args(fixture_path, synergy, edhrec_db=str(edhrec), output=str(out_path)),
    )
    assert rc == 0
    assert out_path.exists()
    content = out_path.read_text(encoding="utf-8")
    assert "bench.py audit --inspect-gems" in content
    captured = capsys.readouterr()
    assert captured.out == ""
    assert str(out_path) in captured.err


# ---------------------------------------------------------------------------
# Edge: commander in pinned but not live
# ---------------------------------------------------------------------------


def test_commander_missing_from_live_skipped(
    tmp_path: Path,
    _stub_dbs: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Commander in pinned but missing from live → skipped with stderr note."""
    synergy, edhrec = _stub_dbs
    pinned = _make_fixture(
        [
            FixtureEntry(
                commander="A",
                legacy={"hidden_gem_hit_rate": 0.1, "hidden_cards": ["X"]},
            ),
            FixtureEntry(
                commander="B",
                legacy={"hidden_gem_hit_rate": 0.2, "hidden_cards": ["Y"]},
            ),
        ]
    )
    fixture_path = tmp_path / "baseline.json"
    _write_fixture(fixture_path, pinned)

    live = _make_fixture(
        [
            # Only A; B is missing.
            FixtureEntry(
                commander="A",
                legacy={"hidden_gem_hit_rate": 0.05, "hidden_cards": []},
            ),
        ]
    )
    _patch_build_fixture(monkeypatch, live)

    rc = handle_inspect_gems(_args(fixture_path, synergy, edhrec_db=str(edhrec)))
    assert rc == 0
    captured = capsys.readouterr()
    out = captured.out
    err = captured.err
    # A is present; B is skipped.
    assert "A" in out
    assert "skipped 1" in err.lower() or "missing from live" in err.lower()


# ---------------------------------------------------------------------------
# Edge: rate_delta = None sorted last
# ---------------------------------------------------------------------------


def test_none_rate_delta_sorts_last(
    tmp_path: Path,
    _stub_dbs: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Commanders with missing legacy rate on one side sort to the end."""
    synergy, edhrec = _stub_dbs
    pinned = _make_fixture(
        [
            FixtureEntry(
                commander="HasBoth",
                legacy={"hidden_gem_hit_rate": 0.2, "hidden_cards": ["X"]},
            ),
            FixtureEntry(
                commander="PinnedOnly",
                legacy={"hidden_gem_hit_rate": 0.5, "hidden_cards": ["Z"]},
            ),
        ]
    )
    fixture_path = tmp_path / "baseline.json"
    _write_fixture(fixture_path, pinned)

    live = _make_fixture(
        [
            FixtureEntry(
                commander="HasBoth",
                legacy={"hidden_gem_hit_rate": 0.1, "hidden_cards": []},
            ),
            # No rate or hidden_cards on the live side for PinnedOnly.
            FixtureEntry(commander="PinnedOnly", legacy={}),
        ]
    )
    _patch_build_fixture(monkeypatch, live)

    rc = handle_inspect_gems(_args(fixture_path, synergy, edhrec_db=str(edhrec)))
    assert rc == 0
    out = capsys.readouterr().out
    # HasBoth rendered first; PinnedOnly last.
    has_both_pos = out.index("HasBoth")
    pinned_only_pos = out.index("PinnedOnly")
    assert has_both_pos < pinned_only_pos
    # Δ column shows "—" for the None row — look for it on the PinnedOnly line.
    for line in out.splitlines():
        if "PinnedOnly" in line:
            assert "—" in line
            break
    else:
        raise AssertionError("PinnedOnly row not found in output")


# ---------------------------------------------------------------------------
# Edge: commas in names
# ---------------------------------------------------------------------------


def test_names_with_commas_render_correctly(
    tmp_path: Path,
    _stub_dbs: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Card + commander names with commas don't break markdown cells."""
    synergy, edhrec = _stub_dbs
    cmdr_name = "Korvold, Fae-Cursed King"
    gem_lost = "Curtains' Call"
    pinned = _make_fixture(
        [
            FixtureEntry(
                commander=cmdr_name,
                legacy={
                    "hidden_gem_hit_rate": 0.2,
                    "hidden_cards": [gem_lost, "Citizen, Angel"],
                },
            ),
        ]
    )
    fixture_path = tmp_path / "baseline.json"
    _write_fixture(fixture_path, pinned)

    live = _make_fixture(
        [
            FixtureEntry(
                commander=cmdr_name,
                legacy={"hidden_gem_hit_rate": 0.1, "hidden_cards": []},
            ),
        ]
    )
    _patch_build_fixture(monkeypatch, live)

    rc = handle_inspect_gems(_args(fixture_path, synergy, edhrec_db=str(edhrec)))
    assert rc == 0
    out = capsys.readouterr().out
    # Full commander name present, not split.
    assert cmdr_name in out
    # Gem name with apostrophe preserved.
    assert gem_lost in out

    # JSON format preserves commas cleanly.
    rc = handle_inspect_gems(_args(fixture_path, synergy, edhrec_db=str(edhrec), fmt="json"))
    assert rc == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["rows"][0]["commander"] == cmdr_name
    assert gem_lost in payload["rows"][0]["lost"]
