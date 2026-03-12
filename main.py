"""
MTG Deck Optimizer — CLI v0.1
Test: Pick the best card from a candidate pool given a deck context.

Usage:
  python main.py --mode rank_deck      # Score all cards in the deck
  python main.py --mode needle_test    # Remove one card, add distractors, see if model picks it back
"""

import argparse
import sys
from enricher import fetch_card, extract_features, CardFeatures
from scorer import score_card, build_deck_profile
from necessity import necessity_score

# ── DECKLIST ────────────────────────────────────────────────────────────────

DECKLIST = [
    "Greymond, Avacyn's Stalwart", "Hardened Scales", "Luminarch Aspirant",
    "Mikaeus, the Lunarch", "Ozolith, the Shattered Spire", "Pir, Imaginative Rascal",
    "Roaming Throne", "Thalia's Lieutenant", "The Ozolith",
    "Augur of Autumn", "Esper Sentinel", "Garruk's Uprising", "Outcaster Trailblazer",
    "Realmwalker", "The Great Henge", "Tribute to the World Tree",
    "Abzan Falconer", "Champion of Lambholt", "Surrak and Goreclaw", "Tuskguard Captain",
    "Bastion Protector", "Boromir, Warden of the Tower", "Clever Concealment",
    "Everybody Lives!", "Flare of Fortitude", "Flawless Maneuver", "Grand Abolisher",
    "Heroic Intervention", "Mother of Runes", "Saffi Eriksdotter", "Saryth, the Viper's Fang",
    "Sigarda, Font of Blessings", "Slippery Bogbonder", "Spectacular Spider-Man",
    "Swiftfoot Boots", "Sylvan Safekeeper", "Teferi's Protection", "The Wandering Rescuer",
    "Zack Fair", "Wild Beastmaster",
    "Avacyn's Pilgrim", "Biophagus", "Cultivate", "Farseek", "Heronblade Elite",
    "Katilda, Dawnhart Prime", "Nature's Lore", "Rampant Growth", "Sol Ring",
    "Somberwald Sage", "Three Visits",
    "Aura Shards", "Damning Verdict", "Druid of Purification", "Loran of the Third Path",
    "Swords to Plowshares", "Witch Enchanter",
    "Adeline, Resplendent Cathar", "Call the Coppercoats", "Horn of Gondor",
    "Torens, Fist of the Angels", "Wyrm's Crossing Patrol",
    "Ranger-Captain of Eos", "Recruiter of the Guard",
    # Lands (simplified - just a few key ones)
    "Command Tower", "Cavern of Souls", "Gavony Township", "Plaza of Heroes",
    "Boseiju, Who Endures", "Windswept Heath", "Temple Garden",
]

# Needle test: target card to remove + distractor cards (worse fits)
NEEDLE_TARGET = "Roaming Throne"
DISTRACTORS = [
    "Panharmonicon",          # also doubles ETB triggers, but only artifacts/creatures — misses Kyler
    "Lithoform Engine",       # copies triggered abilities but costs {4} + {4}{T} each use
    "Strionic Resonator",     # copies one triggered ability per {2}{T} — reactive, not passive
    "Illusionist's Bracers",  # copies ACTIVATED abilities, not triggered — wrong type entirely
    "Anointed Procession",    # doubles tokens, not counters — wrong axis
    "Mondrak, Glory Dominus", # also doubles tokens, not counters
    "Doubling Season",        # doubles counters AND tokens but CMC 5 and not Human
    "Elesh Norn, Grand Cenobite",  # anthem effect, no counter/trigger synergy
    "Solidarity of Heroes",   # counter doubler but single-target, reactive
]


def fetch_all(card_names: list[str], label: str = "") -> list[CardFeatures]:
    results = []
    total = len(card_names)
    for i, name in enumerate(card_names, 1):
        print(f"  [{i}/{total}] Fetching: {name}")
        data = fetch_card(name)
        if data:
            features = extract_features(data)
            results.append(features)
        else:
            print(f"  [WARN] Could not fetch '{name}' — not in DB, skipping")
    return results


def print_card_result(card: CardFeatures, rank: int = None):
    prefix = f"#{rank:2d}" if rank else "   "
    print(f"\n{prefix}  {card.name}")
    print(f"      Score: {card.final_score:.2f}  (tribal={card.tribal_score:.1f}, "
          f"counter={card.counter_score:.1f}, role={card.role_score:.1f}, "
          f"mult=×{card.commander_multiplier:.1f}, synergy=+{card.synergy_bonus:.1f})")
    print(f"      Role: {card.role} | CMC: {card.cmc} | "
          f"Human: {card.is_human} | Type: {card.type_line}")
    for line in card.explanation:
        print(f"        → {line}")


def mode_rank_deck():
    print("\n═══ MODE: RANK FULL DECK — DUAL SCORE ═══\n")
    print(f"Fetching {len(DECKLIST)} cards...")
    deck_cards = fetch_all(DECKLIST)

    print(f"\nBuilding deck profile...")
    profile = build_deck_profile(deck_cards)
    print(f"  Humans: {profile.human_count} | Ramp: {profile.ramp_count} | "
          f"Draw: {profile.draw_count} | Removal: {profile.removal_count} | "
          f"Protection: {profile.protection_count} | Counter engine: {profile.counter_engine_count}\n")

    # Compute both scores for every card
    results = []
    for card in deck_cards:
        card = score_card(card, deck_cards, profile)
        nec_score, nec_explanation = necessity_score(card, deck_cards, profile)
        results.append((card, nec_score, nec_explanation))

    # Build rank lookups
    by_synergy   = sorted(results, key=lambda x: x[0].final_score, reverse=True)
    by_necessity = sorted(results, key=lambda x: x[1], reverse=True)

    syn_rank = {card.name: i+1 for i, (card, _, _) in enumerate(by_synergy)}
    nec_rank = {card.name: i+1 for i, (card, _, _) in enumerate(by_necessity)}

    # Combined view: sort by average rank (lower = better in both)
    combined = sorted(results,
        key=lambda x: (syn_rank[x[0].name] + nec_rank[x[0].name]) / 2)

    W = 62
    print(f"{'═'*W}")
    print(f"{'CARD':<30} {'SYN':>5} {'NEC':>5}  {'VERDICT'}")
    print(f"{'─'*W}")

    for card, nec_s, _ in combined:
        sr = syn_rank[card.name]
        nr = nec_rank[card.name]
        avg = (sr + nr) / 2

        if sr <= 15 and nr <= 15:
            verdict = "★ CORE — high synergy + high necessity"
        elif sr <= 20 and nr > 40:
            verdict = "↑ SYNERGY — thematic, low staple value"
        elif nr <= 15 and sr > 40:
            verdict = "⚙ STAPLE — necessary, low commander synergy"
        elif sr > 50 and nr > 50:
            verdict = "✂ CUT CANDIDATE"
        else:
            verdict = ""

        syn_str = f"#{sr}"
        nec_str = f"#{nr}"
        print(f"  {card.name:<30} {syn_str:>5} {nec_str:>5}  {verdict}")

    print(f"{'═'*W}")
    print(f"\nSYN = synergy rank (fit with Kyler's strategy)")
    print(f"NEC = necessity rank (functional value in any EDH deck)")

    # Detailed breakdown for cut candidates
    cuts = [(c, ns, ne) for c, ns, ne in combined
            if syn_rank[c.name] > 50 and nec_rank[c.name] > 50]
    if cuts:
        print(f"\n── CUT CANDIDATES (low synergy + low necessity) ──")
        for card, nec_s, nec_exp in cuts:
            print(f"\n  {card.name}  [syn=#{syn_rank[card.name]}, nec=#{nec_rank[card.name]}]")
            print(f"  Synergy breakdown:")
            for line in card.explanation:
                print(f"    → {line}")
            print(f"  Necessity breakdown:")
            for line in nec_exp:
                print(f"    → {line}")


def mode_needle_test():
    print("\n═══ MODE: NEEDLE-IN-HAYSTACK TEST ═══")
    print(f"Target card to find: '{NEEDLE_TARGET}'")
    print(f"Distractors: {DISTRACTORS}\n")

    # Build deck WITHOUT the target card
    deck_without_target = [n for n in DECKLIST if n != NEEDLE_TARGET]

    print(f"Fetching deck context ({len(deck_without_target)} cards)...")
    deck_cards = fetch_all(deck_without_target)

    print(f"\nFetching candidate pool (target + {len(DISTRACTORS)} distractors)...")
    candidates_raw = fetch_all([NEEDLE_TARGET] + DISTRACTORS)

    print(f"\nBuilding deck profile...")
    profile = build_deck_profile(deck_cards)
    print(f"  Humans: {profile.human_count} | Ramp: {profile.ramp_count} | "
          f"Draw: {profile.draw_count} | Removal: {profile.removal_count} | "
          f"Protection: {profile.protection_count} | Counter engine: {profile.counter_engine_count}")

    print(f"\nScoring candidates...")
    # Score each candidate as if it were being added to the deck
    scored_candidates = [score_card(card, deck_cards, profile) for card in candidates_raw]
    scored_candidates.sort(key=lambda c: c.final_score, reverse=True)

    print(f"\n{'═'*60}")
    print(f"CANDIDATE RANKING")
    print(f"{'═'*60}")
    for i, card in enumerate(scored_candidates, 1):
        marker = " ◄ TARGET" if card.name == NEEDLE_TARGET else ""
        print_card_result(card, i)
        if marker:
            print(f"      {marker}")

    winner = scored_candidates[0]
    print(f"\n{'═'*60}")
    if winner.name == NEEDLE_TARGET:
        print(f"✓ TEST PASSED — Model correctly picked '{NEEDLE_TARGET}' as best candidate")
    else:
        print(f"✗ TEST FAILED — Model picked '{winner.name}' instead of '{NEEDLE_TARGET}'")
        target_rank = next((i+1 for i, c in enumerate(scored_candidates) if c.name == NEEDLE_TARGET), None)
        print(f"  '{NEEDLE_TARGET}' ranked #{target_rank}")
    print(f"{'═'*60}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="MTG Deck Optimizer v0.1")
    parser.add_argument(
        "--mode",
        choices=["rank_deck", "needle_test"],
        default="needle_test",
        help="rank_deck: score all cards | needle_test: pick best from candidates",
    )
    args = parser.parse_args()

    if args.mode == "needle_test":
        mode_needle_test()
    else:
        mode_rank_deck()
