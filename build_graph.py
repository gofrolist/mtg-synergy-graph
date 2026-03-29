#!/usr/bin/env python3
"""Build the causal interaction graph from parsed abilities."""
import argparse
from mtg_synergy.db import get_connection
from mtg_synergy.parse import load_parsed, ensure_parse_schema
from mtg_synergy.causal import build_and_store_graph, ensure_causal_schema


def _build_synthetic_edges(conn, idx, name_to_oid):
    """Build edges for implicit game events.

    Existing: SpellCast (instants/sorceries), Attacks (creatures), LandPlayed (lands)
    New: ChangesZone+Battlefield — permanents entering the battlefield implicitly
         trigger cards that care about creature/artifact/enchantment ETB.

    Creates "exact" edges for subtype matches (e.g., Human creature → Kyler)
    and "broad" edges for card_type matches (e.g., any creature → Soul Warden).
    Streams edges to DB in chunks to avoid holding millions in memory.
    """
    import json as _json
    from mtg_synergy.parse.forge_filter_parser import parse_forge_filter
    from mtg_synergy.parse.forge_types import ForgeFilter

    # (trigger_mode, card_type_sql, resp_dest_filter, include_broad)
    # resp_dest_filter: only include responders matching this destination (None = any)
    # include_broad: also include card_type-only responders (not just subtype)
    SYNTHETIC_EVENTS = [
        ("SpellCast", "c.type_line NOT LIKE '%Land%'", None, False),
        ("Attacks", "c.type_line LIKE '%Creature%'", None, False),
        ("LandPlayed", "c.type_line LIKE '%Land%'", None, False),
        # Permanents entering battlefield
        ("ChangesZone", "c.type_line LIKE '%Creature%'", "Battlefield", True),
        ("ChangesZone", "c.type_line LIKE '%Artifact%'", "Battlefield", True),
        ("ChangesZone", "c.type_line LIKE '%Enchantment%'", "Battlefield", True),
        ("ChangesZone", "c.type_line LIKE '%Planeswalker%'", "Battlefield", True),
    ]

    total = 0
    for mode, type_filter, resp_dest, include_broad in SYNTHETIC_EVENTS:
        responders = idx.responders_for(mode)
        if not responders:
            continue

        # Pre-parse responder filters into subtype (exact) and card_type (broad)
        subtype_resps = []
        broad_resps = []
        for resp_name, resp_idx, resp_filter_str, resp_origin, resp_dest_r in responders:
            # Skip Self-only triggers
            if resp_filter_str and "Self" in resp_filter_str and "Other" not in resp_filter_str:
                continue
            # Filter by destination if required (e.g., only Battlefield for ETB)
            if resp_dest and resp_dest_r and resp_dest not in resp_dest_r:
                continue
            resp_filter = parse_forge_filter(resp_filter_str) if resp_filter_str else ForgeFilter()
            resp_id = name_to_oid.get(resp_name)
            if not resp_id:
                continue
            if resp_filter.subtypes:
                subtype_resps.append((resp_name, resp_id, resp_idx, resp_filter))
            elif include_broad and resp_filter.card_types:
                broad_resps.append((resp_name, resp_id, resp_idx, resp_filter))

        if not subtype_resps and not broad_resps:
            continue

        # Build subtype lookup for fast filtering
        wanted_subtypes = set()
        for _, _, _, rf in subtype_resps:
            for st in rf.subtypes:
                wanted_subtypes.add(st.lower())

        # Build card_type lookup for broad responders
        broad_card_types = set()
        for _, _, _, rf in broad_resps:
            for ct in rf.card_types:
                broad_card_types.add(ct.lower())

        # Stream through producer cards
        batch = []
        for row in conn.execute(
            f"SELECT fnm.forge_name, c.oracle_id, c.type_line FROM cards c "
            f"JOIN forge_name_map fnm ON fnm.oracle_id = c.oracle_id "
            f"WHERE ({type_filter}) AND c.type_line NOT LIKE '%Token%'"
        ):
            prod_name, prod_oid, prod_type = row
            if not prod_type:
                continue
            prod_type_lower = prod_type.lower()

            # Exact matches: producer has a subtype that a responder wants
            if wanted_subtypes and any(st in prod_type_lower for st in wanted_subtypes):
                for resp_name, resp_oid, resp_idx, resp_filter in subtype_resps:
                    if prod_oid == resp_oid:
                        continue
                    if any(st.lower() in prod_type_lower for st in resp_filter.subtypes):
                        detail = _json.dumps({"event": mode, "filter_precision": "exact"})
                        batch.append((prod_oid, resp_oid, "triggers", -1, resp_idx, 0.3, detail))

            # Broad matches: producer's card type matches responder's required type
            if broad_resps:
                for resp_name, resp_oid, resp_idx, resp_filter in broad_resps:
                    if prod_oid == resp_oid:
                        continue
                    if any(ct.lower() in prod_type_lower for ct in resp_filter.card_types):
                        detail = _json.dumps({"event": mode, "filter_precision": "broad"})
                        batch.append((prod_oid, resp_oid, "triggers", -1, resp_idx, 0.15, detail))

            if len(batch) >= 10000:
                conn.executemany(
                    "INSERT OR IGNORE INTO interaction_edges (source_id, target_id, edge_type, ability_a, ability_b, strength, detail) VALUES (?,?,?,?,?,?,?)",
                    batch)
                total += len(batch)
                batch.clear()

        if batch:
            conn.executemany(
                "INSERT OR IGNORE INTO interaction_edges (source_id, target_id, edge_type, ability_a, ability_b, strength, detail) VALUES (?,?,?,?,?,?,?)",
                batch)
            total += len(batch)
            batch.clear()

        conn.commit()

    return total


def _build_entity_presence_edges(conn, name_to_oid):
    """Build edges for state-based entity presence synergies.

    Connects cards that PRODUCE entities (tokens, creatures of specific types)
    to cards that BENEFIT from those entities existing:
    - "for each [Type]" scaling effects (Brightstone Ritual, Battle Hymn)
    - "Sac<N/Type>" sacrifice outlets (Goblin Bombardment)
    - "tap an untapped [Type]" tap abilities

    These synergies are invisible to event-based trigger matching because
    they scale with board STATE, not individual EVENTS.
    """
    import json as _json
    import re

    # ── Step 1: Build consumer index ────────────────────────────────────
    # Cards that benefit from entities of specific types
    # consumer_types[oid] = set of (type_lower, precision)
    # precision: "exact" for specific subtypes, "broad" for card types

    consumer_types = {}  # oid → {(type, precision)}

    # 1a. Oracle text: "for each [Type]" patterns
    card_types = {"creature", "artifact", "enchantment", "land", "planeswalker",
                  "instant", "sorcery", "permanent", "spell", "nonland",
                  "noncreature", "nontoken", "token"}
    skip_words = {"card", "time", "point", "mana", "of", "the", "counter",
                  "charge", "player", "opponent", "color", "life", "other",
                  "coin", "die", "copy", "way", "them", "instance", "1",
                  "additional", "that", "damage", "those"}

    for row in conn.execute(
        "SELECT oracle_id, oracle_text FROM cards WHERE oracle_text IS NOT NULL"
    ):
        oid, text = row[0], (row[1] or "").lower()
        types_found = set()
        for m in re.finditer(r"for each (\w+)", text):
            t = m.group(1)
            if t in skip_words:
                continue
            if t in card_types:
                types_found.add((t, "broad"))
            else:
                types_found.add((t, "exact"))  # subtype like "goblin"

        # "tap an untapped [Type]" — benefits from having that type
        for m in re.finditer(r"tap (?:an|a|\d+) untapped (\w+)", text):
            t = m.group(1)
            if t in card_types:
                types_found.add((t, "broad"))
            elif t not in skip_words:
                types_found.add((t, "exact"))

        if types_found:
            consumer_types[oid] = types_found

    # 1b. Forge Sac<N/Type> costs
    oid_to_forge = {}
    for row in conn.execute("SELECT oracle_id, forge_name FROM forge_name_map"):
        oid_to_forge[row[0]] = row[1]
    forge_to_oid = {v: k for k, v in oid_to_forge.items()}

    for row in conn.execute(
        "SELECT card_name, raw_line FROM forge_abilities WHERE raw_line LIKE '%Sac<%'"
    ):
        oid = forge_to_oid.get(row[0])
        if not oid:
            continue
        for m in re.findall(r"Sac<\d+/([^/>]+)", row[1]):
            sac_type = m.split("/")[0].strip()
            if sac_type == "CARDNAME":
                continue
            # Parse Forge type specs like "Creature.Other", "Goblin", "Artifact;Creature"
            for part in sac_type.split(";"):
                part = part.split(".")[0].strip()
                if not part:
                    continue
                if part.lower() in card_types:
                    consumer_types.setdefault(oid, set()).add((part.lower(), "broad"))
                else:
                    consumer_types.setdefault(oid, set()).add((part.lower(), "exact"))

    print(f"  Entity consumers: {len(consumer_types)} cards")

    # ── Step 2: Build producer index ────────────────────────────────────
    # Cards that produce entities of specific types

    # 2a. Token creators: parse token_script for creature types
    token_types = {}  # oid → set of type strings
    known_token_types = set()

    for row in conn.execute(
        "SELECT fnm.oracle_id, fa.token_script FROM forge_abilities fa "
        "JOIN forge_name_map fnm ON fnm.forge_name = fa.card_name "
        "WHERE fa.verb = 'Token' AND fa.token_script IS NOT NULL"
    ):
        oid, script = row[0], row[1].lower()
        # Parse token_script: "color_power_toughness_type[_keywords...]"
        # e.g., "r_1_1_goblin", "w_1_1_spirit_flying", "c_a_treasure_sac"
        parts = script.split("_")
        if len(parts) >= 4:
            # Types start at index 3, keywords follow
            keywords = {"flying", "haste", "trample", "vigilance", "deathtouch",
                        "lifelink", "menace", "reach", "defender", "first",
                        "strike", "double", "indestructible", "hexproof",
                        "sac", "unblockable"}
            for part in parts[3:]:
                if part and part not in keywords:
                    token_types.setdefault(oid, set()).add(part)
                    known_token_types.add(part)

    # 2b. Creature cards produce their own subtypes (already in DB)
    # We'll match them against consumers when building edges

    print(f"  Token creators: {len(token_types)} cards, {len(known_token_types)} token types")

    # ── Step 3: Build edges ─────────────────────────────────────────────
    # Match producers → consumers by type

    # Pre-build consumer lookups for fast matching
    exact_consumers = {}  # type_lower → [(oid, ...)]
    broad_consumers = {}  # card_type → [(oid, ...)]
    for oid, types in consumer_types.items():
        for t, prec in types:
            if prec == "exact":
                exact_consumers.setdefault(t, []).append(oid)
            else:
                broad_consumers.setdefault(t, []).append(oid)

    total = 0
    batch = []

    # 3a. Token creators → consumers of that token type
    for prod_oid, types in token_types.items():
        for token_type in types:
            # Exact: consumer wants this specific type
            for cons_oid in exact_consumers.get(token_type, []):
                if prod_oid == cons_oid:
                    continue
                detail = _json.dumps({"event": "EntityPresence",
                                      "filter_precision": "exact"})
                batch.append((prod_oid, cons_oid, "enables", -1, -1, 0.4, detail))

            # Broad: consumer wants "creature" and token is a creature type
            for cons_oid in broad_consumers.get("creature", []):
                if prod_oid == cons_oid:
                    continue
                detail = _json.dumps({"event": "EntityPresence",
                                      "filter_precision": "broad"})
                batch.append((prod_oid, cons_oid, "enables", -1, -1, 0.2, detail))

            if len(batch) >= 10000:
                conn.executemany(
                    "INSERT OR IGNORE INTO interaction_edges "
                    "(source_id, target_id, edge_type, ability_a, ability_b, strength, detail) "
                    "VALUES (?,?,?,?,?,?,?)", batch)
                total += len(batch)
                batch.clear()

    # 3b. Creature cards → consumers of their subtype
    # Stream through all creatures and match subtypes
    for row in conn.execute(
        "SELECT c.oracle_id, c.type_line FROM cards c "
        "WHERE c.type_line LIKE '%Creature%' AND c.type_line LIKE '%—%' "
        "AND c.type_line NOT LIKE '%Token%'"
    ):
        prod_oid, type_line = row
        if not type_line:
            continue
        try:
            subtypes = {s.lower().strip() for s in type_line.split("—")[1].strip().split()}
        except (IndexError, AttributeError):
            continue

        for subtype in subtypes:
            # Exact: consumer wants this specific subtype
            for cons_oid in exact_consumers.get(subtype, []):
                if prod_oid == cons_oid:
                    continue
                detail = _json.dumps({"event": "EntityPresence",
                                      "filter_precision": "exact"})
                batch.append((prod_oid, cons_oid, "enables", -1, -1, 0.25, detail))

            if len(batch) >= 10000:
                conn.executemany(
                    "INSERT OR IGNORE INTO interaction_edges "
                    "(source_id, target_id, edge_type, ability_a, ability_b, strength, detail) "
                    "VALUES (?,?,?,?,?,?,?)", batch)
                total += len(batch)
                batch.clear()

    # 3c. All creatures → broad consumers of "creature"
    # (sacrifice outlets, "for each creature" effects)
    # Skip this — would create too many edges (30k creatures × 300 consumers = 9M)
    # The ChangesZone synthetic edges already cover broad creature-entering triggers

    if batch:
        conn.executemany(
            "INSERT OR IGNORE INTO interaction_edges "
            "(source_id, target_id, edge_type, ability_a, ability_b, strength, detail) "
            "VALUES (?,?,?,?,?,?,?)", batch)
        total += len(batch)
        batch.clear()

    conn.commit()
    return total


def _build_continuous_pump_edges(conn, name_to_oid):
    """Build synthetic edges from Continuous pump abilities to matching creatures.

    Cards with Continuous mode + AddPower$ that Affect other creatures create
    "pumps" edges. E.g., Kyler (Affected$ Card.Human+Other+YouCtrl) creates
    edges to every Human creature.

    This captures the feed-forward relationship: pump card → pumped creature,
    which was previously invisible to the causal graph. Combined with existing
    trigger edges (creature ETB → pump card), this enables the model to detect
    feedback loops like Kyler + Maester Seymour.
    """
    import json as _json
    import re as _re

    # Load all Continuous pump abilities with their Affected$ filter
    pump_cards = []
    for row in conn.execute(
        "SELECT fa.card_name, fa.raw_line "
        "FROM forge_abilities fa "
        "WHERE fa.verb = 'Continuous' AND fa.raw_line LIKE '%AddPower%'"
    ):
        card_name, raw = row
        if not raw:
            continue
        # Parse Affected$ filter
        m = _re.search(r'Affected\$\s*(\S+)', raw)
        if not m:
            continue
        affected = m.group(1)
        # Skip self-only effects — no cross-card synergy
        if 'Self' in affected and 'Other' not in affected:
            continue
        if 'EnchantedBy' in affected or 'EquippedBy' in affected:
            continue

        # Resolve to oracle_id
        oid = name_to_oid.get(card_name)
        if not oid:
            continue

        # Parse what creatures the filter targets
        # Format: Card.Human+Other+YouCtrl or Creature.Goblin+YouCtrl etc.
        # Extract subtypes and card types
        parts = affected.split('.')
        base = parts[0]  # Card, Creature, Permanent
        filters = parts[1].split('+') if len(parts) > 1 else []

        # Extract creature subtypes from filters (capitalized words that aren't keywords)
        _keywords = {'Other', 'YouCtrl', 'OppCtrl', 'attacking', 'blocking',
                     'token', 'nontoken', 'enchanted', 'Legendary', 'tapped',
                     'untapped', 'castSATrigger'}
        _color_words = {'White', 'Blue', 'Black', 'Red', 'Green', 'Colorless'}
        subtypes = set()
        colors = set()
        for f in filters:
            if f in _keywords:
                continue
            if f in _color_words:
                colors.add(f.lower())
                continue
            if f == 'ChosenType':
                continue  # Can't resolve at build time
            subtypes.add(f.lower())

        # Only create edges for subtype-specific pumps (e.g., "Humans get +1/+1").
        # Broad pumps ("all creatures get +1/+1") don't indicate specific synergy
        # and create too many edges (319 × 18k = 5.7M).
        if not subtypes:
            continue

        pump_cards.append((oid, card_name, subtypes, colors, base))

    print(f"  Continuous pump cards: {len(pump_cards)}")

    # Build lookup: creature oracle_id → subtypes (from type_line)
    creature_subtypes = {}
    creature_colors = {}
    for row in conn.execute(
        "SELECT c.oracle_id, c.type_line, c.color_identity "
        "FROM cards c "
        "WHERE c.type_line LIKE '%Creature%' AND c.type_line NOT LIKE '%Token%'"
    ):
        oid, type_line, ci_json = row
        if not type_line:
            continue
        try:
            subs = {s.lower().strip() for s in type_line.split("—")[1].strip().split()}
        except (IndexError, AttributeError):
            subs = set()
        creature_subtypes[oid] = subs
        try:
            ci = set(_json.loads(ci_json or '[]'))
            color_map = {'W': 'white', 'U': 'blue', 'B': 'black', 'R': 'red', 'G': 'green'}
            creature_colors[oid] = {color_map.get(c, c.lower()) for c in ci}
        except (ValueError, TypeError):
            creature_colors[oid] = set()

    # Create edges: pump_card → each matching creature
    batch = []
    total = 0
    for pump_oid, pump_name, req_subtypes, req_colors, base in pump_cards:
        for creature_oid, creature_subs in creature_subtypes.items():
            if creature_oid == pump_oid:
                continue

            # Subtype filter: creature must share at least one subtype
            if not (req_subtypes & creature_subs):
                continue
            precision = "exact"
            strength = 0.3

            # Color filter: if pump requires specific colors, creature must match
            if req_colors:
                c_colors = creature_colors.get(creature_oid, set())
                if not (req_colors & c_colors):
                    continue

            detail = _json.dumps({"event": "ContinuousPump",
                                  "filter_precision": precision})
            batch.append((pump_oid, creature_oid, "pumps", -1, -1, strength, detail))

            if len(batch) >= 10000:
                conn.executemany(
                    "INSERT OR IGNORE INTO interaction_edges "
                    "(source_id, target_id, edge_type, ability_a, ability_b, strength, detail) "
                    "VALUES (?,?,?,?,?,?,?)", batch)
                total += len(batch)
                batch.clear()

    if batch:
        conn.executemany(
            "INSERT OR IGNORE INTO interaction_edges "
            "(source_id, target_id, edge_type, ability_a, ability_b, strength, detail) "
            "VALUES (?,?,?,?,?,?,?)", batch)
        total += len(batch)

    conn.commit()
    return total


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rebuild", action="store_true")
    parser.add_argument("--forge", action="store_true", help="Build from Forge data (new system)")
    parser.add_argument("--stats", action="store_true")
    args = parser.parse_args()
    conn = get_connection()
    ensure_parse_schema(conn)
    ensure_causal_schema(conn)
    if args.forge:
        from mtg_synergy.causal.forge_indexer import build_forge_index
        from mtg_synergy.causal.forge_graph_builder import build_forge_edges
        from mtg_synergy.causal import store_edges
        print("Building Forge-native graph...")
        # Load name→oracle_id mapping so edges use oracle_ids
        name_to_oid = {}
        for row in conn.execute("SELECT forge_name, oracle_id FROM forge_name_map"):
            name_to_oid[row[0]] = row[1]
        print(f"  Name mapping: {len(name_to_oid)} cards")
        idx = build_forge_index(conn)
        edges = build_forge_edges(idx, name_to_oid=name_to_oid)
        count = store_edges(conn, edges)
        print(f"  Forge edges: {count}")

        # Synthetic edges: SpellCast, Attacks, LandPlayed, ChangesZone+Battlefield
        # Streamed directly to DB to avoid holding millions of edges in memory
        syn_count = _build_synthetic_edges(conn, idx, name_to_oid)
        print(f"  Synthetic edges: {syn_count}")

        # Entity presence edges: token creators → scaling/sacrifice/tap cards
        ent_count = _build_entity_presence_edges(conn, name_to_oid)
        print(f"  Entity presence edges: {ent_count}")

        # Continuous pump edges: lord/anthem effects → creatures they buff
        pump_count = _build_continuous_pump_edges(conn, name_to_oid)
        print(f"  Continuous pump edges: {pump_count}")
        print(f"Done: {count + syn_count + ent_count + pump_count} total edges")
    elif args.rebuild:
        rows = conn.execute("SELECT DISTINCT oracle_id FROM parsed_abilities").fetchall()
        cards = {}
        for (oid,) in rows:
            cards[oid] = load_parsed(conn, oid)
        print(f"Building graph from {len(cards)} cards...")
        n_edges = build_and_store_graph(conn, cards)
        print(f"Done: {n_edges} edges built")
    elif args.stats:
        n_edges = conn.execute("SELECT COUNT(*) FROM interaction_edges").fetchone()[0]
        n_cards = conn.execute("SELECT COUNT(DISTINCT source_id) FROM interaction_edges").fetchone()[0]
        print(f"Edges: {n_edges}")
        print(f"Cards with edges: {n_cards}")
    else:
        parser.print_help()
    conn.close()

if __name__ == "__main__":
    main()
