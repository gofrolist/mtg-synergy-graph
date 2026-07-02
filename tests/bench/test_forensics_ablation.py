"""Tiebreaker-ablation tests (Unit 6 of plan 2026-06-10-001, R8).

Pure tests drive :func:`compute_tiebreak_ablation` /
:func:`verify_production_sort` over hand-built
:class:`RankedCandidate` tuples (synthetic score ties, planted
sentinel drift). DB-shell tests build a synthetic synergy.db from the
committed Forge fixture cards under ``tmp_path`` ONLY (the session
conftest sentinel fails the run on any stray ``*.db`` under the repo
root or ``data/``) and exercise the ``--ablate-tiebreak`` section
presence/absence through :func:`handle_forensics` and the CLI
companion-flag warning.

Seeding helpers are local copies of the ones in
``tests/bench/test_handle_forensics.py`` (tests/ is not an importable
package).
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from mtg_synergy_graph.bench.forensics import (
    FORENSICS_PLAN_PATH,
    TIEBREAK_FLAG_THRESHOLD,
    ForensicsReconciliationError,
    RankedCandidate,
    TiebreakSelfCheckError,
    compute_tiebreak_ablation,
    verify_production_sort,
)
from mtg_synergy_graph.bench.forensics_report import (
    TIEBREAK_SECTION_HEADER,
    handle_forensics,
)
from mtg_synergy_graph.bench.tensor import compute_config_hash
from mtg_synergy_graph.db import open_db
from mtg_synergy_graph.importer import import_cards_folder

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"

KORVOLD = "Korvold, Fae-Cursed King"
KORVOLD_SLUG = "korvold-fae-cursed-king"

CMDR = "General Gee"
RUN_DATE = "2026-06-10"


# ---------------------------------------------------------------------------
# Pure helpers — captured rankings from (name, score, cmc, edhrec_rank) specs
# ---------------------------------------------------------------------------


def _ranking(specs: list[tuple[str, float, float, int]]) -> tuple[RankedCandidate, ...]:
    """Build a captured ranking, ranks assigned by the PRODUCTION key
    ``(-total_score, cmc, edhrec_rank, name)`` — exactly the order
    ``engine.page()`` would have emitted."""
    keyed = sorted(specs, key=lambda t: (-t[1], t[2], t[3], t[0]))
    return tuple(
        RankedCandidate(name=name, rank=position, total_score=score, cmc=cmc, edhrec_rank=rank)
        for position, (name, score, cmc, rank) in enumerate(keyed, start=1)
    )


def _dcg(gains: list[float]) -> float:
    return sum((math.pow(2.0, rel) - 1.0) / math.log2(i + 2) for i, rel in enumerate(gains) if rel > 0)


#: Synthetic tie scenario: the score-8 tie (cmc also tied) is decided
#: ONLY by edhrec_rank (Yarrow rank 1 vs Beta rank 9), and the score-10
#: tie is decided by cmc (Zinnia 1.0 vs Echo 2.0). With top_n=3 the
#: strong key loses Yarrow from the window (edhrec_rank credit) and the
#: weak key additionally scrambles Zinnia/Echo (cmc + edhrec_rank
#: credit) — so 0 < strong_delta < weak_delta.
TIE_SPECS: list[tuple[str, float, float, int]] = [
    ("Zinnia", 10.0, 1.0, 50),
    ("Echo", 10.0, 2.0, 1),
    ("Yarrow", 8.0, 2.0, 1),
    ("Beta", 8.0, 2.0, 9),
]
TIE_LABELS: dict[str, float] = {"Zinnia": 3.0, "Yarrow": 1.0}
TIE_TOP_N = 3


class TestSyntheticTies:
    def test_edhrec_rank_decided_ties_produce_ordered_bracket(self) -> None:
        ranking = _ranking(TIE_SPECS)
        # Sanity: production order is Zinnia, Echo, Yarrow, Beta.
        assert [rc.name for rc in ranking] == ["Zinnia", "Echo", "Yarrow", "Beta"]

        ablation = compute_tiebreak_ablation(
            {CMDR: ranking},
            {CMDR: TIE_LABELS},
            n_canonical=1,
            run_date=RUN_DATE,
            top_n=TIE_TOP_N,
        )

        ideal = _dcg([3.0, 1.0])
        expected_production = _dcg([3.0, 0.0, 1.0]) / ideal  # [Zinnia, Echo, Yarrow]
        expected_strong = _dcg([3.0, 0.0, 0.0]) / ideal  # [Zinnia, Echo, Beta]
        expected_weak = _dcg([0.0, 3.0, 0.0]) / ideal  # [Echo, Zinnia, Beta]

        assert ablation.production_ndcg == pytest.approx(expected_production)
        assert ablation.strong_ndcg == pytest.approx(expected_strong)
        assert ablation.weak_ndcg == pytest.approx(expected_weak)

        # Weak-key delta nonzero; strong-key delta smaller or equal;
        # both signed in the production-minus-replacement direction.
        assert ablation.weak_delta > 0.0
        assert ablation.strong_delta > 0.0
        assert ablation.strong_delta <= ablation.weak_delta
        assert ablation.weak_delta == pytest.approx(expected_production - expected_weak)
        assert ablation.strong_delta == pytest.approx(expected_production - expected_strong)

        # Range is (min, max)-ordered and the upper bound is the larger
        # production-minus-replacement delta.
        lo, hi = ablation.delta_range
        assert lo <= hi
        assert (lo, hi) == (
            min(ablation.weak_delta, ablation.strong_delta),
            max(ablation.weak_delta, ablation.strong_delta),
        )
        assert ablation.upper_bound == hi == ablation.weak_delta

    def test_canonical_denominator_dilutes_with_skipped_commanders(self) -> None:
        """Zero-label / skipped commanders contribute exactly 0.0 on
        every key — aggregates scale by the canonical denominator."""
        ranking = _ranking(TIE_SPECS)
        one = compute_tiebreak_ablation(
            {CMDR: ranking}, {CMDR: TIE_LABELS}, n_canonical=1, run_date=RUN_DATE, top_n=TIE_TOP_N
        )
        two = compute_tiebreak_ablation(
            {CMDR: ranking}, {CMDR: TIE_LABELS}, n_canonical=2, run_date=RUN_DATE, top_n=TIE_TOP_N
        )
        assert two.production_ndcg == pytest.approx(one.production_ndcg / 2)
        assert two.weak_delta == pytest.approx(one.weak_delta / 2)
        assert two.strong_delta == pytest.approx(one.strong_delta / 2)

    def test_n_canonical_below_rankings_raises(self) -> None:
        with pytest.raises(ValueError, match="n_canonical"):
            compute_tiebreak_ablation({CMDR: _ranking(TIE_SPECS)}, {CMDR: TIE_LABELS}, n_canonical=0, run_date=RUN_DATE)


class TestNoScoreTies:
    def test_no_ties_means_both_deltas_exactly_zero(self) -> None:
        """Distinct total_scores everywhere → all three keys agree on
        the prefix ordering → deltas are EXACTLY 0.0 (not approx)."""
        specs = [
            ("Zinnia", 10.0, 1.0, 50),
            ("Echo", 9.0, 2.0, 1),
            ("Yarrow", 8.0, 2.0, 1),
            ("Beta", 7.0, 2.0, 9),
        ]
        ablation = compute_tiebreak_ablation(
            {CMDR: _ranking(specs)},
            {CMDR: TIE_LABELS},
            n_canonical=1,
            run_date=RUN_DATE,
            top_n=3,
        )
        assert ablation.weak_delta == 0.0
        assert ablation.strong_delta == 0.0
        assert ablation.delta_range == (0.0, 0.0)
        assert ablation.upper_bound == 0.0
        assert ablation.flagged is False
        assert ablation.rule_history_markdown is None


# ---------------------------------------------------------------------------
# RULE_HISTORY flag text — emitted above the 0.01 threshold
# ---------------------------------------------------------------------------


class TestRuleHistoryFlag:
    def test_planted_large_tie_credit_emits_flag_text(self) -> None:
        ablation = compute_tiebreak_ablation(
            {CMDR: _ranking(TIE_SPECS)},
            {CMDR: TIE_LABELS},
            n_canonical=1,
            run_date=RUN_DATE,
            top_n=TIE_TOP_N,
        )
        assert ablation.upper_bound > TIEBREAK_FLAG_THRESHOLD
        assert ablation.flagged is True
        entry = ablation.rule_history_markdown
        assert entry is not None
        # Dated entry, citing the plan path, carrying the measured range.
        assert entry.startswith(f"## {RUN_DATE}")
        assert FORENSICS_PLAN_PATH in entry
        lo, hi = ablation.delta_range
        assert f"[{lo:+.6f}, {hi:+.6f}]" in entry
        assert f"{ablation.weak_delta:+.6f}" in entry
        assert f"{ablation.strong_delta:+.6f}" in entry
        assert "sort-key remediation" in entry


# ---------------------------------------------------------------------------
# Mandatory self-check — corrupted capture → exit-2-style exception
# ---------------------------------------------------------------------------


class TestSelfCheck:
    @staticmethod
    def _corrupted_ranking() -> tuple[RankedCandidate, ...]:
        """Corrupt Zinnia's captured cmc (1.0 → 5.0): the reconstructed
        production key now orders Echo before Zinnia while the captured
        rank order says Zinnia first — sentinel/NULL-drift stand-in."""
        ranking = _ranking(TIE_SPECS)
        return tuple(replace(rc, cmc=5.0) if rc.name == "Zinnia" else rc for rc in ranking)

    def test_verify_production_sort_raises_on_drift(self) -> None:
        with pytest.raises(TiebreakSelfCheckError, match=CMDR):
            verify_production_sort(CMDR, self._corrupted_ranking())

    def test_compute_raises_before_any_deltas(self) -> None:
        """One corrupt commander poisons the whole run — the self-check
        covers EVERY commander before any NDCG is computed."""
        with pytest.raises(TiebreakSelfCheckError) as excinfo:
            compute_tiebreak_ablation(
                {"Aardvark, Honest": _ranking(TIE_SPECS), CMDR: self._corrupted_ranking()},
                {CMDR: TIE_LABELS},
                n_canonical=2,
                run_date=RUN_DATE,
                top_n=TIE_TOP_N,
            )
        assert CMDR in str(excinfo.value)
        assert "no ablation" in str(excinfo.value).lower() or "deltas" in str(excinfo.value)

    def test_self_check_error_maps_to_exit_2_family(self) -> None:
        """The handler's exit-2 mapping catches ForensicsReconciliationError;
        the self-check error must be in that family."""
        assert issubclass(TiebreakSelfCheckError, ForensicsReconciliationError)

    def test_clean_capture_passes_and_returns_rank_order(self) -> None:
        ranking = _ranking(TIE_SPECS)
        captured = verify_production_sort(CMDR, ranking)
        assert [rc.name for rc in captured] == [rc.name for rc in ranking]


# ---------------------------------------------------------------------------
# Handler integration — section presence/absence, exit 2, determinism
# (seeding helpers are local copies from test_handle_forensics.py)
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
    tmp_path: Path,
    *,
    fmt: str = "md",
    output: str | None = None,
    ablate_tiebreak: bool = False,
) -> argparse.Namespace:
    """``forensics_history`` is always set explicitly under ``tmp_path``
    (the test_forensics_history.py pattern) so the Unit-5 history
    append never depends on chdir side effects."""
    return argparse.Namespace(
        db=str(db),
        fixture=str(fixture),
        edhrec_db=str(tags),
        format=fmt,
        output=output,
        ablate_tiebreak=ablate_tiebreak,
        forensics_history=str(tmp_path / "forensics_history.csv"),
    )


@pytest.fixture(scope="module")
def synergy_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """synergy.db built from the committed Forge fixture cards, with
    tensor rows at the CURRENT config hash."""
    db_path = tmp_path_factory.mktemp("forensics_ablation") / "synergy.db"
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


class TestHandlerSectionPresence:
    def test_md_section_present_only_with_flag(
        self,
        paths: dict[str, Path],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.chdir(tmp_path)
        rc = handle_forensics(_args(paths["db"], paths["fixture"], paths["tags"], tmp_path, ablate_tiebreak=True))
        assert rc == 0
        out = capsys.readouterr().out
        assert TIEBREAK_SECTION_HEADER in out
        assert "delta range: [" in out
        # The production-key self-check passed on the live engine
        # capture (the load-bearing Unit-1 prerequisite).
        assert "self-check passed on every commander" in out

        rc = handle_forensics(_args(paths["db"], paths["fixture"], paths["tags"], tmp_path, ablate_tiebreak=False))
        assert rc == 0
        assert TIEBREAK_SECTION_HEADER not in capsys.readouterr().out

    def test_json_block_present_only_with_flag(
        self,
        paths: dict[str, Path],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.chdir(tmp_path)
        with_flag = tmp_path / "with_flag.json"
        without_flag = tmp_path / "without_flag.json"
        assert (
            handle_forensics(
                _args(
                    paths["db"],
                    paths["fixture"],
                    paths["tags"],
                    tmp_path,
                    fmt="json",
                    output=str(with_flag),
                    ablate_tiebreak=True,
                )
            )
            == 0
        )
        assert (
            handle_forensics(
                _args(paths["db"], paths["fixture"], paths["tags"], tmp_path, fmt="json", output=str(without_flag))
            )
            == 0
        )

        payload = json.loads(with_flag.read_text(encoding="utf-8"))
        block = payload["tiebreak_ablation"]
        assert block["n_commanders"] == 1
        assert block["flag_threshold"] == TIEBREAK_FLAG_THRESHOLD
        assert block["delta_range"] == sorted([block["weak_delta"], block["strong_delta"]])
        assert block["upper_bound"] == max(block["weak_delta"], block["strong_delta"])
        assert block["flagged"] == (block["upper_bound"] > TIEBREAK_FLAG_THRESHOLD)
        if block["flagged"]:
            assert FORENSICS_PLAN_PATH in block["rule_history_entry"]
        else:
            assert block["rule_history_entry"] is None

        # One-off mode: absent entirely without the flag — no null stub.
        assert "tiebreak_ablation" not in json.loads(without_flag.read_text(encoding="utf-8"))

    def test_self_check_failure_exits_2_writes_nothing(
        self,
        paths: dict[str, Path],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A planted self-check failure maps to exit 2 BEFORE any write:
        no report file, no .audit/, no history row, no deltas."""
        import mtg_synergy_graph.bench.forensics_report as forensics_report_module

        def _boom(*args: object, **kwargs: object) -> object:
            raise TiebreakSelfCheckError("planted sentinel drift")

        monkeypatch.setattr(forensics_report_module, "compute_tiebreak_ablation", _boom)
        monkeypatch.chdir(tmp_path)
        out_path = tmp_path / "report.md"
        rc = handle_forensics(
            _args(paths["db"], paths["fixture"], paths["tags"], tmp_path, output=str(out_path), ablate_tiebreak=True)
        )
        assert rc == 2
        cap = capsys.readouterr()
        assert "planted sentinel drift" in cap.err
        assert "delta" not in cap.out
        assert not out_path.exists()
        assert not (tmp_path / ".audit").exists()


class TestDeterminism:
    @pytest.mark.parametrize("fmt", ["md", "json"])
    def test_repeat_runs_byte_identical_with_flag(
        self,
        paths: dict[str, Path],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        fmt: str,
    ) -> None:
        monkeypatch.chdir(tmp_path)
        first = tmp_path / f"first.{fmt}"
        second = tmp_path / f"second.{fmt}"
        for target in (first, second):
            rc = handle_forensics(
                _args(
                    paths["db"],
                    paths["fixture"],
                    paths["tags"],
                    tmp_path,
                    fmt=fmt,
                    output=str(target),
                    ablate_tiebreak=True,
                )
            )
            assert rc == 0
        assert first.read_bytes() == second.read_bytes()


# ---------------------------------------------------------------------------
# CLI companion-flag warning — never a mode of its own
# ---------------------------------------------------------------------------


class TestCompanionWarning:
    def test_warning_without_forensics(self, capsys: pytest.CaptureFixture[str]) -> None:
        """--ablate-tiebreak alone dispatches the default audit mode and
        warns on stderr (the --trend-n companion-flag pattern). The
        audit handler is swapped for a no-op so the test never runs a
        real audit."""
        from mtg_synergy_graph.bench import cli as bench_cli

        original = bench_cli._HANDLERS["audit"]
        try:
            bench_cli.register("audit", lambda args: 0)
            rc = bench_cli.main(["audit", "--ablate-tiebreak"])
        finally:
            bench_cli.register("audit", original)
        assert rc == 0
        assert "--ablate-tiebreak has no effect without --forensics" in capsys.readouterr().err

    def test_no_warning_with_forensics(self, capsys: pytest.CaptureFixture[str]) -> None:
        from mtg_synergy_graph.bench import cli as bench_cli

        original = bench_cli._HANDLERS["forensics"]
        try:
            bench_cli.register("forensics", lambda args: 0)
            rc = bench_cli.main(["audit", "--forensics", "--ablate-tiebreak"])
        finally:
            bench_cli.register("forensics", original)
        assert rc == 0
        assert "--ablate-tiebreak" not in capsys.readouterr().err

    def test_no_warning_without_the_flag(self, capsys: pytest.CaptureFixture[str]) -> None:
        from mtg_synergy_graph.bench import cli as bench_cli

        original = bench_cli._HANDLERS["audit"]
        try:
            bench_cli.register("audit", lambda args: 0)
            rc = bench_cli.main(["audit"])
        finally:
            bench_cli.register("audit", original)
        assert rc == 0
        assert "--ablate-tiebreak" not in capsys.readouterr().err
