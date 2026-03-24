# tests/test_ast_types.py
"""Tests for AST dataclass construction and JSON serialization."""
import json
import pytest


def test_object_filter_basic():
    from mtg_synergy.parse.ast_types import ObjectFilter
    f = ObjectFilter(card_type="creature", subtype="Goblin", controller="you")
    assert f.card_type == "creature"
    assert f.subtype == "Goblin"
    assert f.controller == "you"
    assert f.is_token is None
    assert f.is_another is None


def test_object_filter_defaults():
    from mtg_synergy.parse.ast_types import ObjectFilter
    f = ObjectFilter()
    assert f.card_type is None
    assert f.subtype is None
    assert f.controller is None


def test_amount_fixed():
    from mtg_synergy.parse.ast_types import Amount
    a = Amount(value=2)
    assert a.value == 2
    assert a.scales_with is None


def test_amount_variable():
    from mtg_synergy.parse.ast_types import Amount, ScalesWith
    a = Amount(value="X", scales_with=ScalesWith(what="Goblins you control", how="linear"))
    assert a.value == "X"
    assert a.scales_with.how == "linear"


def test_cost_with_sacrifice():
    from mtg_synergy.parse.ast_types import Cost, ObjectFilter, ManaAmount
    c = Cost(
        mana=ManaAmount(total=2, colors={"generic": 2}),
        tap=True,
        sacrifice=ObjectFilter(card_type="creature"),
    )
    assert c.mana.total == 2
    assert c.tap is True
    assert c.sacrifice.card_type == "creature"
    assert c.pay_life is None


def test_trigger():
    from mtg_synergy.parse.ast_types import Trigger, ObjectFilter
    t = Trigger(
        event="enters_the_battlefield",
        subject=ObjectFilter(card_type="creature", subtype="Goblin", controller="you"),
    )
    assert t.event == "enters_the_battlefield"
    assert t.subject.subtype == "Goblin"
    assert t.condition is None


def test_condition_structured():
    from mtg_synergy.parse.ast_types import Condition
    c = Condition(kind="count_threshold", what="creatures you control",
                  comparator=">=", value=3, restrictiveness="mild")
    assert c.kind == "count_threshold"
    assert c.value == 3
    assert c.raw is None


def test_condition_raw_fallback():
    from mtg_synergy.parse.ast_types import Condition
    c = Condition(kind="raw", raw="if you both own and control it",
                  restrictiveness="severe")
    assert c.kind == "raw"
    assert c.raw == "if you both own and control it"


def test_effect_create_token():
    from mtg_synergy.parse.ast_types import Effect, Amount, TokenDef
    e = Effect(
        verb="create",
        amount=Amount(value=2),
        token=TokenDef(card_type="creature", subtype="Goblin",
                       power=1, toughness=1, keywords=[], color="red"),
    )
    assert e.verb == "create"
    assert e.token.subtype == "Goblin"
    assert e.token.power == 1


def test_ability_triggered():
    from mtg_synergy.parse.ast_types import Ability, Trigger, Effect, Amount, ObjectFilter
    a = Ability(
        kind="triggered",
        trigger=Trigger(event="enters_the_battlefield",
                        subject=ObjectFilter(card_type="creature")),
        effects=[Effect(verb="deal_damage", amount=Amount(value=2),
                        target=ObjectFilter(controller="opponent"))],
    )
    assert a.kind == "triggered"
    assert a.trigger.event == "enters_the_battlefield"
    assert len(a.effects) == 1
    assert a.restrictions is None


def test_ability_with_restrictions():
    from mtg_synergy.parse.ast_types import Ability, Effect, Amount, Restrictions
    a = Ability(
        kind="activated",
        effects=[Effect(verb="draw", amount=Amount(value=1))],
        restrictions=Restrictions(once_per_turn=True, sorcery_speed=True),
    )
    assert a.restrictions.once_per_turn is True
    assert a.restrictions.sorcery_speed is True
    assert a.restrictions.once_per_game is False


def test_mana_amount():
    from mtg_synergy.parse.ast_types import ManaAmount
    m = ManaAmount(total=5, colors={"G": 1, "generic": 4})
    assert m.total == 5
    assert m.colors["G"] == 1
    assert m.is_any_color is False


def test_mana_amount_any_color():
    from mtg_synergy.parse.ast_types import ManaAmount
    m = ManaAmount(total=1, colors={"any": 1}, is_any_color=True)
    assert m.is_any_color is True


def test_effect_with_condition():
    from mtg_synergy.parse.ast_types import Effect, Amount, Condition
    e = Effect(
        verb="draw", amount=Amount(value=1),
        condition=Condition(kind="raw", raw="unless that player pays {1}",
                            restrictiveness="mild"),
    )
    assert e.condition.kind == "raw"
    assert e.unresolved_ref is None


def test_effect_with_unresolved_ref():
    from mtg_synergy.parse.ast_types import Effect, Amount, ObjectFilter
    e = Effect(verb="return", amount=Amount(value=1),
               target=ObjectFilter(), destination="battlefield",
               unresolved_ref="it")
    assert e.unresolved_ref == "it"


def test_ability_to_dict():
    """AST nodes should be JSON-serializable via to_dict()."""
    from mtg_synergy.parse.ast_types import Ability, Trigger, Effect, Amount, ObjectFilter
    a = Ability(
        kind="triggered",
        trigger=Trigger(event="dies", subject=ObjectFilter(card_type="creature")),
        effects=[Effect(verb="draw", amount=Amount(value=1))],
    )
    d = a.to_dict()
    assert d["kind"] == "triggered"
    assert d["trigger"]["event"] == "dies"
    # Should round-trip through JSON
    json_str = json.dumps(d)
    assert "dies" in json_str


def test_ability_from_dict():
    """Deserialization: dict → Ability for DB round-trip."""
    from mtg_synergy.parse.ast_types import Ability, Trigger, Effect, Amount, ObjectFilter
    a = Ability(
        kind="triggered",
        trigger=Trigger(event="dies", subject=ObjectFilter(card_type="creature")),
        effects=[Effect(verb="draw", amount=Amount(value=1))],
    )
    d = a.to_dict()
    restored = Ability.from_dict(d)
    assert restored.kind == "triggered"
    assert restored.trigger.event == "dies"
    assert restored.effects[0].verb == "draw"
