"""Mechanics-based synergy matching engine.

Matches cards based on structured game mechanics extracted by extract_mechanics.py.
The core idea: synergy = card A produces game events that card B responds to,
with filter-aware matching to avoid false positives.

Matching modes:
1. Event chain: A produces events → B triggers on those events
2. Shared trigger: A and B both trigger on the same event type
3. Modifier: A amplifies/doubles what B does
4. Enabler: A's static ability applies to B (scope-aware)

Used by synergy_graph.py --recommend as an additional scoring signal.
"""

import json
import sqlite3
from collections import defaultdict

# Action → what game events it produces (when this action resolves)
ACTION_TO_EVENTS = {
    "create-token": ["creature-enters", "token-created"],
    "deal-damage": ["damage-dealt"],
    "draw-card": ["card-drawn"],
    "put-counter": ["counter-placed"],
    "destroy": ["creature-dies", "permanent-leaves"],
    "sacrifice": ["creature-dies", "permanent-leaves"],
    "exile": ["permanent-leaves"],
    "discard": ["card-discarded"],
    "gain-life": ["life-gained", "you-gain-life"],
    "lose-life": ["life-lost", "opponent-loses-life"],
    "mill": ["card-discarded"],
    "return-to-battlefield": ["creature-enters"],
    "copy": ["creature-enters"],
    "extra-combat": ["beginning-of-combat", "attack-declared"],
    "untap": [],
    "pump": [],
    "grant-keyword": [],
    "reduce-cost": [],
    "add-mana": [],
    "search-library": [],
    "scry": [],
    "surveil": ["card-discarded"],
    "counter-spell": [],
    "protect": [],
    "tap": [],
    "return-to-hand": [],
    "phase-out": ["permanent-leaves"],
    "transform": [],
    "extra-turn": ["upkeep", "beginning-of-combat"],
    "remove-counter": [],
    # Set-specific / keyword mechanics
    "amass": ["creature-enters", "counter-placed", "token-created"],
    "ring-tempts": ["ring-tempted"],
    "discover": [],
    "explore": [],
    "cascade": ["spell-cast"],
    "connive": ["card-drawn", "card-discarded"],
    "venture": [],
    "adapt": ["counter-placed"],
    "proliferate": ["counter-placed"],
}

# Additional trigger events for set-specific mechanics
# (added to the canonical event list implicitly)
# "ring-tempted" — The Ring tempts you
# These are matched like any other event


def filter_matches(trigger_filter: dict | None, producer_output: dict) -> bool:
    """Check if a trigger's filter is satisfied by what the producer creates."""
    if not trigger_filter:
        return True

    for key, required_val in trigger_filter.items():
        if required_val is None:
            continue  # FIX #1: skip None values
        if key == "controller":
            if required_val == "you":
                continue
            if required_val == "opponent" and producer_output.get("controller") != "opponent":
                return False
        elif key in ("is_another", "is_other"):
            continue  # Different cards = always "another"
        elif key == "is_nontoken":
            if producer_output.get("is_token"):
                return False
        elif key == "subtype":
            producer_subtype = producer_output.get("subtype") or ""
            if str(required_val).lower() != str(producer_subtype).lower():
                return False
        elif key == "card_type":
            producer_type = producer_output.get("card_type") or "creature"
            if str(required_val).lower() != str(producer_type).lower():
                return False
        elif key == "has_keyword":
            producer_kw = producer_output.get("has_keyword") or producer_output.get("keywords") or ""
            if isinstance(producer_kw, list):
                producer_kw_set = {k.lower() for k in producer_kw}
            else:
                producer_kw_set = {k.strip().lower() for k in str(producer_kw).split(",") if k.strip()}
            if isinstance(required_val, list):
                required_kw = {k.lower() for k in required_val}
            else:
                required_kw = {str(required_val).lower()}
            if not producer_kw_set & required_kw:
                return False
        elif key == "is_equipped":
            if required_val and not producer_output.get("is_equipped"):
                return False
        elif key == "counter_type":
            prod_ct = str(producer_output.get("counter_type", "")).lower()
            if str(required_val).lower() != prod_ct:
                return False
        elif key == "power":
            prod_power = producer_output.get("power")
            if prod_power is None:
                return False
            try:
                if isinstance(required_val, str) and required_val.startswith(">="):
                    if int(prod_power) < int(required_val[2:]):
                        return False
                elif isinstance(required_val, str) and required_val.startswith("<="):
                    if int(prod_power) > int(required_val[2:]):
                        return False
                else:
                    if int(prod_power) != int(required_val):
                        return False
            except (ValueError, TypeError):
                return False  # Non-numeric power (e.g., *) — can't verify
        elif key == "tapped":
            if required_val and not producer_output.get("tapped"):
                return False
        elif key == "condition":
            pass  # Complex conditions still ignored (would need game-state simulation)
        # Ignore remaining unknown filter keys (amount, name, source, etc.)

    return True


def filters_compatible(filter_a: dict | None, filter_b: dict | None) -> bool:
    """Check if two trigger filters respond to overlapping events.

    Used for shared-trigger synergy: if A and B both trigger on
    "spell-cast" with filter {card_type: "Aura"}, they share a trigger.
    """
    if not filter_a and not filter_b:
        return True
    if not filter_a or not filter_b:
        return True  # One unfiltered = the filtered one is a subset

    # Type hierarchy: Aura/Equipment are subtypes of Enchantment/Artifact
    TYPE_HIERARCHY = {
        "aura": "enchantment",
        "equipment": "artifact",
        "vehicle": "artifact",
    }

    for key in set(list(filter_a.keys()) + list(filter_b.keys())):
        if key in ("controller", "is_another", "is_other", "amount"):
            continue
        val_a = filter_a.get(key)
        val_b = filter_b.get(key)
        if val_a is not None and val_b is not None:
            a_low = str(val_a).lower()
            b_low = str(val_b).lower()
            if a_low == b_low:
                continue
            # Check type hierarchy (Aura ⊂ Enchantment)
            if key in ("card_type", "subtype"):
                a_parent = TYPE_HIERARCHY.get(a_low, a_low)
                b_parent = TYPE_HIERARCHY.get(b_low, b_low)
                if a_parent == b_low or b_parent == a_low or a_parent == b_parent:
                    continue
            return False
    return True


_mechanics_cache = {}  # db_path -> mechanics dict


def load_mechanics(db_path: str) -> dict:
    """Load all extracted mechanics from DB. Cached after first call per path."""
    if db_path in _mechanics_cache:
        return _mechanics_cache[db_path]

    conn = sqlite3.connect(db_path)
    mechanics = defaultdict(list)
    for row in conn.execute(
        "SELECT oracle_id, mechanic_type, trigger_event, trigger_filter, "
        "effect_action, effect_detail, modifier_target, modifier_how, scope_filter, cost "
        "FROM card_mechanics"
    ):
        oid, mtype, trigger_event, trigger_filter, action, detail, mod_target, mod_how, scope, cost = row
        m = {
            "type": mtype,
            "trigger_event": trigger_event,
            "filter": json.loads(trigger_filter) if trigger_filter else None,
            "action": action,
            "detail": json.loads(detail) if detail else None,
            "modifier_target": mod_target,
            "modifier_how": mod_how,
            "scope": json.loads(scope) if scope else None,
            "cost": cost,
        }
        mechanics[oid].append(m)
    conn.close()
    result = dict(mechanics)
    _mechanics_cache[db_path] = result
    return result


def card_produces_events(mechanics: list[dict]) -> list[dict]:
    """What game events does this card produce?"""
    events = []
    has_self_sacrifice = False

    for m in mechanics:
        action = m.get("action")
        cost = m.get("cost") or ""

        # Self-sacrifice detection: if the cost includes "sacrifice" (self),
        # this card produces creature-dies when activated.
        # e.g., Spore Frog: "{1}, Sacrifice Spore Frog: Prevent all combat damage"
        if isinstance(cost, str) and "sacrifice" in cost.lower():
            has_self_sacrifice = True

        if not action or action not in ACTION_TO_EVENTS:
            continue
        produced_events = ACTION_TO_EVENTS[action]
        if not produced_events:
            continue

        output = {"controller": "you"}
        detail = m.get("detail") or {}
        if isinstance(detail, dict):
            token = detail.get("token", {})
            if isinstance(token, dict):
                if token.get("subtype"):
                    output["subtype"] = token["subtype"]
                if token.get("power"):
                    output["card_type"] = "creature"
            if action == "create-token":
                output["card_type"] = "creature"
                output["is_token"] = True
            if isinstance(token, dict) and token.get("keywords"):
                kw = token["keywords"]
                output["has_keyword"] = kw if isinstance(kw, list) else [kw]

        if action == "grant-keyword" and isinstance(detail, dict):
            kw = detail.get("keyword") or detail.get("keywords")
            if kw:
                output["has_keyword"] = [kw] if isinstance(kw, str) else kw

        for event in produced_events:
            events.append({"event": event, "output": output})

    # Cards with self-sacrifice cost produce creature-dies
    if has_self_sacrifice:
        events.append({
            "event": "creature-dies",
            "output": {"controller": "you", "card_type": "creature"},
        })
        events.append({
            "event": "permanent-leaves",
            "output": {"controller": "you"},
        })

    return events


def card_responds_to(mechanics: list[dict]) -> list[dict]:
    """What game events does this card respond to?"""
    responses = []
    for m in mechanics:
        if m.get("trigger_event"):
            responses.append({
                "event": m["trigger_event"],
                "filter": m.get("filter"),
                "action": m.get("action"),
            })
    return responses


def card_modifies(mechanics: list[dict]) -> list[dict]:
    """What game actions does this card modify/amplify?"""
    mods = []
    for m in mechanics:
        if m.get("modifier_target"):
            mods.append({
                "modifies": m["modifier_target"],
                "how": m.get("modifier_how", ""),
                "scope": m.get("scope"),
            })
    return mods


def card_enables(mechanics: list[dict]) -> list[dict]:
    """What static abilities does this card provide?"""
    enables = []
    for m in mechanics:
        if m["type"] == "static" and m.get("action"):
            enables.append({
                "action": m["action"],
                "scope": m.get("scope") or m.get("filter"),
            })
    return enables


def _extract_subtypes(type_line: str) -> set:
    """Extract creature subtypes from type line."""
    if not type_line or " — " not in type_line.lower():
        return set()
    return {s.strip().lower() for s in type_line.lower().split(" — ")[1].split()}


def compute_synergy(card_a_mechs: list[dict], card_b_mechs: list[dict],
                    card_a_type_line: str = "", card_b_type_line: str = "") -> float:
    """Compute synergy score between two cards based on mechanics.

    Checks:
    1. Event chain: A produces events that B responds to (and vice versa)
    2. Shared trigger: A and B both respond to the same event with compatible filters
    3. Modifier: A modifies actions that B performs (and vice versa)
    4. Enabler: A's static scope includes B's creature types (and vice versa)
    """
    score = 0.0

    a_produces = card_produces_events(card_a_mechs)
    b_responds = card_responds_to(card_b_mechs)
    b_produces = card_produces_events(card_b_mechs)
    a_responds = card_responds_to(card_a_mechs)

    # --- 1. Event chains: A→B and B→A ---
    # FIX #3: Cap per event type — best match per event, not sum of all
    # A produces → B responds
    best_per_event_ab = {}  # event → best score
    for prod in a_produces:
        for resp in b_responds:
            if prod["event"] == resp["event"]:
                if filter_matches(resp["filter"], prod["output"]):
                    key = ("ab", prod["event"])
                    best_per_event_ab[key] = max(best_per_event_ab.get(key, 0), 2.0)
                else:
                    key = ("ab", prod["event"])
                    best_per_event_ab[key] = max(best_per_event_ab.get(key, 0), 0.3)

    # B produces → A responds
    for prod in b_produces:
        for resp in a_responds:
            if prod["event"] == resp["event"]:
                if filter_matches(resp["filter"], prod["output"]):
                    key = ("ba", prod["event"])
                    best_per_event_ab[key] = max(best_per_event_ab.get(key, 0), 2.0)
                else:
                    key = ("ba", prod["event"])
                    best_per_event_ab[key] = max(best_per_event_ab.get(key, 0), 0.3)

    score += sum(best_per_event_ab.values())

    # --- 1b. "Card IS the event" matching ---
    # A card doesn't just produce events — when you CAST it or it ENTERS,
    # it IS an event that can trigger abilities.
    # A Vampire creature entering triggers "creature-enters with filter Vampire"
    # An Aura spell being cast triggers "spell-cast with filter Aura"
    # Check if the candidate card ITSELF satisfies the commander's trigger filters.
    b_subtypes = _extract_subtypes(card_b_type_line)
    a_subtypes = _extract_subtypes(card_a_type_line)
    b_type_lower = card_b_type_line.lower() if card_b_type_line else ""
    a_type_lower = card_a_type_line.lower() if card_a_type_line else ""

    # What card types is B?
    b_is = set()
    for st in b_subtypes:
        b_is.add(st)
    if "creature" in b_type_lower:
        b_is.add("creature")
    if "artifact" in b_type_lower:
        b_is.add("artifact")
    if "enchantment" in b_type_lower:
        b_is.add("enchantment")
    if "instant" in b_type_lower:
        b_is.add("instant")
    if "sorcery" in b_type_lower:
        b_is.add("sorcery")
    if "equipment" in b_type_lower:
        b_is.add("equipment")
    if "aura" in b_type_lower:
        b_is.add("aura")
    if "vehicle" in b_type_lower:
        b_is.add("vehicle")
    if "planeswalker" in b_type_lower:
        b_is.add("planeswalker")

    # What card types is A?
    a_is = set()
    for st in a_subtypes:
        a_is.add(st)
    if "creature" in a_type_lower:
        a_is.add("creature")
    if "artifact" in a_type_lower:
        a_is.add("artifact")
    if "enchantment" in a_type_lower:
        a_is.add("enchantment")
    if "equipment" in a_type_lower:
        a_is.add("equipment")
    if "aura" in a_type_lower:
        a_is.add("aura")

    TYPE_HIERARCHY = {"aura": "enchantment", "equipment": "artifact", "vehicle": "artifact"}

    def card_satisfies_filter(card_types: set, trigger_filter: dict | None) -> bool:
        """Does a card's type line satisfy a trigger filter?

        Filter says "equipment" → card must be Equipment (or child of Equipment).
        Filter says "creature" → card must be a Creature.
        Filter says ["Angel", "Demon", "Dragon"] → card must be any of these.
        Hierarchy: equipment IS-A artifact, aura IS-A enchantment.
        """
        if not trigger_filter:
            return True
        for key, val in trigger_filter.items():
            if val is None or key in ("controller", "is_another", "is_other", "amount",
                                       "is_source", "attacks_opponent", "defending_player",
                                       "attacker", "timing", "source_zone", "target_controller",
                                       "attack_target", "condition"):
                continue
            if key in ("subtype", "card_type", "target_filter"):
                # Handle nested target_filter
                if key == "target_filter" and isinstance(val, dict):
                    return card_satisfies_filter(card_types, val)
                # Handle array of types: ["Angel", "Demon", "Dragon"]
                if isinstance(val, list):
                    if any(v.lower() in card_types for v in val if isinstance(v, str)):
                        continue
                    # Check hierarchy for each
                    matched = False
                    for v in val:
                        if not isinstance(v, str):
                            continue
                        v_low = v.lower()
                        for ct in card_types:
                            if TYPE_HIERARCHY.get(ct) == v_low:
                                matched = True
                                break
                        if matched:
                            break
                    if matched:
                        continue
                    return False
                # Single string value
                val_low = str(val).lower()
                if val_low in card_types:
                    continue
                for ct in card_types:
                    if TYPE_HIERARCHY.get(ct) == val_low:
                        break
                else:
                    return False
        return True

    # A triggers on something, B IS that something
    for resp in a_responds:
        event = resp["event"]
        filt = resp["filter"]
        # creature-enters / spell-cast → check if B is the matching type
        if event in ("creature-enters", "spell-cast", "enchantment-enters", "artifact-enters"):
            if card_satisfies_filter(b_is, filt):
                key = ("card-is-event", event)
                if key not in best_per_event_ab:
                    best_per_event_ab[key] = 2.0
                    if filt and any(k in filt for k in ("subtype",)):
                        best_per_event_ab[key] = 3.0

    # B triggers on something, A IS that something
    for resp in b_responds:
        event = resp["event"]
        filt = resp["filter"]
        if event in ("creature-enters", "spell-cast", "enchantment-enters", "artifact-enters"):
            if card_satisfies_filter(a_is, filt):
                key = ("card-is-event-ba", event)
                if key not in best_per_event_ab:
                    best_per_event_ab[key] = 2.0
                    if filt and any(k in filt for k in ("subtype",)):
                        best_per_event_ab[key] = 3.0

    # A's EFFECT targets B's type (e.g., Kaalia puts Angel/Demon/Dragon from hand)
    for m in card_a_mechs:
        detail = m.get("detail") or {}
        if isinstance(detail, dict):
            target_filter = detail.get("target_filter")
            if target_filter and isinstance(target_filter, dict):
                if card_satisfies_filter(b_is, target_filter):
                    key = ("effect-targets-b",)
                    if key not in best_per_event_ab:
                        best_per_event_ab[key] = 3.0

    # B's EFFECT targets A's type
    for m in card_b_mechs:
        detail = m.get("detail") or {}
        if isinstance(detail, dict):
            target_filter = detail.get("target_filter")
            if target_filter and isinstance(target_filter, dict):
                if card_satisfies_filter(a_is, target_filter):
                    key = ("effect-targets-a",)
                    if key not in best_per_event_ab:
                        best_per_event_ab[key] = 3.0

    # Take best match per direction instead of summing all.
    # A card with 5 weak event chains shouldn't beat a card with 1 strong chain.
    ab_scores = [v for k, v in best_per_event_ab.items() if k[0] in ("ab", "card-is-event", "effect-targets-b")]
    ba_scores = [v for k, v in best_per_event_ab.items() if k[0] in ("ba", "card-is-event-ba", "effect-targets-a")]
    # Best match in each direction + bonus if BOTH directions match (bidirectional)
    best_ab = max(ab_scores) if ab_scores else 0
    best_ba = max(ba_scores) if ba_scores else 0
    score += best_ab + best_ba
    if best_ab > 0 and best_ba > 0:
        score += 1.0  # Bidirectional bonus

    # --- 2. Shared trigger synergy (FIX #4) ---
    # Best single shared trigger only (not accumulated)
    best_shared = 0.0
    for resp_a in a_responds:
        for resp_b in b_responds:
            if resp_a["event"] == resp_b["event"]:
                if filters_compatible(resp_a["filter"], resp_b["filter"]):
                    has_specific_filter = (
                        (resp_a["filter"] and any(k in resp_a["filter"] for k in ("subtype", "card_type"))) or
                        (resp_b["filter"] and any(k in resp_b["filter"] for k in ("subtype", "card_type")))
                    )
                    best_shared = max(best_shared, 1.5 if has_specific_filter else 0.8)
    score += best_shared

    # --- 3. Modifiers: A modifies what B does (and vice versa) ---
    a_mods = card_modifies(card_a_mechs)
    b_mods = card_modifies(card_b_mechs)
    a_actions = {m["action"] for m in card_a_mechs if m.get("action")}
    b_actions = {m["action"] for m in card_b_mechs if m.get("action")}

    for mod in a_mods:
        target = (mod["modifies"] or "").lower()
        for action in b_actions:
            if action and action.lower() in target:
                score += 3.0
                break  # One modifier match is enough

    for mod in b_mods:
        target = (mod["modifies"] or "").lower()
        for action in a_actions:
            if action and action.lower() in target:
                score += 3.0
                break

    # --- 4. Enablers: static scope matches (FIX #5) ---
    a_enables = card_enables(card_a_mechs)
    b_enables = card_enables(card_b_mechs)
    b_subtypes = _extract_subtypes(card_b_type_line)
    a_subtypes = _extract_subtypes(card_a_type_line)

    # A enables B / B enables A — best single enabler match per direction
    best_enable_ab = 0.0
    for en in a_enables:
        scope = en.get("scope") or {}
        scope_subtype = scope.get("subtype", "")
        if isinstance(scope_subtype, list):
            scope_subtype = scope_subtype[0] if scope_subtype else ""
        if scope_subtype and isinstance(scope_subtype, str):
            if scope_subtype.lower() in b_subtypes:
                best_enable_ab = max(best_enable_ab, 1.5)
            elif scope_subtype.lower() == "creature" and b_subtypes:
                best_enable_ab = max(best_enable_ab, 0.5)
        elif en["action"] in ("grant-keyword", "pump", "reduce-cost") and not scope:
            if b_subtypes:
                best_enable_ab = max(best_enable_ab, 0.3)
    score += best_enable_ab

    best_enable_ba = 0.0
    for en in b_enables:
        scope = en.get("scope") or {}
        scope_subtype = scope.get("subtype", "")
        if isinstance(scope_subtype, list):
            scope_subtype = scope_subtype[0] if scope_subtype else ""
        if scope_subtype and isinstance(scope_subtype, str):
            if scope_subtype.lower() in a_subtypes:
                best_enable_ba = max(best_enable_ba, 1.5)
            elif scope_subtype.lower() == "creature" and a_subtypes:
                best_enable_ba = max(best_enable_ba, 0.5)
        elif en["action"] in ("grant-keyword", "pump", "reduce-cost") and not scope:
            if a_subtypes:
                best_enable_ba = max(best_enable_ba, 0.3)
    score += best_enable_ba

    return score


def compute_deck_synergies(commander_oid: str, candidate_oids: list[str],
                           all_mechanics: dict, card_types: dict,
                           deck_oids: set = None) -> dict:
    """Compute mechanics-based synergy scores for candidates against a commander.

    If deck_oids is provided, also computes two-step chains:
    candidate → deck_card → commander (indirect synergy via deck cards).
    """
    cmdr_mechs = all_mechanics.get(commander_oid, [])
    if not cmdr_mechs:
        return {}

    cmdr_type = card_types.get(commander_oid, "")
    scores = {}

    # Direct: candidate ↔ commander
    for oid in candidate_oids:
        card_mechs = all_mechanics.get(oid, [])
        if not card_mechs:
            continue
        card_type = card_types.get(oid, "")
        s = compute_synergy(cmdr_mechs, card_mechs, cmdr_type, card_type)
        if s > 0:
            scores[oid] = s

    # Two-step chains: candidate → deck_card → commander
    # A candidate that synergizes with deck cards (which synergize with commander)
    # gets a bonus. This catches indirect synergies like:
    # "Buried Alive fills graveyard → Meren reanimates from graveyard"
    if deck_oids:
        # Pre-compute: which events does the commander respond to?
        cmdr_responds = set()
        for r in card_responds_to(cmdr_mechs):
            cmdr_responds.add(r["event"])

        # Pre-compute: for each deck card, what events does it respond to
        # and does it produce events the commander wants?
        # deck_responds_to_event: event → list of deck_oids that respond AND produce cmdr events
        deck_bridges = {}  # event → set of deck_oids that bridge candidate→commander
        for deck_oid in deck_oids:
            deck_mechs = all_mechanics.get(deck_oid, [])
            if not deck_mechs:
                continue
            # Check if this deck card produces any event the commander responds to
            deck_events = card_produces_events(deck_mechs)
            produces_cmdr_event = any(dev["event"] in cmdr_responds for dev in deck_events)
            if not produces_cmdr_event:
                continue
            # Record which events this deck card responds to (with filters)
            for deck_resp in card_responds_to(deck_mechs):
                deck_bridges.setdefault(deck_resp["event"], []).append(
                    {"oid": deck_oid, "filter": deck_resp["filter"]}
                )

        # For each candidate: does it produce events that bridge through deck cards?
        if deck_bridges:
            for oid in candidate_oids:
                card_mechs = all_mechanics.get(oid, [])
                if not card_mechs:
                    continue

                candidate_events = card_produces_events(card_mechs)
                bonus = 0.0

                for cev in candidate_events:
                    bridges = deck_bridges.get(cev["event"])
                    if not bridges:
                        continue
                    for bridge in bridges:
                        if filter_matches(bridge["filter"], cev["output"]):
                            bonus = 1.0  # Two-step chain found
                            break
                    if bonus > 0:
                        break

                if bonus > 0:
                    scores[oid] = scores.get(oid, 0) + bonus

    return scores
