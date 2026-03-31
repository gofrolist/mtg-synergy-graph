#!/usr/bin/env python3
"""Validate top recommendations for suspicious false positives.

Runs recommendations for top commanders and flags cards that look wrong
based on mechanical heuristics (wrong counter types, wrong tribal, etc.).

Usage:
    python3 scripts/validate_recommendations.py                # top 10 commanders
    python3 scripts/validate_recommendations.py --top 20       # top 20
    python3 scripts/validate_recommendations.py --commander "Kyler, Sigardian Emissary"
"""
import argparse
import sqlite3
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from mtg_synergy.config import DB_PATH


def get_top_commanders(conn, n=10):
    """Get top N commanders by EDHREC deck count."""
    from fetch_edhrec_all import name_to_slug

    # Build slug→name mapping from all legendary cards (creatures, planeswalkers, vehicles, etc.)
    slug_map = {}
    for (name,) in conn.execute("SELECT name FROM cards WHERE type_line LIKE '%Legendary%'"):
        slug = name_to_slug(name)
        slug_map[slug] = name

    rows = conn.execute("""
        SELECT commander_slug, COUNT(*) as cnt
        FROM edhrec_card_synergy
        GROUP BY commander_slug
        ORDER BY cnt DESC
        LIMIT ?
    """, (n,)).fetchall()

    return [slug_map[slug] for slug, _ in rows if slug in slug_map]


def check_card(card_name, card_oid, card_type, card_text, card_profile,
               cmdr_name, cmdr_profile, cmdr_subtypes, cmdr_strategies):
    """Check a recommended card for suspicious patterns. Returns list of warnings."""
    warnings = []
    card_counters = card_profile.get("counter_types", set())
    card_verbs = card_profile.get("verbs", set())
    cmdr_counters = cmdr_profile.get("counter_types", set())

    # 1. Wrong counter type (exclude generic counter movers that work with any type)
    generic_counter_types = {"All", "Any", "EachFromSource", "EachType"}
    specific_card_counters = card_counters - generic_counter_types
    if "P1P1" in cmdr_counters and specific_card_counters:
        if "P1P1" not in specific_card_counters:
            warnings.append(f"WRONG_COUNTER: card uses {specific_card_counters} counters, commander wants P1P1")

    # 2. Counter verbs but wrong specific counter type
    counter_verbs = card_verbs & {"PutCounter", "PutCounterAll", "Proliferate", "MoveCounter"}
    if "P1P1" in cmdr_counters and counter_verbs and specific_card_counters and "P1P1" not in specific_card_counters:
        warnings.append(f"COUNTER_VERB_MISMATCH: has {counter_verbs} but puts {specific_card_counters}")

    # 3. Doctor's companion without Doctor commander
    if card_text and "Doctor's companion" in card_text:
        if "Doctor" not in cmdr_name and "Doctor" not in card_type:
            warnings.append("DOCTORS_COMPANION: card is Doctor's companion but commander isn't a Doctor")

    # 4. Partner / background mismatch
    if card_text and "Choose a Background" in card_text and "Background" not in card_type:
        warnings.append("BACKGROUND_REQ: card requires a Background but isn't one")

    # 5. Tribal mismatch - card requires specific creature subtypes
    #    Filter out card types, targeting terms, and game mechanics — only check creature subtypes
    card_req_subs = card_profile.get("required_subtypes", set())
    non_tribal = {"card", "creature", "permanent", "self", "other", "nontoken",
                  "token", "artifact", "enchantment", "land", "spell", "any",
                  "instant", "sorcery", "planeswalker", "battle", "aura", "equipment",
                  "historic", "legendary", "snow", "food", "clue", "treasure", "blood",
                  "opponent", "you", "youown", "youctrl", "youdontctrl", "oppown",
                  "player", "chosencard", "chosentype", "chosencolor",
                  "emblem", "iscommander", "isremembered", "istriggerremembered",
                  "hascounters", "hascardsinhand_card_eq0", "thisturnentered",
                  "equippedby", "enchantedby", "attachedby", "pairedwith",
                  "controlledby", "notdefinedtargeted", "triggereddefender", "toplibrary",
                  "blue", "black", "red", "green", "white",
                  "swamp", "island", "mountain", "forest", "plains",
                  "outlaw"}
    creature_req = card_req_subs - non_tribal
    # Case-insensitive comparison (Forge uses lowercase, cards use Title Case)
    creature_req_lower = {s.lower() for s in creature_req}
    cmdr_subtypes_lower = {s.lower() for s in cmdr_subtypes}
    if creature_req_lower and cmdr_subtypes_lower and not (creature_req_lower & cmdr_subtypes_lower):
        warnings.append(f"TRIBAL_MISMATCH: card requires {creature_req}, commander is {cmdr_subtypes}")

    # 6. Card excludes commander's creature type
    excl_subs = card_profile.get("excluded_subtypes", set())
    if excl_subs and cmdr_subtypes and (excl_subs & cmdr_subtypes):
        warnings.append(f"EXCLUDED_TYPE: card excludes {excl_subs & cmdr_subtypes}")

    # 7. Suspend / time counter cards in non-suspend commanders
    if card_counters == {"TIME"} and "TIME" not in cmdr_counters:
        if "suspend" not in (card_text or "").lower() or "suspend" not in str(cmdr_strategies).lower():
            warnings.append("TIME_COUNTER: card uses time counters, commander doesn't")

    # 8. "Experience counter" cards for non-experience commanders
    if "EXPERIENCE" in card_counters and "EXPERIENCE" not in cmdr_counters:
        warnings.append("EXPERIENCE_COUNTER: card uses experience counters, commander doesn't")

    # 9. Energy counter mismatch
    if "ENERGY" in card_counters and "ENERGY" not in cmdr_counters:
        if "energy" not in (card_text or "").lower():
            pass  # energy cards sometimes work generically
        else:
            warnings.append("ENERGY_COUNTER: card uses energy, commander doesn't")

    # 10. Token type mismatch for tribal commanders
    token_script = card_profile.get("token_subtypes", set())
    if token_script and cmdr_subtypes and not (token_script & cmdr_subtypes):
        if cmdr_profile.get("trigger_filters", set()) & cmdr_subtypes:
            warnings.append(f"WRONG_TOKEN: creates {token_script} tokens, commander cares about {cmdr_subtypes}")

    return warnings


def validate_commanders(conn, commanders, ctx, all_recs, top_n=30, quiet=False):
    """Validate recommendations for a list of commanders.

    all_recs: dict[commander_name -> list[(card_name, score)]] from batch_recommend.
    Returns list of (cmdr_name, issues) tuples.
    """
    results = []
    for cmdr_name in commanders:
        recs = all_recs.get(cmdr_name, [])
        if not recs:
            continue

        cmdr_row = conn.execute(
            "SELECT oracle_id, type_line FROM cards WHERE name = ?", (cmdr_name,)
        ).fetchone()
        if not cmdr_row:
            continue
        cmdr_oid = cmdr_row[0]

        cmdr_profile = ctx._forge_profiles.get(cmdr_oid, {})
        from mtg_synergy.config import extract_subtypes
        cmdr_type = cmdr_row[1] or ""
        cmdr_subtypes = extract_subtypes(cmdr_type)

        cmdr_strats = ctx.card_strats.get(cmdr_oid, set())

        issues = []
        for rank, (name, score) in enumerate(recs, 1):
            card_row = conn.execute(
                "SELECT oracle_id, type_line, oracle_text FROM cards WHERE name = ?",
                (name,)
            ).fetchone()
            if not card_row:
                continue
            card_oid, card_type, card_text = card_row
            card_profile = ctx._forge_profiles.get(card_oid, {})

            warnings = check_card(
                name, card_oid, card_type or "", card_text or "", card_profile,
                cmdr_name, cmdr_profile, cmdr_subtypes, cmdr_strats
            )
            if warnings:
                issues.append((rank, name, score, warnings))

        results.append((cmdr_name, issues))
    return results


def main():
    parser = argparse.ArgumentParser(description="Validate recommendations for suspicious cards")
    parser.add_argument("--commander", type=str, help="Single commander to validate")
    parser.add_argument("--top", type=int, default=10, help="Number of top commanders to check")
    parser.add_argument("--recs", type=int, default=30, help="Number of recommendations per commander")
    parser.add_argument("-q", "--quiet", action="store_true", help="Summary only")
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    if args.commander:
        commanders = [args.commander]
    else:
        commanders = get_top_commanders(conn, args.top)
    print(f"Validating {len(commanders)} commanders, {args.recs} recs each...")

    # Batch-recommend all commanders at once (loads model + context once)
    from mtg_synergy.recommend.scoring import batch_recommend
    all_recs = batch_recommend(conn, commanders, top_n=args.recs, verbose=False)

    # Load shared context for validation checks
    from mtg_synergy.recommend.forge_features import ForgeFeatureContext
    print("Loading validation context...")
    ctx = ForgeFeatureContext(conn, preload_edges=False)

    results = validate_commanders(conn, commanders, ctx, all_recs,
                                  top_n=args.recs, quiet=args.quiet)

    total_issues = 0
    total_recs = len(commanders) * args.recs
    cmdr_summary = []

    for cmdr, issues in results:
        total_issues += len(issues)
        if issues:
            cmdr_summary.append((cmdr, len(issues)))
            if not args.quiet:
                print(f"\n{'='*70}")
                print(f"  {cmdr} — {len(issues)} suspicious card(s)")
                print(f"{'='*70}")
                for rank, name, score, warnings in issues:
                    print(f"  #{rank:>2} {name:<35} score={score:.1f}")
                    for w in warnings:
                        print(f"       -> {w}")

    print(f"\n{'='*70}")
    print(f"SUMMARY: {total_issues} suspicious cards in {total_recs} total recommendations")
    print(f"         ({total_issues/max(total_recs,1)*100:.1f}% flagged)")
    print(f"{'='*70}")
    if cmdr_summary:
        for cmdr, count in sorted(cmdr_summary, key=lambda x: -x[1]):
            print(f"  {count:>2} flags — {cmdr}")
    else:
        print("  No issues found!")

    conn.close()


if __name__ == "__main__":
    main()
