#!/usr/bin/env python3
"""Build the causal interaction graph from parsed abilities."""
import argparse
from mtg_synergy.db import get_connection
from mtg_synergy.parse import load_parsed, ensure_parse_schema
from mtg_synergy.causal import build_and_store_graph, ensure_causal_schema


def _build_synthetic_edges(conn, idx, name_to_oid):
    """Build edges for implicit game events.

    Existing: SpellCast (instants/sorceries), Attacks (creatures), LandPlayed (lands)
    New: ChangesZone+Battlefield — permanents entering the battlefield implicitly
         trigger cards that care about creature/artifact/enchantment ETB.

    Creates "exact" edges for subtype matches (e.g., Human creature → Kyler)
    and "broad" edges for card_type matches (e.g., any creature → Soul Warden).
    Streams edges to DB in chunks to avoid holding millions in memory.
    """
    import json as _json
    from mtg_synergy.parse.forge_filter_parser import parse_forge_filter
    from mtg_synergy.parse.forge_types import ForgeFilter

    # (trigger_mode, card_type_sql, resp_dest_filter, include_broad)
    # resp_dest_filter: only include responders matching this destination (None = any)
    # include_broad: also include card_type-only responders (not just subtype)
    SYNTHETIC_EVENTS = [
        ("SpellCast", "c.type_line NOT LIKE '%Land%'", None, False),
        ("Attacks", "c.type_line LIKE '%Creature%'", None, False),
        ("LandPlayed", "c.type_line LIKE '%Land%'", None, False),
        # Permanents entering battlefield
        ("ChangesZone", "c.type_line LIKE '%Creature%'", "Battlefield", True),
        ("ChangesZone", "c.type_line LIKE '%Artifact%'", "Battlefield", True),
        ("ChangesZone", "c.type_line LIKE '%Enchantment%'", "Battlefield", True),
        ("ChangesZone", "c.type_line LIKE '%Planeswalker%'", "Battlefield", True),
    ]

    total = 0
    for mode, type_filter, resp_dest, include_broad in SYNTHETIC_EVENTS:
        responders = idx.responders_for(mode)
        if not responders:
            continue

        # Pre-parse responder filters into subtype (exact) and card_type (broad)
        subtype_resps = []
        broad_resps = []
        for resp_name, resp_idx, resp_filter_str, resp_origin, resp_dest_r in responders:
            # Skip Self-only triggers
            if resp_filter_str and "Self" in resp_filter_str and "Other" not in resp_filter_str:
                continue
            # Filter by destination if required (e.g., only Battlefield for ETB)
            if resp_dest and resp_dest_r and resp_dest not in resp_dest_r:
                continue
            resp_filter = parse_forge_filter(resp_filter_str) if resp_filter_str else ForgeFilter()
            resp_id = name_to_oid.get(resp_name)
            if not resp_id:
                continue
            if resp_filter.subtypes:
                subtype_resps.append((resp_name, resp_id, resp_idx, resp_filter))
            elif include_broad and resp_filter.card_types:
                broad_resps.append((resp_name, resp_id, resp_idx, resp_filter))

        if not subtype_resps and not broad_resps:
            continue

        # Build subtype lookup for fast filtering
        wanted_subtypes = set()
        for _, _, _, rf in subtype_resps:
            for st in rf.subtypes:
                wanted_subtypes.add(st.lower())

        # Build card_type lookup for broad responders
        broad_card_types = set()
        for _, _, _, rf in broad_resps:
            for ct in rf.card_types:
                broad_card_types.add(ct.lower())

        # Stream through producer cards
        batch = []
        for row in conn.execute(
            f"SELECT fnm.forge_name, c.oracle_id, c.type_line FROM cards c "
            f"JOIN forge_name_map fnm ON fnm.oracle_id = c.oracle_id "
            f"WHERE ({type_filter}) AND c.type_line NOT LIKE '%Token%'"
        ):
            prod_name, prod_oid, prod_type = row
            if not prod_type:
                continue
            prod_type_lower = prod_type.lower()

            # Exact matches: producer has a subtype that a responder wants
            if wanted_subtypes and any(st in prod_type_lower for st in wanted_subtypes):
                for resp_name, resp_oid, resp_idx, resp_filter in subtype_resps:
                    if prod_oid == resp_oid:
                        continue
                    if any(st.lower() in prod_type_lower for st in resp_filter.subtypes):
                        detail = _json.dumps({"event": mode, "filter_precision": "exact"})
                        batch.append((prod_oid, resp_oid, "triggers", -1, resp_idx, 0.3, detail))

            # Broad matches: producer's card type matches responder's required type
            if broad_resps:
                for resp_name, resp_oid, resp_idx, resp_filter in broad_resps:
                    if prod_oid == resp_oid:
                        continue
                    if any(ct.lower() in prod_type_lower for ct in resp_filter.card_types):
                        detail = _json.dumps({"event": mode, "filter_precision": "broad"})
                        batch.append((prod_oid, resp_oid, "triggers", -1, resp_idx, 0.15, detail))

            if len(batch) >= 10000:
                conn.executemany(
                    "INSERT OR IGNORE INTO interaction_edges VALUES (?,?,?,?,?,?,?)",
                    batch)
                total += len(batch)
                batch.clear()

        if batch:
            conn.executemany(
                "INSERT OR IGNORE INTO interaction_edges VALUES (?,?,?,?,?,?,?)",
                batch)
            total += len(batch)
            batch.clear()

        conn.commit()

    return total


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rebuild", action="store_true")
    parser.add_argument("--forge", action="store_true", help="Build from Forge data (new system)")
    parser.add_argument("--stats", action="store_true")
    args = parser.parse_args()
    conn = get_connection()
    ensure_parse_schema(conn)
    ensure_causal_schema(conn)
    if args.forge:
        from mtg_synergy.causal.forge_indexer import build_forge_index
        from mtg_synergy.causal.forge_graph_builder import build_forge_edges
        from mtg_synergy.causal import store_edges
        print("Building Forge-native graph...")
        # Load name→oracle_id mapping so edges use oracle_ids
        name_to_oid = {}
        for row in conn.execute("SELECT forge_name, oracle_id FROM forge_name_map"):
            name_to_oid[row[0]] = row[1]
        print(f"  Name mapping: {len(name_to_oid)} cards")
        idx = build_forge_index(conn)
        edges = build_forge_edges(idx, name_to_oid=name_to_oid)
        count = store_edges(conn, edges)
        print(f"  Forge edges: {count}")

        # Synthetic edges: SpellCast, Attacks, LandPlayed, ChangesZone+Battlefield
        # Streamed directly to DB to avoid holding millions of edges in memory
        syn_count = _build_synthetic_edges(conn, idx, name_to_oid)
        print(f"  Synthetic edges: {syn_count}")
        print(f"Done: {count + syn_count} total edges")
    elif args.rebuild:
        rows = conn.execute("SELECT DISTINCT oracle_id FROM parsed_abilities").fetchall()
        cards = {}
        for (oid,) in rows:
            cards[oid] = load_parsed(conn, oid)
        print(f"Building graph from {len(cards)} cards...")
        n_edges = build_and_store_graph(conn, cards)
        print(f"Done: {n_edges} edges built")
    elif args.stats:
        n_edges = conn.execute("SELECT COUNT(*) FROM interaction_edges").fetchone()[0]
        n_cards = conn.execute("SELECT COUNT(DISTINCT source_id) FROM interaction_edges").fetchone()[0]
        print(f"Edges: {n_edges}")
        print(f"Cards with edges: {n_cards}")
    else:
        parser.print_help()
    conn.close()

if __name__ == "__main__":
    main()
