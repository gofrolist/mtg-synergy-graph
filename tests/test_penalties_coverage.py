"""Additional tests for penalties.py — targeting CI-missed lines.

Covers bulk loaders, _decode_json_field edge cases, _token_subtype fallback,
apply_penalties slow path, apply_signal_penalties, and _scope_subtypes.
"""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest

from mtg_synergy_graph.db import _schema_sql
from mtg_synergy_graph.penalties import (
    COUNTERS_ON_LANDS_MULT,
    EXCLUDED_SUBTYPES_MULT,
    HARD_FILTER_SCORE,
    NON_COUNTER_CREATURE_MULT,
    OPPONENT_MILL_MULT,
    SUBTYPE_MISMATCH_MULT,
    WRONG_COUNTER_TYPE_MULT,
    WRONG_TOKEN_TYPE_MULT,
    PenaltyContext,
    _decode_json_field,
    _scope_subtypes,
    _token_subtype,
    apply_penalties,
    apply_penalties_ctx,
    apply_signal_penalties,
    build_candidate_cache,
    build_penalty_context,
)

# ---------------------------------------------------------------------------
# In-memory DB helpers
# ---------------------------------------------------------------------------


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(_schema_sql())
    conn.commit()
    return conn


def _insert_card(conn: sqlite3.Connection, **kw: Any) -> None:
    defaults: dict[str, Any] = {
        "name": "Test Card",
        "oracle_id": None,
        "mana_cost": None,
        "cmc": 0,
        "types": "",
        "supertypes": "",
        "subtypes": "",
        "card_types": "",
        "colors": "",
        "color_identity": "",
        "power": None,
        "toughness": None,
        "loyalty": None,
        "keywords": "[]",
        "oracle_text": "",
        "is_commander": False,
        "deck_hints": None,
        "deck_needs": None,
        "deck_has": None,
        "edhrec_rank": None,
        "rarity": None,
        "set_code": None,
    }
    defaults.update(kw)
    cols = ", ".join(defaults.keys())
    placeholders = ", ".join("?" * len(defaults))
    conn.execute(f"INSERT INTO cards ({cols}) VALUES ({placeholders})", tuple(defaults.values()))  # noqa: S608
    conn.commit()


def _insert_port(conn: sqlite3.Connection, **kw: Any) -> None:
    defaults: dict[str, Any] = {
        "card_name": "Test Card",
        "port_type": "effect",
        "event_class": "",
        "valid_filter": None,
        "zone_origin": None,
        "zone_destination": None,
        "phase": None,
        "affected_scope": None,
        "effect_zone": None,
        "cost_subtype": None,
        "cost_target": None,
        "trigger_source": None,
        "mana_restriction": None,
        "amount": None,
        "counter_type": None,
        "granted_keyword": None,
        "granted_ability": None,
        "execute_ref": None,
        "sub_ability_ref": None,
        "is_conditional": False,
        "branch_kind": "root",
        "branch_parent": None,
        "source_svar": None,
        "chain_depth": 0,
        "scaling_expression": None,
        "is_optional": False,
        "is_combat": False,
        "is_curse": False,
        "replacement_event": None,
        "replacement_result": None,
        "replacement_player": None,
        "duration": None,
        "raw_line": None,
    }
    defaults.update(kw)
    cols = ", ".join(defaults.keys())
    placeholders = ", ".join("?" * len(defaults))
    conn.execute(f"INSERT INTO card_ports ({cols}) VALUES ({placeholders})", tuple(defaults.values()))  # noqa: S608
    conn.commit()


# ---------------------------------------------------------------------------
# _decode_json_field edge cases (lines 114, 117-118, 120)
# ---------------------------------------------------------------------------


class TestDecodeJsonField:
    def test_dict_input_passes_through(self):
        result = _decode_json_field({"Type": ["Goblin"]})
        assert result == {"Type": ["Goblin"]}

    def test_invalid_json_string_returns_empty(self):
        assert _decode_json_field("not json at all") == {}

    def test_non_dict_json_returns_empty(self):
        # JSON that decodes to a list, not a dict
        assert _decode_json_field("[1,2,3]") == {}

    def test_filters_non_list_values(self):
        result = _decode_json_field('{"Type": ["Goblin"], "bad": "string"}')
        assert result == {"Type": ["Goblin"]}


# ---------------------------------------------------------------------------
# _token_subtype fallback (line 194)
# ---------------------------------------------------------------------------


class TestTokenSubtypeFallback:
    def test_three_part_token_falls_back_to_last(self):
        # 3 parts, parts[1] is digit → creature layout but len(parts) < 4
        # Falls through to parts[-1].capitalize()
        result = _token_subtype("w_2_2")
        assert result == "2"

    def test_artifact_creature_layout(self):
        result = _token_subtype("u_1_1_a_thopter_flying")
        assert result == "Thopter"


# ---------------------------------------------------------------------------
# Bulk loaders with in-memory DB (lines 320, 337-339, 369, 382)
# ---------------------------------------------------------------------------


class TestBulkLoadStaticScopes:
    def test_loads_creature_static_scopes(self):
        conn = _make_conn()
        _insert_card(conn, name="Anthem Card")
        _insert_port(
            conn,
            card_name="Anthem Card",
            port_type="static",
            event_class="Continuous",
            affected_scope="Creature.Elf.YouCtrl",
        )
        cache = build_candidate_cache(conn)
        assert "Anthem Card" in cache.creature_static_scopes
        assert "Creature.Elf.YouCtrl" in cache.creature_static_scopes["Anthem Card"]
        conn.close()


class TestBulkLoadExcludes:
    def test_loads_non_exclusions_from_static(self):
        conn = _make_conn()
        _insert_card(conn, name="Angel of Jubilation")
        _insert_port(
            conn,
            card_name="Angel of Jubilation",
            port_type="static",
            event_class="Continuous",
            affected_scope="Creature.nonBlack.YouCtrl",
        )
        cache = build_candidate_cache(conn)
        assert "Angel of Jubilation" in cache.candidate_excludes
        assert "Black" in cache.candidate_excludes["Angel of Jubilation"]
        conn.close()

    def test_ignores_non_static_exclusions(self):
        conn = _make_conn()
        _insert_card(conn, name="Mystic Remora")
        _insert_port(
            conn,
            card_name="Mystic Remora",
            port_type="trigger",
            event_class="SpellCast",
            affected_scope="nonCreature",
        )
        cache = build_candidate_cache(conn)
        assert "Mystic Remora" not in cache.candidate_excludes
        conn.close()


class TestBulkLoadCountersOnLands:
    def test_detects_land_counter_cards(self):
        conn = _make_conn()
        _insert_card(conn, name="Land Counter Card")
        _insert_port(
            conn,
            card_name="Land Counter Card",
            port_type="effect",
            event_class="PutCounter",
            valid_filter="Land.YouCtrl",
            counter_type="P1P1",
        )
        cache = build_candidate_cache(conn)
        assert cache.candidate_counters_on_land.get("Land Counter Card") is True
        conn.close()


class TestBulkLoadOppMill:
    def test_detects_opponent_mill(self):
        conn = _make_conn()
        _insert_card(conn, name="Opp Mill Card")
        _insert_port(
            conn,
            card_name="Opp Mill Card",
            port_type="replacement",
            event_class="Mill",
            replacement_event="Mill",
            replacement_player="Opponent",
        )
        cache = build_candidate_cache(conn)
        assert cache.candidate_opp_mill.get("Opp Mill Card") is True
        conn.close()


# ---------------------------------------------------------------------------
# _scope_subtypes (line 588)
# ---------------------------------------------------------------------------


class TestScopeSubtypes:
    def test_extracts_subtypes_ignoring_modifiers(self):
        result = _scope_subtypes("Creature.Elf.YouCtrl")
        assert result == {"Elf"}

    def test_ignores_non_prefix(self):
        result = _scope_subtypes("Creature.nonHuman.Elf")
        assert result == {"Elf"}

    def test_empty_scope(self):
        assert _scope_subtypes("") == set()
        assert _scope_subtypes(None) == set()


# ---------------------------------------------------------------------------
# apply_penalties_ctx — candidate not found (line 603)
# ---------------------------------------------------------------------------


class TestApplyPenaltiesCandidateNotFound:
    def test_returns_original_score_for_unknown(self):
        ctx = _make_minimal_ctx()
        assert apply_penalties_ctx(ctx, "Nonexistent Card", 42.0) == 42.0


# ---------------------------------------------------------------------------
# apply_penalties_ctx — Rule 2 Background / Doctor (lines 614, 616)
# ---------------------------------------------------------------------------


class TestRule2BackgroundDoctor:
    def test_background_hard_filtered_without_permission(self):
        ctx = _make_minimal_ctx(
            cmdr_allows_background=False,
            candidate_rows={
                "Some Background": {
                    "name": "Some Background",
                    "color_identity": "G",
                    "subtypes": "Background",
                    "oracle_text": "",
                    "card_types": "Enchantment",
                    "cmc": 2,
                    "deck_needs": None,
                    "deck_hints": None,
                    "deck_has": None,
                },
            },
        )
        assert apply_penalties_ctx(ctx, "Some Background", 100.0) == HARD_FILTER_SCORE

    def test_doctors_companion_hard_filtered(self):
        ctx = _make_minimal_ctx(
            cmdr_has_doctor=False,
            candidate_rows={
                "Doctor's Friend": {
                    "name": "Doctor's Friend",
                    "color_identity": "G",
                    "subtypes": "Human",
                    "oracle_text": "Doctor's Companion",
                    "card_types": "Creature",
                    "cmc": 3,
                    "deck_needs": None,
                    "deck_hints": None,
                    "deck_has": None,
                },
            },
        )
        assert apply_penalties_ctx(ctx, "Doctor's Friend", 100.0) == HARD_FILTER_SCORE


# ---------------------------------------------------------------------------
# apply_penalties_ctx — Rule 3 subtype mismatch (lines 621-625)
# ---------------------------------------------------------------------------


class TestRule3SubtypeMismatch:
    def test_static_scope_mismatch_penalises(self):
        ctx = _make_minimal_ctx(
            cmdr_subtypes=frozenset({"Goblin"}),
            creature_static_scopes={"Elf Lord": ["Creature.Elf.YouCtrl"]},
            candidate_rows={
                "Elf Lord": {
                    "name": "Elf Lord",
                    "color_identity": "G",
                    "subtypes": "Elf",
                    "oracle_text": "",
                    "card_types": "Creature",
                    "cmc": 3,
                    "deck_needs": None,
                    "deck_hints": None,
                    "deck_has": None,
                },
            },
        )
        assert apply_penalties_ctx(ctx, "Elf Lord", 100.0) == pytest.approx(100.0 * SUBTYPE_MISMATCH_MULT)


# ---------------------------------------------------------------------------
# apply_penalties_ctx — Rule 4 excluded subtypes (lines 632-634)
# ---------------------------------------------------------------------------


class TestRule4ExcludedSubtypes:
    def test_excludes_commander_type(self):
        ctx = _make_minimal_ctx(
            cmdr_subtypes=frozenset({"Zombie"}),
            cmdr_card_types=frozenset({"Creature"}),
            candidate_excludes={"Anti-Zombie": {"Zombie"}},
            candidate_rows={
                "Anti-Zombie": {
                    "name": "Anti-Zombie",
                    "color_identity": "G",
                    "subtypes": "",
                    "oracle_text": "",
                    "card_types": "Enchantment",
                    "cmc": 3,
                    "deck_needs": None,
                    "deck_hints": None,
                    "deck_has": None,
                },
            },
        )
        assert apply_penalties_ctx(ctx, "Anti-Zombie", 100.0) == pytest.approx(100.0 * EXCLUDED_SUBTYPES_MULT)


# ---------------------------------------------------------------------------
# apply_penalties_ctx — Rule 5 wrong token type (line 639)
# ---------------------------------------------------------------------------


class TestRule5WrongTokenType:
    def test_wrong_token_penalises_tribal(self):
        ctx = _make_minimal_ctx(
            cmdr_is_tribal=True,
            cmdr_token_subtypes=frozenset({"Goblin"}),
            candidate_token_types={"Token Maker": {"Saproling"}},
            candidate_rows={
                "Token Maker": {
                    "name": "Token Maker",
                    "color_identity": "G",
                    "subtypes": "",
                    "oracle_text": "",
                    "card_types": "Enchantment",
                    "cmc": 3,
                    "deck_needs": None,
                    "deck_hints": None,
                    "deck_has": None,
                },
            },
        )
        assert apply_penalties_ctx(ctx, "Token Maker", 100.0) == pytest.approx(100.0 * WRONG_TOKEN_TYPE_MULT)


# ---------------------------------------------------------------------------
# apply_penalties_ctx — Rule 6 wrong counter type (line 651)
# ---------------------------------------------------------------------------


class TestRule6WrongCounterType:
    def test_wrong_counter_type_penalises(self):
        ctx = _make_minimal_ctx(
            cmdr_uses_p1p1=True,
            candidate_counter_types={"Shield Card": {"SHIELD"}},
            candidate_rows={
                "Shield Card": {
                    "name": "Shield Card",
                    "color_identity": "G",
                    "subtypes": "",
                    "oracle_text": "",
                    "card_types": "Artifact",
                    "cmc": 3,
                    "deck_needs": None,
                    "deck_hints": None,
                    "deck_has": None,
                },
            },
        )
        assert apply_penalties_ctx(ctx, "Shield Card", 100.0) == pytest.approx(100.0 * WRONG_COUNTER_TYPE_MULT)


# ---------------------------------------------------------------------------
# apply_penalties_ctx — Rule 8 counters on lands (line 659)
# ---------------------------------------------------------------------------


class TestRule8CountersOnLands:
    def test_land_counter_penalises(self):
        ctx = _make_minimal_ctx(
            cmdr_uses_p1p1=True,
            candidate_counters_on_land={"Land Ctr": True},
            candidate_rows={
                "Land Ctr": {
                    "name": "Land Ctr",
                    "color_identity": "G",
                    "subtypes": "",
                    "oracle_text": "",
                    "card_types": "Enchantment",
                    "cmc": 2,
                    "deck_needs": None,
                    "deck_hints": None,
                    "deck_has": None,
                },
            },
        )
        assert apply_penalties_ctx(ctx, "Land Ctr", 100.0) == pytest.approx(100.0 * COUNTERS_ON_LANDS_MULT)


# ---------------------------------------------------------------------------
# apply_penalties_ctx — Rule 10 opponent mill (line 680)
# ---------------------------------------------------------------------------


class TestRule10OppMill:
    def test_opp_mill_penalises_self_mill_commander(self):
        ctx = _make_minimal_ctx(
            cmdr_self_mill=True,
            candidate_opp_mill={"Opp Mill": True},
            candidate_rows={
                "Opp Mill": {
                    "name": "Opp Mill",
                    "color_identity": "G",
                    "subtypes": "",
                    "oracle_text": "",
                    "card_types": "Enchantment",
                    "cmc": 2,
                    "deck_needs": None,
                    "deck_hints": None,
                    "deck_has": None,
                },
            },
        )
        assert apply_penalties_ctx(ctx, "Opp Mill", 100.0) == pytest.approx(100.0 * OPPONENT_MILL_MULT)


# ---------------------------------------------------------------------------
# apply_penalties slow path (lines 714-715)
# ---------------------------------------------------------------------------


class TestApplyPenaltiesSlowPath:
    def test_slow_path_delegates_to_ctx(self):
        conn = _make_conn()
        _insert_card(conn, name="Cmdr", color_identity="G", subtypes="Elf", card_types="Creature")
        _insert_card(conn, name="Candidate", color_identity="W", subtypes="Human", card_types="Creature")
        score = apply_penalties(conn, ["Cmdr"], "Candidate", 100.0)
        # W not in G → hard filter
        assert score == HARD_FILTER_SCORE
        conn.close()

    def test_slow_path_passes_legal_card(self):
        conn = _make_conn()
        _insert_card(conn, name="Cmdr", color_identity="G", subtypes="Elf", card_types="Creature")
        _insert_card(conn, name="GreenCard", color_identity="G", subtypes="Elf", card_types="Creature")
        score = apply_penalties(conn, ["Cmdr"], "GreenCard", 50.0)
        assert score > 0
        conn.close()


# ---------------------------------------------------------------------------
# apply_signal_penalties (lines 749-861)
# ---------------------------------------------------------------------------


class TestApplySignalPenalties:
    def test_hard_filter_returns_empty_signals(self):
        from mtg_synergy_graph.signals import CandidateSignals, Signal

        ctx = _make_minimal_ctx(
            cmdr_identity=frozenset({"G"}),
            candidate_rows={
                "White Card": {
                    "name": "White Card",
                    "color_identity": "W",
                    "subtypes": "",
                    "oracle_text": "",
                    "card_types": "Enchantment",
                    "cmc": 2,
                    "deck_needs": None,
                    "deck_hints": None,
                    "deck_has": None,
                },
            },
        )
        sigs = CandidateSignals(signals=[Signal(signal_type="port_match", confidence=0.8, tier=1, evidence={})])
        result = apply_signal_penalties(ctx, "White Card", sigs)
        assert result.signals == []

    def test_background_hard_filter_in_signals(self):
        from mtg_synergy_graph.signals import CandidateSignals, Signal

        ctx = _make_minimal_ctx(
            cmdr_allows_background=False,
            candidate_rows={
                "BG Card": {
                    "name": "BG Card",
                    "color_identity": "G",
                    "subtypes": "Background",
                    "oracle_text": "",
                    "card_types": "Enchantment",
                    "cmc": 2,
                    "deck_needs": None,
                    "deck_hints": None,
                    "deck_has": None,
                },
            },
        )
        sigs = CandidateSignals(signals=[Signal(signal_type="port_match", confidence=0.8, tier=1, evidence={})])
        result = apply_signal_penalties(ctx, "BG Card", sigs)
        assert result.signals == []

    def test_doctor_companion_hard_filter_in_signals(self):
        from mtg_synergy_graph.signals import CandidateSignals, Signal

        ctx = _make_minimal_ctx(
            cmdr_has_doctor=False,
            candidate_rows={
                "Doc Friend": {
                    "name": "Doc Friend",
                    "color_identity": "G",
                    "subtypes": "Human",
                    "oracle_text": "Doctor's Companion",
                    "card_types": "Creature",
                    "cmc": 3,
                    "deck_needs": None,
                    "deck_hints": None,
                    "deck_has": None,
                },
            },
        )
        sigs = CandidateSignals(signals=[Signal(signal_type="port_match", confidence=0.8, tier=1, evidence={})])
        result = apply_signal_penalties(ctx, "Doc Friend", sigs)
        assert result.signals == []

    def test_unknown_candidate_returns_signals_unchanged(self):
        from mtg_synergy_graph.signals import CandidateSignals, Signal

        ctx = _make_minimal_ctx()
        sigs = CandidateSignals(signals=[Signal(signal_type="port_match", confidence=0.8, tier=1, evidence={})])
        result = apply_signal_penalties(ctx, "Nonexistent", sigs)
        assert result.signals == sigs.signals

    def test_no_penalties_returns_original(self):
        from mtg_synergy_graph.signals import CandidateSignals, Signal

        ctx = _make_minimal_ctx(
            candidate_rows={
                "Clean Card": {
                    "name": "Clean Card",
                    "color_identity": "G",
                    "subtypes": "",
                    "oracle_text": "",
                    "card_types": "Enchantment",
                    "cmc": 2,
                    "deck_needs": None,
                    "deck_hints": None,
                    "deck_has": None,
                },
            },
        )
        sigs = CandidateSignals(signals=[Signal(signal_type="port_match", confidence=0.8, tier=1, evidence={})])
        result = apply_signal_penalties(ctx, "Clean Card", sigs)
        assert result is sigs  # identity — no copy needed

    def test_targeted_penalty_reduces_matching_signals_only(self):
        from mtg_synergy_graph.signals import CandidateSignals, Signal

        ctx = _make_minimal_ctx(
            cmdr_uses_p1p1=True,
            candidate_counter_types={"Counter Card": {"SHIELD"}},
            candidate_rows={
                "Counter Card": {
                    "name": "Counter Card",
                    "color_identity": "G",
                    "subtypes": "",
                    "oracle_text": "",
                    "card_types": "Artifact",
                    "cmc": 3,
                    "deck_needs": None,
                    "deck_hints": None,
                    "deck_has": None,
                },
            },
        )
        sigs = CandidateSignals(
            signals=[
                Signal(signal_type="counter_synergy", confidence=1.0, tier=1, evidence={}),
                Signal(signal_type="port_match", confidence=1.0, tier=1, evidence={}),
            ]
        )
        result = apply_signal_penalties(ctx, "Counter Card", sigs)
        # counter_synergy should be reduced by WRONG_COUNTER_TYPE_MULT
        counter_sig = next(s for s in result.signals if s.signal_type == "counter_synergy")
        port_sig = next(s for s in result.signals if s.signal_type == "port_match")
        assert counter_sig.confidence == pytest.approx(WRONG_COUNTER_TYPE_MULT)
        assert port_sig.confidence == pytest.approx(1.0)  # untouched

    def test_untargeted_rule9_reduces_all_signals(self):
        from mtg_synergy_graph.signals import CandidateSignals, Signal

        ctx = _make_minimal_ctx(
            cmdr_uses_p1p1=True,
            cmdr_is_tribal=False,
            candidate_rows={
                "Plain Bear": {
                    "name": "Plain Bear",
                    "color_identity": "G",
                    "subtypes": "Bear",
                    "oracle_text": "",
                    "card_types": "Creature",
                    "cmc": 2,
                    "deck_needs": None,
                    "deck_hints": None,
                    "deck_has": None,
                },
            },
        )
        sigs = CandidateSignals(
            signals=[
                Signal(signal_type="port_match", confidence=1.0, tier=1, evidence={}),
                Signal(signal_type="counter_synergy", confidence=1.0, tier=1, evidence={}),
            ]
        )
        result = apply_signal_penalties(ctx, "Plain Bear", sigs)
        for s in result.signals:
            assert s.confidence == pytest.approx(NON_COUNTER_CREATURE_MULT)


# ---------------------------------------------------------------------------
# build_penalty_context with commander port rows (lines 438-439, 474-487, 518-525)
# ---------------------------------------------------------------------------


class TestBuildPenaltyContextPorts:
    def test_commander_keywords_parsed(self):
        conn = _make_conn()
        _insert_card(
            conn,
            name="Keyword Cmdr",
            color_identity="G",
            subtypes="Elf",
            card_types="Creature",
            keywords='["Flying", "Trample"]',
        )
        ctx = build_penalty_context(conn, ["Keyword Cmdr"])
        assert "Flying" in ctx.cmdr_has_keywords
        assert "Trample" in ctx.cmdr_has_keywords
        conn.close()

    def test_bad_keywords_json_ignored(self):
        conn = _make_conn()
        _insert_card(
            conn,
            name="Bad KW Cmdr",
            color_identity="G",
            subtypes="Elf",
            card_types="Creature",
            keywords="not-json",
        )
        ctx = build_penalty_context(conn, ["Bad KW Cmdr"])
        assert ctx.cmdr_has_keywords == frozenset()
        conn.close()

    def test_scales_with_injects_types(self):
        conn = _make_conn()
        _insert_card(
            conn,
            name="Uril",
            color_identity="G,R,W",
            subtypes="Beast",
            card_types="Creature",
        )
        _insert_port(
            conn,
            card_name="Uril",
            port_type="scales_with",
            event_class="CardCounters.P1P1",
            raw_line="Aura stuff",
            valid_filter="Aura",
        )
        ctx = build_penalty_context(conn, ["Uril"])
        assert "Aura" in ctx.cmdr_has_types
        # Aura → parent Enchantment also injected
        assert "Enchantment" in ctx.cmdr_has_types
        assert ctx.cmdr_p1p1_is_primary is True
        conn.close()

    def test_scales_with_all_and_p1p1_effect(self):
        conn = _make_conn()
        _insert_card(
            conn,
            name="All Scales Cmdr",
            color_identity="G",
            subtypes="Ooze",
            card_types="Creature",
        )
        _insert_port(
            conn,
            card_name="All Scales Cmdr",
            port_type="scales_with",
            event_class="CardCounters.ALL",
        )
        _insert_port(
            conn,
            card_name="All Scales Cmdr",
            port_type="effect",
            event_class="PutCounter",
            counter_type="P1P1",
        )
        ctx = build_penalty_context(conn, ["All Scales Cmdr"])
        assert ctx.cmdr_p1p1_is_primary is True
        conn.close()

    def test_token_port_sets_token_subtypes(self):
        conn = _make_conn()
        _insert_card(
            conn,
            name="Token Cmdr",
            color_identity="R",
            subtypes="Goblin",
            card_types="Creature",
        )
        _insert_port(
            conn,
            card_name="Token Cmdr",
            port_type="effect",
            event_class="Token",
            raw_line="{'TokenScript': 'r_1_1_goblin'}",
        )
        ctx = build_penalty_context(conn, ["Token Cmdr"])
        assert "Goblin" in ctx.cmdr_token_subtypes
        assert "Token" in ctx.cmdr_has_abilities
        assert ctx.cmdr_is_tribal is True  # Goblin subtype matches Goblin token
        conn.close()

    def test_mill_self_detected(self):
        conn = _make_conn()
        _insert_card(
            conn,
            name="Mill Cmdr",
            color_identity="U,B",
            subtypes="Wizard",
            card_types="Creature",
        )
        _insert_port(
            conn,
            card_name="Mill Cmdr",
            port_type="effect",
            event_class="Mill",
            valid_filter="You",
        )
        ctx = build_penalty_context(conn, ["Mill Cmdr"])
        assert ctx.cmdr_self_mill is True
        conn.close()

    def test_candidate_cache_reused(self):
        conn = _make_conn()
        _insert_card(conn, name="Cmdr A", color_identity="G", subtypes="Elf", card_types="Creature")
        _insert_card(conn, name="Cand X", color_identity="G", subtypes="Elf", card_types="Creature")
        cache = build_candidate_cache(conn)
        ctx = build_penalty_context(conn, ["Cmdr A"], candidate_cache=cache)
        assert "Cand X" in ctx.candidate_rows
        conn.close()


# ---------------------------------------------------------------------------
# Helper to build a minimal PenaltyContext for unit tests
# ---------------------------------------------------------------------------


def _make_minimal_ctx(**overrides: Any) -> PenaltyContext:
    defaults: dict[str, Any] = {
        "cmdr_identity": frozenset({"G"}),
        "cmdr_subtypes": frozenset(),
        "cmdr_card_types": frozenset({"Creature"}),
        "cmdr_allows_background": False,
        "cmdr_has_doctor": False,
        "cmdr_counter_types": frozenset(),
        "cmdr_uses_p1p1": False,
        "cmdr_p1p1_is_primary": False,
        "cmdr_self_mill": False,
        "cmdr_has_abilities": frozenset(),
        "cmdr_has_types": frozenset(),
        "cmdr_has_keywords": frozenset(),
        "cmdr_token_subtypes": frozenset(),
        "cmdr_is_tribal": False,
        "candidate_rows": {},
        "creature_static_scopes": {},
        "candidate_excludes": {},
        "candidate_token_types": {},
        "candidate_counter_types": {},
        "candidate_counters_on_land": {},
        "candidate_opp_mill": {},
    }
    defaults.update(overrides)
    return PenaltyContext(**defaults)
