# Tag Sub-categorization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split 6 generic tags into 20 sub-tags and re-enable the tag overlap tiebreaker in recommendations.

**Architecture:** New `reclassify_tags.py` script queries cards with generic tags, sends them through OpenAI Batch API for reclassification, and updates the DB. Then update all downstream consumers (constants, parsers, strategy detector, tests) to use sub-tag names. Finally enable the overlap tiebreaker in the scoring formula.

**Tech Stack:** Python 3, SQLite, OpenAI Batch API (gpt-4.1-mini), pytest

**Spec:** `docs/superpowers/specs/2026-03-23-tag-subcategorization-design.md`

---

## File Map

| Action | File | Responsibility |
|--------|------|---------------|
| Create | `reclassify_tags.py` | Reclassification script: query generic tags, build LLM prompts, submit batch, update DB |
| Create | `tests/test_reclassify_tags.py` | Tests for reclassification prompt building + result parsing |
| Modify | `tag_registry.json` | Remove 6 parent tags, add 20 sub-tags with definitions + aliases |
| Modify | `mtg_synergy/constants.py` | Update ~70 SEMANTIC_BRIDGES + TRIGGER_EFFECT_BRIDGES entries |
| Modify | `normalize_tags.py` | Update inference rules to use sub-tags instead of parent tags |
| Modify | `ability_parser.py` | Update all keyword→tag and regex→tag mappings |
| Modify | `strategy_detector.py` | Update strategy rules to use sub-tag sets |
| Modify | `mtg_synergy/recommend/swaps.py` | Update SYNERGY_PROVIDES set |
| Modify | `mtg_synergy/recommend/engine.py` | Enable overlap tiebreaker with board-generic exclusion |
| Modify | `mtg_synergy/graph/edges.py` | Update SKIP_WANTS from `creature-board` to `board-generic` |
| Modify | `mtg_synergy/analysis/strategy.py` | Update comment referencing `token-generation` |
| Modify | `prompt_builder.py` | Update example tags in LLM prompt to use sub-tag names |
| Modify | `tag_db.py` | Update CLI help text examples |
| Modify | `tests/test_constants.py` | Update bridge assertions |
| Modify | `tests/test_combo_tiers.py` | Update tag references in test data |
| Modify | `tests/test_ability_parser.py` | Update tag assertions |
| Modify | `tests/test_strategy_recommend.py` | Update `token-generation` INSERT to sub-tag |
| Modify | `tests/test_strategy_detector.py` | Update tag references |

---

### Task 1: Record Baseline EDHREC Scores

**Files:**
- None (data collection only)

- [ ] **Step 1: Run baseline comparison**

Run: `python3 compare_edhrec.py --fast --quiet`

Save output to `data/baseline_edhrec_before_retag.txt` for later comparison.

- [ ] **Step 2: Commit baseline**

```bash
git add data/baseline_edhrec_before_retag.txt
git commit -m "data: record baseline EDHREC scores before tag subcategorization"
```

---

### Task 2: Update tag_registry.json — Replace 6 Parent Tags With 20 Sub-tags

**Files:**
- Modify: `tag_registry.json`

- [ ] **Step 1: Write test for new registry structure**

Create `tests/test_tag_registry_subtags.py`:

```python
"""Verify tag_registry.json has sub-tags and no parent tags."""
import json
import pytest

PARENT_TAGS = [
    "creature-pump", "creature-board", "creature-etb",
    "combat-events", "token-generation", "evasion-grant",
]

SUB_TAGS = {
    "pump-lord", "pump-anthem", "pump-combat", "pump-self",
    "board-tokens", "board-tribal", "board-go-wide", "board-generic",
    "etb-value", "etb-tokens", "etb-tribal",
    "combat-attack", "combat-damage", "combat-block",
    "tokens-creature", "tokens-artifact", "tokens-tribal",
    "evasion-flying", "evasion-unblockable", "evasion-menace",
}

@pytest.fixture
def registry():
    with open("tag_registry.json") as f:
        return json.load(f)

def test_no_parent_tags(registry):
    for tag in PARENT_TAGS:
        assert tag not in registry["tags"], f"Parent tag {tag} should be removed"

def test_all_subtags_present(registry):
    for tag in SUB_TAGS:
        assert tag in registry["tags"], f"Sub-tag {tag} missing"

def test_subtags_have_definitions(registry):
    for tag in SUB_TAGS:
        entry = registry["tags"][tag]
        assert entry.get("definition"), f"Sub-tag {tag} missing definition"
        assert entry.get("kind") in ("provides", "wants"), f"Sub-tag {tag} missing kind"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_tag_registry_subtags.py -v`
Expected: FAIL — parent tags still present, sub-tags missing.

- [ ] **Step 3: Update tag_registry.json**

Remove the 6 parent tag entries and add 20 sub-tag entries. For each sub-tag:
- Set `kind` to the parent's kind (provides or wants)
- Set `definition` from the spec tables
- Redistribute aliases from the parent to the best-fit sub-tag

Provides sub-tags (from `creature-pump`):
- `pump-lord`: kind=provides, aliases from parent that reference tribal/type buffs (e.g., `tribal-boost`, `creature-type-boost`, `creature-type-buff`, `human-pump`, `vampire-pump`, `ally-triggered-pump`, `anthem-effect-for-humans`)
- `pump-anthem`: kind=provides, aliases for board-wide buffs (e.g., `board-wide-buff`, `team boost`, `power-amplification`)
- `pump-combat`: kind=provides, aliases for temporary buffs (e.g., `combat-pump`, `temporary-pump`, `pump-spell`, `combat-power-boost`, `temporary-power-boost`, `temporary-power-increase`)
- `pump-self`: kind=provides, aliases for self-growth (e.g., `self-buff`, `self-growth`, `self-pump`, `power-scaling`, `power-increment`, `prowess-trigger`)

Provides sub-tags (from `token-generation`):
- `tokens-creature`: kind=provides, aliases for generic creature tokens (e.g., `creature-token-creation`, `creature-token-generation`, `mass-token-generation`, `token-swarm`, `extra-attackers`, `creature-conjuration`, `creature-generation`, `creature-copy`, `self-copying`, `token-copying`, `game-ending-token`)
- `tokens-artifact`: kind=provides, aliases for non-creature tokens (e.g., `treasure-token-creation`, `treasure-token-generation`, `treasure-token-generator`, `clue-token-production`, `food-token-creation`, `food-token-generation`, `artifact-token-creation`, `artifact-token-generation`, `enchantment-token-generation`, `mutagen-token-creation`, `mutagen-token-generation`)
- `tokens-tribal`: kind=provides, aliases for typed creature tokens (e.g., `goblin-token`, `goblin-token-generation`, `goblin-summoning`, `goblin-permanent-placement`, `goblin-placement`, `zombie-token-creation`, `vampire-token-generation`, `dragon-token-creation`, `dragon-token-generation`, `giant-token-creation`, `human-token-creation`, `saproling-token-generation`, `spirit-trigger`, `wizard-token-creation`, `survivor-token-generation`, `camarid-token-production`)

Provides sub-tags (from `evasion-grant`):
- `evasion-flying`: kind=provides, aliases (e.g., `flying`, `flying option`, `flying-granter`, `temporary-flying`)
- `evasion-unblockable`: kind=provides, aliases (e.g., `unblockable`, `unblockable-attack`, `unblockable-creature`, `unblockable-granter`, `blockage-prevention`, `creature-block-prevention`, `power-based evasion`, `unblockable by weaker creatures`, `unblockable-against-1-power`)
- `evasion-menace`: kind=provides, aliases (e.g., `menace`, `menace-granter`, `fear`, `mountainwalk-grant`, `evasion-for-countered-creatures`)

Wants sub-tags (from `creature-board`):
- `board-tokens`: kind=wants, aliases (e.g., `token-events` — note: only if not already a separate tag)
- `board-tribal`: kind=wants, aliases (e.g., `creature-type-selection`, `creature-diversity`)
- `board-go-wide`: kind=wants, aliases (e.g., `wide-board`, `wide-board strategies`, `wide-board-strategies`, `creature-swarm`, `creature-population`, `creature-count`, `creature-multiplication`, `creature-count-disparity`)
- `board-generic`: kind=wants, aliases (e.g., `creature-presence`, `creature-heavy decks`, `creature-heavy-decks`, `creature-heavy strategies`, `creature-heavy-board`, `creature-control`, `creatures`, `creature-synergies`, `creature-synergy`, `creature-based strategies`, `creature-based-strategies`, all other creature-board aliases not assigned above)

Wants sub-tags (from `creature-etb`):
- `etb-value`: kind=wants, aliases (e.g., `creature-enters`, `creature-enters-battlefield`, `creature-etb-events`, `creature-etb-trigger`, `creature-etb-triggers`, `enter-the-battlefield-trigger`, `creature-entries`, `creature-entry`, `creature-entry-events`)
- `etb-tokens`: kind=wants, aliases (e.g., `nonland-permanent-entry`, `creature-placement-events`)
- `etb-tribal`: kind=wants, aliases (e.g., `ally-enters`, `ally-entry-events`, `artifact-creature-entry`, `board-wide-human-placement`, `human-enter-events`, `family-creature-placement`, `green-creature-entries`)

Wants sub-tags (from `combat-events`):
- `combat-attack`: kind=wants, aliases (e.g., `combat-attacks`, `combat-initiation`, `combat-start`, `combat-start-events`, `combat-start-trigger`, `combat-phase`, `combat-phase-activation`, `combat-readiness`)
- `combat-damage`: kind=wants, aliases (e.g., `combat-damage`, `combat-damage-deal`, `combat-damage-dealt`, `combat-damage-events`, `combat-damage-occurrence`, `combat-damage-sources`, `creature-combat-damage`, `creature-damage`, `creature-damage-events`, `damage-dealing-events`, `damage-dealing-trigger`, `damage-dealt`, `damage-dealt-events`, `damage-events`, `opponent-damage`, `opponent-life-loss`, `excess-damage`, `excess-damage-events`)
- `combat-block`: kind=wants, aliases (e.g., `blocking-creatures`, `creature-block-events`, `creature-blocking`, `creature-blocks`, `combat-protection`, `first-strike`, `regeneration-need`, `regeneration-utility`)

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_tag_registry_subtags.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tag_registry.json tests/test_tag_registry_subtags.py
git commit -m "feat: replace 6 generic tags with 20 sub-tags in tag registry"
```

---

### Task 3: Create reclassify_tags.py — Reclassification Script

**Files:**
- Create: `reclassify_tags.py`
- Create: `tests/test_reclassify_tags.py`

- [ ] **Step 1: Write tests for prompt building and result parsing**

Create `tests/test_reclassify_tags.py`:

```python
"""Tests for tag reclassification pipeline."""
import json
import pytest


def test_build_reclassify_prompt_creature_pump():
    from reclassify_tags import build_reclassify_prompt
    cards = [
        {"name": "Goblin King", "oracle_text": "Other Goblins get +1/+1 and mountainwalk.", "type_line": "Creature — Goblin"},
    ]
    prompt = build_reclassify_prompt("creature-pump", cards)
    assert "pump-lord" in prompt
    assert "pump-anthem" in prompt
    assert "pump-combat" in prompt
    assert "pump-self" in prompt
    assert "Goblin King" in prompt


def test_build_reclassify_prompt_creature_board():
    from reclassify_tags import build_reclassify_prompt
    cards = [
        {"name": "Craterhoof Behemoth", "oracle_text": "When Craterhoof Behemoth enters, creatures you control gain trample and get +X/+X until end of turn, where X is the number of creatures you control.", "type_line": "Creature — Beast"},
    ]
    prompt = build_reclassify_prompt("creature-board", cards)
    assert "board-tokens" in prompt
    assert "board-tribal" in prompt
    assert "board-go-wide" in prompt
    assert "board-generic" in prompt


def test_parse_reclassify_results():
    from reclassify_tags import parse_reclassify_results
    raw = json.dumps({"cards": [
        {"name": "Goblin King", "sub_tag": "pump-lord"},
        {"name": "Glorious Anthem", "sub_tag": "pump-anthem"},
    ]})
    results = parse_reclassify_results(raw, "creature-pump", 2)
    assert results == [
        {"name": "Goblin King", "sub_tag": "pump-lord"},
        {"name": "Glorious Anthem", "sub_tag": "pump-anthem"},
    ]


def test_parse_reclassify_rejects_invalid_subtag():
    from reclassify_tags import parse_reclassify_results
    raw = json.dumps({"cards": [
        {"name": "Goblin King", "sub_tag": "pump-lord"},
        {"name": "Bad Card", "sub_tag": "invalid-tag"},
    ]})
    results = parse_reclassify_results(raw, "creature-pump", 2)
    # Invalid sub_tag should be replaced with fallback
    assert results[1]["sub_tag"] in ("pump-combat", "pump-self", "pump-anthem", "pump-lord")  # any valid sub-tag
    # Actually, invalid should get the generic/default fallback
    # For creature-pump there is no -generic, so let's just check it's valid


VALID_SUBTAGS = {
    "creature-pump": {"pump-lord", "pump-anthem", "pump-combat", "pump-self"},
    "creature-board": {"board-tokens", "board-tribal", "board-go-wide", "board-generic"},
    "creature-etb": {"etb-value", "etb-tokens", "etb-tribal"},
    "combat-events": {"combat-attack", "combat-damage", "combat-block"},
    "token-generation": {"tokens-creature", "tokens-artifact", "tokens-tribal"},
    "evasion-grant": {"evasion-flying", "evasion-unblockable", "evasion-menace"},
}


def test_valid_subtags_complete():
    """All parent tags have defined valid sub-tags."""
    from reclassify_tags import SUBTAG_MAP
    for parent, expected in VALID_SUBTAGS.items():
        assert set(SUBTAG_MAP[parent].keys()) == expected, f"Mismatch for {parent}"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_reclassify_tags.py -v`
Expected: FAIL — `reclassify_tags` module doesn't exist.

- [ ] **Step 3: Implement reclassify_tags.py**

Create `reclassify_tags.py` with:

1. **`SUBTAG_MAP`** — dict mapping each parent tag to its sub-tags with definitions:
```python
SUBTAG_MAP = {
    "creature-pump": {
        "pump-lord": "Permanently buffs creatures of a specific type",
        "pump-anthem": "Permanently buffs all/most creatures regardless of type",
        "pump-combat": "Temporary combat buffs, voltron equipment/auras",
        "pump-self": "Card grows itself via counters or scaling",
    },
    "creature-board": {
        "board-tokens": "Payoff for having token creatures specifically",
        "board-tribal": "Payoff for having creatures of a specific type on board",
        "board-go-wide": "Payoff for having many creatures (count matters)",
        "board-generic": "Needs creatures on board but not count/type dependent",
    },
    "creature-etb": {
        "etb-value": "Triggers on any creature entering for value (draw, removal, ramp)",
        "etb-tokens": "Triggers on creature entry to make tokens or deal damage",
        "etb-tribal": "Triggers on specific creature type entering",
    },
    "combat-events": {
        "combat-attack": "Triggers on attacking or beginning of combat",
        "combat-damage": "Triggers on dealing combat damage",
        "combat-block": "Triggers on blocking or cares about being blocked",
    },
    "token-generation": {
        "tokens-creature": "Creates creature tokens",
        "tokens-artifact": "Creates treasure, clue, food, or other artifact tokens",
        "tokens-tribal": "Creates tokens of a specific creature type",
    },
    "evasion-grant": {
        "evasion-flying": "Grants or has flying",
        "evasion-unblockable": "Makes creatures unblockable",
        "evasion-menace": "Grants menace, fear, intimidate, or similar partial evasion",
    },
}
```

Which table each parent tag lives in:
```python
TAG_TABLE = {
    "creature-pump": "provides",
    "token-generation": "provides",
    "evasion-grant": "provides",
    "creature-board": "wants",
    "creature-etb": "wants",
    "combat-events": "wants",
}
```

2. **`build_reclassify_prompt(parent_tag, cards)`** — builds a system+user prompt for reclassification:
   - System prompt lists the sub-categories with definitions
   - Instructs: "Choose the single most specific sub-category. If the card fits multiple, pick the narrower one."
   - User prompt lists cards as `Name | Type Line | Oracle Text`
   - Asks for JSON response: `{"cards": [{"name": "...", "sub_tag": "..."}]}`

3. **`parse_reclassify_results(raw, parent_tag, expected_count)`** — parses LLM JSON response:
   - Handles `{"cards": [...]}` wrapper
   - Validates each `sub_tag` is in `SUBTAG_MAP[parent_tag]`
   - Invalid sub_tags get the first/default sub-tag for that parent (e.g., `pump-combat` for creature-pump, `board-generic` for creature-board)

4. **`get_cards_with_tag(conn, parent_tag)`** — queries DB for all cards with the parent tag:
```python
def get_cards_with_tag(conn, parent_tag):
    table = TAG_TABLE[parent_tag]
    rows = conn.execute(f"""
        SELECT p.oracle_id, c.name, c.type_line, c.oracle_text
        FROM {table} p JOIN cards c ON p.oracle_id = c.oracle_id
        WHERE p.tag = ?
    """, (parent_tag,)).fetchall()
    return [{"oracle_id": r[0], "name": r[1], "type_line": r[2] or "", "oracle_text": r[3] or ""} for r in rows]
```

5. **`apply_reclassification(conn, parent_tag, results, oracle_id_lookup)`** — updates DB:
```python
def apply_reclassification(conn, parent_tag, results, oracle_id_lookup):
    table = TAG_TABLE[parent_tag]
    for card in results:
        oid = oracle_id_lookup.get(card["name"])
        if not oid:
            continue
        conn.execute(f"DELETE FROM {table} WHERE oracle_id = ? AND tag = ?", (oid, parent_tag))
        conn.execute(f"INSERT OR IGNORE INTO {table} (oracle_id, tag) VALUES (?, ?)", (oid, card["sub_tag"]))
    conn.commit()
```

6. **`run_batch_api(...)`** — follows same pattern as `batch_extract.py`:
   - Build JSONL with reclassification prompts (50 cards per request)
   - Upload file to OpenAI
   - Create batch job
   - Poll until completion
   - Download results, parse, apply to DB

7. **CLI with argparse:**
   - `--parent-tag TAG` — reclassify one parent tag
   - `--all` — reclassify all 6 parent tags
   - `--dry-run` — show what would be reclassified without changing DB
   - `--test N` — only process first N cards (for testing)
   - `--model MODEL` — OpenAI model (default: gpt-4.1-mini)

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_reclassify_tags.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add reclassify_tags.py tests/test_reclassify_tags.py
git commit -m "feat: add reclassify_tags.py for tag sub-categorization via Batch API"
```

---

### Task 4: Update SEMANTIC_BRIDGES in constants.py

**Files:**
- Modify: `mtg_synergy/constants.py`
- Modify: `tests/test_constants.py`

- [ ] **Step 1: Write test for updated bridges**

Update `tests/test_constants.py` to assert:
- No bridge references any of the 6 parent tags (as either provides or wants)
- Key sub-tag bridges exist (e.g., `("tokens-creature", "etb-value")`, `("pump-lord", "board-tribal")`)

```python
REMOVED_PARENT_TAGS = {
    "creature-pump", "creature-board", "creature-etb",
    "combat-events", "token-generation", "evasion-grant",
}

def test_no_parent_tags_in_bridges():
    from mtg_synergy.constants import SEMANTIC_BRIDGES
    for (p, w), weight in SEMANTIC_BRIDGES.items():
        assert p not in REMOVED_PARENT_TAGS, f"Provides '{p}' is a removed parent tag"
        assert w not in REMOVED_PARENT_TAGS, f"Wants '{w}' is a removed parent tag"

def test_key_subtag_bridges_exist():
    from mtg_synergy.constants import SEMANTIC_BRIDGES
    assert ("tokens-creature", "etb-value") in SEMANTIC_BRIDGES
    assert ("tokens-tribal", "etb-tribal") in SEMANTIC_BRIDGES
    assert ("pump-lord", "board-tribal") in SEMANTIC_BRIDGES
    assert ("pump-anthem", "board-go-wide") in SEMANTIC_BRIDGES
    assert ("tokens-creature", "board-tokens") in SEMANTIC_BRIDGES
    assert ("combat-enabler", "combat-attack") in SEMANTIC_BRIDGES
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_constants.py -v`
Expected: FAIL — parent tags still in bridges.

- [ ] **Step 3: Update SEMANTIC_BRIDGES**

For each of the ~70 bridge entries referencing the 6 parent tags, apply these remapping rules:

**Provides-side remapping** (the provides tag IS one of our 6):
- `token-generation` → Split into `tokens-creature` and/or `tokens-tribal` based on the wants tag:
  - `→ creature-etb` becomes `tokens-creature → etb-value` (0.9) + `tokens-tribal → etb-tribal` (0.95)
  - `→ creature-board` becomes `tokens-creature → board-tokens` (0.95) + `tokens-creature → board-go-wide` (0.8)
  - `→ sacrifice-events` becomes `tokens-creature → sacrifice-events` (0.8)
  - `→ creature-death` becomes `tokens-creature → creature-death` (0.6)
  - `→ goblin-tribal` becomes `tokens-tribal → goblin-tribal` (0.7)
  - `→ sacrifice-fodder` becomes `tokens-creature → sacrifice-fodder` (0.9)
  - `→ creature-death-payoff` becomes `tokens-creature → creature-death-payoff` (0.7)
  - `→ creature-etb-payoff` becomes `tokens-creature → creature-etb-payoff` (0.9)
  - `→ creature-count-matters` becomes `tokens-creature → creature-count-matters` (0.9)
  - `→ combat-attackers` becomes `tokens-creature → combat-attackers` (0.6)
  - `→ sacrifice-outlet` becomes `tokens-creature → sacrifice-outlet` (0.6)
- `creature-pump` → Split based on context:
  - `→ creature-power` becomes `pump-anthem → creature-power` (0.8) + `pump-lord → creature-power` (0.8)
  - `→ attack-events` becomes `pump-anthem → attack-events` (0.5) + `pump-combat → attack-events` (0.5)
  - `→ sacrifice-fodder` becomes `pump-self → sacrifice-fodder` (0.3)
  - `→ creature-power-matters` becomes `pump-anthem → creature-power-matters` (0.9)
  - `→ combat-attackers` becomes `pump-anthem → combat-attackers` (0.7) + `pump-combat → combat-attackers` (0.7)
- `evasion-grant` → Split based on context:
  - `→ attack-events` becomes `evasion-unblockable → attack-events` (0.6) + `evasion-flying → attack-events` (0.5)
  - `→ combat-events` — this is also a parent; remap both sides (e.g., `evasion-unblockable → combat-attack` (0.6))
  - `→ counter-placement-events` becomes `evasion-flying → counter-placement-events` (0.5)
  - `→ combat-attackers` becomes `evasion-unblockable → combat-attackers` (0.8) + `evasion-flying → combat-attackers` (0.6)
  - `→ mana-needs` — remove (weak bridge, 0.3)
  - `→ triggered-abilities` — remove (too generic, 0.4)
  - `→ life-gain-events` — remove (too generic, 0.4)

**Wants-side remapping** (the wants tag IS one of our 6):
- `→ creature-etb` becomes `→ etb-value` (1:1 replacement, same weight)
- `→ creature-board` becomes `→ board-go-wide` for most, or `→ board-generic` for weak connections:
  - `board-protection → creature-board` becomes `board-protection → board-go-wide` (0.5)
  - `etb-payoff → creature-board` becomes `etb-payoff → board-go-wide` (0.5)
  - `counter-placement → creature-board` becomes `counter-placement → board-go-wide` (0.4)
  - `board-wide-counter-placement → creature-board` becomes `board-wide-counter-placement → board-go-wide` (0.5)
  - `infect → creature-board` becomes `infect → board-go-wide` (0.4)
- `→ combat-events` becomes `→ combat-attack` for most:
  - `combat-enabler → combat-events` becomes `combat-enabler → combat-attack` (0.7)
  - `combat-trigger → combat-events` becomes `combat-trigger → combat-attack` (0.8)
  - `damage-dealing → combat-events` becomes `damage-dealing → combat-damage` (0.6)

- [ ] **Step 4: Update TRIGGER_EFFECT_BRIDGES**

Remap entries at lines 566 and 600:
```python
# Old:
"token-generation": {"creature-etb"},
"creature-pump": {"attack-events"},

# New:
"tokens-creature": {"etb-value", "etb-tokens"},
"tokens-tribal": {"etb-tribal"},
"tokens-artifact": set(),  # artifact tokens don't trigger ETB payoffs
"pump-lord": {"attack-events"},
"pump-anthem": {"attack-events"},
"pump-combat": {"attack-events"},
```

- [ ] **Step 5: Run tests**

Run: `python3 -m pytest tests/test_constants.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add mtg_synergy/constants.py tests/test_constants.py
git commit -m "feat: update SEMANTIC_BRIDGES and TRIGGER_EFFECT_BRIDGES for sub-tags"
```

---

### Task 5: Update normalize_tags.py Inference Rules

**Files:**
- Modify: `normalize_tags.py`

- [ ] **Step 1: Write test**

Add to existing test file or create `tests/test_normalize_subtags.py`:

```python
def test_infer_wants_uses_subtags():
    from normalize_tags import infer_wants
    # Card with pump-lord should infer board-tribal, not creature-board
    provides = {"pump-lord"}
    wants = set()
    inferred = infer_wants(provides, wants)
    assert "creature-board" not in inferred
    assert "board-tribal" in inferred or len(inferred) == 0  # Either infers sub-tag or nothing

def test_infer_wants_tokens_creature():
    from normalize_tags import infer_wants
    provides = {"tokens-creature"}
    wants = set()
    inferred = infer_wants(provides, wants)
    assert "creature-etb" not in inferred
    # Should infer etb-value (creating creature tokens triggers ETB)
    assert "etb-value" in inferred
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_normalize_subtags.py -v`
Expected: FAIL

- [ ] **Step 3: Update inference rules in normalize_tags.py**

At lines 108-116 (ETB inference):
```python
# Old:
etb_signals = [
    "token-generation" in provides,
    "etb-payoff" in provides,
    ...
]
if any(etb_signals) and "creature-etb" not in wants:
    inferred.append("creature-etb")

# New:
if "tokens-creature" in provides and "etb-value" not in wants:
    inferred.append("etb-value")
if "tokens-tribal" in provides and "etb-tribal" not in wants:
    inferred.append("etb-tribal")
if "etb-payoff" in provides and "etb-value" not in wants:
    inferred.append("etb-value")
```

At lines 129-136 (board inference):
```python
# Old:
pump_signals = [
    "creature-pump" in provides,
    "board-wide-counter-placement" in provides,
    ...
]
if any(pump_signals) and "creature-board" not in wants:
    inferred.append("creature-board")

# New:
if "pump-lord" in provides and "board-tribal" not in wants:
    inferred.append("board-tribal")
if "pump-anthem" in provides and "board-go-wide" not in wants:
    inferred.append("board-go-wide")
if "board-wide-counter-placement" in provides and "board-go-wide" not in wants:
    inferred.append("board-go-wide")
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_normalize_subtags.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add normalize_tags.py tests/test_normalize_subtags.py
git commit -m "feat: update normalize_tags inference rules for sub-tags"
```

---

### Task 6: Update ability_parser.py Tag Mappings

**Files:**
- Modify: `ability_parser.py`
- Modify: `tests/test_ability_parser.py`

- [ ] **Step 1: Update test assertions**

In `tests/test_ability_parser.py`, replace all assertions checking for parent tags with sub-tag equivalents:
- `"token-generation"` → `"tokens-creature"` (for generic token keywords like fabricate, embalm, populate)
- `"token-generation"` → `"tokens-tribal"` (for tribal-specific keywords if any)
- `"creature-pump"` → `"pump-self"` (for prowess)
- `"creature-pump"` → `"pump-anthem"` (for battle cry)
- `"creature-etb"` → `"etb-value"` (for generic ETB triggers)

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_ability_parser.py -v`
Expected: FAIL

- [ ] **Step 3: Update KEYWORD_EFFECT_TAGS**

Replace all parent tag string literals in `ability_parser.py`:

```python
# Lines 112-117: Token-creating keywords
"fabricate": ["tokens-creature", "counter-placement"],
"embalm": ["tokens-creature"],
"eternalize": ["tokens-creature"],
"amass": ["tokens-creature", "counter-placement"],
"populate": ["tokens-creature"],
"crew": ["tokens-creature"],

# Lines 144-146:
"encore": ["graveyard-recursion", "tokens-creature"],
"embalm": ["graveyard-recursion", "tokens-creature"],
"eternalize": ["graveyard-recursion", "tokens-creature"],

# Line 175:
"living weapon": ["equipment-synergy", "tokens-creature"],

# Line 185:
"prowess": ["spell-cast-payoff", "pump-self"],

# Line 188:
"battle cry": ["pump-anthem", "attack-events"],

# Line 235:
"afterlife": ["tokens-creature", "creature-death"],

# Line 239:
"decayed": ["tokens-creature", "sacrifice-trigger"],

# Line 242-243:
"squad": ["tokens-creature"],
"enlist": ["attack-events", "pump-combat"],

# Line 246:
"for mirrodin!": ["equipment-synergy", "tokens-creature"],

# Line 250:
"offspring": ["tokens-creature"],
```

Update EFFECT_TAG_PATTERNS:
```python
# Line 460:
(re.compile(r'create.*token', re.I), "tokens-creature"),

# Line 479:
(re.compile(r'get[s]?\s+[+\-]\d+/[+\-]\d+', re.I), "pump-combat"),
```

Update TRIGGER_TAG_PATTERNS:
```python
# Line 495:
(re.compile(r'creature.*enters|enters the battlefield', re.I), "etb-value"),
```

Note: The regex-based patterns use the default/most common sub-tag. The LLM reclassification handles the more nuanced cases.

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_ability_parser.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add ability_parser.py tests/test_ability_parser.py
git commit -m "feat: update ability_parser tag mappings to use sub-tags"
```

---

### Task 7: Update strategy_detector.py

**Files:**
- Modify: `strategy_detector.py`
- Modify: `tests/test_strategy_detector.py`

- [ ] **Step 1: Update test tag references**

In `tests/test_strategy_detector.py`, update all INSERT statements and assertions to use sub-tags:
- `token-generation` → `tokens-creature`
- `creature-pump` → `pump-anthem`
- `evasion-grant` → `evasion-unblockable`
- `creature-etb` → `etb-value`
- `creature-board` → `board-go-wide`

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_strategy_detector.py -v`
Expected: FAIL

- [ ] **Step 3: Update strategy rules**

```python
# Line 23 — old:
({"token-generation"}, "tokens", 1.0),
# New:
({"tokens-creature", "tokens-tribal"}, "tokens", 1.0),

# Line 66 — old:
({"trample-grant", "evasion-grant"}, "voltron", 0.5),
# New:
({"trample-grant", "evasion-flying", "evasion-unblockable", "evasion-menace"}, "voltron", 0.5),

# Line 108 — old:
({"board-wide-creature-pump", "creature-pump"}, "go-wide", 0.7),
# New:
({"board-wide-creature-pump", "pump-anthem", "pump-lord"}, "go-wide", 0.7),
```

Update ABILITY_STRATEGY_MAP:
```python
# Line 296 — old:
"token-generation": ("tokens", 0.9),
# New:
"tokens-creature": ("tokens", 0.9),
"tokens-tribal": ("tokens", 0.9),
"tokens-artifact": ("tokens", 0.6),
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_strategy_detector.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add strategy_detector.py tests/test_strategy_detector.py
git commit -m "feat: update strategy_detector rules for sub-tags"
```

---

### Task 8: Update swaps.py and edges.py

**Files:**
- Modify: `mtg_synergy/recommend/swaps.py`
- Modify: `mtg_synergy/graph/edges.py`

- [ ] **Step 1: Write test asserting no parent tags in SYNERGY_PROVIDES**

Add to existing swap tests or create inline:
```python
def test_no_parent_tags_in_synergy_provides():
    from mtg_synergy.recommend.swaps import SYNERGY_PROVIDES
    parent_tags = {"creature-pump", "token-generation", "evasion-grant"}
    assert not parent_tags & SYNERGY_PROVIDES, f"Parent tags found: {parent_tags & SYNERGY_PROVIDES}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/ -k "parent_tags_in_synergy" -v`
Expected: FAIL

- [ ] **Step 3: Update SYNERGY_PROVIDES**

```python
# Old (lines 28-33):
SYNERGY_PROVIDES = {
    "token-generation",
    "creature-pump",
    ...
}

# New:
SYNERGY_PROVIDES = {
    "tokens-creature", "tokens-artifact", "tokens-tribal",
    "pump-lord", "pump-anthem", "pump-combat", "pump-self",
    ...  # keep all other entries unchanged
}
```

- [ ] **Step 4: Update SKIP_WANTS in edges.py**

At `mtg_synergy/graph/edges.py` line 180:
```python
# Old:
SKIP_WANTS = {"creature-board", "mana-needs"}

# New:
SKIP_WANTS = {"board-generic", "mana-needs"}
```

- [ ] **Step 5: Run tests**

Run: `python3 -m pytest tests/ -k "swap or edge" -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add mtg_synergy/recommend/swaps.py mtg_synergy/graph/edges.py
git commit -m "feat: update swaps SYNERGY_PROVIDES and edges SKIP_WANTS for sub-tags"
```

---

### Task 9: Update Remaining Files and Tests

**Files:**
- Modify: `tests/test_combo_tiers.py`
- Modify: `tests/test_integration.py`
- Modify: `tests/test_registry_rebuild.py`
- Modify: `tests/test_strategy_recommend.py` (line 13: `token-generation` → `tokens-creature`)
- Modify: `mtg_synergy/analysis/strategy.py` (line 204: update comment)
- Modify: `prompt_builder.py` (lines 21, 59: update example tags in LLM prompt to use sub-tag names like `etb-value` instead of `creature-etb-triggers`)
- Modify: `tag_db.py` (lines 11-12: update CLI help text examples)
- Modify: any other files referencing parent tags

- [ ] **Step 1: Find all remaining parent tag references**

Run: `grep -rn "creature-pump\|creature-board\|creature-etb\|combat-events\|token-generation\|evasion-grant" tests/ mtg_synergy/ prompt_builder.py tag_db.py normalize_tags.py`

**WARNING:** The pattern `creature-etb` will also match `creature-etb-payoff` and `creature-etb-triggers`, which are SEPARATE tags NOT being split. Only replace exact matches of the 6 parent tag names. Do NOT rename `creature-etb-payoff`, `creature-etb-triggers`, or similar compound tags.

- [ ] **Step 2: Update each file**

Replace parent tag string literals with appropriate sub-tags:
- In test data INSERTs: use the most common sub-tag for that parent
- In assertions: match what the code now produces
- In comments: update to reference sub-tags
- In `prompt_builder.py`: update example tags so the LLM learns sub-tag naming convention
- In `tag_db.py`: update help text examples

- [ ] **Step 3: Run full test suite**

Run: `python3 -m pytest tests/ -v`
Expected: ALL PASS

- [ ] **Step 4: Commit**

```bash
git add tests/ mtg_synergy/analysis/strategy.py prompt_builder.py tag_db.py
git commit -m "feat: update remaining files and tests for sub-tag references"
```

---

### Task 10: Verify No Regression (Tiebreaker Still Off)

**Files:**
- None (validation only)

**Note:** At this point the code uses sub-tags but the DB still has parent tags (reclassification happens in Task 11). This is expected — tests use fixtures, not the live DB. The EDHREC comparison runs against the live DB, so scores may shift slightly because SEMANTIC_BRIDGES changed. Do NOT re-run `ability_parser.py` or `normalize_tags.py` against existing cards until after Task 11 completes the reclassification.

- [ ] **Step 1: Run full test suite**

Run: `python3 -m pytest tests/ -v`
Expected: ALL PASS

- [ ] **Step 2: Run EDHREC comparison with tiebreaker still off**

Run: `python3 compare_edhrec.py --fast --quiet`

Compare output with `data/baseline_edhrec_before_retag.txt`. Scores may shift by ±1-2 due to bridge changes. Large regressions (>3 points) indicate a bridge remapping error — investigate before proceeding.

- [ ] **Step 3: Save post-reclassification baseline**

Save output to `data/baseline_edhrec_after_retag.txt`.

- [ ] **Step 4: Commit**

```bash
git add data/baseline_edhrec_after_retag.txt
git commit -m "data: record EDHREC scores after tag reclassification (tiebreaker off)"
```

---

### Task 11: Run Reclassification via Batch API

**Files:**
- None (runs `reclassify_tags.py` against DB)

- [ ] **Step 1: Back up the database**

```bash
cp data/tags.db data/tags.db.backup-before-reclassify
```

If reclassification produces poor results, restore with `cp data/tags.db.backup-before-reclassify data/tags.db`.

- [ ] **Step 2: Dry run to check counts**

Run: `python3 reclassify_tags.py --all --dry-run`

Verify it reports ~40k tags to reclassify across 6 parent tags.

- [ ] **Step 3: Test with small sample**

Run: `python3 reclassify_tags.py --parent-tag creature-pump --test 10`

Check output: 10 cards classified into pump-lord/pump-anthem/pump-combat/pump-self. Verify results make sense.

- [ ] **Step 4: Run full reclassification**

Run: `python3 reclassify_tags.py --all`

This submits to OpenAI Batch API. Wait for completion (~1-4 hours). Cost: ~$1.50-2.50.

- [ ] **Step 5: Verify DB state**

```bash
python3 -c "
import sqlite3
conn = sqlite3.connect('data/tags.db')
parents = ['creature-pump','creature-board','creature-etb','combat-events','token-generation','evasion-grant']
for p in parents:
    for table in ['provides', 'wants']:
        cnt = conn.execute(f'SELECT COUNT(*) FROM {table} WHERE tag = ?', (p,)).fetchone()[0]
        if cnt > 0:
            print(f'WARNING: {cnt} rows still have {p} in {table}')
print('Subtag distribution:')
for table in ['provides', 'wants']:
    rows = conn.execute(f'SELECT tag, COUNT(*) FROM {table} GROUP BY tag ORDER BY COUNT(*) DESC LIMIT 30').fetchall()
    print(f'\\n{table}:')
    for tag, cnt in rows:
        print(f'  {tag}: {cnt}')
"
```

Verify: zero parent tags remain, sub-tags have reasonable distribution.

- [ ] **Step 6: Commit DB backup note**

No DB committed (too large), but record the reclassification event:
```bash
git commit --allow-empty -m "data: completed tag reclassification via Batch API (~40k tags)"
```

---

### Task 12: Enable Overlap Tiebreaker in engine.py

**Files:**
- Modify: `mtg_synergy/recommend/engine.py`

- [ ] **Step 1: Write test**

Add to `tests/test_recommend_engine.py` or create new:

```python
from mtg_synergy.recommend.engine import OVERLAP_EXCLUDE

def test_overlap_exclude_contains_board_generic():
    """board-generic must be in the exclusion set."""
    assert "board-generic" in OVERLAP_EXCLUDE

def test_overlap_exclude_does_not_contain_specific_subtags():
    """Specific sub-tags should NOT be excluded."""
    for tag in ("board-tokens", "board-tribal", "board-go-wide",
                "pump-lord", "tokens-creature", "etb-value"):
        assert tag not in OVERLAP_EXCLUDE, f"{tag} should not be excluded"
```

- [ ] **Step 2: Update engine.py scoring formula**

At line 490 (current):
```python
info["total"] = score_val * 1000.0 + tower_score * 10.0 + rank_tiebreak * 0.1
```

Change to:
```python
# Filter out board-generic from overlap (too common to be discriminative)
OVERLAP_EXCLUDE = {"board-generic"}
```

In the overlap computation section (around line 459), filter excluded tags:
```python
# Old:
cmdr_tag_overlap[card_name] = len(cp & cmdr_wants) + len(cw & cmdr_provides)

# New:
filtered_cp = cp - OVERLAP_EXCLUDE
filtered_cw = cw - OVERLAP_EXCLUDE
filtered_cmdr_wants = cmdr_wants - OVERLAP_EXCLUDE
filtered_cmdr_provides = cmdr_provides - OVERLAP_EXCLUDE
cmdr_tag_overlap[card_name] = len(filtered_cp & filtered_cmdr_wants) + len(filtered_cw & filtered_cmdr_provides)
```

Then in the scoring formula:
```python
# Old:
info["total"] = score_val * 1000.0 + tower_score * 10.0 + rank_tiebreak * 0.1

# New:
info["total"] = score_val * 1000.0 + overlap * 20.0 + tower_score * 10.0 + rank_tiebreak * 0.1
```

- [ ] **Step 3: Run tests**

Run: `python3 -m pytest tests/ -v`
Expected: ALL PASS

- [ ] **Step 4: Commit**

```bash
git add mtg_synergy/recommend/engine.py
git commit -m "feat: enable tag overlap tiebreaker with board-generic exclusion"
```

---

### Task 13: End-to-End Validation

**Files:**
- None (validation only)

- [ ] **Step 1: Run EDHREC comparison with tiebreaker enabled**

Run: `python3 compare_edhrec.py --fast --quiet`

- [ ] **Step 2: Compare against baselines**

Compare with `data/baseline_edhrec_before_retag.txt` and `data/baseline_edhrec_after_retag.txt`.

**Success criteria:**
- No deck loses more than 2 points vs before-retag baseline
- Average improves from 13.4/30
- Sram and Edgar don't regress

- [ ] **Step 3: If regression detected**

If any deck regresses >2 points:
1. Run `python3 compare_edhrec.py --deck <name> --fast` to see which cards changed
2. Check if `board-generic` exclusion needs to be expanded
3. Consider reducing `× 20` weight or adding IDF weighting
4. Re-run validation after tuning

- [ ] **Step 4: Save final results and commit**

```bash
python3 compare_edhrec.py --fast --quiet > data/baseline_edhrec_with_tiebreaker.txt
git add data/baseline_edhrec_with_tiebreaker.txt
git commit -m "data: record EDHREC scores with overlap tiebreaker enabled"
```

---

### Task 14: Update CLAUDE.md

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Update tag schema section**

Add sub-tag information to the Tag Schema section. Mention the 6→20 split and that parent tags no longer exist.

- [ ] **Step 2: Update DB schema table**

Update the provides/wants row counts if they changed.

- [ ] **Step 3: Update recommendation pipeline section**

Note that overlap tiebreaker is now active with `× 20` weight and `board-generic` exclusion.

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: update CLAUDE.md for tag sub-categorization"
```
