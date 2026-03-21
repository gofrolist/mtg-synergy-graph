"""Tests for cross_validate.py"""
import sqlite3
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from cross_validate import (
    THEME_MAP,
    validate_theme,
    run_full_validation,
    get_our_strategy_cards,
    resolve_names_to_oracle_ids,
)


def test_theme_map_covers_major_themes():
    major = ["tokens", "+1/+1-counters", "lifegain", "aristocrats", "artifacts"]
    for theme in major:
        assert theme in THEME_MAP


def test_theme_map_skips_infrastructure():
    """Generic/infrastructure themes should map to None."""
    assert THEME_MAP.get("combo") is None
    assert THEME_MAP.get("ramp") is None
    assert THEME_MAP.get("card-draw") is None


def test_theme_map_sacrifice_maps_to_aristocrats():
    assert THEME_MAP["sacrifice"] == "aristocrats"


def test_cross_validate_basic(tmp_db):
    """Token Maker is in EDHREC tokens and in our tokens strategy — should count as agreement."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("INSERT INTO cards (oracle_id, name) VALUES ('c1', 'Token Maker')")
    conn.execute(
        "INSERT INTO card_strategies (oracle_id, strategy, confidence) VALUES ('c1', 'tokens', 1.0)"
    )
    conn.commit()
    conn.close()

    edhrec_cards = [{"name": "Token Maker", "synergy": 0.20}]
    result = validate_theme("tokens", "tokens", edhrec_cards, tmp_db)

    assert result["agreement"] >= 1
    assert result["false_negatives"] == 0


def test_cross_validate_false_negative(tmp_db):
    """Card present in EDHREC but absent from our strategy is a false negative."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("INSERT INTO cards (oracle_id, name) VALUES ('c2', 'Impact Tremors')")
    # No card_strategies row for this card
    conn.commit()
    conn.close()

    edhrec_cards = [{"name": "Impact Tremors", "synergy": 0.23}]
    result = validate_theme("tokens", "tokens", edhrec_cards, tmp_db)

    assert result["false_negatives"] == 1
    assert result["agreement"] == 0
    fn = result["false_negative_cards"]
    assert len(fn) == 1
    assert fn[0]["name"] == "Impact Tremors"
    assert abs(fn[0]["synergy"] - 0.23) < 1e-6


def test_cross_validate_below_threshold_not_counted(tmp_db):
    """EDHREC cards with synergy <= threshold are not counted as high-synergy."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("INSERT INTO cards (oracle_id, name) VALUES ('c3', 'Low Synergy Card')")
    conn.commit()
    conn.close()

    edhrec_cards = [{"name": "Low Synergy Card", "synergy": 0.10}]
    result = validate_theme("tokens", "tokens", edhrec_cards, tmp_db)

    assert result["edhrec_high_synergy_count"] == 0
    assert result["false_negatives"] == 0
    assert result["agreement"] == 0


def test_cross_validate_card_not_in_db(tmp_db):
    """EDHREC cards not in our DB should be ignored (not counted as false negatives)."""
    # DB is empty — no cards inserted
    edhrec_cards = [{"name": "Unknown Card", "synergy": 0.30}]
    result = validate_theme("tokens", "tokens", edhrec_cards, tmp_db)

    assert result["edhrec_high_synergy_count"] == 0
    assert result["false_negatives"] == 0


def test_cross_validate_us_only(tmp_db):
    """Cards we tag but not in EDHREC high-synergy list count as us-only."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("INSERT INTO cards (oracle_id, name) VALUES ('c4', 'Our Card')")
    conn.execute(
        "INSERT INTO card_strategies (oracle_id, strategy, confidence) VALUES ('c4', 'tokens', 0.9)"
    )
    conn.commit()
    conn.close()

    # EDHREC has no high-synergy cards
    edhrec_cards = []
    result = validate_theme("tokens", "tokens", edhrec_cards, tmp_db)

    assert result["us_only_count"] >= 1


def test_cross_validate_confidence_filter(tmp_db):
    """Cards with confidence below 0.3 should not count towards our strategy set."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("INSERT INTO cards (oracle_id, name) VALUES ('c5', 'Weak Card')")
    conn.execute(
        "INSERT INTO card_strategies (oracle_id, strategy, confidence) VALUES ('c5', 'tokens', 0.1)"
    )
    # Also add it to EDHREC high-synergy
    conn.commit()
    conn.close()

    edhrec_cards = [{"name": "Weak Card", "synergy": 0.20}]
    result = validate_theme("tokens", "tokens", edhrec_cards, tmp_db)

    # Our set doesn't include it (low confidence) → it should be a false negative
    assert result["false_negatives"] == 1
    assert result["agreement"] == 0


def test_false_negatives_sorted_by_synergy(tmp_db):
    """False negatives should be sorted by synergy score descending."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("INSERT INTO cards (oracle_id, name) VALUES ('a1', 'Alpha Card')")
    conn.execute("INSERT INTO cards (oracle_id, name) VALUES ('a2', 'Beta Card')")
    conn.commit()
    conn.close()

    edhrec_cards = [
        {"name": "Alpha Card", "synergy": 0.16},
        {"name": "Beta Card", "synergy": 0.22},
    ]
    result = validate_theme("tokens", "tokens", edhrec_cards, tmp_db)

    fn = result["false_negative_cards"]
    assert len(fn) == 2
    # Beta Card has higher synergy so should come first
    assert fn[0]["name"] == "Beta Card"
    assert fn[1]["name"] == "Alpha Card"


def test_resolve_names_to_oracle_ids(tmp_db):
    """Name→oracle_id resolution should work for cards in DB."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("INSERT INTO cards (oracle_id, name) VALUES ('id-xyz', 'Sol Ring')")
    conn.commit()
    conn.close()

    mapping = resolve_names_to_oracle_ids(["Sol Ring", "Not In DB"], tmp_db)
    assert mapping["Sol Ring"] == "id-xyz"
    assert "Not In DB" not in mapping


def test_run_full_validation_with_mock_data(tmp_db):
    """run_full_validation returns a list of result dicts for mapped themes."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("INSERT INTO cards (oracle_id, name) VALUES ('tok1', 'Token Maker')")
    conn.execute(
        "INSERT INTO card_strategies (oracle_id, strategy, confidence) VALUES ('tok1', 'tokens', 1.0)"
    )
    conn.commit()
    conn.close()

    edhrec_data = {
        "tokens": [{"name": "Token Maker", "synergy": 0.20}],
    }
    results = run_full_validation(edhrec_data, db_path=tmp_db, single_theme="tokens")
    assert len(results) == 1
    r = results[0]
    assert r["edhrec_theme"] == "tokens"
    assert r["our_strategy"] == "tokens"
    assert r["agreement"] >= 1


def test_run_full_validation_skips_none_strategies(tmp_db):
    """Themes that map to None in THEME_MAP should be skipped."""
    edhrec_data = {
        "combo": [{"name": "Some Card", "synergy": 0.30}],
        "ramp": [{"name": "Sol Ring", "synergy": 0.40}],
    }
    results = run_full_validation(edhrec_data, db_path=tmp_db)
    theme_names = [r["edhrec_theme"] for r in results]
    assert "combo" not in theme_names
    assert "ramp" not in theme_names
