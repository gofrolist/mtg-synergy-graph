"""Combo display and validation output."""
from mtg_synergy.combos.detector import find_combos_tiered, find_partial_combos


def show_combos_tiered(deck_oids, commander_name=None, db_path=None, color_identity=None):
    """Display 3-tier combo output."""
    combos = find_combos_tiered(deck_oids, db_path)
    partials = find_partial_combos(deck_oids, db_path, color_identity=color_identity)

    confirmed = [c for c in combos if c["tier"] == "infinite-confirmed"]
    likely = [c for c in combos if c["tier"] == "combo-likely"]
    synergy = [c for c in combos if c["tier"] == "synergy"]

    if confirmed:
        print(f"\n{'='*60}")
        print(f"CONFIRMED INFINITE COMBOS ({len(confirmed)})")
        print(f"{'='*60}")
        for c in confirmed:
            print(f"  {' + '.join(c['cards'])}")
            print(f"    Result: {c['result']}")
            print(f"    Source: {c['reason']}")

    if likely:
        print(f"\n{'='*60}")
        print(f"LIKELY COMBOS ({len(likely)})")
        print(f"{'='*60}")
        for c in likely:
            print(f"  {' + '.join(c['cards'])}")
            print(f"    Chain: {c['result']}")

    if partials:
        print(f"\n{'='*60}")
        print(f"NEAR-COMPLETE COMBOS — 1 card away ({len(partials)})")
        print(f"{'='*60}")
        for p in partials:
            print(f"  {' + '.join(p['present_cards'])} + [{p['missing_cards'][0]}]")
            print(f"    Result: {p['result']}")

    if synergy:
        print(f"\n  Synergy pairs: {len(synergy)} (use --verbose to list)")

    print(f"\n  Total: {len(confirmed)} confirmed, {len(likely)} likely, {len(synergy)} synergy")
