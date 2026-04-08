"""Tests for the §4.5 penalty rules (Phase 4.5)."""

from __future__ import annotations

from pathlib import Path

import pytest

from mtg_synergy_graph.db import open_db
from mtg_synergy_graph.importer import import_cards_folder
from mtg_synergy_graph.penalties import (
    HARD_FILTER_SCORE,
    NICHE_COUNTERS,
    NICHE_COUNTER_MULT,
    UNMET_TYPE_MULT,
    PenaltyContext,
    _exclusion_tokens,
    _token_subtype,
    apply_penalties_ctx,
    build_penalty_context,
)

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def populated_db(tmp_path_factory):
    db_path = tmp_path_factory.mktemp("penalties") / "synergy.db"
    conn = open_db(db_path)
    import_cards_folder(conn, FIXTURES)
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_exclusion_tokens_parses_non_segments():
    assert _exclusion_tokens("Creature.nonHuman+!Black") == {"Human"}
    assert _exclusion_tokens("Permanent.nonLand+Other") == {"Land"}
    assert _exclusion_tokens("Card.Self") == set()
    assert _exclusion_tokens(None) == set()


def test_exclusion_tokens_handles_multiple_non():
    assert _exclusion_tokens("Creature.nonGoblin.nonZombie") == {"Goblin", "Zombie"}


def test_token_subtype_parses_forge_script():
    assert _token_subtype("g_1_1_insect") == "Insect"
    assert _token_subtype("c_a_treasure_sac") == "Treasure"
    assert _token_subtype(None) is None
    assert _token_subtype("invalid") is None


def test_niche_counters_set_matches_spec():
    assert NICHE_COUNTERS == frozenset({"TIME", "EXPERIENCE", "ENERGY"})


# ---------------------------------------------------------------------------
# build_penalty_context bulk loaders
# ---------------------------------------------------------------------------


def test_build_penalty_context_for_korvold(populated_db):
    ctx = build_penalty_context(populated_db, ["Korvold, Fae-Cursed King"])
    assert isinstance(ctx, PenaltyContext)
    assert ctx.cmdr_identity == frozenset({"B", "G", "R"})
    assert "Dragon" in ctx.cmdr_subtypes
    assert "Creature" in ctx.cmdr_card_types
    assert ctx.cmdr_uses_p1p1 is True  # Korvold's TrigPutCounter uses P1P1
    assert "Counters" in ctx.cmdr_has_abilities
    # Korvold isn't tribal — he is a Dragon Noble but doesn't make Dragon
    # tokens, so the tribal-anchor heuristic must NOT classify him as tribal.
    # (Round 11 fixed the previous heuristic that conflated counter use
    # with tribal identity.)
    assert ctx.cmdr_is_tribal is False


def test_build_penalty_context_for_cathars_crusade(populated_db):
    """Cathars' Crusade is Korvold's spiritual sibling for the rule 9 test —
    it puts P1P1 counters on every creature, so cmdr_uses_p1p1 must be True
    when used as commander."""
    ctx = build_penalty_context(populated_db, ["Cathars' Crusade"])
    assert ctx.cmdr_uses_p1p1 is True
    assert "P1P1" in ctx.cmdr_counter_types


# ---------------------------------------------------------------------------
# Rule 1 + 2 hard filters (regression coverage from Phase 3)
# ---------------------------------------------------------------------------


def test_rule_1_wrong_color_identity_hard_filters(populated_db):
    ctx = build_penalty_context(populated_db, ["Korvold, Fae-Cursed King"])
    # Wrath of God is white — outside Korvold's BRG identity → hard filter.
    assert apply_penalties_ctx(ctx, "Wrath of God", 100.0) == HARD_FILTER_SCORE


def test_rule_1_passes_in_color(populated_db):
    ctx = build_penalty_context(populated_db, ["Korvold, Fae-Cursed King"])
    # Phyrexian Altar is colourless → passes the colour-identity filter.
    score = apply_penalties_ctx(ctx, "Phyrexian Altar", 50.0)
    assert score > 0


# ---------------------------------------------------------------------------
# Rule 4 — exclusion tokens are scoped to STATIC ports only
# ---------------------------------------------------------------------------


def test_rule_4_does_not_fire_on_trigger_filter_exclusions(populated_db):
    """Rule 4 is the regression test for the Mystic Remora bug:
    a trigger filter ``nonCreature`` must NOT count as 'excludes Creature'.
    Fixture set doesn't include Mystic Remora, but we can simulate the
    bulk-loader semantics: only static affected_scope contributes."""
    from mtg_synergy_graph.penalties import _bulk_load_excludes
    excludes = _bulk_load_excludes(populated_db)
    # No fixture card should produce a 'Creature' exclusion via this loader.
    for card_name, tokens in excludes.items():
        assert "Creature" not in tokens, (
            f"{card_name} produced 'Creature' exclusion — rule 4 over-fires"
        )


# ---------------------------------------------------------------------------
# Rule 9 — non-counter creatures penalty
# ---------------------------------------------------------------------------


def test_rule_9_skips_tribal_commanders():
    """A commander whose deck identity is its tribe must NOT lose every
    non-P1P1 creature to rule 9. The cmdr_is_tribal flag gates this."""
    ctx = PenaltyContext(
        cmdr_identity=frozenset({"R"}),
        cmdr_subtypes=frozenset({"Goblin"}),
        cmdr_card_types=frozenset({"Creature"}),
        cmdr_allows_background=False,
        cmdr_has_doctor=False,
        cmdr_counter_types=frozenset({"P1P1"}),
        cmdr_uses_p1p1=True,
        cmdr_self_mill=False,
        cmdr_has_abilities=frozenset({"Counters", "Token"}),
        cmdr_has_types=frozenset({"Goblin", "Creature"}),
        cmdr_has_keywords=frozenset(),
        cmdr_token_subtypes=frozenset({"Goblin"}),
        cmdr_is_tribal=True,
        candidate_rows={
            "Goblin Buddy": {
                "name": "Goblin Buddy",
                "color_identity": "R",
                "subtypes": "Goblin",
                "oracle_text": "",
                "card_types": "Creature",
                "cmc": 2,
                "deck_needs": None,
                "deck_hints": None,
                "deck_has": None,
            }
        },
    )
    assert apply_penalties_ctx(ctx, "Goblin Buddy", 100.0) == pytest.approx(100.0)


def test_rule_9_fires_on_non_tribal_counter_commander():
    """Same setup but cmdr_is_tribal=False — rule 9 should fire."""
    ctx = PenaltyContext(
        cmdr_identity=frozenset({"G"}),
        cmdr_subtypes=frozenset({"Druid"}),
        cmdr_card_types=frozenset({"Creature"}),
        cmdr_allows_background=False,
        cmdr_has_doctor=False,
        cmdr_counter_types=frozenset({"P1P1"}),
        cmdr_uses_p1p1=True,
        cmdr_self_mill=False,
        cmdr_has_abilities=frozenset({"Counters"}),
        cmdr_has_types=frozenset({"Druid"}),
        cmdr_has_keywords=frozenset(),
        cmdr_token_subtypes=frozenset(),
        cmdr_is_tribal=False,
        candidate_rows={
            "Random Bear": {
                "name": "Random Bear",
                "color_identity": "G",
                "subtypes": "Bear",
                "oracle_text": "",
                "card_types": "Creature",
                "cmc": 2,
                "deck_needs": None,
                "deck_hints": None,
                "deck_has": None,
            }
        },
    )
    assert apply_penalties_ctx(ctx, "Random Bear", 100.0) == pytest.approx(60.0)


# ---------------------------------------------------------------------------
# Rule 11 — unmet Type$ need
# ---------------------------------------------------------------------------


def test_rule_11_unmet_type_need_penalises():
    ctx = PenaltyContext(
        cmdr_identity=frozenset({"R"}),
        cmdr_subtypes=frozenset({"Wizard"}),
        cmdr_card_types=frozenset({"Creature"}),
        cmdr_allows_background=False,
        cmdr_has_doctor=False,
        cmdr_counter_types=frozenset(),
        cmdr_uses_p1p1=False,
        cmdr_self_mill=False,
        cmdr_has_abilities=frozenset(),
        cmdr_has_types=frozenset({"Wizard", "Creature"}),
        cmdr_has_keywords=frozenset(),
        cmdr_token_subtypes=frozenset(),
        cmdr_is_tribal=False,
        candidate_rows={
            "Dinosaur Buddy": {
                "name": "Dinosaur Buddy",
                "color_identity": "R",
                "subtypes": "Dinosaur",
                "oracle_text": "",
                "card_types": "Creature",
                "cmc": 4,
                "deck_needs": '{"Type": ["Dinosaur"]}',
                "deck_hints": None,
                "deck_has": None,
            }
        },
    )
    score = apply_penalties_ctx(ctx, "Dinosaur Buddy", 100.0)
    assert score == pytest.approx(100.0 * UNMET_TYPE_MULT)


def test_rule_11_satisfied_when_commander_has_type():
    ctx = PenaltyContext(
        cmdr_identity=frozenset({"R"}),
        cmdr_subtypes=frozenset({"Dinosaur"}),
        cmdr_card_types=frozenset({"Creature"}),
        cmdr_allows_background=False,
        cmdr_has_doctor=False,
        cmdr_counter_types=frozenset(),
        cmdr_uses_p1p1=False,
        cmdr_self_mill=False,
        cmdr_has_abilities=frozenset(),
        cmdr_has_types=frozenset({"Dinosaur", "Creature"}),
        cmdr_has_keywords=frozenset(),
        cmdr_token_subtypes=frozenset(),
        cmdr_is_tribal=True,
        candidate_rows={
            "Dinosaur Buddy": {
                "name": "Dinosaur Buddy",
                "color_identity": "R",
                "subtypes": "Dinosaur",
                "oracle_text": "",
                "card_types": "Creature",
                "cmc": 4,
                "deck_needs": '{"Type": ["Dinosaur"]}',
                "deck_hints": None,
                "deck_has": None,
            }
        },
    )
    assert apply_penalties_ctx(ctx, "Dinosaur Buddy", 100.0) == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# Rule 12 — unmet Ability$ need
# ---------------------------------------------------------------------------


def test_rule_12_unmet_ability_need():
    ctx = PenaltyContext(
        cmdr_identity=frozenset({"W"}),
        cmdr_subtypes=frozenset({"Soldier"}),
        cmdr_card_types=frozenset({"Creature"}),
        cmdr_allows_background=False,
        cmdr_has_doctor=False,
        cmdr_counter_types=frozenset(),
        cmdr_uses_p1p1=False,
        cmdr_self_mill=False,
        cmdr_has_abilities=frozenset({"Token"}),
        cmdr_has_types=frozenset({"Soldier", "Creature"}),
        cmdr_has_keywords=frozenset(),
        cmdr_token_subtypes=frozenset(),
        cmdr_is_tribal=True,
        candidate_rows={
            "Lifegain Synergist": {
                "name": "Lifegain Synergist",
                "color_identity": "W",
                "subtypes": "",
                "oracle_text": "",
                "card_types": "Enchantment",
                "cmc": 3,
                "deck_needs": '{"Ability": ["LifeGain"]}',
                "deck_hints": None,
                "deck_has": None,
            }
        },
    )
    assert apply_penalties_ctx(ctx, "Lifegain Synergist", 100.0) == pytest.approx(85.0)


# ---------------------------------------------------------------------------
# Rule 7 — niche counter penalty
# ---------------------------------------------------------------------------


def test_rule_7_niche_counter_penalises_when_commander_does_not_use_them():
    ctx = PenaltyContext(
        cmdr_identity=frozenset({"G"}),
        cmdr_subtypes=frozenset({"Elf"}),
        cmdr_card_types=frozenset({"Creature"}),
        cmdr_allows_background=False,
        cmdr_has_doctor=False,
        cmdr_counter_types=frozenset({"P1P1"}),
        cmdr_uses_p1p1=True,
        cmdr_self_mill=False,
        cmdr_has_abilities=frozenset({"Counters"}),
        cmdr_has_types=frozenset({"Elf"}),
        cmdr_has_keywords=frozenset(),
        cmdr_token_subtypes=frozenset({"Elf"}),
        cmdr_is_tribal=True,
        candidate_rows={
            "Energy Engine": {
                "name": "Energy Engine",
                "color_identity": "G",
                "subtypes": "",
                "oracle_text": "",
                "card_types": "Artifact",
                "cmc": 3,
                "deck_needs": None,
                "deck_hints": None,
                "deck_has": None,
            }
        },
        candidate_counter_types={"Energy Engine": {"ENERGY"}},
    )
    assert apply_penalties_ctx(ctx, "Energy Engine", 100.0) == pytest.approx(
        100.0 * NICHE_COUNTER_MULT
    )
