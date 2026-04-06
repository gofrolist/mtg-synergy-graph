"""Full Forge DSL import with shallow SVar resolution.

Single-pass import with deferred resolution:
  1. Collect SVars, metadata, and buffer ability lines in one pass
  2. Resolve Execute$ references via SVars after all SVars are collected
"""
import logging
import os

_log = logging.getLogger(__name__)

CARDS_DIR_DEFAULT = os.path.join("data", "forge", "forge-gui", "res", "cardsfolder")


def ensure_forge_schema(conn):
    """Create Forge tables."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS forge_abilities (
            card_name TEXT NOT NULL,
            ability_index INTEGER NOT NULL,
            ability_type TEXT NOT NULL,
            verb TEXT,
            trigger_mode TEXT,
            trigger_filter TEXT,
            trigger_origin TEXT,
            trigger_destination TEXT,
            trigger_phase TEXT,
            trigger_zones TEXT,
            target TEXT,
            defined TEXT,
            amount TEXT,
            cost TEXT,
            keyword TEXT,
            token_script TEXT,
            counter_type TEXT,
            sub_ability TEXT,
            unless_cost TEXT,
            raw_line TEXT NOT NULL,
            PRIMARY KEY (card_name, ability_index)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS forge_deck_tags (
            card_name TEXT NOT NULL,
            tag_type TEXT NOT NULL,
            tag TEXT NOT NULL,
            PRIMARY KEY (card_name, tag_type, tag)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_forge_ab_name ON forge_abilities(card_name)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_forge_tags_name ON forge_deck_tags(card_name)")
    conn.commit()


def _parse_kv_line(line: str) -> dict:
    """Parse 'Key$ Value | Key2$ Value2' into dict."""
    fields = {}
    for pair in line.split(" | "):
        pair = pair.strip()
        if "$ " in pair:
            key, val = pair.split("$ ", 1)
            fields[key.strip()] = val.strip()
        elif "$" in pair:
            key, val = pair.split("$", 1)
            fields[key.strip()] = val.strip()
    return fields


def shallow_svar_resolve(svar_name: str, svars: dict) -> dict:
    """Resolve one level of SVar to extract verb and parameters.

    Input: SVar value like 'DB$ Draw | Defined$ You | NumCards$ 1'
    Returns: {verb, amount, defined, target, keyword, ...}
    """
    svar_value = svars.get(svar_name, "")
    if not svar_value:
        return {}
    fields = _parse_kv_line(svar_value)
    result = {}
    result["verb"] = fields.get("DB") or fields.get("SP")
    result["defined"] = fields.get("Defined")
    result["target"] = fields.get("ValidTgts") or fields.get("Tgt")
    result["amount"] = (fields.get("NumDmg") or fields.get("NumCards")
                        or fields.get("TokenAmount") or fields.get("CounterNum")
                        or fields.get("LifeAmount") or fields.get("Amount"))
    result["keyword"] = fields.get("KW")
    result["token_script"] = fields.get("TokenScript")
    result["counter_type"] = fields.get("CounterType")
    result["unless_cost"] = fields.get("UnlessCost")
    result["sub_ability"] = fields.get("SubAbility")
    return {k: v for k, v in result.items() if v is not None}


def extract_ability_fields(line: str, prefix: str, svars: dict) -> dict:
    """Extract structured fields from an ability line."""
    fields = _parse_kv_line(line)

    result = {
        "ability_type": prefix,
        "raw_line": f"{prefix}:{line}",
        "verb": None,
        "trigger_mode": None,
        "trigger_filter": None,
        "trigger_origin": None,
        "trigger_destination": None,
        "trigger_phase": None,
        "trigger_zones": None,
        "target": None,
        "defined": None,
        "amount": None,
        "cost": None,
        "keyword": None,
        "token_script": None,
        "counter_type": None,
        "sub_ability": None,
        "unless_cost": None,
    }

    if prefix == "A":
        result["verb"] = fields.get("SP") or fields.get("AB")
        result["cost"] = fields.get("Cost")
    elif prefix == "T":
        result["trigger_mode"] = fields.get("Mode")
        result["trigger_filter"] = fields.get("ValidCard")
        result["trigger_origin"] = fields.get("Origin")
        result["trigger_destination"] = fields.get("Destination")
        result["trigger_phase"] = fields.get("Phase")
        result["trigger_zones"] = fields.get("TriggerZones")
        # Shallow SVar resolution for verb
        execute_ref = fields.get("Execute")
        if execute_ref:
            resolved = shallow_svar_resolve(execute_ref, svars)
            result["verb"] = resolved.get("verb")
            if not result.get("amount"):
                result["amount"] = resolved.get("amount")
            if not result.get("defined"):
                result["defined"] = resolved.get("defined")
            if not result.get("target"):
                result["target"] = resolved.get("target")
            if not result.get("keyword"):
                result["keyword"] = resolved.get("keyword")
            if not result.get("token_script"):
                result["token_script"] = resolved.get("token_script")
            if not result.get("counter_type"):
                result["counter_type"] = resolved.get("counter_type")
            if not result.get("unless_cost"):
                result["unless_cost"] = resolved.get("unless_cost")
            if not result.get("sub_ability"):
                result["sub_ability"] = resolved.get("sub_ability")
            # Append Execute$ SVar content to raw_line so downstream
            # parsers (mechanics_vectors.py) can extract the effect's
            # zone/type info.  The trigger line has the trigger context
            # (Origin$ Any = self enters), but the SVar has the actual
            # effect context (Origin$ Graveyard, ChangeType$ Land).
            svar_value = svars.get(execute_ref, "")
            if svar_value and "|EXEC|" not in result["raw_line"]:
                result["raw_line"] += " |EXEC| " + svar_value
    elif prefix == "S":
        result["verb"] = fields.get("SP") or fields.get("Mode")
    elif prefix == "R":
        # Replacement effects: Event$ is the event being replaced/amplified,
        # ReplaceWith$ is what replaces it, ValidPlayer$ is who's affected.
        # Do NOT store Event$ as verb — that would pollute forge_profiles
        # and create false verb alignment (e.g., Bruvac "Mill" matching Sidisi's
        # ChangesZone trigger). Instead, mechanics_vectors.py parses Event$
        # and ValidPlayer$ directly from raw_line.
        # Store player targeting for downstream use
        result["target"] = (fields.get("ValidPlayer")
                            or fields.get("ValidTarget")
                            or fields.get("ValidSource"))
        result["defined"] = fields.get("ValidSource")
    elif prefix == "K":
        # K: lines store keyword name directly
        kw_part = line.split("|")[0].strip()
        if ":" in kw_part:
            result["keyword"] = kw_part.split(":")[0].strip()
        else:
            result["keyword"] = kw_part.strip()

    # Common fields across all types
    if not result.get("target"):
        result["target"] = fields.get("ValidTgts") or fields.get("Tgt")
    if not result.get("defined"):
        result["defined"] = fields.get("Defined")
    if not result.get("amount"):
        result["amount"] = (fields.get("NumDmg") or fields.get("NumCards")
                            or fields.get("TokenAmount") or fields.get("CounterNum")
                            or fields.get("Amount"))
    if not result.get("keyword"):
        result["keyword"] = fields.get("KW")
    if not result.get("token_script"):
        result["token_script"] = fields.get("TokenScript")
    if not result.get("counter_type"):
        result["counter_type"] = fields.get("CounterType")
    if not result.get("sub_ability"):
        result["sub_ability"] = fields.get("SubAbility")
    if not result.get("unless_cost"):
        result["unless_cost"] = fields.get("UnlessCost")

    return result


def _follow_sub_abilities(parent_ab: dict, svars: dict, ab_idx_start: int,
                          max_depth: int = 10) -> list[dict]:
    """Walk SubAbility$ chain from a parent ability, emitting one row per sub-verb.

    Each sub-ability inherits the parent's trigger context (trigger_mode, filter, etc.)
    so the causal graph knows the sub-effect fires in the same trigger context.
    """
    result = []
    visited = set()
    current_ref = parent_ab.get("sub_ability")
    idx = ab_idx_start

    while current_ref and current_ref not in visited and len(result) < max_depth:
        visited.add(current_ref)
        resolved = shallow_svar_resolve(current_ref, svars)
        verb = resolved.get("verb")
        if not verb:
            break

        sub_ab = {
            "ability_type": parent_ab.get("ability_type", "A"),
            "ability_index": idx,
            "verb": verb,
            # Inherit trigger context from parent
            "trigger_mode": parent_ab.get("trigger_mode"),
            "trigger_filter": parent_ab.get("trigger_filter"),
            "trigger_origin": parent_ab.get("trigger_origin"),
            "trigger_destination": parent_ab.get("trigger_destination"),
            "trigger_phase": parent_ab.get("trigger_phase"),
            "trigger_zones": parent_ab.get("trigger_zones"),
            # Sub-ability's own fields
            "target": resolved.get("target"),
            "defined": resolved.get("defined"),
            "amount": resolved.get("amount"),
            "cost": None,
            "keyword": resolved.get("keyword"),
            "token_script": resolved.get("token_script"),
            "counter_type": resolved.get("counter_type"),
            "sub_ability": resolved.get("sub_ability"),
            "unless_cost": resolved.get("unless_cost"),
            "raw_line": f"Sub:{current_ref}:{svars.get(current_ref, '')}",
        }
        result.append(sub_ab)
        idx += 1
        current_ref = resolved.get("sub_ability")

    return result


def parse_forge_card_file(text: str) -> dict:
    """Parse a Forge card file text into structured data.

    Returns: {name, abilities: [...], svars: {...}, deck_tags: [...]}
    """
    name = None
    svars = {}
    abilities = []
    deck_tags = []
    ab_idx = 0

    # Single pass: collect SVars/metadata and remember ability lines
    ability_lines: list[tuple[str, str]] = []  # (prefix, line_body)
    for line in text.strip().split("\n"):
        line = line.strip()
        if line.startswith("Name:"):
            name = line[5:].strip()
        elif line.startswith("SVar:"):
            # SVar:VarName:value
            rest = line[5:]
            colon_idx = rest.index(":")
            svar_name = rest[:colon_idx].strip()
            svar_value = rest[colon_idx + 1:].strip()
            svars[svar_name] = svar_value
        elif line.startswith("DeckHas:"):
            for tag in line[8:].strip().split(" & "):
                deck_tags.append({"tag_type": "has", "tag": tag.strip()})
        elif line.startswith("DeckHints:"):
            for tag in line[10:].strip().split(" & "):
                deck_tags.append({"tag_type": "hints", "tag": tag.strip()})
        elif line.startswith("DeckNeeds:"):
            for tag in line[10:].strip().split(" & "):
                deck_tags.append({"tag_type": "needs", "tag": tag.strip()})
        else:
            for p in ("A:", "T:", "S:", "K:", "R:"):
                if line.startswith(p):
                    ability_lines.append((p[0], line[len(p):]))
                    break

    # Resolve ability lines (SVars now fully collected)
    for prefix, line_body in ability_lines:
        ab = extract_ability_fields(line_body, prefix, svars)
        ab["ability_index"] = ab_idx
        abilities.append(ab)
        ab_idx += 1

    # Pass 3: Follow SubAbility$ chains to capture secondary effects
    sub_idx = 10000
    additional = []
    for ab in abilities:
        if ab.get("sub_ability"):
            subs = _follow_sub_abilities(ab, svars, sub_idx)
            additional.extend(subs)
            sub_idx += len(subs)
    abilities.extend(additional)

    return {
        "name": name,
        "abilities": abilities,
        "svars": svars,
        "deck_tags": deck_tags,
    }


def import_card_to_db(conn, card: dict):
    """Insert a parsed card into the forge_* tables."""
    name = card["name"]
    if not name:
        return

    for ab in card["abilities"]:
        conn.execute(
            "INSERT OR REPLACE INTO forge_abilities VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (name, ab["ability_index"], ab["ability_type"], ab.get("verb"),
             ab.get("trigger_mode"), ab.get("trigger_filter"),
             ab.get("trigger_origin"), ab.get("trigger_destination"),
             ab.get("trigger_phase"), ab.get("trigger_zones"),
             ab.get("target"), ab.get("defined"), ab.get("amount"),
             ab.get("cost"), ab.get("keyword"), ab.get("token_script"),
             ab.get("counter_type"), ab.get("sub_ability"),
             ab.get("unless_cost"), ab.get("raw_line", "")),
        )

    for tag in card["deck_tags"]:
        conn.execute(
            "INSERT OR IGNORE INTO forge_deck_tags VALUES (?,?,?)",
            (name, tag["tag_type"], tag["tag"]),
        )



def import_all(conn, cards_dir=None):
    """Import all Forge card files to DB."""
    if cards_dir is None:
        cards_dir = CARDS_DIR_DEFAULT

    ensure_forge_schema(conn)
    conn.execute("DELETE FROM forge_abilities")
    conn.execute("DELETE FROM forge_deck_tags")

    if not os.path.exists(cards_dir):
        print(f"Forge cards not found at {cards_dir}")
        return 0

    imported = 0
    errors = 0
    for root, dirs, files in os.walk(cards_dir):
        for fname in files:
            if not fname.endswith(".txt"):
                continue
            try:
                with open(os.path.join(root, fname), "r", errors="ignore") as f:
                    text = f.read()
                card = parse_forge_card_file(text)
                if card["name"]:
                    import_card_to_db(conn, card)
                    imported += 1
            except Exception as exc:
                _log.warning("Failed to import %s: %s", fname, exc)
                errors += 1

    conn.commit()
    print(f"Imported {imported} cards ({errors} errors)")
    return imported


def build_name_mapping(conn):
    """Build forge_name -> oracle_id mapping for card matching."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS forge_name_map (
            forge_name TEXT PRIMARY KEY,
            oracle_id TEXT NOT NULL
        )
    """)
    conn.execute("DELETE FROM forge_name_map")

    # Exact match — prefer non-token versions (tokens have CMC 0, wrong type_line)
    conn.execute("""
        INSERT OR IGNORE INTO forge_name_map (forge_name, oracle_id)
        SELECT DISTINCT fa.card_name, c.oracle_id
        FROM forge_abilities fa
        JOIN cards c ON c.name = fa.card_name
        WHERE c.type_line NOT LIKE '%Token%'
    """)

    # Fallback: token version if no real card exists
    conn.execute("""
        INSERT OR IGNORE INTO forge_name_map (forge_name, oracle_id)
        SELECT DISTINCT fa.card_name, c.oracle_id
        FROM forge_abilities fa
        JOIN cards c ON c.name = fa.card_name
        WHERE fa.card_name NOT IN (SELECT forge_name FROM forge_name_map)
    """)

    # DFC front face match
    conn.execute("""
        INSERT OR IGNORE INTO forge_name_map (forge_name, oracle_id)
        SELECT DISTINCT fa.card_name, c.oracle_id
        FROM forge_abilities fa
        JOIN cards c ON c.name LIKE fa.card_name || ' //%'
        WHERE fa.card_name NOT IN (SELECT forge_name FROM forge_name_map)
    """)

    # DFC/MDFC back face match (e.g., 'Harnfel, Horn of Bounty' → 'Birgi, God of Storytelling // Harnfel, Horn of Bounty')
    conn.execute("""
        INSERT OR IGNORE INTO forge_name_map (forge_name, oracle_id)
        SELECT DISTINCT fa.card_name, c.oracle_id
        FROM forge_abilities fa
        JOIN cards c ON c.name LIKE '%// ' || fa.card_name
        WHERE fa.card_name NOT IN (SELECT forge_name FROM forge_name_map)
    """)

    conn.commit()
    matched = conn.execute("SELECT COUNT(*) FROM forge_name_map").fetchone()[0]
    total = conn.execute("SELECT COUNT(DISTINCT card_name) FROM forge_abilities").fetchone()[0]
    print(f"Name mapping: {matched}/{total} Forge cards matched to Scryfall oracle_ids")
    return matched


def show_stats(conn):
    """Print Forge import statistics: card/ability/trigger counts, deck tags, and SVars."""
    ensure_forge_schema(conn)
    cards = conn.execute("SELECT COUNT(DISTINCT card_name) FROM forge_abilities").fetchone()[0]
    abilities = conn.execute("SELECT COUNT(*) FROM forge_abilities").fetchone()[0]
    with_verb = conn.execute("SELECT COUNT(*) FROM forge_abilities WHERE verb IS NOT NULL").fetchone()[0]
    triggers = conn.execute("SELECT COUNT(*) FROM forge_abilities WHERE ability_type = 'T'").fetchone()[0]
    trig_with_verb = conn.execute(
        "SELECT COUNT(*) FROM forge_abilities WHERE ability_type = 'T' AND verb IS NOT NULL"
    ).fetchone()[0]
    deck_has = conn.execute("SELECT COUNT(*) FROM forge_deck_tags WHERE tag_type = 'has'").fetchone()[0]
    deck_hints = conn.execute("SELECT COUNT(*) FROM forge_deck_tags WHERE tag_type = 'hints'").fetchone()[0]

    print(f"Forge import stats:")
    print(f"  Cards: {cards}")
    print(f"  Abilities: {abilities} ({with_verb} with verb)")
    print(f"  Triggers: {triggers} ({trig_with_verb} with resolved verb via SVar)")
    print(f"  DeckHas tags: {deck_has}")
    print(f"  DeckHints tags: {deck_hints}")
