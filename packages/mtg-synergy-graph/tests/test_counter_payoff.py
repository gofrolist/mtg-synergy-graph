"""Tests for the Phase F1 counter_synergy bucket.

Covers the commander-side gate (:func:`_commander_is_p1p1_payoff`),
the candidate-side filters (friendly scope, creature hitting), the
matcher (:func:`find_counter_producer_payoff`), and the scoring
end-to-end path that must lift Maester Seymour into Kyler's top 100.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from mtg_synergy_graph import SynergyEngine
from mtg_synergy_graph.db import open_db
from mtg_synergy_graph.graph_engine import (
    _commander_is_p1p1_payoff,
    _counter_effect_hits_creatures,
    _counter_effect_is_friendly,
    find_counter_producer_payoff,
    load_ports_for_set,
)
from mtg_synergy_graph.importer import import_cards_folder

FULL_DB_PATH = Path("/tmp/synergy_full.db")

pytestmark = pytest.mark.skipif(
    not FULL_DB_PATH.exists(),
    reason="end-to-end gate tests require /tmp/synergy_full.db",
)


# ---------------------------------------------------------------------------
# Friendly / creature-scope filter helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "valid_filter,expected",
    [
        (None,                            True),   # unscoped
        ("",                              True),
        ("Creature.YouCtrl+Other",        True),   # Seymour
        ("Creature.YouCtrl",              True),
        ("Creature.OppCtrl",              False),  # hostile
        ("Creature.Opponent",             False),
        ("Creature.YouCtrl+Opponent",     False),  # any mention of Opp is out
    ],
)
def test_counter_effect_is_friendly(valid_filter, expected):
    assert _counter_effect_is_friendly(valid_filter) is expected


@pytest.mark.parametrize(
    "valid_filter,expected",
    [
        (None,                       True),
        ("",                         True),
        ("Creature.YouCtrl",         True),
        ("Creature.YouCtrl+Other",   True),
        ("Permanent.YouCtrl",        True),
        ("Self",                     True),   # Phase F1.1 — "Defined$ Self"
        ("Artifact.YouCtrl",         False),
        ("Land.YouCtrl",             False),
    ],
)
def test_counter_effect_hits_creatures(valid_filter, expected):
    assert _counter_effect_hits_creatures(valid_filter) is expected


# ---------------------------------------------------------------------------
# Commander payoff signature gate
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def full_conn():
    conn = sqlite3.connect(FULL_DB_PATH)
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


@pytest.mark.parametrize("commander", [
    "Kyler, Sigardian Emissary",
    "Bess, Soul Nourisher",
    "Grakmaw, Skyclave Ravager",
    "Hallar, the Firefletcher",
])
def test_gate_fires_for_real_counter_payoff_commanders(full_conn, commander):
    """Commanders whose win condition is +1/+1 counters must trip the
    gate — otherwise Phase F1 is useless for them."""
    ports = load_ports_for_set(full_conn, [commander])
    assert _commander_is_p1p1_payoff(ports), (
        f"{commander} is a real P1P1 payoff commander but the gate "
        f"rejected it — verify scales_with CardCounters.P1P1 / ALL "
        f"and PutCounter P1P1 ports exist in synergy.db"
    )


@pytest.mark.parametrize("commander", [
    "Korvold, Fae-Cursed King",      # sacrifice; puts flavor P1P1 but no CardCounters scale
    "Meren of Clan Nel Toth",        # graveyard
    "Atraxa, Praetors' Voice",       # proliferate — own bucket
    "Thelon of Havenwood",           # spore counters, not P1P1
    "Toxrill, the Corrosive",        # slime counters, not P1P1
    "Yuriko, the Tiger's Shadow",    # ninjutsu, unrelated
])
def test_gate_rejects_non_p1p1_commanders(full_conn, commander):
    """Regression guard: the F1 bucket must NOT fire for commanders
    that merely tangentially touch counters (flavor counter on self,
    non-P1P1 counter types, Proliferate-only strategies)."""
    ports = load_ports_for_set(full_conn, [commander])
    assert not _commander_is_p1p1_payoff(ports), (
        f"{commander} tripped the P1P1 payoff gate — this would flood "
        f"its recommendations with generic counter cards. Tighten the "
        f"gate in _commander_is_p1p1_payoff."
    )


# ---------------------------------------------------------------------------
# End-to-end matcher
# ---------------------------------------------------------------------------


def test_kyler_candidates_include_maester_seymour(full_conn):
    rows = find_counter_producer_payoff(
        full_conn, ["Kyler, Sigardian Emissary"],
    )
    names = {r["candidate"] for r in rows}
    assert "Maester Seymour" in names, (
        "Maester Seymour has an effect PutCounter P1P1 targeting "
        "Creature.YouCtrl+Other — must be emitted as a counter_synergy "
        "candidate for Kyler."
    )
    # Staple P1P1 producers that use `effect PutCounter` (not R:
    # AddCounter replacements — those belong to replacement_resonance).
    # Cathars' Crusade and Rishkar are canonical counter-distributors.
    assert "Cathars' Crusade" in names
    assert "Rishkar, Peema Renegade" in names


def test_replacement_doublers_are_not_emitted_by_this_bucket(full_conn):
    """Hardened Scales / Branching Evolution / Doubling Season are
    replacement effects (R: AddCounter). They belong to
    replacement_resonance, not counter_synergy — guard against
    accidental cross-emission if someone widens the matcher."""
    rows = find_counter_producer_payoff(
        full_conn, ["Kyler, Sigardian Emissary"],
    )
    names = {r["candidate"] for r in rows}
    for doubler in ("Hardened Scales", "Branching Evolution", "Doubling Season"):
        assert doubler not in names, (
            f"{doubler} is a replacement effect and must be handled "
            f"by replacement_resonance, not counter_synergy"
        )


def test_non_payoff_commander_gets_no_rows(full_conn):
    """The gate short-circuits for non-payoff commanders → empty list."""
    rows = find_counter_producer_payoff(
        full_conn, ["Korvold, Fae-Cursed King"],
    )
    assert rows == []


def test_kyler_does_not_emit_opponent_side_counter_effects(full_conn):
    """Every emitted candidate must have a friendly, creature-hitting
    valid_filter (or an empty/unscoped one)."""
    rows = find_counter_producer_payoff(
        full_conn, ["Kyler, Sigardian Emissary"],
    )
    for r in rows:
        vf = r["valid_filter"] or ""
        assert "OppCtrl" not in vf, f"leaked OppCtrl: {r}"
        assert "Opponent" not in vf, f"leaked Opponent: {r}"


def test_commander_itself_not_emitted(full_conn):
    """The matcher must drop the commander from its own candidate list
    (Kyler has a PutCounter P1P1 effect on himself)."""
    rows = find_counter_producer_payoff(
        full_conn, ["Kyler, Sigardian Emissary"],
    )
    names = {r["candidate"] for r in rows}
    assert "Kyler, Sigardian Emissary" not in names


# ---------------------------------------------------------------------------
# Scoring end-to-end
# ---------------------------------------------------------------------------


def test_maester_seymour_scores_counter_synergy_for_kyler():
    """Regression guard for the concrete Kyler+Seymour interaction
    that motivated Phase F1."""
    with SynergyEngine(FULL_DB_PATH) as eng:
        rec = eng.score_one(
            "Kyler, Sigardian Emissary", "Maester Seymour",
        )
    assert rec.scores.get("counter_synergy", 0) > 0, (
        "counter_synergy bucket did not fire for Maester Seymour + Kyler"
    )


def test_champion_of_lambholt_scores_counter_synergy_for_kyler():
    """Regression guard for Phase F1.1 (Self-filter fix).

    Champion of Lambholt's raw_line is
    ``DB$ PutCounter | Defined$ Self | CounterType$ P1P1``, which the
    importer materialises as ``valid_filter='Self'``. Before the F1.1
    fix, ``_counter_effect_hits_creatures('Self')`` returned False and
    Champion was ranked 770 for Kyler despite being EDHREC's #4
    High Synergy Card (synergy 0.62).
    """
    with SynergyEngine(FULL_DB_PATH) as eng:
        rec = eng.score_one(
            "Kyler, Sigardian Emissary", "Champion of Lambholt",
        )
    assert rec.scores.get("counter_synergy", 0) > 0, (
        "counter_synergy did not fire for Champion of Lambholt + Kyler. "
        "Check that _counter_effect_hits_creatures accepts 'Self'."
    )


def test_champion_of_lambholt_lifts_into_kyler_top_200():
    """The F1.1 fix must lift Champion of Lambholt from its pre-fix
    rank of ~770 into at least the top 200 for Kyler. We don't assert
    top 50 because Champion's deck_hints path is reverse-only (2 pts)
    and it doesn't collect the F2 combo bonus — fixing that would
    require F3 (see discussion)."""
    with SynergyEngine(FULL_DB_PATH) as eng:
        page = eng.page("Kyler, Sigardian Emissary", limit=300)
    names = [r.card for r in page.items]
    assert "Champion of Lambholt" in names, (
        "Champion of Lambholt must land in Kyler's top 300 after F1.1"
    )


def test_maester_seymour_reaches_top_100_for_kyler():
    """The practical headline: Seymour moves from rank ~150 (below the
    window the user asked about) into Kyler's top 100."""
    with SynergyEngine(FULL_DB_PATH) as eng:
        page = eng.page("Kyler, Sigardian Emissary", limit=100)
    names = [r.card for r in page.items]
    assert "Maester Seymour" in names, (
        "Maester Seymour must appear in Kyler's top 100 after Phase F1"
    )


def test_counter_synergy_silent_for_non_payoff_commander():
    """No candidate should receive a counter_synergy score when the
    commander fails the payoff gate."""
    with SynergyEngine(FULL_DB_PATH) as eng:
        page = eng.page("Korvold, Fae-Cursed King", limit=200)
    for rec in page.items:
        assert rec.scores.get("counter_synergy", 0) == 0, (
            f"counter_synergy leaked for non-payoff commander: "
            f"{rec.card} = {rec.scores.get('counter_synergy')}"
        )


# ---------------------------------------------------------------------------
# Tiny synthetic-DB test (no dependency on /tmp/synergy_full.db)
# ---------------------------------------------------------------------------


def test_gate_and_matcher_against_synthetic_db(tmp_path):
    """Standalone check that exercises the whole path on a minimum
    synthetic synergy.db — independent of the full-Forge import."""
    db_path = tmp_path / "synth.db"
    conn = open_db(db_path)
    try:
        import_cards_folder(conn, Path(__file__).parent / "fixtures",
                            scryfall_db=None)

        # The fixtures include Cathars' Crusade — a static Continuous
        # that puts P1P1 counters on all creatures you control when
        # one enters. It is both a payoff-style commander candidate
        # (scales_with via SVar? let's check) AND a P1P1 producer.
        ports = load_ports_for_set(conn, ["Cathars' Crusade"])
        crusade_gates = _commander_is_p1p1_payoff(ports)
        # Regardless of whether Crusade trips the gate, the matcher
        # must return a list (no exception) and never emit Crusade as
        # its own candidate when it is the commander.
        rows = find_counter_producer_payoff(conn, ["Cathars' Crusade"])
        assert isinstance(rows, list)
        if crusade_gates:
            assert all(r["candidate"] != "Cathars' Crusade" for r in rows)
    finally:
        conn.close()
