"""Tests for the counter_producer complement rule and etbCounter keyword fix.

Marchesa, Ghave, Pir/Toothy and other +1/+1 counter commanders benefit
from cards that ACTIVELY ADD +1/+1 counters to creatures. Two gaps
identified:

1. No rule matches ``effect: PutCounter`` / ``PutCounterAll`` with
   counter_type ``P1P1`` targeting creatures — so Unspeakable Symbol
   ("Pay 3 life: put +1/+1 counter on target creature"), Thran Vigil,
   and Drana, Liberator of Malakir score 0 against Marchesa.

2. The existing ``counter_keyword`` rule enumerates a fixed keyword
   list (Modular, Undying, Persist, Evolve, Fabricate, Riot) but the
   importer emits ``etbCounter:P1P1:N`` for creatures that enter with
   N +1/+1 counters (Iron Apprentice, Walking Ballista). Those were
   slipping past.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

# Integration: requires the full 32k-card synergy.db produced by
# scripts/import_cardsfolder.py.
pytestmark = pytest.mark.skipif(
    not Path("data/synergy.db").exists(),
    reason="requires data/synergy.db (run scripts/import_cardsfolder.py)",
)


@pytest.fixture(scope="module")
def conn():
    c = sqlite3.connect("data/synergy.db")
    c.row_factory = sqlite3.Row
    yield c
    c.close()


class TestCounterProducerRule:
    """Direct unit tests via _find_counter_producer helper."""

    def test_marchesa_catches_unspeakable_symbol(self, conn):
        from mtg_synergy_graph.complement_rules.density import _find_counter_producer
        from mtg_synergy_graph.graph_engine import load_ports_for_set

        ports = load_ports_for_set(conn, ["Marchesa, the Black Rose"])
        results = _find_counter_producer(conn, ports, {"Marchesa, the Black Rose"})
        names = {r.candidate for r in results}
        assert "Unspeakable Symbol" in names
        assert "Thran Vigil" in names
        assert "Drana, Liberator of Malakir" in names

    def test_rule_id(self, conn):
        from mtg_synergy_graph.complement_rules.density import _find_counter_producer
        from mtg_synergy_graph.graph_engine import load_ports_for_set

        ports = load_ports_for_set(conn, ["Marchesa, the Black Rose"])
        results = _find_counter_producer(conn, ports, {"Marchesa, the Black Rose"})
        assert all(r.rule_id == "counter_producer" for r in results)

    def test_no_counter_interest_no_match(self, conn):
        """Commander without P1P1 counter interest -> no matches."""
        from mtg_synergy_graph.complement_rules.density import _find_counter_producer
        from mtg_synergy_graph.graph_engine import load_ports_for_set

        ports = load_ports_for_set(conn, ["Marrow-Gnawer"])
        results = _find_counter_producer(conn, ports, {"Marrow-Gnawer"})
        assert results == []

    def test_counter_producer_commander_no_match(self, conn):
        """Commanders who PUT counters themselves (Lathiel, Ezuri, Hamza)
        don't need more counter producers — they need payoffs. Rule must
        NOT fire: narrow gate requires a trigger whose valid_filter looks
        for creatures that ALREADY have counters (Marchesa pattern)."""
        from mtg_synergy_graph.complement_rules.density import _find_counter_producer
        from mtg_synergy_graph.graph_engine import load_ports_for_set

        for cmdr in [
            "Lathiel, the Bounteous Dawn",
            "Ezuri, Claw of Progress",
            "Hamza, Guardian of Arashin",
            "Animar, Soul of Elements",
        ]:
            ports = load_ports_for_set(conn, [cmdr])
            results = _find_counter_producer(conn, ports, {cmdr})
            assert results == [], f"{cmdr} should not trigger counter_producer"


class TestEtbCounterInCounterProducer:
    """etbCounter:P1P1:N creatures (Iron Apprentice, Walking Ballista)
    should match via counter_producer with a separate cand_event so
    they don't dilute the existing counter_keyword IDF group."""

    def test_iron_apprentice_in_counter_producer(self, conn):
        from mtg_synergy_graph.complement_rules.density import _find_counter_producer
        from mtg_synergy_graph.graph_engine import load_ports_for_set

        ports = load_ports_for_set(conn, ["Marchesa, the Black Rose"])
        results = _find_counter_producer(conn, ports, {"Marchesa, the Black Rose"})
        names = {r.candidate for r in results}
        assert "Iron Apprentice" in names
        assert "Walking Ballista" in names

    def test_etb_counter_uses_distinct_cand_event(self, conn):
        """Keep etbCounter matches in a separate IDF group from PutCounter
        matches — distinct cand_event values."""
        from mtg_synergy_graph.complement_rules.density import _find_counter_producer
        from mtg_synergy_graph.graph_engine import load_ports_for_set

        ports = load_ports_for_set(conn, ["Marchesa, the Black Rose"])
        results = _find_counter_producer(conn, ports, {"Marchesa, the Black Rose"})
        events = {(r.candidate, r.cand_event) for r in results}
        # Iron Apprentice should be tagged as etbCounter_P1P1
        assert any(c == "Iron Apprentice" and e == "etbCounter_P1P1" for c, e in events)
        # Unspeakable Symbol should be tagged as PutCounter
        assert any(c == "Unspeakable Symbol" and e == "PutCounter" for c, e in events)

    def test_counter_keyword_unchanged(self, conn):
        """Existing counter_keyword behaviour (Modular/Undying/Persist/etc)
        must be preserved — unaffected by the etbCounter addition to
        counter_producer."""
        from mtg_synergy_graph.complement_rules.density import (
            _find_counter_keyword_synergy,
        )
        from mtg_synergy_graph.graph_engine import load_ports_for_set

        ports = load_ports_for_set(conn, ["Marchesa, the Black Rose"])
        results = _find_counter_keyword_synergy(conn, ports, {"Marchesa, the Black Rose"})
        names = {r.candidate for r in results}
        # Undying creature — classic counter_keyword match
        assert "Flayer of the Hatebound" in names
        # Iron Apprentice (etbCounter) must NOT be here — it's in counter_producer
        assert "Iron Apprentice" not in names
