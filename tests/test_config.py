def test_fusion_config_entries():
    from mtg_synergy.config import SCORING_WEIGHTS, FUSION_MODEL_PATH, TOWER_EDHREC_PATH, USE_FUSION_MODEL
    assert "FUSION" in SCORING_WEIGHTS
    assert SCORING_WEIGHTS["FUSION"] == 10.0
    assert "fusion_model.lgb" in str(FUSION_MODEL_PATH)
    assert "tower_model_edhrec.npz" in str(TOWER_EDHREC_PATH)
    assert isinstance(USE_FUSION_MODEL, bool)


def test_config_paths_exist():
    from mtg_synergy.config import PROJECT_ROOT, DATA_DIR, DB_PATH
    assert PROJECT_ROOT.is_dir()
    assert DATA_DIR.is_dir()

def test_config_constants():
    from mtg_synergy.config import RECOMMENDATION_WEIGHTS
    assert "LLM" in RECOMMENDATION_WEIGHTS
    assert "TOWER" in RECOMMENDATION_WEIGHTS
    assert "RANK_TIEBREAK" in RECOMMENDATION_WEIGHTS
