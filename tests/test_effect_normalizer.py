"""Tests for effect text normalization rules."""
import pytest
from mtg_synergy.parse.effect_parser import _normalize_effect_text


def _texts(result):
    """Extract just the text strings from normalize result tuples."""
    return [r[0] if isinstance(r, tuple) else r for r in result]


# --- Rule 1: Strip "you may" ---

def test_strip_you_may():
    result = _normalize_effect_text("you may search your library for a card")
    texts = _texts(result)
    assert any("search" in r.lower() for r in texts)
    for r in texts:
        assert not r.lower().startswith("you may")
    assert all(r[1] for r in result)


def test_strip_you_might():
    result = _normalize_effect_text("you might reveal the top card")
    texts = _texts(result)
    for r in texts:
        assert not r.lower().startswith("you might")


def test_strip_you_may_preserves_verb():
    result = _normalize_effect_text("you may draw a card")
    texts = _texts(result)
    assert any("draw" in r.lower() for r in texts)


# --- Rule 2: Extract conditional ---

def test_extract_conditional():
    result = _normalize_effect_text("if the player doesn't, you create a Treasure token")
    texts = _texts(result)
    assert any("create" in r.lower() for r in texts)


def test_extract_conditional_simple():
    result = _normalize_effect_text("if you do, exile it")
    texts = _texts(result)
    assert any("exile" in r.lower() for r in texts)


def test_extract_conditional_nested():
    result = _normalize_effect_text("if you control a creature, if it's your turn, draw a card")
    texts = _texts(result)
    assert any("draw" in r.lower() for r in texts)


def test_conditional_max_iterations():
    result = _normalize_effect_text("if a, if b, if c, if d, draw a card")
    assert len(result) >= 1


# --- Rule 4: Normalize "you verb" ---

def test_normalize_you_create():
    result = _normalize_effect_text("you create a Treasure token")
    texts = _texts(result)
    assert any(r.startswith("Create") for r in texts)


def test_normalize_you_destroy():
    result = _normalize_effect_text("you destroy target artifact")
    texts = _texts(result)
    assert any(r.startswith("Destroy") for r in texts)


def test_you_draw_unchanged():
    result = _normalize_effect_text("you draw a card")
    texts = _texts(result)
    assert any("draw" in r.lower() for r in texts)


# --- Rule 6: Strip "for each" ---

def test_strip_for_each():
    result = _normalize_effect_text("for each creature you control, create a 1/1 token")
    texts = _texts(result)
    assert any("create" in r.lower() for r in texts)


def test_strip_for_each_preserves_action():
    result = _normalize_effect_text("for each opponent, draw a card")
    texts = _texts(result)
    assert any("draw" in r.lower() for r in texts)


def test_no_op_on_clean_text():
    result = _normalize_effect_text("draw a card")
    assert _texts(result) == ["draw a card"]
    assert result[0][1] is False


def test_no_op_on_deal_damage():
    result = _normalize_effect_text("Purphoros deals 2 damage to each opponent")
    assert _texts(result) == ["Purphoros deals 2 damage to each opponent"]


# --- Rule 3: Subject normalization + deconjugation ---

from mtg_synergy.parse.effect_parser import _normalize_subject


def test_normalize_each_opponent():
    # "draws" is in already_handled set — _normalize_subject preserves original text
    # because _try_draw uses re.search that already handles "each opponent draws"
    result = _normalize_subject("each opponent draws a card")
    assert "draws" in result or "draw" in result


def test_normalize_that_player():
    result = _normalize_subject("that player creates a token")
    assert result == "create a token"


def test_normalize_its_controller():
    result = _normalize_subject("its controller creates a 3/3 green Beast creature token")
    assert result.startswith("create")


def test_normalize_target_opponent():
    result = _normalize_subject("target opponent discards a card")
    assert result == "discard a card"


def test_deconjugation_all_verbs():
    from mtg_synergy.parse.effect_parser import _VERB_DECONJ
    # Verbs already handled by re.search parsers are preserved with their subject
    already_handled = {
        "sacrifices", "loses", "gains", "deals",
        "draws", "mills", "puts",
    }
    for conj, inf in _VERB_DECONJ.items():
        result = _normalize_subject(f"each opponent {conj} something")
        if conj in already_handled:
            # These return original text (existing parsers handle them)
            assert result == f"each opponent {conj} something", (
                f"{conj} should be preserved, got: {result}"
            )
        else:
            assert result.startswith(inf), f"{conj} should become {inf}, got: {result}"


def test_no_normalization_on_clean_text():
    result = _normalize_subject("draw a card")
    assert result == "draw a card"
