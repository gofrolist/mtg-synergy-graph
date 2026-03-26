"""Card swap suggestion engine with multi-layer protection."""
import json
import sqlite3

from mtg_synergy.recommend.engine import _deck_card_scores, _candidate_scores
from mtg_synergy.recommend.scoring import DeckContext, score_all_candidates
from mtg_synergy.combos.detector import compute_strategy_relevance

# Provides tags that indicate synergy value (okay to keep as "spell").
# Uses Forge-derived vocabulary (verb→provides mapping from derive_forge_tags.py).
SYNERGY_PROVIDES = {
    "token", "token-treasure", "put-counter", "put-counter-all",
    "sacrifice-outlet", "pump", "pump-all", "deal-damage",
    "gain-life", "lose-life", "mill", "reanimate", "copy-permanent",
    "proliferate", "amass", "connive", "explore", "animate",
}

# Alias used inside _classify_card_slot to keep the function readable
_SYNERGY_PROVIDES = SYNERGY_PROVIDES


def _classify_card_slot(name: str, cards: list[dict],
                        deck_types: set = None) -> str:
    """Classify a card as 'land', 'staple', or 'spell' for swap bucketing.

    Lands swap with lands, staples are protected, spells swap with spells.
    Protected from cuts: removal, protection, ramp, card draw, commander's tribe.
    """
    from card_db import CARD_DB, NAME_INDEX

    INFRASTRUCTURE_ROLES = {"removal", "ramp", "protection", "draw", "tutor"}

    # Provides tags that indicate infrastructure function (should be protected)
    INFRASTRUCTURE_PROVIDES = {
        "destroy", "destroy-all", "counter-spell", "exile", "remove",
        "force-sacrifice", "mana", "reduce-cost", "draw",
        "dig", "scry", "surveil", "fog", "prevent-damage",
        "hexproof", "indestructible", "ward",
    }

    # Provides tags that indicate synergy value (okay to keep as "spell")
    SYNERGY_PROVIDES = _SYNERGY_PROVIDES

    card_data = None
    for card in cards:
        if card["name"] == name:
            card_data = card
            break

    if card_data:
        role = card_data.get("role", "")
        type_line = card_data.get("type_line", "")
        provides = set(card_data.get("provides", []))

        # Land detection
        if role == "land" or "Land" in type_line:
            return "land"

        # Role-based protection
        if role in INFRASTRUCTURE_ROLES:
            if provides & SYNERGY_PROVIDES:
                pass  # Has synergy value, classify as spell
            else:
                return "staple"

        # Provides-based protection: cards providing removal/protection/ramp
        if provides & INFRASTRUCTURE_PROVIDES:
            if not (provides & SYNERGY_PROVIDES):
                return "staple"

        # Tribal protection: creatures matching the deck's tribal type
        # A Human in a Human deck shouldn't be cut for a non-Human
        # Changelings/Shapeshifters count as all types
        if deck_types and type_line:
            if "Shapeshifter" in type_line or "Changeling" in type_line:
                return "staple"
            for dt in deck_types:
                if dt in type_line:
                    return "staple"

    # Fallback: Scryfall type_line
    oid = NAME_INDEX.get(name.lower())
    if oid and oid in CARD_DB:
        type_line = CARD_DB[oid].get("type_line", "")
        if "Land" in type_line:
            return "land"

    return "spell"


def suggest_swaps(graph: dict, deck_cards: set[str], commander: str,
                  cards: list[dict], top_n: int = 15,
                  active_strategies: set = None, db_path: str = None,
                  deck_types: set = None, edhrec_slug: str = None,
                  color_identity: set = None) -> list[dict]:
    """Suggest swaps: pair weak deck cards with strong non-deck candidates.

    Lands swap with lands, spells swap with spells. Commander and staple
    infrastructure (mana rocks, removal, protection) are never cut.
    Uses the same dynamic feature-based scoring as recommend.
    """
    deck_scores = _deck_card_scores(graph, deck_cards)
    cand_scores = _candidate_scores(graph, deck_cards, commander=commander)

    # Apply dynamic scoring to candidates (same as recommend)
    if commander and db_path:
        import sqlite3 as _sq_swap
        try:
            _sc = _sq_swap.connect(db_path)
            ctx = DeckContext(_sc, commander, deck_cards, cards,
                              deck_types=deck_types, active_strategies=active_strategies,
                              edhrec_slug=edhrec_slug)
            score_all_candidates(cand_scores, cards, ctx, _sc, verbose=False)

            # Also score deck cards so cut/add comparison uses the same scale
            from mtg_synergy.recommend.scoring import compute_dynamic_score
            for card_name, info in deck_scores.items():
                if card_name == commander:
                    continue
                cd = next((c for c in cards if c["name"] == card_name), None)
                if cd:
                    features = compute_dynamic_score(card_name, cd, ctx, _sc)
                    info["total"] = features["total"]

            _sc.close()
        except Exception:
            pass

    # Build card metadata for strategy/anti-synergy checks
    card_oid_lookup = {}
    card_meta = {}
    for c in cards:
        card_oid_lookup[c["name"]] = c.get("oracle_id", "")
        card_meta[c["name"]] = {
            "type_line": c.get("type_line", ""),
            "cmc": c.get("cmc", 0),
        }

    # Annotate strategy relevance for display (not used in scoring —
    # LLM scoring already accounts for card quality)
    if active_strategies and db_path:
        for card_name, info in cand_scores.items():
            oid = card_oid_lookup.get(card_name, "")
            if oid:
                rel = compute_strategy_relevance(oid, active_strategies, db_path)
                info["strategy_rel"] = rel

    # Check which deck cards have zero strategy overlap (cut priority)
    anti_synergy_cards = set()
    if active_strategies and db_path:
        import sqlite3 as _sq
        _conn = _sq.connect(db_path)
        for card_name in deck_cards:
            oid = card_oid_lookup.get(card_name, "")
            if not oid:
                continue
            card_strats = {r[0] for r in _conn.execute(
                "SELECT strategy FROM card_strategies WHERE oracle_id = ? AND confidence >= 0.3",
                (oid,)
            ).fetchall()}
            if not (card_strats & active_strategies):
                anti_synergy_cards.add(card_name)
        _conn.close()

    # Protect cards with high commander synergy from cuts.
    # Cards that strongly synergize with the commander are key pieces.
    commander_protected = set()
    cmdr_adj = graph["adjacency"].get(commander, [])
    if cmdr_adj:
        cmdr_edge_scores = {e["target"]: e["score"] for e in cmdr_adj if e["target"] in deck_cards}
        if cmdr_edge_scores:
            # Protect top 20 commander-synergy cards (generous — these are key pieces)
            n_protect = min(20, len(cmdr_edge_scores))
            threshold = sorted(cmdr_edge_scores.values(), reverse=True)[n_protect - 1]
            commander_protected = {name for name, score in cmdr_edge_scores.items() if score >= threshold}

    # Protect cards with high mechanics synergy with the commander.
    try:
        from mechanics_matcher import load_mechanics, compute_synergy
        _all_mechs = load_mechanics(db_path) if db_path else {}
        cmdr_oid_swap = card_oid_lookup.get(commander, "")
        cmdr_mechs_swap = _all_mechs.get(cmdr_oid_swap, [])
        cmdr_type_swap = card_meta.get(commander, {}).get("type_line", "")
        if cmdr_mechs_swap:
            for card_name in deck_cards:
                if card_name == commander:
                    continue
                card_oid_s = card_oid_lookup.get(card_name, "")
                card_mechs_s = _all_mechs.get(card_oid_s, [])
                if card_mechs_s:
                    ms = compute_synergy(cmdr_mechs_swap, card_mechs_s,
                                         cmdr_type_swap, card_meta.get(card_name, {}).get("type_line", ""))
                    if ms >= 2.0:  # Strong mechanics match = protected
                        commander_protected.add(card_name)
    except Exception:
        pass

    # Protect cards in known Spellbook combos with the commander.
    combo_protected = set()
    if db_path:
        import sqlite3 as _sq2
        _cc = _sq2.connect(db_path)
        cmdr_oid = card_oid_lookup.get(commander, "")
        if cmdr_oid:
            # Find combos including the commander
            combo_ids = [r[0] for r in _cc.execute(
                "SELECT combo_id FROM spellbook_combo_cards WHERE oracle_id = ?", (cmdr_oid,))]
            for cid in combo_ids:
                combo_card_oids = {r[0] for r in _cc.execute(
                    "SELECT oracle_id FROM spellbook_combo_cards WHERE combo_id = ?", (cid,))}
                # If all combo cards are in our deck, protect them all
                combo_names = set()
                for coid in combo_card_oids:
                    name_row = _cc.execute("SELECT name FROM cards WHERE oracle_id = ?", (coid,)).fetchone()
                    if name_row:
                        combo_names.add(name_row[0])
                if combo_names.issubset(deck_cards):
                    combo_protected.update(combo_names - {commander})
        _cc.close()

    # Classify every deck card and candidate by slot type
    deck_slots = {name: _classify_card_slot(name, cards, deck_types=deck_types) for name in deck_cards}
    cand_slots = {name: _classify_card_slot(name, cards, deck_types=deck_types) for name in cand_scores}

    # Split into buckets: lands vs spells (protected cards excluded from cuts)
    cuttable = {"land": [], "spell": []}
    for card, info in deck_scores.items():
        slot = deck_slots.get(card, "spell")
        if card == commander or slot == "staple":
            continue
        if card in commander_protected:
            continue  # High commander synergy — don't cut
        if card in combo_protected:
            continue  # Part of a known combo — don't cut
        info["anti_synergy"] = card in anti_synergy_cards
        cuttable[slot].append((card, info))

    # Sort cuttable: anti-synergy cards first (worst cuts), then by lowest synergy
    for bucket in cuttable.values():
        bucket.sort(key=lambda x: (not x[1].get("anti_synergy", False), x[1]["total"]))

    candidate_lists = {"land": [], "spell": []}
    for card, info in sorted(cand_scores.items(), key=lambda x: x[1]["total"], reverse=True):
        slot = cand_slots.get(card, "spell")
        if slot == "staple":
            slot = "spell"
        candidate_lists[slot].append((card, info))

    # Pair within each bucket
    used_candidates = set()
    swaps = []

    for bucket in ("spell", "land"):
        for cut_card, cut_info in cuttable[bucket]:
            if len(swaps) >= top_n * 2:
                break

            for add_card, add_info in candidate_lists[bucket]:
                if add_card in used_candidates:
                    continue
                net = add_info["total"] - cut_info["total"]
                if net <= 0:
                    break

                used_candidates.add(add_card)
                top_partners = sorted(add_info["partners"], key=lambda x: x[1], reverse=True)
                swaps.append({
                    "cut": cut_card,
                    "cut_score": round(cut_info["total"], 1),
                    "cut_partners": cut_info["partners"],
                    "cut_anti_synergy": cut_info.get("anti_synergy", False),
                    "slot": bucket,
                    "add": add_card,
                    "add_score": round(add_info["total"], 1),
                    "add_partners": len(add_info["partners"]),
                    "add_multi_sig": add_info["multi_sig"],
                    "add_top_partners": top_partners[:5],
                    "add_strategy_rel": add_info.get("strategy_rel"),
                    "add_tower": add_info.get("tower_score"),
                    "add_mechanics": add_info.get("mechanics_score"),
                    "add_edhrec_syn": add_info.get("edhrec_syn"),
                    "net_delta": round(net, 1),
                })
                break

    swaps.sort(key=lambda s: s["net_delta"], reverse=True)
    return swaps[:top_n]


def show_swaps(swaps: list[dict], top_n: int = 15):
    """Display suggested swaps with strategy annotations."""
    print(f"\n{'═' * 70}")
    print(f"SUGGESTED SWAPS — {len(swaps)} upgrades found")
    print(f"{'═' * 70}")

    if not swaps:
        print("  No beneficial swaps found.")
        return

    # Normalize net_delta to percentage (best swap = 100%)
    max_delta = max(s["net_delta"] for s in swaps) if swaps else 1.0
    if max_delta <= 0:
        max_delta = 1.0

    for i, swap in enumerate(swaps[:top_n], 1):
        pct = round(swap["net_delta"] / max_delta * 100, 1)
        bar_len = round(pct / 5)
        bar = "█" * bar_len + "░" * (20 - bar_len)
        slot_label = f" [land]" if swap.get("slot") == "land" else ""
        anti = " ← no strategy match" if swap.get("cut_anti_synergy") else ""
        strat_rel = swap.get("add_strategy_rel")
        strat_str = f" [strat×{strat_rel:.1f}]" if strat_rel and strat_rel != 1.0 else ""

        tower_str = f" T={swap['add_tower']}" if swap.get("add_tower") else ""
        mech_str = f" M={swap['add_mechanics']}" if swap.get("add_mechanics") else ""
        edh_str = f" EDH={swap['add_edhrec_syn']:.2f}" if swap.get("add_edhrec_syn") else ""
        print(f"\n  {pct:5.1f}% {bar}{slot_label}")
        print(f"    OUT: {swap['cut']:<35} (score: {swap['cut_score']:>7.1f}, "
              f"{swap['cut_partners']} partners){anti}")
        print(f"     IN: {swap['add']:<35} (score: {swap['add_score']:>7.1f}, "
              f"{swap['add_partners']} partners){strat_str}{tower_str}{mech_str}{edh_str}")
        if swap["add_top_partners"]:
            top = swap["add_top_partners"][:3]
            partners_str = ", ".join(
                f"{p[0]} ({p[1]:.1f})" for p in top
            )
            print(f"         top synergies: {partners_str}")
