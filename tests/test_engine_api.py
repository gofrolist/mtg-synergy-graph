"""Public SynergyEngine surface tests (SPEC §14)."""

from __future__ import annotations

from pathlib import Path

import pytest

from mtg_synergy_graph import (
    HARD_FILTER_SCORE,
    Recommendation,
    RecommendationPage,
    SPEC_VERSION,
    SynergyEngine,
)
from mtg_synergy_graph.db import open_db
from mtg_synergy_graph.importer import import_cards_folder

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def engine_db(tmp_path_factory):
    db_path = tmp_path_factory.mktemp("engine") / "synergy.db"
    conn = open_db(db_path)
    import_cards_folder(conn, FIXTURES, scryfall_db=None)
    conn.close()
    return db_path


@pytest.fixture()
def engine(engine_db):
    eng = SynergyEngine(engine_db)
    yield eng
    eng.close()


# ---------------------------------------------------------------------------
# metadata
# ---------------------------------------------------------------------------


def test_metadata_reports_spec_version(engine):
    meta = engine.metadata()
    assert meta["spec_version"] == SPEC_VERSION
    assert "engine_version" in meta


# ---------------------------------------------------------------------------
# legal_cards — colour identity hard filter
# ---------------------------------------------------------------------------


def test_legal_cards_for_korvold_includes_red_and_excludes_white(engine):
    legal = set(engine.legal_cards("Korvold, Fae-Cursed King"))
    # Korvold is BRG. Phyrexian Altar is colourless → legal.
    assert "Phyrexian Altar" in legal
    # Wrath of God is W → NOT legal in Korvold colours.
    assert "Wrath of God" not in legal


def test_legal_cards_excludes_commander_itself(engine):
    legal = set(engine.legal_cards("Korvold, Fae-Cursed King"))
    assert "Korvold, Fae-Cursed King" not in legal


def test_legal_cards_excludes_non_edh_card_types(engine):
    """Planechase planes (e.g. ``Jund``, ``Murasa``) and other non-EDH
    types must NEVER appear in any commander's legal pool. They have
    matching triggers in the causal graph but aren't legal deck cards."""
    # Inject a synthetic plane into the fixture db so the test is
    # self-contained — we don't need a 32 k Forge import for it.
    engine._conn.execute(
        "INSERT OR REPLACE INTO cards (name, types, card_types, "
        "color_identity, supertypes, subtypes) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("Test Plane", "Plane Test", "Plane", "", "", "Test"),
    )
    engine._conn.commit()
    legal = set(engine.legal_cards("Korvold, Fae-Cursed King"))
    assert "Test Plane" not in legal
    page = engine.page("Korvold, Fae-Cursed King", offset=0, limit=1_000_000)
    assert "Test Plane" not in {r.card for r in page.items}


# ---------------------------------------------------------------------------
# page() — pagination contract
# ---------------------------------------------------------------------------


def test_page_returns_recommendation_page(engine):
    page = engine.page("Korvold, Fae-Cursed King", offset=0, limit=10)
    assert isinstance(page, RecommendationPage)
    assert page.commander == ["Korvold, Fae-Cursed King"]
    assert page.offset == 0
    assert page.limit == 10
    assert page.spec_version == SPEC_VERSION
    assert page.total >= len(page.items)
    assert all(isinstance(it, Recommendation) for it in page.items)


def test_page_total_equals_len_when_limit_huge(engine):
    page = engine.page("Korvold, Fae-Cursed King", offset=0, limit=1_000_000)
    assert page.total == len(page.items)


def test_page_offset_returns_subsequent_window(engine):
    full = engine.page("Korvold, Fae-Cursed King", offset=0, limit=1_000_000)
    if full.total < 4:
        pytest.skip("not enough cards in fixture for pagination check")
    head = engine.page("Korvold, Fae-Cursed King", offset=0, limit=2)
    tail = engine.page("Korvold, Fae-Cursed King", offset=2, limit=2)
    assert head.items[0].rank == 1
    assert tail.items[0].rank == 3
    head_names = [r.card for r in head.items]
    tail_names = [r.card for r in tail.items]
    assert not (set(head_names) & set(tail_names))


def test_page_rejects_negative_offset_and_zero_limit(engine):
    with pytest.raises(ValueError):
        engine.page("Korvold, Fae-Cursed King", offset=-1, limit=10)
    with pytest.raises(ValueError):
        engine.page("Korvold, Fae-Cursed King", offset=0, limit=0)


def test_page_unknown_commander_raises(engine):
    with pytest.raises(ValueError):
        engine.page("Bogus, the Made Up", offset=0, limit=10)


# ---------------------------------------------------------------------------
# Hard filters: Wrath of God must NOT appear in any Korvold page
# ---------------------------------------------------------------------------


def test_wrath_of_god_filtered_out_by_color_identity(engine):
    page = engine.page("Korvold, Fae-Cursed King", offset=0, limit=1_000_000)
    cards = [r.card for r in page.items]
    assert "Wrath of God" not in cards


# ---------------------------------------------------------------------------
# Phyrexian Altar appears with positive total for Korvold
# ---------------------------------------------------------------------------


def test_phyrexian_altar_ranks_for_korvold(engine):
    page = engine.page("Korvold, Fae-Cursed King", offset=0, limit=1_000_000)
    by_card = {r.card: r for r in page.items}
    assert "Phyrexian Altar" in by_card
    altar = by_card["Phyrexian Altar"]
    assert altar.total_score > 0
    assert altar.scores["cost_synergy"] > 0


# ---------------------------------------------------------------------------
# score_one — single-card path matches page() ordering
# ---------------------------------------------------------------------------


def test_score_one_returns_recommendation(engine):
    rec = engine.score_one("Korvold, Fae-Cursed King", "Phyrexian Altar")
    assert isinstance(rec, Recommendation)
    assert rec.card == "Phyrexian Altar"
    assert rec.total_score > 0
    assert rec.scores["cost_synergy"] > 0


def test_score_one_with_explanation_returns_lines(engine):
    rec = engine.score_one(
        "Korvold, Fae-Cursed King",
        "Phyrexian Altar",
        include_explanation=True,
    )
    assert rec.explanation is not None
    assert any("cost" in line.lower() for line in rec.explanation)


def test_score_one_unrelated_card_is_zero_or_filtered(engine):
    """Cathars' Crusade has nothing to do with Korvold mechanically (and is
    white, so it would also fail the colour-identity hard filter)."""
    rec = engine.score_one("Korvold, Fae-Cursed King", "Cathars' Crusade")
    # Either filtered (HARD_FILTER_SCORE-equivalent) or just no positive
    # synergy bucket — both outcomes are acceptable for the purposes of
    # the public API contract.
    assert rec.total_score <= 0 or rec.total_score == HARD_FILTER_SCORE
