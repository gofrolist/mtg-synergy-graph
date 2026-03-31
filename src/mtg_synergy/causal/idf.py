"""Shared IDF computation and precision constants for event weighting.

Used by ForgeIndex (forge_indexer.py) and forge_graph_builder.py.
"""
import math

# Shared precision-to-strength mapping for causal edge building.
PRECISION_STRENGTH = {"exact": 1.0, "broad": 0.6, "unfiltered": 0.3, "none": 0.0}


def compute_event_idf(total_cards: int,
                      producer_counts: dict[str, int],
                      responder_counts: dict[str, int]) -> dict:
    """Compute IDF multipliers for producer and responder events.

    Rare events get higher weight (up to 3.0x), common events get lower (down to 0.3x).

    Args:
        total_cards: total number of indexed cards
        producer_counts: {event: num_unique_producer_cards}
        responder_counts: {event: num_unique_responder_cards}

    Returns:
        {"producer": {event: idf}, "responder": {event: idf}}
    """
    result = {"producer": {}, "responder": {}}
    n = max(total_cards, 1)
    max_idf = math.log(n) if n > 1 else 1.0
    min_idf = math.log(2) if n > 2 else 0.1
    span = max_idf - min_idf if max_idf > min_idf else 1.0

    for side, counts in [("producer", producer_counts),
                         ("responder", responder_counts)]:
        for event, count in counts.items():
            raw = math.log(max(n / max(count, 1), 1))
            normalized = 0.3 + 2.7 * (raw - min_idf) / span
            result[side][event] = round(max(0.3, min(3.0, normalized)), 3)
    return result
