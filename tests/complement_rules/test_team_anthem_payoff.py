import sqlite3
import types

import pytest

from mtg_synergy_graph.complement_rules import statics as statics_mod
from mtg_synergy_graph.complement_rules.registry import attributable_rules_for_port
from mtg_synergy_graph.complement_rules.statics import (
    _commander_has_team_anthem_static,
    _find_team_anthem_payoffs,
)


def _static(affected_scope, raw_line):
    return {
        "port_type": "static",
        "event_class": "Continuous",
        "affected_scope": affected_scope,
        "raw_line": raw_line,
    }


@pytest.mark.parametrize(
    "scope,raw",
    [
        # Avacyn: grants Indestructible to your other permanents.
        ("Permanent.Other+YouCtrl", "{'AddKeyword': 'Indestructible'}"),
        # Iroas: Menace to your creatures.
        ("Creature.YouCtrl", "{'AddKeyword': 'Menace'}"),
        # Plain your-team +1/+1 anthem.
        ("Creature.YouCtrl", "{'AddPower': '1', 'AddToughness': '1'}"),
    ],
)
def test_team_anthem_static_qualifies(scope, raw):
    assert _commander_has_team_anthem_static([_static(scope, raw)]) is True


@pytest.mark.parametrize(
    "scope,raw,why",
    [
        ("Creature.Black+Other", "{'AddPower': '1', 'AddToughness': '1'}", "symmetric, no YouCtrl"),
        ("Card.Self", "{'AddKeyword': 'Double Strike'}", "voltron self-only"),
        ("Goblin.YouCtrl", "{'AddPower': '1', 'AddToughness': '1'}", "subtype base (lord)"),
        # Subtype folded in as a +-qualifier after a Creature base (Admiral Beckett Brass).
        ("Creature.Pirate+Other+YouCtrl", "{'AddPower': '1', 'AddToughness': '1'}", "subtype qualifier (restricted lord)"),
        # Condition folded in as a qualifier (Abzan Falconer — only counter-bearing creatures).
        ("Creature.YouCtrl+counters_GE1_P1P1", "{'AddPower': '1', 'AddToughness': '1'}", "conditional anthem"),
        # Negative pump is a drawback, not a payoff.
        ("Creature.YouCtrl", "{'AddPower': '-1', 'AddToughness': '-1'}", "drawback"),
    ],
)
def test_team_anthem_static_rejected(scope, raw, why):
    assert _commander_has_team_anthem_static([_static(scope, raw)]) is False, why


def test_non_static_port_ignored():
    trigger = {"port_type": "trigger", "event_class": "Attacks", "affected_scope": "", "raw_line": ""}
    assert _commander_has_team_anthem_static([trigger]) is False


@pytest.fixture()
def anthem_conn(monkeypatch):
    # The rule is flag-gated default-OFF. Every emitter test below exercises the
    # firing path, so enable the flag here; the flag-off test overrides it.
    monkeypatch.setattr(statics_mod, "_ENABLE_TEAM_ANTHEM_PAYOFF", True)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE card_ports (
            id INTEGER PRIMARY KEY,
            card_name TEXT,
            port_type TEXT,
            event_class TEXT,
            affected_scope TEXT,
            raw_line TEXT
        );
        """
    )
    rows = [
        # Own-board creature-token producer (P/T in TokenScript, owner defaults to controller).
        ("Grave Titan", "effect", "Token", "{'TokenScript': 'b_2_2_zombie', 'TokenOwner': 'You'}"),
        # Non-creature (Treasure) producer — no P/T in TokenScript.
        ("Smothering Tithe", "effect", "Token", "{'TokenScript': 'c_a_treasure'}"),
        # Opponent-owned creature token — the body lands off the anthem's board.
        ("Akroan Horse", "effect", "Token", "{'TokenScript': 'w_1_1_soldier', 'TokenOwner': 'Player.Opponent'}"),
        # Own-board creature-token multiplier.
        ("Doubling Season", "replacement", "CreateToken", "{'ValidToken': 'Card.YouCtrl', 'ReplaceWith': 'DoubleToken'}"),
        # Halving replacement — inverted polarity, must NOT be scored as positive.
        ("Halving Season", "replacement", "CreateToken", "{'ValidToken': 'Card.OppCtrl', 'ReplaceWith': 'HalveToken'}"),
        # Non-creature-only doubler (Clue/Food/Treasure) — not a creature-body multiplier.
        ("Academy Manufactor", "replacement", "CreateToken", "{'ValidPlayer': 'You', 'ValidToken': 'Clue,Food,Treasure', 'ReplaceWith': 'TokenReplace'}"),
    ]
    conn.executemany(
        "INSERT INTO card_ports (card_name, port_type, event_class, raw_line) VALUES (?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    return conn


_AVACYN_STATIC = {
    "port_type": "static",
    "event_class": "Continuous",
    "affected_scope": "Permanent.Other+YouCtrl",
    "raw_line": "{'AddKeyword': 'Indestructible'}",
}


def test_emitter_creature_producer_fires(anthem_conn):
    comps = _find_team_anthem_payoffs(anthem_conn, [_AVACYN_STATIC], set())
    grave_titan = next(c for c in comps if c.candidate == "Grave Titan")
    assert grave_titan.rule_id == "team_anthem_payoff"
    assert grave_titan.cand_event == "token_producer"


def test_emitter_treasure_maker_excluded(anthem_conn):
    comps = _find_team_anthem_payoffs(anthem_conn, [_AVACYN_STATIC], set())
    assert "Smothering Tithe" not in {c.candidate for c in comps}


def test_emitter_opponent_owned_producer_excluded(anthem_conn):
    # Akroan Horse gives its Soldier to an opponent — not a body the anthem buffs.
    comps = _find_team_anthem_payoffs(anthem_conn, [_AVACYN_STATIC], set())
    assert "Akroan Horse" not in {c.candidate for c in comps}


def test_emitter_doubler_tier(anthem_conn):
    comps = _find_team_anthem_payoffs(anthem_conn, [_AVACYN_STATIC], set())
    doubling_season = next(c for c in comps if c.candidate == "Doubling Season")
    assert doubling_season.cand_event == "token_doubler"


def test_emitter_halving_replacement_excluded(anthem_conn):
    # Halving Season reduces (opponent) tokens — inverted polarity, must not score.
    comps = _find_team_anthem_payoffs(anthem_conn, [_AVACYN_STATIC], set())
    assert "Halving Season" not in {c.candidate for c in comps}


def test_emitter_non_creature_doubler_excluded(anthem_conn):
    # Academy Manufactor only doubles Clue/Food/Treasure — no creature bodies.
    comps = _find_team_anthem_payoffs(anthem_conn, [_AVACYN_STATIC], set())
    assert "Academy Manufactor" not in {c.candidate for c in comps}


def test_emitter_no_qualifying_static_returns_empty(anthem_conn):
    self_only = dict(_AVACYN_STATIC, affected_scope="Card.Self")
    assert _find_team_anthem_payoffs(anthem_conn, [self_only], set()) == []


def test_emitter_dedup_single_complement_per_candidate(anthem_conn):
    # A card that is BOTH a doubler and a creature producer resolves to ONE
    # complement, at the strong (doubler) tier.
    anthem_conn.execute(
        "INSERT INTO card_ports (card_name, port_type, event_class, raw_line) VALUES ('Ojer Taq', 'effect', 'Token', ?)",
        ("{'TokenScript': 'w_1_1_human', 'TokenOwner': 'You'}",),
    )
    anthem_conn.execute(
        "INSERT INTO card_ports (card_name, port_type, event_class, raw_line) "
        "VALUES ('Ojer Taq', 'replacement', 'CreateToken', ?)",
        ("{'ValidToken': 'Card.YouCtrl', 'ReplaceWith': 'TripleToken'}",),
    )
    anthem_conn.commit()
    comps = [c for c in _find_team_anthem_payoffs(anthem_conn, [_AVACYN_STATIC], set()) if c.candidate == "Ojer Taq"]
    assert len(comps) == 1
    assert comps[0].cand_event == "token_doubler"


def test_emitter_excludes_commander_itself(anthem_conn):
    comps = _find_team_anthem_payoffs(anthem_conn, [_AVACYN_STATIC], {"Grave Titan"})
    assert "Grave Titan" not in {c.candidate for c in comps}


def test_emitter_flag_off_returns_empty(anthem_conn, monkeypatch):
    monkeypatch.setattr(statics_mod, "_ENABLE_TEAM_ANTHEM_PAYOFF", False)
    assert _find_team_anthem_payoffs(anthem_conn, [_AVACYN_STATIC], set()) == []


def test_emitter_producer_tier_reads_candidate_cache(anthem_conn):
    # When a candidate_cache is supplied, the producer tier reads its
    # token_effect_rows instead of re-scanning the DB. Prove it by putting a
    # producer ONLY in the cache (absent from anthem_conn's card_ports).
    cache = types.SimpleNamespace(
        token_effect_rows=(("Cached Beast", "{'TokenScript': 'g_3_3_beast', 'TokenOwner': 'You'}"),),
    )
    comps = _find_team_anthem_payoffs(anthem_conn, [_AVACYN_STATIC], set(), cache)
    cands = {c.candidate for c in comps}
    assert "Cached Beast" in cands  # came from the cache
    assert "Grave Titan" not in cands  # DB producer NOT scanned when cache is used


def test_rule_gate_flag_aware():
    port = {
        "port_type": "static",
        "event_class": "Continuous",
        "affected_scope": "Creature.YouCtrl",
        "raw_line": "{'AddKeyword': 'Menace'}",
    }
    # Flag off: gate reports NO coverage so gap_report/quality see the truth.
    statics_mod._ENABLE_TEAM_ANTHEM_PAYOFF = False
    assert "team_anthem_payoff" not in attributable_rules_for_port(port)
    statics_mod._ENABLE_TEAM_ANTHEM_PAYOFF = True
    try:
        assert "team_anthem_payoff" in attributable_rules_for_port(port)
    finally:
        statics_mod._ENABLE_TEAM_ANTHEM_PAYOFF = False
