"""Tests for the Phase F2 deck_hints combo bonus.

Path B follow-up to the edhrec_rank tiebreaker (Path A). The combo
bonus adds :data:`DECKHINTS_COMBO_WEIGHT` once per candidate that
accumulates BOTH a forward (``hints``/``needs``) and a reverse
(``has_has``/``wants_has``) source match. It is a pure additive
bonus — existing scores on single-source cards are untouched.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mtg_synergy_graph import SynergyEngine
from mtg_synergy_graph.scoring import (
    DECKHINTS_COMBO_WEIGHT,
    DECKHINTS_REVERSE_WEIGHT,
    DECKHINTS_WEIGHT,
    _score_deckhints,
    empty_buckets,
)

FULL_DB_PATH = Path("/tmp/synergy_full.db")

pytestmark_full_db = pytest.mark.skipif(
    not FULL_DB_PATH.exists(),
    reason="end-to-end Phase F2 tests require /tmp/synergy_full.db",
)


# ---------------------------------------------------------------------------
# Unit tests against a fake find_deckhints_matches return value
# ---------------------------------------------------------------------------


class _StubRows:
    """Monkey-patch target for scoring._score_deckhints so we can drive
    it with a handwritten row stream — no DB required."""

    def __init__(self, rows):
        self._rows = rows

    def __call__(self, *args, **kwargs):
        return self._rows


def _run_deckhints(rows, monkeypatch):
    """Invoke :func:`_score_deckhints` against a synthetic row stream.
    Returns ``(scores, matches)`` for inspection."""
    monkeypatch.setattr(
        "mtg_synergy_graph.scoring.find_deckhints_matches",
        _StubRows(rows),
    )
    scores: dict = {}
    matches: dict = {}

    def _ensure(name: str) -> None:
        if name not in scores:
            scores[name] = empty_buckets()
            matches[name] = []

    # Wrap the real _add_bucket-using function: pre-initialise entries
    # for every candidate that will appear in the row stream.
    for row in rows:
        _ensure(row["candidate"])

    _score_deckhints(None, ["Dummy"], scores, matches)  # conn unused here
    return scores, matches


def test_forward_only_single_source(monkeypatch):
    rows = [
        {"candidate": "Card1", "source": "hints", "kind": "Ability",
         "matched": ["Counters"]},
    ]
    scores, _ = _run_deckhints(rows, monkeypatch)
    assert scores["Card1"]["deck_hints"] == DECKHINTS_WEIGHT
    assert scores["Card1"]["deck_hints"] == 4


def test_reverse_only_single_source(monkeypatch):
    rows = [
        {"candidate": "Card1", "source": "has_has", "kind": "Ability",
         "matched": ["Token"]},
    ]
    scores, _ = _run_deckhints(rows, monkeypatch)
    assert scores["Card1"]["deck_hints"] == DECKHINTS_REVERSE_WEIGHT
    assert scores["Card1"]["deck_hints"] == 2


def test_two_reverse_sources_no_combo(monkeypatch):
    """Two reverse-only hits don't qualify as a combo — forward is
    required for the bonus to fire."""
    rows = [
        {"candidate": "Card1", "source": "has_has", "kind": "Ability",
         "matched": ["Counters"]},
        {"candidate": "Card1", "source": "wants_has", "kind": "Type",
         "matched": ["Human"]},
    ]
    scores, _ = _run_deckhints(rows, monkeypatch)
    assert scores["Card1"]["deck_hints"] == 2 + 2
    # Explicitly: no combo bonus.
    assert scores["Card1"]["deck_hints"] == 4, "Should NOT fire combo"


def test_two_forward_sources_no_combo(monkeypatch):
    """Two forward-only hits don't qualify either — reverse is required."""
    rows = [
        {"candidate": "Card1", "source": "hints", "kind": "Ability",
         "matched": ["A"]},
        {"candidate": "Card1", "source": "needs", "kind": "Ability",
         "matched": ["B"]},
    ]
    scores, _ = _run_deckhints(rows, monkeypatch)
    assert scores["Card1"]["deck_hints"] == 4 + 4
    assert scores["Card1"]["deck_hints"] == 8, "Should NOT fire combo"


def test_combo_fires_on_forward_plus_reverse(monkeypatch):
    """The 2-source forward+reverse path is the minimum trigger for
    the combo bonus."""
    rows = [
        {"candidate": "Card1", "source": "hints", "kind": "Ability",
         "matched": ["Counters"]},
        {"candidate": "Card1", "source": "has_has", "kind": "Ability",
         "matched": ["Counters"]},
    ]
    scores, _ = _run_deckhints(rows, monkeypatch)
    expected = DECKHINTS_WEIGHT + DECKHINTS_REVERSE_WEIGHT + DECKHINTS_COMBO_WEIGHT
    assert scores["Card1"]["deck_hints"] == expected
    assert scores["Card1"]["deck_hints"] == 8


def test_combo_fires_once_only_on_three_source_full_match(monkeypatch):
    """Fat 3-source cards (Heronblade Elite class) must get the combo
    bonus exactly once, not twice or three times."""
    rows = [
        {"candidate": "Card1", "source": "hints", "kind": "Ability",
         "matched": ["Counters"]},
        {"candidate": "Card1", "source": "has_has", "kind": "Ability",
         "matched": ["Counters"]},
        {"candidate": "Card1", "source": "wants_has", "kind": "Type",
         "matched": ["Human"]},
    ]
    scores, _ = _run_deckhints(rows, monkeypatch)
    expected = (
        DECKHINTS_WEIGHT            # hints      = 4
        + DECKHINTS_REVERSE_WEIGHT  # has_has    = 2
        + DECKHINTS_REVERSE_WEIGHT  # wants_has  = 2
        + DECKHINTS_COMBO_WEIGHT    # combo      = 2 (once)
    )
    assert scores["Card1"]["deck_hints"] == expected
    assert scores["Card1"]["deck_hints"] == 10


def test_combo_is_per_candidate(monkeypatch):
    """Combo bookkeeping must not bleed across candidates."""
    rows = [
        # Card A: forward only → no combo
        {"candidate": "CardA", "source": "hints", "kind": "Ability",
         "matched": ["A"]},
        # Card B: forward + reverse → combo fires
        {"candidate": "CardB", "source": "hints", "kind": "Ability",
         "matched": ["B"]},
        {"candidate": "CardB", "source": "has_has", "kind": "Ability",
         "matched": ["B"]},
        # Card C: reverse only → no combo
        {"candidate": "CardC", "source": "has_has", "kind": "Ability",
         "matched": ["C"]},
    ]
    scores, _ = _run_deckhints(rows, monkeypatch)
    assert scores["CardA"]["deck_hints"] == 4
    assert scores["CardB"]["deck_hints"] == 4 + 2 + 2
    assert scores["CardC"]["deck_hints"] == 2


def test_duplicate_source_rows_still_trigger_combo_only_once(monkeypatch):
    """Source dedupe (inherited from the base scorer) must still work
    when combined with the combo bonus."""
    rows = [
        {"candidate": "Card1", "source": "hints", "kind": "Ability",
         "matched": ["A"]},
        # Duplicate hints row — ignored by source-dedupe.
        {"candidate": "Card1", "source": "hints", "kind": "Ability",
         "matched": ["B"]},
        {"candidate": "Card1", "source": "has_has", "kind": "Ability",
         "matched": ["A"]},
        # Duplicate has_has row — ignored.
        {"candidate": "Card1", "source": "has_has", "kind": "Type",
         "matched": ["Human"]},
    ]
    scores, _ = _run_deckhints(rows, monkeypatch)
    # hints=4 + has_has=2 + combo=2 — duplicates collapsed.
    assert scores["Card1"]["deck_hints"] == 8


# ---------------------------------------------------------------------------
# End-to-end sanity on the real Forge DB (Kyler regression)
# ---------------------------------------------------------------------------


@pytestmark_full_db
def test_heronblade_elite_beats_maester_seymour_for_kyler():
    """Regression guard for the concrete case that motivated Path B.

    Heronblade Elite is EDHREC's #1 Hi-Syn card for Kyler (synergy
    0.79). Maester Seymour is a mid-tier counter producer. The combo
    bonus must rank Heronblade ABOVE Seymour.
    """
    with SynergyEngine(FULL_DB_PATH) as eng:
        page = eng.page("Kyler, Sigardian Emissary", limit=150)
    names = [r.card for r in page.items]
    assert "Heronblade Elite" in names
    assert "Maester Seymour" in names
    heron_rank = names.index("Heronblade Elite")
    seymour_rank = names.index("Maester Seymour")
    assert heron_rank < seymour_rank, (
        f"Heronblade Elite at index {heron_rank} should rank above "
        f"Maester Seymour at index {seymour_rank} — the combo bonus "
        "is not firing or is being undercounted."
    )


@pytestmark_full_db
def test_kyler_fat_deckhints_card_beats_single_source_peer_at_same_port_match():
    """Pair-wise comparison holding port_match + counter_synergy
    fixed. Heronblade Elite (fat, 3-source deckhints, no
    counter_synergy) must beat Maester Seymour (1-source deckhints +
    counter_synergy), because after Phase F2 the combo-bonus path
    (10 deck_hints) strictly beats the counter_synergy+1-source path
    (4 + 4 = 8 combined).
    """
    with SynergyEngine(FULL_DB_PATH) as eng:
        heron = eng.score_one("Kyler, Sigardian Emissary", "Heronblade Elite")
        seymour = eng.score_one("Kyler, Sigardian Emissary", "Maester Seymour")

    # Both should share port_match=10 (single trigger feed each) so
    # the remaining buckets are the clean comparison.
    assert heron.scores["port_match"] == seymour.scores["port_match"], (
        "Test prerequisite violated: port_match differs between the "
        "two cards; pick another pair that shares port_match."
    )
    assert heron.total_score > seymour.total_score, (
        f"Heronblade {heron.total_score} must beat Seymour "
        f"{seymour.total_score}: combo bonus is undercounted."
    )
