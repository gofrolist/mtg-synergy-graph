"""``bench.py audit --forensics`` handler + renderer tests (Unit 4 of
plan 2026-06-10-001).

DB-shell tests build a synthetic synergy.db from the committed Forge
fixture cards under ``tmp_path`` ONLY (the session conftest sentinel
fails the run on any stray ``*.db`` under the repo root or ``data/``),
so the handler runs against production-faithful ``engine.page()``
output. Seeding helpers are local copies of the ones in
``tests/bench/test_forensics_metrics.py`` (tests/ is not an importable
package); the argparse.Namespace pattern follows
``tests/bench/test_handle_unknowns.py``.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

import pytest

from mtg_synergy_graph.bench.forensics import (
    BUCKETS,
    N_RULES_BIN_LABELS,
    RATIO_BIN_LABELS,
    CommanderForensics,
    JustifiedDivergence,
    aggregate_forensics,
)
from mtg_synergy_graph.bench.forensics_report import (
    GATE_SATURATED_ANNOTATION,
    ForensicsRenderData,
    handle_forensics,
    render_forensics_markdown,
    rule_family,
)
from mtg_synergy_graph.bench.tensor import compute_config_hash
from mtg_synergy_graph.db import open_db
from mtg_synergy_graph.importer import import_cards_folder

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"

KORVOLD = "Korvold, Fae-Cursed King"
KORVOLD_SLUG = "korvold-fae-cursed-king"

#: Every ``##`` section header (plus load-bearing header lines) the
#: markdown report must contain (R2/R3/R9).
EXPECTED_MD_SECTIONS = (
    "# bench.py audit --forensics",
    "config_hash:",
    "fixture:",
    "Golden-set bubble",
    "Aggregate NDCG@30",
    "Aggregate raw DCG@30",
    "Total misses:",
    "## Aggregate bucket proportions",
    "## Worst-divergence commanders",
    "## OUTRANKED breakdown",
    "## Displacer profiles",
    "## NO_RULES port shapes",
    "## Justified divergence (R9)",
)


# ---------------------------------------------------------------------------
# Seeding helpers — local copies from sibling forensics test modules
# ---------------------------------------------------------------------------


def _write_fixture(path: Path, commanders: list[object]) -> None:
    """Minimal PinnedFixture JSON (schema v2 shape)."""
    payload = {
        "schema_version": 2,
        "config_hash": "irrelevant-for-forensics",
        "created_at": "2026-06-10T00:00:00+00:00",
        "entries": [{"commander": c, "scores": {}} for c in commanders],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def _seed_tensor_row(
    conn: sqlite3.Connection,
    commander: str,
    candidate: str,
    config_hash: str,
    *,
    rule_id: str = "test_rule",
    contribution: float = 1.0,
) -> None:
    conn.execute(
        "INSERT INTO rule_contributions "
        "(commander, candidate, rule_id, contribution, idf_weight, raw_count, config_hash, computed_at) "
        "VALUES (?, ?, ?, ?, 1.0, 1, ?, '2026-06-10T00:00:00+00:00')",
        (commander, candidate, rule_id, contribution, config_hash),
    )


def _make_tags_db(path: Path, rows: list[tuple[str, str, str, float]]) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE edhrec_card_synergy (commander_slug TEXT, card_name TEXT, section TEXT, synergy REAL)"
        )
        conn.executemany("INSERT INTO edhrec_card_synergy VALUES (?, ?, ?, ?)", rows)
        conn.commit()
    finally:
        conn.close()


def _args(
    db: Path,
    fixture: Path,
    tags: Path,
    *,
    fmt: str = "md",
    output: str | None = None,
) -> argparse.Namespace:
    """Namespace with the attrs handle_forensics reads (the
    test_handle_unknowns pattern)."""
    return argparse.Namespace(
        db=str(db),
        fixture=str(fixture),
        edhrec_db=str(tags),
        format=fmt,
        output=output,
    )


# ---------------------------------------------------------------------------
# Fixtures — module-scoped synergy.db (import is the slow part)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def synergy_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """synergy.db built from the committed Forge fixture cards, with
    tensor rows at the CURRENT config hash (tensor-populated
    precondition + displacer/justified inputs)."""
    db_path = tmp_path_factory.mktemp("handle_forensics") / "synergy.db"
    conn = open_db(db_path)
    try:
        import_cards_folder(conn, FIXTURES, scryfall_db=None)
        config_hash = compute_config_hash()
        _seed_tensor_row(conn, KORVOLD, "Phyrexian Altar", config_hash, rule_id="trigger_effect", contribution=2.0)
        _seed_tensor_row(conn, KORVOLD, "Bloodghast", config_hash, rule_id="gy_fuel_feeder", contribution=1.0)
        conn.commit()
    finally:
        conn.close()
    return db_path


@pytest.fixture()
def paths(synergy_db: Path, tmp_path: Path) -> dict[str, Path]:
    """tags.db + fixture.json with one ranked label, one HS member, and
    one unmatched EDHREC-only name (a guaranteed DATA_GAP miss)."""
    tags_path = tmp_path / "tags.db"
    fixture_path = tmp_path / "fixture.json"
    _make_tags_db(
        tags_path,
        [
            (KORVOLD_SLUG, "Phyrexian Altar", "High Synergy Cards", 2.0),
            (KORVOLD_SLUG, "Bloodghast", "Top Cards", 1.0),
            (KORVOLD_SLUG, "Imaginary Synergy Piece", "Creatures", 0.5),
        ],
    )
    _write_fixture(fixture_path, [KORVOLD])
    return {"db": synergy_db, "tags": tags_path, "fixture": fixture_path}


# ---------------------------------------------------------------------------
# rule_family — documented grouping choice
# ---------------------------------------------------------------------------


class TestRuleFamily:
    def test_derivable_families(self) -> None:
        assert rule_family("repl_damagedone_counters_stack") == "replacement_stack"
        assert rule_family("prowess_tribal") == "tribal"
        assert rule_family("gy_fuel_feeder") == "axis_feeder"

    def test_fallback_is_rule_id(self) -> None:
        assert rule_family("trigger_effect") == "trigger_effect"
        assert rule_family("self_bridging_cascade") == "self_bridging_cascade"


# ---------------------------------------------------------------------------
# Happy path — md sections + json mirror
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_markdown_contains_every_section(
        self,
        paths: dict[str, Path],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.chdir(tmp_path)
        rc = handle_forensics(_args(paths["db"], paths["fixture"], paths["tags"]))
        assert rc == 0
        out = capsys.readouterr().out
        for section in EXPECTED_MD_SECTIONS:
            assert section in out, f"missing section: {section}"
        # The unmatched EDHREC-only name is a DATA_GAP miss.
        assert "Total misses: 1" in out
        # Default output landed at .audit/forensics.md and mirrors stdout.
        default_target = tmp_path / ".audit" / "forensics.md"
        assert default_target.exists()
        assert default_target.read_text(encoding="utf-8") == out

    def test_json_parses_and_mirrors_bucket_counts(
        self,
        paths: dict[str, Path],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # chdir so the Unit-5 history append (default
        # .audit/forensics_history.csv) lands under tmp_path, never the
        # repo root.
        monkeypatch.chdir(tmp_path)
        out_path = tmp_path / "report.json"
        rc = handle_forensics(_args(paths["db"], paths["fixture"], paths["tags"], fmt="json", output=str(out_path)))
        assert rc == 0
        payload = json.loads(out_path.read_text(encoding="utf-8"))

        assert set(payload["bucket_counts"]) == set(BUCKETS)
        assert payload["aggregate"]["total_misses"] == sum(payload["bucket_counts"].values()) == 1
        assert payload["bucket_counts"]["DATA_GAP"] == 1
        assert payload["reason_counts"]["DATA_GAP"] == {"card_absent": 1}
        assert payload["aggregate"]["name_unmatched_misses"] == 1
        # Proportions sum to 100 over the five buckets.
        assert sum(payload["bucket_proportions"].values()) == pytest.approx(100.0)
        # Leaderboard row mirrors the entry (1 miss, divergent annotated).
        (row,) = payload["leaderboard"]
        assert row["commander"] == KORVOLD
        assert row["misses"] == 1
        assert row["tensor_candidates"] == 2
        assert isinstance(row["divergent"], int)
        # Displacer profile from the seeded tensor rows (top-30 hits both).
        per_cmdr = payload["displacers"]["per_commander"]
        assert per_cmdr[0]["commander"] == KORVOLD
        families = {f["family"]: f["share"] for f in per_cmdr[0]["families"]}
        assert families == {"trigger_effect": pytest.approx(2.0 / 3.0), "axis_feeder": pytest.approx(1.0 / 3.0)}
        # R9 block mirrors counts and distributions carry every bin key.
        r9 = payload["justified_divergence"]
        assert r9["divergent"] == r9["justified"] + r9["unjustified"]
        assert set(r9["n_rules_distribution"]) == set(N_RULES_BIN_LABELS)
        assert set(r9["ratio_distribution"]) == set(RATIO_BIN_LABELS)


# ---------------------------------------------------------------------------
# Empty miss set — zeros / em-dashes, exit 0
# ---------------------------------------------------------------------------


class TestEmptyMissSet:
    def test_zero_misses_renders_dashes(
        self,
        synergy_db: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Every label ranked inside the live top-30 → zero misses; the
        proportions column and leaderboard degrade to em-dashes."""
        tags_path = tmp_path / "tags.db"
        fixture_path = tmp_path / "fixture.json"
        _make_tags_db(
            tags_path,
            [
                (KORVOLD_SLUG, "Phyrexian Altar", "High Synergy Cards", 2.0),
                (KORVOLD_SLUG, "Bloodghast", "Top Cards", 1.0),
            ],
        )
        _write_fixture(fixture_path, [KORVOLD])

        monkeypatch.chdir(tmp_path)
        rc = handle_forensics(_args(synergy_db, fixture_path, tags_path))
        assert rc == 0
        out = capsys.readouterr().out
        assert "Total misses: 0" in out
        assert "— (no misses anywhere)" in out
        assert "— (no NO_RULES misses)" in out
        # Zero-count bucket rows render an em-dash share (no div-by-zero).
        for bucket in BUCKETS:
            assert f"| {bucket} | 0 | — |" in out


# ---------------------------------------------------------------------------
# Error paths — exit 2, NOTHING written
# ---------------------------------------------------------------------------


class TestErrorPaths:
    def test_missing_fixture_exits_2_writes_nothing(
        self,
        paths: dict[str, Path],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.chdir(tmp_path)
        missing_fixture = tmp_path / "nope.json"
        rc = handle_forensics(_args(paths["db"], missing_fixture, paths["tags"]))
        assert rc == 2
        err = capsys.readouterr().err
        assert "not found" in err
        assert not (tmp_path / ".audit").exists()

    def test_config_hash_mismatch_exits_2_writes_nothing(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Tensor rows ONLY at a stale hash → precondition exit 2 with a
        re-pin hint; no report file, no .audit/ directory."""
        db_path = tmp_path / "synergy.db"
        conn = open_db(db_path)
        try:
            _seed_tensor_row(conn, KORVOLD, "Phyrexian Altar", "stale-hash-not-current")
            conn.commit()
        finally:
            conn.close()
        tags_path = tmp_path / "tags.db"
        _make_tags_db(tags_path, [(KORVOLD_SLUG, "Phyrexian Altar", "High Synergy Cards", 2.0)])
        fixture_path = tmp_path / "fixture.json"
        _write_fixture(fixture_path, [KORVOLD])

        monkeypatch.chdir(tmp_path)
        out_path = tmp_path / "report.md"
        rc = handle_forensics(_args(db_path, fixture_path, tags_path, output=str(out_path)))
        assert rc == 2
        err = capsys.readouterr().err
        assert "stale or absent" in err
        assert "--repin" in err
        assert not out_path.exists()
        assert not (tmp_path / ".audit").exists()


# ---------------------------------------------------------------------------
# Output routing — --output skips .audit/; default degrades on OSError
# ---------------------------------------------------------------------------


class TestOutputRouting:
    def test_output_flag_writes_there_and_skips_audit_dir(
        self,
        paths: dict[str, Path],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.chdir(tmp_path)
        out_path = tmp_path / "report.md"
        rc = handle_forensics(_args(paths["db"], paths["fixture"], paths["tags"], output=str(out_path)))
        assert rc == 0
        assert out_path.exists()
        assert "# bench.py audit --forensics" in out_path.read_text(encoding="utf-8")
        # The REPORT skips .audit/ under --output; the Unit-5 history
        # append (an independent artifact) still lands there by default.
        assert not (tmp_path / ".audit" / "forensics.md").exists()
        assert "report written to" in capsys.readouterr().err

    def test_unwritable_default_audit_degrades_to_warning(
        self,
        paths: dict[str, Path],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A read-only ``.audit/`` must not fail the run: stderr warning,
        exit 0, report still on stdout (audit.py degrade pattern)."""
        monkeypatch.chdir(tmp_path)
        original_mkdir = Path.mkdir

        def _boom(self: Path, *args: object, **kwargs: object) -> None:
            if self.name == ".audit":
                raise PermissionError("read-only audit dir")
            original_mkdir(self, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(Path, "mkdir", _boom)
        rc = handle_forensics(_args(paths["db"], paths["fixture"], paths["tags"]))
        assert rc == 0
        cap = capsys.readouterr()
        assert "could not write" in cap.err.lower()
        assert ".audit" in cap.err
        assert "# bench.py audit --forensics" in cap.out


# ---------------------------------------------------------------------------
# Determinism — byte-identical reports across runs
# ---------------------------------------------------------------------------


class TestDeterminism:
    @pytest.mark.parametrize("fmt", ["md", "json"])
    def test_byte_identical_across_runs(
        self,
        paths: dict[str, Path],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        fmt: str,
    ) -> None:
        # chdir so the Unit-5 history append (default
        # .audit/forensics_history.csv) lands under tmp_path, never the
        # repo root.
        monkeypatch.chdir(tmp_path)
        first = tmp_path / f"first.{fmt}"
        second = tmp_path / f"second.{fmt}"
        assert handle_forensics(_args(paths["db"], paths["fixture"], paths["tags"], fmt=fmt, output=str(first))) == 0
        assert handle_forensics(_args(paths["db"], paths["fixture"], paths["tags"], fmt=fmt, output=str(second))) == 0
        assert first.read_bytes() == second.read_bytes()


# ---------------------------------------------------------------------------
# R9 gate-saturation annotation (pure renderer level)
# ---------------------------------------------------------------------------


class TestGateSaturationAnnotation:
    @staticmethod
    def _render_data_with_pass_rate(justified: int, divergent: int) -> ForensicsRenderData:
        view = JustifiedDivergence(
            divergent=divergent,
            justified_divergences=justified,
            unjustified_divergences=divergent - justified,
            gate_pass_rate=justified / divergent if divergent else None,
            listed_nonpositive=0,
            justified_cards=tuple(f"J{i}" for i in range(justified)),
            unjustified_cards=tuple(f"U{i}" for i in range(divergent - justified)),
            n_rules_distribution=dict.fromkeys(N_RULES_BIN_LABELS, 0),
            ratio_distribution=dict.fromkeys(RATIO_BIN_LABELS, 0),
        )
        entry = CommanderForensics(
            commander="General Gee",
            misses=(),
            bucket_counts=dict.fromkeys(BUCKETS, 0),
            live_top_30=(),
            ranking=(),
            justified_divergence=view,
        )
        report = aggregate_forensics([entry])
        return ForensicsRenderData(
            report=report,
            config_hash="cafebabe0000",
            fixture_path="fixture.json",
            enrichments=(),
            outranked_rank_quantiles=(("61-100", 0), ("101-500", 0), (">500", 0)),
            outranked_family_contributions=(),
            aggregate_displacer_shares=(),
            no_rules_port_shapes=(),
        )

    def test_saturated_gate_renders_annotation(self) -> None:
        out = render_forensics_markdown(self._render_data_with_pass_rate(justified=2, divergent=2))
        assert GATE_SATURATED_ANNOTATION in out
        assert "gate pass-rate: 1.0000" in out

    def test_unsaturated_gate_omits_annotation(self) -> None:
        out = render_forensics_markdown(self._render_data_with_pass_rate(justified=1, divergent=2))
        assert GATE_SATURATED_ANNOTATION not in out

    def test_bucket_share_column_is_a_real_percentage(self) -> None:
        """Regression: bucket_proportions() already returns 0-100 values.

        The share cell must not be scaled by 100 a second time (a 7.1%
        bucket once rendered as 713.2%).
        """
        counts = dict.fromkeys(BUCKETS, 0)
        counts["NEAR_MISS"] = 1
        counts["OUTRANKED"] = 3
        entry = CommanderForensics(
            commander="General Gee",
            misses=(),
            bucket_counts=counts,
            live_top_30=(),
            ranking=(),
        )
        report = aggregate_forensics([entry])
        data = ForensicsRenderData(
            report=report,
            config_hash="cafebabe0000",
            fixture_path="fixture.json",
            enrichments=(),
            outranked_rank_quantiles=(("61-100", 0), ("101-500", 0), (">500", 0)),
            outranked_family_contributions=(),
            aggregate_displacer_shares=(),
            no_rules_port_shapes=(),
        )
        out = render_forensics_markdown(data)
        assert "| NEAR_MISS | 1 | 25.0% |" in out
        assert "| OUTRANKED | 3 | 75.0% |" in out
        assert "2500.0%" not in out


# ---------------------------------------------------------------------------
# CLI wiring — dispatch + mutex group
# ---------------------------------------------------------------------------


class TestCliWiring:
    def test_cli_dispatches_forensics(
        self,
        paths: dict[str, Path],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        import mtg_synergy_graph.bench  # ensure handlers register  # noqa: F401
        from mtg_synergy_graph.bench.cli import main

        monkeypatch.chdir(tmp_path)
        rc = main(
            [
                "audit",
                "--forensics",
                "--db",
                str(paths["db"]),
                "--fixture",
                str(paths["fixture"]),
                "--edhrec-db",
                str(paths["tags"]),
            ]
        )
        assert rc == 0
        assert "# bench.py audit --forensics" in capsys.readouterr().out

    def test_forensics_mutually_exclusive_with_other_modes(self) -> None:
        from mtg_synergy_graph.bench.cli import main

        with pytest.raises(SystemExit):
            main(["audit", "--forensics", "--unknowns"])

    def test_format_csv_rejected_for_forensics(self) -> None:
        from mtg_synergy_graph.bench.cli import main

        with pytest.raises(SystemExit):
            main(["audit", "--forensics", "--format", "csv"])
