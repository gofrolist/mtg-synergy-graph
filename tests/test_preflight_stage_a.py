"""Tests for ``preflight.gates.stage_a_golden_coverage``.

Stage A is deterministic: given a fixed DB state and fixture, the verdict
is purely a function of the candidate's signature. These tests construct
small in-memory SQLite databases that mirror the project schema's relevant
columns, plus a tiny fixture file, to exercise every branch of the
verdict matrix.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from mtg_synergy_graph.preflight import (
    Candidate,
    PipelineVerdict,
    Severity,
    evaluate_one,
)
from mtg_synergy_graph.preflight.gates import stage_a_golden_coverage

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_db(
    *,
    cards: list[dict],
    ports: list[dict],
    has_legal_column: bool = True,
) -> sqlite3.Connection:
    """Build an in-memory synergy-DB-like schema with the cards/ports rows."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    cards_cols = "name TEXT PRIMARY KEY, types TEXT, color_identity TEXT, card_types TEXT"
    if has_legal_column:
        cards_cols += ", legal_commander INTEGER DEFAULT 1"
    conn.execute(f"CREATE TABLE cards ({cards_cols})")
    conn.execute(
        """
        CREATE TABLE card_ports (
            id INTEGER PRIMARY KEY,
            card_name TEXT,
            port_type TEXT,
            event_class TEXT,
            valid_filter TEXT,
            zone_origin TEXT,
            zone_destination TEXT,
            replacement_result TEXT
        )
        """
    )
    for card in cards:
        if has_legal_column:
            conn.execute(
                "INSERT INTO cards (name, types, color_identity, card_types, legal_commander) VALUES (?, ?, ?, ?, ?)",
                (
                    card["name"],
                    card.get("types", "Legendary Creature"),
                    card.get("color_identity", "B"),
                    card.get("card_types", "Creature"),
                    card.get("legal_commander", 1),
                ),
            )
        else:
            conn.execute(
                "INSERT INTO cards (name, types, color_identity, card_types) VALUES (?, ?, ?, ?)",
                (
                    card["name"],
                    card.get("types", "Legendary Creature"),
                    card.get("color_identity", "B"),
                    card.get("card_types", "Creature"),
                ),
            )
    for port in ports:
        conn.execute(
            """
            INSERT INTO card_ports
                (card_name, port_type, event_class, valid_filter,
                 zone_origin, zone_destination, replacement_result)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                port["card_name"],
                port.get("port_type", ""),
                port.get("event_class", ""),
                port.get("valid_filter", ""),
                port.get("zone_origin", ""),
                port.get("zone_destination", ""),
                port.get("replacement_result", ""),
            ),
        )
    conn.commit()
    return conn


def _make_fixture(tmp_path: Path, commanders: list[str]) -> Path:
    """Write a minimal 500-fixture-shaped JSON file to ``tmp_path``."""
    path = tmp_path / "fixture.json"
    payload = {
        "schema_version": 1,
        "config_hash": "test",
        "created_at": "2026-05-02T00:00:00Z",
        "entries": [{"commander": c, "scores": [], "hidden_cards": [], "hidden_gem_hit_rate": 0.0} for c in commanders],
    }
    path.write_text(json.dumps(payload))
    return path


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_stage_a_pass_when_fixture_commander_matches_gate(tmp_path):
    """Known-good: fixture commander has a port matching the gate -> PASS."""
    conn = _make_db(
        cards=[{"name": "Fixture Commander"}, {"name": "Other Commander"}],
        ports=[
            {
                "card_name": "Fixture Commander",
                "port_type": "trigger",
                "event_class": "ChangesZone",
                "zone_origin": "Battlefield",
                "zone_destination": "Graveyard",
            },
        ],
    )
    fixture = _make_fixture(tmp_path, ["Fixture Commander", "Other Commander"])
    candidate = Candidate(
        signature=("trigger", "ChangesZone", "Battlefield->Graveyard"),
        gap_id="trigger.ChangesZone[Battlefield->Graveyard]",
    )
    verdict = stage_a_golden_coverage(candidate, conn, fixture_path=fixture)
    assert verdict.severity is Severity.PASS
    assert verdict.name == "stage_a"
    assert "1 fixture commanders" in verdict.reason


# ---------------------------------------------------------------------------
# FIXTURE_BLIND_SPOT (WARN) — the canonical damage_prevention_voltron save case
# ---------------------------------------------------------------------------


def test_stage_a_warn_fixture_blind_spot_when_legal_universe_has_threshold(tmp_path):
    """0 fixture, >=3 legal-universe -> FIXTURE_BLIND_SPOT WARN.

    This is the canonical save case: rule is structurally legitimate but
    no fixture commander carries the gate. Stage A flags it without
    hard-blocking.
    """
    legal_only = [f"Legal Cmdr {i}" for i in range(5)]
    conn = _make_db(
        cards=[{"name": "Fixture Commander"}] + [{"name": n} for n in legal_only],
        ports=[
            {
                "card_name": n,
                "port_type": "replacement",
                "event_class": "DamageDone",
                "replacement_result": "Prevent",
            }
            for n in legal_only
        ],
    )
    fixture = _make_fixture(tmp_path, ["Fixture Commander"])
    candidate = Candidate(
        signature=("replacement", "DamageDone", "Prevent"),
        gap_id="replacement.DamageDone[Prevent]",
    )
    verdict = stage_a_golden_coverage(candidate, conn, fixture_path=fixture)
    assert verdict.severity is Severity.WARN
    assert verdict.name == "stage_a"
    assert "FIXTURE_BLIND_SPOT" in verdict.reason
    assert "5 legal-universe" in verdict.reason


# ---------------------------------------------------------------------------
# REJECT — UNTESTABLE
# ---------------------------------------------------------------------------


def test_stage_a_reject_when_zero_in_both_corpora(tmp_path):
    """0 fixture and 0 legal-universe -> UNTESTABLE REJECT.

    The partner_friends_tribal case from the historical revert corpus
    (0/2737 commanders activate gate).
    """
    conn = _make_db(
        cards=[{"name": "Fixture Commander"}, {"name": "Other Commander"}],
        ports=[],  # no card has the matching port
    )
    fixture = _make_fixture(tmp_path, ["Fixture Commander"])
    candidate = Candidate(
        signature=("trigger", "PartnerFriends", ""),
        gap_id="trigger.PartnerFriends[*]",
    )
    verdict = stage_a_golden_coverage(candidate, conn, fixture_path=fixture)
    assert verdict.severity is Severity.REJECT
    assert "UNTESTABLE" in verdict.reason


def test_stage_a_reject_when_legal_universe_below_threshold(tmp_path):
    """0 fixture and 1 legal-universe -> still UNTESTABLE (below ≥3 threshold)."""
    conn = _make_db(
        cards=[{"name": "Fixture Commander"}, {"name": "One Other"}],
        ports=[
            {
                "card_name": "One Other",
                "port_type": "replacement",
                "event_class": "DamageDone",
                "replacement_result": "Prevent",
            },
        ],
    )
    fixture = _make_fixture(tmp_path, ["Fixture Commander"])
    candidate = Candidate(
        signature=("replacement", "DamageDone", "Prevent"),
        gap_id="replacement.DamageDone[Prevent]",
    )
    verdict = stage_a_golden_coverage(candidate, conn, fixture_path=fixture)
    assert verdict.severity is Severity.REJECT
    assert "UNTESTABLE" in verdict.reason
    assert "1 legal-universe" in verdict.reason


# ---------------------------------------------------------------------------
# Error path
# ---------------------------------------------------------------------------


def test_stage_a_raises_on_empty_port_type(tmp_path):
    """Empty port_type would produce a vacuous predicate -> raise ValueError."""
    conn = _make_db(cards=[], ports=[])
    fixture = _make_fixture(tmp_path, [])
    candidate = Candidate(signature=("", "ChangesZone", ""), gap_id="bad")
    with pytest.raises(ValueError, match="empty port_type"):
        stage_a_golden_coverage(candidate, conn, fixture_path=fixture)


# ---------------------------------------------------------------------------
# Defensive PRAGMA fallback (legacy DB without legal_commander column)
# ---------------------------------------------------------------------------


def test_stage_a_falls_back_to_fixture_only_without_legal_column(tmp_path, caplog):
    """Legacy DB lacking legal_commander column -> fixture-only check.

    Under fallback, FIXTURE_BLIND_SPOT cannot fire — REJECT is emitted
    whenever fixture count is zero.
    """
    legal_only = [f"Legal Cmdr {i}" for i in range(5)]
    conn = _make_db(
        cards=[{"name": "Fixture Commander"}] + [{"name": n} for n in legal_only],
        ports=[
            {
                "card_name": n,
                "port_type": "replacement",
                "event_class": "DamageDone",
                "replacement_result": "Prevent",
            }
            for n in legal_only
        ],
        has_legal_column=False,
    )
    fixture = _make_fixture(tmp_path, ["Fixture Commander"])
    candidate = Candidate(
        signature=("replacement", "DamageDone", "Prevent"),
        gap_id="replacement.DamageDone[Prevent]",
    )
    with caplog.at_level("WARNING", logger="mtg_synergy_graph.preflight.gates"):
        verdict = stage_a_golden_coverage(candidate, conn, fixture_path=fixture)
    # Without the legal column we can't distinguish FIXTURE_BLIND_SPOT
    # from UNTESTABLE — degrade to REJECT and warn.
    assert verdict.severity is Severity.REJECT
    assert "fallback" in verdict.reason.lower()
    assert any("legal_commander column absent" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Pipeline integration
# ---------------------------------------------------------------------------


def test_evaluate_one_returns_pipeline_verdict_with_stage_a_only(tmp_path):
    """In v1.0, evaluate_one wires only Stage A; PipelineVerdict mirrors its severity."""
    conn = _make_db(
        cards=[{"name": "Fixture Commander"}],
        ports=[
            {
                "card_name": "Fixture Commander",
                "port_type": "trigger",
                "event_class": "DiesThisTurn",
                "valid_filter": "",
            },
        ],
    )
    fixture = _make_fixture(tmp_path, ["Fixture Commander"])
    candidate = Candidate(signature=("trigger", "DiesThisTurn", ""), gap_id="t")
    verdict = evaluate_one(candidate, conn, fixture_path=fixture)
    assert isinstance(verdict, PipelineVerdict)
    assert verdict.severity is Severity.PASS
    assert len(verdict.gates) == 1
    assert verdict.gates[0].name == "stage_a"
    assert verdict.reason == "PASS"


def test_pipeline_reason_concatenates_non_pass_gate_reasons(tmp_path):
    """When all gates are non-PASS, .reason concatenates them with ' | '."""
    conn = _make_db(cards=[{"name": "X"}], ports=[])
    fixture = _make_fixture(tmp_path, ["X"])
    candidate = Candidate(signature=("trigger", "Nonexistent", ""), gap_id="t")
    verdict = evaluate_one(candidate, conn, fixture_path=fixture)
    assert verdict.severity is Severity.REJECT
    assert "UNTESTABLE" in verdict.reason


# ---------------------------------------------------------------------------
# Signature-to-SQL translation edge cases
# ---------------------------------------------------------------------------


def test_stage_a_matches_valid_filter_qualifier_substring(tmp_path):
    """Plain signatures with a qualifier match valid_filter LIKE '%qualifier%'."""
    conn = _make_db(
        cards=[{"name": "Fixture Commander"}],
        ports=[
            {
                "card_name": "Fixture Commander",
                "port_type": "scales_with",
                "event_class": "Valid",
                "valid_filter": "Creature.attacking",
            },
        ],
    )
    fixture = _make_fixture(tmp_path, ["Fixture Commander"])
    candidate = Candidate(
        signature=("scales_with", "Valid", "attacking"),
        gap_id="scales_with.Valid[attacking]",
    )
    verdict = stage_a_golden_coverage(candidate, conn, fixture_path=fixture)
    assert verdict.severity is Severity.PASS


def test_stage_a_replacement_signature_requires_matching_result(tmp_path):
    """Replacement signatures must match replacement_result, not just port_type+event."""
    conn = _make_db(
        cards=[{"name": "Fixture Commander"}],
        ports=[
            {
                "card_name": "Fixture Commander",
                "port_type": "replacement",
                "event_class": "DamageDone",
                "replacement_result": "DmgTwice",  # NOT "Prevent"
            },
        ],
    )
    fixture = _make_fixture(tmp_path, ["Fixture Commander"])
    candidate_prevent = Candidate(
        signature=("replacement", "DamageDone", "Prevent"),
        gap_id="replacement.DamageDone[Prevent]",
    )
    verdict = stage_a_golden_coverage(candidate_prevent, conn, fixture_path=fixture)
    # Fixture cmdr's replacement_result is DmgTwice, not Prevent — so 0 fixture hits.
    # Legal universe also has only this one cmdr with DmgTwice — so 0 legal hits for Prevent.
    assert verdict.severity is Severity.REJECT
