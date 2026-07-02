"""Tests for the ``_ENABLE_PATHWAY_RULES`` feature flag wiring.

Unit 3 of plan ``docs/plans/2026-04-23-001-feat-self-bridging-cascade-pathway-plan.md``.
Verifies that with the flag off (default), ``find_all_complements``
emits zero ``self_bridging_cascade`` complements, and with the flag
patched on it does. Also asserts the rule is NOT in
``DECLARATIVE_RULE_IDS`` and that the config hash is stable.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from mtg_synergy_graph.bench.tensor import compute_config_hash
from mtg_synergy_graph.complement_rules import pathway
from mtg_synergy_graph.complement_rules.core import find_all_complements
from mtg_synergy_graph.complement_rules.registry import CARD_LEVEL_RULES, DECLARATIVE_RULE_IDS
from mtg_synergy_graph.db import open_db
from mtg_synergy_graph.universal_scorer import _RULE_TO_BUCKET

# ---------------------------------------------------------------------------
# In-memory DB setup — a Korvold-shape commander + Gravecrawler candidate.
# ---------------------------------------------------------------------------


@pytest.fixture()
def mini_db(tmp_path: Path) -> sqlite3.Connection:
    """Production-schema DB (via ``open_db``) seeded with a Korvold
    commander + Gravecrawler cascade-candidate. Uses the real schema
    so ``find_all_complements`` can execute its full column query."""
    conn = open_db(tmp_path / "synergy.db")
    conn.execute(
        "INSERT INTO cards (name, card_types, types, subtypes, supertypes, "
        "keywords, color_identity, cmc, edhrec_rank, oracle_id, legal_commander) "
        "VALUES ('Korvold', 'Creature', 'Legendary Creature Dragon Noble', "
        "'Dragon Noble', 'Legendary', '', 'BRG', 5, NULL, NULL, 1)"
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
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Flag default state
# ---------------------------------------------------------------------------


def test_flag_default_is_true() -> None:
    """The default flag state is ``True`` as of the 2026-04-23 audit
    landing (verdict: positive, hidden_gem_hit_rate +0.1136 vs baseline
    at flag=False). See ``docs/RULE_HISTORY.md`` for the audit detail."""
    assert pathway._ENABLE_PATHWAY_RULES is True


# ---------------------------------------------------------------------------
# End-to-end: find_all_complements respects the flag
# ---------------------------------------------------------------------------


def test_find_all_complements_emits_nothing_when_flag_patched_off(
    mini_db: sqlite3.Connection,
) -> None:
    """With the flag patched off, find_all_complements must not emit
    any self_bridging_cascade complements -- the guarded out.extend()
    is skipped. Regression guard for the flag toggle itself."""
    with patch.object(pathway, "_ENABLE_PATHWAY_RULES", False):
        complements = find_all_complements(mini_db, ["Korvold"])
    rule_ids = {c.rule_id for c in complements}
    assert "self_bridging_cascade" not in rule_ids


def test_find_all_complements_emits_at_default_flag(mini_db: sqlite3.Connection) -> None:
    """With the default (landed) flag, the rule fires on the
    Gravecrawler cascade shape exactly once."""
    complements = find_all_complements(mini_db, ["Korvold"])
    pathway_hits = [c for c in complements if c.rule_id == "self_bridging_cascade"]
    assert len(pathway_hits) == 1
    hit = pathway_hits[0]
    assert hit.candidate == "Gravecrawler"
    assert hit.cand_event == "self_bridging"
    assert hit.filter_group == "depth_2"
    assert hit.direction == "synergy"


# ---------------------------------------------------------------------------
# Registry invariants
# ---------------------------------------------------------------------------


def test_rule_is_not_in_declarative_rule_ids() -> None:
    """The depth-2 walker is inherently algorithmic — it cannot be
    expressed as a declarative JSON rule. Guard against accidental
    addition to ``data/rules_seed.json``."""
    assert "self_bridging_cascade" not in DECLARATIVE_RULE_IDS


def test_rule_is_in_card_level_rules() -> None:
    """The rule requires >=2 distinct commander-port matches —
    attribution via single-port gates would over-attribute.
    ``CARD_LEVEL_RULES`` documents this correctly."""
    assert "self_bridging_cascade" in CARD_LEVEL_RULES


def test_rule_to_bucket_entry_exists() -> None:
    """``_RULE_TO_BUCKET`` routes the rule's score contribution to a
    legacy bucket for ``UniversalScore.to_legacy_buckets()``."""
    assert _RULE_TO_BUCKET.get("self_bridging_cascade") == "port_match"


# ---------------------------------------------------------------------------
# Config-hash stability — Unit 3 must not invalidate pinned tensor.
# ---------------------------------------------------------------------------


def test_scoring_config_inputs_exposes_pathway_flag() -> None:
    """After Unit 6, ``ScoringConfigInputs`` has a fourth field
    ``enable_pathway_rules`` so ``compute_config_hash`` catches flag
    flips. The first three fields stay at their legacy positions for
    backward compatibility with any callsite that constructs the
    tuple positionally.

    Plan 2026-04-23-003 Unit 7 appends four additional fields
    (``enable_embedding_contribution``, ``embedding_w``, ``embedding_k``,
    ``vectorizer_version``) so the embedding flip + weight knobs also
    invalidate the pinned tensor. The leading four fields stay at their
    legacy positions.

    2026-06-09 audit follow-up appends three more
    (``event_match_seed_digest``, ``declarative_rules_digest``,
    ``staples``) so seed-JSON edits and STAPLES tuning also invalidate
    the pinned tensor.
    """
    from mtg_synergy_graph.universal_scorer import ScoringConfigInputs

    assert ScoringConfigInputs._fields == (
        "rule_quality_multiplier",
        "flat_weight_overrides",
        "synergy_pairs",
        "enable_pathway_rules",
        "enable_embedding_contribution",
        "embedding_w",
        "embedding_k",
        "vectorizer_version",
        "event_match_seed_digest",
        "declarative_rules_digest",
        "staples",
        "enable_concave_family_agg",
    )


def test_config_hash_is_stable_across_repeated_calls() -> None:
    """Smoke test: ``compute_config_hash`` is deterministic when the
    scoring-config inputs don't change between calls."""
    h1 = compute_config_hash()
    h2 = compute_config_hash()
    assert h1 == h2


def test_config_hash_flips_when_pathway_flag_flips(mini_db: sqlite3.Connection) -> None:
    """Flipping ``_ENABLE_PATHWAY_RULES`` must shift the config hash
    so flag=True vs flag=False runs cannot compare against each
    other's pinned tensor.
    """
    before = compute_config_hash()
    # Flag default is now True (landed 2026-04-23); patch to False
    # to observe the flip.
    with patch.object(pathway, "_ENABLE_PATHWAY_RULES", False):
        during = compute_config_hash()
    after = compute_config_hash()
    assert before == after  # flag reverts cleanly
    assert before != during  # flag flip shifts the hash
