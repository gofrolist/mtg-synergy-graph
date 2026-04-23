"""Tests for ``forge_oracle.ranking`` — kendall_tau + forge_rank_candidates.

Plan: docs/plans/2026-04-23-002-feat-forge-second-oracle-plan.md Unit 7.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator

import pytest

from mtg_synergy_graph.forge_oracle import ranking

# ---------------------------------------------------------------------------
# kendall_tau — hand-computed reference values
# ---------------------------------------------------------------------------


def test_kendall_tau_perfect_agreement_is_one() -> None:
    pairs = [(1, 1), (2, 2), (3, 3), (4, 4)]
    assert ranking.kendall_tau(pairs) == pytest.approx(1.0)


def test_kendall_tau_perfect_disagreement_is_minus_one() -> None:
    pairs = [(1, 4), (2, 3), (3, 2), (4, 1)]
    assert ranking.kendall_tau(pairs) == pytest.approx(-1.0)


def test_kendall_tau_single_swap_is_partial() -> None:
    """Swapping one adjacent pair out of 4: 5 concordant, 1 discordant → τ = 4/6."""
    pairs = [(1, 1), (2, 3), (3, 2), (4, 4)]
    assert ranking.kendall_tau(pairs) == pytest.approx(4.0 / 6.0)


def test_kendall_tau_all_ties_on_one_side_is_zero() -> None:
    """Ties on one side → denominator collapses → defined 0.0."""
    pairs = [(1, 1), (2, 1), (3, 1), (4, 1)]
    assert ranking.kendall_tau(pairs) == pytest.approx(0.0)


def test_kendall_tau_less_than_two_is_one() -> None:
    assert ranking.kendall_tau([]) == pytest.approx(1.0)
    assert ranking.kendall_tau([(1, 1)]) == pytest.approx(1.0)


def test_kendall_tau_with_partial_ties() -> None:
    """Both sides have ties — τ-b uses the sqrt denominator correction."""
    pairs = [(1, 1), (2, 2), (3, 2), (4, 4)]
    # Compute by hand:
    # pairs (i<j): (0,1),(0,2),(0,3),(1,2),(1,3),(2,3)
    # deltas A: -1,-2,-3,-1,-2,-1
    # deltas B: -1,-1,-3, 0,-2,-2
    # (0,1): a<0, b<0 → concordant
    # (0,2): a<0, b<0 → concordant
    # (0,3): a<0, b<0 → concordant
    # (1,2): a<0, b==0 → ties_b +=1
    # (1,3): a<0, b<0 → concordant
    # (2,3): a<0, b<0 → concordant
    # → concordant=5, discordant=0, ties_a=0, ties_b=1
    # denom_a = 5+0+0 = 5, denom_b = 5+0+1 = 6
    # τ = (5-0) / sqrt(5*6) = 5 / sqrt(30) ≈ 0.9129
    import math

    expected = 5.0 / math.sqrt(30.0)
    assert ranking.kendall_tau(pairs) == pytest.approx(expected, abs=1e-9)


def test_kendall_tau_is_deterministic() -> None:
    pairs = [(1, 3), (2, 1), (3, 4), (4, 2), (5, 5)]
    values = [ranking.kendall_tau(pairs) for _ in range(5)]
    assert len(set(values)) == 1


# ---------------------------------------------------------------------------
# ranks_of helper
# ---------------------------------------------------------------------------


def test_ranks_of_returns_positional_ranks() -> None:
    assert ranking.ranks_of(["a", "b", "c"]) == {"a": 0, "b": 1, "c": 2}


def test_ranks_of_empty_returns_empty_dict() -> None:
    assert ranking.ranks_of([]) == {}


# ---------------------------------------------------------------------------
# forge_rank_candidates — scores via pair_scorer, sorts desc
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


@pytest.fixture()
def db() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    yield conn
    conn.close()


def _add_card(conn: sqlite3.Connection, *, name: str, oracle_id: str, subtypes: str = "", keywords: str = "[]") -> None:
    conn.execute(
        "INSERT INTO cards (name, oracle_id, card_types, types, subtypes, supertypes, color_identity, keywords) "
        "VALUES (?, ?, 'Creature', ?, ?, '', '', ?)",
        (name, oracle_id, f"Creature {subtypes}".strip(), subtypes, keywords),
    )


def test_forge_rank_returns_candidates_sorted_by_score(db: sqlite3.Connection) -> None:
    _add_card(db, name="Goblin Lord", oracle_id="gl1", subtypes="Goblin")
    db.execute("INSERT INTO card_hints VALUES ('Goblin Lord', 'hints', 'Type', 'Goblin')")

    _add_card(db, name="Goblin Warrior", oracle_id="gw1", subtypes="Goblin")
    _add_card(db, name="Serra Angel", oracle_id="sa1", subtypes="Angel", keywords='["Flying"]')
    _add_card(db, name="Lightning Bolt", oracle_id="lb1")
    db.execute("UPDATE cards SET card_types='Instant', types='Instant' WHERE name='Lightning Bolt'")

    ranked = ranking.forge_rank_candidates(
        "gl1",  # commander = Goblin Lord (but rate_pair is directional; here cmdr is arg a)
        ["gw1", "sa1", "lb1"],
        db,
    )
    # rate_pair(A=cand, B=cmdr=Goblin Lord):
    #   gw1 (Goblin) matches Goblin Lord's hint → score 3.
    #   sa1 (Angel) no match → 0.
    #   lb1 (Instant) no match → 0.
    # Goblin Warrior should rank first.
    assert ranked[0][0] == "gw1"
    assert ranked[0][1] == pytest.approx(3.0)
    # Ties broken by oracle_id lexical — sa1 < lb1? No, lb1 < sa1 alphabetically.
    assert [oid for oid, _ in ranked[1:]] == sorted(["sa1", "lb1"])


def test_forge_rank_empty_candidate_list_is_empty(db: sqlite3.Connection) -> None:
    _add_card(db, name="Commander", oracle_id="c1")
    assert ranking.forge_rank_candidates("c1", [], db) == []


def test_forge_rank_propagates_lookup_error_on_unknown(db: sqlite3.Connection) -> None:
    _add_card(db, name="Commander", oracle_id="c1")
    with pytest.raises(LookupError):
        ranking.forge_rank_candidates("c1", ["nonexistent"], db)
