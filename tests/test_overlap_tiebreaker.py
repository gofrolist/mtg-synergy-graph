"""Test overlap tiebreaker configuration."""


def test_overlap_exclude_contains_board_generic():
    from mtg_synergy.recommend.scoring import OVERLAP_EXCLUDE
    assert "board-generic" in OVERLAP_EXCLUDE


def test_overlap_exclude_does_not_contain_specific_subtags():
    from mtg_synergy.recommend.scoring import OVERLAP_EXCLUDE
    for tag in ("board-tokens", "board-tribal", "board-go-wide",
                "pump-lord", "tokens-creature", "etb-value"):
        assert tag not in OVERLAP_EXCLUDE, f"{tag} should not be excluded"
