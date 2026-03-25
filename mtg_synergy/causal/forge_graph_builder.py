"""Build causal edges between cards using Forge vocabulary.

Matches effect producers against trigger responders using ForgeFilter
matching and IDF weighting. Replaces the old graph_builder.py.
"""
from collections import defaultdict

from mtg_synergy.causal.forge_indexer import ForgeIndex
from mtg_synergy.causal.types import Edge, EdgeDetail
from mtg_synergy.parse.forge_filter_parser import parse_forge_filter
from mtg_synergy.parse.forge_types import ForgeFilter


def compute_filter_match(responder_filter: ForgeFilter, producer_detail: dict,
                         trigger_mode: str) -> str:
    """Determine how well a producer matches a responder's filter.

    Returns: "exact", "broad", "unfiltered", or "none".
    """
    if not responder_filter or not responder_filter.card_types:
        if not responder_filter or (not responder_filter.subtypes and
                                     not responder_filter.controller):
            return "unfiltered"

    # Check subtype match (Goblin.YouCtrl -> exact if producer makes Goblins)
    if responder_filter.subtypes:
        # Check if the producer's token/target mentions this subtype
        verb = producer_detail.get("verb", "")
        target = producer_detail.get("target", "") or ""
        target_lower = target.lower()
        for st in responder_filter.subtypes:
            if st.lower() in target_lower:
                return "exact"
        # Subtype required but not matched -- still possible as broad
        # if card_type matches
        if responder_filter.card_types:
            return "broad"
        return "none"

    # Card type only (Creature.YouCtrl -> broad)
    if responder_filter.card_types:
        return "broad"

    return "unfiltered"


_PRECISION_STRENGTH = {"exact": 1.0, "broad": 0.6, "unfiltered": 0.3, "none": 0.0}


def build_forge_edges(idx: ForgeIndex, max_edges_per_event: int = 50000) -> list[Edge]:
    """Build causal edges from the Forge index.

    For each trigger mode, cross-match producers x responders with
    filter matching and IDF weighting.
    """
    event_idf = idx.compute_event_idf()
    edges = []

    # Get all trigger modes that have both producers and responders
    all_modes = set(idx._producers.keys()) & set(idx._responders.keys())

    for mode in all_modes:
        producers = idx._producers[mode]
        responders = idx._responders[mode]

        # IDF for this event
        p_idf = event_idf["producer"].get(mode, 1.0)
        r_idf = event_idf["responder"].get(mode, 1.0)
        combined_idf = min(p_idf * r_idf, 3.0)

        edge_count = 0
        for prod_name, prod_idx, prod_detail in producers:
            for resp_name, resp_idx, resp_filter_str, resp_origin, resp_dest in responders:
                if prod_name == resp_name:
                    continue

                # Check zone match for ChangesZone
                if mode == "ChangesZone":
                    prod_dest = prod_detail.get("destination", "")
                    if resp_dest and prod_dest and resp_dest != prod_dest:
                        continue
                    prod_orig = prod_detail.get("origin", "")
                    if resp_origin and prod_orig and resp_origin != "Any" and resp_origin != prod_orig:
                        continue

                # Filter matching
                resp_filter = parse_forge_filter(resp_filter_str) if resp_filter_str else ForgeFilter()
                precision = compute_filter_match(resp_filter, prod_detail, mode)
                strength = _PRECISION_STRENGTH.get(precision, 0.0)
                if strength <= 0:
                    continue

                strength *= combined_idf

                edges.append(Edge(
                    source=prod_name,
                    target=resp_name,
                    edge_type="triggers",
                    ability_a=prod_idx,
                    ability_b=resp_idx,
                    strength=strength,
                    detail=EdgeDetail(
                        event=mode,
                        filter_precision=precision,
                    ),
                ))
                edge_count += 1

                if edge_count >= max_edges_per_event:
                    break
            if edge_count >= max_edges_per_event:
                break

    # Dedup: keep strongest edge per (source, target)
    best = {}
    for e in edges:
        key = (e.source, e.target)
        if key not in best or e.strength > best[key].strength:
            best[key] = e

    return list(best.values())
