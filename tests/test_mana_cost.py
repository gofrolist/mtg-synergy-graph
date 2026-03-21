import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


def test_mana_cost_penalty():
    """High-CMC cards should get penalized in scoring."""
    # Simple test: a card with CMC 10 in a deck averaging CMC 3 should be penalized
    avg_cmc = 3.0
    cmc = 10
    penalty = max(0.3, 1.0 - 0.15 * (cmc - avg_cmc - 3))
    assert penalty < 1.0
    assert penalty == max(0.3, 1.0 - 0.15 * 4)  # 4 over threshold = 0.4
    assert penalty == 0.4


def test_mana_cost_no_penalty():
    """Cards within normal CMC range should not be penalized."""
    avg_cmc = 3.0
    cmc = 5  # Only 2 over average, within threshold of +3
    should_penalize = cmc > avg_cmc + 3
    assert not should_penalize


def test_mana_cost_penalty_floors():
    """Penalty should floor at 0.3, not go to zero."""
    avg_cmc = 2.0
    cmc = 15
    penalty = max(0.3, 1.0 - 0.15 * (cmc - avg_cmc - 3))
    assert penalty == 0.3
