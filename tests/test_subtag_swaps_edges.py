"""Tests that parent tags have been replaced with sub-tags in swaps.py and edges.py."""


def test_no_parent_tags_in_synergy_provides():
    from mtg_synergy.recommend.swaps import SYNERGY_PROVIDES
    parent_tags = {"creature-pump", "token-generation", "evasion-grant"}
    assert not parent_tags & SYNERGY_PROVIDES, f"Parent tags found: {parent_tags & SYNERGY_PROVIDES}"


def test_skip_wants_uses_board_generic():
    """SKIP_WANTS should use board-generic, not creature-board."""
    from mtg_synergy.graph.edges import SKIP_WANTS
    assert "creature-board" not in SKIP_WANTS
    assert "board-generic" in SKIP_WANTS
