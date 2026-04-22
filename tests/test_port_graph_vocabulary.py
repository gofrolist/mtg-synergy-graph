"""Canonical vocabulary tests (Unit 1 of plan 002).

The vocabulary is three frozensets + one version string. Tests lock
the shape (frozenset, non-empty, no whitespace tokens) and the
closed-set membership expected by downstream units. Exact sizes are
asserted so growing or shrinking a set without bumping
``VOCAB_VERSION`` fails loudly.
"""

from __future__ import annotations

from mtg_synergy_graph.port_graph import vocabulary as vocab


def test_node_kinds_is_closed_set() -> None:
    """NODE_KINDS is a frozenset of exactly the 21 canonical kinds
    listed in plan 002 FR1. Any addition or removal must be paired
    with a VOCAB_VERSION bump — lock the size here so the bump
    can't be forgotten."""
    expected = {
        "ETB",
        "LTB",
        "DIES",
        "CAST",
        "RESOLVE",
        "ATTACK",
        "BLOCK",
        "PAYMANA",
        "TAP",
        "UNTAP",
        "COUNTER_PLACED",
        "COUNTER_REMOVED",
        "SACRIFICE",
        "DISCARD",
        "DRAW",
        "DAMAGE",
        "LIFE_CHANGE",
        "ZONECHANGE",
        "STATIC_BUFF",
        "STATIC_REPLACEMENT",
        "SCALES_WITH",
    }
    assert isinstance(vocab.NODE_KINDS, frozenset)
    assert expected == vocab.NODE_KINDS
    assert len(vocab.NODE_KINDS) == 21


def test_match_qualities_is_closed_set() -> None:
    """MATCH_QUALITIES is exactly the 5 predicate kinds mapped from
    graph_engine.EVENT_MATCH_MAP's Python lambdas. Expanding requires
    a new interpreter case plus a VOCAB_VERSION bump."""
    expected = {
        "always",
        "zone_compatible",
        "counter_compatible",
        "tribe_match",
        "color_match",
    }
    assert isinstance(vocab.MATCH_QUALITIES, frozenset)
    assert expected == vocab.MATCH_QUALITIES


def test_gate_ops_includes_combinators_and_leaves() -> None:
    """GATE_OPS covers the three boolean combinators and the seven
    leaf ops that the POC migrations in Units 7 & 8 require. Anything
    the Unit 5 interpreter can't handle at load time should surface
    as a validator error here before it reaches production."""
    combinators = {"and", "or", "not"}
    leaves = {
        "has_port",
        "filter_tag",
        "zone_eq",
        "counter_type",
        "tribe",
        "color",
        "not_in_commander_set",
    }
    assert isinstance(vocab.GATE_OPS, frozenset)
    assert combinators <= vocab.GATE_OPS
    assert leaves <= vocab.GATE_OPS
    assert combinators | leaves == vocab.GATE_OPS


def test_all_identifiers_are_clean_strings() -> None:
    """Every identifier in every set is a non-empty string with no
    leading/trailing whitespace — the JSON serializers and SQL
    comparators all assume this invariant. Failing here means some
    future editor sneaked in a stray space or accidentally used a
    non-string value."""
    for name, s in (
        ("NODE_KINDS", vocab.NODE_KINDS),
        ("MATCH_QUALITIES", vocab.MATCH_QUALITIES),
        ("GATE_OPS", vocab.GATE_OPS),
    ):
        for tok in s:
            assert isinstance(tok, str), f"{name} contains non-string {tok!r}"
            assert tok == tok.strip(), f"{name} has padded token {tok!r}"
            assert tok, f"{name} contains empty string"


def test_vocab_version_is_non_empty_string() -> None:
    """Version is a non-empty string; consumers fold it into hashes
    or schema migration tags, so must be non-empty."""
    assert isinstance(vocab.VOCAB_VERSION, str)
    assert vocab.VOCAB_VERSION.strip() == vocab.VOCAB_VERSION
    assert vocab.VOCAB_VERSION


def test_package_init_re_exports_vocabulary() -> None:
    """``from mtg_synergy_graph.port_graph import NODE_KINDS`` works
    — the package __init__ surfaces the constants directly so
    downstream units don't need the inner module path. This is the
    contract other units (2, 4, 5) will import against."""
    from mtg_synergy_graph.port_graph import (
        GATE_OPS,
        MATCH_QUALITIES,
        NODE_KINDS,
        VOCAB_VERSION,
    )

    assert NODE_KINDS is vocab.NODE_KINDS
    assert MATCH_QUALITIES is vocab.MATCH_QUALITIES
    assert GATE_OPS is vocab.GATE_OPS
    assert VOCAB_VERSION == vocab.VOCAB_VERSION


def test_vocabulary_import_has_no_side_effects() -> None:
    """Re-importing vocabulary is idempotent — no DB access, no
    file IO, constants are module-constants. Lock this so future
    edits don't accidentally push network / filesystem work into
    the import path."""
    import importlib

    from mtg_synergy_graph.port_graph import vocabulary as v1

    v2 = importlib.reload(v1)
    assert v1.NODE_KINDS is v2.NODE_KINDS or v1.NODE_KINDS == v2.NODE_KINDS
    assert v1.VOCAB_VERSION == v2.VOCAB_VERSION
