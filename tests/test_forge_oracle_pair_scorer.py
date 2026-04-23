"""Tests for forge_oracle.pair_scorer.rate_pair.

Port of Forge CardRanker.getScoreForDeckHints at Forge SHA
ed97d9bb (see docs/spikes/2026-04-23-boosterdraft-port-feasibility.md).
Hand-computed reference scores from the Forge algorithm — no JVM
in this test suite. Reference formulas:

  score_hints = |B.DeckHints.filterByType([A])| * typeFactors[cat]
  score_needs = -(max(threshold[cat] - |A.DeckNeeds.filterByType([B])|, 0)
                  / threshold[cat]) * typeFactors[cat]
  rate_pair(A, B) = sum over hint categories + sum over need categories

  typeFactors:    {Ability: 3, Color: 1, Keyword: 3, Name: 10, Type: 3}
  typeThresholds: {Ability: 5, Color: 10, Keyword: 8, Name: 2, Type: 8}

Plan: docs/plans/2026-04-23-002-feat-forge-second-oracle-plan.md Unit 3.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator

import pytest

from mtg_synergy_graph.forge_oracle import pair_scorer

# ---------------------------------------------------------------------------
# Synthetic in-memory test DB — mirrors the subset of the real schema the
# port reads. Keeps tests hermetic (no import_cardsfolder dependency).
# ---------------------------------------------------------------------------


_SCHEMA = """
CREATE TABLE cards (
    name TEXT PRIMARY KEY,
    oracle_id TEXT,
    card_types TEXT,
    types TEXT,
    subtypes TEXT,
    supertypes TEXT,
    color_identity TEXT,
    keywords TEXT
);
CREATE TABLE card_hints (
    card_name TEXT NOT NULL,
    kind TEXT NOT NULL,
    category TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (card_name, kind, category, value)
);
"""


def _insert_card(
    conn: sqlite3.Connection,
    *,
    name: str,
    oracle_id: str,
    card_types: str = "",
    subtypes: str = "",
    supertypes: str = "",
    keywords: list[str] | None = None,
    color_identity: str = "",
) -> None:
    conn.execute(
        "INSERT INTO cards (name, oracle_id, card_types, types, subtypes, "
        "supertypes, color_identity, keywords) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            name,
            oracle_id,
            card_types,
            f"{supertypes} {card_types} {subtypes}".strip(),
            subtypes,
            supertypes,
            color_identity,
            json.dumps(keywords or []),
        ),
    )


def _insert_hint(
    conn: sqlite3.Connection,
    *,
    card_name: str,
    kind: str,
    category: str,
    value: str,
) -> None:
    conn.execute(
        "INSERT INTO card_hints (card_name, kind, category, value) VALUES (?, ?, ?, ?)",
        (card_name, kind, category, value),
    )


@pytest.fixture()
def db() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# Happy path — hint matching by each Forge DeckHints.Type category
# ---------------------------------------------------------------------------


def test_rate_pair_type_match_scores_typefactor(db: sqlite3.Connection) -> None:
    """B wants Type=Goblin. A is a Goblin. Score = 1 * Type-factor (3)."""
    _insert_card(db, name="Goblin Warrior", oracle_id="g1", card_types="Creature", subtypes="Goblin Warrior")
    _insert_card(db, name="Goblin King", oracle_id="g2", card_types="Creature", subtypes="Goblin")
    _insert_hint(db, card_name="Goblin King", kind="hints", category="Type", value="Goblin")

    assert pair_scorer.rate_pair(db, "g1", "g2") == pytest.approx(3.0)


def test_rate_pair_type_mismatch_scores_zero(db: sqlite3.Connection) -> None:
    """B wants Type=Goblin. A is an Instant. No match → 0."""
    _insert_card(db, name="Lightning Bolt", oracle_id="l1", card_types="Instant")
    _insert_card(db, name="Goblin King", oracle_id="g2", card_types="Creature", subtypes="Goblin")
    _insert_hint(db, card_name="Goblin King", kind="hints", category="Type", value="Goblin")

    assert pair_scorer.rate_pair(db, "l1", "g2") == pytest.approx(0.0)


def test_rate_pair_keyword_match_scores_typefactor(db: sqlite3.Connection) -> None:
    """B wants Keyword=Flying. A has Flying. Score = 1 * Keyword-factor (3)."""
    _insert_card(
        db,
        name="Serra Angel",
        oracle_id="a1",
        card_types="Creature",
        subtypes="Angel",
        keywords=["Flying", "Vigilance"],
    )
    _insert_card(db, name="Sky Lord", oracle_id="s1", card_types="Creature", subtypes="Bird")
    _insert_hint(db, card_name="Sky Lord", kind="hints", category="Keyword", value="Flying")

    assert pair_scorer.rate_pair(db, "a1", "s1") == pytest.approx(3.0)


def test_rate_pair_name_match_scores_typefactor(db: sqlite3.Connection) -> None:
    """B wants Name=Goblin King. A is Goblin King. Score = 1 * Name-factor (10)."""
    _insert_card(db, name="Goblin King", oracle_id="g2", card_types="Creature", subtypes="Goblin")
    _insert_card(db, name="Goblin Summoner", oracle_id="g3", card_types="Creature", subtypes="Goblin")
    _insert_hint(db, card_name="Goblin Summoner", kind="hints", category="Name", value="Goblin King")

    assert pair_scorer.rate_pair(db, "g2", "g3") == pytest.approx(10.0)


def test_rate_pair_color_match_scores_typefactor(db: sqlite3.Connection) -> None:
    """B wants Color=Red. A is red. Score = 1 * Color-factor (1)."""
    _insert_card(db, name="Shock", oracle_id="sh1", card_types="Instant", color_identity="R")
    _insert_card(db, name="Red Lord", oracle_id="rl1", card_types="Creature", color_identity="R")
    _insert_hint(db, card_name="Red Lord", kind="hints", category="Color", value="Red")

    assert pair_scorer.rate_pair(db, "sh1", "rl1") == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# DeckNeeds penalty path
# ---------------------------------------------------------------------------


def test_rate_pair_needs_unmet_full_penalty(db: sqlite3.Connection) -> None:
    """A needs Type=Zombie. B is not a Zombie. count=0. threshold=8, factor=3.

    penalty = (max(8 - 0, 0) / 8) * 3 = 3.0. Final score = -3.0.
    """
    _insert_card(db, name="Raise Dead", oracle_id="rd1", card_types="Sorcery")
    _insert_card(db, name="Serra Angel", oracle_id="a1", card_types="Creature", subtypes="Angel")
    _insert_hint(db, card_name="Raise Dead", kind="needs", category="Type", value="Zombie")

    assert pair_scorer.rate_pair(db, "rd1", "a1") == pytest.approx(-3.0)


def test_rate_pair_needs_partially_met_scaled_penalty(db: sqlite3.Connection) -> None:
    """A needs Type=Zombie. B is a Zombie. count=1, threshold=8, factor=3.

    penalty = (max(8 - 1, 0) / 8) * 3 = 21/8 = 2.625. Final score = -2.625.
    """
    _insert_card(db, name="Raise Dead", oracle_id="rd1", card_types="Sorcery")
    _insert_card(db, name="Grave Zombie", oracle_id="gz1", card_types="Creature", subtypes="Zombie")
    _insert_hint(db, card_name="Raise Dead", kind="needs", category="Type", value="Zombie")

    assert pair_scorer.rate_pair(db, "rd1", "gz1") == pytest.approx(-21.0 / 8.0)


def test_rate_pair_needs_threshold_met_zero_penalty(db: sqlite3.Connection) -> None:
    """A needs Name=Goblin King (threshold=2). B is Goblin King. count=1, threshold=2.

    penalty = (max(2 - 1, 0) / 2) * 10 = 5.0. Final score = -5.0.
    """
    _insert_card(db, name="Goblin Summoner", oracle_id="g3", card_types="Creature", subtypes="Goblin")
    _insert_card(db, name="Goblin King", oracle_id="g2", card_types="Creature", subtypes="Goblin")
    _insert_hint(db, card_name="Goblin Summoner", kind="needs", category="Name", value="Goblin King")

    assert pair_scorer.rate_pair(db, "g3", "g2") == pytest.approx(-5.0)


# ---------------------------------------------------------------------------
# Hints + needs combined
# ---------------------------------------------------------------------------


def test_rate_pair_combined_hint_and_need(db: sqlite3.Connection) -> None:
    """A has Keyword=Flying AND deck_needs Type=Bird. B wants Keyword=Flying.

    Hint: +3 (B pulls in A's Flying).
    Need: B is a Creature Bird → count=1, threshold=8, factor=3
          → penalty = (7/8) * 3 = 2.625. Final = 3.0 - 2.625 = 0.375.
    """
    _insert_card(db, name="Serra Angel", oracle_id="a1", card_types="Creature", subtypes="Angel", keywords=["Flying"])
    _insert_card(db, name="Bird Rider", oracle_id="br1", card_types="Creature", subtypes="Bird")
    _insert_hint(db, card_name="Bird Rider", kind="hints", category="Keyword", value="Flying")
    _insert_hint(db, card_name="Serra Angel", kind="needs", category="Type", value="Bird")

    assert pair_scorer.rate_pair(db, "a1", "br1") == pytest.approx(3.0 - 21.0 / 8.0)


# ---------------------------------------------------------------------------
# filterByType intersection semantics (Forge: retainAll when category repeats)
# ---------------------------------------------------------------------------


def test_rate_pair_repeated_category_intersects(db: sqlite3.Connection) -> None:
    """B has two Keyword hints (Flying AND Haste). A has Flying only.

    Per Forge filterByType: when a category appears twice, intersect matches.
    A matches Flying but not Haste → intersection empty → 0 contribution.
    """
    _insert_card(db, name="Serra Angel", oracle_id="a1", card_types="Creature", keywords=["Flying"])
    _insert_card(db, name="Picky Lord", oracle_id="pl1", card_types="Creature")
    _insert_hint(db, card_name="Picky Lord", kind="hints", category="Keyword", value="Flying")
    _insert_hint(db, card_name="Picky Lord", kind="hints", category="Keyword", value="Haste")

    assert pair_scorer.rate_pair(db, "a1", "pl1") == pytest.approx(0.0)


def test_rate_pair_repeated_category_both_match(db: sqlite3.Connection) -> None:
    """B wants Keyword Flying AND Haste. A has both. Intersection = {A}, size 1.

    Score = 1 * Keyword-factor (3) = 3.0 (not 2*3 — intersection wins, not union).
    """
    _insert_card(db, name="Akroma", oracle_id="ak1", card_types="Creature", keywords=["Flying", "Haste", "Vigilance"])
    _insert_card(db, name="Picky Lord", oracle_id="pl1", card_types="Creature")
    _insert_hint(db, card_name="Picky Lord", kind="hints", category="Keyword", value="Flying")
    _insert_hint(db, card_name="Picky Lord", kind="hints", category="Keyword", value="Haste")

    assert pair_scorer.rate_pair(db, "ak1", "pl1") == pytest.approx(3.0)


# ---------------------------------------------------------------------------
# Determinism + symmetry documentation
# ---------------------------------------------------------------------------


def test_rate_pair_is_deterministic(db: sqlite3.Connection) -> None:
    """Same inputs → bitwise-identical output across repeated calls."""
    _insert_card(db, name="Goblin Warrior", oracle_id="g1", card_types="Creature", subtypes="Goblin")
    _insert_card(db, name="Goblin King", oracle_id="g2", card_types="Creature", subtypes="Goblin")
    _insert_hint(db, card_name="Goblin King", kind="hints", category="Type", value="Goblin")

    scores = [pair_scorer.rate_pair(db, "g1", "g2") for _ in range(5)]
    assert len(set(scores)) == 1


def test_rate_pair_asymmetric_when_hints_differ(db: sqlite3.Connection) -> None:
    """rate_pair(a, b) != rate_pair(b, a) in general.

    A has no hints/needs. B wants Type=Goblin, A is Goblin.
    rate_pair(A, B) = 3 (B wants A).
    rate_pair(B, A) = 0 (A has no hints, no needs).
    Document the directional contract via test.
    """
    _insert_card(db, name="Goblin Warrior", oracle_id="g1", card_types="Creature", subtypes="Goblin")
    _insert_card(db, name="Goblin King", oracle_id="g2", card_types="Creature", subtypes="Goblin")
    _insert_hint(db, card_name="Goblin King", kind="hints", category="Type", value="Goblin")

    assert pair_scorer.rate_pair(db, "g1", "g2") == pytest.approx(3.0)
    assert pair_scorer.rate_pair(db, "g2", "g1") == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_rate_pair_no_hints_anywhere_is_zero(db: sqlite3.Connection) -> None:
    """Neither card has hints/needs → score 0."""
    _insert_card(db, name="Sol Ring", oracle_id="sr1", card_types="Artifact")
    _insert_card(db, name="Lightning Bolt", oracle_id="l1", card_types="Instant")

    assert pair_scorer.rate_pair(db, "sr1", "l1") == pytest.approx(0.0)


def test_rate_pair_self_pair_defined(db: sqlite3.Connection) -> None:
    """rate_pair(a, a) must not crash. B's hints filtered against [A=B] and A's
    needs against [B=A] both yield deterministic numbers."""
    _insert_card(db, name="Goblin King", oracle_id="g2", card_types="Creature", subtypes="Goblin")
    _insert_hint(db, card_name="Goblin King", kind="hints", category="Type", value="Goblin")
    # B (= A = Goblin King) wants Type=Goblin. A (= Goblin King) matches. score += 3.
    # A has no needs. No penalty.
    assert pair_scorer.rate_pair(db, "g2", "g2") == pytest.approx(3.0)


def test_rate_pair_unknown_oracle_id_raises_lookup(db: sqlite3.Connection) -> None:
    _insert_card(db, name="Goblin King", oracle_id="g2", card_types="Creature", subtypes="Goblin")
    with pytest.raises(LookupError, match="unknown"):
        pair_scorer.rate_pair(db, "nonexistent", "g2")
    with pytest.raises(LookupError, match="unknown"):
        pair_scorer.rate_pair(db, "g2", "nonexistent")


def test_rate_pair_rejects_none_conn() -> None:
    with pytest.raises(ValueError, match="conn"):
        pair_scorer.rate_pair(None, "g1", "g2")  # type: ignore[arg-type]


def test_rate_pair_type_match_case_insensitive(db: sqlite3.Connection) -> None:
    """Forge joinedType uses CONTAINS_IC (case-insensitive) — a card typed
    'Creature' matches a hint for Type=creature."""
    _insert_card(db, name="Serra Angel", oracle_id="a1", card_types="Creature", subtypes="Angel")
    _insert_card(db, name="Creature Lover", oracle_id="cl1", card_types="Enchantment")
    _insert_hint(db, card_name="Creature Lover", kind="hints", category="Type", value="creature")

    assert pair_scorer.rate_pair(db, "a1", "cl1") == pytest.approx(3.0)
