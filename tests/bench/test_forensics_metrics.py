"""Forensics metric-sidecar tests (Unit 2 of plan 2026-06-10-001).

Pure tests cover :func:`compute_raw_dcg` (hand-computed gains /
discounts mirroring ``validate.compute_ndcg``) and
:func:`reconcile_canonical_ndcg`; DB-shell tests build a synthetic
synergy.db from the committed Forge fixture cards under ``tmp_path``
ONLY (the session conftest sentinel fails the run on any stray
``*.db`` under the repo root or ``data/``), so the engine produces a
real nonzero ranking for Korvold and the NDCG sidecars, the
reconciliation assertion, and the sampled independent-engine check
are exercised against production-faithful ``engine.page()`` output.

Seeding helpers are local copies of the ones in
``tests/bench/test_forensics_classify.py`` (tests/ is not an
importable package).
"""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from mtg_synergy_graph.bench import forensics
from mtg_synergy_graph.bench.forensics import (
    BUCKET_DATA_GAP,
    ForensicsPreconditionError,
    ForensicsReconciliationError,
    ReconciliationPair,
    aggregate_forensics,
    compute_forensics,
    compute_raw_dcg,
    reconcile_canonical_ndcg,
    run_independent_engine_check,
)
from mtg_synergy_graph.bench.tensor import compute_config_hash
from mtg_synergy_graph.db import open_db
from mtg_synergy_graph.engine import SynergyEngine
from mtg_synergy_graph.importer import import_cards_folder
from mtg_synergy_graph.validate import compute_ndcg

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"

KORVOLD = "Korvold, Fae-Cursed King"
KORVOLD_SLUG = "korvold-fae-cursed-king"
LABELLESS = "Labelless Lass"

#: Graded labels for Korvold: two cards the fixture engine actually
#: ranks (validate-path NDCG > 0) plus one unranked EDHREC-only name.
LABELS: dict[str, float] = {
    "Phyrexian Altar": 2.0,
    "Bloodghast": 1.0,
    "Imaginary Synergy Piece": 0.5,
}


def _ideal_dcg(labels: dict[str, float], k: int = 30) -> float:
    """Ideal DCG mirroring validate.compute_ndcg's conventions."""
    scores = sorted((v for v in labels.values() if v > 0), reverse=True)[:k]
    return sum((math.pow(2.0, rel) - 1.0) / math.log2(i + 2) for i, rel in enumerate(scores))


# ---------------------------------------------------------------------------
# Seeding helpers — local copies from test_forensics_classify.py
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


def _seed_tensor_row(conn: sqlite3.Connection, commander: str, candidate: str, config_hash: str) -> None:
    conn.execute(
        "INSERT INTO rule_contributions "
        "(commander, candidate, rule_id, contribution, idf_weight, raw_count, config_hash, computed_at) "
        "VALUES (?, ?, 'test_rule', 1.0, 1.0, 1, ?, '2026-06-10T00:00:00+00:00')",
        (commander, candidate, config_hash),
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


# ---------------------------------------------------------------------------
# compute_raw_dcg — pure, hand-computed
# ---------------------------------------------------------------------------


class TestComputeRawDcg:
    def test_known_case_hand_computed(self) -> None:
        """labels A=2, B=1; predicted [A, X, B] → 3/log2(2) + 1/log2(4)."""
        labels = {"A": 2.0, "B": 1.0}
        raw = compute_raw_dcg(["A", "X", "B"], labels)
        assert raw == pytest.approx(3.0 / math.log2(2) + 1.0 / math.log2(4))
        assert raw == pytest.approx(3.5)

    def test_equals_ndcg_times_ideal_dcg(self) -> None:
        labels = {"A": 2.0, "B": 1.0}
        predicted = ["A", "X", "B"]
        ndcg = compute_ndcg(predicted, labels, k=30)
        assert compute_raw_dcg(predicted, labels) == pytest.approx(ndcg * _ideal_dcg(labels))

    def test_non_negative_and_zero_cases(self) -> None:
        assert compute_raw_dcg(["A", "B"], {"A": 2.0}) >= 0.0
        assert compute_raw_dcg(["X", "Y"], {"A": 2.0}) == 0.0  # no relevant hits
        assert compute_raw_dcg(["A"], {}) == 0.0  # empty labels (compute_ndcg parity)
        assert compute_raw_dcg(["A"], {"A": 2.0}, k=0) == 0.0  # k <= 0 (parity)


# ---------------------------------------------------------------------------
# reconcile_canonical_ndcg — pure
# ---------------------------------------------------------------------------


class TestReconcileCanonicalNdcg:
    def test_matching_pairs_return_aggregates(self) -> None:
        pairs = [
            ReconciliationPair("Alpha", 0.5, 0.5),
            ReconciliationPair("Beta", 0.0, 0.0),
        ]
        assert reconcile_canonical_ndcg(pairs) == (pytest.approx(0.25), pytest.approx(0.25))

    def test_within_epsilon_passes(self) -> None:
        pairs = [ReconciliationPair("Alpha", 0.5, 0.5 + 5e-7)]
        agg_a, agg_b = reconcile_canonical_ndcg(pairs)
        assert abs(agg_a - agg_b) <= 1e-6

    def test_divergence_names_first_divergent_commander(self) -> None:
        pairs = [
            ReconciliationPair("Alpha", 0.5, 0.5),
            ReconciliationPair("Beta", 0.9, 0.1),
            ReconciliationPair("Gamma", 0.3, 0.8),
        ]
        with pytest.raises(ForensicsReconciliationError, match="'Beta'"):
            reconcile_canonical_ndcg(pairs)

    def test_empty_pairs_return_zero(self) -> None:
        assert reconcile_canonical_ndcg([]) == (0.0, 0.0)


# ---------------------------------------------------------------------------
# DB-shell integration — fixture-card engine, tmp_path DBs only
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def synergy_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """synergy.db built from the committed Forge fixture cards.

    Korvold pages a real nonzero ranking here (Phyrexian Altar,
    Bloodghast, ... — see test_engine_api.py), so the NDCG sidecars
    are exercised against production-faithful page() output. One
    tensor row at the CURRENT config hash satisfies the
    tensor-populated precondition.
    """
    db_path = tmp_path_factory.mktemp("forensics_metrics") / "synergy.db"
    conn = open_db(db_path)
    try:
        import_cards_folder(conn, FIXTURES, scryfall_db=None)
        _seed_tensor_row(conn, KORVOLD, "Phyrexian Altar", compute_config_hash())
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
            (KORVOLD_SLUG, "Phyrexian Altar", "High Synergy Cards", LABELS["Phyrexian Altar"]),
            (KORVOLD_SLUG, "Bloodghast", "Top Cards", LABELS["Bloodghast"]),
            (KORVOLD_SLUG, "Imaginary Synergy Piece", "Creatures", LABELS["Imaginary Synergy Piece"]),
            # Zero-label commander: no rows for "Labelless Lass".
        ],
    )
    _write_fixture(fixture_path, [KORVOLD, LABELLESS])
    return {"db": synergy_db, "tags": tags_path, "fixture": fixture_path}


def _run(paths: dict[str, Path], **kwargs: object):
    return compute_forensics(
        db_path=paths["db"],
        tags_path=paths["tags"],
        fixture_path=paths["fixture"],
        **kwargs,  # type: ignore[arg-type]
    )


class TestMetricSidecars:
    def test_per_commander_ndcg_matches_validate_path(self, paths: dict[str, Path]) -> None:
        """ndcg30 equals the canonical validate-path value computed
        independently in this test (page(limit=30) + compute_ndcg)."""
        report = _run(paths, independent_check=False)
        entry = next(e for e in report.entries if e.commander == KORVOLD)

        with SynergyEngine(paths["db"]) as eng:
            window = [rec.card for rec in eng.page([KORVOLD], offset=0, limit=30).items]
        expected = compute_ndcg(window, LABELS, k=30)
        assert expected > 0.0  # labels hit ranked cards — meaningful case
        assert entry.ndcg30 == pytest.approx(expected, abs=1e-12)

    def test_raw_dcg_equals_ndcg_times_ideal_in_report(self, paths: dict[str, Path]) -> None:
        report = _run(paths, independent_check=False)
        entry = next(e for e in report.entries if e.commander == KORVOLD)
        assert entry.raw_dcg30 >= 0.0
        assert entry.raw_dcg30 == pytest.approx(entry.ndcg30 * _ideal_dcg(LABELS), abs=1e-12)

    def test_zero_label_commander_canonical_aggregate(self, paths: dict[str, Path]) -> None:
        """Zero-label commander: 0.0 in the canonical denominator,
        absent from the per-commander (exclusion-based) bucket table."""
        report = _run(paths, independent_check=False)
        assert report.skipped_commanders == (LABELLESS,)
        assert [e.commander for e in report.entries] == [KORVOLD]

        entry = report.entries[0]
        # Canonical denominator = 2 (Korvold + zero-label at 0.0).
        assert report.aggregate_ndcg_canonical == pytest.approx(entry.ndcg30 / 2, abs=1e-12)
        assert report.aggregate_raw_dcg_canonical == pytest.approx(entry.raw_dcg30 / 2, abs=1e-12)

    def test_miss_classification_unchanged_by_sidecars(self, paths: dict[str, Path]) -> None:
        """Ranked labels are not misses; the unranked EDHREC-only name
        still classifies (Unit 1 behavior preserved under Unit 2)."""
        report = _run(paths, independent_check=False)
        entry = report.entries[0]
        by_name = {m.card_name: m for m in entry.misses}
        assert set(by_name) == {"Imaginary Synergy Piece"}
        assert by_name["Imaginary Synergy Piece"].bucket == BUCKET_DATA_GAP


class TestReconciliationEndToEnd:
    def test_reconciliation_and_independent_check_pass(self, paths: dict[str, Path]) -> None:
        """Default run: reconciliation + sampled independent-engine
        check both pass; aggregate matches an external recompute
        within 1e-6."""
        report = _run(paths)  # independent_check defaults ON

        with SynergyEngine(paths["db"]) as eng:
            window = [rec.card for rec in eng.page([KORVOLD], offset=0, limit=30).items]
        recompute_aggregate = compute_ndcg(window, LABELS, k=30) / 2  # canonical denominator
        assert abs(report.aggregate_ndcg_canonical - recompute_aggregate) <= 1e-6

    def test_planted_divergence_names_commander(self, paths: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
        """A planted rank-extraction bug (wrong recompute window) must
        trip the reconciliation assertion and name the commander."""

        def _wrong_window(engine: SynergyEngine, commander: str, *, top_n: int = 30) -> tuple[str, ...]:
            return ()

        monkeypatch.setattr(forensics, "extract_canonical_window", _wrong_window)
        with pytest.raises(ForensicsReconciliationError, match="Korvold, Fae-Cursed King"):
            _run(paths, independent_check=False)

    def test_independent_check_runs_by_default_and_is_skippable(
        self, paths: dict[str, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[dict[str, object]] = []
        real = forensics.run_independent_engine_check

        def _recorder(report: forensics.ForensicsReport, **kwargs: object) -> tuple[str, ...]:
            calls.append(kwargs)
            return real(report, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(forensics, "run_independent_engine_check", _recorder)
        _run(paths, independent_check=False)
        assert calls == []
        _run(paths)
        assert len(calls) == 1


class TestIndependentEngineCheck:
    def test_sampled_check_agrees_on_synthetic_db(self, paths: dict[str, Path]) -> None:
        report = _run(paths, independent_check=False)
        sampled = run_independent_engine_check(
            report,
            db_path=paths["db"],
            tags_path=paths["tags"],
        )
        assert sampled == (KORVOLD,)  # first-N-alphabetical, non-skipped only

    def test_sampled_check_detects_tampered_ndcg(self, paths: dict[str, Path]) -> None:
        report = _run(paths, independent_check=False)
        tampered_entry = replace(report.entries[0], ndcg30=report.entries[0].ndcg30 + 0.25)
        tampered = aggregate_forensics([tampered_entry])
        with pytest.raises(ForensicsReconciliationError, match="Korvold, Fae-Cursed King"):
            run_independent_engine_check(
                tampered,
                db_path=paths["db"],
                tags_path=paths["tags"],
            )

    def test_missing_tags_db_raises_precondition(self, paths: dict[str, Path], tmp_path: Path) -> None:
        """Guard against sqlite silently materializing a missing tags.db."""
        report = _run(paths, independent_check=False)
        missing = tmp_path / "missing_tags.db"
        with pytest.raises(ForensicsPreconditionError, match="not found"):
            run_independent_engine_check(report, db_path=paths["db"], tags_path=missing)
        assert not missing.exists()
