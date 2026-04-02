def test_config_paths_exist():
    from mtg_synergy.config import PROJECT_ROOT, DATA_DIR, DB_PATH
    assert PROJECT_ROOT.is_dir()
    assert DATA_DIR.is_dir()


def test_config_db_pragmas():
    from mtg_synergy.config import DB_PRAGMAS
    assert "journal_mode" in DB_PRAGMAS
    assert "synchronous" in DB_PRAGMAS
