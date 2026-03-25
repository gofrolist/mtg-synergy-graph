"""Dynamic feature-based scoring — computes synergy at recommendation time.

Replaces static LLM scores with a weighted combination of:
  tower model, mechanics, tag overlap, strategy, tribal, rank, EDHREC,
  and strategy-specific keyword matching.

All features are computed on the fly from the DB and models, so scores
adapt to the current deck composition and never go stale.
"""
import math
import os

from mtg_synergy.config import DATA_DIR, SCORING_WEIGHTS

# Tags excluded from overlap (too broad to be discriminative)
OVERLAP_EXCLUDE = {"board-generic"}

# Strategy keyword maps: oracle text patterns that indicate real synergy
# with each strategy (not just generic triggers).
STRATEGY_KEYWORDS = {
    "+1/+1-counters": ["+1/+1 counter", "proliferate", "+1/+1 counters on",
                       "counter equal", "counter among", "modify", "modified",
                       "twice that many", "additional +1"],
    "tokens": ["create a", "create two", "create x", "token creature", "populate", "amass"],
    "spellslinger": ["instant or sorcery", "noncreature spell", "magecraft",
                     "prowess", "storm", "copy a spell", "copies of"],
    "aristocrats": ["whenever a creature dies", "whenever another creature",
                    "sacrifice a creature", "sacrifice a permanent", "blood artist"],
    "equipment": ["equip", "equipment enters", "equipped creature", "attach"],
    "auras": ["enchant creature", "enchanted creature", "constellation", "aura"],
    "enchantress": ["enchantment enters", "constellation", "whenever you cast an enchantment"],
    "landfall": ["landfall", "whenever a land enters", "land you control enters"],
    "reanimator": ["return target creature card from your graveyard",
                   "put a creature card from a graveyard", "unearth", "reanimate"],
    "mill": ["mill", "cards from the top of", "into your graveyard from your library"],
    "self-mill": ["mill", "cards from the top of your library"],
    "artifacts": ["artifact enters", "affinity for artifacts", "metalcraft", "improvise"],
    "voltron": ["equipped creature gets", "enchanted creature gets", "hexproof",
                "indestructible", "double strike", "commander damage"],
    "lifegain": ["gain life", "lifelink", "whenever you gain life"],
    "lifedrain": ["each opponent loses", "drain", "deals damage to each opponent"],
    "go-wide": ["create a 1/1", "create two", "create x", "for each creature you control"],
    "blink": ["exile target creature you control, then return",
              "exile any number of target creatures you control",
              "flicker", "enters, you may"],
    "wheels": ["each player discards", "wheel", "each player draws"],
    "proliferate": ["proliferate", "+1/+1 counter"],
    "burn": ["deals damage to any target", "deals damage to each opponent",
             "whenever.*deals damage"],
    "stax": ["can't cast", "costs .* more to cast", "each opponent sacrifices",
             "opponents can't"],
}


class DeckContext:
    """Pre-computed deck-level data for scoring candidates against."""

    def __init__(self, conn, commander: str, deck_cards: set, cards: list,
                 deck_types: set = None, active_strategies: set = None,
                 edhrec_slug: str = None):
        self.commander = commander
        self.deck_cards = deck_cards
        self.deck_types = deck_types or set()
        self.active_strategies = active_strategies or set()
        self.is_tribal = bool(deck_types)

        # Collect strategy-relevant keywords for the active strategies
        self.strategy_keywords = []
        for strat in self.active_strategies:
            self.strategy_keywords.extend(STRATEGY_KEYWORDS.get(strat, []))

        # Commander OID
        self.cmdr_oid = ""
        for c in cards:
            if c["name"] == commander:
                self.cmdr_oid = c.get("oracle_id", "")
                break

        # Card OID lookup
        self.card_oid = {}
        for c in cards:
            self.card_oid[c["name"]] = c.get("oracle_id", "")

        # Commander tags
        self.cmdr_provides = set()
        self.cmdr_wants = set()
        if self.cmdr_oid:
            self.cmdr_provides = set(
                r[0] for r in conn.execute(
                    "SELECT tag FROM provides WHERE oracle_id = ?", (self.cmdr_oid,))
            ) - OVERLAP_EXCLUDE
            self.cmdr_wants = set(
                r[0] for r in conn.execute(
                    "SELECT tag FROM wants WHERE oracle_id = ?", (self.cmdr_oid,))
            ) - OVERLAP_EXCLUDE

        # Deck-wide tags (union of all deck card provides/wants)
        self.deck_provides = set()
        self.deck_wants = set()
        deck_oids = [self.card_oid.get(n, "") for n in deck_cards if self.card_oid.get(n)]
        for i in range(0, len(deck_oids), 500):
            chunk = deck_oids[i:i + 500]
            ph = ",".join("?" * len(chunk))
            for r in conn.execute(
                f"SELECT tag FROM provides WHERE oracle_id IN ({ph})", chunk
            ).fetchall():
                self.deck_provides.add(r[0])
            for r in conn.execute(
                f"SELECT tag FROM wants WHERE oracle_id IN ({ph})", chunk
            ).fetchall():
                self.deck_wants.add(r[0])
        self.deck_provides -= OVERLAP_EXCLUDE
        self.deck_wants -= OVERLAP_EXCLUDE

        # EDHREC synergy map (DFC-aware)
        self.edhrec = {}
        if edhrec_slug:
            has = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='edhrec_card_synergy'"
            ).fetchone()[0]
            if has:
                raw = {}
                for r in conn.execute(
                    "SELECT card_name, synergy FROM edhrec_card_synergy WHERE commander_slug = ?",
                    (edhrec_slug,)):
                    raw[r[0]] = r[1]
                dfc_map = {}
                for r in conn.execute("SELECT name FROM cards WHERE name LIKE '%//%'"):
                    dfc_map[r[0].split(" // ")[0]] = r[0]
                for name, syn in raw.items():
                    self.edhrec[name] = syn
                    dfc = dfc_map.get(name)
                    if dfc and dfc not in self.edhrec:
                        self.edhrec[dfc] = syn

        # Forge DeckHas/DeckHints tags (cached for all candidates)
        self.forge_cmdr_has = set()
        self.forge_cmdr_hints = set()
        try:
            for r in conn.execute(
                "SELECT tag_type, tag FROM forge_deck_tags WHERE card_name = ?",
                (commander,)).fetchall():
                if r[0] == "has":
                    self.forge_cmdr_has.add(r[1])
                elif r[0] == "hints":
                    self.forge_cmdr_hints.add(r[1])
        except Exception:
            pass

        # Bulk-load all candidate forge tags
        self.forge_card_has = {}   # {card_name: set of tags}
        self.forge_card_hints = {} # {card_name: set of tags}
        try:
            for r in conn.execute("SELECT card_name, tag_type, tag FROM forge_deck_tags"):
                if r[1] == "has":
                    self.forge_card_has.setdefault(r[0], set()).add(r[2])
                elif r[1] == "hints":
                    self.forge_card_hints.setdefault(r[0], set()).add(r[2])
        except Exception:
            pass

        # Tower model (loaded once, cached on class)
        self.tower_model = _load_tower_model()

        # Mechanics data
        self.mechanics = {}
        self.cmdr_mechanics = []
        self.cmdr_type = ""
        try:
            from mechanics_matcher import load_mechanics
            self.mechanics = load_mechanics(str(conn.execute("PRAGMA database_list").fetchone()[2]))
            self.cmdr_mechanics = self.mechanics.get(self.cmdr_oid, [])
            row = conn.execute("SELECT type_line FROM cards WHERE oracle_id = ?",
                               (self.cmdr_oid,)).fetchone()
            self.cmdr_type = row[0] if row else ""
        except Exception:
            pass

        # LLM synergy scores (pre-scored by score_synergies.py)
        self.llm_scores = {}  # {oracle_id: score}
        try:
            has = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='synergy_scores'"
            ).fetchone()[0]
            if has and self.cmdr_oid:
                for r in conn.execute(
                    "SELECT card_oid, score FROM synergy_scores WHERE commander_oid = ?",
                    (self.cmdr_oid,)):
                    self.llm_scores[r[0]] = r[1]
        except Exception:
            pass

        # Commander profile fallback (when no strategies provided by caller)
        if not self.active_strategies and self.cmdr_oid:
            try:
                from mtg_synergy.recommend.commander_profile import load_profile
                profile = load_profile(conn, self.cmdr_oid)
                if profile:
                    self.active_strategies = profile.strategies
                    if profile.tribal_type and not self.deck_types:
                        self.deck_types = {profile.tribal_type}
                        self.is_tribal = True
            except Exception:
                pass

        # Causal graph (pre-loaded, O(1) per candidate)
        self.causal_ctx = None
        try:
            from mtg_synergy.causal import CausalContext
            deck_oids = {self.card_oid.get(c) for c in self.deck_cards
                         if c in self.card_oid} - {None}
            self.causal_ctx = CausalContext(conn, self.cmdr_oid, deck_oids)
        except Exception:
            pass


# Trigger timing quality: how often/reliably does a trigger fire?
# Higher = fires more often or at better timing.
TRIGGER_QUALITY = {
    "creature-enters": 1.0,         # Fires many times per turn cycle
    "spell-cast": 0.9,              # Fires on every spell
    "land-enters": 0.8,             # Once per turn usually
    "counter-placed": 0.8,          # Fires often in counter decks
    "token-created": 0.7,           # Fires when making tokens
    "beginning-of-combat": 0.7,     # Once per turn, reliable timing
    "end-step": 0.6,                # Once per turn, after actions
    "creature-dies": 0.6,           # Depends on board state
    "card-drawn": 0.6,              # Depends on draw effects
    "combat-damage-dealt": 0.5,     # Only if creature connects
    "creature-attacks": 0.5,        # Only on attack step
    "attack-declared": 0.5,         # Same as attacks
    "upkeep": 0.4,                  # Once per turn, before you act
    "permanent-leaves": 0.4,        # Situational
    "card-discarded": 0.3,          # Niche
    "creature-blocks": 0.3,         # Only on opponents' turns
    "you-gain-life": 0.3,           # Deck-dependent
    "damage-dealt": 0.5,            # Common in combat
    "enchantment-enters": 0.3,      # Niche
    "artifact-enters": 0.3,         # Niche
}

# Effect relevance to common strategies
STRATEGY_EFFECTS = {
    "+1/+1-counters": {"put-counter": 1.0, "proliferate": 0.9, "double-counters": 0.9,
                       "pump": 0.1, "create-token": 0.3, "draw-card": 0.2},
    "tokens": {"create-token": 1.0, "populate": 0.8, "pump": 0.3, "put-counter": 0.3},
    "spellslinger": {"copy": 1.0, "draw-card": 0.7, "deal-damage": 0.5, "reduce-cost": 0.6},
    "aristocrats": {"sacrifice": 0.8, "create-token": 0.6, "drain": 0.7, "draw-card": 0.5},
    "equipment": {"equip": 1.0, "attach": 0.8, "pump": 0.5, "grant-keyword": 0.4},
    "landfall": {"put-land": 0.9, "search-library": 0.6, "create-token": 0.5, "draw-card": 0.5},
    "reanimator": {"return-to-battlefield": 1.0, "mill": 0.6, "sacrifice": 0.4},
    "lifegain": {"gain-life": 1.0, "grant-keyword": 0.3, "draw-card": 0.3},
    "burn": {"deal-damage": 1.0, "copy": 0.5, "draw-card": 0.3},
    "mill": {"mill": 1.0, "draw-card": 0.3, "sacrifice": 0.2},
}


def _is_plus1_counter(mech: dict, oracle_text: str) -> bool:
    """Check if a put-counter mechanic is about +1/+1 counters (not time/charge/etc)."""
    # Check raw_json for counter_type
    import json as _json
    raw = mech.get("raw_json") or ""
    if raw:
        try:
            r = _json.loads(raw)
            # Walk the JSON looking for counter_type
            def find_counter_type(obj):
                if isinstance(obj, dict):
                    ct = obj.get("counter_type")
                    if ct:
                        return ct
                    for v in obj.values():
                        result = find_counter_type(v)
                        if result:
                            return result
                return None
            ct = find_counter_type(r)
            if ct:
                return ct in ("+1/+1", "plus1plus1")
        except Exception:
            pass
    # Fallback: check oracle text
    oracle_lower = oracle_text.lower()
    if "+1/+1 counter" in oracle_lower:
        return True
    # If oracle mentions other counter types and NOT +1/+1, it's not +1/+1
    other_types = ["time counter", "charge counter", "loyalty counter", "lore counter",
                   "oil counter", "blood counter", "age counter", "verse counter",
                   "quest counter", "page counter", "void counter", "level counter",
                   "fate counter", "luck counter", "ki counter", "ore counter"]
    if any(ct in oracle_lower for ct in other_types) and "+1/+1 counter" not in oracle_lower:
        return False
    # Default: assume +1/+1 for creatures, unknown for non-creatures
    return True


def _compute_interaction_score(cmdr_mechs: list, card_mechs: list,
                                active_strategies: set,
                                card_oracle: str = "", cmdr_oracle: str = "") -> float:
    """Score the mechanical interaction between commander and card.

    Analyzes trigger→effect chains, timing quality, strategy relevance,
    and counter type matching.
    Returns 0-10 score.
    """
    if not cmdr_mechs or not card_mechs:
        return 0.0

    # Get commander's trigger events and effects
    cmdr_triggers = set()
    cmdr_effects = set()
    for m in cmdr_mechs:
        if m.get("trigger_event"):
            cmdr_triggers.add(m["trigger_event"])
        if m.get("effect_action"):
            cmdr_effects.add(m["effect_action"])

    # Does the commander's put-counter use +1/+1 counters?
    cmdr_uses_plus1 = any(
        m.get("effect_action") == "put-counter" and _is_plus1_counter(m, cmdr_oracle)
        for m in cmdr_mechs
    )

    # Get strategy-relevant effects
    strat_effects = {}
    for strat in active_strategies:
        strat_effects.update(STRATEGY_EFFECTS.get(strat, {}))

    score = 0.0

    for m in card_mechs:
        card_effect = m.get("effect_action") or ""
        card_trigger = m.get("trigger_event") or ""
        card_modifier = m.get("modifier_how") or ""
        card_modifier_target = m.get("modifier_target") or ""
        card_type = m.get("mechanic_type") or ""
        card_cost = m.get("cost") or ""

        # --- Counter type check ---
        # If strategy cares about +1/+1 counters, penalize non-+1/+1 put-counter effects
        is_counter_strategy = "+1/+1-counters" in active_strategies
        if card_effect == "put-counter" and is_counter_strategy:
            if not _is_plus1_counter(m, card_oracle):
                # This card puts non-+1/+1 counters — much less relevant
                card_effect = "put-other-counter"

        # --- A. Card's effect matches what the strategy cares about ---
        effect_relevance = strat_effects.get(card_effect, 0.0)
        if effect_relevance > 0:
            timing = TRIGGER_QUALITY.get(card_trigger, 0.5) if card_trigger else 0.5
            cost_penalty = 0.7 if card_cost else 1.0
            if card_type == "replacement":
                timing = 0.9
                cost_penalty = 1.0
            if card_type == "static":
                timing = 0.8
                cost_penalty = 1.0
            score += effect_relevance * timing * cost_penalty * 5.0

        # --- B. Card triggers on the same event as commander ---
        if card_trigger and card_trigger in cmdr_triggers:
            score += 1.0

        # --- C. Commander produces events that trigger the card ---
        if card_trigger == "counter-placed" and "put-counter" in cmdr_effects:
            # Only score if both sides deal with the same counter type
            if cmdr_uses_plus1 and is_counter_strategy:
                # Card must also care about +1/+1 counters (not time/charge counters)
                if _is_plus1_counter(m, card_oracle):
                    score += 3.0

        # --- D. Card is a modifier for the commander's effects ---
        if card_modifier_target in cmdr_effects:
            # Check: if modifying put-counter, is it for the right counter type?
            if card_modifier_target == "put-counter" and is_counter_strategy:
                if not _is_plus1_counter(m, card_oracle):
                    continue  # Skip — modifying non-+1/+1 counters
            modifier_value = {"double": 4.0, "triple": 5.0, "increase-by-1": 3.0,
                              "add-one": 2.0, "add-one-more": 2.0}.get(card_modifier, 1.0)
            score += modifier_value

    return min(score, 10.0)


def compute_forge_deck_overlap(conn, commander_name: str, candidate_name: str) -> int:
    """Count matching DeckHas/DeckHints between commander and candidate.

    DeckHas = what the card provides (abilities, types)
    DeckHints = what the card wants in the deck

    Overlap = (candidate provides what commander wants) +
              (commander provides what candidate wants)
    """
    cmdr_has = set()
    cmdr_hints = set()
    for r in conn.execute(
        "SELECT tag_type, tag FROM forge_deck_tags WHERE card_name = ?",
        (commander_name,)
    ).fetchall():
        if r[0] == "has":
            cmdr_has.add(r[1])
        elif r[0] == "hints":
            cmdr_hints.add(r[1])

    cand_has = set()
    cand_hints = set()
    for r in conn.execute(
        "SELECT tag_type, tag FROM forge_deck_tags WHERE card_name = ?",
        (candidate_name,)
    ).fetchall():
        if r[0] == "has":
            cand_has.add(r[1])
        elif r[0] == "hints":
            cand_hints.add(r[1])

    return len(cand_has & cmdr_hints) + len(cand_hints & cmdr_has)


def compute_dynamic_score(card_name: str, card_data: dict, ctx: DeckContext,
                          conn, oracle_text: str = "") -> dict:
    """Compute feature-based synergy score for a single card.

    Returns dict with 'total' score and individual feature values.
    """
    w = SCORING_WEIGHTS
    oid = card_data.get("oracle_id") or ctx.card_oid.get(card_name, "")
    type_line = card_data.get("type_line", "")
    is_creature = "Creature" in type_line
    rank = card_data.get("edhrec_rank") or 50000

    # --- Feature 1: Tower model (semantic synergy) ---
    tower = _get_tower_score(ctx, oid)

    # --- Feature 2: Structured interaction score ---
    # Analyzes trigger→effect chains, timing quality, strategy relevance,
    # and counter type matching (distinguishes +1/+1 vs time/charge counters).
    mech = 0.0
    if ctx.cmdr_oid:
        card_mechs_raw = conn.execute(
            "SELECT mechanic_type, trigger_event, trigger_filter, effect_action, "
            "modifier_target, modifier_how, cost, raw_json FROM card_mechanics WHERE oracle_id = ?",
            (oid,)).fetchall()
        if card_mechs_raw:
            card_mechs_dicts = [
                {"mechanic_type": r[0], "trigger_event": r[1],
                 "trigger_filter": r[2], "effect_action": r[3],
                 "modifier_target": r[4], "modifier_how": r[5], "cost": r[6],
                 "raw_json": r[7]}
                for r in card_mechs_raw
            ]
            cmdr_mechs_dicts = [
                {"mechanic_type": r[0], "trigger_event": r[1],
                 "trigger_filter": r[2], "effect_action": r[3],
                 "modifier_target": r[4], "modifier_how": r[5], "cost": r[6],
                 "raw_json": r[7]}
                for r in conn.execute(
                    "SELECT mechanic_type, trigger_event, trigger_filter, effect_action, "
                    "modifier_target, modifier_how, cost, raw_json FROM card_mechanics WHERE oracle_id = ?",
                    (ctx.cmdr_oid,)).fetchall()
            ]
            # Get oracle texts for counter type detection
            cmdr_oracle_row = conn.execute(
                "SELECT oracle_text FROM cards WHERE oracle_id = ?", (ctx.cmdr_oid,)).fetchone()
            cmdr_oracle = (cmdr_oracle_row[0] or "") if cmdr_oracle_row else ""
            mech = _compute_interaction_score(cmdr_mechs_dicts, card_mechs_dicts,
                                               ctx.active_strategies,
                                               card_oracle=oracle_text,
                                               cmdr_oracle=cmdr_oracle)

    # --- Feature 3: Commander tag overlap ---
    card_provides = set(
        r[0] for r in conn.execute("SELECT tag FROM provides WHERE oracle_id = ?", (oid,))
    ) - OVERLAP_EXCLUDE
    card_wants = set(
        r[0] for r in conn.execute("SELECT tag FROM wants WHERE oracle_id = ?", (oid,))
    ) - OVERLAP_EXCLUDE
    cmdr_overlap = len(card_provides & ctx.cmdr_wants) + len(card_wants & ctx.cmdr_provides)

    # --- Feature 4: Deck-wide tag overlap ---
    deck_overlap = len(card_provides & ctx.deck_wants) + len(card_wants & ctx.deck_provides)

    # --- Feature 5: Strategy overlap (tag-based) ---
    strat_overlap = 0
    if ctx.active_strategies:
        card_strats = set(
            r[0] for r in conn.execute(
                "SELECT strategy FROM card_strategies WHERE oracle_id = ? AND confidence >= 0.3",
                (oid,))
        )
        strat_overlap = len(card_strats & ctx.active_strategies)

    # --- Feature 6: Tribal match ---
    tribal_adj = 0.0
    tribal_match = False
    if ctx.is_tribal and is_creature:
        type_lower = type_line.lower()
        matches = any(t.lower() in type_lower for t in ctx.deck_types)
        if matches:
            tribal_adj = w["TRIBAL_BONUS"]
            tribal_match = True
        else:
            tribal_adj = w["TRIBAL_PENALTY"]

    # --- Feature 7: Card quality (EDHREC rank) ---
    rank_score = max(0, 3.0 - 0.6 * math.log10(max(rank, 1)))

    # --- Feature 8: EDHREC synergy ---
    edhrec_syn = max(0, ctx.edhrec.get(card_name, 0.0))

    # --- Feature 9: Strategy keyword match ---
    # Does this card's oracle text actually DO what the deck's strategy cares about?
    # A +1/+1-counters deck cares about cards that mention "+1/+1 counter", not just ETB.
    strat_keyword_hits = 0
    if ctx.strategy_keywords and oracle_text:
        oracle_lower = oracle_text.lower()
        strat_keyword_hits = sum(1 for kw in ctx.strategy_keywords if kw in oracle_lower)

    # --- Feature 10: Causal graph score ---
    causal = 0.0
    if ctx.causal_ctx:
        causal = ctx.causal_ctx.causal_score(oid)

    # --- Feature 11: LLM synergy score ---
    llm = ctx.llm_scores.get(oid, 0)

    # --- Feature 12: Forge DeckHas/DeckHints overlap ---
    forge_overlap = 0
    if ctx.forge_cmdr_hints or ctx.forge_cmdr_has:
        cand_has = ctx.forge_card_has.get(card_name, set())
        cand_hints = ctx.forge_card_hints.get(card_name, set())
        forge_overlap = len(cand_has & ctx.forge_cmdr_hints) + len(cand_hints & ctx.forge_cmdr_has)

    # --- Combine ---
    total = (llm * w.get("LLM", 0)
             + tower * w["TOWER"]
             + min(mech, 10) * w["MECHANICS"]
             + cmdr_overlap * w["CMDR_TAG_OVERLAP"]
             + deck_overlap * w["DECK_TAG_OVERLAP"]
             + strat_overlap * w["STRATEGY"]
             + strat_keyword_hits * w["STRATEGY_KEYWORD"]
             + tribal_adj
             + rank_score * w["RANK"]
             + edhrec_syn * w["EDHREC_SYNERGY"]
             + causal * w.get("CAUSAL", 0)
             + forge_overlap * w.get("FORGE_DECK_OVERLAP", 0))

    return {
        "total": total,
        "llm": llm,
        "tower": round(tower, 1),
        "mechanics": round(mech, 1),
        "cmdr_overlap": cmdr_overlap,
        "deck_overlap": deck_overlap,
        "strat_overlap": strat_overlap,
        "strat_keywords": strat_keyword_hits,
        "tribal_match": tribal_match,
        "rank_score": round(rank_score, 2),
        "edhrec_syn": round(edhrec_syn, 3) if edhrec_syn > 0 else 0,
        "causal": round(causal, 1),
        "forge_overlap": forge_overlap,
    }


def score_all_candidates(candidate_scores: dict, cards: list, ctx: DeckContext,
                         conn, verbose: bool = True) -> None:
    """Score all candidates using dynamic features. Modifies candidate_scores in-place.

    Also injects high-tower and high-EDHREC candidates not in the graph pool.
    """
    # Build card data lookup
    card_data = {}
    for c in cards:
        card_data[c["name"]] = c

    # Inject high-EDHREC candidates
    edh_threshold = SCORING_WEIGHTS.get("EDHREC_INJECTION_THRESHOLD", 0.25)
    edh_injected = 0
    for card_name, syn in ctx.edhrec.items():
        if syn < edh_threshold or card_name in ctx.deck_cards or card_name in candidate_scores:
            continue
        row = conn.execute(
            "SELECT oracle_id, name, type_line, mana_cost, cmc, edhrec_rank, color_identity "
            "FROM cards WHERE name = ?", (card_name,)).fetchone()
        if not row:
            continue
        oid, name, type_line, mana_cost, cmc, edhrec_rank, ci_json = row
        import json as _json
        card_ci = set(_json.loads(ci_json or "[]"))
        # Color identity check (use deck commander's CI)
        cmdr_ci_row = conn.execute(
            "SELECT color_identity FROM cards WHERE oracle_id = ?", (ctx.cmdr_oid,)).fetchone()
        if cmdr_ci_row:
            cmdr_ci = set(_json.loads(cmdr_ci_row[0] or "[]"))
            if not card_ci.issubset(cmdr_ci):
                continue
        candidate_scores[name] = {
            "total": 0.0, "partners": [], "multi_sig": 0,
            "commander_synergy": 0.0, "key_synergy": 0.0,
        }
        new_card = {"oracle_id": oid, "name": name, "type_line": type_line or "",
                    "mana_cost": mana_cost or "", "cmc": cmc or 0, "edhrec_rank": edhrec_rank}
        cards.append(new_card)
        card_data[name] = new_card
        ctx.card_oid[name] = oid
        edh_injected += 1
    if edh_injected and verbose:
        print(f"  EDHREC injection: {edh_injected} high-synergy cards added to candidate pool")
    if ctx.edhrec and verbose:
        print(f"  EDHREC synergy: {len(ctx.edhrec)} cards loaded for blending")

    # Batch-load oracle text for strategy keyword matching
    oracle_texts = {}  # card_name -> oracle_text
    if ctx.strategy_keywords:
        all_cand_oids = [ctx.card_oid.get(cn, "") for cn in candidate_scores if ctx.card_oid.get(cn)]
        for i in range(0, len(all_cand_oids), 500):
            chunk = all_cand_oids[i:i + 500]
            ph = ",".join("?" * len(chunk))
            for r in conn.execute(
                f"SELECT name, oracle_text FROM cards WHERE oracle_id IN ({ph})", chunk
            ).fetchall():
                oracle_texts[r[0]] = r[1] or ""

    # Score all candidates
    scored = 0
    for card_name, info in candidate_scores.items():
        cd = card_data.get(card_name)
        if not cd:
            continue
        features = compute_dynamic_score(card_name, cd, ctx, conn,
                                          oracle_text=oracle_texts.get(card_name, ""))
        info["total"] = features["total"]
        # Copy feature values for display
        if features["tower"] > 0:
            info["tower_score"] = features["tower"]
        if features["mechanics"] > 0:
            info["mechanics_score"] = features["mechanics"]
        if features["cmdr_overlap"] > 0:
            info["cmdr_overlap"] = features["cmdr_overlap"]
        if features["tribal_match"]:
            info["tribal_match"] = True
        if features["edhrec_syn"] > 0:
            info["edhrec_syn"] = features["edhrec_syn"]
        if features["strat_keywords"] > 0:
            info["strat_keywords"] = features["strat_keywords"]
        scored += 1

    if verbose:
        print(f"  Dynamic scoring: {scored} candidates scored "
              f"(tower + mechanics + tags + strategy + tribal)")


# === Tower model singleton ===

_tower_cache = {}


def _load_tower_model():
    """Load tower model data (cached). Returns dict or None."""
    if _tower_cache:
        return _tower_cache
    tower_path = os.path.join(DATA_DIR, "tower_model.npz")
    if not os.path.exists(tower_path):
        return None
    try:
        import numpy as np
        from train_tower_model import (load_embeddings, load_structural_features,
                                        compute_struct_features, forward)
        td = np.load(tower_path)
        model = {k: td[k] for k in td.files if k not in ("struct_means", "struct_stds")}
        if model["W1"].shape[0] != 140:
            return None
        normed_emb, oid_list, oid_to_idx = load_embeddings()
        sf_data = load_structural_features()
        _tower_cache.update({
            "model": model,
            "means": td["struct_means"],
            "stds": td["struct_stds"],
            "emb": normed_emb,
            "oid_to_idx": oid_to_idx,
            "sf_data": sf_data,
            "compute_sf": compute_struct_features,
            "forward": forward,
        })
        return _tower_cache
    except Exception:
        return None


def _get_tower_score(ctx: DeckContext, card_oid: str) -> float:
    """Get tower model prediction for a (commander, card) pair."""
    tm = ctx.tower_model
    if not tm or not ctx.cmdr_oid:
        return 5.0  # neutral default
    try:
        import numpy as np
        cmdr_idx = tm["oid_to_idx"].get(ctx.cmdr_oid)
        card_idx = tm["oid_to_idx"].get(card_oid)
        if cmdr_idx is None or card_idx is None:
            return 5.0
        sf = tm["compute_sf"](ctx.cmdr_oid, card_oid, *tm["sf_data"])
        sf_norm = ((sf - tm["means"]) / tm["stds"]).reshape(1, -1).astype(np.float32)
        X_cmdr = tm["emb"][cmdr_idx].reshape(1, -1).astype(np.float32)
        X_card = tm["emb"][card_idx].reshape(1, -1).astype(np.float32)
        score, _ = tm["forward"](tm["model"], X_cmdr, X_card, sf_norm)
        return float(np.clip(score, 1, 10)[0])
    except Exception:
        return 5.0
