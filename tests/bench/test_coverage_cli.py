from pathlib import Path

import pytest

from mtg_synergy_graph.bench.coverage_report import main

_DB_PATH = Path("data/synergy.db")


def test_queue_reads_baseline_and_prints(tmp_path, capsys):
    from mtg_synergy_graph.bench.coverage import CoverageMetrics
    from mtg_synergy_graph.bench.coverage_report import write_baseline

    bp = tmp_path / "baseline.json"
    write_baseline(
        bp,
        {"Zzz": CoverageMetrics(20, 100, 5), "Aaa": CoverageMetrics(0, 0, 0)},
        config_hash="abc",
    )
    rc = main(["queue", "--baseline", str(bp), "--top", "2"])
    out = capsys.readouterr().out
    assert rc == 0
    # Poorest first.
    assert out.index("Aaa") < out.index("Zzz")


def test_gate_reports_stale_baseline(tmp_path, capsys):
    from mtg_synergy_graph.bench.coverage import CoverageMetrics
    from mtg_synergy_graph.bench.coverage_report import write_baseline

    bp = tmp_path / "baseline.json"
    write_baseline(bp, {"A": CoverageMetrics(0, 0, 0)}, config_hash="OLD")
    # Force a mismatching live hash via the documented override flag.
    rc = main(
        [
            "gate",
            "--baseline",
            str(bp),
            "--cohort",
            "toughness_payoff",
            "--force-config-hash",
            "NEW",
        ]
    )
    out = capsys.readouterr().out
    assert "stale" in out.lower()
    assert rc != 0  # stale baseline is a non-zero exit


@pytest.mark.skipif(not _DB_PATH.exists(), reason="data/synergy.db not present")
def test_census_smoke(tmp_path):
    from mtg_synergy_graph.bench.coverage_report import read_baseline

    out = tmp_path / "baseline.json"
    rc = main(
        [
            "census",
            "--out",
            str(out),
            "--commander",
            "Phenax, God of Deception",
        ]
    )
    assert rc == 0
    _config_hash, metrics = read_baseline(out)
    # NOTE: the task-6 brief expected earned_top30 == 0 here (the design
    # doc's premise that Phenax "fires zero rules"). On the current
    # data/synergy.db, the already-active declarative `toughness_synergy`
    # rule (rules_seed.json, shipped 2026-06-09 in 68d0d8a — predates this
    # branch) DOES fire for Phenax (scales_with CardToughness -> Defender
    # keyword candidates), giving all 30 top-30 Walls a nonzero `scaling`
    # bucket. Under Task 1's committed "earned" definition (bucket
    # PRESENCE, see coverage.py docstring) that counts as earned, so the
    # measured value is 30/30, not 0/30. This smoke test asserts the
    # metric plumbing is wired correctly end-to-end (a real, bounded,
    # reproducible value) rather than the brief's stale expectation --
    # see task-6-report.md for the full discrepancy writeup.
    assert metrics["Phenax, God of Deception"].earned_top30 == 30
