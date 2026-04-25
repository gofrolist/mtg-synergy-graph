"""One-shot parity test for the scoring-weights externalization PR.

Asserts that the dicts loaded from data/scoring_weights.json match —
key-for-key and bit-identical via repr() — the values that lived as
inline Python literals in universal_scorer.py before the migration.

Deleted in the follow-up cleanup commit alongside
scripts/migrate_scoring_weights.py. The longer-running checks
(strict-shape validation, dead-key detection, hash invariants) live
in tests/test_scoring_weights.py.
"""

from __future__ import annotations

from mtg_synergy_graph.universal_scorer import (
    _FLAT_WEIGHT_OVERRIDES,
    _RULE_QUALITY_MULTIPLIER,
)

# Snapshot of the pre-migration literal values, copied from
# universal_scorer.py at HEAD~1. If any value diverges, the migration
# changed scoring (which is out of scope for this PR per the spec).

# Snapshot extracted from `git show HEAD:src/mtg_synergy_graph/universal_scorer.py`
# at the migration commit's parent (the last revision with the inline literals).
# Order matches the original literal so a reviewer can diff the snapshot
# against the parent file directly.

_EXPECTED_RULE_QUALITY_MULTIPLIER: dict[str, float] = {
    "trigger_resonance": 0.7,
    "edict_feeder": 2.0,
    "panharmonicon": 2.0,
    "cost_reducer": 1.2,
    "value_engine": 0.5,
    "cost_reduction_target": 1.0,
    "token_etb_damage": 0.3,
    "combat_enhancer": 0.7,
    "zone_resonance": 1.3,
    "counter_keyword": 0.7,
    "counter_doubler": 1.5,
    "dies_drain": 1.5,
    "gy_loader": 1.5,
    "untap_combo": 3.0,
    "attack_payoffs": 1.5,
    "aura_equipment_support": 2.5,
    "wheel_synergy": 2.0,
    "monarch_synergy": 2.5,
    "counter_target_payoff": 2.0,
    "creature_untap_engine": 3.0,
    "populate_stack": 2.5,
    "landfall_enabler": 2.0,
    "subject_zone_feeder": 2.5,
    "counter_axis_feeder": 3.0,
    "modified_axis_feeder": 3.0,
    "cardpower_axis_feeder": 3.5,
    "tap_type_feeder": 1.0,
    "hand_size_feeder": 2.5,
    "gy_fuel_feeder": 1.2,
    "lifegain_feeder": 2.5,
    "life_total_feeder": 2.5,
    "land_bounce_feeder": 2.5,
    "etb_tapped_stax_feeder": 2.5,
    "party_feeder": 2.5,
    "creature_died_feeder": 2.5,
    "creatures_as_lands_landfall": 2.5,
    "damage_doubler_synergy": 2.5,
    "choose_tribal": 2.0,
    "doctor_s_tribal": 2.0,
    "more_tribal": 2.0,
    "prowess_tribal": 2.0,
    "etbreplacement_copy_dbcopy_optional_tribal": 2.0,
    "firebending_2_tribal": 2.0,
    "start_tribal": 2.0,
    "etbreplacement_other_choosect_tribal": 2.0,
    "mentor_tribal": 2.0,
    "repl_moved_exile_stack": 2.0,
    "changeling_tribal": 2.0,
    "landwalk_island_tribal": 2.0,
    "melee_tribal": 2.0,
    "training_tribal": 2.0,
    "repl_damagedone_counters_stack": 2.0,
    "cascade_tribal": 2.0,
}

_EXPECTED_FLAT_WEIGHT_OVERRIDES: dict[str, float] = {
    "spell_density": 0.3,
    "scaling": 0.3,
    "tribal_density": 0.5,
    "token_producer": 0.18,
    "evasion": 0.10,
    "etb_self": 0.01,
}


def test_rule_quality_multiplier_keys_match() -> None:
    assert set(_RULE_QUALITY_MULTIPLIER) == set(_EXPECTED_RULE_QUALITY_MULTIPLIER), (
        "key set drift in rule_quality_multiplier"
    )


def test_rule_quality_multiplier_values_repr_match() -> None:
    for key, expected in _EXPECTED_RULE_QUALITY_MULTIPLIER.items():
        actual = _RULE_QUALITY_MULTIPLIER[key]
        assert repr(actual) == repr(expected), (
            f"{key}: float repr drift {actual!r} vs {expected!r} (would change compute_config_hash)"
        )


def test_flat_weight_overrides_keys_match() -> None:
    assert set(_FLAT_WEIGHT_OVERRIDES) == set(_EXPECTED_FLAT_WEIGHT_OVERRIDES), "key set drift in flat_weight_overrides"


def test_flat_weight_overrides_values_repr_match() -> None:
    for key, expected in _EXPECTED_FLAT_WEIGHT_OVERRIDES.items():
        actual = _FLAT_WEIGHT_OVERRIDES[key]
        assert repr(actual) == repr(expected), (
            f"{key}: float repr drift {actual!r} vs {expected!r} (would change compute_config_hash)"
        )
