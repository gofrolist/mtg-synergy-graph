"""Tests for the portfolio-selection family map (plan 2026-07-02-004, Unit 1).

Covers the strict loader (``portfolio.load_family_map``), the shape
validation error paths, PINNED-SNAPSHOT coverage of the committed
artifact, the dead-key direction, the mandated R6 sibling merges, and
consistency with ``rules_seed.json``'s per-row ``family`` field.

Coverage is deliberately snapshot-based, NOT full-universe: plan
2026-07-02-004 was DECLINED at R0 and the scaffold write-path that
would have kept a full-universe check green as rules land (Unit 3) is
UNFUNDED. A full-universe assertion here would break the autonomous
walker's pytest pass on every new rule — a standing authoring
obligation the DECLINE explicitly does not carry. Re-enable the
full-universe direction only if the Unit 3 mechanism is revived.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

import mtg_synergy_graph.universal_scorer as universal_scorer
from mtg_synergy_graph.complement_rules.registry import (
    COMPLEMENT_RULES,
    DECLARATIVE_RULE_IDS,
    RULE_GATES,
)
from mtg_synergy_graph.portfolio import FAMILY_MAP, load_family_map

_RULES_SEED_PATH = Path(universal_scorer.__file__).resolve().parent / "data" / "rules_seed.json"


# ---------------------------------------------------------------------------
# Rule universe (mirrors tests/test_scoring_weights.py::_emitted_rule_ids,
# plus RULE_GATES, with a broader literal regex)
# ---------------------------------------------------------------------------


def _rule_universe() -> set[str]:
    """Universe of rule_ids that any complement helper might emit.

    Combines (a) rule_id string literals scraped from
    ``complement_rules/*.py`` source, (b) ``COMPLEMENT_RULES`` registry
    rule_ids, (c) ``RULE_GATES`` rule_ids, and (d) declarative rule ids
    (``DECLARATIVE_RULE_IDS`` from ``rules_seed.json``).

    The scrape regex here is deliberately BROADER than
    ``test_scoring_weights._emitted_rule_ids``'s: it also matches the
    assignment form ``rule_id = "..."`` (with spaces), which is how
    ``tribal_density`` / ``tribal_body`` are emitted in ``density.py``.
    Those two are mandated members of the ``tribal`` family merge, so
    the family map must carry them and the dead-key direction below
    must not flag them.
    """
    src_dir = Path(universal_scorer.__file__).parent / "complement_rules"
    literals: set[str] = set()
    pattern = re.compile(r"""rule_id\s*=\s*['"]([a-z_0-9]+)['"]""")
    for path in src_dir.rglob("*.py"):
        for match in pattern.finditer(path.read_text(encoding="utf-8")):
            literals.add(match.group(1))
    # Sanity floor: a regex breakage returning zero matches would
    # silently weaken both directions of the coverage check.
    assert len(literals) >= 30, (
        f"rule_id literal scrape returned only {len(literals)} matches — "
        "regex may be broken or complement_rules layout changed"
    )
    return (
        literals
        | {rule.rule_id for rule in COMPLEMENT_RULES}
        | {gate.rule_id for gate in RULE_GATES}
        | set(DECLARATIVE_RULE_IDS)
    )


# ---------------------------------------------------------------------------
# Loader happy path
# ---------------------------------------------------------------------------


def test_loader_returns_committed_map() -> None:
    loaded = load_family_map()
    assert isinstance(loaded, dict)
    assert loaded, "committed family_map.json must not be empty"
    assert loaded == FAMILY_MAP
    assert all(isinstance(k, str) and isinstance(v, str) for k, v in loaded.items())


# ---------------------------------------------------------------------------
# Loader error paths (explicit tmp_path artifacts; never the packaged seed)
# ---------------------------------------------------------------------------


def _write(path: Path, payload: dict[str, Any]) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_loader_missing_families_raises(tmp_path: Path) -> None:
    p = _write(tmp_path / "family_map.json", {"_readme": ["x"]})
    with pytest.raises(ValueError, match=r"missing required top-level keys.*families"):
        load_family_map(p)


def test_loader_missing_readme_raises(tmp_path: Path) -> None:
    p = _write(tmp_path / "family_map.json", {"families": {"lord": "tribal"}})
    with pytest.raises(ValueError, match=r"missing required top-level keys.*_readme"):
        load_family_map(p)


def test_loader_unknown_top_level_key_raises(tmp_path: Path) -> None:
    p = _write(
        tmp_path / "family_map.json",
        {"_readme": ["x"], "families": {"lord": "tribal"}, "weights": {}},
    )
    with pytest.raises(ValueError, match=r"unknown top-level keys.*weights"):
        load_family_map(p)


def test_loader_non_string_family_value_raises(tmp_path: Path) -> None:
    p = _write(
        tmp_path / "family_map.json",
        {"_readme": ["x"], "families": {"lord": 3}},
    )
    with pytest.raises(ValueError, match=r"families.lord.*non-empty string"):
        load_family_map(p)


def test_loader_empty_family_value_raises(tmp_path: Path) -> None:
    p = _write(
        tmp_path / "family_map.json",
        {"_readme": ["x"], "families": {"lord": ""}},
    )
    with pytest.raises(ValueError, match=r"families.lord.*non-empty string"):
        load_family_map(p)


def test_loader_non_list_readme_raises(tmp_path: Path) -> None:
    p = _write(
        tmp_path / "family_map.json",
        {"_readme": "prose", "families": {"lord": "tribal"}},
    )
    with pytest.raises(ValueError, match="'_readme' must be a list of strings"):
        load_family_map(p)


def test_loader_non_object_families_raises(tmp_path: Path) -> None:
    p = _write(
        tmp_path / "family_map.json",
        {"_readme": ["x"], "families": ["lord"]},
    )
    with pytest.raises(ValueError, match="'families' must be an object"):
        load_family_map(p)


def test_loader_non_object_top_level_raises(tmp_path: Path) -> None:
    p = tmp_path / "family_map.json"
    p.write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")
    with pytest.raises(ValueError, match="top level must be a JSON object"):
        load_family_map(p)


def test_loader_malformed_json_raises(tmp_path: Path) -> None:
    p = tmp_path / "family_map.json"
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="malformed JSON"):
        load_family_map(p)


def test_loader_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=r"family_map.json missing"):
        load_family_map(tmp_path / "does_not_exist.json")


# ---------------------------------------------------------------------------
# Coverage: pinned snapshot + dead-key direction
# ---------------------------------------------------------------------------

#: The 92 rule_ids in the emitted-rule universe at authoring time
#: (2026-07-02, plan 2026-07-02-004 Unit 1). Full-universe standing
#: enforcement was plan Unit 3, UNFUNDED on the DECLINE — pinning the
#: snapshot keeps the committed artifact honest without obligating the
#: autonomous walker to author a family entry for every new rule.
#: Re-enable a universe ⊆ map assertion only if the Unit 3
#: scaffold write-path is revived. If a rule is deliberately REMOVED
#: from the codebase, drop it here AND from family_map.json (see
#: test_no_dead_keys_in_family_map).
_PINNED_RULE_SNAPSHOT: frozenset[str] = frozenset(
    {
        "affinity_archetype",
        "anthem_payoff",
        "artifact_recursion",
        "attack_payoffs",
        "aura_equipment_support",
        "cardpower_axis_feeder",
        "cascade_tribal",
        "cascade_value",
        "changeling_tribal",
        "cheat_cmc",
        "choose_tribal",
        "combat_enhancer",
        "copy_synergy",
        "cost_feeds_trigger",
        "cost_payoff",
        "cost_reducer",
        "cost_reduction_target",
        "counter_axis_feeder",
        "counter_doubler",
        "counter_keyword",
        "counter_target_payoff",
        "creature_died_feeder",
        "creature_untap_engine",
        "creatures_as_lands_landfall",
        "damage_doubler_synergy",
        "dies_drain",
        "doctor_s_tribal",
        "edict_feeder",
        "effect_feeds_trigger",
        "effect_resonance",
        "etb_self",
        "etb_tapped_stax_feeder",
        "etbreplacement_copy_dbcopy_optional_tribal",
        "etbreplacement_other_choosect_tribal",
        "evasion",
        "exalted_density",
        "extra_land_plays",
        "firebending_2_tribal",
        "flicker_payoff",
        "flicker_synergy",
        "graveyard_play",
        "gy_fuel_feeder",
        "gy_loader",
        "hand_size_feeder",
        "land_bounce_feeder",
        "land_to_gy_synergy",
        "landfall_enabler",
        "landwalk_island_tribal",
        "life_total_feeder",
        "lifegain_feeder",
        "lord",
        "mana_doubler",
        "melee_tribal",
        "mentor_tribal",
        "modified_axis_feeder",
        "monarch_synergy",
        "more_tribal",
        "multicolor_untap",
        "opponent_forcing",
        "panharmonicon",
        "party_feeder",
        "populate_stack",
        "prepared_mechanic",
        "proliferate_synergy",
        "prowess_tribal",
        "repl_damagedone_counters_stack",
        "repl_moved_exile_stack",
        "replacement_blocks",
        "replacement_producer",
        "replacement_resonance",
        "sacrifice_cluster",
        "scaling",
        "self_bridging_cascade",
        "spell_density",
        "spellcast_resonance",
        "start_tribal",
        "subject_zone_feeder",
        "tap_type_feeder",
        "token_etb_damage",
        "token_producer",
        "toughness_synergy",
        "training_tribal",
        "tribal_body",
        "tribal_density",
        "trigger_effect",
        "trigger_resonance",
        "untap_combo",
        "untap_synergy",
        "value_engine",
        "voltron",
        "wheel_synergy",
        "zone_resonance",
    }
)


def test_every_registered_rule_has_a_family_entry() -> None:
    """Pinned-snapshot coverage check of the committed artifact.

    Asserts every AUTHORING-TIME rule_id keeps its family entry —
    i.e. nobody deletes map entries out from under the selection
    helpers. Deliberately does NOT assert the full current universe is
    mapped: a newly registered rule_id must NOT fail this test (no
    authoring obligation under the 2026-07-02 DECLINE; the standing
    full-universe enforcement was plan Unit 3, unfunded — see module
    docstring).
    """
    missing = _PINNED_RULE_SNAPSHOT - set(FAMILY_MAP)
    assert not missing, (
        f"authoring-time rule_ids missing from family_map.json: {sorted(missing)}. "
        "If the rule was deliberately removed from the codebase, drop it from "
        "_PINNED_RULE_SNAPSHOT in this test alongside the map entry; otherwise "
        "restore the entry."
    )
    # Sanity: the snapshot itself was derived from the authoring-time
    # universe; a subset check against today's universe is intentionally
    # absent (new rules are allowed to be unmapped).


def test_no_dead_keys_in_family_map() -> None:
    """Every ``families`` key must be a rule_id some helper actually
    emits — a dead key indicates a typo or a rule renamed/deleted
    without updating the JSON (scoring_weights dead-key precedent).
    """
    universe = _rule_universe()
    dead = set(FAMILY_MAP) - universe
    assert not dead, (
        f"family_map.json contains entries for unregistered rule_ids: {sorted(dead)}. "
        "If these rules were deliberately REMOVED from the codebase, delete their "
        "family_map.json entries and drop them from _PINNED_RULE_SNAPSHOT in this "
        "test (the map only carries live or pinned rule_ids); if they were renamed, "
        "rename the map keys to match."
    )


# ---------------------------------------------------------------------------
# Mandated R6 sibling merges
# ---------------------------------------------------------------------------


def test_lord_and_tribal_density_share_a_family() -> None:
    assert FAMILY_MAP["lord"] == FAMILY_MAP["tribal_density"] == "tribal"


def test_all_tribal_rules_share_the_tribal_family() -> None:
    tribal_ids = {rid for rid in FAMILY_MAP if rid.endswith("_tribal")}
    assert tribal_ids, "expected *_tribal declarative rules in the map"
    off_family = {rid for rid in tribal_ids if FAMILY_MAP[rid] != "tribal"}
    assert not off_family, f"*_tribal rules not mapped to 'tribal': {sorted(off_family)}"
    # tribal_body is the payoff-direction sibling of tribal_density
    # (same emission site in density.py) — documented merge #2.
    assert FAMILY_MAP["tribal_body"] == "tribal"


def test_spell_density_and_spellcast_resonance_share_a_family() -> None:
    assert FAMILY_MAP["spell_density"] == FAMILY_MAP["spellcast_resonance"] == "spell"


# ---------------------------------------------------------------------------
# Consistency with rules_seed.json per-row `family` field
# ---------------------------------------------------------------------------

#: rules_seed.json rows whose `family` field deliberately differs from
#: the portfolio map. rule_id -> (rules_seed family, family_map family).
#: rules_seed.json is NOT edited (editing its functional block flips the
#: config hash for nothing — plan Key Technical Decision "Family-map
#: authority"); each divergence is intentional:
_SEED_FAMILY_ALLOWLIST: dict[str, tuple[str, str]] = {
    # Spelling convention only: forensics_report.rule_family() spells the
    # replacement family with an underscore; rules_seed predates it with
    # a hyphen. Same grouping, different string.
    "repl_damagedone_counters_stack": ("replacement-stack", "replacement_stack"),
    "repl_moved_exile_stack": ("replacement-stack", "replacement_stack"),
    # rules_seed groups monarch under a curated 'political' reporting
    # family; monarch_synergy is that family's only member in the rule
    # universe, so the portfolio map keeps the identity convention
    # (renaming a singleton adds no grouping information).
    "monarch_synergy": ("political", "monarch_synergy"),
    # rules_seed groups toughness under 'scaling' for interpreter
    # reporting; the portfolio map applies ONLY the mandated R6 merges
    # and keeps identity here (conservative — no invented groupings).
    "toughness_synergy": ("scaling", "toughness_synergy"),
    # *_feeder rules follow the forensics axis_feeder convention in the
    # portfolio map; rules_seed assigned bespoke reporting families.
    "party_feeder": ("scaling", "axis_feeder"),
    "etb_tapped_stax_feeder": ("stax", "axis_feeder"),
}


def test_consistent_with_rules_seed_family_field() -> None:
    with _RULES_SEED_PATH.open(encoding="utf-8") as f:
        seed = json.load(f)
    checked = 0
    for row in seed["rules"]:
        rule_id = row["rule_id"]
        seed_family = row.get("family")
        if seed_family is None:
            continue
        checked += 1
        map_family = FAMILY_MAP.get(rule_id)
        assert map_family is not None, f"declarative rule {rule_id} missing from family_map.json"
        if rule_id in _SEED_FAMILY_ALLOWLIST:
            expected_pair = _SEED_FAMILY_ALLOWLIST[rule_id]
            assert (seed_family, map_family) == expected_pair, (
                f"{rule_id}: allowlist expects {expected_pair}, got "
                f"({seed_family!r}, {map_family!r}) — update the allowlist "
                "rationale or the map, not silently"
            )
        else:
            assert map_family == seed_family, (
                f"{rule_id}: family_map.json says {map_family!r} but "
                f"rules_seed.json says {seed_family!r}; either align them or "
                "add a documented _SEED_FAMILY_ALLOWLIST entry"
            )
    assert checked > 0, "rules_seed.json unexpectedly has no per-row family fields"


def test_allowlist_has_no_stale_entries() -> None:
    """Every allowlist rule_id must still exist in rules_seed.json with a
    family field — otherwise the entry is rot and must be removed."""
    with _RULES_SEED_PATH.open(encoding="utf-8") as f:
        seed = json.load(f)
    seed_families = {row["rule_id"]: row.get("family") for row in seed["rules"]}
    stale = [rid for rid in _SEED_FAMILY_ALLOWLIST if seed_families.get(rid) is None]
    assert not stale, f"_SEED_FAMILY_ALLOWLIST entries no longer in rules_seed.json: {stale}"
