"""Combo display and validation output."""
from mtg_synergy.combos.detector import find_combos_tiered, find_partial_combos


def show_combos(combos: dict, commander: str, top_n: int = 15):
    """Display detected combos."""
    pairs = combos.get("pairs", [])
    triangles = combos["triangles"]
    quads = combos["quads"]

    print(f"\n{'═' * 70}")
    print(f"COMBO DETECTION — {combos.get('total_pairs', 0)} pairs, "
          f"{combos['total_triangles']} triangles, "
          f"{combos['total_quads']} four-card combos found")
    print(f"{'═' * 70}")

    if pairs:
        print(f"\nTop 2-card pairs (provides→wants cycles):")
        print(f"{'─' * 70}")
        for i, pair in enumerate(pairs[:top_n], 1):
            star = " ★ commander" if pair["commander"] else ""
            label = pair.get("label", "synergy").upper()
            a, b = pair["cards"]
            print(f"\n  #{i} [{label}] score: {pair['score']}{star}")
            print(f"     {a} + {b}")
            print(f"     {a} → {b}: {pair['a_provides_b_wants']}")
            print(f"     {b} → {a}: {pair['b_provides_a_wants']}")

    if triangles:
        print(f"\nTop 3-card combos:")
        print(f"{'─' * 70}")
        for i, tri in enumerate(triangles[:top_n], 1):
            star = " ★ commander" if tri["commander"] else ""
            print(f"\n  #{i} [{tri['type']}] score: {tri['score']}{star}")
            print(f"     {' + '.join(tri['cards'])}")
            print(f"     min edge: {tri['min_edge']}")

    if quads:
        print(f"\nTop 4-card combos:")
        print(f"{'─' * 70}")
        for i, quad in enumerate(quads[:top_n], 1):
            star = " ★ commander" if quad["commander"] else ""
            print(f"\n  #{i} [{quad['type']}] score: {quad['score']} "
                  f"({quad['edges']}/6 edges){star}")
            print(f"     {' + '.join(quad['cards'])}")
            print(f"     min edge: {quad['min_edge']}")


def validate_against_curated(graph: dict, synergy_pairs: list[tuple]):
    """Compare graph edges against hand-curated synergy pairs."""

    adj = graph["adjacency"]

    print(f"\nValidation: graph edges vs {len(synergy_pairs)} hand-curated synergy pairs")
    print(f"{'═' * 70}")

    found = 0
    missed = 0

    for card_a, card_b, reason in synergy_pairs:
        edges_a = adj.get(card_a, [])
        edges_b = adj.get(card_b, [])

        edge = next(
            (e for e in edges_a if e["target"] == card_b),
            next((e for e in edges_b if e["target"] == card_a), None)
        )

        if edge:
            found += 1
            sig = f"{edge['signals']}sig" if edge["signals"] > 1 else "1sig"
            print(f"  ✓  {card_a} ↔ {card_b}  (score={edge['score']}, {sig})")
            print(f"       curated: {reason}")
            for r in edge["reasons"][:2]:
                print(f"       {r}")
        else:
            missed += 1
            print(f"  ✗  {card_a} ↔ {card_b}")
            print(f"       curated: {reason}")
            a_exists = card_a in adj
            b_exists = card_b in adj
            if not a_exists:
                print(f"       ('{card_a}' not in graph)")
            if not b_exists:
                print(f"       ('{card_b}' not in graph)")

    print(f"\n{'═' * 70}")
    print(f"Found: {found}/{len(synergy_pairs)} ({100*found/len(synergy_pairs):.0f}%)")
    print(f"Missed: {missed}/{len(synergy_pairs)}")


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
