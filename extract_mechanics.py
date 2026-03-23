#!/usr/bin/env python3
"""Extract structured game mechanics from MTG cards using LLM.

Extracts what each card DOES in terms of game events — triggers, effects,
filters, modifiers. This is deeper than provides/wants tags: it captures
the CONDITIONS under which abilities work, enabling filter-aware matching.

Usage:
    python3 extract_mechanics.py --batch 1000       # Extract first 1000 unprocessed cards
    python3 extract_mechanics.py --batch 1000 --offset 1000  # Next 1000
    python3 extract_mechanics.py --card "Krenko, Mob Boss"   # Single card
    python3 extract_mechanics.py --validate          # Test matching quality
    python3 extract_mechanics.py --stats              # Show extraction progress
    python3 extract_mechanics.py --dry-run --batch 1000  # Cost estimate
"""

import argparse
import json
import os
import re
import sqlite3
import sys
import time
import urllib.request
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

DB_PATH = os.path.join(os.path.dirname(__file__), "data", "tags.db")
CARDS_PER_BATCH = 10  # Cards per API call (small for reliability)
MAX_WORKERS = 5

# ── Canonical vocabulary ────────────────────────────────────────────────────
# These are the ONLY terms the LLM should use. Matching depends on consistency.

ACTIONS = [
    "create-token", "deal-damage", "draw-card", "put-counter", "remove-counter",
    "add-mana", "exile", "destroy", "sacrifice", "return-to-hand",
    "return-to-battlefield", "gain-life", "lose-life", "mill", "discard",
    "search-library", "reduce-cost", "grant-keyword", "pump", "untap",
    "extra-combat", "extra-turn", "copy", "counter-spell", "protect",
    "tap", "phase-out", "transform", "scry", "surveil",
    # Set-specific keyword actions
    "amass", "ring-tempts", "discover", "explore", "connive",
    "cascade", "adapt", "proliferate", "venture",
]

EVENTS = [
    "creature-enters", "creature-dies", "spell-cast", "permanent-leaves",
    "combat-damage-dealt", "attack-declared", "land-enters", "counter-placed",
    "life-gained", "life-lost", "card-drawn", "card-discarded",
    "beginning-of-combat", "upkeep", "end-step", "damage-dealt",
    "token-created", "equipped", "enchanted", "artifact-enters",
    "enchantment-enters", "you-gain-life", "opponent-loses-life",
    "creature-attacks", "creature-blocks",
    # Set-specific trigger events
    "ring-tempted", "army-deals-combat-damage",
]

# Action → what game events it produces
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
    "grant-keyword": [],
    "pump": [],
    "reduce-cost": [],
    "add-mana": [],
    "search-library": [],
    "untap": [],
    "extra-combat": ["beginning-of-combat", "attack-declared"],
    "copy": ["creature-enters"],
    "scry": [],
    "surveil": ["card-discarded"],
}


# ── Schema ──────────────────────────────────────────────────────────────────

def ensure_schema(conn):
    """Create card_mechanics table."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS card_mechanics (
            oracle_id TEXT NOT NULL,
            mechanic_index INTEGER NOT NULL,
            mechanic_type TEXT NOT NULL,
            trigger_event TEXT,
            trigger_filter TEXT,
            effect_action TEXT,
            effect_detail TEXT,
            modifier_target TEXT,
            modifier_how TEXT,
            scope_filter TEXT,
            cost TEXT,
            raw_json TEXT,
            PRIMARY KEY (oracle_id, mechanic_index)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_mechanics_action
        ON card_mechanics(effect_action)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_mechanics_trigger
        ON card_mechanics(trigger_event)
    """)
    conn.commit()


# ── Prompt ──────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = f"""You are a Magic: The Gathering rules parser. Extract structured game mechanics from each card.

For each card, return a JSON object:
{{
  "name": "Card Name",
  "mechanics": [
    {{
      "type": "triggered" | "activated" | "static" | "replacement" | "keyword",
      "trigger": {{                          // only for triggered abilities
        "event": "<EVENT>",                  // what game event triggers this
        "filter": {{...}} | null             // conditions on the triggering event
      }},
      "cost": "<cost string>",               // only for activated abilities
      "effect": {{
        "action": "<ACTION>",                // what this mechanic does
        "target": "self" | "each-opponent" | "target-creature" | "all-creatures-you-control" | ...,
        "amount": <number or "X">,
        "token": {{"subtype": "...", "power": N, "toughness": N}} // for create-token
      }},
      "modifier": {{                         // only for replacement effects
        "modifies": "<ACTION being modified>",
        "how": "double" | "prevent" | "redirect" | ...
      }},
      "scope": {{                            // filter for static/triggered - WHO is affected
        "subtype": "Goblin",                 // creature type filter
        "card_type": "creature",             // card type filter
        "controller": "you" | "opponent",
        "is_another": true,                  // "another" = excludes source
        "is_nontoken": true,
        "has_keyword": "flying"
      }}
    }}
  ]
}}

ALLOWED ACTIONS: {', '.join(ACTIONS)}
ALLOWED TRIGGER EVENTS: {', '.join(EVENTS)}

Rules:
- Use ONLY the allowed actions and events listed above
- trigger.filter and scope use the same field names
- For "whenever another Human enters" → trigger.event="creature-enters", scope.subtype="Human", scope.is_another=true
- For "Goblin spells cost 1 less" → type="static", effect.action="reduce-cost", scope.subtype="Goblin"
- Include keyword abilities as type="keyword" with effect.action="grant-keyword"
- Omit empty/null fields
- Return ONLY a JSON array, no other text"""


def build_user_prompt(cards: list[dict]) -> str:
    lines = []
    for c in cards:
        oracle = (c.get("oracle_text") or "")[:300]
        lines.append(f"CARD: {c['name']} [{c.get('type_line', '')}]")
        lines.append(f"Oracle: {oracle}")
        lines.append("")
    return "\n".join(lines)


# ── API call ────────────────────────────────────────────────────────────────

def call_api(system: str, user: str, api_key: str, model: str) -> str | None:
    for attempt in range(6):
        try:
            token_param = "max_completion_tokens" if "gpt-5" in model else "max_tokens"
            payload = json.dumps({
                "model": model,
                token_param: 8192,
                "temperature": 0.1,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            }).encode()
            req = urllib.request.Request(
                "https://api.openai.com/v1/chat/completions",
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                },
            )
            with urllib.request.urlopen(req, timeout=180) as resp:
                data = json.loads(resp.read())
                return data["choices"][0]["message"]["content"].strip()
        except urllib.request.HTTPError as e:
            if e.code == 429:
                retry_after = e.headers.get("Retry-After")
                wait = float(retry_after) if retry_after else min(2 ** (attempt + 1), 60)
                time.sleep(wait)
            elif e.code >= 500:
                time.sleep(2 ** attempt)
            else:
                body = e.read().decode()[:200]
                print(f"  [API {e.code}] {body}")
                return None
        except Exception as e:
            if attempt < 5:
                time.sleep(2 ** attempt)
            else:
                print(f"  [ERROR] {e}")
    return None


def parse_response(raw: str) -> list[dict] | None:
    if not raw:
        return None
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL)
    raw = re.sub(r"^```json\s*", "", raw.strip())
    raw = re.sub(r"\s*```$", "", raw)
    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r'\[.*\]', raw, re.DOTALL)
        if match:
            try:
                result = json.loads(match.group())
            except json.JSONDecodeError:
                return None
        else:
            return None
    if isinstance(result, dict):
        result = [result]
    return result


# ── Extraction ──────────────────────────────────────────────────────────────

def get_unprocessed_cards(conn, limit=1000, offset=0) -> list[dict]:
    """Get cards that don't have mechanics extracted yet."""
    rows = conn.execute("""
        SELECT c.oracle_id, c.name, c.oracle_text, c.type_line, c.keywords
        FROM cards c
        LEFT JOIN card_mechanics m ON c.oracle_id = m.oracle_id
        WHERE m.oracle_id IS NULL
        AND c.legal_commander = 1
        AND c.oracle_text IS NOT NULL
        AND c.oracle_text != ''
        ORDER BY c.edhrec_rank ASC NULLS LAST
        LIMIT ? OFFSET ?
    """, (limit, offset)).fetchall()
    return [{
        "oracle_id": r[0], "name": r[1], "oracle_text": r[2],
        "type_line": r[3], "keywords": json.loads(r[4] or "[]"),
    } for r in rows]


def store_mechanics(conn, oracle_id: str, mechanics: list[dict]):
    """Store extracted mechanics in DB."""
    conn.execute("DELETE FROM card_mechanics WHERE oracle_id = ?", (oracle_id,))
    for i, m in enumerate(mechanics):
        if not isinstance(m, dict):
            continue
        trigger = m.get("trigger") or {}
        if not isinstance(trigger, dict):
            trigger = {}
        effect = m.get("effect") or {}
        if isinstance(effect, list):
            effect = effect[0] if effect else {}
        if not isinstance(effect, dict):
            effect = {"action": str(effect)}
        modifier = m.get("modifier") or {}
        if not isinstance(modifier, dict):
            modifier = {}
        scope = m.get("scope") or {}
        if not isinstance(scope, dict):
            scope = {}

        # Merge trigger.filter into scope for unified filtering
        trigger_filter = trigger.get("filter") or {}
        if not isinstance(trigger_filter, dict):
            trigger_filter = {}
        combined_filter = {**scope, **trigger_filter} if (scope or trigger_filter) else None

        conn.execute(
            "INSERT INTO card_mechanics VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                oracle_id, i,
                m.get("type", ""),
                trigger.get("event"),
                json.dumps(combined_filter) if combined_filter else None,
                effect.get("action"),
                json.dumps(effect) if effect else None,
                modifier.get("modifies"),
                modifier.get("how"),
                json.dumps(scope) if scope else None,
                m.get("cost"),
                json.dumps(m),
            )
        )


def extract_batch(cards: list[dict], api_key: str, model: str) -> dict:
    """Extract mechanics for a batch. Returns {oracle_id: [mechanics]}."""
    user = build_user_prompt(cards)
    raw = call_api(SYSTEM_PROMPT, user, api_key, model)
    parsed = parse_response(raw)
    if not parsed:
        return {}

    result = {}
    name_to_oid = {c["name"].lower(): c["oracle_id"] for c in cards}
    for card_data in parsed:
        name = card_data.get("name", "")
        oid = name_to_oid.get(name.lower())
        if oid and "mechanics" in card_data:
            result[oid] = card_data["mechanics"]
    return result


def run_extraction(batch_size: int, api_key: str, model: str, dry_run: bool = False):
    """Extract mechanics for unprocessed cards."""
    conn = sqlite3.connect(DB_PATH)
    ensure_schema(conn)

    cards = get_unprocessed_cards(conn, limit=batch_size)
    print(f"Cards to process: {len(cards)}")

    if not cards:
        print("Nothing to extract!")
        conn.close()
        return

    # Batch into groups of CARDS_PER_BATCH
    batches = [cards[i:i + CARDS_PER_BATCH] for i in range(0, len(cards), CARDS_PER_BATCH)]

    # Cost estimate
    input_tokens = (len(SYSTEM_PROMPT) // 4 + 100 * CARDS_PER_BATCH) * len(batches)
    output_tokens = 200 * CARDS_PER_BATCH * len(batches)
    cost = input_tokens * 0.40 / 1e6 + output_tokens * 1.60 / 1e6
    print(f"Batches: {len(batches)} × {CARDS_PER_BATCH} cards")
    print(f"Estimated cost: ${cost:.2f}")

    if dry_run:
        for c in cards[:5]:
            print(f"  - {c['name']}: {c['oracle_text'][:60]}...")
        print(f"  ... and {len(cards) - 5} more")
        conn.close()
        return

    # Process with rate-limited concurrency
    rate_lock = threading.Lock()
    last_time = [0.0]
    total_extracted = 0
    total_failed = 0
    start = time.time()

    def process(batch):
        with rate_lock:
            wait = max(0, 0.3 - (time.time() - last_time[0]))
            if wait > 0:
                time.sleep(wait)
            last_time[0] = time.time()
        return extract_batch(batch, api_key, model)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(process, b): b for b in batches}
        completed = 0
        for future in as_completed(futures):
            batch = futures[future]
            result = future.result()
            completed += 1

            for oid, mechanics in result.items():
                store_mechanics(conn, oid, mechanics)
                total_extracted += 1

            total_failed += len(batch) - len(result)
            conn.commit()

            if completed % 10 == 0 or completed == len(batches):
                rate = completed / (time.time() - start) * 60
                print(f"  Progress: {completed}/{len(batches)} batches, "
                      f"{total_extracted} extracted, {rate:.0f} batches/min")

    # Retry failed
    if total_failed > 0:
        still_missing = get_unprocessed_cards(conn, limit=total_failed)
        if still_missing:
            print(f"  Retrying {len(still_missing)} failed cards...")
            retry_batches = [still_missing[i:i + CARDS_PER_BATCH]
                             for i in range(0, len(still_missing), CARDS_PER_BATCH)]
            for batch in retry_batches:
                time.sleep(1)
                result = extract_batch(batch, api_key, model)
                for oid, mechanics in result.items():
                    store_mechanics(conn, oid, mechanics)
                    total_extracted += 1
                conn.commit()

    elapsed = time.time() - start
    print(f"\nDone: {total_extracted} extracted, {total_failed} failed, {elapsed:.1f}s")

    # Auto-normalize vocabulary drift
    normalized = normalize_vocabulary(conn)
    if normalized:
        print(f"  Normalized: {normalized} entries fixed")

    conn.close()


# ── Vocabulary normalization ────────────────────────────────────────────────

ACTION_NORMALIZE = {
    "enter-tapped": None, "enters-tapped": None, "enter-tapped-unless": None,
    "enter-untapped": None, "attach": None, "equip": None, "cast": None,
    "prevent": "protect", "redirect": "protect", "proliferate": "put-counter",
    "put-onto-battlefield": "return-to-battlefield", "shuffle": None,
    "extra-land": None, "extra-land-play": None, "play-additional-land": None,
    "level-up": "put-counter", "fight": "deal-damage", "move-counter": "put-counter",
    "pay-life": "lose-life", "trigger-additional": "copy",
    "flash": "grant-keyword", "indestructible": "grant-keyword",
    "lifelink": "grant-keyword", "haste": "grant-keyword",
    "flying": "grant-keyword", "trample": "grant-keyword",
    "vigilance": "grant-keyword", "deathtouch": "grant-keyword",
    "menace": "grant-keyword", "hexproof": "grant-keyword",
    "reach": "grant-keyword", "first-strike": "grant-keyword",
    "double-strike": "grant-keyword", "ward": "grant-keyword",
    "convoke": "reduce-cost", "extort": "lose-life",
    "play-from-top": "draw-card", "cast-from-top": "draw-card",
    "cast-from-top-library": "draw-card",
    "cast-without-paying-cost": "reduce-cost",
    "cast-without-paying-mana-cost": "reduce-cost",
    "cannot-be-blocked": "grant-keyword",
    "cannot-be-blocked-by": "grant-keyword",
    "put-into-hand": "return-to-hand", "bounce": "return-to-hand",
    "reveal": "scry", "reveal-top-card": "scry",
    "look-at-top-card": "scry", "look-at-top-cards": "scry",
    "win-game": "deal-damage", "gain-control": None, "goad": None,
    "choose-one": None, "choose-color": None, "choose-subtype": None,
    "choose-target-opponent": None, "enchant": None,
    "no-maximum-hand-size": None, "remove-keyword": None,
    "play": None, "play-exiled-card": None, "play-exiled-cards": None,
    "play-lands-from-graveyard": None, "play-from-nonhand": None,
    "play-land-from-top-library": None, "play-land-from-top-of-library": None,
    "play-land-and-cast-permanent-from-graveyard": None,
    "play-with-top-card-revealed": None, "cast-anytime": None,
    "cast-creature-spells-from-top-library": None,
    "cast-from-graveyard": "return-to-hand",
    "cannot-block": None, "restrict-attack-block": None,
    "restrict": None, "restrict-casting": None,
    "not-a-creature": None, "not-creature": None,
    "not-creature-if-devotion-less-than": None,
    "regenerate": "protect", "attack-each-combat-if-able": None,
    "grant-basic-land-types": None, "ignore-legend-rule": None,
    "add-type": None, "afflict": "deal-damage", "partner": None,
    "optional": None, "reduce-draw": None, "skip-draw-step": None,
    "spend-mana-any-color": None, "tempt-ring": None,
    "discover": "exile", "search-graveyard": "search-library",
    "shuffle-graveyard-into-library": None,
    "shuffle-hand-and-graveyard-into-library-then-draw": "draw-card",
    "pay-life-or-enter-tapped": None, "allow-play-from-graveyard": None,
    "extra-sacrifice": "sacrifice",
}

EVENT_NORMALIZE = {
    "permanent-enters": "creature-enters",
    "activated": None, "activated-ability": None,
    "sacrifice": "creature-dies",
    "search-library": None, "add-mana": None, "copy": None, "tap": None,
    "beginning-of-draw-step": "upkeep", "untap-step": "upkeep",
    "untap step": "upkeep",
    "card-put-into-graveyard": "creature-dies",
    "draw-card": "card-drawn",
    "exile": "permanent-leaves", "land-destroyed": "permanent-leaves",
    "mana-added": None, "mana-spent": None,
    "permanent-tapped": None, "permanent-untaps": None,
    "phase-out": "permanent-leaves",
    "return-to-battlefield": "creature-enters",
    "untap": None, "postcombat-main-phase": "end-step",
    "lore-counter-placed": "counter-placed",
    "lore-counter-added": "counter-placed",
    "card-exiled": "permanent-leaves", "counter-spell": None,
    "class-becomes-level-2": None, "class-level-up": None,
    "siege-enters": "creature-enters",
}

TYPE_NORMALIZE = {
    "instant": "activated", "sorcery": "activated", "spell": "activated",
    "effect": "static", "spell-cast": "triggered",
    "enchantment-enters": "triggered",
}


def normalize_vocabulary(conn) -> int:
    """Normalize off-vocabulary terms in card_mechanics. Returns count of fixes."""
    fixed = 0
    for oid, idx, val in conn.execute(
        "SELECT oracle_id, mechanic_index, effect_action FROM card_mechanics WHERE effect_action IS NOT NULL"
    ):
        if val in ACTION_NORMALIZE:
            new = ACTION_NORMALIZE[val]
            if new is None:
                conn.execute("UPDATE card_mechanics SET effect_action = NULL WHERE oracle_id = ? AND mechanic_index = ?", (oid, idx))
            else:
                conn.execute("UPDATE card_mechanics SET effect_action = ? WHERE oracle_id = ? AND mechanic_index = ?", (new, oid, idx))
            fixed += 1

    for oid, idx, val in conn.execute(
        "SELECT oracle_id, mechanic_index, trigger_event FROM card_mechanics WHERE trigger_event IS NOT NULL"
    ):
        if val in EVENT_NORMALIZE:
            new = EVENT_NORMALIZE[val]
            if new is None:
                conn.execute("UPDATE card_mechanics SET trigger_event = NULL WHERE oracle_id = ? AND mechanic_index = ?", (oid, idx))
            else:
                conn.execute("UPDATE card_mechanics SET trigger_event = ? WHERE oracle_id = ? AND mechanic_index = ?", (new, oid, idx))
            fixed += 1

    for oid, idx, val in conn.execute(
        "SELECT oracle_id, mechanic_index, mechanic_type FROM card_mechanics"
    ):
        if val in TYPE_NORMALIZE:
            conn.execute("UPDATE card_mechanics SET mechanic_type = ? WHERE oracle_id = ? AND mechanic_index = ?",
                         (TYPE_NORMALIZE[val], oid, idx))
            fixed += 1

    conn.commit()
    return fixed


# ── Validation ──────────────────────────────────────────────────────────────

def validate():
    """Test matching quality using known synergy pairs."""
    conn = sqlite3.connect(DB_PATH)
    ensure_schema(conn)

    total = conn.execute("SELECT COUNT(DISTINCT oracle_id) FROM card_mechanics").fetchone()[0]
    print(f"Cards with mechanics: {total}")

    if total < 10:
        print("Need at least 10 cards extracted to validate. Run extraction first.")
        conn.close()
        return

    # Test: for each commander, find top matches using mechanics
    from decks import list_decks, load_deck

    for deck_name in list_decks():
        deck = load_deck(deck_name)
        cmdr_oid = conn.execute("SELECT oracle_id FROM cards WHERE name = ?",
                                (deck.COMMANDER,)).fetchone()
        if not cmdr_oid:
            continue
        cmdr_oid = cmdr_oid[0]

        # Get commander's mechanics
        cmdr_mechs = conn.execute(
            "SELECT effect_action, trigger_event, scope_filter, effect_detail FROM card_mechanics WHERE oracle_id = ?",
            (cmdr_oid,)
        ).fetchall()

        if not cmdr_mechs:
            continue

        # What events does the commander produce?
        cmdr_events = set()
        cmdr_scopes = []
        for action, trigger, scope, detail in cmdr_mechs:
            if action and action in ACTION_TO_EVENTS:
                cmdr_events.update(ACTION_TO_EVENTS[action])
            if scope:
                cmdr_scopes.append(json.loads(scope))
            if detail:
                d = json.loads(detail)
                if d.get("token", {}).get("subtype"):
                    cmdr_events.add(f"creature-enters:{d['token']['subtype']}")

        # Find cards that trigger on commander's events
        matches = []
        for row in conn.execute(
            "SELECT cm.oracle_id, c.name, cm.trigger_event, cm.scope_filter, cm.effect_action "
            "FROM card_mechanics cm JOIN cards c ON c.oracle_id = cm.oracle_id "
            "WHERE cm.trigger_event IS NOT NULL"
        ).fetchall():
            oid, name, trigger_event, scope_filter, effect_action = row
            if trigger_event in cmdr_events:
                # Check scope filter compatibility
                scope = json.loads(scope_filter) if scope_filter else {}
                matches.append((name, trigger_event, scope, effect_action))

        if matches:
            print(f"\n{deck.COMMANDER}: produces events {cmdr_events}")
            print(f"  Top matches ({len(matches)} total):")
            for name, event, scope, action in matches[:10]:
                scope_str = json.dumps(scope) if scope else "any"
                print(f"    {name}: triggers on {event}, filter={scope_str}, does={action}")

    conn.close()


def show_stats():
    conn = sqlite3.connect(DB_PATH)
    ensure_schema(conn)

    total_cards = conn.execute("SELECT COUNT(DISTINCT oracle_id) FROM card_mechanics").fetchone()[0]
    total_mechs = conn.execute("SELECT COUNT(*) FROM card_mechanics").fetchone()[0]
    total_legal = conn.execute(
        "SELECT COUNT(*) FROM cards WHERE legal_commander = 1 AND oracle_text IS NOT NULL AND oracle_text != ''"
    ).fetchone()[0]

    print(f"Cards with mechanics: {total_cards}/{total_legal} ({total_cards/max(total_legal,1)*100:.1f}%)")
    print(f"Total mechanics: {total_mechs} ({total_mechs/max(total_cards,1):.1f} per card)")

    print(f"\nBy type:")
    for row in conn.execute(
        "SELECT mechanic_type, COUNT(*) FROM card_mechanics GROUP BY mechanic_type ORDER BY COUNT(*) DESC"
    ):
        print(f"  {row[0]}: {row[1]}")

    print(f"\nTop actions:")
    for row in conn.execute(
        "SELECT effect_action, COUNT(*) FROM card_mechanics WHERE effect_action IS NOT NULL "
        "GROUP BY effect_action ORDER BY COUNT(*) DESC LIMIT 15"
    ):
        print(f"  {row[0]}: {row[1]}")

    print(f"\nTop trigger events:")
    for row in conn.execute(
        "SELECT trigger_event, COUNT(*) FROM card_mechanics WHERE trigger_event IS NOT NULL "
        "GROUP BY trigger_event ORDER BY COUNT(*) DESC LIMIT 15"
    ):
        print(f"  {row[0]}: {row[1]}")

    conn.close()


# ── CLI ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Extract structured game mechanics from MTG cards")
    parser.add_argument("--batch", type=int, default=0, help="Number of cards to extract")
    parser.add_argument("--card", help="Extract single card by name")
    parser.add_argument("--model", default="gpt-4.1-mini")
    parser.add_argument("--validate", action="store_true")
    parser.add_argument("--stats", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.stats:
        show_stats()
        return

    if args.validate:
        validate()
        return

    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key and not args.dry_run:
        print("ERROR: Set OPENAI_API_KEY")
        sys.exit(1)

    if args.card:
        conn = sqlite3.connect(DB_PATH)
        ensure_schema(conn)
        row = conn.execute(
            "SELECT oracle_id, name, oracle_text, type_line, keywords FROM cards WHERE name = ?",
            (args.card,)
        ).fetchone()
        if not row:
            print(f"Card not found: {args.card}")
            sys.exit(1)
        card = {"oracle_id": row[0], "name": row[1], "oracle_text": row[2],
                "type_line": row[3], "keywords": json.loads(row[4] or "[]")}
        result = extract_batch([card], api_key, args.model)
        for oid, mechs in result.items():
            store_mechanics(conn, oid, mechs)
            conn.commit()
            print(json.dumps(mechs, indent=2))
        conn.close()
    elif args.batch > 0:
        run_extraction(args.batch, api_key, args.model, args.dry_run)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
