#!/usr/bin/env python3
"""CLI for the oracle text parser.

Usage:
    python3 oracle_parser.py --card "Krenko, Mob Boss" --verbose
    python3 oracle_parser.py --parse-all --top 5000
    python3 oracle_parser.py --stats
"""
import argparse
import json
import sys

from mtg_synergy.config import DB_PATH
from mtg_synergy.db import get_connection
from mtg_synergy.parse import parse_card, save_parsed, ensure_parse_schema


def main():
    parser = argparse.ArgumentParser(description="Oracle text parser")
    parser.add_argument("--card", help="Parse a single card by name")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--parse-all", action="store_true", help="Parse top N cards")
    parser.add_argument("--top", type=int, default=5000)
    parser.add_argument("--stats", action="store_true")
    args = parser.parse_args()

    conn = get_connection()
    ensure_parse_schema(conn)

    if args.card:
        _parse_single(conn, args.card, args.verbose)
    elif args.parse_all:
        _parse_batch(conn, args.top)
    elif args.stats:
        _show_stats(conn)
    else:
        parser.print_help()

    conn.close()


def _parse_single(conn, card_name, verbose):
    row = conn.execute(
        "SELECT oracle_id, name, oracle_text, type_line, mana_cost FROM cards WHERE name = ?",
        (card_name,)
    ).fetchone()
    if not row:
        print(f"Card not found: {card_name}")
        return
    oracle_id, name, oracle_text, type_line, mana_cost = row
    abilities = parse_card(oracle_text or "", type_line or "", mana_cost or "")
    save_parsed(conn, oracle_id, abilities)
    conn.commit()
    print(f"Parsed {name}: {len(abilities)} abilities")
    if verbose:
        for i, a in enumerate(abilities):
            print(f"  [{i}] {a.kind}: {json.dumps(a.to_dict(), indent=2)}")


def _parse_batch(conn, top_n):
    rows = conn.execute(
        "SELECT oracle_id, name, oracle_text, type_line, mana_cost FROM cards "
        "WHERE oracle_text IS NOT NULL AND oracle_text != '' "
        "ORDER BY edhrec_rank ASC NULLS LAST LIMIT ?",
        (top_n,)
    ).fetchall()
    parsed, failed = 0, 0
    for oracle_id, name, oracle_text, type_line, mana_cost in rows:
        try:
            abilities = parse_card(oracle_text, type_line or "", mana_cost or "")
            save_parsed(conn, oracle_id, abilities)
            parsed += 1
        except Exception as e:
            failed += 1
            print(f"  FAIL: {name}: {e}", file=sys.stderr)
        if parsed % 500 == 0 and parsed > 0:
            conn.commit()
            print(f"  Parsed {parsed}/{top_n}...")
    conn.commit()
    print(f"Done: {parsed} parsed, {failed} failed out of {len(rows)}")


def _show_stats(conn):
    total_cards = conn.execute(
        "SELECT COUNT(*) FROM cards WHERE oracle_text IS NOT NULL AND oracle_text != ''"
    ).fetchone()[0]
    parsed_cards = conn.execute(
        "SELECT COUNT(DISTINCT oracle_id) FROM parsed_abilities"
    ).fetchone()[0]
    total_abilities = conn.execute(
        "SELECT COUNT(*) FROM parsed_abilities"
    ).fetchone()[0]
    print(f"Total cards with oracle text: {total_cards}")
    print(f"Parsed cards:                 {parsed_cards} ({100*parsed_cards/max(total_cards,1):.1f}%)")
    print(f"Total abilities parsed:       {total_abilities}")


if __name__ == "__main__":
    main()
