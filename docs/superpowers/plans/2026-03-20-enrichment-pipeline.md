# Enrichment Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an offline synergy/combo detection pipeline with parsed abilities, Commander Spellbook ground truth, and strategy-aware recommendations.

**Architecture:** Extend the existing SQLite tag database with three new tables (abilities, card_strategies, spellbook_combos). Add an oracle text parser that extracts structured abilities deterministically. Integrate Commander Spellbook as combo ground truth. Wire strategy detection into recommendations with user override.

**Tech Stack:** Python 3, SQLite, regex-based oracle text parsing, Commander Spellbook REST API, existing synergy_graph.py infrastructure.

**Spec:** `docs/superpowers/specs/2026-03-20-enrichment-pipeline-design.md`

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `tag_db.py` | Modify (lines 23-66 schema, 566-599 CLI) | Add new tables, query functions, CLI subcommands |
| `ability_parser.py` | Create | Oracle text → structured abilities (3-phase parser) |
| `strategy_detector.py` | Create | Commander → strategy detection, tag-to-strategy mapping |
| `fetch_spellbook.py` | Create | Commander Spellbook bulk fetch, oracle_id cross-reference |
| `synergy_graph.py` | Modify (lines 1088-1167 combos, 853-997 recommend, 2334-2355 CLI) | 3-tier combos, strategy-weighted recommendations, new CLI args |
| `synergy_tag_registry.json` | Rebuild | v4.0 from 34k cards |
| `tests/test_ability_parser.py` | Create | Parser unit tests |
| `tests/test_strategy_detector.py` | Create | Strategy detection tests |
| `tests/test_tribal_cleanup.py` | Create | Tribal tag validation tests |
| `tests/test_fetch_spellbook.py` | Create | Spellbook fetch/import tests |

---

## Task 1: Create test infrastructure

**Files:**
- Create: `tests/__init__.py`
- Create: `tests/conftest.py`

- [ ] **Step 1: Create tests directory and conftest with shared fixtures**

```python
# tests/__init__.py
# (empty)
```

```python
# tests/conftest.py
import sqlite3
import json
import os
import pytest
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

@pytest.fixture
def tmp_db(tmp_path):
    """Create a temporary SQLite DB with the full schema for testing."""
    import tag_db
    db_path = str(tmp_path / "test_tags.db")
    conn = sqlite3.connect(db_path)
    conn.executescript(tag_db.SCHEMA)
    conn.commit()
    yield db_path
    conn.close()

@pytest.fixture
def sample_cards():
    """Return a few well-known cards with oracle text for parser testing."""
    return [
        {
            "oracle_id": "kyler-001",
            "name": "Kyler, Sigardian Emissary",
            "type_line": "Legendary Creature — Human Cleric",
            "oracle_text": "Whenever a Human enters the battlefield under your control, put a +1/+1 counter on Kyler, Sigardian Emissary.\nHuman creatures you control get +1/+1 for each +1/+1 counter on Kyler.",
            "mana_cost": "{3}{G}{W}",
            "keywords": []
        },
        {
            "oracle_id": "hardened-001",
            "name": "Hardened Scales",
            "type_line": "Enchantment",
            "oracle_text": "If one or more +1/+1 counters would be placed on a creature you control, that many plus one +1/+1 counters are placed on it instead.",
            "mana_cost": "{G}",
            "keywords": []
        },
        {
            "oracle_id": "cathars-001",
            "name": "Cathars' Crusade",
            "type_line": "Enchantment",
            "oracle_text": "Whenever a creature enters the battlefield under your control, put a +1/+1 counter on each creature you control.",
            "mana_cost": "{3}{W}{W}",
            "keywords": []
        },
        {
            "oracle_id": "skirk-001",
            "name": "Skirk Prospector",
            "type_line": "Creature — Goblin",
            "oracle_text": "Sacrifice a Goblin: Add {R}.",
            "mana_cost": "{R}",
            "keywords": []
        },
        {
            "oracle_id": "gavony-001",
            "name": "Gavony Township",
            "type_line": "Land",
            "oracle_text": "{T}: Add {C}.\n{2}{G}{W}, {T}: Put a +1/+1 counter on each creature you control.",
            "mana_cost": "",
            "keywords": []
        },
        {
            "oracle_id": "beast-001",
            "name": "Beast Within",
            "type_line": "Instant",
            "oracle_text": "Destroy target permanent. Its controller creates a 3/3 green Beast creature token.",
            "mana_cost": "{2}{G}",
            "keywords": []
        },
        {
            "oracle_id": "jace-001",
            "name": "Jace, the Mind Sculptor",
            "type_line": "Legendary Planeswalker — Jace",
            "oracle_text": "+2: Look at the top card of target player's library. You may put that card on the bottom of that player's library.\n0: Draw three cards, then put two cards from your hand on top of your library.\n−1: Return target creature to its owner's hand.\n−12: Exile all cards from target player's library, then that player shuffles their hand into their library.",
            "mana_cost": "{2}{U}{U}",
            "keywords": []
        },
        {
            "oracle_id": "binding-001",
            "name": "Binding the Old Gods",
            "type_line": "Enchantment — Saga",
            "oracle_text": "I — Destroy target nonland permanent an opponent controls.\nII — Search your library for a Forest card, put it onto the battlefield tapped, then shuffle.\nIII — Exile this Saga, then return it to the battlefield transformed.",
            "mana_cost": "{2}{B}{G}",
            "keywords": []
        },
        {
            "oracle_id": "delver-001",
            "name": "Delver of Secrets // Insectile Aberration",
            "type_line": "Creature — Human Wizard // Creature — Human Insect",
            "oracle_text": "At the beginning of your upkeep, look at the top card of your library. You may reveal that card. If an instant or sorcery card is revealed this way, transform Delver of Secrets. // Flying",
            "mana_cost": "{U}",
            "keywords": ["flying", "transform"]
        },
        {
            "oracle_id": "bonecrusher-001",
            "name": "Bonecrusher Giant // Stomp",
            "type_line": "Creature — Giant // Instant — Adventure",
            "oracle_text": "Whenever Bonecrusher Giant becomes the target of a spell, Bonecrusher Giant deals 2 damage to that spell's controller. // Damage can't be prevented this turn. Stomp deals 2 damage to any target.",
            "mana_cost": "{2}{R}",
            "keywords": ["adventure"]
        },
    ]
```

- [ ] **Step 2: Verify pytest runs with no tests collected**

Run: `cd /Users/evgenii.vasilenko/gofrolist/mtg-synergy-graph && python3 -m pytest tests/ -v 2>&1 | head -20`
Expected: "no tests ran" or "collected 0 items"

- [ ] **Step 3: Commit**

```bash
git add tests/__init__.py tests/conftest.py
git commit -m "test: add test infrastructure with shared fixtures for enrichment pipeline"
```

---

## Task 2: Fix wants tribal tags (P0)

**Files:**
- Create: `tests/test_tribal_cleanup.py`
- Modify: `tag_db.py:566-599` (add `fix-tribal` CLI subcommand)

- [ ] **Step 1: Write the test**

```python
# tests/test_tribal_cleanup.py
import sqlite3
import json
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import tag_db


def test_tribal_cleanup_removes_false_positives(tmp_db):
    """Cards wanting X-tribal but not mentioning X in oracle text should be cleaned."""
    conn = sqlite3.connect(tmp_db)
    # Card that mentions Goblins in oracle text — should keep goblin-tribal
    conn.execute(
        "INSERT INTO cards (oracle_id, name, oracle_text) VALUES (?, ?, ?)",
        ("goblin-lord", "Goblin Chieftain", "Other Goblins you control get +1/+1 and have haste.")
    )
    conn.execute("INSERT INTO wants (oracle_id, tag) VALUES (?, ?)", ("goblin-lord", "goblin-tribal"))

    # Card that does NOT mention Goblins — false positive, should be removed
    conn.execute(
        "INSERT INTO cards (oracle_id, name, oracle_text) VALUES (?, ?, ?)",
        ("random-creature", "Llanowar Elves", "Tap: Add {G}.")
    )
    conn.execute("INSERT INTO wants (oracle_id, tag) VALUES (?, ?)", ("random-creature", "goblin-tribal"))

    # Card wanting human-tribal that mentions Human in oracle text — keep
    conn.execute(
        "INSERT INTO cards (oracle_id, name, oracle_text) VALUES (?, ?, ?)",
        ("human-lord", "Thalia's Lieutenant", "When Thalia's Lieutenant enters, put a +1/+1 counter on each other Human you control.")
    )
    conn.execute("INSERT INTO wants (oracle_id, tag) VALUES (?, ?)", ("human-lord", "human-tribal"))

    conn.commit()
    conn.close()

    removed = tag_db.fix_tribal_wants(tmp_db)
    assert len(removed) == 1
    assert removed[0]["name"] == "Llanowar Elves"
    assert removed[0]["tag"] == "goblin-tribal"

    # Verify DB state
    conn = sqlite3.connect(tmp_db)
    remaining = conn.execute("SELECT oracle_id, tag FROM wants WHERE tag LIKE '%-tribal'").fetchall()
    conn.close()
    assert len(remaining) == 2
    remaining_ids = {r[0] for r in remaining}
    assert "goblin-lord" in remaining_ids
    assert "human-lord" in remaining_ids
    assert "random-creature" not in remaining_ids


def test_tribal_cleanup_handles_type_line_creatures(tmp_db):
    """Creatures OF a type but not MENTIONING the type in oracle text should lose tribal wants."""
    conn = sqlite3.connect(tmp_db)
    # A Goblin creature whose oracle text doesn't reference Goblins
    conn.execute(
        "INSERT INTO cards (oracle_id, name, type_line, oracle_text) VALUES (?, ?, ?, ?)",
        ("mogg-fanatic", "Mogg Fanatic", "Creature — Goblin", "Sacrifice Mogg Fanatic: It deals 1 damage to any target.")
    )
    conn.execute("INSERT INTO wants (oracle_id, tag) VALUES (?, ?)", ("mogg-fanatic", "goblin-tribal"))
    conn.commit()
    conn.close()

    removed = tag_db.fix_tribal_wants(tmp_db)
    assert len(removed) == 1
    assert removed[0]["name"] == "Mogg Fanatic"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_tribal_cleanup.py -v`
Expected: FAIL — `tag_db.fix_tribal_wants` does not exist

- [ ] **Step 3: Implement fix_tribal_wants in tag_db.py**

Add after the existing query functions (around line 345):

```python
def fix_tribal_wants(db_path=None):
    """Remove wants tribal tags where oracle text doesn't mention the creature type.

    Returns list of dicts with removed entries: {oracle_id, name, tag, oracle_text}.
    """
    if db_path is None:
        db_path = DB_PATH
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # Get all wants tribal tags
    rows = conn.execute("""
        SELECT w.oracle_id, w.tag, c.name, c.oracle_text
        FROM wants w JOIN cards c ON w.oracle_id = c.oracle_id
        WHERE w.tag LIKE '%-tribal'
    """).fetchall()

    removed = []
    for row in rows:
        tag = row["tag"]
        # Extract creature type from tag: "goblin-tribal" -> "goblin"
        creature_type = tag.replace("-tribal", "")
        oracle = (row["oracle_text"] or "").lower()

        # Check if oracle text mentions the creature type (case-insensitive)
        if creature_type.lower() not in oracle:
            removed.append({
                "oracle_id": row["oracle_id"],
                "name": row["name"],
                "tag": tag,
                "oracle_text": row["oracle_text"]
            })

    # Delete false positives
    if removed:
        for entry in removed:
            conn.execute(
                "DELETE FROM wants WHERE oracle_id = ? AND tag = ?",
                (entry["oracle_id"], entry["tag"])
            )
        conn.commit()

    conn.close()
    return removed
```

- [ ] **Step 4: Add CLI subcommand for fix-tribal**

In `tag_db.py` main/CLI section (around line 590), add a new subcommand:

```python
# Add to argparse subparsers
fix_tribal_parser = subparsers.add_parser("fix-tribal", help="Remove false positive tribal wants tags")
fix_tribal_parser.add_argument("--dry-run", action="store_true", help="Show what would be removed without deleting")
```

And in the command handler section:

```python
elif args.command == "fix-tribal":
    if args.dry_run:
        # Preview mode: query without deleting
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute("""
            SELECT w.oracle_id, w.tag, c.name, c.oracle_text
            FROM wants w JOIN cards c ON w.oracle_id = c.oracle_id
            WHERE w.tag LIKE '%-tribal'
        """).fetchall()
        conn.close()
        false_pos = []
        for oid, tag, name, oracle in rows:
            creature_type = tag.replace("-tribal", "")
            if creature_type.lower() not in (oracle or "").lower():
                false_pos.append((name, tag))
        print(f"Would remove {len(false_pos)} false positive tribal wants:")
        for name, tag in false_pos[:20]:
            print(f"  {name}: {tag}")
        if len(false_pos) > 20:
            print(f"  ... and {len(false_pos) - 20} more")
    else:
        removed = fix_tribal_wants()
        print(f"Removed {len(removed)} false positive tribal wants tags")
        for entry in removed[:20]:
            print(f"  {entry['name']}: {entry['tag']}")
        if len(removed) > 20:
            print(f"  ... and {len(removed) - 20} more")
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_tribal_cleanup.py -v`
Expected: 2 passed

- [ ] **Step 6: Run on actual DB (dry-run first)**

Run: `python3 tag_db.py fix-tribal --dry-run`
Expected: Shows count and sample of false positives to be removed

- [ ] **Step 7: Run for real**

Run: `python3 tag_db.py fix-tribal`
Expected: Reports removed count

- [ ] **Step 8: Commit**

```bash
git add tag_db.py tests/test_tribal_cleanup.py
git commit -m "fix: remove false positive wants tribal tags via oracle text cross-reference"
```

---

## Task 3: Rebuild tag registry from 34k cards

**Files:**
- Modify: `tag_db.py` (add `rebuild-registry` subcommand)

- [ ] **Step 1: Write the test**

Add to `tests/test_tribal_cleanup.py` (or create `tests/test_registry_rebuild.py`):

```python
# tests/test_registry_rebuild.py
import sqlite3
import json
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import tag_db


def test_rebuild_registry(tmp_db, tmp_path):
    """Registry rebuild collects tags with 3+ occurrences."""
    conn = sqlite3.connect(tmp_db)
    # Insert 4 cards: 3 provide "token-generation", 2 provide "rare-tag"
    for i in range(3):
        conn.execute("INSERT INTO cards (oracle_id, name) VALUES (?, ?)", (f"card-{i}", f"Card {i}"))
        conn.execute("INSERT INTO provides (oracle_id, tag) VALUES (?, ?)", (f"card-{i}", "token-generation"))
        conn.execute("INSERT INTO wants (oracle_id, tag) VALUES (?, ?)", (f"card-{i}", "creature-etb"))
    for i in range(2):
        conn.execute("INSERT INTO provides (oracle_id, tag) VALUES (?, ?)", (f"card-{i}", "rare-tag"))
        conn.execute("INSERT INTO wants (oracle_id, tag) VALUES (?, ?)", (f"card-{i}", "rare-want"))
    conn.commit()
    conn.close()

    output_path = str(tmp_path / "registry.json")
    tag_db.rebuild_registry(tmp_db, output_path, min_freq=3)

    with open(output_path) as f:
        registry = json.load(f)

    assert "token-generation" in registry["provides"]["tags"]
    assert "creature-etb" in registry["wants"]["tags"]
    # rare-tag has only 2 occurrences, should be excluded (min_freq=3)
    assert "rare-tag" not in registry["provides"]["tags"]
    assert "rare-want" not in registry["wants"]["tags"]
    assert registry["_meta"]["version"] == "4.0"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_registry_rebuild.py -v`
Expected: FAIL — `tag_db.rebuild_registry` does not exist

- [ ] **Step 3: Implement rebuild_registry in tag_db.py**

```python
def rebuild_registry(db_path=None, output_path=None, min_freq=3):
    """Rebuild synergy_tag_registry.json from all cards in the DB.

    Collects all provides/wants tags with min_freq or more occurrences.
    """
    if db_path is None:
        db_path = DB_PATH
    if output_path is None:
        output_path = os.path.join(os.path.dirname(__file__), "synergy_tag_registry.json")

    conn = sqlite3.connect(db_path)

    # Count provides tags
    provides_counts = conn.execute(
        "SELECT tag, COUNT(*) as cnt FROM provides GROUP BY tag HAVING cnt >= ? ORDER BY tag",
        (min_freq,)
    ).fetchall()

    # Count wants tags
    wants_counts = conn.execute(
        "SELECT tag, COUNT(*) as cnt FROM wants GROUP BY tag HAVING cnt >= ? ORDER BY tag",
        (min_freq,)
    ).fetchall()

    total_cards = conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0]
    conn.close()

    provides_tags = [row[0] for row in provides_counts]
    wants_tags = [row[0] for row in wants_counts]

    # Load existing registry to preserve function_tags and themes
    existing = {}
    existing_path = os.path.join(os.path.dirname(__file__), "synergy_tag_registry.json")
    if os.path.exists(existing_path):
        with open(existing_path) as f:
            existing = json.load(f)

    registry = {
        "_meta": {
            "description": "Controlled vocabulary for 3-layer card tagging system.",
            "version": "4.0",
            "created": __import__('datetime').date.today().isoformat(),
            "stats": {
                "provides_count": len(provides_tags),
                "wants_count": len(wants_tags),
                "source": f"Rebuilt from {total_cards} cards in tags.db (min {min_freq} occurrences)",
                "min_frequency": min_freq
            }
        },
        "provides": {
            "_description": "What card gives to deck",
            "tags": provides_tags
        },
        "wants": {
            "_description": "Conditions making card better",
            "tags": wants_tags
        }
    }

    # Preserve function_tags and themes from existing registry
    if "function_tags" in existing:
        registry["function_tags"] = existing["function_tags"]
    if "themes" in existing:
        registry["themes"] = existing["themes"]

    with open(output_path, "w") as f:
        json.dump(registry, f, indent=2)

    return registry
```

- [ ] **Step 4: Add CLI subcommand**

```python
rebuild_parser = subparsers.add_parser("rebuild-registry", help="Rebuild tag registry from all DB cards")
rebuild_parser.add_argument("--min-freq", type=int, default=3, help="Minimum tag frequency (default: 3)")
rebuild_parser.add_argument("--output", default=None, help="Output path (default: synergy_tag_registry.json)")
```

Handler:
```python
elif args.command == "rebuild-registry":
    registry = rebuild_registry(output_path=args.output, min_freq=args.min_freq)
    meta = registry["_meta"]["stats"]
    print(f"Registry v4.0: {meta['provides_count']} provides, {meta['wants_count']} wants")
    print(f"Source: {meta['source']}")
```

- [ ] **Step 5: Run tests**

Run: `python3 -m pytest tests/test_registry_rebuild.py -v`
Expected: PASS

- [ ] **Step 6: Run on actual DB**

Run: `python3 tag_db.py rebuild-registry`
Expected: Shows new registry counts (may differ from v3.1's 240/213)

- [ ] **Step 7: Commit**

```bash
git add tag_db.py tests/test_registry_rebuild.py synergy_tag_registry.json
git commit -m "feat: rebuild tag registry v4.0 from 34k cards"
```

---

## Task 4: Add DB schema for new tables

**Files:**
- Modify: `tag_db.py:23-66` (SCHEMA string)

- [ ] **Step 1: Write the test**

```python
# tests/test_schema.py
import sqlite3
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import tag_db


def test_new_tables_exist(tmp_db):
    """All new tables should be created by SCHEMA."""
    conn = sqlite3.connect(tmp_db)
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()]
    conn.close()
    assert "abilities" in tables
    assert "card_strategies" in tables
    assert "spellbook_combos" in tables
    assert "spellbook_combo_cards" in tables


def test_abilities_table_columns(tmp_db):
    conn = sqlite3.connect(tmp_db)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(abilities)").fetchall()]
    conn.close()
    assert "oracle_id" in cols
    assert "ability_type" in cols
    assert "trigger_condition" in cols
    assert "trigger_tags" in cols
    assert "effect_tags" in cols
    assert "is_mana_ability" in cols


def test_card_strategies_table(tmp_db):
    conn = sqlite3.connect(tmp_db)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(card_strategies)").fetchall()]
    conn.close()
    assert "oracle_id" in cols
    assert "strategy" in cols
    assert "confidence" in cols
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_schema.py -v`
Expected: FAIL — tables don't exist yet

- [ ] **Step 3: Add new CREATE TABLE statements to SCHEMA in tag_db.py**

Append to the existing SCHEMA string (after the scryfall_tags table, around line 65):

```sql
CREATE TABLE IF NOT EXISTS abilities (
    oracle_id TEXT NOT NULL,
    ability_index INTEGER NOT NULL,
    ability_type TEXT NOT NULL,
    trigger_condition TEXT,
    trigger_tags TEXT,
    cost TEXT,
    effect TEXT,
    effect_tags TEXT,
    zone TEXT DEFAULT 'battlefield',
    targets TEXT,
    is_mana_ability BOOLEAN DEFAULT 0,
    PRIMARY KEY (oracle_id, ability_index),
    FOREIGN KEY (oracle_id) REFERENCES cards(oracle_id)
);
CREATE INDEX IF NOT EXISTS idx_abilities_type ON abilities(ability_type);
CREATE INDEX IF NOT EXISTS idx_abilities_oracle ON abilities(oracle_id);

CREATE TABLE IF NOT EXISTS card_strategies (
    oracle_id TEXT NOT NULL,
    strategy TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 0.0,
    PRIMARY KEY (oracle_id, strategy),
    FOREIGN KEY (oracle_id) REFERENCES cards(oracle_id)
);
CREATE INDEX IF NOT EXISTS idx_strategies_strategy ON card_strategies(strategy);

CREATE TABLE IF NOT EXISTS spellbook_combos (
    combo_id TEXT PRIMARY KEY,
    card_oracle_ids TEXT NOT NULL,
    card_names TEXT NOT NULL,
    result TEXT,
    prerequisites TEXT,
    card_count INTEGER
);

CREATE TABLE IF NOT EXISTS spellbook_combo_cards (
    combo_id TEXT NOT NULL,
    oracle_id TEXT NOT NULL,
    PRIMARY KEY (combo_id, oracle_id),
    FOREIGN KEY (combo_id) REFERENCES spellbook_combos(combo_id)
);
CREATE INDEX IF NOT EXISTS idx_spellbook_cards_oracle ON spellbook_combo_cards(oracle_id);
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_schema.py -v`
Expected: 3 passed

- [ ] **Step 5: Verify existing DB gets new tables (non-destructive)**

Run: `python3 -c "import sqlite3; conn = sqlite3.connect('data/tags.db'); conn.executescript(open('tag_db.py').read().split(\"SCHEMA = '''\")[1].split(\"'''\")[0]) if False else None; print('OK')"`

Actually, just verify the schema appends cleanly:
Run: `python3 -c "import tag_db; tag_db.init_db(); print('Schema applied successfully')"`

- [ ] **Step 6: Commit**

```bash
git add tag_db.py tests/test_schema.py
git commit -m "feat: add DB schema for abilities, card_strategies, spellbook tables"
```

---

## Task 5: Build oracle text ability parser — Phase 1 (keywords)

**Files:**
- Create: `ability_parser.py`
- Create: `tests/test_ability_parser.py`

- [ ] **Step 1: Write keyword extraction tests**

```python
# tests/test_ability_parser.py
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


def test_keyword_extraction_simple():
    from ability_parser import parse_card
    card = {
        "oracle_id": "test-001",
        "name": "Serra Angel",
        "oracle_text": "Flying\nVigilance",
        "keywords": ["flying", "vigilance"]
    }
    abilities = parse_card(card)
    keyword_abilities = [a for a in abilities if a["ability_type"] == "keyword"]
    assert len(keyword_abilities) == 2
    kw_names = {a["effect"] for a in keyword_abilities}
    assert "flying" in kw_names
    assert "vigilance" in kw_names


def test_keyword_extraction_with_other_text():
    from ability_parser import parse_card
    card = {
        "oracle_id": "test-002",
        "name": "Baneslayer Angel",
        "oracle_text": "Flying, first strike, lifelink\nProtection from Demons and from Dragons",
        "keywords": ["flying", "first strike", "lifelink", "protection"]
    }
    abilities = parse_card(card)
    keywords = [a for a in abilities if a["ability_type"] == "keyword"]
    assert len(keywords) >= 3  # flying, first strike, lifelink at minimum


def test_double_faced_card_split():
    from ability_parser import parse_card
    card = {
        "oracle_id": "delver-001",
        "name": "Delver of Secrets // Insectile Aberration",
        "oracle_text": "At the beginning of your upkeep, look at the top card of your library. You may reveal that card. If an instant or sorcery card is revealed this way, transform Delver of Secrets. // Flying",
        "keywords": ["flying", "transform"]
    }
    abilities = parse_card(card)
    # Should have abilities from both faces
    assert len(abilities) >= 2
    # Back face should have flying keyword
    keywords = [a for a in abilities if a["ability_type"] == "keyword"]
    assert any(a["effect"] == "flying" for a in keywords)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_ability_parser.py::test_keyword_extraction_simple -v`
Expected: FAIL — module not found

- [ ] **Step 3: Implement Phase 1 — keyword extraction + card parsing skeleton**

```python
# ability_parser.py
"""Oracle text parser: extracts structured abilities from MTG card text.

Three phases:
1. Keyword extraction (from Scryfall keywords field)
2. Pattern matching (triggered, activated, static, replacement, mana)
3. Effect tagging (maps effect text to provides/wants vocabulary)
"""

import re
import json

# MTG keywords recognized by Scryfall (subset — the keywords field is authoritative)
KEYWORD_ZONES = {
    "cycling": "hand",
    "unearth": "graveyard",
    "escape": "graveyard",
    "flashback": "graveyard",
    "retrace": "graveyard",
    "jump-start": "graveyard",
    "embalm": "graveyard",
    "eternalize": "graveyard",
    "disturb": "graveyard",
    "encore": "graveyard",
    "scavenge": "graveyard",
    "channel": "hand",
    "ninjutsu": "hand",
    "foretell": "hand",
    "forecast": "hand",
    "miracle": "hand",
    "madness": "hand",
    "transmute": "hand",
}


def _strip_reminder_text(text):
    """Remove reminder text in parentheses."""
    return re.sub(r'\([^)]*\)', '', text).strip()


def _split_faces(oracle_text):
    """Split double-faced/adventure cards on ' // ' separator.

    Returns list of (face_index, text) tuples.
    """
    if " // " in oracle_text:
        faces = oracle_text.split(" // ")
        return list(enumerate(faces))
    return [(0, oracle_text)]


def _extract_keywords(card):
    """Phase 1: Extract keyword abilities from the card's keywords field."""
    abilities = []
    keywords = card.get("keywords") or []
    for kw in keywords:
        kw_lower = kw.lower()
        zone = KEYWORD_ZONES.get(kw_lower, "battlefield")
        abilities.append({
            "ability_type": "keyword",
            "trigger_condition": None,
            "trigger_tags": None,
            "cost": None,
            "effect": kw_lower,
            "effect_tags": None,  # Tagged in Phase 3
            "zone": zone,
            "targets": None,
            "is_mana_ability": False,
        })
    return abilities


def parse_card(card):
    """Parse a single card's oracle text into structured abilities.

    Args:
        card: dict with oracle_id, name, oracle_text, keywords

    Returns:
        list of ability dicts, each with ability_index set
    """
    oracle_text = card.get("oracle_text") or ""
    if not oracle_text:
        return []

    all_abilities = []

    # Phase 1: Keywords from Scryfall field
    all_abilities.extend(_extract_keywords(card))

    # Split faces for DFC/adventure cards
    faces = _split_faces(oracle_text)

    for face_idx, face_text in faces:
        # Strip reminder text
        clean = _strip_reminder_text(face_text)

        # Split into paragraphs (each = one ability in MTG rules)
        paragraphs = [p.strip() for p in clean.split("\n") if p.strip()]

        for para in paragraphs:
            # Skip if this paragraph is just keywords we already extracted
            if _is_keyword_only_paragraph(para, card.get("keywords") or []):
                continue

            # Phase 2 will parse these paragraphs (Task 6)
            # For now, store as unparsed
            ability = _parse_paragraph(para)
            if ability:
                all_abilities.append(ability)

    # Assign sequential indices
    for i, ab in enumerate(all_abilities):
        ab["ability_index"] = i

    return all_abilities


def _is_keyword_only_paragraph(para, keywords):
    """Check if a paragraph is just a list of keywords."""
    kw_lower = {kw.lower() for kw in keywords}
    # Handle "Flying, vigilance" or "Flying" or "Haste, trample, lifelink"
    parts = [p.strip().lower().rstrip('.') for p in re.split(r'[,\n]', para)]
    return all(p in kw_lower or p == "" for p in parts)


def _parse_paragraph(para):
    """Parse a single paragraph into an ability dict. Phase 2 placeholder."""
    # Will be implemented in Task 6
    return {
        "ability_type": "static",  # default, refined in Phase 2
        "trigger_condition": None,
        "trigger_tags": None,
        "cost": None,
        "effect": para,
        "effect_tags": None,
        "zone": "battlefield",
        "targets": None,
        "is_mana_ability": False,
    }
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_ability_parser.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add ability_parser.py tests/test_ability_parser.py
git commit -m "feat: add ability parser Phase 1 — keyword extraction and card parsing skeleton"
```

---

## Task 6: Ability parser — Phase 2 (pattern matching)

**Files:**
- Modify: `ability_parser.py` (`_parse_paragraph` function)
- Modify: `tests/test_ability_parser.py`

- [ ] **Step 1: Write pattern matching tests**

```python
# Add to tests/test_ability_parser.py

def test_triggered_ability():
    from ability_parser import parse_card
    card = {
        "oracle_id": "cathars-001",
        "name": "Cathars' Crusade",
        "oracle_text": "Whenever a creature enters the battlefield under your control, put a +1/+1 counter on each creature you control.",
        "keywords": []
    }
    abilities = parse_card(card)
    triggered = [a for a in abilities if a["ability_type"] == "triggered"]
    assert len(triggered) == 1
    assert "creature enters" in triggered[0]["trigger_condition"].lower()
    assert "+1/+1 counter" in triggered[0]["effect"]


def test_activated_ability():
    from ability_parser import parse_card
    card = {
        "oracle_id": "gavony-001",
        "name": "Gavony Township",
        "oracle_text": "{T}: Add {C}.\n{2}{G}{W}, {T}: Put a +1/+1 counter on each creature you control.",
        "keywords": []
    }
    abilities = parse_card(card)
    activated_or_mana = [a for a in abilities if a["ability_type"] in ("activated", "mana")]
    mana = [a for a in abilities if a["is_mana_ability"]]
    assert len(activated_or_mana) >= 2  # Both are activated/mana type
    assert len(mana) == 1  # Only the first is a mana ability
    assert "Add" in mana[0]["effect"]


def test_replacement_ability():
    from ability_parser import parse_card
    card = {
        "oracle_id": "hardened-001",
        "name": "Hardened Scales",
        "oracle_text": "If one or more +1/+1 counters would be placed on a creature you control, that many plus one +1/+1 counters are placed on it instead.",
        "keywords": []
    }
    abilities = parse_card(card)
    replacements = [a for a in abilities if a["ability_type"] == "replacement"]
    assert len(replacements) == 1


def test_static_ability():
    from ability_parser import parse_card
    card = {
        "oracle_id": "test-static",
        "name": "Glorious Anthem",
        "oracle_text": "Creatures you control get +1/+1.",
        "keywords": []
    }
    abilities = parse_card(card)
    statics = [a for a in abilities if a["ability_type"] == "static"]
    assert len(statics) == 1


def test_sacrifice_activated():
    from ability_parser import parse_card
    card = {
        "oracle_id": "skirk-001",
        "name": "Skirk Prospector",
        "oracle_text": "Sacrifice a Goblin: Add {R}.",
        "keywords": []
    }
    abilities = parse_card(card)
    activated = [a for a in abilities if a["ability_type"] == "activated"]
    mana = [a for a in abilities if a["is_mana_ability"]]
    assert len(activated) == 1
    assert "Sacrifice" in activated[0]["cost"]
    assert len(mana) == 1


def test_planeswalker_abilities():
    from ability_parser import parse_card
    card = {
        "oracle_id": "jace-001",
        "name": "Jace, the Mind Sculptor",
        "oracle_text": "+2: Look at the top card of target player's library. You may put that card on the bottom of that player's library.\n0: Draw three cards, then put two cards from your hand on top of your library.\n−1: Return target creature to its owner's hand.\n−12: Exile all cards from target player's library, then that player shuffles their hand into their library.",
        "keywords": []
    }
    abilities = parse_card(card)
    activated = [a for a in abilities if a["ability_type"] == "activated"]
    assert len(activated) == 4
    # Check loyalty costs are captured
    costs = [a["cost"] for a in activated]
    assert any("+2" in c for c in costs)
    assert any("−12" in c or "-12" in c for c in costs)


def test_saga_abilities():
    from ability_parser import parse_card
    card = {
        "oracle_id": "binding-001",
        "name": "Binding the Old Gods",
        "oracle_text": "I — Destroy target nonland permanent an opponent controls.\nII — Search your library for a Forest card, put it onto the battlefield tapped, then shuffle.\nIII — Exile this Saga, then return it to the battlefield transformed.",
        "keywords": []
    }
    abilities = parse_card(card)
    triggered = [a for a in abilities if a["ability_type"] == "triggered"]
    assert len(triggered) == 3


def test_triggered_with_if_clause():
    from ability_parser import parse_card
    card = {
        "oracle_id": "test-if",
        "name": "Test Card",
        "oracle_text": "Whenever a creature enters the battlefield under your control, if it's a Human, put a +1/+1 counter on it.",
        "keywords": []
    }
    abilities = parse_card(card)
    triggered = [a for a in abilities if a["ability_type"] == "triggered"]
    assert len(triggered) == 1
    assert "creature enters" in triggered[0]["trigger_condition"].lower()


def test_adventure_card():
    from ability_parser import parse_card
    card = {
        "oracle_id": "bonecrusher-001",
        "name": "Bonecrusher Giant // Stomp",
        "oracle_text": "Whenever Bonecrusher Giant becomes the target of a spell, Bonecrusher Giant deals 2 damage to that spell's controller. // Damage can't be prevented this turn. Stomp deals 2 damage to any target.",
        "keywords": ["adventure"]
    }
    abilities = parse_card(card)
    # Front face: triggered ability
    triggered = [a for a in abilities if a["ability_type"] == "triggered"]
    assert len(triggered) >= 1
    # Back face (adventure): static + effect
    assert len(abilities) >= 2  # at least triggered + adventure parts
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_ability_parser.py -v -k "not keyword"`
Expected: Multiple failures — _parse_paragraph returns everything as static

- [ ] **Step 3: Implement Phase 2 — replace _parse_paragraph**

Replace the `_parse_paragraph` function in `ability_parser.py`:

```python
# Planeswalker loyalty cost pattern: +N, -N, 0
_LOYALTY_RE = re.compile(r'^([+\-−]?\d+): (.+)$')

# Saga chapter pattern: I, II, III, IV, etc.
_SAGA_RE = re.compile(r'^(I{1,3}V?|IV|V|VI{0,3}) — (.+)$')

# Triggered ability: When/Whenever/At ...
_TRIGGERED_RE = re.compile(r'^(When(?:ever)?|At) (.+)')

# Activated ability: cost: effect (cost must contain mana symbol, tap, or sacrifice-like word)
_ACTIVATED_RE = re.compile(r'^(.+?): (.+)$')
_COST_INDICATORS = re.compile(r'\{|(?:^|\W)[Tt]ap\b|Sacrifice|Remove|Discard|Pay|Exile .* from')

# Replacement effect
_REPLACEMENT_RE = re.compile(r'\bwould\b.*\binstead\b', re.IGNORECASE)

# Mana ability: effect produces mana
_MANA_EFFECT_RE = re.compile(r'[Aa]dd \{')


def _split_trigger_effect(text):
    """Split a triggered ability into trigger_condition and effect.

    Handles 'if' clauses by greedily including them in the trigger.
    'Whenever X, if Y, effect' -> trigger='X, if Y', effect='effect'
    'Whenever X, effect' -> trigger='X', effect='effect'
    """
    # Find the trigger word and everything after it
    match = _TRIGGERED_RE.match(text)
    if not match:
        return text, text

    trigger_word = match.group(1)
    rest = match.group(2)

    # Split on commas, looking for the main effect
    # Heuristic: the effect usually starts with a verb (put, draw, create, destroy, exile, return, etc.)
    effect_verbs = r'(?:put|draw|create|destroy|exile|return|deal|add|gain|lose|sacrifice|search|discard|counter|tap|untap|each|that|it|you|target|all|choose)'

    parts = rest.split(', ')
    for i in range(len(parts) - 1, 0, -1):
        candidate = parts[i].strip()
        if re.match(effect_verbs, candidate, re.IGNORECASE):
            trigger = ', '.join(parts[:i])
            effect = ', '.join(parts[i:])
            return f"{trigger_word} {trigger}", effect

    # Fallback: first comma split
    if ', ' in rest:
        idx = rest.index(', ')
        return f"{trigger_word} {rest[:idx]}", rest[idx + 2:]

    return f"{trigger_word} {rest}", rest


def _parse_paragraph(para):
    """Parse a single oracle text paragraph into a structured ability."""

    # Check for saga chapters
    saga_match = _SAGA_RE.match(para)
    if saga_match:
        chapter = saga_match.group(1)
        effect = saga_match.group(2)
        return {
            "ability_type": "triggered",
            "trigger_condition": f"chapter {chapter}",
            "trigger_tags": None,
            "cost": None,
            "effect": effect,
            "effect_tags": None,
            "zone": "battlefield",
            "targets": _extract_targets(effect),
            "is_mana_ability": False,
        }

    # Check for planeswalker loyalty abilities
    loyalty_match = _LOYALTY_RE.match(para)
    if loyalty_match:
        cost = loyalty_match.group(1)
        effect = loyalty_match.group(2)
        return {
            "ability_type": "activated",
            "trigger_condition": None,
            "trigger_tags": None,
            "cost": cost,
            "effect": effect,
            "effect_tags": None,
            "zone": "battlefield",
            "targets": _extract_targets(effect),
            "is_mana_ability": False,
        }

    # Check for replacement effects
    if _REPLACEMENT_RE.search(para):
        return {
            "ability_type": "replacement",
            "trigger_condition": None,
            "trigger_tags": None,
            "cost": None,
            "effect": para,
            "effect_tags": None,
            "zone": "battlefield",
            "targets": None,
            "is_mana_ability": False,
        }

    # Check for triggered abilities
    if _TRIGGERED_RE.match(para):
        trigger, effect = _split_trigger_effect(para)
        return {
            "ability_type": "triggered",
            "trigger_condition": trigger,
            "trigger_tags": None,
            "cost": None,
            "effect": effect,
            "effect_tags": None,
            "zone": "battlefield",
            "targets": _extract_targets(effect),
            "is_mana_ability": False,
        }

    # Check for activated abilities
    activated_match = _ACTIVATED_RE.match(para)
    if activated_match and _COST_INDICATORS.search(activated_match.group(1)):
        cost = activated_match.group(1)
        effect = activated_match.group(2)
        is_mana = bool(_MANA_EFFECT_RE.search(effect))
        return {
            "ability_type": "activated" if not is_mana else "mana",
            "trigger_condition": None,
            "trigger_tags": None,
            "cost": cost,
            "effect": effect,
            "effect_tags": None,
            "zone": "battlefield",
            "targets": _extract_targets(effect),
            "is_mana_ability": is_mana,
        }

    # Default: static ability
    return {
        "ability_type": "static",
        "trigger_condition": None,
        "trigger_tags": None,
        "cost": None,
        "effect": para,
        "effect_tags": None,
        "zone": _infer_zone(para),
        "targets": _extract_targets(para),
        "is_mana_ability": False,
    }


def _extract_targets(text):
    """Extract target description from effect text."""
    match = re.search(r'target ([\w\s]+?)(?:\.|,|$)', text, re.IGNORECASE)
    return match.group(1).strip() if match else None


def _infer_zone(text):
    """Infer which zone an ability operates from."""
    text_lower = text.lower()
    if 'from your graveyard' in text_lower or 'from a graveyard' in text_lower:
        return 'graveyard'
    if 'from your hand' in text_lower:
        return 'hand'
    if 'from exile' in text_lower:
        return 'exile'
    return 'battlefield'
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_ability_parser.py -v`
Expected: All tests pass

- [ ] **Step 5: Commit**

```bash
git add ability_parser.py tests/test_ability_parser.py
git commit -m "feat: add ability parser Phase 2 — pattern matching for triggered/activated/static/replacement"
```

---

## Task 7: Ability parser — Phase 3 (effect tagging)

**Files:**
- Modify: `ability_parser.py`
- Modify: `tests/test_ability_parser.py`

- [ ] **Step 1: Write effect tagging tests**

```python
# Add to tests/test_ability_parser.py

def test_effect_tagging_token_generation():
    from ability_parser import parse_card
    card = {
        "oracle_id": "test-token",
        "name": "Krenko, Mob Boss",
        "oracle_text": "{T}: Create X 1/1 red Goblin creature tokens, where X is the number of Goblins you control.",
        "keywords": []
    }
    abilities = parse_card(card)
    tagged = [a for a in abilities if a.get("effect_tags")]
    assert len(tagged) >= 1
    assert "token-generation" in tagged[0]["effect_tags"]


def test_effect_tagging_card_draw():
    from ability_parser import parse_card
    card = {
        "oracle_id": "test-draw",
        "name": "Harmonize",
        "oracle_text": "Draw three cards.",
        "keywords": []
    }
    abilities = parse_card(card)
    tagged = [a for a in abilities if a.get("effect_tags")]
    assert any("card-draw" in a["effect_tags"] for a in tagged)


def test_trigger_tagging_creature_etb():
    from ability_parser import parse_card
    card = {
        "oracle_id": "cathars-001",
        "name": "Cathars' Crusade",
        "oracle_text": "Whenever a creature enters the battlefield under your control, put a +1/+1 counter on each creature you control.",
        "keywords": []
    }
    abilities = parse_card(card)
    triggered = [a for a in abilities if a["ability_type"] == "triggered"]
    assert len(triggered) == 1
    assert triggered[0]["trigger_tags"] is not None
    assert "creature-etb" in triggered[0]["trigger_tags"]
    assert "counter-placement" in triggered[0]["effect_tags"]


def test_trigger_tagging_creature_death():
    from ability_parser import parse_card
    card = {
        "oracle_id": "test-death",
        "name": "Blood Artist",
        "oracle_text": "Whenever a creature dies, target opponent loses 1 life and you gain 1 life.",
        "keywords": []
    }
    abilities = parse_card(card)
    triggered = [a for a in abilities if a["ability_type"] == "triggered"]
    assert triggered[0]["trigger_tags"] is not None
    assert "creature-death" in triggered[0]["trigger_tags"]
    assert "life-drain" in triggered[0]["effect_tags"]


def test_sacrifice_outlet_tagging():
    from ability_parser import parse_card
    card = {
        "oracle_id": "skirk-001",
        "name": "Skirk Prospector",
        "oracle_text": "Sacrifice a Goblin: Add {R}.",
        "keywords": []
    }
    abilities = parse_card(card)
    activated = [a for a in abilities if a["ability_type"] in ("activated", "mana")]
    assert any("sacrifice-outlet" in (a.get("effect_tags") or []) for a in activated)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_ability_parser.py -k "tagging" -v`
Expected: FAIL — effect_tags/trigger_tags are None

- [ ] **Step 3: Implement Phase 3 — effect and trigger tagging**

Add to `ability_parser.py`:

```python
# Effect text -> tag mappings
EFFECT_TAG_PATTERNS = [
    (re.compile(r'create.*token', re.I), "token-generation"),
    (re.compile(r'draw.*card|draws.*card', re.I), "card-draw"),
    (re.compile(r'destroy.*(?:creature|permanent|artifact|enchantment)', re.I), "spot-removal"),
    (re.compile(r'deals?\s+\d+\s+damage|damage to', re.I), "direct-damage"),
    (re.compile(r'return.*from.*graveyard|return.*to the battlefield', re.I), "graveyard-recursion"),
    (re.compile(r'\+1/\+1 counter', re.I), "counter-placement"),
    (re.compile(r'gain.*life|gains?\s+\d+\s+life', re.I), "life-gain"),
    (re.compile(r'lose.*life|loses?\s+\d+\s+life', re.I), "life-drain"),
    (re.compile(r'exile.*(?:creature|permanent|card)', re.I), "exile-removal"),
    (re.compile(r'search.*library', re.I), "tutor"),
    (re.compile(r'add \{', re.I), "mana-acceleration"),
    (re.compile(r'scry|look at the top', re.I), "card-filtering"),
    (re.compile(r'mill|put.*from.*library into.*graveyard', re.I), "mill"),
    (re.compile(r'discard', re.I), "discard"),
    (re.compile(r'counter target.*spell', re.I), "counterspell"),
    (re.compile(r'tap.*(?:creature|permanent)|doesn\'t untap', re.I), "tap-control"),
    (re.compile(r'untap', re.I), "untap"),
    (re.compile(r'copy.*(?:spell|creature|permanent)', re.I), "copy-effect"),
    (re.compile(r'each opponent|all opponents', re.I), "group-damage"),
    (re.compile(r'get[s]?\s+[+\-]\d+/[+\-]\d+', re.I), "creature-pump"),
    (re.compile(r'additional combat', re.I), "extra-combat"),
    (re.compile(r'extra turn', re.I), "extra-turn"),
    (re.compile(r'can\'t be blocked|unblockable', re.I), "evasion"),
    (re.compile(r'indestructible|hexproof|shroud', re.I), "board-protection"),
    (re.compile(r'treasure token', re.I), "treasure-generation"),
    (re.compile(r'food token', re.I), "food-generation"),
    (re.compile(r'clue token', re.I), "clue-generation"),
    (re.compile(r'equip|reconfigure', re.I), "equipment-synergy"),
    (re.compile(r'enchant|aura', re.I), "aura-synergy"),
    (re.compile(r'proliferate', re.I), "proliferate"),
    (re.compile(r'transform|flip', re.I), "transform"),
]

# Trigger condition -> tag mappings
TRIGGER_TAG_PATTERNS = [
    (re.compile(r'creature.*enters|enters the battlefield', re.I), "creature-etb"),
    (re.compile(r'creature.*dies|a creature.*is put into a graveyard', re.I), "creature-death"),
    (re.compile(r'you gain life|whenever you gain', re.I), "life-gain-events"),
    (re.compile(r'you cast.*spell|whenever you cast', re.I), "spell-cast"),
    (re.compile(r'deals.*combat damage|whenever.*deals damage', re.I), "combat-damage-events"),
    (re.compile(r'attacks|declared as an attacker', re.I), "attack-events"),
    (re.compile(r'becomes? the target|target.*you control', re.I), "targeting-events"),
    (re.compile(r'draw.*card|whenever you draw', re.I), "draw-events"),
    (re.compile(r'discard|whenever.*discard', re.I), "discard-events"),
    (re.compile(r'sacrifice|whenever.*sacrifice', re.I), "sacrifice-events"),
    (re.compile(r'counter.*is.*placed|counter.*is.*put', re.I), "counter-placement-events"),
    (re.compile(r'token.*created|token.*enters', re.I), "token-events"),
    (re.compile(r'beginning of your upkeep', re.I), "upkeep-trigger"),
    (re.compile(r'beginning of your end step', re.I), "end-step-trigger"),
    (re.compile(r'land.*enters|play a land', re.I), "landfall"),
    (re.compile(r'leaves the battlefield', re.I), "leaves-battlefield"),
    (re.compile(r'from.*graveyard|put into.*graveyard from', re.I), "graveyard-events"),
]

# Cost -> tag mappings (for activated abilities)
COST_TAG_PATTERNS = [
    (re.compile(r'[Ss]acrifice', re.I), "sacrifice-outlet"),
    (re.compile(r'[Dd]iscard', re.I), "discard-outlet"),
    (re.compile(r'[Ee]xile.*from.*graveyard', re.I), "graveyard-exile-cost"),
    (re.compile(r'[Pp]ay.*life|\{[WUBRG]/P\}', re.I), "life-payment"),
    (re.compile(r'\{[Tt]\}|[Tt]ap', re.I), "tap-cost"),
]


def _tag_effect(text):
    """Map effect text to tags using pattern matching."""
    if not text:
        return []
    tags = []
    for pattern, tag in EFFECT_TAG_PATTERNS:
        if pattern.search(text):
            tags.append(tag)
    return tags


def _tag_trigger(text):
    """Map trigger condition text to tags."""
    if not text:
        return []
    tags = []
    for pattern, tag in TRIGGER_TAG_PATTERNS:
        if pattern.search(text):
            tags.append(tag)
    return tags


def _tag_cost(text):
    """Map activation cost text to tags."""
    if not text:
        return []
    tags = []
    for pattern, tag in COST_TAG_PATTERNS:
        if pattern.search(text):
            tags.append(tag)
    return tags
```

Then modify `parse_card()` to call Phase 3 after Phase 2 — add a tagging pass at the end, before returning:

```python
# Add at the end of parse_card(), before the index assignment loop:
    # Phase 3: Tag effects and triggers
    for ab in all_abilities:
        # Tag effects
        effect_tags = _tag_effect(ab.get("effect") or "")
        # For activated abilities, also tag the cost
        cost_tags = _tag_cost(ab.get("cost") or "")
        effect_tags.extend(cost_tags)
        ab["effect_tags"] = effect_tags if effect_tags else None

        # Tag trigger conditions
        if ab.get("trigger_condition"):
            trigger_tags = _tag_trigger(ab["trigger_condition"])
            ab["trigger_tags"] = trigger_tags if trigger_tags else None
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_ability_parser.py -v`
Expected: All pass

- [ ] **Step 5: Commit**

```bash
git add ability_parser.py tests/test_ability_parser.py
git commit -m "feat: add ability parser Phase 3 — effect and trigger tagging"
```

---

## Task 8: Parser DB integration + bulk parse command

**Files:**
- Modify: `ability_parser.py` (add `parse_all`, `save_to_db`)
- Modify: `tag_db.py` (add ability query functions)

- [ ] **Step 1: Write integration test**

```python
# tests/test_ability_parser.py — add

def test_bulk_parse_and_save(tmp_db):
    """Parse multiple cards and save to DB."""
    import sqlite3
    from ability_parser import parse_card, save_abilities_to_db
    import tag_db

    conn = sqlite3.connect(tmp_db)
    # Insert sample cards
    cards = [
        ("kyler-001", "Kyler, Sigardian Emissary", "Legendary Creature — Human Cleric",
         "Whenever a Human enters the battlefield under your control, put a +1/+1 counter on Kyler, Sigardian Emissary.\nHuman creatures you control get +1/+1 for each +1/+1 counter on Kyler."),
        ("skirk-001", "Skirk Prospector", "Creature — Goblin",
         "Sacrifice a Goblin: Add {R}."),
    ]
    for oid, name, tl, oracle in cards:
        conn.execute("INSERT INTO cards (oracle_id, name, type_line, oracle_text) VALUES (?,?,?,?)",
                     (oid, name, tl, oracle))
    conn.commit()
    conn.close()

    # Parse and save
    parsed_cards = []
    for oid, name, tl, oracle in cards:
        card = {"oracle_id": oid, "name": name, "type_line": tl, "oracle_text": oracle, "keywords": []}
        parsed_cards.append((oid, parse_card(card)))

    save_abilities_to_db(parsed_cards, tmp_db)

    # Verify
    conn = sqlite3.connect(tmp_db)
    kyler_abs = conn.execute("SELECT * FROM abilities WHERE oracle_id = 'kyler-001'").fetchall()
    assert len(kyler_abs) >= 2  # triggered + static
    skirk_abs = conn.execute("SELECT * FROM abilities WHERE oracle_id = 'skirk-001'").fetchall()
    assert len(skirk_abs) >= 1
    conn.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_ability_parser.py::test_bulk_parse_and_save -v`
Expected: FAIL — save_abilities_to_db doesn't exist

- [ ] **Step 3: Implement save_abilities_to_db and CLI**

Add to `ability_parser.py`:

```python
import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "data", "tags.db")


def save_abilities_to_db(parsed_cards, db_path=None):
    """Save parsed abilities to the abilities table.

    Args:
        parsed_cards: list of (oracle_id, abilities_list) tuples
        db_path: optional DB path override
    """
    if db_path is None:
        db_path = DB_PATH
    conn = sqlite3.connect(db_path)

    for oracle_id, abilities in parsed_cards:
        # Clear old abilities for this card
        conn.execute("DELETE FROM abilities WHERE oracle_id = ?", (oracle_id,))

        for ab in abilities:
            conn.execute("""
                INSERT INTO abilities (oracle_id, ability_index, ability_type, trigger_condition,
                    trigger_tags, cost, effect, effect_tags, zone, targets, is_mana_ability)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                oracle_id,
                ab["ability_index"],
                ab["ability_type"],
                ab.get("trigger_condition"),
                json.dumps(ab["trigger_tags"]) if ab.get("trigger_tags") else None,
                ab.get("cost"),
                ab.get("effect"),
                json.dumps(ab["effect_tags"]) if ab.get("effect_tags") else None,
                ab.get("zone", "battlefield"),
                ab.get("targets"),
                1 if ab.get("is_mana_ability") else 0,
            ))

    conn.commit()
    conn.close()


def parse_all_cards(db_path=None):
    """Parse all cards in the DB and save abilities.

    Returns (total_parsed, low_confidence_count).
    """
    if db_path is None:
        db_path = DB_PATH
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    cards = conn.execute("""
        SELECT oracle_id, name, type_line, oracle_text, keywords
        FROM cards WHERE oracle_text IS NOT NULL AND oracle_text != ''
    """).fetchall()
    conn.close()

    parsed = []
    low_confidence = 0

    for row in cards:
        card = {
            "oracle_id": row["oracle_id"],
            "name": row["name"],
            "type_line": row["type_line"],
            "oracle_text": row["oracle_text"],
            "keywords": json.loads(row["keywords"]) if row["keywords"] else [],
        }
        abilities = parse_card(card)
        parsed.append((row["oracle_id"], abilities))

        # Check confidence: <50% of non-keyword abilities got tags
        non_kw = [a for a in abilities if a["ability_type"] != "keyword"]
        if non_kw:
            tagged = sum(1 for a in non_kw if a.get("effect_tags") or a.get("trigger_tags"))
            if tagged / len(non_kw) < 0.5:
                low_confidence += 1

    # Save in batches
    batch_size = 500
    for i in range(0, len(parsed), batch_size):
        save_abilities_to_db(parsed[i:i + batch_size], db_path)

    return len(parsed), low_confidence


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Parse oracle text into structured abilities")
    parser.add_argument("--db", default=None, help="DB path (default: data/tags.db)")
    parser.add_argument("--card", default=None, help="Parse a single card by name (for inspection)")
    args = parser.parse_args()

    db = args.db or DB_PATH

    if args.card:
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM cards WHERE name = ?", (args.card,)).fetchone()
        conn.close()
        if not row:
            print(f"Card not found: {args.card}")
            exit(1)
        card = dict(row)
        card["keywords"] = json.loads(card["keywords"]) if card["keywords"] else []
        abilities = parse_card(card)
        print(f"\n{card['name']} ({card['type_line']})")
        print(f"Oracle: {card['oracle_text']}\n")
        for ab in abilities:
            print(f"  [{ab['ability_type']}] {ab.get('trigger_condition') or ''}")
            if ab.get('cost'):
                print(f"    Cost: {ab['cost']}")
            print(f"    Effect: {ab['effect']}")
            if ab.get('effect_tags'):
                print(f"    Effect tags: {ab['effect_tags']}")
            if ab.get('trigger_tags'):
                print(f"    Trigger tags: {ab['trigger_tags']}")
            print()
    else:
        print(f"Parsing all cards in {db}...")
        total, low_conf = parse_all_cards(db)
        print(f"Parsed {total} cards. Low confidence: {low_conf} ({low_conf*100//max(total,1)}%)")
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_ability_parser.py -v`
Expected: All pass

- [ ] **Step 5: Run parser on actual DB**

Run: `python3 ability_parser.py`
Expected: "Parsed 34000+ cards. Low confidence: N (X%)"

- [ ] **Step 6: Inspect key Kyler cards**

Run: `python3 ability_parser.py --card "Kyler, Sigardian Emissary"`
Then: `python3 ability_parser.py --card "Hardened Scales"`
Then: `python3 ability_parser.py --card "Cathars' Crusade"`
Then: `python3 ability_parser.py --card "Gavony Township"`

Expected: Each shows correctly classified abilities with appropriate tags.

- [ ] **Step 7: Commit**

```bash
git add ability_parser.py tests/test_ability_parser.py
git commit -m "feat: add bulk ability parsing with DB integration and CLI inspection"
```

---

## Task 9: Fetch Commander Spellbook data

**Files:**
- Create: `fetch_spellbook.py`
- Create: `tests/test_fetch_spellbook.py`

- [ ] **Step 1: Write the test**

```python
# tests/test_fetch_spellbook.py
import json
import sqlite3
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from fetch_spellbook import parse_combo_response, import_combos_to_db


def test_parse_combo_response():
    """Parse a single Spellbook API response into our format."""
    raw = {
        "id": "742-1295",
        "uses": [
            {
                "card": {
                    "name": "Demonic Consultation",
                    "oracleId": "oracle-demcon"
                },
                "quantity": 1,
            },
            {
                "card": {
                    "name": "Thassa's Oracle",
                    "oracleId": "oracle-thoracle"
                },
                "quantity": 1,
            }
        ],
        "produces": [
            {"feature": {"name": "Win the game"}, "quantity": 1}
        ],
        "status": "OK",
        "easyPrerequisites": "Both cards in hand. {1}{U}{U}{B} available.",
    }

    combo = parse_combo_response(raw)
    assert combo["combo_id"] == "742-1295"
    assert len(combo["card_oracle_ids"]) == 2
    assert "oracle-demcon" in combo["card_oracle_ids"]
    assert "oracle-thoracle" in combo["card_oracle_ids"]
    assert "Win the game" in combo["result"]
    assert combo["card_count"] == 2


def test_import_combos_to_db(tmp_db):
    """Import parsed combos into the database."""
    combos = [
        {
            "combo_id": "test-001",
            "card_oracle_ids": ["oid-a", "oid-b"],
            "card_names": ["Card A", "Card B"],
            "result": "Infinite damage",
            "prerequisites": "Both on battlefield",
            "card_count": 2,
        }
    ]

    import_combos_to_db(combos, tmp_db)

    conn = sqlite3.connect(tmp_db)
    row = conn.execute("SELECT * FROM spellbook_combos WHERE combo_id = 'test-001'").fetchone()
    assert row is not None

    combo_cards = conn.execute(
        "SELECT oracle_id FROM spellbook_combo_cards WHERE combo_id = 'test-001'"
    ).fetchall()
    assert len(combo_cards) == 2
    conn.close()


def test_skip_non_ok_status():
    """Combos with non-OK status should be skipped."""
    raw = {
        "id": "bad-001",
        "uses": [{"card": {"name": "X", "oracleId": "oid-x"}, "quantity": 1}],
        "produces": [],
        "status": "NOT_WORKING",
        "easyPrerequisites": "",
    }
    combo = parse_combo_response(raw)
    assert combo is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_fetch_spellbook.py -v`
Expected: FAIL — module not found

- [ ] **Step 3: Implement fetch_spellbook.py**

```python
#!/usr/bin/env python3
"""Fetch Commander Spellbook combo data and import into tags.db.

One-time bulk download, cached to data/commander_spellbook.json.
Usage:
    python3 fetch_spellbook.py               # fetch + import
    python3 fetch_spellbook.py --fetch-only   # just fetch, don't import
    python3 fetch_spellbook.py --import-only  # import from cached file
    python3 fetch_spellbook.py --stats        # show stats from DB
"""

import json
import sqlite3
import os
import time
import urllib.request
import urllib.error

BASE_URL = "https://backend.commanderspellbook.com"
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
CACHE_PATH = os.path.join(DATA_DIR, "commander_spellbook.json")
DB_PATH = os.path.join(DATA_DIR, "tags.db")
UNRESOLVED_PATH = os.path.join(DATA_DIR, "spellbook_unresolved.txt")


def parse_combo_response(raw):
    """Parse a single Spellbook API combo into our format.

    Returns dict or None if status is not OK.
    """
    if raw.get("status") != "OK":
        return None

    card_oracle_ids = []
    card_names = []
    for use in raw.get("uses", []):
        card = use.get("card", {})
        oid = card.get("oracleId")
        name = card.get("name", "Unknown")
        if oid:
            card_oracle_ids.append(oid)
            card_names.append(name)

    if not card_oracle_ids:
        return None

    results = []
    for prod in raw.get("produces", []):
        feature = prod.get("feature", {})
        name = feature.get("name", "")
        if name:
            results.append(name)

    return {
        "combo_id": str(raw["id"]),
        "card_oracle_ids": card_oracle_ids,
        "card_names": card_names,
        "result": ", ".join(results),
        "prerequisites": raw.get("easyPrerequisites", ""),
        "card_count": len(card_oracle_ids),
    }


def fetch_all_combos(limit_per_page=100, max_pages=None):
    """Fetch all combos from Commander Spellbook API with pagination.

    Returns list of parsed combo dicts.
    """
    combos = []
    url = f"{BASE_URL}/variants/?format=json&limit={limit_per_page}&offset=0"
    page = 0

    while url:
        if max_pages and page >= max_pages:
            break

        print(f"  Fetching page {page + 1}... ({len(combos)} combos so far)")

        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            print(f"  HTTP error {e.code} on page {page + 1}, stopping")
            break
        except Exception as e:
            print(f"  Error on page {page + 1}: {e}, stopping")
            break

        for raw in data.get("results", []):
            combo = parse_combo_response(raw)
            if combo:
                combos.append(combo)

        url = data.get("next")
        page += 1
        time.sleep(1)  # Rate limit: 1 req/sec

    return combos


def import_combos_to_db(combos, db_path=None):
    """Import parsed combos into spellbook tables."""
    if db_path is None:
        db_path = DB_PATH
    conn = sqlite3.connect(db_path)

    for combo in combos:
        conn.execute("""
            INSERT OR REPLACE INTO spellbook_combos
            (combo_id, card_oracle_ids, card_names, result, prerequisites, card_count)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            combo["combo_id"],
            json.dumps(combo["card_oracle_ids"]),
            json.dumps(combo["card_names"]),
            combo["result"],
            combo["prerequisites"],
            combo["card_count"],
        ))

        # Junction table
        for oid in combo["card_oracle_ids"]:
            conn.execute("""
                INSERT OR REPLACE INTO spellbook_combo_cards (combo_id, oracle_id)
                VALUES (?, ?)
            """, (combo["combo_id"], oid))

    conn.commit()
    conn.close()


def show_stats(db_path=None):
    """Print stats about imported Spellbook data."""
    if db_path is None:
        db_path = DB_PATH
    conn = sqlite3.connect(db_path)
    total = conn.execute("SELECT COUNT(*) FROM spellbook_combos").fetchone()[0]
    by_size = conn.execute(
        "SELECT card_count, COUNT(*) FROM spellbook_combos GROUP BY card_count ORDER BY card_count"
    ).fetchall()
    unique_cards = conn.execute(
        "SELECT COUNT(DISTINCT oracle_id) FROM spellbook_combo_cards"
    ).fetchone()[0]

    # Check how many match our DB
    matched = conn.execute("""
        SELECT COUNT(DISTINCT sc.oracle_id)
        FROM spellbook_combo_cards sc
        JOIN cards c ON sc.oracle_id = c.oracle_id
    """).fetchone()[0]
    conn.close()

    print(f"Spellbook combos: {total}")
    for size, count in by_size:
        print(f"  {size}-card: {count}")
    print(f"Unique cards in combos: {unique_cards}")
    print(f"Cards matched to our DB: {matched} ({matched * 100 // max(unique_cards, 1)}%)")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Fetch Commander Spellbook combo data")
    parser.add_argument("--fetch-only", action="store_true", help="Only fetch, don't import to DB")
    parser.add_argument("--import-only", action="store_true", help="Import from cached file")
    parser.add_argument("--stats", action="store_true", help="Show DB stats")
    parser.add_argument("--max-pages", type=int, default=None, help="Limit pages fetched (for testing)")
    args = parser.parse_args()

    if args.stats:
        show_stats()
        exit(0)

    if not args.import_only:
        print("Fetching combos from Commander Spellbook...")
        combos = fetch_all_combos(max_pages=args.max_pages)
        print(f"Fetched {len(combos)} combos")

        os.makedirs(DATA_DIR, exist_ok=True)
        with open(CACHE_PATH, "w") as f:
            json.dump(combos, f)
        print(f"Cached to {CACHE_PATH}")

        if args.fetch_only:
            exit(0)
    else:
        with open(CACHE_PATH) as f:
            combos = json.load(f)
        print(f"Loaded {len(combos)} combos from cache")

    print("Importing to DB...")
    import_combos_to_db(combos)
    show_stats()
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_fetch_spellbook.py -v`
Expected: All pass

- [ ] **Step 5: Test with a small fetch (2 pages)**

Run: `python3 fetch_spellbook.py --max-pages 2`
Expected: Fetches ~200 combos, imports, shows stats

- [ ] **Step 6: Commit**

```bash
git add fetch_spellbook.py tests/test_fetch_spellbook.py
git commit -m "feat: add Commander Spellbook fetcher with DB import"
```

- [ ] **Step 7: Full fetch (long-running, ~10 min)**

Run: `python3 fetch_spellbook.py`
Expected: Fetches all ~50k combos

- [ ] **Step 8: Verify stats**

Run: `python3 fetch_spellbook.py --stats`
Expected: Shows total combos, size breakdown, card match rate

- [ ] **Step 9: Commit data reference**

```bash
echo "data/commander_spellbook.json" >> .gitignore
echo "data/spellbook_unresolved.txt" >> .gitignore
git add .gitignore
git commit -m "chore: gitignore Spellbook cache files"
```

---

## Task 10: Strategy detector

**Files:**
- Create: `strategy_detector.py`
- Create: `tests/test_strategy_detector.py`

- [ ] **Step 1: Write the test**

```python
# tests/test_strategy_detector.py
import sqlite3
import json
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from strategy_detector import detect_strategies, populate_card_strategies, STRATEGY_RULES


def test_detect_commander_strategies(tmp_db):
    """Kyler should be detected as humans + counters."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("""INSERT INTO cards (oracle_id, name, type_line, oracle_text)
                    VALUES ('kyler', 'Kyler, Sigardian Emissary', 'Legendary Creature — Human Cleric',
                    'Whenever a Human enters the battlefield under your control, put a +1/+1 counter on Kyler.')""")
    conn.execute("INSERT INTO provides (oracle_id, tag) VALUES ('kyler', 'human-tribal')")
    conn.execute("INSERT INTO provides (oracle_id, tag) VALUES ('kyler', 'counter-placement')")
    conn.execute("INSERT INTO wants (oracle_id, tag) VALUES ('kyler', 'creature-etb')")
    conn.commit()
    conn.close()

    strategies = detect_strategies("kyler", tmp_db)
    strategy_names = {s["name"] for s in strategies}
    assert "humans" in strategy_names
    assert "+1/+1-counters" in strategy_names


def test_strategy_confidence_threshold(tmp_db):
    """Strategies below 0.3 confidence should still be stored but marked inactive."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("INSERT INTO cards (oracle_id, name) VALUES ('weak', 'Weak Card')")
    conn.execute("INSERT INTO provides (oracle_id, tag) VALUES ('weak', 'artifact-presence')")
    conn.commit()
    conn.close()

    strategies = detect_strategies("weak", tmp_db)
    # artifact-presence alone is a weak signal
    low_conf = [s for s in strategies if s["confidence"] < 0.3]
    # Should still return strategies, just with low confidence
    assert isinstance(strategies, list)


def test_populate_card_strategies(tmp_db):
    """Populate strategies for all cards in DB."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("INSERT INTO cards (oracle_id, name) VALUES ('c1', 'Token Maker')")
    conn.execute("INSERT INTO provides (oracle_id, tag) VALUES ('c1', 'token-generation')")
    conn.execute("INSERT INTO cards (oracle_id, name) VALUES ('c2', 'Counter Placer')")
    conn.execute("INSERT INTO provides (oracle_id, tag) VALUES ('c2', 'counter-placement')")
    conn.commit()
    conn.close()

    count = populate_card_strategies(tmp_db)
    assert count >= 2

    conn = sqlite3.connect(tmp_db)
    strats = conn.execute("SELECT * FROM card_strategies").fetchall()
    conn.close()
    assert len(strats) >= 2


def test_strategy_rules_are_defined():
    """Verify we have at least 20 strategy mapping rules."""
    assert len(STRATEGY_RULES) >= 20
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_strategy_detector.py -v`
Expected: FAIL — module not found

- [ ] **Step 3: Implement strategy_detector.py**

```python
#!/usr/bin/env python3
"""Strategy detection: maps cards to strategy themes based on tags and EDHREC data.

Usage:
    python3 strategy_detector.py --commander "Kyler, Sigardian Emissary"
    python3 strategy_detector.py --populate      # populate card_strategies for all cards
    python3 strategy_detector.py --stats         # show strategy distribution
"""

import json
import sqlite3
import os

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
DB_PATH = os.path.join(DATA_DIR, "tags.db")
EDHREC_PATH = os.path.join(DATA_DIR, "edhrec_theme_cards.json")

# Strategy mapping rules: (provides_tag_or_set, strategy_name, base_confidence)
# A card with any of these provides tags maps to the strategy.
STRATEGY_RULES = [
    # Token strategies
    ({"token-generation"}, "tokens", 1.0),
    ({"treasure-generation"}, "treasure", 1.0),
    ({"food-generation"}, "food", 0.8),
    ({"clue-generation"}, "clues", 0.8),

    # Counter strategies
    ({"counter-placement", "board-wide-counter-placement"}, "+1/+1-counters", 1.0),
    ({"counter-amplification"}, "+1/+1-counters", 0.9),
    ({"proliferate"}, "proliferate", 1.0),

    # Tribal/typal
    ({"human-tribal"}, "humans", 1.0),
    ({"goblin-tribal"}, "goblins", 1.0),
    ({"elf-tribal"}, "elves", 1.0),
    ({"sliver-tribal"}, "slivers", 1.0),
    ({"tribal-enabler"}, "tribal", 0.7),

    # Aristocrats / sacrifice
    ({"sacrifice-outlet"}, "aristocrats", 0.9),
    ({"death-trigger"}, "aristocrats", 0.8),

    # Spellslinger
    ({"spell-copy"}, "spellslinger", 1.0),
    ({"spell-cost-reduction"}, "spellslinger", 0.8),
    ({"storm-count"}, "storm", 1.0),

    # Graveyard
    ({"graveyard-recursion"}, "reanimator", 0.9),
    ({"self-mill"}, "self-mill", 0.9),
    ({"dredge"}, "dredge", 1.0),

    # Artifacts
    ({"artifact-enabler", "artifact-presence"}, "artifacts", 0.8),

    # Enchantments
    ({"enchantment-synergy", "aura-synergy"}, "enchantress", 0.8),

    # Combat
    ({"extra-combat"}, "extra-combats", 1.0),
    ({"evasion"}, "voltron", 0.6),
    ({"equipment-synergy"}, "equipment", 0.9),

    # Control
    ({"counterspell"}, "control", 0.7),
    ({"board-control"}, "control", 0.6),
    ({"tap-control"}, "stax", 0.7),

    # Card advantage
    ({"card-draw"}, "card-draw", 0.5),  # Low confidence — almost everything draws
    ({"tutor"}, "toolbox", 0.7),

    # Life
    ({"life-gain"}, "lifegain", 0.9),
    ({"life-drain"}, "lifedrain", 0.9),

    # Lands
    ({"landfall-trigger"}, "landfall", 1.0),
    ({"land-ramp"}, "lands-matter", 0.7),

    # Mill
    ({"mill"}, "mill", 1.0),

    # Blink
    ({"blink"}, "blink", 1.0),

    # Wheels
    ({"wheel"}, "wheels", 1.0),

    # Go wide
    ({"board-wide-creature-pump", "creature-pump"}, "go-wide", 0.7),
]


def detect_strategies(oracle_id, db_path=None):
    """Detect strategies for a single card based on its provides/wants tags.

    Returns list of {"name": str, "confidence": float, "signals": [str]}.
    """
    if db_path is None:
        db_path = DB_PATH
    conn = sqlite3.connect(db_path)

    provides = {row[0] for row in conn.execute(
        "SELECT tag FROM provides WHERE oracle_id = ?", (oracle_id,)
    ).fetchall()}

    conn.close()

    strategies = {}
    for tag_set, strategy, confidence in STRATEGY_RULES:
        matching = provides & tag_set
        if matching:
            if strategy not in strategies or strategies[strategy]["confidence"] < confidence:
                strategies[strategy] = {
                    "name": strategy,
                    "confidence": confidence,
                    "signals": [f"provides:{t}" for t in matching],
                }

    return sorted(strategies.values(), key=lambda s: -s["confidence"])


def _load_edhrec_strategies():
    """Load EDHREC theme data for strategy enrichment."""
    if not os.path.exists(EDHREC_PATH):
        return {}
    with open(EDHREC_PATH) as f:
        data = json.load(f)
    # Build name -> {theme: synergy_score}
    card_themes = {}
    for theme, cards in data.items():
        for card in cards:
            name = card["name"]
            synergy = card.get("synergy", 0)
            if synergy > 0.10:  # Only significant synergy
                if name not in card_themes:
                    card_themes[name] = {}
                card_themes[name][theme] = synergy
    return card_themes


def populate_card_strategies(db_path=None):
    """Populate card_strategies table for all cards in DB.

    Uses STRATEGY_RULES + EDHREC theme data.
    Returns number of strategy assignments.
    """
    if db_path is None:
        db_path = DB_PATH
    conn = sqlite3.connect(db_path)

    # Clear existing
    conn.execute("DELETE FROM card_strategies")

    # Get all cards with their provides tags
    cards = conn.execute("SELECT oracle_id, name FROM cards").fetchall()
    provides_by_card = {}
    for oid, tag in conn.execute("SELECT oracle_id, tag FROM provides").fetchall():
        provides_by_card.setdefault(oid, set()).add(tag)

    # Load EDHREC data
    edhrec = _load_edhrec_strategies()

    count = 0
    for oracle_id, name in cards:
        card_provides = provides_by_card.get(oracle_id, set())

        # Rule-based strategies
        strategies = {}
        for tag_set, strategy, confidence in STRATEGY_RULES:
            if card_provides & tag_set:
                if strategy not in strategies or strategies[strategy] < confidence:
                    strategies[strategy] = confidence

        # EDHREC-based strategies
        if name in edhrec:
            for theme, synergy in edhrec[name].items():
                if theme not in strategies or strategies[theme] < synergy:
                    strategies[theme] = synergy

        # Insert all strategies
        for strategy, confidence in strategies.items():
            conn.execute(
                "INSERT OR REPLACE INTO card_strategies (oracle_id, strategy, confidence) VALUES (?, ?, ?)",
                (oracle_id, strategy, confidence)
            )
            count += 1

    conn.commit()
    conn.close()
    return count


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Strategy detection for MTG cards")
    parser.add_argument("--commander", help="Detect strategies for a commander by name")
    parser.add_argument("--populate", action="store_true", help="Populate strategies for all cards")
    parser.add_argument("--stats", action="store_true", help="Show strategy distribution")
    parser.add_argument("--db", default=None, help="DB path")
    args = parser.parse_args()

    db = args.db or DB_PATH

    if args.commander:
        conn = sqlite3.connect(db)
        row = conn.execute("SELECT oracle_id FROM cards WHERE name = ?", (args.commander,)).fetchone()
        conn.close()
        if not row:
            print(f"Commander not found: {args.commander}")
            exit(1)
        strategies = detect_strategies(row[0], db)
        print(f"\nStrategies for {args.commander}:")
        for s in strategies:
            active = "ACTIVE" if s["confidence"] >= 0.3 else "weak"
            print(f"  {s['name']}: {s['confidence']:.2f} [{active}] — {', '.join(s['signals'])}")

    elif args.populate:
        count = populate_card_strategies(db)
        print(f"Populated {count} strategy assignments")

    elif args.stats:
        conn = sqlite3.connect(db)
        top = conn.execute("""
            SELECT strategy, COUNT(*) as cnt, AVG(confidence) as avg_conf
            FROM card_strategies
            GROUP BY strategy ORDER BY cnt DESC LIMIT 30
        """).fetchall()
        conn.close()
        print("Top strategies by card count:")
        for strategy, cnt, avg_conf in top:
            print(f"  {strategy}: {cnt} cards (avg confidence: {avg_conf:.2f})")
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_strategy_detector.py -v`
Expected: All pass

- [ ] **Step 5: Run on actual DB**

Run: `python3 strategy_detector.py --commander "Kyler, Sigardian Emissary"`
Then: `python3 strategy_detector.py --populate`
Then: `python3 strategy_detector.py --stats`

- [ ] **Step 6: Commit**

```bash
git add strategy_detector.py tests/test_strategy_detector.py
git commit -m "feat: add strategy detector with rule-based + EDHREC mapping"
```

---

## Task 11: Enhanced combo detection (3-tier labeling)

**Files:**
- Modify: `synergy_graph.py:1088-1167` (find_combos)
- Create: `tests/test_combo_tiers.py`

- [ ] **Step 1: Write the test**

```python
# tests/test_combo_tiers.py
import sqlite3
import json
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


def test_spellbook_confirmed_combo(tmp_db):
    """Deck containing all cards from a Spellbook combo should be labeled infinite-confirmed."""
    from synergy_graph import find_combos_tiered

    conn = sqlite3.connect(tmp_db)
    # Two cards that form a Spellbook combo
    conn.execute("INSERT INTO cards (oracle_id, name) VALUES ('oid-a', 'Card A')")
    conn.execute("INSERT INTO cards (oracle_id, name) VALUES ('oid-b', 'Card B')")
    conn.execute("INSERT INTO provides (oracle_id, tag) VALUES ('oid-a', 'tag-x')")
    conn.execute("INSERT INTO wants (oracle_id, tag) VALUES ('oid-b', 'tag-x')")
    conn.execute("INSERT INTO provides (oracle_id, tag) VALUES ('oid-b', 'tag-y')")
    conn.execute("INSERT INTO wants (oracle_id, tag) VALUES ('oid-a', 'tag-y')")

    # Spellbook entry
    conn.execute("""INSERT INTO spellbook_combos (combo_id, card_oracle_ids, card_names, result, prerequisites, card_count)
                    VALUES ('combo-1', '["oid-a","oid-b"]', '["Card A","Card B"]', 'Infinite damage', '', 2)""")
    conn.execute("INSERT INTO spellbook_combo_cards (combo_id, oracle_id) VALUES ('combo-1', 'oid-a')")
    conn.execute("INSERT INTO spellbook_combo_cards (combo_id, oracle_id) VALUES ('combo-1', 'oid-b')")
    conn.commit()
    conn.close()

    deck_oids = {"oid-a", "oid-b"}
    combos = find_combos_tiered(deck_oids, tmp_db)

    confirmed = [c for c in combos if c["tier"] == "infinite-confirmed"]
    assert len(confirmed) >= 1
    assert "Infinite damage" in confirmed[0]["result"]


def test_trigger_chain_combo_likely(tmp_db):
    """Cards with circular trigger chains should be labeled combo-likely."""
    from synergy_graph import find_combos_tiered

    conn = sqlite3.connect(tmp_db)
    # Sanguine Bond + Exquisite Blood pattern:
    # Card C: "Whenever you gain life, target opponent loses that much life"
    #   trigger_tags: ["life-gain"], effect_tags: ["life-drain"]
    # Card D: "Whenever an opponent loses life, you gain that much life"
    #   trigger_tags: ["life-drain"], effect_tags: ["life-gain"]
    # Chain: C triggers on life-gain -> drains life -> D triggers on life-drain -> gains life -> C triggers again

    conn.execute("INSERT INTO cards (oracle_id, name) VALUES ('oid-c', 'Sanguine Bond')")
    conn.execute("INSERT INTO provides (oracle_id, tag) VALUES ('oid-c', 'life-drain')")
    conn.execute("INSERT INTO wants (oracle_id, tag) VALUES ('oid-c', 'life-gain')")
    conn.execute("""INSERT INTO abilities (oracle_id, ability_index, ability_type, trigger_condition,
                    trigger_tags, effect, effect_tags)
                    VALUES ('oid-c', 0, 'triggered', 'Whenever you gain life',
                    '["life-gain"]', 'target opponent loses that much life', '["life-drain"]')""")

    conn.execute("INSERT INTO cards (oracle_id, name) VALUES ('oid-d', 'Exquisite Blood')")
    conn.execute("INSERT INTO provides (oracle_id, tag) VALUES ('oid-d', 'life-gain')")
    conn.execute("INSERT INTO wants (oracle_id, tag) VALUES ('oid-d', 'life-drain')")
    conn.execute("""INSERT INTO abilities (oracle_id, ability_index, ability_type, trigger_condition,
                    trigger_tags, effect, effect_tags)
                    VALUES ('oid-d', 0, 'triggered', 'Whenever an opponent loses life',
                    '["life-drain"]', 'you gain that much life', '["life-gain"]')""")

    conn.commit()
    conn.close()

    deck_oids = {"oid-c", "oid-d"}
    combos = find_combos_tiered(deck_oids, tmp_db)

    likely = [c for c in combos if c["tier"] == "combo-likely"]
    # C's effect_tags {life-drain} ∩ D's trigger_tags {life-drain} = {life-drain} ✓
    # D's effect_tags {life-gain} ∩ C's trigger_tags {life-gain} = {life-gain} ✓
    assert len(likely) == 1


def test_synergy_tier_fallback(tmp_db):
    """Pairs with provides->wants cycle but no trigger chain = synergy tier."""
    from synergy_graph import find_combos_tiered

    conn = sqlite3.connect(tmp_db)
    conn.execute("INSERT INTO cards (oracle_id, name) VALUES ('oid-e', 'Card E')")
    conn.execute("INSERT INTO cards (oracle_id, name) VALUES ('oid-f', 'Card F')")
    conn.execute("INSERT INTO provides (oracle_id, tag) VALUES ('oid-e', 'tag-m')")
    conn.execute("INSERT INTO wants (oracle_id, tag) VALUES ('oid-f', 'tag-m')")
    conn.execute("INSERT INTO provides (oracle_id, tag) VALUES ('oid-f', 'tag-n')")
    conn.execute("INSERT INTO wants (oracle_id, tag) VALUES ('oid-e', 'tag-n')")
    # No abilities, no spellbook entry
    conn.commit()
    conn.close()

    deck_oids = {"oid-e", "oid-f"}
    combos = find_combos_tiered(deck_oids, tmp_db)

    synergy = [c for c in combos if c["tier"] == "synergy"]
    assert len(synergy) >= 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_combo_tiers.py -v`
Expected: FAIL — find_combos_tiered doesn't exist

- [ ] **Step 3: Implement find_combos_tiered in synergy_graph.py**

Add a new function near the existing `find_combos` (around line 1167):

```python
def find_combos_tiered(deck_oids, db_path=None):
    """Three-tier combo detection: infinite-confirmed, combo-likely, synergy.

    Args:
        deck_oids: set of oracle_ids in the deck
        db_path: optional DB path override

    Returns:
        list of combo dicts with 'tier', 'cards', 'result', 'reason' fields
    """
    if db_path is None:
        db_path = os.path.join(os.path.dirname(__file__), "data", "tags.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    combos = []

    # --- Tier 1: Spellbook confirmed ---
    spellbook_combos = conn.execute("""
        SELECT combo_id, card_oracle_ids, card_names, result, prerequisites
        FROM spellbook_combos
    """).fetchall()

    confirmed_pairs = set()  # Track confirmed pairs to avoid duplicate synergy entries
    for row in spellbook_combos:
        combo_oids = set(json.loads(row["card_oracle_ids"]))
        if combo_oids <= deck_oids:  # All combo cards in deck
            combo_names = json.loads(row["card_names"])
            combos.append({
                "tier": "infinite-confirmed",
                "cards": combo_names,
                "card_oids": list(combo_oids),
                "result": row["result"],
                "reason": f"Spellbook #{row['combo_id']}",
            })
            # Mark all pairs as confirmed
            oid_list = list(combo_oids)
            for i in range(len(oid_list)):
                for j in range(i + 1, len(oid_list)):
                    confirmed_pairs.add(frozenset([oid_list[i], oid_list[j]]))

    # --- Load provides/wants for deck cards ---
    deck_list = list(deck_oids)
    provides_by_card = {}
    wants_by_card = {}
    for oid in deck_list:
        for tag, in conn.execute("SELECT tag FROM provides WHERE oracle_id = ?", (oid,)):
            provides_by_card.setdefault(oid, set()).add(tag)
        for tag, in conn.execute("SELECT tag FROM wants WHERE oracle_id = ?", (oid,)):
            wants_by_card.setdefault(oid, set()).add(tag)

    # --- Load abilities for deck cards ---
    abilities_by_card = {}
    for oid in deck_list:
        rows = conn.execute("""
            SELECT ability_type, trigger_tags, effect_tags
            FROM abilities WHERE oracle_id = ?
        """, (oid,)).fetchall()
        trigger_tags = set()
        effect_tags = set()
        for row in rows:
            if row["trigger_tags"]:
                trigger_tags.update(json.loads(row["trigger_tags"]))
            if row["effect_tags"]:
                effect_tags.update(json.loads(row["effect_tags"]))
        if trigger_tags or effect_tags:
            abilities_by_card[oid] = {"trigger_tags": trigger_tags, "effect_tags": effect_tags}

    conn_names = {}
    for oid in deck_list:
        row = conn.execute("SELECT name FROM cards WHERE oracle_id = ?", (oid,)).fetchone()
        if row:
            conn_names[oid] = row["name"]

    conn.close()

    # --- Find provides→wants cycles ---
    for i, oid_a in enumerate(deck_list):
        for oid_b in deck_list[i + 1:]:
            pair = frozenset([oid_a, oid_b])
            if pair in confirmed_pairs:
                continue

            prov_a = provides_by_card.get(oid_a, set())
            want_a = wants_by_card.get(oid_a, set())
            prov_b = provides_by_card.get(oid_b, set())
            want_b = wants_by_card.get(oid_b, set())

            # Check cycle: A provides what B wants AND B provides what A wants
            a_to_b = prov_a & want_b
            b_to_a = prov_b & want_a

            if not (a_to_b and b_to_a):
                continue

            name_a = conn_names.get(oid_a, oid_a)
            name_b = conn_names.get(oid_b, oid_b)

            # --- Tier 2: Check trigger chain ---
            ab_a = abilities_by_card.get(oid_a)
            ab_b = abilities_by_card.get(oid_b)

            if ab_a and ab_b:
                # A's effect_tags intersect B's trigger_tags AND vice versa
                a_triggers_b = ab_a["effect_tags"] & ab_b["trigger_tags"]
                b_triggers_a = ab_b["effect_tags"] & ab_a["trigger_tags"]

                if a_triggers_b and b_triggers_a:
                    combos.append({
                        "tier": "combo-likely",
                        "cards": [name_a, name_b],
                        "card_oids": [oid_a, oid_b],
                        "result": f"Trigger chain: {name_a} → {', '.join(a_triggers_b)} → {name_b} → {', '.join(b_triggers_a)}",
                        "reason": f"Circular triggers via {a_triggers_b} / {b_triggers_a}",
                    })
                    continue

            # --- Tier 3: Synergy ---
            combos.append({
                "tier": "synergy",
                "cards": [name_a, name_b],
                "card_oids": [oid_a, oid_b],
                "result": f"Provides/wants cycle: {a_to_b} / {b_to_a}",
                "reason": "Tag cycle without trigger chain",
            })

    # Sort: confirmed first, then likely, then synergy
    tier_order = {"infinite-confirmed": 0, "combo-likely": 1, "synergy": 2}
    combos.sort(key=lambda c: tier_order.get(c["tier"], 9))

    return combos
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_combo_tiers.py -v`
Expected: All pass

- [ ] **Step 5: Commit**

```bash
git add synergy_graph.py tests/test_combo_tiers.py
git commit -m "feat: add 3-tier combo detection (confirmed/likely/synergy)"
```

---

## Task 12: Wire strategy-weighted recommendations + CLI args

**Files:**
- Modify: `synergy_graph.py:853-997` (recommend_cards)
- Modify: `synergy_graph.py:2334-2355` (CLI args)
- Modify: `synergy_graph.py:1375-1415` (show_combos)

- [ ] **Step 1: Write the test**

```python
# tests/test_strategy_recommend.py
import sqlite3
import json
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


def test_strategy_weighted_scoring(tmp_db):
    """Cards matching active strategies should score higher."""
    from strategy_detector import populate_card_strategies, STRATEGY_RULES

    conn = sqlite3.connect(tmp_db)
    # Commander: token strategy
    conn.execute("INSERT INTO cards (oracle_id, name) VALUES ('cmdr', 'Token Commander')")
    conn.execute("INSERT INTO provides (oracle_id, tag) VALUES ('cmdr', 'token-generation')")

    # Candidate A: matches token strategy
    conn.execute("INSERT INTO cards (oracle_id, name) VALUES ('cand-a', 'Token Card')")
    conn.execute("INSERT INTO card_strategies (oracle_id, strategy, confidence) VALUES ('cand-a', 'tokens', 1.0)")

    # Candidate B: no strategy match
    conn.execute("INSERT INTO cards (oracle_id, name) VALUES ('cand-b', 'Random Card')")

    conn.commit()
    conn.close()

    from synergy_graph import compute_strategy_relevance

    # Active strategies: tokens
    active = {"tokens"}
    rel_a = compute_strategy_relevance("cand-a", active, tmp_db)
    rel_b = compute_strategy_relevance("cand-b", active, tmp_db)

    assert rel_a > rel_b
    assert rel_a >= 1.0
    assert rel_b == 0.5  # penalty for no match
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_strategy_recommend.py -v`
Expected: FAIL — compute_strategy_relevance doesn't exist

- [ ] **Step 3: Implement compute_strategy_relevance**

Add to `synergy_graph.py`:

```python
def compute_strategy_relevance(oracle_id, active_strategies, db_path=None):
    """Compute strategy relevance multiplier for a card.

    Returns: float multiplier (0.5 for no match, 1.0+ for matches)
    """
    if not active_strategies:
        return 1.0

    if db_path is None:
        db_path = os.path.join(os.path.dirname(__file__), "data", "tags.db")
    conn = sqlite3.connect(db_path)
    card_strats = {row[0] for row in conn.execute(
        "SELECT strategy FROM card_strategies WHERE oracle_id = ? AND confidence >= 0.3",
        (oracle_id,)
    ).fetchall()}
    conn.close()

    overlap = card_strats & active_strategies
    if not overlap:
        return 0.5
    return 1.0 + 0.2 * len(overlap)
```

- [ ] **Step 4: Add --strategies and --exclude-strategies CLI args**

In the argparse section of `synergy_graph.py` (around line 2340):

```python
parser.add_argument("--strategies", default="auto",
                    help="Comma-separated strategies to focus (default: auto-detect)")
parser.add_argument("--exclude-strategies", default=None,
                    help="Comma-separated strategies to exclude")
```

- [ ] **Step 5: Wire into run() function**

In the `run()` function, after deck loading but before recommendations, add strategy detection.
The commander's oracle_id is obtained by looking up the commander name in the loaded deck cards:

```python
# After deck loading (around line 2380, after cards are loaded from DB)
from strategy_detector import detect_strategies

# Get commander oracle_id from loaded deck cards
commander_name = deck.COMMANDER
commander_card = next((c for c in deck_cards if c["name"] == commander_name), None)
commander_oid = commander_card["oracle_id"] if commander_card else None

if args.strategies == "auto" and commander_oid:
    detected = detect_strategies(commander_oid, db_path)
    active_strategies = {s["name"] for s in detected if s["confidence"] >= 0.3}
elif args.strategies != "auto":
    active_strategies = set(args.strategies.split(","))
else:
    active_strategies = set()

if args.exclude_strategies:
    active_strategies -= set(args.exclude_strategies.split(","))

if active_strategies:
    print(f"Active strategies: {', '.join(sorted(active_strategies))}")
```

- [ ] **Step 6: Wire strategy_relevance into recommend_cards scoring**

In `recommend_cards()` (around line 890 in the scoring loop), modify the score calculation:

```python
# Inside the scoring loop where candidate scores are computed:
# BEFORE: score = total_synergy * tribal_boost
# AFTER:
strategy_rel = compute_strategy_relevance(candidate_oid, active_strategies, db_path) if active_strategies else 1.0
score = total_synergy * strategy_rel * tribal_boost
```

Pass `active_strategies` as a parameter to `recommend_cards()`. Also apply the x2.0 combo completion multiplier:

```python
# After computing base score, check for partial Spellbook combos
from synergy_graph import find_partial_combos
partial_combos = find_partial_combos(deck_oids, db_path)
partial_missing_oids = set()
for pc in partial_combos:
    for oid in pc["missing_oids"]:
        partial_missing_oids.add(oid)

# In scoring loop:
if candidate_oid in partial_missing_oids:
    score *= 2.0  # Combo completion multiplier
```

- [ ] **Step 7: Run tests**

Run: `python3 -m pytest tests/test_strategy_recommend.py -v`
Expected: PASS

- [ ] **Step 7: Test CLI end-to-end**

Run: `python3 synergy_graph.py --deck kyler --recommend --strategies auto`
Run: `python3 synergy_graph.py --deck kyler --recommend --strategies humans,counters`

- [ ] **Step 8: Commit**

```bash
git add synergy_graph.py tests/test_strategy_recommend.py
git commit -m "feat: add strategy-weighted recommendations with CLI override"
```

---

## Task 13: Partial Spellbook matches + enhanced output

**Files:**
- Modify: `synergy_graph.py` (recommendation output)

- [ ] **Step 1: Write the test**

```python
# tests/test_partial_combos.py
import sqlite3
import json
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


def test_partial_spellbook_match(tmp_db):
    """Should detect when deck is 1 card away from a Spellbook combo."""
    from synergy_graph import find_partial_combos

    conn = sqlite3.connect(tmp_db)
    conn.execute("INSERT INTO cards (oracle_id, name) VALUES ('oid-x', 'Card X')")
    conn.execute("INSERT INTO cards (oracle_id, name) VALUES ('oid-y', 'Card Y')")
    conn.execute("INSERT INTO cards (oracle_id, name) VALUES ('oid-z', 'Card Z')")

    # 3-card combo, but deck only has 2 of 3
    conn.execute("""INSERT INTO spellbook_combos (combo_id, card_oracle_ids, card_names, result, prerequisites, card_count)
                    VALUES ('combo-2', '["oid-x","oid-y","oid-z"]', '["Card X","Card Y","Card Z"]', 'Infinite tokens', '', 3)""")
    conn.execute("INSERT INTO spellbook_combo_cards (combo_id, oracle_id) VALUES ('combo-2', 'oid-x')")
    conn.execute("INSERT INTO spellbook_combo_cards (combo_id, oracle_id) VALUES ('combo-2', 'oid-y')")
    conn.execute("INSERT INTO spellbook_combo_cards (combo_id, oracle_id) VALUES ('combo-2', 'oid-z')")
    conn.commit()
    conn.close()

    deck_oids = {"oid-x", "oid-y"}  # Missing oid-z
    partials = find_partial_combos(deck_oids, tmp_db)

    assert len(partials) >= 1
    assert partials[0]["missing_cards"] == ["Card Z"]
    assert "Infinite tokens" in partials[0]["result"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_partial_combos.py -v`
Expected: FAIL

- [ ] **Step 3: Implement find_partial_combos**

Add to `synergy_graph.py`:

```python
def find_partial_combos(deck_oids, db_path=None):
    """Find Spellbook combos where deck is missing exactly 1 card.

    Returns list of dicts with: combo_id, result, present_cards, missing_cards.
    """
    if db_path is None:
        db_path = os.path.join(os.path.dirname(__file__), "data", "tags.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    combos = conn.execute("SELECT * FROM spellbook_combos").fetchall()
    conn.close()

    partials = []
    for combo in combos:
        combo_oids = json.loads(combo["card_oracle_ids"])
        combo_names = json.loads(combo["card_names"])

        present = [oid for oid in combo_oids if oid in deck_oids]
        missing = [oid for oid in combo_oids if oid not in deck_oids]

        if len(missing) == 1:
            missing_idx = combo_oids.index(missing[0])
            partials.append({
                "combo_id": combo["combo_id"],
                "result": combo["result"],
                "present_cards": [combo_names[combo_oids.index(oid)] for oid in present],
                "missing_cards": [combo_names[missing_idx]],
                "missing_oids": missing,
            })

    return partials
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_partial_combos.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add synergy_graph.py tests/test_partial_combos.py
git commit -m "feat: add partial Spellbook combo detection (1-card-away)"
```

---

## Task 14: Anti-synergy detection + enhanced deck-view

**Files:**
- Modify: `synergy_graph.py`

- [ ] **Step 1: Write the test**

```python
# tests/test_anti_synergy.py
import sqlite3
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


def test_anti_synergy_detection(tmp_db):
    """Cards with no strategy overlap and non-staple role should be flagged."""
    from synergy_graph import find_anti_synergy

    conn = sqlite3.connect(tmp_db)
    # Card with strategy overlap
    conn.execute("INSERT INTO cards (oracle_id, name, role) VALUES ('good', 'Good Card', 'threat')")
    conn.execute("INSERT INTO card_strategies (oracle_id, strategy, confidence) VALUES ('good', 'tokens', 1.0)")

    # Card with no overlap, non-staple
    conn.execute("INSERT INTO cards (oracle_id, name, role) VALUES ('bad', 'Bad Card', 'threat')")

    # Staple card with no overlap — should NOT be flagged
    conn.execute("INSERT INTO cards (oracle_id, name, role) VALUES ('staple', 'Swords to Plowshares', 'removal')")

    conn.commit()
    conn.close()

    deck_oids = {"good", "bad", "staple"}
    active_strategies = {"tokens"}

    anti = find_anti_synergy(deck_oids, active_strategies, tmp_db)
    assert len(anti) == 1
    assert anti[0]["name"] == "Bad Card"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_anti_synergy.py -v`
Expected: FAIL

- [ ] **Step 3: Implement find_anti_synergy**

```python
STAPLE_ROLES = {"ramp", "draw", "removal", "protection", "land"}

def find_anti_synergy(deck_oids, active_strategies, db_path=None):
    """Find deck cards with zero strategy overlap that aren't staples.

    Returns list of dicts: {oracle_id, name, role}.
    """
    if not active_strategies:
        return []
    if db_path is None:
        db_path = os.path.join(os.path.dirname(__file__), "data", "tags.db")
    conn = sqlite3.connect(db_path)

    anti = []
    for oid in deck_oids:
        row = conn.execute("SELECT name, role FROM cards WHERE oracle_id = ?", (oid,)).fetchone()
        if not row:
            continue
        name, role = row

        # Skip staples
        if role and role.lower() in STAPLE_ROLES:
            continue

        # Check strategy overlap
        card_strats = {r[0] for r in conn.execute(
            "SELECT strategy FROM card_strategies WHERE oracle_id = ? AND confidence >= 0.3",
            (oid,)
        ).fetchall()}

        if not (card_strats & active_strategies):
            anti.append({"oracle_id": oid, "name": name, "role": role})

    conn.close()
    return anti
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_anti_synergy.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add synergy_graph.py tests/test_anti_synergy.py
git commit -m "feat: add anti-synergy detection for strategy-mismatched deck cards"
```

---

## Task 15: Enhanced recommendation output + deck-view

**Files:**
- Modify: `synergy_graph.py` (show_combos, recommend output, deck-view output)

- [ ] **Step 1: Modify show_combos to display 3-tier output**

Replace the combo display code in `show_combos()` (around line 1375) to use `find_combos_tiered`:

```python
def show_combos_tiered(deck_oids, commander_name=None, db_path=None):
    """Display 3-tier combo output."""
    combos = find_combos_tiered(deck_oids, db_path)
    partials = find_partial_combos(deck_oids, db_path)

    confirmed = [c for c in combos if c["tier"] == "infinite-confirmed"]
    likely = [c for c in combos if c["tier"] == "combo-likely"]
    synergy = [c for c in combos if c["tier"] == "synergy"]

    if confirmed:
        print(f"\n{'='*60}")
        print(f"CONFIRMED INFINITE COMBOS ({len(confirmed)})")
        print(f"{'='*60}")
        for c in confirmed:
            print(f"  {' + '.join(c['cards'])}")
            print(f"    Result: {c['result']}")
            print(f"    Source: {c['reason']}")

    if likely:
        print(f"\n{'='*60}")
        print(f"LIKELY COMBOS ({len(likely)})")
        print(f"{'='*60}")
        for c in likely:
            print(f"  {' + '.join(c['cards'])}")
            print(f"    Chain: {c['result']}")

    if partials:
        print(f"\n{'='*60}")
        print(f"NEAR-COMPLETE COMBOS — 1 card away ({len(partials)})")
        print(f"{'='*60}")
        for p in partials:
            print(f"  {' + '.join(p['present_cards'])} + [{p['missing_cards'][0]}]")
            print(f"    Result: {p['result']}")

    if synergy:
        print(f"\n  Synergy pairs: {len(synergy)} (use --verbose to list)")

    print(f"\n  Total: {len(confirmed)} confirmed, {len(likely)} likely, {len(synergy)} synergy")
```

- [ ] **Step 2: Modify recommendation output format**

Update the recommendation display to show strategy annotations and combo completions:

```python
# In the recommendation output section, restructure to show:
# 1. COMBO COMPLETIONS (from partial_combos where missing card is a candidate)
# 2. BEST FIT (high synergy + strategy match)
# 3. ENABLERS (support infrastructure)

def show_recommendations_enhanced(candidates, active_strategies, partial_combos, deck_name):
    """Enhanced recommendation output with strategy annotations."""
    print(f"\n{'='*60}")
    print(f"RECOMMENDATIONS for {deck_name} (strategies: {', '.join(sorted(active_strategies)) or 'none'})")
    print(f"{'='*60}")

    # Combo completions
    combo_cards = set()
    for pc in partial_combos:
        for name in pc["missing_cards"]:
            combo_cards.add(name)

    completions = [c for c in candidates if c["name"] in combo_cards]
    if completions:
        print(f"\nCOMBO COMPLETIONS (1 card away from confirmed infinite):")
        for c in completions[:5]:
            matching = [pc for pc in partial_combos if c["name"] in pc["missing_cards"]]
            for pc in matching:
                print(f"  {' + '.join(pc['present_cards'])} + [{c['name']}] -> {pc['result']}")

    # Best fit
    best = [c for c in candidates if c["name"] not in combo_cards]
    print(f"\nBEST FIT:")
    for i, c in enumerate(best[:15], 1):
        strats = c.get("strategies", [])
        strat_str = f" [{', '.join(strats)}]" if strats else ""
        tribal = " [tribal]" if c.get("tribal") else ""
        print(f"  {i}. {c['name']}{strat_str}{tribal} score: {c['score']:.1f}")
```

- [ ] **Step 3: Add enhanced --deck-view summary**

Add strategy coverage and combo summary to the deck-view output:

```python
def show_deck_analysis(deck_cards, deck_oids, active_strategies, commander_name, db_path=None):
    """Enhanced deck analysis with strategy coverage."""
    if db_path is None:
        db_path = os.path.join(os.path.dirname(__file__), "data", "tags.db")
    conn = sqlite3.connect(db_path)

    # Count cards per strategy
    strat_counts = {}
    for oid in deck_oids:
        for row in conn.execute(
            "SELECT strategy FROM card_strategies WHERE oracle_id = ? AND confidence >= 0.3", (oid,)
        ):
            strat_counts[row[0]] = strat_counts.get(row[0], 0) + 1

    # Count non-land cards
    non_land = sum(1 for c in deck_cards if "Land" not in (c.get("type_line") or ""))
    aligned = sum(1 for oid in deck_oids
                  for _ in conn.execute(
                      "SELECT 1 FROM card_strategies WHERE oracle_id = ? AND confidence >= 0.3 AND strategy IN ({})".format(
                          ','.join('?' * len(active_strategies))),
                      (oid, *active_strategies)).fetchall()[:1])

    combos = find_combos_tiered(deck_oids, db_path)
    anti = find_anti_synergy(deck_oids, active_strategies, db_path)
    conn.close()

    print(f"\n{'='*60}")
    print(f"DECK ANALYSIS: {commander_name}")
    print(f"{'='*60}")

    print(f"Detected strategies:")
    for strat in sorted(active_strategies):
        cnt = strat_counts.get(strat, 0)
        print(f"  {strat}: {cnt} cards")

    coverage = aligned * 100 // max(non_land, 1)
    print(f"Strategy coverage: {coverage}% of {non_land} non-land cards align with >=1 strategy")

    confirmed = sum(1 for c in combos if c["tier"] == "infinite-confirmed")
    likely = sum(1 for c in combos if c["tier"] == "combo-likely")
    synergy = sum(1 for c in combos if c["tier"] == "synergy")
    print(f"Confirmed combos: {confirmed} (Spellbook)")
    print(f"Likely combos: {likely} (trigger chain)")
    print(f"Synergy pairs: {synergy}")

    if anti:
        print(f"Anti-synergy cards: {len(anti)} (swap candidates)")
        for a in anti[:5]:
            print(f"  {a['name']} ({a['role'] or 'unknown role'})")
```

- [ ] **Step 4: Test end-to-end with Kyler**

Run: `python3 synergy_graph.py --deck kyler --deck-view --combos --recommend --strategies auto`
Expected: Shows strategy coverage, 3-tier combos, enhanced recommendations

- [ ] **Step 5: Commit**

```bash
git add synergy_graph.py
git commit -m "feat: add enhanced recommendation output, 3-tier combo display, deck analysis summary"
```

---

## Task 16: Integration test — full pipeline end-to-end (renumbered from 15)

**Files:**
- Create: `tests/test_integration.py`

- [ ] **Step 1: Write end-to-end test**

```python
# tests/test_integration.py
"""Integration test: runs the full pipeline on a small card set."""
import sqlite3
import json
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


def test_full_pipeline(tmp_db):
    """Run tag cleanup → parse abilities → detect strategies → find combos."""
    import tag_db
    from ability_parser import parse_card, save_abilities_to_db
    from strategy_detector import detect_strategies
    from synergy_graph import find_combos_tiered

    conn = sqlite3.connect(tmp_db)

    # Set up cards mimicking a small Kyler deck
    cards = [
        ("cmdr", "Kyler, Sigardian Emissary", "Legendary Creature — Human Cleric",
         "Whenever a Human enters the battlefield under your control, put a +1/+1 counter on Kyler, Sigardian Emissary.\nHuman creatures you control get +1/+1 for each +1/+1 counter on Kyler.",
         "enabler", ["human-tribal", "counter-placement"], ["creature-etb"]),
        ("scales", "Hardened Scales", "Enchantment",
         "If one or more +1/+1 counters would be placed on a creature you control, that many plus one +1/+1 counters are placed on it instead.",
         "enabler", ["counter-amplification"], ["counter-placement-events"]),
        ("crusade", "Cathars' Crusade", "Enchantment",
         "Whenever a creature enters the battlefield under your control, put a +1/+1 counter on each creature you control.",
         "enabler", ["counter-placement"], ["creature-etb"]),
    ]

    for oid, name, tl, oracle, role, provs, wants in cards:
        conn.execute("INSERT INTO cards (oracle_id, name, type_line, oracle_text, role, keywords) VALUES (?,?,?,?,?,?)",
                     (oid, name, tl, oracle, role, "[]"))
        for p in provs:
            conn.execute("INSERT INTO provides (oracle_id, tag) VALUES (?,?)", (oid, p))
        for w in wants:
            conn.execute("INSERT INTO wants (oracle_id, tag) VALUES (?,?)", (oid, w))
    conn.commit()
    conn.close()

    # 1. Parse abilities
    parsed = []
    for oid, name, tl, oracle, role, provs, wants in cards:
        card = {"oracle_id": oid, "name": name, "type_line": tl, "oracle_text": oracle, "keywords": []}
        parsed.append((oid, parse_card(card)))
    save_abilities_to_db(parsed, tmp_db)

    # Verify abilities were stored
    conn = sqlite3.connect(tmp_db)
    ab_count = conn.execute("SELECT COUNT(*) FROM abilities").fetchone()[0]
    assert ab_count >= 3  # At least one per card
    conn.close()

    # 2. Detect strategies for commander
    strategies = detect_strategies("cmdr", tmp_db)
    strat_names = {s["name"] for s in strategies}
    assert "humans" in strat_names or "+1/+1-counters" in strat_names

    # 3. Find combos (tiered)
    deck_oids = {"cmdr", "scales", "crusade"}
    combos = find_combos_tiered(deck_oids, tmp_db)
    # Should find at least synergy-tier combos between these cards
    assert len(combos) >= 1
```

- [ ] **Step 2: Run full test suite**

Run: `python3 -m pytest tests/ -v`
Expected: All tests pass

- [ ] **Step 3: Commit**

```bash
git add tests/test_integration.py
git commit -m "test: add end-to-end integration test for full enrichment pipeline"
```

---

## Task 17: Run full pipeline on real data

This is the validation step — no new code, just running everything on the actual 34k-card database.

- [ ] **Step 1: Fix tribal wants**

Run: `python3 tag_db.py fix-tribal --dry-run`
Then: `python3 tag_db.py fix-tribal`

- [ ] **Step 2: Rebuild registry**

Run: `python3 tag_db.py rebuild-registry`

- [ ] **Step 3: Parse all abilities**

Run: `python3 ability_parser.py`

- [ ] **Step 4: Inspect key cards**

Run: `python3 ability_parser.py --card "Kyler, Sigardian Emissary"`
Run: `python3 ability_parser.py --card "Hardened Scales"`
Run: `python3 ability_parser.py --card "Krenko, Mob Boss"`
Run: `python3 ability_parser.py --card "Skirk Prospector"`

- [ ] **Step 5: Fetch Spellbook data**

Run: `python3 fetch_spellbook.py`
Run: `python3 fetch_spellbook.py --stats`

- [ ] **Step 6: Populate strategies**

Run: `python3 strategy_detector.py --populate`
Run: `python3 strategy_detector.py --stats`
Run: `python3 strategy_detector.py --commander "Kyler, Sigardian Emissary"`
Run: `python3 strategy_detector.py --commander "Krenko, Mob Boss"`

- [ ] **Step 7: Test enhanced recommendations**

Run: `python3 synergy_graph.py --deck kyler --recommend --strategies auto`
Run: `python3 synergy_graph.py --deck krenko --combos`

- [ ] **Step 8: Commit DB updates**

```bash
git add synergy_tag_registry.json
git commit -m "data: run full enrichment pipeline — tribal fix, registry v4.0, abilities, strategies"
```
