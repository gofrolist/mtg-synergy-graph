"""IDF (Inverse Document Frequency) computation for tag weighting."""
import math
from collections import Counter


def compute_idf(cards: list[dict]) -> dict[str, float]:
    """Compute IDF multipliers for provides and wants tags.

    Rare tags get higher weight (up to 2.0x), common tags get lower (down to 0.5x).
    This prevents ubiquitous tags like trigger-doubling (on 27% of cards) from
    dominating edge scores while boosting rare, specific matches.
    """
    n = len(cards)
    if n == 0:
        return {}

    freq = Counter()
    for card in cards:
        for t in card.get("provides", []):
            freq[t] += 1
        for t in card.get("wants", []):
            freq[t] += 1

    idf = {}
    # Raw IDF range is ~1.3 (27% freq) to ~6.8 (1 card).
    # Normalize to 0.5-2.0 multiplier range.
    max_idf = math.log(n)  # theoretical max (tag on 1 card)
    min_idf = math.log(2)  # theoretical min (tag on n/2 cards)
    span = max_idf - min_idf if max_idf > min_idf else 1.0

    for tag, count in freq.items():
        raw = math.log(n / count)
        # Linear map: min_idf -> 0.5, max_idf -> 2.0
        normalized = 0.5 + 1.5 * (raw - min_idf) / span
        idf[tag] = round(max(0.5, min(2.0, normalized)), 3)

    return idf
