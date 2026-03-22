#!/usr/bin/env python3
"""Validation suite: measures how well our synergy system matches ground truth.

Four metrics:
1. Spellbook Combo Recall — of known 2-card combos, what % do we detect as synergy?
2. EDHREC Theme Agreement — do our strategies match EDHREC theme memberships?
3. Curated Pair Recall — of hand-verified synergy pairs, what % scores above median?
4. Keyword Coverage — of all MTG keywords in our DB, what % have effect_tags?

Usage:
    python3 validate_system.py              # full validation report
    python3 validate_system.py --spellbook  # just Spellbook recall
    python3 validate_system.py --curated    # just curated pairs
    python3 validate_system.py --keywords   # keyword coverage audit
"""

import json
import os
import sqlite3
import sys

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
DB_PATH = os.path.join(DATA_DIR, "tags.db")


def validate_spellbook_recall(db_path=None):
    """Metric 1: Of 2-card Spellbook combos, what % have a synergy edge?

    A "detected" combo means the two cards share at least one provides→wants
    connection (either direct tag match or via semantic bridge).
    """
    if db_path is None:
        db_path = DB_PATH
    conn = sqlite3.connect(db_path)

    # Get all 2-card combos where both cards exist in our DB
    combos = conn.execute("""
        SELECT sc.combo_id, sc.card_oracle_ids, sc.card_names, sc.result
        FROM spellbook_combos sc
        WHERE sc.card_count = 2
    """).fetchall()

    # Build provides/wants index
    provides_by_card = {}
    for oid, tag in conn.execute("SELECT oracle_id, tag FROM provides").fetchall():
        provides_by_card.setdefault(oid, set()).add(tag)
    wants_by_card = {}
    for oid, tag in conn.execute("SELECT oracle_id, tag FROM wants").fetchall():
        wants_by_card.setdefault(oid, set()).add(tag)

    # Load semantic bridges
    sys.path.insert(0, os.path.dirname(__file__))
    from synergy_graph import SEMANTIC_BRIDGES

    # Load ability effect/trigger tags
    ability_tags_by_card = {}
    for oid, etags, ttags in conn.execute(
        "SELECT oracle_id, effect_tags, trigger_tags FROM abilities"
    ).fetchall():
        if oid not in ability_tags_by_card:
            ability_tags_by_card[oid] = {"effect": set(), "trigger": set()}
        if etags:
            ability_tags_by_card[oid]["effect"].update(json.loads(etags))
        if ttags:
            ability_tags_by_card[oid]["trigger"].update(json.loads(ttags))

    all_card_oids = set(conn.execute("SELECT oracle_id FROM cards").fetchall())
    all_card_oids = {r[0] for r in all_card_oids}
    conn.close()

    total = 0
    detected_direct = 0
    detected_bridge = 0
    detected_ability = 0
    missed = []

    for combo_id, oids_json, names_json, result in combos:
        oids = json.loads(oids_json)
        names = json.loads(names_json)
        if len(oids) != 2:
            continue
        a, b = oids
        if a not in all_card_oids or b not in all_card_oids:
            continue

        total += 1
        prov_a = provides_by_card.get(a, set())
        want_a = wants_by_card.get(a, set())
        prov_b = provides_by_card.get(b, set())
        want_b = wants_by_card.get(b, set())

        # Direct match: A provides what B wants or vice versa
        a_to_b = prov_a & want_b
        b_to_a = prov_b & want_a
        if a_to_b or b_to_a:
            detected_direct += 1
            continue

        # Semantic bridge match
        bridge_found = False
        for (p_tag, w_tag), weight in SEMANTIC_BRIDGES.items():
            if (p_tag in prov_a and w_tag in want_b) or (p_tag in prov_b and w_tag in want_a):
                bridge_found = True
                break
        if bridge_found:
            detected_bridge += 1
            continue

        # Ability-based match: effect_tags of A intersect trigger_tags of B or vice versa
        ab_a = ability_tags_by_card.get(a, {"effect": set(), "trigger": set()})
        ab_b = ability_tags_by_card.get(b, {"effect": set(), "trigger": set()})
        if (ab_a["effect"] & ab_b["trigger"]) or (ab_b["effect"] & ab_a["trigger"]):
            detected_ability += 1
            continue

        missed.append((names, result))

    detected_total = detected_direct + detected_bridge + detected_ability
    recall = detected_total / max(total, 1) * 100

    return {
        "total_combos": total,
        "detected": detected_total,
        "detected_direct": detected_direct,
        "detected_bridge": detected_bridge,
        "detected_ability": detected_ability,
        "missed": len(missed),
        "recall_pct": round(recall, 1),
        "missed_examples": missed[:10],
    }


def validate_curated_pairs(db_path=None):
    """Metric 3: Of hand-curated synergy pairs, what % have a synergy edge?"""
    if db_path is None:
        db_path = DB_PATH
    conn = sqlite3.connect(db_path)

    sys.path.insert(0, os.path.dirname(__file__))
    from decks import list_decks, load_deck
    from synergy_graph import SEMANTIC_BRIDGES

    # Build provides/wants index
    provides_by_name = {}
    wants_by_name = {}
    name_to_oid = {}
    for oid, name in conn.execute("SELECT oracle_id, name FROM cards").fetchall():
        name_to_oid[name] = oid
    for oid, tag in conn.execute("SELECT oracle_id, tag FROM provides").fetchall():
        provides_by_name.setdefault(oid, set()).add(tag)
    for oid, tag in conn.execute("SELECT oracle_id, tag FROM wants").fetchall():
        wants_by_name.setdefault(oid, set()).add(tag)
    conn.close()

    results = {}
    total_all = 0
    detected_all = 0

    for deck_name in list_decks():
        deck = load_deck(deck_name)
        pairs = getattr(deck, "SYNERGY_PAIRS", [])
        if not pairs:
            continue

        detected = 0
        missed = []
        for pair in pairs:
            card_a, card_b = pair[0], pair[1]
            reason = pair[2] if len(pair) > 2 else ""

            oid_a = name_to_oid.get(card_a)
            oid_b = name_to_oid.get(card_b)
            if not oid_a or not oid_b:
                missed.append((card_a, card_b, reason, "card not in DB"))
                continue

            prov_a = provides_by_name.get(oid_a, set())
            want_a = wants_by_name.get(oid_a, set())
            prov_b = provides_by_name.get(oid_b, set())
            want_b = wants_by_name.get(oid_b, set())

            # Direct
            if (prov_a & want_b) or (prov_b & want_a):
                detected += 1
                continue

            # Bridge
            found = False
            for (p_tag, w_tag), weight in SEMANTIC_BRIDGES.items():
                if (p_tag in prov_a and w_tag in want_b) or (p_tag in prov_b and w_tag in want_a):
                    found = True
                    break
            if found:
                detected += 1
                continue

            missed.append((card_a, card_b, reason, "no edge"))

        recall = detected / max(len(pairs), 1) * 100
        results[deck_name] = {
            "total": len(pairs),
            "detected": detected,
            "missed": len(pairs) - detected,
            "recall_pct": round(recall, 1),
            "missed_pairs": missed,
        }
        total_all += len(pairs)
        detected_all += detected

    overall = round(detected_all / max(total_all, 1) * 100, 1)
    return {"per_deck": results, "total": total_all, "detected": detected_all, "recall_pct": overall}


def validate_keyword_coverage(db_path=None):
    """Metric 4: Of all keywords in our DB, what % have ability effect_tags?"""
    if db_path is None:
        db_path = DB_PATH
    conn = sqlite3.connect(db_path)

    # Get all unique keywords across all cards
    all_keywords = {}
    for kw_json, in conn.execute("SELECT keywords FROM cards WHERE keywords IS NOT NULL AND keywords != ''"):
        try:
            for kw in json.loads(kw_json):
                kw_lower = kw.lower()
                all_keywords[kw_lower] = all_keywords.get(kw_lower, 0) + 1
        except (json.JSONDecodeError, TypeError):
            pass

    # Check which keywords have effect_tags in abilities table
    tagged = set()
    untagged = set()
    for kw, count in sorted(all_keywords.items(), key=lambda x: -x[1]):
        # Check if any ability with this keyword effect has non-null effect_tags
        row = conn.execute("""
            SELECT effect_tags FROM abilities
            WHERE ability_type = 'keyword' AND effect = ?
            AND effect_tags IS NOT NULL
            LIMIT 1
        """, (kw,)).fetchone()
        if row:
            tagged.add(kw)
        else:
            untagged.add(kw)

    conn.close()

    coverage = len(tagged) / max(len(all_keywords), 1) * 100
    # Sort untagged by frequency
    untagged_freq = [(kw, all_keywords[kw]) for kw in untagged]
    untagged_freq.sort(key=lambda x: -x[1])

    return {
        "total_keywords": len(all_keywords),
        "tagged": len(tagged),
        "untagged": len(untagged),
        "coverage_pct": round(coverage, 1),
        "top_untagged": untagged_freq[:20],
        "tagged_list": sorted(tagged),
    }


def validate_edhrec_theme_agreement(db_path=None):
    """Metric 2: Do our strategies match EDHREC theme memberships?"""
    edhrec_path = os.path.join(DATA_DIR, "edhrec_theme_cards.json")
    if not os.path.exists(edhrec_path):
        return {"error": "EDHREC data not found"}

    if db_path is None:
        db_path = DB_PATH
    conn = sqlite3.connect(db_path)

    with open(edhrec_path) as f:
        edhrec = json.load(f)

    # Map EDHREC themes to our strategy names
    THEME_MAP = {
        "tokens": "tokens", "+1/+1-counters": "+1/+1-counters",
        "lifegain": "lifegain", "aristocrats": "aristocrats",
        "spellslinger": "spellslinger", "reanimator": "reanimator",
        "artifacts": "artifacts", "enchantress": "enchantress",
        "voltron": "voltron", "mill": "mill", "blink": "blink",
        "landfall": "landfall", "stax": "stax", "storm": "storm",
        "wheels": "wheels", "treasure": "treasure",
        "proliferate": "proliferate", "sacrifice": "aristocrats",
        "lifedrain": "lifedrain", "equipment": "equipment",
    }

    results = {}
    total_checked = 0
    total_agreement = 0

    for theme, our_strat in THEME_MAP.items():
        if theme not in edhrec:
            continue

        # EDHREC high-synergy cards for this theme
        high_syn = [c for c in edhrec[theme] if c.get("synergy", 0) > 0.15]
        if not high_syn:
            continue

        # Resolve to oracle_ids and check our strategies
        agreement = 0
        in_db = 0
        for card in high_syn:
            row = conn.execute("SELECT oracle_id FROM cards WHERE name = ?", (card["name"],)).fetchone()
            if not row:
                continue
            in_db += 1
            strat = conn.execute(
                "SELECT 1 FROM card_strategies WHERE oracle_id = ? AND strategy = ? AND confidence >= 0.3",
                (row[0], our_strat)
            ).fetchone()
            if strat:
                agreement += 1

        if in_db > 0:
            pct = round(agreement / in_db * 100, 1)
            results[theme] = {"edhrec_cards": in_db, "agreement": agreement, "pct": pct}
            total_checked += in_db
            total_agreement += agreement

    conn.close()
    overall = round(total_agreement / max(total_checked, 1) * 100, 1)
    return {"per_theme": results, "total_checked": total_checked, "agreement": total_agreement, "pct": overall}


def print_report(spellbook, curated, keywords, edhrec):
    """Print full validation report."""
    print("=" * 70)
    print("SYSTEM VALIDATION REPORT")
    print("=" * 70)

    # 1. Spellbook
    print(f"\n{'─' * 70}")
    print(f"1. SPELLBOOK COMBO RECALL")
    print(f"   Of {spellbook['total_combos']} known 2-card combos, we detect {spellbook['detected']}:")
    print(f"     Direct tag match:    {spellbook['detected_direct']}")
    print(f"     Semantic bridge:     {spellbook['detected_bridge']}")
    print(f"     Ability-based:       {spellbook['detected_ability']}")
    print(f"     MISSED:              {spellbook['missed']}")
    print(f"   ► RECALL: {spellbook['recall_pct']}%")
    if spellbook["missed_examples"]:
        print(f"\n   Top missed combos:")
        for names, result in spellbook["missed_examples"][:5]:
            print(f"     {' + '.join(names)}")
            print(f"       → {result[:80]}")

    # 2. EDHREC
    print(f"\n{'─' * 70}")
    print(f"2. EDHREC THEME AGREEMENT")
    print(f"   Of {edhrec['total_checked']} high-synergy cards across themes, {edhrec['agreement']} match our strategies")
    print(f"   ► AGREEMENT: {edhrec['pct']}%")
    print(f"\n   Per theme:")
    for theme, data in sorted(edhrec.get("per_theme", {}).items(), key=lambda x: -x[1]["pct"]):
        bar_len = round(data["pct"] / 5)
        bar = "█" * bar_len + "░" * (20 - bar_len)
        print(f"     {theme:<20} {bar} {data['pct']:5.1f}% ({data['agreement']}/{data['edhrec_cards']})")

    # 3. Curated pairs
    print(f"\n{'─' * 70}")
    print(f"3. CURATED SYNERGY PAIR RECALL")
    print(f"   Of {curated['total']} hand-verified pairs, {curated['detected']} have synergy edges")
    print(f"   ► RECALL: {curated['recall_pct']}%")
    for deck, data in curated.get("per_deck", {}).items():
        status = "✓" if data["recall_pct"] >= 80 else "✗"
        print(f"     {status} {deck:<15} {data['detected']}/{data['total']} ({data['recall_pct']}%)")
        if data["missed_pairs"]:
            for a, b, reason, why in data["missed_pairs"][:3]:
                print(f"       MISS: {a} + {b} — {reason} [{why}]")

    # 4. Keywords
    print(f"\n{'─' * 70}")
    print(f"4. KEYWORD EFFECT TAG COVERAGE")
    print(f"   Of {keywords['total_keywords']} unique keywords, {keywords['tagged']} have effect_tags")
    print(f"   ► COVERAGE: {keywords['coverage_pct']}%")
    if keywords["top_untagged"]:
        print(f"\n   Top untagged keywords (by frequency):")
        for kw, count in keywords["top_untagged"][:15]:
            print(f"     {kw:<25} ({count} cards)")

    # Summary
    print(f"\n{'═' * 70}")
    print(f"SUMMARY")
    print(f"  Spellbook combo recall:   {spellbook['recall_pct']}%")
    print(f"  EDHREC theme agreement:   {edhrec['pct']}%")
    print(f"  Curated pair recall:      {curated['recall_pct']}%")
    print(f"  Keyword coverage:         {keywords['coverage_pct']}%")
    print(f"{'═' * 70}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Validate synergy system against ground truth")
    parser.add_argument("--spellbook", action="store_true", help="Spellbook combo recall only")
    parser.add_argument("--curated", action="store_true", help="Curated pair recall only")
    parser.add_argument("--keywords", action="store_true", help="Keyword coverage only")
    parser.add_argument("--edhrec", action="store_true", help="EDHREC theme agreement only")
    parser.add_argument("--db", default=None, help="DB path")
    args = parser.parse_args()

    db = args.db or DB_PATH
    run_all = not (args.spellbook or args.curated or args.keywords or args.edhrec)

    if args.spellbook or run_all:
        print("Computing Spellbook combo recall...")
        spellbook = validate_spellbook_recall(db)
    else:
        spellbook = {"total_combos": 0, "detected": 0, "detected_direct": 0,
                     "detected_bridge": 0, "detected_ability": 0, "missed": 0,
                     "recall_pct": 0, "missed_examples": []}

    if args.curated or run_all:
        print("Computing curated pair recall...")
        curated = validate_curated_pairs(db)
    else:
        curated = {"total": 0, "detected": 0, "recall_pct": 0, "per_deck": {}}

    if args.keywords or run_all:
        print("Computing keyword coverage...")
        keywords = validate_keyword_coverage(db)
    else:
        keywords = {"total_keywords": 0, "tagged": 0, "untagged": 0,
                    "coverage_pct": 0, "top_untagged": [], "tagged_list": []}

    if args.edhrec or run_all:
        print("Computing EDHREC theme agreement...")
        edhrec = validate_edhrec_theme_agreement(db)
    else:
        edhrec = {"total_checked": 0, "agreement": 0, "pct": 0, "per_theme": {}}

    print()
    print_report(spellbook, curated, keywords, edhrec)
