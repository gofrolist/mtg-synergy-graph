"""Tests for the Phase F5 graveyard_synergy bucket.

Gate discrimination (creature-reanimator vs artifact-only / lands-only),
candidate tier coverage (library_to_grave / self_mill / reanimator_cluster),
and the concrete lift for Karador and Muldrotha that motivated F5.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mtg_synergy_graph.graph_engine import (
    _commander_is_graveyard_value,
    find_graveyard_value_synergies,
    load_ports_for_set,
)

pytestmark = pytest.mark.skipif(
    not Path("/tmp/synergy_full.db").exists(),
    reason="Phase F5 tests require /tmp/synergy_full.db",
)


# ---------------------------------------------------------------------------
# Gate discrimination
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("commander", [
    "Meren of Clan Nel Toth",       # effect ChangeZone Grave→BF creature
    "Karador, Ghost Chieftain",     # scales_with ValidGraveyard + MayPlay
    "Chainer, Nightmare Adept",     # STYardCast substring
    "Gisa and Geralf",              # MayPlay + AffectedZone: Graveyard
    "Muldrotha, the Gravetide",     # cast any permanent type from grave
    "The Mimeoplasm",               # scales_with ValidGraveyard Creature
])
def test_gate_fires_for_creature_reanimator_commanders(full_conn, commander):
    ports = load_ports_for_set(full_conn, [commander])
    assert _commander_is_graveyard_value(ports), (
        f"{commander} is a creature-reanimator commander but the "
        f"graveyard gate rejected it — verify its ports carry one of "
        f"the four expected signatures."
    )


@pytest.mark.parametrize("commander", [
    "Korvold, Fae-Cursed King",      # sacrifice, not graveyard cast
    "Atraxa, Praetors' Voice",       # proliferate, unrelated
    "Krenko, Mob Boss",              # goblin tribal
    "Edgar Markov",                  # vampire tribal
    "Phenax, God of Deception",      # opponent mill, not self-grave
    "Sharuum the Hegemon",           # ARTIFACT reanimator (filter tightening)
    "Lord Windgrace",                # LAND recursion, not creature reanim
    "The Gitrog Monster",            # graveyard-cares but no reanim
])
def test_gate_rejects_non_creature_reanimator(full_conn, commander):
    """Regression guard. The F5 bucket is narrowly scoped to creature-
    reanimator commanders. Sharuum (artifact reanim) and Windgrace
    (land recursion) both trip an ``effect ChangeZone Graveyard→
    Battlefield`` port, but their valid_filter scopes to Artifact /
    Land. The tightened signature-1 check must exclude them."""
    ports = load_ports_for_set(full_conn, [commander])
    assert not _commander_is_graveyard_value(ports), (
        f"{commander} tripped the graveyard gate but shouldn't — "
        f"check _commander_is_graveyard_value's filter-scope rules "
        f"for signature 1."
    )


# ---------------------------------------------------------------------------
# Matcher tiers
# ---------------------------------------------------------------------------


def test_meren_emits_library_to_grave_tier(full_conn):
    """Buried Alive / Entomb / Gravebreaker Lamia are the tier-1 Library→
    Graveyard direct placement cluster. They must be emitted for
    creature-reanimator commanders."""
    rows = find_graveyard_value_synergies(full_conn, ["Meren of Clan Nel Toth"])
    names = {r["candidate"]: r["direction"] for r in rows}
    assert "Buried Alive" in names
    assert names["Buried Alive"] == "library_to_grave"


def test_meren_emits_self_mill_tier(full_conn):
    rows = find_graveyard_value_synergies(full_conn, ["Meren of Clan Nel Toth"])
    names = {r["candidate"]: r["direction"] for r in rows}
    assert "Stitcher's Supplier" in names
    assert names["Stitcher's Supplier"] == "self_mill"


def test_meren_emits_reanimator_cluster_tier(full_conn):
    """Doomed Necromancer and Reassembling Skeleton both have
    effect ChangeZone Graveyard→Battlefield and must cluster with
    Meren as payoff-for-payoff mates."""
    rows = find_graveyard_value_synergies(full_conn, ["Meren of Clan Nel Toth"])
    names = {r["candidate"]: r["direction"] for r in rows}
    assert "Doomed Necromancer" in names
    assert "Reassembling Skeleton" in names


def test_non_gate_commander_gets_empty(full_conn):
    rows = find_graveyard_value_synergies(full_conn, ["Korvold, Fae-Cursed King"])
    assert rows == []


def test_commander_not_emitted_as_candidate(full_conn):
    rows = find_graveyard_value_synergies(full_conn, ["Meren of Clan Nel Toth"])
    names = {r["candidate"] for r in rows}
    assert "Meren of Clan Nel Toth" not in names


def test_opponent_mill_effects_not_emitted(full_conn):
    """Mill effects targeting opponents (Bruvac class) must NOT be
    emitted as ``self_mill`` candidates — they fill the opponent's
    graveyard, not yours."""
    rows = find_graveyard_value_synergies(full_conn, ["Meren of Clan Nel Toth"])
    names = {r["candidate"] for r in rows}
    assert "Bruvac the Grandiloquent" not in names


# ---------------------------------------------------------------------------
# Scoring end-to-end
# ---------------------------------------------------------------------------


def test_karador_nails_reassembling_skeleton(full_engine):
    """Reassembling Skeleton self-reanimates each turn, so both Karador
    and Reassembling Skeleton fire Graveyard→Battlefield effects. The
    cluster synergy must be captured by the reanimator_cluster tier."""
    rec = full_engine.score_one(
        "Karador, Ghost Chieftain", "Reassembling Skeleton",
    )
    assert rec.scores.get("graveyard_synergy", 0) > 0


def test_chainer_scores_buried_alive(full_engine):
    """Buried Alive is Chainer's #4 EDHREC Hi-Syn pick. Even though
    F5 weight=4 isn't enough to lift it into the top 30 (it has no
    port_match signal as a sorcery), the bucket must at least fire."""
    rec = full_engine.score_one(
        "Chainer, Nightmare Adept", "Buried Alive",
    )
    assert rec.scores.get("graveyard_synergy", 0) > 0


def test_krenko_does_not_score_graveyard_synergy(full_engine):
    """Krenko is a tribal go-wide commander with no graveyard mechanic.
    Buried Alive must NOT fire graveyard_synergy for him."""
    rec = full_engine.score_one("Krenko, Mob Boss", "Buried Alive")
    assert rec.scores.get("graveyard_synergy", 0) == 0


def test_karador_target_lift_is_substantial(full_engine):
    """The headline F5 result: Karador's NDCG@30 was 0.002 in the F4
    baseline (essentially zero). F5 must lift it above 0.03 via the
    combination of library_to_grave + self_mill + reanimator_cluster
    picks reshuffling into the top 30."""
    import sqlite3
    from mtg_synergy_graph.validate import (
        compute_ndcg, edhrec_labels_for_commander,
    )
    tags = sqlite3.connect("data/tags.db")
    tags.row_factory = sqlite3.Row
    try:
        labels = edhrec_labels_for_commander(tags, "Karador, Ghost Chieftain")
        page = full_engine.page("Karador, Ghost Chieftain", limit=30)
        ranking = [r.card for r in page.items]
        ndcg = compute_ndcg(ranking, labels, k=30)
    finally:
        tags.close()
    assert ndcg > 0.03, (
        f"Karador NDCG@30 = {ndcg:.4f} after F5 — expected > 0.03. "
        f"If this drops, re-check the gate and tier weights."
    )
