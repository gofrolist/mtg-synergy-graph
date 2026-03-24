"""Test normalize_tags inference rules use sub-tags."""

def test_infer_wants_uses_subtags_for_pump_lord():
    from normalize_tags import infer_wants
    provides = {"pump-lord"}
    wants = set()
    inferred = infer_wants(provides, wants)
    assert "creature-board" not in inferred
    assert "board-tribal" in inferred

def test_infer_wants_uses_subtags_for_pump_anthem():
    from normalize_tags import infer_wants
    provides = {"pump-anthem"}
    wants = set()
    inferred = infer_wants(provides, wants)
    assert "creature-board" not in inferred
    assert "board-go-wide" in inferred

def test_infer_wants_tokens_creature():
    from normalize_tags import infer_wants
    provides = {"tokens-creature"}
    wants = set()
    inferred = infer_wants(provides, wants)
    assert "creature-etb" not in inferred
    assert "etb-value" in inferred

def test_infer_wants_tokens_tribal():
    from normalize_tags import infer_wants
    provides = {"tokens-tribal"}
    wants = set()
    inferred = infer_wants(provides, wants)
    assert "creature-etb" not in inferred
    assert "etb-tribal" in inferred

def test_no_old_parent_tags_inferred():
    """No inference rule should ever produce an old parent tag."""
    from normalize_tags import infer_wants
    old_parents = {"creature-board", "creature-etb", "combat-events"}
    # Try various provides combinations
    test_provides = [
        {"pump-lord"}, {"pump-anthem"}, {"pump-combat"}, {"pump-self"},
        {"tokens-creature"}, {"tokens-tribal"}, {"tokens-artifact"},
        {"evasion-flying"}, {"evasion-unblockable"}, {"evasion-menace"},
        {"etb-payoff"}, {"board-wide-counter-placement"},
    ]
    for prov in test_provides:
        inferred = infer_wants(prov, set())
        for tag in inferred:
            assert tag not in old_parents, f"Old parent tag '{tag}' inferred from {prov}"
