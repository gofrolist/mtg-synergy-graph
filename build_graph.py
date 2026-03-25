#!/usr/bin/env python3
"""Build the causal interaction graph from parsed abilities."""
import argparse
from mtg_synergy.db import get_connection
from mtg_synergy.parse import load_parsed, ensure_parse_schema
from mtg_synergy.causal import build_and_store_graph, ensure_causal_schema


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
        idx = build_forge_index(conn)
        edges = build_forge_edges(idx)
        count = store_edges(conn, edges)
        print(f"Done: {count} edges built")
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
