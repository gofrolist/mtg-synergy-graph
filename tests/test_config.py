def test_config_paths_exist():
    from mtg_synergy.config import DATA_DIR, DB_PATH
    assert DATA_DIR.is_dir()
