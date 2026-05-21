"""Tests for ``SynergyEngine._render_explanation`` path-info rendering.

Unit 4 of plan ``docs/plans/2026-04-23-001-feat-self-bridging-cascade-pathway-plan.md``.
Verifies that the explain surface surfaces a ``self_bridging_cascade:``
line per firing when the flag is on, and preserves prior behaviour
(byte-identical output) when the flag is off or no self-bridging
complements exist.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

from mtg_synergy_graph.complement_rules import pathway
from mtg_synergy_graph.complement_rules.core import PortComplement
from mtg_synergy_graph.db import open_db
from mtg_synergy_graph.engine import SynergyEngine
from mtg_synergy_graph.universal_scorer import UniversalScore


@pytest.fixture()
def engine_with_mini_db(tmp_path: Path) -> Iterator[SynergyEngine]:
    """Production-schema DB seeded with Korvold + Gravecrawler."""
    db_path = tmp_path / "synergy.db"
    conn = open_db(db_path)
    conn.execute(
        "INSERT INTO cards (name, card_types, types, subtypes, supertypes, "
        "keywords, color_identity, cmc, edhrec_rank, oracle_id, legal_commander) "
        "VALUES ('Korvold', 'Creature', 'Legendary Creature Dragon Noble', "
        "'Dragon Noble', 'Legendary', '', 'B,R,G', 5, NULL, NULL, 1)"
    )
    conn.execute(
        "INSERT INTO card_ports (card_name, port_type, event_class, valid_filter) "
        "VALUES ('Korvold', 'trigger', 'Sacrificed', 'Permanent')"
    )
    conn.execute(
        "INSERT INTO card_ports (card_name, port_type, event_class, valid_filter) "
        "VALUES ('Korvold', 'effect', 'Sacrifice', 'Permanent.Other')"
    )
    conn.execute(
        "INSERT INTO cards (name, card_types, types, subtypes, supertypes, "
        "keywords, color_identity, cmc, edhrec_rank, oracle_id, legal_commander) "
        "VALUES ('Gravecrawler', 'Creature', 'Creature Zombie', 'Zombie', '', '', 'B', 1, NULL, NULL, 1)"
    )
    conn.execute(
        "INSERT INTO card_ports (card_name, port_type, event_class) VALUES ('Gravecrawler', 'cost', 'sacrifice')"
    )
    conn.execute(
        "INSERT INTO card_ports (card_name, port_type, event_class) VALUES ('Gravecrawler', 'trigger', 'Sacrificed')"
    )
    conn.commit()
    conn.close()
    engine = SynergyEngine(db_path)
    try:
        yield engine
    finally:
        engine.close()


# ---------------------------------------------------------------------------
# path_info population
# ---------------------------------------------------------------------------


def test_helper_emits_populated_path_info() -> None:
    """``_find_self_bridging_cascade`` sets ``path_info`` on every
    complement it emits. Empty string is reserved for other rule
    families that don't carry narrator metadata."""
    conn = sqlite3.connect(":memory:")
    try:
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE cards (name TEXT PRIMARY KEY);
            CREATE TABLE card_ports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                card_name TEXT, port_type TEXT, event_class TEXT,
                valid_filter TEXT, zone_origin TEXT, zone_destination TEXT,
                counter_type TEXT
            );
            """
        )
        conn.execute("INSERT INTO cards VALUES ('SacFodder')")
        conn.execute(
            "INSERT INTO card_ports (card_name, port_type, event_class) VALUES ('SacFodder', 'cost', 'sacrifice')"
        )
        conn.execute(
            "INSERT INTO card_ports (card_name, port_type, event_class) VALUES ('SacFodder', 'trigger', 'Sacrificed')"
        )
        conn.commit()

        cmdr_ports = [
            {
                "port_type": "trigger",
                "event_class": "Sacrificed",
                "valid_filter": "Permanent",
                "zone_origin": "",
                "zone_destination": "",
                "counter_type": "",
            },
            {
                "port_type": "effect",
                "event_class": "Sacrifice",
                "valid_filter": "Permanent.Other",
                "zone_origin": "",
                "zone_destination": "",
                "counter_type": "",
            },
        ]
        result = pathway._find_self_bridging_cascade(conn, cmdr_ports, set())
        assert len(result) == 1
        info = result[0].path_info
        # Path info names both port subkinds and the channel.
        assert "cost.sacrifice" in info
        assert "trigger.Sacrificed" in info
        assert "cost_feeds" in info
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# _render_explanation plumbing
# ---------------------------------------------------------------------------


def test_render_explanation_adds_path_line_at_default_flag(
    engine_with_mini_db: SynergyEngine,
) -> None:
    """With the flag at its landed default (True) and a page that
    surfaces Gravecrawler, the explanation contains exactly one
    ``self_bridging_cascade:`` line."""
    page = engine_with_mini_db.page(
        commander=("Korvold",),
        offset=0,
        limit=10,
        include_explanations=True,
    )
    gravecrawler = next((item for item in page.items if item.card == "Gravecrawler"), None)
    assert gravecrawler is not None
    assert gravecrawler.explanation is not None
    path_lines = [line for line in gravecrawler.explanation if line.startswith("self_bridging_cascade:")]
    assert len(path_lines) == 1
    line = path_lines[0]
    assert "cost.sacrifice" in line
    assert "trigger.Sacrificed" in line
    assert "cost_feeds" in line


def test_render_explanation_omits_path_line_when_flag_patched_off(
    engine_with_mini_db: SynergyEngine,
) -> None:
    """With the flag patched off, no self_bridging_cascade complement
    exists and no path line is emitted. Guards the flag toggle path."""
    with patch.object(pathway, "_ENABLE_PATHWAY_RULES", False):
        page = engine_with_mini_db.page(
            commander=("Korvold",),
            offset=0,
            limit=10,
            include_explanations=True,
        )
    for item in page.items:
        if item.explanation is None:
            continue
        for line in item.explanation:
            assert not line.startswith("self_bridging_cascade:")


def test_render_explanation_accepts_none_universal_score() -> None:
    """Legacy callers that pass only (card, scores) without the
    UniversalScore must still work -- the new parameter defaults to
    ``None`` and the renderer skips the path-info loop."""
    engine = SynergyEngine.__new__(SynergyEngine)  # type: ignore[call-arg]
    scores = {"port_match": 5.0, "total": 5.0}
    out = engine._render_explanation("TestCard", scores)
    assert out  # at least the prose bucket line
    assert not any(line.startswith("self_bridging_cascade:") for line in out)


def test_render_explanation_dedups_identical_path_info() -> None:
    """If the same path_info appears on multiple complements for a
    candidate, only one line is emitted."""
    engine = SynergyEngine.__new__(SynergyEngine)  # type: ignore[call-arg]
    duplicate_info = "cost.sacrifice <-> trigger.Sacrificed (channel: cost_feeds)"
    us = UniversalScore(
        complements=[
            PortComplement(
                rule_id="self_bridging_cascade",
                direction="synergy",
                candidate="X",
                cmdr_event="a",
                cand_event="self_bridging",
                filter_group="depth_2",
                path_info=duplicate_info,
            ),
            PortComplement(
                rule_id="self_bridging_cascade",
                direction="synergy",
                candidate="X",
                cmdr_event="b",
                cand_event="self_bridging",
                filter_group="depth_2",
                path_info=duplicate_info,
            ),
        ]
    )
    scores = {"total": 0.0}
    out = engine._render_explanation("X", scores, us)
    path_lines = [line for line in out if line.startswith("self_bridging_cascade:")]
    assert len(path_lines) == 1


def test_render_explanation_skips_other_rules_path_info() -> None:
    """Only ``self_bridging_cascade`` complements surface their
    ``path_info``; other rules are ignored in this loop even if they
    populate the field (future-proofing)."""
    engine = SynergyEngine.__new__(SynergyEngine)  # type: ignore[call-arg]
    us = UniversalScore(
        complements=[
            PortComplement(
                rule_id="some_other_rule",
                direction="synergy",
                candidate="X",
                cmdr_event="a",
                cand_event="b",
                filter_group="",
                path_info="should not surface",
            ),
        ]
    )
    scores = {"total": 0.0}
    out = engine._render_explanation("X", scores, us)
    assert not any("should not surface" in line for line in out)
