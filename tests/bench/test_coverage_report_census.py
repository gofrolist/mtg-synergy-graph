import json

from mtg_synergy_graph.bench.coverage import CoverageMetrics
from mtg_synergy_graph.bench.coverage_report import (
    read_baseline,
    write_baseline,
)


def test_baseline_roundtrip(tmp_path):
    path = tmp_path / "baseline.json"
    metrics = {
        "Phenax, God of Deception": CoverageMetrics(0, 0, 0),
        "Korvold, Fae-Cursed King": CoverageMetrics(24, 130, 5),
    }
    write_baseline(path, metrics, config_hash="deadbeef")
    cfg, back = read_baseline(path)
    assert cfg == "deadbeef"
    assert back == metrics


def test_baseline_json_shape(tmp_path):
    path = tmp_path / "baseline.json"
    write_baseline(path, {"X": CoverageMetrics(1, 2, 3)}, config_hash="abc")
    doc = json.loads(path.read_text())
    assert doc["config_hash"] == "abc"
    assert doc["commanders"]["X"] == {
        "earned_top30": 1,
        "n_scored_cands": 2,
        "n_synergy_buckets": 3,
    }
