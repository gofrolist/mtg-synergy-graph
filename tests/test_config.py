def test_config_paths_exist():
    from mtg_synergy.config import PROJECT_ROOT, DATA_DIR, DB_PATH
    assert PROJECT_ROOT.is_dir()
    assert DATA_DIR.is_dir()

def test_config_constants():
    from mtg_synergy.config import RECOMMENDATION_WEIGHTS
    assert "LLM" in RECOMMENDATION_WEIGHTS
    assert "TOWER" in RECOMMENDATION_WEIGHTS
    assert "RANK_TIEBREAK" in RECOMMENDATION_WEIGHTS
