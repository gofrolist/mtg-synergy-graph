# Scoring Model Improvement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate the 2^N port extraction bug, extract unused Forge DSL fields, drop hand-tuned scoring weights, and add curated-hint rules — in three staged phases each with its own NDCG acceptance gate.

**Architecture:** Phase A fixes the data layer (ports.py, importer.py, schema.sql); Phase C re-derives scoring weights from cleaned data (universal_scorer.py); Phase B adds a new curated-hint rule tier on top (complement_rules/hints.py). Each phase is an independent commit set with rollback.

**Tech Stack:** Python 3.13, sqlite3, pytest, uv. Project is 32k cards × 184k ports imported from Forge DSL card files.

**Reference spec:** [docs/superpowers/specs/2026-04-16-scoring-model-improvement-design.md](../specs/2026-04-16-scoring-model-improvement-design.md)

**Baseline (before Phase A):** NDCG@30 = 0.19397, card_ports row count = 184,106.

---

## File Structure

| Phase | File | Action | Responsibility |
|---|---|---|---|
| A1 | `src/mtg_synergy_graph/ports.py` | Modify | Fix `extract_effect_ports` exponential re-walk |
| A1 | `tests/test_ports.py` | Modify | Add regression tests for the bug |
| A2 | `src/mtg_synergy_graph/ports.py` | Modify | Emit ChangeType attributes on ChangeZone effects |
| A2 | `src/mtg_synergy_graph/importer.py` | Modify | Insert change_type port_attributes rows |
| A2 | `tests/test_importer.py` | Modify | Assert ChangeType rows populated for Kaalia |
| A3 | `src/mtg_synergy_graph/ports.py` | Modify | Parse TokenScript, emit token_color / token_subtype attrs |
| A3 | `src/mtg_synergy_graph/importer.py` | Modify | Insert token_* port_attributes rows |
| A4 | `src/mtg_synergy_graph/schema.sql` | Modify | Add `card_hints` table and index |
| A4 | `src/mtg_synergy_graph/importer.py` | Modify | Populate card_hints from deck_needs/hints/has JSON |
| A4 | `tests/test_importer.py` | Modify | Assert card_hints rows populated |
| A5 | `src/mtg_synergy_graph/importer.py` | Modify | Populate card_hints.kind='buffed_by' from BuffedBy SVars |
| A6 | — | Run | `scripts/import_cardsfolder.py` + golden-set NDCG |
| C1 | `src/mtg_synergy_graph/universal_scorer.py` | Modify | Remove `_FLAT_WEIGHT_OVERRIDES`, `_FLAT_COUNT_RULES` |
| C2 | `src/mtg_synergy_graph/universal_scorer.py` | Modify | Add data-derived ceiling for broad-filter rules |
| C3 | `src/mtg_synergy_graph/universal_scorer.py` | Modify | Audit `_RULE_QUALITY_MULTIPLIER` entries |
| B1 | `src/mtg_synergy_graph/complement_rules/hints.py` | Create | `deck_hint_match` rule |
| B2 | `src/mtg_synergy_graph/complement_rules/hints.py` | Modify | `deck_needs_fulfilled` rule |
| B3 | `src/mtg_synergy_graph/complement_rules/hints.py` | Modify | `buffed_by_match` rule |
| B | `src/mtg_synergy_graph/complement_rules/__init__.py` | Modify | Export hint rule finder |
| B | `src/mtg_synergy_graph/complement_rules/core.py` | Modify | Dispatch hint finders in `find_all_complements` |
| B | `src/mtg_synergy_graph/universal_scorer.py` | Modify | Add rule_ids to `_RULE_TO_BUCKET` + pair bonuses |
| B | `tests/test_hint_rules.py` | Create | Rule-firing tests |

---

## Phase A — Data-layer cleanup

### Task A1: Fix exponential SubAbility re-walk

**Files:**
- Modify: `src/mtg_synergy_graph/ports.py` (function `extract_effect_ports`, lines 235-366)
- Test: `tests/test_ports.py` (add new tests after the existing Scute Swarm tests)

**Root cause:** `extract_effect_ports` calls `walk_svar_chain(ref_name, ...)` for every SubAbility key, then recursively calls `extract_effect_ports(card_name, sub_node, svars)` on each returned node — which ALSO walks its own SubAbility chain. For a linear N-deep SVar chain this emits `2^N - 1` ports. Akroma, Vision of Ixidor produces 16,389 ports; correct count is 14.

**Fix approach:** When `extract_effect_ports` receives a `ChainNode` (the only caller pattern from chain-walking contexts), skip the CHAIN_KEYS re-walk — `walk_svar_chain` already flattened the entire chain into siblings. Keep the GenericChoice expansion because `walk_svar_chain` does NOT follow `Choices$` (only CHAIN_KEYS).

- [ ] **Step A1.1: Write failing test for Akroma port count**

Add to `tests/test_ports.py` (after the existing tests):

```python
from pathlib import Path
from mtg_synergy_graph import extract_all_ports, parse_card_file

FORGE_CARDSFOLDER = Path(__file__).parent.parent / "data" / "forge" / "forge-gui" / "res" / "cardsfolder"


def test_akroma_vision_of_ixidor_does_not_explode():
    """Regression: extract_effect_ports used to emit 2^N ports for an N-deep
    SubAbility chain. Akroma has a 14-deep linear chain (Flying → FirstStrike
    → ... → Partner); correct port count is 14 PumpAll effects + 1 trigger +
    4 keyword ports = 19 total, not 16,389."""
    card_path = FORGE_CARDSFOLDER / "a" / "akroma_vision_of_ixidor.txt"
    card = parse_card_file(card_path)
    ports = extract_all_ports(card)

    pump_all_ports = [p for p in ports if p["port_type"] == "effect" and p["event_class"] == "PumpAll"]
    assert len(pump_all_ports) == 14, (
        f"Akroma should emit exactly 14 PumpAll ports (one per keyword in the chain), "
        f"got {len(pump_all_ports)}"
    )
    # Total ports: 1 trigger + 14 effects + 4 keywords = 19
    assert len(ports) < 50, f"Akroma total port count should be small; got {len(ports)}"


def test_nature_demands_an_offering_does_not_explode():
    """Regression: another exponential-chain card. Expected < 30 ports."""
    card_path = FORGE_CARDSFOLDER / "n" / "nature_demands_an_offering.txt"
    card = parse_card_file(card_path)
    ports = extract_all_ports(card)
    assert len(ports) < 30, f"Nature Demands an Offering port count exploded: {len(ports)}"


def test_largepox_does_not_explode():
    """Regression: another exponential-chain card. Expected < 30 ports."""
    card_path = FORGE_CARDSFOLDER / "l" / "largepox.txt"
    card = parse_card_file(card_path)
    ports = extract_all_ports(card)
    assert len(ports) < 30, f"Largepox port count exploded: {len(ports)}"
```

- [ ] **Step A1.2: Run tests to verify they fail**

Run: `uv run pytest tests/test_ports.py::test_akroma_vision_of_ixidor_does_not_explode tests/test_ports.py::test_nature_demands_an_offering_does_not_explode tests/test_ports.py::test_largepox_does_not_explode -v`

Expected: all 3 FAIL with counts around 16389, 16384, 5632.

- [ ] **Step A1.3: Apply the fix in `extract_effect_ports`**

Edit `src/mtg_synergy_graph/ports.py`. In `extract_effect_ports`, around lines 305-318, the current code is:

```python
    sub_ports: list[PortRow] = []
    for key, child_branch_kind in CHAIN_KEYS.items():
        ref_name = parsed.get(key)
        if not ref_name:
            continue
        chain = walk_svar_chain(
            ref_name,
            svars,
            branch_kind=child_branch_kind,
            branch_parent=source_svar,
            chain_depth=chain_depth + 1,
        )
        for sub_node in chain:
            sub_ports.extend(extract_effect_ports(card_name, sub_node, svars))
```

Replace with:

```python
    sub_ports: list[PortRow] = []
    # When invoked with a ChainNode, walk_svar_chain has already flattened
    # the entire SubAbility tree — re-walking here causes 2^N port explosion
    # (see test_akroma_vision_of_ixidor_does_not_explode). Only the
    # top-level raw-dict entry path walks the chain.
    is_chain_node = isinstance(parsed_or_node, ChainNode)
    if not is_chain_node:
        for key, child_branch_kind in CHAIN_KEYS.items():
            ref_name = parsed.get(key)
            if not ref_name:
                continue
            chain = walk_svar_chain(
                ref_name,
                svars,
                branch_kind=child_branch_kind,
                branch_parent=source_svar,
                chain_depth=chain_depth + 1,
            )
            for sub_node in chain:
                sub_ports.extend(extract_effect_ports(card_name, sub_node, svars))
```

**Note:** GenericChoice expansion (lines 320-336 currently) must still run for BOTH paths because `walk_svar_chain` does NOT follow `Choices$` — only CHAIN_KEYS. Leave that block unchanged.

- [ ] **Step A1.4: Run all port tests to verify fix + no regressions**

Run: `uv run pytest tests/test_ports.py tests/test_chain_walker.py -v`

Expected: all new tests PASS + all existing tests PASS (in particular `test_korvold_sacrifice_chain_emits_putcounter_and_draw` must still assert `draw["chain_depth"] == 2`).

- [ ] **Step A1.5: Run full test suite**

Run: `uv run pytest tests/ -v`

Expected: all 212+ tests PASS (new total should be 215).

- [ ] **Step A1.6: Commit**

```bash
git add src/mtg_synergy_graph/ports.py tests/test_ports.py
git commit -m "fix: prevent exponential SubAbility re-walk in extract_effect_ports

extract_effect_ports was calling walk_svar_chain for each SubAbility key,
then recursively calling itself on every returned node — which also walked
its own SubAbility chain. For a linear N-deep chain this emitted 2^N-1
ports. Akroma, Vision of Ixidor produced 16,389 ports; correct count is 14.

When the input is a ChainNode, walk_svar_chain has already flattened the
full tree, so skip the re-walk. GenericChoice expansion is preserved
because walk_svar_chain doesn't follow Choices\$.

Affects ~88 cards contributing ~31% of all card_ports rows."
```

---

### Task A2: Extract `ChangeType` into `port_attributes`

**Files:**
- Modify: `src/mtg_synergy_graph/ports.py` (function `extract_effect_ports`)
- Modify: `src/mtg_synergy_graph/importer.py` (around lines 436-445)
- Test: `tests/test_importer.py` (add new test)

**Context:** ChangeZone effect ports currently store `ChangeType$ Creature.Angel+YouCtrl,Creature.Demon+YouCtrl,Creature.Dragon+YouCtrl` only in `raw_line`. We want this explodable into `port_attributes` with `attr_kind='change_type'` (using existing `explode_filter` logic per comma-separated clause), so queries like "commander cheats Dragons into play" work.

- [ ] **Step A2.1: Add a helper for ChangeType attributes on the port dict**

Edit `src/mtg_synergy_graph/ports.py`. In `extract_effect_ports`, just before the `port: PortRow = {...}` dict literal (around line 278), add ChangeType capture to the port row:

Find the existing port dict construction:

```python
    port: PortRow = {
        "card_name": card_name,
        "port_type": "effect",
        "event_class": verb,
        "valid_filter": parsed.get("ValidTgts") or parsed.get("Defined") or parsed.get("ValidCards", ""),
        ...
```

Replace with:

```python
    # ChangeZone effects carry their type-scope in ChangeType$, separate
    # from ValidTgts/Defined. Store it on the port so the importer can
    # explode it into port_attributes under attr_kind='change_type'.
    change_type = parsed.get("ChangeType", "") if verb == "ChangeZone" else ""

    port: PortRow = {
        "card_name": card_name,
        "port_type": "effect",
        "event_class": verb,
        "valid_filter": parsed.get("ValidTgts") or parsed.get("Defined") or parsed.get("ValidCards", ""),
        ...
        "raw_line": repr(parsed),
        "_change_type": change_type,  # consumed by importer, stripped before insert
        **branch,
    }
```

Place `"_change_type": change_type,` immediately after the existing `"raw_line": repr(parsed),` line and before `**branch,`.

- [ ] **Step A2.2: Strip `_change_type` in the importer before insert; emit attrs**

Edit `src/mtg_synergy_graph/importer.py`. Find the port-insert loop around lines 436-445:

```python
    ports = extract_all_ports(card)
    inserted = 0
    for port in ports:
        cur = conn.execute(_PORT_INSERT_SQL, _normalise_port(port))
        port_id = cur.lastrowid
        inserted += 1
        for attr in explode_filter(port.get("valid_filter") or ""):
            conn.execute(
                "INSERT OR IGNORE INTO port_attributes "
                "(port_id, attr_kind, attr_value, is_negated) VALUES (?, ?, ?, ?)",
                (port_id, attr["attr_kind"], attr["attr_value"], attr["is_negated"]),
            )
```

Replace with:

```python
    ports = extract_all_ports(card)
    inserted = 0
    for port in ports:
        change_type = port.pop("_change_type", "") if "_change_type" in port else ""
        cur = conn.execute(_PORT_INSERT_SQL, _normalise_port(port))
        port_id = cur.lastrowid
        inserted += 1
        for attr in explode_filter(port.get("valid_filter") or ""):
            conn.execute(
                "INSERT OR IGNORE INTO port_attributes "
                "(port_id, attr_kind, attr_value, is_negated) VALUES (?, ?, ?, ?)",
                (port_id, attr["attr_kind"], attr["attr_value"], attr["is_negated"]),
            )
        # ChangeType is a comma-separated list of filter clauses; explode
        # each clause with the shared filter parser and re-tag as change_type.
        if change_type:
            for clause in change_type.split(","):
                for attr in explode_filter(clause):
                    # Only keep the semantically-interesting kinds (types
                    # and subtypes). Controller/color/cmc_cmp are already
                    # covered via valid_filter or orthogonal dimensions.
                    if attr["attr_kind"] in ("type", "subtype"):
                        conn.execute(
                            "INSERT OR IGNORE INTO port_attributes "
                            "(port_id, attr_kind, attr_value, is_negated) VALUES (?, ?, ?, ?)",
                            (port_id, "change_type", attr["attr_value"], attr["is_negated"]),
                        )
```

- [ ] **Step A2.3: Write test for Kaalia ChangeType extraction**

Add to `tests/test_importer.py` (or create `tests/test_change_type_attrs.py` if the file doesn't exist — check first with `ls tests/test_importer.py`):

```python
def test_change_type_attributes_populated_for_kaalia(tmp_path):
    """Kaalia of the Vast cheats Angel/Demon/Dragon into play via ChangeType.
    Those subtypes must land in port_attributes with attr_kind='change_type'.
    """
    from mtg_synergy_graph.importer import open_db, upsert_card
    from mtg_synergy_graph import parse_card_file
    from pathlib import Path

    db_path = tmp_path / "test.db"
    conn = open_db(db_path)
    card_path = Path(__file__).parent.parent / "data" / "forge" / "forge-gui" / "res" / "cardsfolder" / "k" / "kaalia_of_the_vast.txt"
    card = parse_card_file(card_path)
    upsert_card(conn, card, oracle_id_resolver=None)

    rows = conn.execute(
        "SELECT attr_kind, attr_value FROM port_attributes "
        "WHERE attr_kind='change_type' "
        "AND port_id IN (SELECT id FROM card_ports WHERE card_name=?)",
        ("Kaalia of the Vast",),
    ).fetchall()
    values = {r[1] for r in rows}
    assert {"Angel", "Demon", "Dragon"} <= values, f"Expected Angel/Demon/Dragon in change_type attrs, got {values}"
    conn.close()
```

If `open_db` or `upsert_card` aren't the exact names, check `src/mtg_synergy_graph/importer.py` imports in `src/mtg_synergy_graph/__init__.py` to find the actual public API.

- [ ] **Step A2.4: Run the new test**

Run: `uv run pytest tests/test_importer.py::test_change_type_attributes_populated_for_kaalia -v`

Expected: PASS.

- [ ] **Step A2.5: Full suite**

Run: `uv run pytest tests/ -v`

Expected: all tests PASS.

- [ ] **Step A2.6: Commit**

```bash
git add src/mtg_synergy_graph/ports.py src/mtg_synergy_graph/importer.py tests/test_importer.py
git commit -m "feat: extract ChangeType from ChangeZone effects into port_attributes

ChangeZone effects like Kaalia's 'ChangeType\$ Creature.Angel+YouCtrl,...'
previously lived only in raw_line. Expose the types/subtypes under
attr_kind='change_type' so rule queries can match 'commander cheats
Dragons into play' directly against the candidate's type."
```

---

### Task A3: Extract `TokenScript` into `port_attributes`

**Files:**
- Modify: `src/mtg_synergy_graph/ports.py` (function `extract_effect_ports`)
- Modify: `src/mtg_synergy_graph/importer.py` (same insert loop as A2)
- Test: `tests/test_importer.py`

**Context:** Token effect ports have `TokenScript$ w_1_1_soldier` (or comma-separated for multi-choice). We want `(attr_kind='token_color', attr_value='W')` and `(attr_kind='token_subtype', attr_value='Soldier')` rows.

Token script format: `<color>_<power>_<toughness>_<subtype>` where color is a single letter (w/u/b/r/g/c), power/toughness are digits, subtype is lowercase. Multi-choice separates scripts by `,`.

- [ ] **Step A3.1: Write a parser helper for TokenScript**

Edit `src/mtg_synergy_graph/ports.py`. Add near the top of the file, after the `COST_PATTERNS` block:

```python
#: Forge token-script colour letters → canonical uppercase.
_TOKEN_COLOR_MAP: dict[str, str] = {
    "w": "W", "u": "U", "b": "B", "r": "R", "g": "G", "c": "C",
}


def _parse_token_script(script: str) -> list[tuple[str, str]]:
    """Parse a TokenScript like ``w_1_1_soldier`` or multi-choice
    ``w_1_1_human,u_1_1_merfolk`` into a list of (attr_kind, attr_value) pairs.

    >>> _parse_token_script("w_1_1_soldier")
    [('token_color', 'W'), ('token_subtype', 'Soldier')]
    >>> _parse_token_script("w_1_1_human,u_1_1_merfolk")
    [('token_color', 'W'), ('token_subtype', 'Human'),
     ('token_color', 'U'), ('token_subtype', 'Merfolk')]
    """
    attrs: list[tuple[str, str]] = []
    if not script:
        return attrs
    for piece in script.split(","):
        parts = piece.strip().split("_")
        if len(parts) < 4:
            continue
        color_letter = parts[0].lower()
        color = _TOKEN_COLOR_MAP.get(color_letter)
        if color:
            attrs.append(("token_color", color))
        subtype_raw = parts[3]
        if subtype_raw:
            # Capitalise per Forge convention (soldier → Soldier).
            attrs.append(("token_subtype", subtype_raw.capitalize()))
    return attrs
```

- [ ] **Step A3.2: Attach TokenScript to the port dict**

In the same `extract_effect_ports` port-dict construction touched in A2, add capture for TokenScript:

Find the line added in A2:

```python
    change_type = parsed.get("ChangeType", "") if verb == "ChangeZone" else ""
```

Add directly below:

```python
    token_script = parsed.get("TokenScript", "") if verb == "Token" else ""
```

In the port dict, add another underscore-prefixed key:

```python
        "_change_type": change_type,  # consumed by importer, stripped before insert
        "_token_script": token_script,  # same
```

- [ ] **Step A3.3: Emit token_* attrs in the importer**

In `src/mtg_synergy_graph/importer.py`, in the insert loop touched in A2, after the `if change_type:` block, add:

```python
        token_script = port.pop("_token_script", "") if "_token_script" in port else ""
        if token_script:
            from .ports import _parse_token_script
            for attr_kind, attr_value in _parse_token_script(token_script):
                conn.execute(
                    "INSERT OR IGNORE INTO port_attributes "
                    "(port_id, attr_kind, attr_value, is_negated) VALUES (?, ?, ?, ?)",
                    (port_id, attr_kind, attr_value, False),
                )
```

Move the `token_script = port.pop(...)` line up to join the `change_type` pop at the top of the loop iteration for consistency:

```python
    for port in ports:
        change_type = port.pop("_change_type", "") if "_change_type" in port else ""
        token_script = port.pop("_token_script", "") if "_token_script" in port else ""
        cur = conn.execute(_PORT_INSERT_SQL, _normalise_port(port))
        ...
```

- [ ] **Step A3.4: Test**

Add to `tests/test_importer.py`:

```python
def test_token_script_attributes_populated_for_tireless_provisioner(tmp_path):
    """Tireless Provisioner's Token effect has TokenScript values for
    Food and Treasure tokens via GenericChoice expansion."""
    from mtg_synergy_graph.importer import open_db, upsert_card
    from mtg_synergy_graph import parse_card_file
    from pathlib import Path

    db_path = tmp_path / "test.db"
    conn = open_db(db_path)
    card_path = Path(__file__).parent.parent / "data" / "forge" / "forge-gui" / "res" / "cardsfolder" / "t" / "tireless_provisioner.txt"
    card = parse_card_file(card_path)
    upsert_card(conn, card, oracle_id_resolver=None)

    rows = conn.execute(
        "SELECT attr_kind, attr_value FROM port_attributes "
        "WHERE attr_kind IN ('token_color', 'token_subtype') "
        "AND port_id IN (SELECT id FROM card_ports WHERE card_name=?)",
        ("Tireless Provisioner",),
    ).fetchall()
    values = {(r[0], r[1]) for r in rows}
    assert ("token_subtype", "Food") in values or ("token_subtype", "Treasure") in values, \
        f"Expected Food or Treasure in token_subtype attrs, got {values}"
    conn.close()
```

Note: Tireless Provisioner is already a fixture file (see tests/fixtures/). The Forge source path above is correct.

- [ ] **Step A3.5: Run tests**

Run: `uv run pytest tests/test_importer.py::test_token_script_attributes_populated_for_tireless_provisioner tests/ -v`

Expected: all PASS.

- [ ] **Step A3.6: Commit**

```bash
git add src/mtg_synergy_graph/ports.py src/mtg_synergy_graph/importer.py tests/test_importer.py
git commit -m "feat: extract TokenScript into token_color/token_subtype attrs

Token effect ports previously kept TokenScript like 'w_1_1_soldier'
only in raw_line. Parse into (token_color, W) and (token_subtype,
Soldier) port_attributes rows so tribal/color-matching rules can
query produced tokens directly."
```

---

### Task A4: Add `card_hints` table and populate from deck_needs/hints/has

**Files:**
- Modify: `src/mtg_synergy_graph/schema.sql`
- Modify: `src/mtg_synergy_graph/importer.py` (inside `upsert_card`, after deck_* JSON persistence)
- Test: `tests/test_importer.py`

**Context:** Parser already parses DeckNeeds/DeckHints/DeckHas lines into `card["deck_needs"] = {"Type": ["Dragon"], ...}` dicts (see parser.py:97-115). Importer currently persists them as JSON in the `cards` table. Add a normalised `card_hints` table that flattens `(kind, category, value)` tuples for queryable joins.

- [ ] **Step A4.1: Extend schema**

Edit `src/mtg_synergy_graph/schema.sql`. Append at the end:

```sql
-- ---------------------------------------------------------------------------
-- card_hints: normalised projection of DeckNeeds / DeckHints / DeckHas
-- (from cards.deck_*) and BuffedBy (from card_svars). Populated by the
-- importer; enables rule queries like "commander DeckHas Token ∩
-- candidate DeckNeeds Token".
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS card_hints (
    card_name  TEXT NOT NULL REFERENCES cards(name),
    kind       TEXT NOT NULL,     -- 'needs' | 'hints' | 'has' | 'buffed_by'
    category   TEXT NOT NULL,     -- 'Type' | 'Ability' | 'Color' | 'Keyword' | 'Name'
    value      TEXT NOT NULL,
    PRIMARY KEY (card_name, kind, category, value)
);

CREATE INDEX IF NOT EXISTS idx_card_hints_lookup
    ON card_hints(kind, category, value);
```

- [ ] **Step A4.2: Populate `card_hints` from deck_* dicts in importer**

Edit `src/mtg_synergy_graph/importer.py`. Find the block inside `upsert_card` where deck_* JSON is persisted (the `_card_row(card)` insert at line 426). After the existing deletes at lines 404-409, add a new DELETE:

Find:

```python
    conn.execute(
        "DELETE FROM port_attributes WHERE port_id IN (SELECT id FROM card_ports WHERE card_name = ?)",
        (name,),
    )
    conn.execute("DELETE FROM card_ports WHERE card_name = ?", (name,))
    conn.execute("DELETE FROM card_svars WHERE card_name = ?", (name,))
```

Add after the svars delete:

```python
    conn.execute("DELETE FROM card_hints WHERE card_name = ?", (name,))
```

Then after `conn.execute(_CARD_INSERT_SQL, _card_row(card))` at line 426, insert the card_hints population block:

```python
    # Project deck_needs / deck_hints / deck_has dicts into the normalised
    # card_hints table so synergy rules can join by (kind, category, value).
    for kind, key in (("needs", "deck_needs"), ("hints", "deck_hints"), ("has", "deck_has")):
        source = card.get(key)
        if not source:
            continue
        for category, values in source.items():
            for value in values:
                conn.execute(
                    "INSERT OR IGNORE INTO card_hints "
                    "(card_name, kind, category, value) VALUES (?, ?, ?, ?)",
                    (name, kind, category, value),
                )
```

- [ ] **Step A4.3: Write test for card_hints population**

Add to `tests/test_importer.py`:

```python
def test_card_hints_populated_from_deck_needs(tmp_path):
    """A card with DeckNeeds:Type$Dragon must produce a
    (kind='needs', category='Type', value='Dragon') row in card_hints."""
    from mtg_synergy_graph.importer import open_db, upsert_card

    db_path = tmp_path / "test.db"
    conn = open_db(db_path)

    card = {
        "name": "Test Dragonlord",
        "types": "Legendary Creature",
        "deck_needs": {"Type": ["Dragon"]},
        "deck_has": {"Ability": ["Token"]},
        "abilities": [],
        "svars": {},
        "keywords": [],
    }
    upsert_card(conn, card, oracle_id_resolver=None)

    rows = conn.execute(
        "SELECT kind, category, value FROM card_hints WHERE card_name=? ORDER BY kind, value",
        ("Test Dragonlord",),
    ).fetchall()
    assert ("has", "Ability", "Token") in rows
    assert ("needs", "Type", "Dragon") in rows
    conn.close()
```

- [ ] **Step A4.4: Run tests**

Run: `uv run pytest tests/test_importer.py -v`

Expected: new test PASSES.

- [ ] **Step A4.5: Commit**

```bash
git add src/mtg_synergy_graph/schema.sql src/mtg_synergy_graph/importer.py tests/test_importer.py
git commit -m "feat: add card_hints table populated from DeckNeeds/Hints/Has

Flatten the (category, value) pairs parsed from DeckNeeds/DeckHints/DeckHas
lines into a normalised card_hints table so complement rules can
join by (kind, category, value) instead of re-parsing JSON. Covers
~7k cards with DeckHas, ~4k with DeckHints, ~1.2k with DeckNeeds."
```

---

### Task A5: Populate `card_hints.kind='buffed_by'` from BuffedBy SVar

**Files:**
- Modify: `src/mtg_synergy_graph/importer.py` (inside `upsert_card`)
- Test: `tests/test_importer.py`

**Context:** `BuffedBy` SVar values are comma-separated filter-style tokens (e.g. `Permanent.Snow`, `Elf`, `Instant,Sorcery`). Use `explode_filter` to classify each token into (category, value) pairs and insert into `card_hints` with `kind='buffed_by'`.

- [ ] **Step A5.1: Add BuffedBy population**

Edit `src/mtg_synergy_graph/importer.py`. Immediately after the svars insert loop (around line 430-432), add:

Find:

```python
    for svar_name, svar_value in card.get("svars", {}).items():
        conn.execute(
            "INSERT OR REPLACE INTO card_svars (card_name, svar_name, svar_value) VALUES (?, ?, ?)",
            (name, svar_name, svar_value),
        )
```

Add immediately after:

```python
    # BuffedBy is Forge's AI hint declaring which permanents buff this card.
    # Flatten into card_hints so complement rules can match it to candidate
    # types/subtypes.
    buffed_by = card.get("svars", {}).get("BuffedBy", "")
    if buffed_by:
        for piece in buffed_by.split(","):
            for attr in explode_filter(piece.strip()):
                if attr["attr_kind"] in ("type", "subtype", "keyword", "supertype", "color"):
                    category_map = {
                        "type": "Type", "subtype": "Type",
                        "keyword": "Keyword",
                        "supertype": "Type",
                        "color": "Color",
                    }
                    conn.execute(
                        "INSERT OR IGNORE INTO card_hints "
                        "(card_name, kind, category, value) VALUES (?, ?, ?, ?)",
                        (name, "buffed_by", category_map[attr["attr_kind"]], attr["attr_value"]),
                    )
```

- [ ] **Step A5.2: Write test**

Add to `tests/test_importer.py`:

```python
def test_buffed_by_svar_populates_card_hints(tmp_path):
    """A card with SVar:BuffedBy:Elf,Permanent.Snow must produce rows in
    card_hints with kind='buffed_by'."""
    from mtg_synergy_graph.importer import open_db, upsert_card

    db_path = tmp_path / "test.db"
    conn = open_db(db_path)

    card = {
        "name": "Test Elvish Lord",
        "types": "Creature",
        "abilities": [],
        "svars": {"BuffedBy": "Elf,Permanent.Snow"},
        "keywords": [],
    }
    upsert_card(conn, card, oracle_id_resolver=None)

    rows = conn.execute(
        "SELECT kind, category, value FROM card_hints "
        "WHERE card_name=? AND kind='buffed_by' ORDER BY value",
        ("Test Elvish Lord",),
    ).fetchall()
    values = {r[2] for r in rows}
    assert "Elf" in values, f"Expected Elf in buffed_by hints, got {values}"
    conn.close()
```

- [ ] **Step A5.3: Run tests**

Run: `uv run pytest tests/test_importer.py -v`

Expected: new test PASSES.

- [ ] **Step A5.4: Commit**

```bash
git add src/mtg_synergy_graph/importer.py tests/test_importer.py
git commit -m "feat: populate card_hints.buffed_by from BuffedBy SVar

Parse BuffedBy SVar values through explode_filter and project the
type/subtype/keyword/color tokens into card_hints with kind='buffed_by'.
Covers ~1.05k cards that Forge explicitly annotates with 'I care about X'."
```

---

### Task A6: Validation gate — fresh reimport + golden-set NDCG

**Files:** none modified; this is a verification step.

- [ ] **Step A6.1: Move aside the existing DB so we can do a fresh import**

```bash
mv data/synergy.db data/synergy.db.pre-phase-a
```

- [ ] **Step A6.2: Reimport**

Run: `uv run python scripts/import_cardsfolder.py`

Expected: ~60-120 seconds. Verify at the end:

```bash
uv run python -c "import sqlite3; c=sqlite3.connect('data/synergy.db'); print('ports:', c.execute('SELECT COUNT(*) FROM card_ports').fetchone()[0]); print('attrs:', c.execute('SELECT COUNT(*) FROM port_attributes').fetchone()[0]); print('hints:', c.execute('SELECT COUNT(*) FROM card_hints').fetchone()[0])"
```

Expected output:
- `ports: ` between 120,000 and 140,000 (was 184,106, dropping 26-35% from A1 alone)
- `attrs: ` similar or slightly higher than 162,652 (A2/A3 add entries; A1 removes duplicates)
- `hints: ` between 12,000 and 15,000 (sum of needs+hints+has+buffed_by)

- [ ] **Step A6.3: Run Akroma sanity check**

```bash
uv run python -c "import sqlite3; c=sqlite3.connect('data/synergy.db'); n=c.execute('SELECT COUNT(*) FROM card_ports WHERE card_name=?', ('Akroma, Vision of Ixidor',)).fetchone()[0]; print('Akroma ports:', n); assert n < 50, f'Akroma still exploding: {n}'"
```

Expected: `Akroma ports: 19` (or similar small number).

- [ ] **Step A6.4: Run golden-set NDCG**

Run: `uv run python scripts/golden_set_track.py --baseline tests/fixtures/golden_set_run.json`

Record output. Expected:
- `fresh agg NDCG: ` ≥ 0.189 (baseline is 0.19397; tolerance -0.005)

- [ ] **Step A6.5: If NDCG passes, snapshot the new baseline**

Only if `fresh agg NDCG ≥ 0.189`:

```bash
uv run python scripts/compare_edhrec.py --commanders tests/fixtures/golden_set.json --output tests/fixtures/golden_set_run.json
git add tests/fixtures/golden_set_run.json
git commit -m "chore: update golden set baseline after phase A data cleanup

Phase A (data-layer) complete:
- Fixed extract_effect_ports 2^N re-walk (88 cards no longer explode)
- Extract ChangeType into port_attributes (attr_kind='change_type')
- Extract TokenScript into port_attributes (token_color, token_subtype)
- New card_hints table populated from DeckNeeds/Hints/Has + BuffedBy

NDCG@30 delta: <record actual delta>"
```

If NDCG < 0.189, stop and investigate. Bisect to find which sub-task caused the regression; the most likely culprit is A1 if rules were accidentally relying on the duplicate ports.

- [ ] **Step A6.6: Retire the pre-phase-A DB snapshot after NDCG passes**

```bash
rm data/synergy.db.pre-phase-a
```

---

## Phase C — Scoring math cleanup

### Task C1: Drop flat weight overrides + flat-count rules

**Files:**
- Modify: `src/mtg_synergy_graph/universal_scorer.py`
- Test: `tests/test_universal_scorer.py` (check if exists; if not, add unit tests in the closest file)

**Context:** `_FLAT_WEIGHT_OVERRIDES` and `_FLAT_COUNT_RULES` at [universal_scorer.py:275-304](../../../src/mtg_synergy_graph/universal_scorer.py:275) are hand-tuned per-rule constants (spell_density 0.3, etb_self 0.01, etc.). CLAUDE.md explicitly says "no hand-tuned weights". With Phase A's clean IDF denominators, we can drop them and let pure IDF handle all rules.

- [ ] **Step C1.1: Capture current golden NDCG as the post-A baseline reference**

```bash
uv run python scripts/golden_set_track.py --baseline tests/fixtures/golden_set_run.json 2>&1 | tee /tmp/post-a-ndcg.txt
```

This is the baseline Phase C must match or beat.

- [ ] **Step C1.2: Remove `_FLAT_WEIGHT_OVERRIDES` and `_FLAT_COUNT_RULES`**

Edit `src/mtg_synergy_graph/universal_scorer.py`. Delete the two module-level constants:

- Delete lines 275-285 (`_FLAT_COUNT_RULES: frozenset[str] = frozenset({...})`)
- Delete lines 296-304 (`_FLAT_WEIGHT_OVERRIDES: dict[str, float] = {...}`)
- Delete the explanatory comment blocks immediately above each (lines 273-274 and 287-295)

Then in `_compute_idf_weights` (function starting ~line 322), delete the conditional branch for `_FLAT_COUNT_RULES`:

Find:

```python
    for key, candidates in freq.items():
        rule_id = key[0]
        if rule_id in _FLAT_COUNT_RULES:
            override = _FLAT_WEIGHT_OVERRIDES.get(rule_id)
            result[key] = override if override is not None else 1.0
        else:
            n = len(candidates)
            # For forward panharmonicon matches, apply minimum N=10 floor.
            ...
```

Replace with:

```python
    for key, candidates in freq.items():
        rule_id = key[0]
        n = len(candidates)
        # For forward panharmonicon matches, apply minimum N=10 floor.
        ...
```

(Keep the rest of the else-branch body: panharmonicon floor, IDF formula, quality multiplier.)

- [ ] **Step C1.3: Run the test suite**

Run: `uv run pytest tests/ -v`

Expected: all tests PASS. Some scoring-specific tests may need updated numeric expectations — if so, run them individually and update expected values with the new weight math (only AFTER confirming the test is asserting on scoring math, not on the behaviours C1 is meant to remove).

- [ ] **Step C1.4: Run NDCG**

Run: `uv run python scripts/golden_set_track.py --baseline tests/fixtures/golden_set_run.json 2>&1 | tee /tmp/c1-ndcg.txt`

Record delta vs `/tmp/post-a-ndcg.txt`. DO NOT commit yet — we're going to compare C1 vs C2 and pick the winner.

- [ ] **Step C1.5: Stash C1 for later comparison**

```bash
git diff src/mtg_synergy_graph/universal_scorer.py > /tmp/c1.patch
git checkout src/mtg_synergy_graph/universal_scorer.py
```

This restores pre-C1 code so Task C2 starts from a clean slate.

---

### Task C2: Drop flat weights + add data-derived ceiling for broad-filter rules

**Files:**
- Modify: `src/mtg_synergy_graph/universal_scorer.py`

**Context:** Alternative to C1. Same removal of `_FLAT_WEIGHT_OVERRIDES`/`_FLAT_COUNT_RULES`, PLUS a new data-derived ceiling that caps broad-filter rules without hand-tuning.

The ceiling idea: for each `(rule_id, cmdr_event, cand_event)` tuple, compute `distinct_filter_groups = count of unique filter_group values within this (rule_id, cmdr_event, cand_event)`. If N (candidate count for a specific filter_group) is large AND `distinct_filter_groups` is small, the rule is genuinely broad (every commander's Creature filter matches the same pool). Apply ceiling `k / log2(1 + distinct_filter_groups)`.

- [ ] **Step C2.1: Apply C1's removals (start from clean)**

Edit `src/mtg_synergy_graph/universal_scorer.py`. Repeat C1.2's deletions:
- Remove `_FLAT_COUNT_RULES` frozenset (lines 275-285 of the pre-C file)
- Remove `_FLAT_WEIGHT_OVERRIDES` dict (lines 296-304)
- Simplify `_compute_idf_weights` to drop the flat-override branch

- [ ] **Step C2.2: Add data-derived ceiling**

Still in `_compute_idf_weights`, replace the function body with:

```python
def _compute_idf_weights(
    complements: list[PortComplement],
) -> dict[tuple[str, str, str, str], float]:
    """Compute IDF weights: 1 / log2(1 + N) per match tuple, with a
    data-derived ceiling for broad-filter rules.

    For each (rule_id, cmdr_event, cand_event) triple, count the number
    of distinct filter_groups observed. If a rule has only a few filter
    groups but matches many candidates (i.e. everyone's filter is 'Creature'),
    the rule is genuinely broad — apply a ceiling of
    1 / log2(1 + distinct_filter_groups).
    """
    freq: dict[tuple[str, str, str, str], set[str]] = defaultdict(set)
    groups_per_rule: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    for c in complements:
        key = (c.rule_id, c.cmdr_event, c.cand_event, c.filter_group)
        freq[key].add(c.candidate)
        groups_per_rule[(c.rule_id, c.cmdr_event, c.cand_event)].add(c.filter_group)

    result: dict[tuple[str, str, str, str], float] = {}
    for key, candidates in freq.items():
        rule_id, cmdr_event, cand_event, _filter_group = key
        n = len(candidates)
        if rule_id == "panharmonicon" and n < 10 and "reverse" not in cmdr_event and "stack" not in cmdr_event:
            n = 10
        w = 1.0 / math.log2(1.0 + n)
        # Data-derived ceiling: rules with few filter groups and many
        # candidates are inherently broad; cap them.
        ng = len(groups_per_rule[(rule_id, cmdr_event, cand_event)])
        if ng >= 1:
            ceiling = 1.0 / math.log2(1.0 + ng + 1)  # +1 avoids div-by-zero when ng=1
            w = min(w, ceiling)
        mult = _RULE_QUALITY_MULTIPLIER.get(rule_id, 1.0)
        result[key] = w * mult
    return result
```

- [ ] **Step C2.3: Run test suite**

Run: `uv run pytest tests/ -v`

Expected: all tests PASS (same caveat as C1.3).

- [ ] **Step C2.4: Run NDCG**

Run: `uv run python scripts/golden_set_track.py --baseline tests/fixtures/golden_set_run.json 2>&1 | tee /tmp/c2-ndcg.txt`

Record.

- [ ] **Step C2.5: Stash C2 patch**

```bash
git diff src/mtg_synergy_graph/universal_scorer.py > /tmp/c2.patch
git checkout src/mtg_synergy_graph/universal_scorer.py
```

---

### Task C3: Compare C1 vs C2, pick winner, commit

- [ ] **Step C3.1: Compare the NDCG files**

```bash
echo "=== post-A baseline ==="; cat /tmp/post-a-ndcg.txt
echo "=== C1 (pure IDF) ==="; cat /tmp/c1-ndcg.txt
echo "=== C2 (IDF + ceiling) ==="; cat /tmp/c2-ndcg.txt
```

Compare `fresh agg NDCG` on each. Decision rule:
1. If both C1 and C2 regress vs post-A baseline by > 0.005, abandon Phase C: `rm /tmp/c*.patch` and skip to Phase B.
2. Otherwise pick the highest NDCG.
3. If within ±0.002, prefer C1 (simpler code).

- [ ] **Step C3.2: Apply the winning patch**

For winner `X` (c1 or c2):

```bash
git apply /tmp/$X.patch
```

- [ ] **Step C3.3: Re-run tests + NDCG as confirmation**

```bash
uv run pytest tests/ -v
uv run python scripts/golden_set_track.py --baseline tests/fixtures/golden_set_run.json
```

Expected: tests PASS, NDCG matches the stashed result.

- [ ] **Step C3.4: Audit `_RULE_QUALITY_MULTIPLIER`**

Open `src/mtg_synergy_graph/universal_scorer.py`. Locate `_RULE_QUALITY_MULTIPLIER` (~line 308 pre-edit). It contains:

```python
_RULE_QUALITY_MULTIPLIER: dict[str, float] = {
    "damage_synergy": 0.5,
    "trigger_resonance": 0.7,
    "value_engine": 0.5,
    "cost_reduction_target": 0.5,
}
```

For each key, remove it temporarily and run the golden set:

```bash
# For each rule_id in the dict, in turn:
# 1. comment out the line
# 2. uv run python scripts/golden_set_track.py --baseline tests/fixtures/golden_set_run.json
# 3. record NDCG
# 4. uncomment
```

After testing all four, remove the lines whose removal produced NDCG ≥ (winner NDCG − 0.001) — i.e. the dampening wasn't earning its keep.

- [ ] **Step C3.5: Commit Phase C result**

```bash
git add src/mtg_synergy_graph/universal_scorer.py
git commit -m "perf: drop hand-tuned flat weights from IDF scoring

Remove _FLAT_WEIGHT_OVERRIDES and _FLAT_COUNT_RULES now that Phase A's
clean IDF denominators produce sensible weights without manual overrides.
<Also note the C2 ceiling if C2 won; also note any removed entries from
_RULE_QUALITY_MULTIPLIER.>

NDCG@30 delta: <record>"
```

- [ ] **Step C3.6: Update golden-set baseline**

```bash
uv run python scripts/compare_edhrec.py --commanders tests/fixtures/golden_set.json --output tests/fixtures/golden_set_run.json
git add tests/fixtures/golden_set_run.json
git commit -m "chore: update golden set baseline after phase C scoring cleanup"
```

---

## Phase B — Curated-hint rules

### Task B1: `deck_hint_match` (symmetric) rule

**Files:**
- Create: `src/mtg_synergy_graph/complement_rules/hints.py`
- Modify: `src/mtg_synergy_graph/complement_rules/__init__.py`
- Modify: `src/mtg_synergy_graph/complement_rules/core.py` (function `find_all_complements`)
- Modify: `src/mtg_synergy_graph/universal_scorer.py` (add rule_id to `_RULE_TO_BUCKET`)
- Test: `tests/test_hint_rules.py` (new)

**Context:** Fire when commander has a `card_hints` row with `kind='has'` AND candidate has a row with `kind='needs'` sharing the same `(category, value)`. IDF key uses `(rule_id, category, category, value)`.

- [ ] **Step B1.1: Create `hints.py` with the symmetric finder**

Create `src/mtg_synergy_graph/complement_rules/hints.py`:

```python
"""Curated-hint complement matchers.

Use Forge's AI annotations (DeckNeeds/DeckHints/DeckHas) and the BuffedBy
SVar to surface synergies that are declarative rather than mechanical.
The annotations live in the card_hints table (see schema.sql), populated
by the importer.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence

from .core import PortComplement, PortRow


def _find_deck_hint_matches(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
) -> list[PortComplement]:
    """Symmetric match: commander has a (category, value) tuple with
    kind='has' AND candidate has the same tuple with kind='needs'.
    """
    del cmdr_ports  # unused — we query card_hints directly by commander name

    commanders = tuple(cmdr_set)
    if not commanders:
        return []
    placeholders = ",".join("?" * len(commanders))

    # Pull commander has-tuples
    cmdr_rows = conn.execute(
        f"SELECT card_name, category, value FROM card_hints "
        f"WHERE kind='has' AND card_name IN ({placeholders})",
        commanders,
    ).fetchall()
    cmdr_tuples: dict[str, set[tuple[str, str]]] = {c: set() for c in commanders}
    for cmdr, cat, val in cmdr_rows:
        cmdr_tuples[cmdr].add((cat, val))

    results: list[PortComplement] = []
    for cmdr, tuples in cmdr_tuples.items():
        for category, value in tuples:
            cand_rows = conn.execute(
                "SELECT card_name FROM card_hints "
                "WHERE kind='needs' AND category=? AND value=? AND card_name NOT IN (%s)"
                % placeholders,
                (category, value, *commanders),
            ).fetchall()
            for (candidate,) in cand_rows:
                results.append(
                    PortComplement(
                        rule_id="deck_hint_match",
                        direction="synergy",
                        candidate=candidate,
                        cmdr_event=category,
                        cand_event=category,
                        filter_group=value,
                        branch_kind="root",
                    )
                )
    return results
```

**Note:** Before writing the SQL, read `core.py` to confirm the exact `PortComplement` dataclass fields (rule_id, direction, candidate, cmdr_event, cand_event, filter_group, branch_kind). Also confirm that `PortRow` type alias lives in `core.py`.

- [ ] **Step B1.2: Export from `__init__.py`**

Edit `src/mtg_synergy_graph/complement_rules/__init__.py`. Add at the bottom:

```python
from .hints import _find_deck_hint_matches as _find_deck_hint_matches
```

- [ ] **Step B1.3: Dispatch from `find_all_complements`**

Edit `src/mtg_synergy_graph/complement_rules/core.py`. Find the block inside `find_all_complements` where `_card_attr_complements()` is defined (around line 856), and add the new import + dispatch:

At the top of `core.py`, add the import near the other `_find_*` imports:

```python
from .hints import _find_deck_hint_matches
```

Inside `_card_attr_complements()`, append to the `out` list:

```python
        out.extend(_find_deck_hint_matches(conn, cmdr_ports, cmdr_set))
```

- [ ] **Step B1.4: Register in `_RULE_TO_BUCKET`**

Edit `src/mtg_synergy_graph/universal_scorer.py`. In `_RULE_TO_BUCKET` dict (around line 34), add:

```python
    "deck_hint_match": "hint_match",
```

Check `scoring.py`'s `BUCKETS` constant — if `hint_match` isn't already there, add it.

- [ ] **Step B1.5: Write a unit test for the rule firing**

Create `tests/test_hint_rules.py`:

```python
"""Unit tests for curated-hint complement rules."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest


@pytest.fixture
def hints_conn(tmp_path):
    """Minimal synergy.db with just cards + card_hints populated."""
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(db_path)
    schema = (Path(__file__).parent.parent / "src/mtg_synergy_graph/schema.sql").read_text()
    conn.executescript(schema)
    conn.execute("INSERT INTO cards (name) VALUES ('TestCmdr')")
    conn.execute("INSERT INTO cards (name) VALUES ('TestCandidate')")
    conn.execute(
        "INSERT INTO card_hints (card_name, kind, category, value) VALUES (?, ?, ?, ?)",
        ("TestCmdr", "has", "Ability", "Token"),
    )
    conn.execute(
        "INSERT INTO card_hints (card_name, kind, category, value) VALUES (?, ?, ?, ?)",
        ("TestCandidate", "needs", "Ability", "Token"),
    )
    conn.commit()
    return conn


def test_deck_hint_match_fires_on_has_needs_pair(hints_conn):
    from mtg_synergy_graph.complement_rules.hints import _find_deck_hint_matches

    results = _find_deck_hint_matches(hints_conn, [], {"TestCmdr"})
    assert len(results) == 1
    c = results[0]
    assert c.rule_id == "deck_hint_match"
    assert c.candidate == "TestCandidate"
    assert c.cmdr_event == "Ability"
    assert c.cand_event == "Ability"
    assert c.filter_group == "Token"


def test_deck_hint_match_skips_self(hints_conn):
    """Commander must not match itself even if it has both has and needs."""
    from mtg_synergy_graph.complement_rules.hints import _find_deck_hint_matches

    hints_conn.execute(
        "INSERT INTO card_hints (card_name, kind, category, value) VALUES (?, ?, ?, ?)",
        ("TestCmdr", "needs", "Ability", "Token"),
    )
    results = _find_deck_hint_matches(hints_conn, [], {"TestCmdr"})
    # Only the TestCandidate match; TestCmdr excluded by NOT IN
    assert len(results) == 1
    assert results[0].candidate == "TestCandidate"
```

- [ ] **Step B1.6: Run the test**

Run: `uv run pytest tests/test_hint_rules.py -v`

Expected: both tests PASS.

- [ ] **Step B1.7: Run full test suite**

Run: `uv run pytest tests/ -v`

Expected: all tests PASS.

- [ ] **Step B1.8: Measure standalone NDCG delta**

```bash
uv run python scripts/golden_set_track.py --baseline tests/fixtures/golden_set_run.json 2>&1 | tee /tmp/b1-ndcg.txt
```

Record delta vs post-C baseline.

- [ ] **Step B1.9: Commit B1**

If delta ≥ +0.001:

```bash
git add src/mtg_synergy_graph/complement_rules/hints.py \
        src/mtg_synergy_graph/complement_rules/__init__.py \
        src/mtg_synergy_graph/complement_rules/core.py \
        src/mtg_synergy_graph/universal_scorer.py \
        tests/test_hint_rules.py
git commit -m "feat: add deck_hint_match complement rule

Symmetric match on card_hints: commander has (category, value) with
kind='has' AND candidate has the same tuple with kind='needs'. Uses
IDF keyed on (rule_id, category, category, value) so rare shared
values score high and common ones score low.

NDCG@30 delta: <record>"
```

If delta < +0.001, revert B1 and skip to B2:

```bash
git checkout src/mtg_synergy_graph/complement_rules/__init__.py \
             src/mtg_synergy_graph/complement_rules/core.py \
             src/mtg_synergy_graph/universal_scorer.py
git clean -f src/mtg_synergy_graph/complement_rules/hints.py tests/test_hint_rules.py
```

---

### Task B2: `deck_needs_fulfilled` (commander-centric) rule

**Files:**
- Modify: `src/mtg_synergy_graph/complement_rules/hints.py`
- Modify: `src/mtg_synergy_graph/complement_rules/core.py` (dispatch list)
- Modify: `src/mtg_synergy_graph/universal_scorer.py`
- Test: `tests/test_hint_rules.py`

**Context:** When commander has `DeckNeeds$ Type$Dragon`, match any candidate whose `cards.subtypes` or `cards.types` contains `Dragon`. Fires even when candidate has no annotations.

- [ ] **Step B2.1: Add `_find_deck_needs_fulfilled` to hints.py**

Append to `src/mtg_synergy_graph/complement_rules/hints.py`:

```python
def _find_deck_needs_fulfilled(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
) -> list[PortComplement]:
    """Commander-centric: cmdr DeckNeeds (category, value) matched against
    candidate's types/subtypes/keywords/color_identity."""
    del cmdr_ports

    commanders = tuple(cmdr_set)
    if not commanders:
        return []
    placeholders = ",".join("?" * len(commanders))

    needs_rows = conn.execute(
        f"SELECT card_name, category, value FROM card_hints "
        f"WHERE kind='needs' AND card_name IN ({placeholders})",
        commanders,
    ).fetchall()

    results: list[PortComplement] = []
    for cmdr, category, value in needs_rows:
        if category == "Type":
            sql_col = "subtypes"  # most DeckNeeds targets are creature subtypes
        elif category == "Color":
            sql_col = "color_identity"
        elif category == "Keyword":
            sql_col = "keywords"
        else:
            continue  # Ability/Name categories not handled here

        cand_rows = conn.execute(
            f"SELECT name FROM cards WHERE {sql_col} LIKE ? AND name NOT IN ({placeholders})",
            (f"%{value}%", *commanders),
        ).fetchall()
        for (candidate,) in cand_rows:
            results.append(
                PortComplement(
                    rule_id="deck_needs_fulfilled",
                    direction="synergy",
                    candidate=candidate,
                    cmdr_event=category,
                    cand_event=category,
                    filter_group=value,
                    branch_kind="root",
                )
            )
    return results
```

**Caveat:** The `LIKE '%Dragon%'` substring match is imprecise (matches "Dragonlord" but also any card with "Dragon" in `subtypes`). In Forge data `subtypes` is a space-separated list, so `LIKE '% Dragon %'` or exact-match splitting is safer. When implementing, verify by querying:

```bash
uv run python -c "import sqlite3; c=sqlite3.connect('data/synergy.db'); print([r[0] for r in c.execute(\"SELECT name FROM cards WHERE subtypes LIKE '%Dragon%' LIMIT 5\")])"
```

If there are false positives, change to an exact-match approach: fetch all candidates, split `subtypes`/`types` in Python, compare sets.

- [ ] **Step B2.2: Register + dispatch + bucket**

Same pattern as B1:

- Add `_find_deck_needs_fulfilled` to `__init__.py` exports
- Append to `_card_attr_complements()` in `core.py`
- Add `"deck_needs_fulfilled": "hint_match"` to `_RULE_TO_BUCKET` in `universal_scorer.py`

- [ ] **Step B2.3: Add unit test**

Append to `tests/test_hint_rules.py`:

```python
def test_deck_needs_fulfilled_matches_type(hints_conn):
    from mtg_synergy_graph.complement_rules.hints import _find_deck_needs_fulfilled

    # Commander needs a Dragon; candidate IS a Dragon.
    hints_conn.execute(
        "INSERT INTO card_hints (card_name, kind, category, value) VALUES (?, ?, ?, ?)",
        ("TestCmdr", "needs", "Type", "Dragon"),
    )
    hints_conn.execute(
        "UPDATE cards SET subtypes='Elder Dragon' WHERE name='TestCandidate'"
    )
    results = _find_deck_needs_fulfilled(hints_conn, [], {"TestCmdr"})
    assert any(c.candidate == "TestCandidate" for c in results)
```

- [ ] **Step B2.4: Run tests + NDCG + commit (if delta ≥ +0.001)**

Same pattern as B1.7-B1.9 with message:

```
feat: add deck_needs_fulfilled rule matching cmdr DeckNeeds to candidate types
```

---

### Task B3: `buffed_by_match` rule

**Files:** same as B1/B2.

**Context:** `card_hints` rows with `kind='buffed_by'` express "I care about X". Match commander's `buffed_by` values against candidate types/subtypes (and vice-versa).

- [ ] **Step B3.1: Add `_find_buffed_by_matches`**

Append to `hints.py`:

```python
def _find_buffed_by_matches(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
) -> list[PortComplement]:
    """Match on BuffedBy SVar: commander 'buffed_by' hint → candidate
    with matching type/subtype. Also the inverse: candidate's buffed_by
    → commander's types/subtypes."""
    del cmdr_ports

    commanders = tuple(cmdr_set)
    if not commanders:
        return []
    placeholders = ",".join("?" * len(commanders))

    # Direction 1: commander BuffedBy value → candidate with that type/subtype
    buffed_rows = conn.execute(
        f"SELECT card_name, category, value FROM card_hints "
        f"WHERE kind='buffed_by' AND card_name IN ({placeholders})",
        commanders,
    ).fetchall()

    results: list[PortComplement] = []
    for cmdr, category, value in buffed_rows:
        if category == "Type":
            cand_rows = conn.execute(
                f"SELECT name FROM cards WHERE (subtypes LIKE ? OR types LIKE ?) "
                f"AND name NOT IN ({placeholders})",
                (f"%{value}%", f"%{value}%", *commanders),
            ).fetchall()
        elif category == "Keyword":
            cand_rows = conn.execute(
                f"SELECT name FROM cards WHERE keywords LIKE ? AND name NOT IN ({placeholders})",
                (f"%{value}%", *commanders),
            ).fetchall()
        else:
            continue
        for (candidate,) in cand_rows:
            results.append(
                PortComplement(
                    rule_id="buffed_by_match",
                    direction="synergy",
                    candidate=candidate,
                    cmdr_event=category,
                    cand_event=category,
                    filter_group=value,
                    branch_kind="root",
                )
            )
    return results
```

(Inverse direction — candidate BuffedBy → commander subtype — is optional; skip unless B3 standalone NDCG suggests it's needed.)

- [ ] **Step B3.2-B3.4: Register, test, commit — same pattern as B1/B2**

Commit message if delta ≥ +0.001:

```
feat: add buffed_by_match rule matching BuffedBy hints to card types
```

---

### Task B4: Add pair bonuses

**Files:**
- Modify: `src/mtg_synergy_graph/universal_scorer.py` (only if B1/B2/B3 committed)

**Context:** `_SYNERGY_PAIRS` at line 114-140 rewards mechanical feedback loops. Curated-hint rules create new meaningful combinations.

- [ ] **Step B4.1: Add pairs that survived B1/B2/B3**

Only add pairs where both rules landed. In `_SYNERGY_PAIRS`:

```python
    # Hint-based pairs: curated hint confirmed by mechanical match
    frozenset({"deck_hint_match", "trigger_effect"}): 0.04,
    frozenset({"deck_needs_fulfilled", "tribal_density"}): 0.03,
    frozenset({"buffed_by_match", "lord"}): 0.03,
```

Drop entries whose first rule_id isn't in the codebase (i.e. was reverted in B1-B3).

- [ ] **Step B4.2: Run NDCG**

```bash
uv run python scripts/golden_set_track.py --baseline tests/fixtures/golden_set_run.json
```

Expected: NDCG matches or exceeds the cumulative post-B1/B2/B3 result.

- [ ] **Step B4.3: Commit**

```bash
git add src/mtg_synergy_graph/universal_scorer.py
git commit -m "feat: add pair bonuses for hint-based rules

Reward meaningful combinations of curated-hint matches with mechanical
ones: deck_hint_match+trigger_effect, deck_needs_fulfilled+tribal_density,
buffed_by_match+lord."
```

---

### Task B5: Final validation and baseline update

- [ ] **Step B5.1: Final golden-set run**

```bash
uv run python scripts/golden_set_track.py --baseline tests/fixtures/golden_set_run.json
uv run python scripts/compare_edhrec.py --commanders tests/fixtures/golden_set.json --output tests/fixtures/golden_set_run.json
```

- [ ] **Step B5.2: Commit final baseline**

```bash
git add tests/fixtures/golden_set_run.json
git commit -m "chore: update golden set baseline after phase B hint rules"
```

- [ ] **Step B5.3: Update CLAUDE.md**

Edit `CLAUDE.md`. Update the rule table in the "Complement Rules" section to include any hint rules that landed (deck_hint_match, deck_needs_fulfilled, buffed_by_match). Update the "IDF Weighting" section to note that `_FLAT_WEIGHT_OVERRIDES` was removed.

Commit:

```bash
git add CLAUDE.md
git commit -m "docs: update CLAUDE.md for scoring-model-improvement changes"
```

---

## Self-Review

**Spec coverage:**
- Spec §Phase A.A1 (bug fix) → Task A1 ✓
- Spec §Phase A.A2 (ChangeType) → Task A2 ✓
- Spec §Phase A.A3 (TokenScript) → Task A3 ✓
- Spec §Phase A.A4 (card_hints from deck_*) → Task A4 ✓
- Spec §Phase A.A5 (BuffedBy) → Task A5 ✓
- Spec §Phase A.A6 (validation) → Task A6 ✓
- Spec §Phase C.C1 → Task C1 ✓
- Spec §Phase C.C2 → Task C2 ✓
- Spec §Phase C.C3 audit → Task C3.4 ✓
- Spec §Phase B.B1 → Task B1 ✓
- Spec §Phase B.B2 → Task B2 ✓
- Spec §Phase B.B3 → Task B3 ✓
- Spec §Phase B.B4 (pair bonuses) → Task B4 ✓
- Spec §Phase B.B5 (per-rule validation) → B1.8, B2.4, B3.3, B5.1 ✓

**Placeholders:** none — every step has concrete code or commands.

**Type consistency:**
- `PortComplement(rule_id, direction, candidate, cmdr_event, cand_event, filter_group, branch_kind)` — matches dataclass signature per core.py read.
- `extract_effect_ports(card_name, parsed_or_node, svars)` — signature preserved.
- `_parse_token_script(script)` — defined in ports.py and imported in importer.py (A3.3).
- `card_hints` columns `(card_name, kind, category, value)` — consistent across A4, A5, B1-B3.

**Caveat flagged inline:** B2 LIKE-based substring match can false-positive (Dragonlord vs Dragon). Implementation note directs the engineer to switch to space-aware split if queries surface false positives.
