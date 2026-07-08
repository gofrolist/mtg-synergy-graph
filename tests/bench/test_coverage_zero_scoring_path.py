"""The coverage instrument must not perturb scoring: config hash is stable."""

from mtg_synergy_graph.bench.tensor import compute_config_hash


def test_config_hash_unchanged_by_import():
    # Importing the instrument must not mutate any scoring-config module state.
    before = compute_config_hash()
    import mtg_synergy_graph.bench.coverage
    import mtg_synergy_graph.bench.coverage_report  # noqa: F401

    assert compute_config_hash() == before
