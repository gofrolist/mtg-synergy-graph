"""Pair-synergy scorer — port of Forge ``CardRanker.getScoreForDeckHints``.

Source (Forge SHA ed97d9bb):
- ``forge-gui/src/main/java/forge/gamemodes/limited/CardRanker.java`` lines
  175-206 (the scoring math) and 19-35 (the constants).
- ``forge-core/src/main/java/forge/card/DeckHints.java`` lines 125-196
  (``filterByType`` + ``getCardsForFilter`` predicate dispatch).

Data inputs are the local ``cards`` + ``card_hints`` tables — our
importer already decomposes Forge's ``DeckHints: Type$Goblin & ...``
SVar strings into normalized ``(card_name, kind, category, value)``
rows per ``src/mtg_synergy_graph/importer.py``, so this port never
touches the raw SVar format.

Scope discipline: the Unit 2 spike confirmed this port does not
require Forge's ``GameState``, ``PlayerAI``, or runtime ``Card`` class.
Everything it needs is already in our DB. See
``docs/spikes/2026-04-23-boosterdraft-port-feasibility.md``.

This module is offline infrastructure. It MUST NOT be imported from
``engine.py``, ``universal_scorer.py``, ``graph_engine.py``,
``complement_rules/*``, or ``scripts/recommend.py``. Enforced by
``tests/test_forge_oracle_isolation.py`` (plan Unit 9).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Literal

#: Forge CardRanker.java:22-28 — multiplier per hint category.
_TYPE_FACTORS: dict[str, int] = {
    "Ability": 3,
    "Color": 1,
    "Keyword": 3,
    "Name": 10,
    "Type": 3,
}

#: Forge CardRanker.java:29-35 — threshold for "deck-needs satisfied."
_TYPE_THRESHOLDS: dict[str, int] = {
    "Ability": 5,
    "Color": 10,
    "Keyword": 8,
    "Name": 2,
    "Type": 8,
}

#: Forge color-name → mana-color letter mapping. Forge's ``ColorSet.fromNames``
#: accepts the color words ("white", "blue", "black", "red", "green") and the
#: single-letter codes ("w", "u", "b", "r", "g"). Case-insensitive.
_COLOR_WORD_TO_LETTER: dict[str, str] = {
    "white": "W",
    "blue": "U",
    "black": "B",
    "red": "R",
    "green": "G",
    "w": "W",
    "u": "U",
    "b": "B",
    "r": "R",
    "g": "G",
}


@dataclass(frozen=True, slots=True)
class _CardView:
    """Projection of ``cards`` + ``card_hints(kind='has')`` rows used by
    the hint-predicate dispatch. Held immutable; one instance per
    ``(conn, oracle_id)`` lookup within a single ``rate_pair`` call.
    """

    name: str
    joined_type_lower: str  # lowercase "supertypes card_types subtypes" for CONTAINS_IC
    color_identity_letters: frozenset[str]  # {'W','U','B','R','G'} projection
    keywords: frozenset[str]
    has_abilities: frozenset[str] = field(default_factory=frozenset)
    has_types: frozenset[str] = field(default_factory=frozenset)
    has_keywords: frozenset[str] = field(default_factory=frozenset)


def _fetch_card_view(conn: sqlite3.Connection, oracle_id: str) -> _CardView:
    """Load one card by ``oracle_id``. Raises ``LookupError`` if unknown.

    ``cards.oracle_id`` is nullable in our schema, but the forge-oracle
    pipeline only queries cards that came through Scryfall resolution —
    callers must pass a non-empty oracle_id.
    """
    row = conn.execute(
        "SELECT name, card_types, subtypes, supertypes, color_identity, keywords "
        "FROM cards WHERE oracle_id = ? LIMIT 1",
        (oracle_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"unknown oracle_id: {oracle_id!r}")
    name, card_types, subtypes, supertypes, color_identity, keywords_json = (
        row["name"],
        row["card_types"] or "",
        row["subtypes"] or "",
        row["supertypes"] or "",
        row["color_identity"] or "",
        row["keywords"] or "[]",
    )
    joined_type_lower = f"{supertypes} {card_types} {subtypes}".strip().lower()
    color_letters = frozenset(part.strip().upper() for part in color_identity.split(",") if part.strip())
    try:
        kw_list = json.loads(keywords_json)
    except json.JSONDecodeError:
        kw_list = []
    keywords = frozenset(kw_list)

    # DeckHas supplements type/keyword info — e.g., a token generator might
    # declare ``DeckHas: Ability$Token``. Load the card's own 'has' rows.
    has_abilities: set[str] = set()
    has_types: set[str] = set()
    has_keywords: set[str] = set()
    for cat, val in conn.execute(
        "SELECT category, value FROM card_hints WHERE card_name = ? AND kind = 'has'",
        (name,),
    ).fetchall():
        if cat == "Ability":
            has_abilities.add(val)
        elif cat == "Type":
            has_types.add(val)
        elif cat == "Keyword":
            has_keywords.add(val)

    return _CardView(
        name=name,
        joined_type_lower=joined_type_lower,
        color_identity_letters=color_letters,
        keywords=keywords,
        has_abilities=frozenset(has_abilities),
        has_types=frozenset(has_types),
        has_keywords=frozenset(has_keywords),
    )


def _fetch_hint_rows(
    conn: sqlite3.Connection,
    card_name: str,
    kind: Literal["hints", "needs"],
) -> list[tuple[str, str]]:
    """Return ``(category, value)`` pairs from ``card_hints`` by kind.

    Rows are returned in ``PRIMARY KEY`` sort order (category, value) so
    the intersection semantics of ``_apply_hints`` are deterministic.
    """
    return [
        (row[0], row[1])
        for row in conn.execute(
            "SELECT category, value FROM card_hints WHERE card_name = ? AND kind = ? ORDER BY category, value",
            (card_name, kind),
        ).fetchall()
    ]


def _card_matches_hint(view: _CardView, category: str, value: str) -> bool:
    """Port of ``DeckHints.getCardsForFilter`` predicate dispatch."""
    if category == "Name":
        return view.name == value
    if category == "Keyword":
        return value in view.keywords or value in view.has_keywords
    if category == "Type":
        # Forge: ``joinedType(CONTAINS_IC, p)`` — the card's joined type
        # line (lowercased) must contain ``p`` (lowercased). DeckHas-declared
        # types also qualify (e.g., token generators declaring ``Type$Spirit``).
        needle = value.lower()
        if needle in view.joined_type_lower:
            return True
        return value in view.has_types
    if category == "Color":
        letter = _COLOR_WORD_TO_LETTER.get(value.strip().lower())
        if letter is None:
            return False
        return letter in view.color_identity_letters
    if category == "Ability":
        # Forge: ``CardRulesPredicates.deckHas(ABILITY, ability)`` — ability
        # matches if the card's ``DeckHas: Ability$...`` declares it.
        return value in view.has_abilities
    return False


def _apply_hints(
    hint_rows: list[tuple[str, str]],
    targets: list[_CardView],
    *,
    positive: bool,
) -> float:
    """Port of ``DeckHints.filterByType`` + the surrounding scoring loop
    in ``CardRanker.getScoreForDeckHints``.

    Forge semantics: when one hint category appears more than once,
    ``filterByType`` intersects the match sets across the duplicates
    (``cards.retainAll``). We mirror that with a per-category set of
    target indices, intersected across duplicates.

    positive=True  (``hints``): score += |matches| * factor  for each category
    positive=False (``needs``): score -= (max(threshold - |matches|, 0) / threshold) * factor
    """
    # Early returns — ordered for clarity, not performance (plan 002 code-review
    # finding #10: the prior shape had a silent fallthrough that was
    # correct-by-accident).
    if not hint_rows:
        return 0.0
    if not targets:
        # No targets: hints can't match anyone; needs are fully short on every
        # category. The per-category loop below happens to produce the same
        # numbers when targets is [], but we prefer an explicit short-circuit.
        if positive:
            return 0.0
        score = 0.0
        seen_cats: set[str] = set()
        for cat, _val in hint_rows:
            if cat in seen_cats:
                continue  # Same intersection-vs-union handling as below (empty ∩ empty = empty).
            seen_cats.add(cat)
            factor = _TYPE_FACTORS.get(cat, 0)
            threshold = _TYPE_THRESHOLDS.get(cat, 0)
            if factor == 0 or threshold == 0:
                continue
            # shortfall = threshold - 0 = threshold; penalty = factor.
            score -= factor
        return score

    matches_by_cat: dict[str, set[int]] = {}
    for cat, val in hint_rows:
        matches = {i for i, tgt in enumerate(targets) if _card_matches_hint(tgt, cat, val)}
        if cat in matches_by_cat:
            matches_by_cat[cat] &= matches  # Forge: retainAll
        else:
            matches_by_cat[cat] = matches

    score = 0.0
    for cat, idxs in matches_by_cat.items():
        count = len(idxs)
        factor = _TYPE_FACTORS.get(cat, 0)
        if factor == 0:
            continue
        if positive:
            score += count * factor
        else:
            threshold = _TYPE_THRESHOLDS.get(cat, 0)
            if threshold > 0:
                shortfall = max(threshold - count, 0)
                score -= (shortfall / threshold) * factor
    return score


def rate_pair(
    conn: sqlite3.Connection,
    a_oracle_id: str,
    b_oracle_id: str,
) -> float:
    """Directional pair-synergy score: "how much does B want A, minus
    how much A's own needs are unmet by B."

    Matches Forge's ``CardRanker.getScoreForDeckHints(A, [B])`` exactly.
    Returns a float; the Forge scale is arbitrary (roughly -100..+100
    depending on need thresholds and hint counts). Downstream consumers
    (``--vs-forge-oracle`` Kendall-τ, ``gap_report.py`` normalization)
    are rank-preserving, so the absolute scale does not affect
    correctness.

    Raises:
      - ``ValueError`` if ``conn`` is None.
      - ``LookupError`` if either oracle_id is unknown in ``cards``.
    """
    if conn is None:
        raise ValueError("conn cannot be None")

    a = _fetch_card_view(conn, a_oracle_id)
    b = _fetch_card_view(conn, b_oracle_id)

    score = 0.0

    # "Does B want A?" — B's hints filtered against [A].
    b_hints = _fetch_hint_rows(conn, b.name, "hints")
    score += _apply_hints(b_hints, [a], positive=True)

    # "Are A's needs met by [B]?" — penalty when not.
    a_needs = _fetch_hint_rows(conn, a.name, "needs")
    score += _apply_hints(a_needs, [b], positive=False)

    return score
