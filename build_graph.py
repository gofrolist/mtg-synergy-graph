#!/usr/bin/env python3
"""Build the causal interaction graph from parsed abilities."""
import argparse
from mtg_synergy.db import get_connection
from mtg_synergy.parse import load_parsed, ensure_parse_schema
from mtg_synergy.causal import build_and_store_graph, ensure_causal_schema


def _build_synthetic_edges(conn, idx, name_to_oid):
    """Build edges for implicit game events (SpellCast, Attacks, LandPlayed).

    Streams edges to DB in chunks to avoid holding millions in memory.
    Only creates "exact" filter matches (subtype-specific, e.g., "Goblin attacks").
    """
    import json as _json
    from mtg_synergy.parse.forge_filter_parser import parse_forge_filter
    from mtg_synergy.parse.forge_types import ForgeFilter

    # Config: (trigger_mode, card_type_filter for SQL, synthetic_verb)
    SYNTHETIC_EVENTS = [
        ("SpellCast", "c.type_line LIKE '%Instant%' OR c.type_line LIKE '%Sorcery%'", "_SpellCast"),
        ("Attacks", "c.type_line LIKE '%Creature%'", "_Attacks"),
        ("LandPlayed", "c.type_line LIKE '%Land%'", "_LandPlayed"),
    ]

    total = 0
    for mode, type_filter, synth_verb in SYNTHETIC_EVENTS:
        responders = idx.responders_for(mode)
        if not responders:
            continue

        # Pre-parse responder filters, keep only those with subtypes (exact match potential)
        parsed_resps = []
        for resp_name, resp_idx, resp_filter_str, resp_origin, resp_dest in responders:
            if resp_filter_str and "Self" in resp_filter_str and "Other" not in resp_filter_str:
                continue
            resp_filter = parse_forge_filter(resp_filter_str) if resp_filter_str else ForgeFilter()
            # Only keep responders that have subtype requirements (enables "exact" matching)
            if resp_filter.subtypes:
                resp_id = name_to_oid.get(resp_name)
                if resp_id:
                    parsed_resps.append((resp_name, resp_id, resp_idx, resp_filter))

        if not parsed_resps:
            continue

        # Build subtype lookup: which subtypes do responders care about?
        wanted_subtypes = set()
        for _, _, _, rf in parsed_resps:
            for st in rf.subtypes:
                wanted_subtypes.add(st.lower())

        if not wanted_subtypes:
            continue

        # Stream through producer cards, only matching those with wanted subtypes
        batch = []
        for row in conn.execute(
            f"SELECT fnm.forge_name, c.oracle_id, c.type_line FROM cards c "
            f"JOIN forge_name_map fnm ON fnm.oracle_id = c.oracle_id "
            f"WHERE {type_filter}"
        ):
            prod_name, prod_oid, prod_type = row
            if not prod_type:
                continue
            prod_type_lower = prod_type.lower()

            # Check if this card has any subtype that responders want
            matching = False
            for st in wanted_subtypes:
                if st in prod_type_lower:
                    matching = True
                    break
            if not matching:
                continue

            # Match against each responder
            for resp_name, resp_oid, resp_idx, resp_filter in parsed_resps:
                if prod_oid == resp_oid:
                    continue
                # Check exact subtype match
                matched = any(st.lower() in prod_type_lower for st in resp_filter.subtypes)
                if not matched:
                    continue

                # Strength: low (0.3) since these are implicit events, IDF dampened
                strength = 0.3
                detail = _json.dumps({"event": mode, "filter_precision": "exact"})
                batch.append((prod_oid, resp_oid, "triggers", -1, resp_idx, strength, detail))

                if len(batch) >= 5000:
                    conn.executemany(
                        "INSERT OR IGNORE INTO interaction_edges VALUES (?,?,?,?,?,?,?)",
                        batch)
                    total += len(batch)
                    batch.clear()

            # Also check for responders without subtypes but with card_types
            # (handled by the regular forge edges, skip here to keep synthetic tight)

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

        # Synthetic edges: SpellCast, Attacks, LandPlayed
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
