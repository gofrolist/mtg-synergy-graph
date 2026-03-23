"""Commander affinity scoring for candidate cards."""
import re

from mtg_synergy.constants import SEMANTIC_BRIDGES


def _compute_commander_affinity(commander_card: dict, candidate_cards: list[dict],
                                db_path: str = None) -> dict:
    """Compute commander-specific affinity for each candidate.

    Three signals:
    1. Tag overlap: provides/wants connections (direct + bridges)
    2. Oracle text: candidate references the same mechanics/creature types
    3. Keyword synergy: shared or complementary keywords

    Returns {candidate_name: affinity_score}.
    """
    if not commander_card:
        return {}

    cmdr_provides = set(commander_card.get("provides", []))
    cmdr_wants = set(commander_card.get("wants", []))
    cmdr_oracle = (commander_card.get("oracle_text") or "").lower()
    cmdr_keywords = {k.lower() for k in (commander_card.get("keywords") or [])}

    # Extract key concepts from commander's oracle text
    cmdr_concepts = set()

    _CONCEPT_WORDS = [
        "human", "goblin", "elf", "zombie", "vampire", "dragon", "angel",
        "demon", "sliver", "artifact", "enchantment", "equipment", "aura",
        "instant", "sorcery", "planeswalker", "land", "token", "counter",
        "poison", "infect", "mill", "draw", "discard", "sacrifice", "exile",
        "return", "graveyard", "library", "damage", "life", "mana", "untap",
        "tap", "equip", "enchant", "proliferate", "toxic", "attack", "combat",
        "enters", "dies", "cast",
    ]

    # Pre-compile concept regexes once
    _concept_regexes = {w: re.compile(r'\b' + w + r's?\b') for w in _CONCEPT_WORDS}
    _reminder_re = re.compile(r'\([^)]*\)')

    for word, rx in _concept_regexes.items():
        if rx.search(cmdr_oracle):
            cmdr_concepts.add(word)

    # Pre-compile concept patterns for candidate matching
    cmdr_concept_rxs = [(c, _concept_regexes[c]) for c in cmdr_concepts]

    # Load bridges (cached at module level would be better, but this is called rarely)
    bridge_provides = {}
    for (p_tag, w_tag), weight in SEMANTIC_BRIDGES.items():
        bridge_provides.setdefault(w_tag, []).append((p_tag, weight))

    # Pre-compute bridge lookups for commander wants/provides
    cmdr_want_bridges = {}
    for want in cmdr_wants:
        cmdr_want_bridges[want] = bridge_provides.get(want, [])
    cmdr_provide_bridge_targets = {}
    for (p_tag, w_tag), weight in SEMANTIC_BRIDGES.items():
        if p_tag in cmdr_provides:
            cmdr_provide_bridge_targets.setdefault(w_tag, []).append((p_tag, weight))

    affinities = {}
    for card in candidate_cards:
        name = card["name"]
        card_provides = set(card.get("provides", []))
        card_wants = set(card.get("wants", []))

        score = 0.0

        # Signal 1: Tag connections (direct + bridges)
        score += 3.0 * len(card_provides & cmdr_wants)
        score += 3.0 * len(card_wants & cmdr_provides)
        # Best bridge per commander want
        for want, bridges in cmdr_want_bridges.items():
            best = 0.0
            for p_tag, weight in bridges:
                if p_tag in card_provides and weight > best:
                    best = weight
            score += best * 1.5
        # Best bridge per card want matching commander provides
        for want in card_wants:
            bridges = cmdr_provide_bridge_targets.get(want, [])
            if bridges:
                best = max(w for _, w in bridges)
                score += best * 1.5

        # Signal 2: Oracle text concept overlap (strip reminder text)
        if cmdr_concept_rxs:
            card_oracle = (card.get("oracle_text") or "").lower()
            card_oracle_stripped = _reminder_re.sub('', card_oracle)
            if card_oracle_stripped:
                concept_matches = sum(1 for _, rx in cmdr_concept_rxs if rx.search(card_oracle_stripped))
                if concept_matches > 0:
                    score += concept_matches ** 1.5

        # Signal 3: Keyword synergy
        card_keywords = {k.lower() for k in (card.get("keywords") or [])}
        n_shared = len(card_keywords & cmdr_keywords)
        if n_shared:
            score += n_shared * 0.5

        affinities[name] = score

    return affinities
