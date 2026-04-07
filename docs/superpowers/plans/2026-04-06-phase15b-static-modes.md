# Phase 1.5 Sub-project B — Static Mode$ Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract Forge S: line `Mode$` values into a new `static_mode` column on `forge_abilities`, fix the existing verb-column pollution, and surface the data through auto-derived `Static$<mode>` deck tags + the `mechanics_vectors` concept space — without adding any new GBM feature columns.

**Architecture:** Schema migration (add `static_mode TEXT` to `forge_abilities`, drop+recreate on detect) → re-import Forge data → wire new column through `forge_features.py` (profile field, auto-tags, schema sanity check) → wire through `mechanics_vectors.py` (synthetic event tuple in produces vector) → retrain → validate per-commander.

**Tech Stack:** Python 3.12, sqlite3, lightgbm, numpy, pytest, `uv run`. Branch: `feat/forge-dsl-phase15b-static-modes` (already created from `feat/forge-dsl-phase1@63b7ca9`).

**Spec:** `docs/superpowers/specs/2026-04-06-phase15b-static-modes-design.md` (committed in `e69f429`).

**Predecessor baseline NDCG@30:** ~0.5691 (Phase 1 final, branch `feat/forge-dsl-phase1@63b7ca9`).

---

## Critical context (read before starting)

1. **Phase 1 lessons** (mandatory):
   - **Corpus verification first.** Phase 1 Task 1 was abandoned because the audit specified a DSL format that did not exist in the real corpus. Tasks 2 and 3 caught this by sampling real DB rows BEFORE writing tests. **You MUST do the same in Task 1 below.**
   - **Single training run with `tee`.** Don't grep partial logs; run training once with full log capture.
   - **Pre-flight backup.** Always backup `data/tags.db` and the trained model before destructive operations.
   - **Strict equality (`==`) on load-bearing invariants.** Use it where the assertion would catch a regression.

2. **The SELECT in `_load_forge_profiles` explicitly names columns** (line 496-503 of `forge_features.py`) in a different order than the table column order. This means **adding `static_mode TEXT` to the table does NOT shift any existing positional indices in this SELECT.** You add `fa.static_mode` to the SELECT list and access it as a new index (15). This is good news — much less audit work than the spec implied.

3. **The `_raw_abilities` tuple is constructed at line 505**: `self._raw_abilities.append(row[:12] + (row[14],))` — takes positions 0-11 and position 14 (defined). Result is 13 elements. To pass `static_mode`, add it to the SELECT (becomes index 15) and change the tuple to `row[:12] + (row[14], row[15])` → 14 elements. `mechanics_vectors.py` must be updated to expect 14 elements.

4. **`mechanics_vectors.py` has TWO code paths**: one for `preloaded_abilities` (used by forge_features.py) and one for direct DB access (line 437-448). Both must be updated.

5. **The category dispatch at `mechanics_vectors.py:399`** has an `elif` chain that promotes specific event_classes to `themes`: `elif ec in ("equip", "attach", "etb_doubled", "defender")`. Add `"static_mode"` to that tuple AND add `"static_mode": "themes"` to `_EVENT_CATEGORY` (belt + suspenders, since both code paths exist).

6. **Project rules**:
   - PEP 8 + type hints (PEP 604 unions OK)
   - pytest, `uv run pytest tests/ -v`
   - No new GBM feature columns
   - General mechanical patterns over per-card rules (`feedback_no_individual_rules`)
   - Extract ALL Forge data (`feedback_extract_all_forge`) — no Mode$ filtering
   - Back up cache before experiments (`feedback_cache_management`)
   - Run training ONCE with `tee` (`feedback_training_workflow`)

---

## File structure

| File | Role | Change kind |
|---|---|---|
| `packages/mtg-synergy-train/src/mtg_synergy_train/parse/forge_import.py` | DSL parser + DB importer | EDIT (3 sites: `ensure_forge_schema`, `extract_ability_fields` S: branch, `import_card_to_db` INSERT) |
| `packages/mtg-synergy/src/mtg_synergy/recommend/forge_features.py` | Profile + feature loader | EDIT (5 sites: `__init__` schema check, `_load_forge_profiles` SELECT + `_raw_abilities` tuple, `_process_forge_ability_row` row destructuring + S: branch, `_compact_forge_profiles`, `_load_deck_tags` auto-tag synthesis) |
| `packages/mtg-synergy/src/mtg_synergy/recommend/mechanics_vectors.py` | Auto-derived concept space | EDIT (3 sites: `_EVENT_CATEGORY` dict, category dispatch elif chain, `build_mechanics_vectors` row processing — both code paths) |
| `tests/conftest.py` | Test DB fixture | EDIT (mirror new schema) |
| `tests/test_schema.py` | Schema assertions | EDIT (add `static_mode` test) |
| `tests/test_forge_features_phase15_static_modes.py` | New test file | CREATE (~15 tests across 3 classes) |
| `CLAUDE.md` | Project documentation | EDIT (note new column + Static$ tag prefix) |

**Files explicitly NOT touched:**
- `packages/mtg-synergy/src/mtg_synergy/recommend/forge_compute.py` — no new feature columns
- `packages/mtg-synergy/src/mtg_synergy/recommend/scoring.py` — no new penalties
- `scripts/train_fusion_model.py` — `FORGE_FEATURE_NAMES` unchanged
- `scripts/build_graph.py` — graph doesn't read `static_mode`
- `scripts/download_cards.py`, `scripts/fetch_edhrec_all.py` — external data unchanged

---

## Task 1: Pre-flight backup + corpus verification

**Files:** none (filesystem operations + DB read-only queries)

**Goal:** Snapshot the current state and verify the actual S: line DSL format in the live corpus. **No code changes in this task.** Establishes the corpus inputs that all subsequent tests will use.

- [ ] **Step 1: Snapshot the current trained state**

```bash
PHASE15_BACKUP="$HOME/mtg-synergy-backups/2026-04-06-pre-phase15b"
mkdir -p "$PHASE15_BACKUP"
cp data/tags.db                              "$PHASE15_BACKUP/tags.db"
cp data/forge_features_cache.npz             "$PHASE15_BACKUP/forge_features_cache.npz" 2>/dev/null || echo "no feature cache (will be rebuilt)"
cp data/fusion_model_forge.lgb               "$PHASE15_BACKUP/fusion_model_forge.lgb"
cp data/fusion_model_forge.lgb.meta.json     "$PHASE15_BACKUP/fusion_model_forge.lgb.meta.json"
echo "PRE_PHASE15B_BASELINE_NDCG: ~0.5691 (Phase 1 final, branch feat/forge-dsl-phase1@63b7ca9)" \
    > "$PHASE15_BACKUP/BASELINE.txt"
ls -lh "$PHASE15_BACKUP"
```

Expected: 4 files (tags.db ~7.8 GB, model 56 MB, meta 5 KB, baseline txt). The feature cache may or may not exist depending on whether the project has been retrained recently.

- [ ] **Step 2: Verify the branch is the right one**

```bash
git branch --show-current
git log --oneline -3
```

Expected: `feat/forge-dsl-phase15b-static-modes`, with the spec commit `e69f429` at HEAD and `63b7ca9` (Phase 1 final) just before it. If the branch is wrong, STOP.

- [ ] **Step 3: Sample real S: rows from the corpus**

```bash
sqlite3 data/tags.db "SELECT card_name, raw_line FROM forge_abilities WHERE ability_type='S' LIMIT 20;"
```

Read each result. Note the format of `Mode$ <value>` in raw_line. Verify the modes are space-separated single tokens (e.g., `Mode$ Continuous`, `Mode$ Panharmonicon`), not concatenated forms or SVar references.

- [ ] **Step 4: Get the top 30 Mode$ values by frequency**

The `verb` column currently holds Mode$ values for S: rows due to the existing pollution bug. Query it to discover the vocabulary:

```bash
sqlite3 data/tags.db "SELECT verb, COUNT(*) FROM forge_abilities WHERE ability_type='S' AND verb IS NOT NULL GROUP BY verb ORDER BY COUNT(*) DESC LIMIT 30;"
```

Record the top 10 in your notes — these become the test inputs in Task 6 onwards. Expected magnitude: largest entries should have hundreds to thousands of rows. Total S: row count should be ~6,000-12,000 (the audit said ~6,000 cards but cards can have multiple S: lines).

- [ ] **Step 5: Get S: row count and distinct card count for sanity baseline**

```bash
sqlite3 data/tags.db "SELECT COUNT(*) AS s_rows, COUNT(DISTINCT card_name) AS s_cards FROM forge_abilities WHERE ability_type='S';"
```

Record both numbers. After re-import in Task 5, these must match exactly (same Forge data, same parsing of A:/T:/R: lines, only the S: column assignment changes).

- [ ] **Step 6: Sample S: lines for the 5 archetype-target commanders**

```bash
sqlite3 data/tags.db "SELECT card_name, raw_line FROM forge_abilities WHERE ability_type='S' AND card_name IN ('Panharmonicon', 'Urza, Lord High Artificer', 'Adriana, Captain of the Guard', 'Yidris, Maelduke of Chaos', 'Sun Quan, Lord of Wu') ORDER BY card_name;"
```

These five rows are the ground-truth references for Task 11's per-commander validation. Save the output to your notes. If any of these commanders has zero S: rows, the validation target list needs adjustment — surface to the controller.

- [ ] **Step 7: Confirm corpus is sane and ready to extract**

You should now have:
- A backup at `~/mtg-synergy-backups/2026-04-06-pre-phase15b/`
- A list of the top 10 Mode$ values
- Total S: row count and distinct card count
- Verbatim raw_line samples for the 5 target commanders
- 20 random S: row samples for use as test inputs

**No commit in this task.** Filesystem operations only.

---

## Task 2: Schema migration in `forge_import.py` (TDD)

**Files:**
- Modify: `packages/mtg-synergy-train/src/mtg_synergy_train/parse/forge_import.py:15-52` (`ensure_forge_schema`), `:55-67` (`_parse_kv_line`), `:94-200` (`extract_ability_fields`), `:316-340` (`import_card_to_db`)
- Modify: `tests/conftest.py:18-43` (mirror schema)
- Modify: `tests/test_schema.py` (add column assertion)
- Test: existing `tests/test_forge_import.py` for the parser change

- [ ] **Step 1: Write the failing schema test**

Append to `tests/test_schema.py`:

```python
def test_forge_abilities_has_static_mode_column(tmp_db):
    """Phase 1.5 sub-project B: forge_abilities must include static_mode column."""
    conn = sqlite3.connect(tmp_db)
    cols = [r[1] for r in conn.execute(
        "PRAGMA table_info(forge_abilities)"
    ).fetchall()]
    conn.close()
    assert "static_mode" in cols, f"static_mode missing from columns: {cols}"
```

- [ ] **Step 2: Run the test, confirm it fails**

```bash
uv run pytest tests/test_schema.py::test_forge_abilities_has_static_mode_column -v
```

Expected: FAIL with `AssertionError: static_mode missing from columns: ['card_name', 'ability_index', ..., 'raw_line']` because `tests/conftest.py` does not yet include `static_mode` in its forge_abilities CREATE.

- [ ] **Step 3: Update `tests/conftest.py` to mirror the new schema**

In `tests/conftest.py`, find the forge_abilities CREATE TABLE inside the `tmp_db` fixture (line 19-42). Add `static_mode TEXT,` immediately before `raw_line TEXT NOT NULL`:

```python
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
            static_mode TEXT,
            raw_line TEXT NOT NULL,
            PRIMARY KEY (card_name, ability_index)
        );
```

- [ ] **Step 4: Run the schema test, confirm it passes**

```bash
uv run pytest tests/test_schema.py::test_forge_abilities_has_static_mode_column -v
```

Expected: PASS.

- [ ] **Step 5: Update `forge_import.py` `ensure_forge_schema` for migration**

In `packages/mtg-synergy-train/src/mtg_synergy_train/parse/forge_import.py`, replace the `ensure_forge_schema` function (currently lines 15-52). Old version creates the table only if not exists. New version detects a stale schema (missing `static_mode` column) and drops + recreates:

```python
def ensure_forge_schema(conn):
    """Create Forge tables.

    Migrates stale schemas by dropping and recreating forge_abilities if
    the static_mode column (Phase 1.5 sub-project B) is missing. The
    project has no migration system; tables are always rebuilt from
    Forge files via import_all().
    """
    existing_cols = {
        r[1] for r in conn.execute("PRAGMA table_info(forge_abilities)")
    }
    if existing_cols and "static_mode" not in existing_cols:
        conn.execute("DROP TABLE forge_abilities")
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
            static_mode TEXT,
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
```

- [ ] **Step 6: Write the failing parser test for the S: branch**

Create `tests/test_forge_features_phase15_static_modes.py`:

```python
"""Phase 1.5 sub-project B — Static Mode$ extraction tests.

Covers:
1. forge_import.py extract_ability_fields S: branch (column extraction)
2. forge_features.py profile loading + auto-tag synthesis (next task)
3. mechanics_vectors.py synthetic event tuple (next task)

All test inputs are verbatim samples from data/tags.db forge_abilities
or data/forge/forge-gui/res/cardsfolder/ files. Do NOT invent S: line
strings — sample them from real corpus.
"""

from __future__ import annotations

from mtg_synergy_train.parse.forge_import import extract_ability_fields


class TestStaticModeColumnExtraction:
    """forge_import.py extract_ability_fields S: branch — column extraction."""

    def test_extracts_continuous_mode(self):
        # Verbatim format: S:Mode$ Continuous | Affected$ ... | AddPower$ ...
        line = "Mode$ Continuous | Affected$ Creature.YouCtrl | AddPower$ 1 | AddToughness$ 1 | Description$ Creatures you control get +1/+1."
        result = extract_ability_fields(line, "S", svars={})
        assert result["static_mode"] == "Continuous"

    def test_extracts_panharmonicon_mode(self):
        line = "Mode$ Panharmonicon | ValidCard$ Permanent.YouCtrl | Description$ If a permanent entering causes a triggered ability of a permanent you control to trigger, that ability triggers an additional time."
        result = extract_ability_fields(line, "S", svars={})
        assert result["static_mode"] == "Panharmonicon"

    def test_extracts_reducecost_mode(self):
        line = "Mode$ ReduceCost | ValidCard$ Artifact.YouCtrl | Type$ Spell | Amount$ 1 | Description$ Artifact spells you cast cost {1} less to cast."
        result = extract_ability_fields(line, "S", svars={})
        assert result["static_mode"] == "ReduceCost"

    def test_non_s_lines_have_null_static_mode(self):
        # A: line — must NOT set static_mode
        a_line = "AB$ Tap | Cost$ T | Defined$ Self | SpellDescription$ Tap CARDNAME."
        a_result = extract_ability_fields(a_line, "A", svars={})
        assert a_result.get("static_mode") is None

        # T: line — must NOT set static_mode
        t_line = "Mode$ ChangesZone | Origin$ Any | Destination$ Battlefield | ValidCard$ Card.Self | Execute$ TrigPump"
        t_result = extract_ability_fields(t_line, "T", svars={})
        assert t_result.get("static_mode") is None

        # R: line — must NOT set static_mode
        r_line = "Event$ Moved | ValidCard$ Card.Self | Destination$ Graveyard | ReplaceWith$ Exile"
        r_result = extract_ability_fields(r_line, "R", svars={})
        assert r_result.get("static_mode") is None

    def test_s_line_no_mode_field_returns_null(self):
        # Defensive: S: line without Mode$ field — should return None, not crash
        line = "SP$ Effect | Description$ Malformed S: line for testing."
        result = extract_ability_fields(line, "S", svars={})
        assert result.get("static_mode") is None
```

- [ ] **Step 7: Run the parser tests, confirm they fail**

```bash
uv run pytest tests/test_forge_features_phase15_static_modes.py::TestStaticModeColumnExtraction -v
```

Expected: 5 failures. The first 3 fail with `KeyError: 'static_mode'` because the result dict doesn't initialize the key. The last 2 use `.get()` so they pass on missing key BUT may fail differently — for `test_non_s_lines_have_null_static_mode` the existing S: branch may write None to verb but doesn't write None to a non-existent static_mode key, so `.get()` returns None and the test PASSES accidentally. **That's fine** — the assertion is correct, the test will hold after implementation.

- [ ] **Step 8: Implement `extract_ability_fields` S: branch fix and result dict init**

In `forge_import.py` `extract_ability_fields` (lines 94-200), update the result dict initializer (around line 98-117). Add `"static_mode": None,` after `"unless_cost": None,`:

```python
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
        "static_mode": None,
    }
```

Then update the S: branch (currently around line 158-159):

```python
    elif prefix == "S":
        # Phase 1.5 sub-project B: Mode$ goes to its own structured column.
        # Previously this stored Mode$ in the verb column, polluting verbs
        # with mode names like "Continuous", "Panharmonicon", "ReduceCost".
        # Drop the SP$ fallback to keep verb semantically pure.
        result["static_mode"] = fields.get("Mode")
```

(The existing line `result["verb"] = fields.get("SP") or fields.get("Mode")` is REPLACED, not augmented. Verb is now `None` for S: rows.)

- [ ] **Step 9: Run the parser tests, confirm they pass**

```bash
uv run pytest tests/test_forge_features_phase15_static_modes.py::TestStaticModeColumnExtraction -v
```

Expected: 5 passed.

- [ ] **Step 10: Update `import_card_to_db` INSERT statement**

In `forge_import.py` `import_card_to_db` (lines 316-340), change the INSERT statement from 20 to 21 placeholders. Add `ab.get("static_mode")` immediately before `ab.get("raw_line", "")`:

```python
def import_card_to_db(conn, card: dict):
    """Insert a parsed card into the forge_* tables."""
    name = card["name"]
    if not name:
        return

    for ab in card["abilities"]:
        conn.execute(
            "INSERT OR REPLACE INTO forge_abilities VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (name, ab["ability_index"], ab["ability_type"], ab.get("verb"),
             ab.get("trigger_mode"), ab.get("trigger_filter"),
             ab.get("trigger_origin"), ab.get("trigger_destination"),
             ab.get("trigger_phase"), ab.get("trigger_zones"),
             ab.get("target"), ab.get("defined"), ab.get("amount"),
             ab.get("cost"), ab.get("keyword"), ab.get("token_script"),
             ab.get("counter_type"), ab.get("sub_ability"),
             ab.get("unless_cost"), ab.get("static_mode"),
             ab.get("raw_line", "")),
        )

    for tag in card["deck_tags"]:
        conn.execute(
            "INSERT OR IGNORE INTO forge_deck_tags VALUES (?,?,?)",
            (name, tag["tag_type"], tag["tag"]),
        )
```

Count the `?` characters: there should be exactly 21 placeholders separated by commas.

- [ ] **Step 11: Run the full test suite to catch any regressions**

```bash
uv run pytest tests/ --tb=short 2>&1 | tail -30
```

Expected: all tests pass. If `tests/test_forge_import.py` has any tests that assert on the S: branch storing in `verb`, they will now fail because the new code stores in `static_mode` instead. **That's the verb-pollution bug being caught — those tests had been validating the bug as if it were correct behavior.** Update them to assert on `static_mode` instead. Specifically grep for any pattern like `assert.*verb.*Continuous` or similar S: + verb assertions and fix.

- [ ] **Step 12: Commit**

```bash
git add packages/mtg-synergy-train/src/mtg_synergy_train/parse/forge_import.py \
        tests/conftest.py \
        tests/test_schema.py \
        tests/test_forge_features_phase15_static_modes.py
# If test_forge_import.py needed updates from step 11:
git add tests/test_forge_import.py 2>/dev/null || true
git commit -F - <<'EOF'
feat(forge): add static_mode column + S: branch parser fix

Phase 1.5 sub-project B layer 1: forge_import.py changes.

Schema migration:
  - forge_abilities gains static_mode TEXT column (positional 19,
    immediately before raw_line)
  - ensure_forge_schema() detects stale schemas via PRAGMA and DROPs
    forge_abilities for recreation when static_mode is missing
  - import_card_to_db INSERT updated from 20 to 21 placeholders

S: branch fix (verb-column pollution bug):
  - Previously: result["verb"] = fields.get("SP") or fields.get("Mode")
    silently dumped Mode$ values into the verb column, mixing modes
    like Continuous/Panharmonicon/ReduceCost with actual verbs like
    Tap/Sacrifice/Mill
  - Now: result["static_mode"] = fields.get("Mode") and verb stays
    None for S: rows. The SP$ fallback is dropped — SP$ on an S: line
    is rare and was previously dumped into verb regardless of meaning

Test infrastructure:
  - tests/conftest.py forge_abilities schema mirrored to include
    static_mode TEXT
  - tests/test_schema.py asserts column presence
  - tests/test_forge_features_phase15_static_modes.py adds 5 parser
    tests using verbatim S: line samples from corpus

Follow-up: re-import live Forge data via scripts/import_forge.py
--import (Task 5 in the implementation plan), then wire the new
column through forge_features.py and mechanics_vectors.py.
EOF
git log --oneline -3
```

---

## Task 3: Forge data re-import

**Files:** none (runtime action against `data/tags.db` — gitignored)

- [ ] **Step 1: Confirm pre-flight backup is in place**

```bash
ls -lh ~/mtg-synergy-backups/2026-04-06-pre-phase15b/
```

Expected: `tags.db`, `fusion_model_forge.lgb`, `fusion_model_forge.lgb.meta.json`, `BASELINE.txt`, optionally `forge_features_cache.npz`. If `tags.db` is missing, STOP and re-run Task 1 step 1.

- [ ] **Step 2: Re-import Forge data**

```bash
mkdir -p logs
python3 scripts/import_forge.py --import 2>&1 | tee logs/2026-04-06-task3-reimport.log
```

Expected runtime: 1-2 minutes. The script reads `data/forge/forge-gui/res/cardsfolder/` (no network), drops the stale `forge_abilities` table, recreates with the new schema, and re-populates. Output ends with "Imported N cards (0 errors)" where N is around 31,000.

- [ ] **Step 3: Verify the schema migration took effect**

```bash
sqlite3 data/tags.db "PRAGMA table_info(forge_abilities);" | grep static_mode
```

Expected: a single line like `19|static_mode|TEXT|0||0`. If empty, the schema migration did not apply — investigate `ensure_forge_schema` and STOP.

- [ ] **Step 4: Verify static_mode is populated for S: rows**

```bash
sqlite3 data/tags.db "SELECT COUNT(*) FROM forge_abilities WHERE ability_type='S' AND static_mode IS NOT NULL;"
sqlite3 data/tags.db "SELECT COUNT(*) FROM forge_abilities WHERE ability_type='S';"
```

Expected: both counts are equal (or nearly so — a tiny number of malformed S: lines without Mode$ may legitimately have `static_mode IS NULL`). If the first count is zero, the S: branch in `extract_ability_fields` did not write the column — investigate Task 2 step 8.

- [ ] **Step 5: Verify verb column is now NULL for S: rows (verb-pollution fix)**

```bash
sqlite3 data/tags.db "SELECT COUNT(*) FROM forge_abilities WHERE ability_type='S' AND verb IS NOT NULL;"
```

Expected: 0 (or very small — only rows where the original `extract_ability_fields` would have populated verb via a non-S/M source, which shouldn't exist). If thousands of rows still have verb populated for S:, the S: branch rewrite was incomplete.

- [ ] **Step 6: Spot-check the top 10 Mode$ values match Task 1 step 4**

```bash
sqlite3 data/tags.db "SELECT static_mode, COUNT(*) FROM forge_abilities WHERE ability_type='S' GROUP BY static_mode ORDER BY COUNT(*) DESC LIMIT 10;"
```

Compare against the top 10 you recorded in Task 1 step 4 (queried from the polluted `verb` column). The lists should be identical — same modes, same counts. If any mode is missing or count differs by more than ±1, something in the parser changed unintentionally.

- [ ] **Step 7: Spot-check the 5 archetype-target commanders**

```bash
sqlite3 data/tags.db "SELECT card_name, static_mode FROM forge_abilities WHERE ability_type='S' AND card_name IN ('Panharmonicon', 'Urza, Lord High Artificer', 'Adriana, Captain of the Guard', 'Yidris, Maelduke of Chaos', 'Sun Quan, Lord of Wu') ORDER BY card_name;"
```

Compare against Task 1 step 6. Each commander should have at least one row, and the static_mode value should match what you'd expect (e.g., Panharmonicon → Panharmonicon, Adriana → Continuous, etc.).

- [ ] **Step 8: No commit (data files are gitignored)**

The `data/tags.db` change is not in git. Move on to Task 4.

---

## Task 4: Audit `FROM forge_abilities` SELECTs

**Files:** read-only audit, no edits in this task

**Goal:** Identify all places in the codebase that read `forge_abilities` so we can verify each one handles the new column correctly. Most should be unaffected because they list columns explicitly, but we cannot ship without the audit.

- [ ] **Step 1: Grep for all SELECTs**

```bash
grep -rn "FROM forge_abilities" packages/ scripts/
```

Record every hit. For each, classify it as:
- **(a) Explicit column list, doesn't read static_mode** → no change needed
- **(b) Explicit column list, should read static_mode** → must add `static_mode` to the list (Task 6)
- **(c) `SELECT *`** → fragile, needs to become explicit column list

Expected hits (approximate, you must verify):
- `packages/mtg-synergy/src/mtg_synergy/recommend/forge_features.py` — `_load_forge_profiles` SELECT (Task 6 will update)
- `packages/mtg-synergy/src/mtg_synergy/recommend/mechanics_vectors.py` — `build_mechanics_vectors` fallback SELECT (Task 8 will update)
- `packages/mtg-synergy-train/src/mtg_synergy_train/causal/forge_indexer.py` — graph builder SELECT (does NOT need static_mode; verify)
- `scripts/strategy_detector.py` — vestigial, reads verb column (does NOT need static_mode)
- `scripts/compare_strategy_vs_mech.py` — analysis script, reads card_strategies not forge_abilities (should not appear)

- [ ] **Step 2: For each `SELECT *` (if any), make it explicit**

If any hit uses `SELECT *`, the new column shifts positional unpacking and silently breaks downstream code. Convert each to an explicit column list. If you find none, skip to step 3.

- [ ] **Step 3: For each explicit list, classify and document**

Write your audit findings to a temporary scratch file (do NOT commit). For example:

```
forge_features.py:497-503 — explicit list, will be updated in Task 6 to add static_mode
mechanics_vectors.py:438-442 — explicit list, will be updated in Task 8 to add static_mode
forge_indexer.py:N — explicit list, reads verb/trigger_mode/trigger_filter only, does NOT need static_mode
strategy_detector.py:N — vestigial, reads verb only, does NOT need static_mode
```

- [ ] **Step 4: No commit (audit only)**

Carry the findings into Task 6 and Task 8.

---

## Task 5: Wire `static_mode` through `forge_features.py` (TDD)

**Files:**
- Modify: `packages/mtg-synergy/src/mtg_synergy/recommend/forge_features.py:347-365` (`__init__` schema check), `:485-506` (`_load_forge_profiles` SELECT + tuple), `:517-596` (`_process_forge_ability_row` row destructuring + S: branch + profile init), `:443-483` (`_compact_forge_profiles`)
- Modify: `tests/test_forge_features_phase15_static_modes.py` (add profile-loading test class)

**Goal:** Single commit that adds `static_mode` column read, populates `ForgeProfile.static_modes` from S: rows, compacts the new field, and adds the `__init__` schema sanity check. This is the entangled bunch from the spec — must land together because the SELECT change and the row destructuring change must be in sync.

- [ ] **Step 1: Write the failing profile-loading test**

Append to `tests/test_forge_features_phase15_static_modes.py`:

```python
import sqlite3

from mtg_synergy.recommend.forge_features import ForgeFeatureContext


def _build_test_db_with_static_mode(db_path: str) -> None:
    """Build a minimal in-memory-style sqlite3 DB with a single S: row.

    Mirrors the schema from tests/conftest.py exactly so ForgeFeatureContext
    can construct a context against it.
    """
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE cards (
            oracle_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            type_line TEXT,
            cmc REAL,
            color_identity TEXT,
            mana_cost TEXT,
            power TEXT,
            toughness TEXT,
            oracle_text TEXT,
            loyalty TEXT,
            rarity TEXT DEFAULT '',
            edhrec_rank INTEGER,
            legal_commander INTEGER DEFAULT 1,
            role TEXT DEFAULT ''
        );
        CREATE TABLE forge_abilities (
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
            static_mode TEXT,
            raw_line TEXT NOT NULL,
            PRIMARY KEY (card_name, ability_index)
        );
        CREATE TABLE forge_name_map (
            forge_name TEXT PRIMARY KEY,
            oracle_id TEXT NOT NULL
        );
        CREATE TABLE forge_deck_tags (
            card_name TEXT NOT NULL,
            tag_type TEXT NOT NULL,
            tag TEXT NOT NULL,
            PRIMARY KEY (card_name, tag_type, tag)
        );
        CREATE TABLE interaction_edges (
            source_id TEXT NOT NULL,
            target_id TEXT NOT NULL,
            edge_type TEXT NOT NULL,
            ability_a INTEGER NOT NULL,
            ability_b INTEGER NOT NULL,
            strength REAL NOT NULL,
            detail TEXT NOT NULL,
            filter_precision TEXT,
            PRIMARY KEY (source_id, target_id, edge_type, ability_a, ability_b)
        );

        INSERT INTO cards (oracle_id, name, type_line, cmc, color_identity)
        VALUES ('test-panharmonicon', 'Test Panharmonicon', 'Artifact', 4, '[]');

        INSERT INTO forge_name_map (forge_name, oracle_id)
        VALUES ('Test Panharmonicon', 'test-panharmonicon');

        INSERT INTO forge_abilities VALUES (
            'Test Panharmonicon', 0, 'S', NULL, NULL, NULL, NULL, NULL,
            NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL,
            NULL, 'Panharmonicon',
            'S:Mode$ Panharmonicon | ValidCard$ Permanent.YouCtrl | Description$ ETB doubler.'
        );
    """)
    conn.commit()
    conn.close()


class TestStaticModeProfileLoading:
    """forge_features.py — static_mode column flows into ForgeProfile."""

    def test_static_mode_populates_profile_set(self, tmp_path):
        db_path = str(tmp_path / "test_static_mode.db")
        _build_test_db_with_static_mode(db_path)
        conn = sqlite3.connect(db_path)
        ctx = ForgeFeatureContext(conn, preload_edges=False)
        profile = ctx._forge_profiles.get("test-panharmonicon")
        assert profile is not None, "Profile not built for test card"
        assert "static_modes" in profile, "Profile missing static_modes field"
        assert "Panharmonicon" in profile["static_modes"], (
            f"Expected Panharmonicon in static_modes, got: {profile['static_modes']}"
        )

    def test_static_mode_compaction_to_frozenset(self, tmp_path):
        db_path = str(tmp_path / "test_compact.db")
        _build_test_db_with_static_mode(db_path)
        conn = sqlite3.connect(db_path)
        ctx = ForgeFeatureContext(conn, preload_edges=False)
        profile = ctx._forge_profiles["test-panharmonicon"]
        # _compact_forge_profiles is called in __init__ — verify the field is frozenset
        assert isinstance(profile["static_modes"], frozenset), (
            f"Expected frozenset, got {type(profile['static_modes']).__name__}"
        )

    def test_schema_check_raises_on_missing_static_mode_column(self, tmp_path):
        """Sanity safety net: clear error message when DB is stale."""
        db_path = str(tmp_path / "test_stale.db")
        conn = sqlite3.connect(db_path)
        # Build forge_abilities WITHOUT the static_mode column to simulate
        # a stale dev DB that hasn't been re-imported.
        conn.executescript("""
            CREATE TABLE cards (oracle_id TEXT PRIMARY KEY, name TEXT,
                type_line TEXT, cmc REAL, color_identity TEXT);
            CREATE TABLE forge_abilities (
                card_name TEXT NOT NULL,
                ability_index INTEGER NOT NULL,
                ability_type TEXT NOT NULL,
                raw_line TEXT NOT NULL,
                PRIMARY KEY (card_name, ability_index)
            );
            CREATE TABLE forge_name_map (forge_name TEXT, oracle_id TEXT);
            CREATE TABLE forge_deck_tags (card_name TEXT, tag_type TEXT, tag TEXT);
            CREATE TABLE interaction_edges (source_id TEXT, target_id TEXT,
                edge_type TEXT, ability_a INTEGER, ability_b INTEGER,
                strength REAL, detail TEXT);
        """)
        conn.commit()
        import pytest
        with pytest.raises(RuntimeError, match=r"static_mode.*Phase 1\.5"):
            ForgeFeatureContext(conn, preload_edges=False)

    def test_non_s_rows_dont_pollute_static_modes(self, tmp_path):
        """Verb-pollution regression: A:/T:/R: rows must NOT add to static_modes."""
        db_path = str(tmp_path / "test_no_pollution.db")
        _build_test_db_with_static_mode(db_path)
        conn = sqlite3.connect(db_path)
        # Add an A: row with the same card_name
        conn.execute(
            "INSERT INTO forge_abilities VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("Test Panharmonicon", 1, "A", "Tap", None, None, None, None,
             None, None, None, "Self", None, "T", None, None, None, None,
             None, None, "A:AB$ Tap | Cost$ T | Defined$ Self"),
        )
        conn.commit()
        ctx = ForgeFeatureContext(conn, preload_edges=False)
        profile = ctx._forge_profiles["test-panharmonicon"]
        # Only "Panharmonicon" should be in static_modes, NOT "Tap"
        assert profile["static_modes"] == frozenset({"Panharmonicon"}), (
            f"static_modes contaminated by non-S row: {profile['static_modes']}"
        )
        # And "Tap" should be in verbs (the A: row's verb)
        assert "Tap" in profile["verbs"], (
            f"A: row's Tap verb not captured: {profile['verbs']}"
        )
```

- [ ] **Step 2: Run the tests, confirm they fail**

```bash
uv run pytest tests/test_forge_features_phase15_static_modes.py::TestStaticModeProfileLoading -v
```

Expected: 4 failures. The first three fail with `KeyError: 'static_modes'` (profile doesn't have the field) or `IndexError` (row tuple too short for the new column). The schema check test fails because no schema check is implemented yet.

- [ ] **Step 3: Add schema sanity check method to `ForgeFeatureContext`**

In `forge_features.py`, find `ForgeFeatureContext.__init__` (line 347). Immediately after `self.conn = conn`, add a call to a new method:

```python
    def __init__(self, conn, preload_edges=False, preload_strength=False,
                 card_provider=None, artifact_dir=None):
        self.conn = conn
        self._check_schema(conn)
        self._has_edge_index = False
        # ... rest of __init__ unchanged
```

Then add the method definition next to other helper methods (suggest near the bottom of the class, before the static helpers):

```python
    def _check_schema(self, conn):
        """Verify the DB schema includes Phase 1.5 sub-project B columns.

        Surfaces a clear error message if the developer forgot to re-run
        scripts/import_forge.py after pulling the schema migration.
        """
        cols = {r[1] for r in conn.execute("PRAGMA table_info(forge_abilities)")}
        if "static_mode" not in cols:
            raise RuntimeError(
                "forge_abilities is missing the 'static_mode' column "
                "(Phase 1.5 sub-project B). Re-import Forge data: "
                "python3 scripts/import_forge.py --import"
            )
```

- [ ] **Step 4: Update `_load_forge_profiles` SELECT to include `static_mode`**

In `forge_features.py` `_load_forge_profiles` (line 485-506), update the SELECT and the `_raw_abilities` tuple. The new SELECT adds `fa.static_mode` at the end:

```python
    def _load_forge_profiles(self, conn):
        """Load forge ability profiles and raw abilities from DB."""
        # Forge ability profiles: per-card structured data from forge_abilities
        # Replaces oracle text regex matching for features F25-F30
        self._forge_profiles = {}
        self._verb_counts = {}  # oracle_id → Counter of verb occurrences (for concentration)
        # Also collect raw abilities for build_mechanics_vectors (avoids redundant DB scan)
        # Output format consumed by mechanics_vectors.py:
        #   (oid, verb, trig_mode, trig_filter, cost, kw, token_script,
        #    counter, raw_line, amount, trigger_origin, trigger_destination,
        #    defined, static_mode)
        self._raw_abilities = []
        for row in conn.execute(
            "SELECT fnm.oracle_id, fa.verb, fa.trigger_mode, fa.trigger_filter, "
            "fa.cost, fa.keyword, fa.token_script, fa.counter_type, fa.raw_line, "
            "fa.amount, fa.trigger_origin, fa.trigger_destination, "
            "fa.target, fa.ability_type, fa.defined, fa.static_mode "
            "FROM forge_abilities fa "
            "JOIN forge_name_map fnm ON fnm.forge_name = fa.card_name"
        ):
            # row[0..11] + row[14] (defined) + row[15] (static_mode) → 14-element tuple
            self._raw_abilities.append(row[:12] + (row[14], row[15]))
            self._process_forge_ability_row(row)

        # Post-process derived fields
        for p in self._forge_profiles.values():
            # Derive grants_abilities boolean from detailed set
            p['grants_abilities'] = bool(p['granted_ability_names'])
            # Affected$ scope ratio: fraction of effects targeting self vs opponents
            a_total = p['affected_self_count'] + p['affected_opp_count']
            p['affected_scope_ratio'] = (p['affected_self_count'] / a_total
                                         if a_total > 0 else 0.5)
```

Note the SELECT now has 16 columns (added `fa.static_mode` at the end as index 15), and the `_raw_abilities` tuple is now 14 elements (indices 0-11 from SELECT, then index 14 for `defined`, then index 15 for `static_mode`).

- [ ] **Step 5: Update `_process_forge_ability_row` row destructuring and add static_mode handling**

In `forge_features.py` `_process_forge_ability_row` (line 517+), add the new local variable destructuring at the top and update the profile dict initializer to include `static_modes`. Also add the S: branch handling.

Find the method's existing destructuring (line 519-532):

```python
    def _process_forge_ability_row(self, row):
        """Process a single forge_abilities row into the profile dict."""
        # Named references for profile building (SELECT order matches tuple order)
        oid = row[0]
        verb = row[1]
        trig_mode = row[2]
        trig_filter = row[3]
        cost = row[4]
        keyword = row[5]
        # row[6] = token_script (used by _raw_abilities only)
        counter_type = row[7]
        raw_line_val = row[8]
        # row[9] = amount, row[10] = trigger_origin, row[11] = trigger_destination
        target = row[12]
        ability_type = row[13]
        defined = row[14]
        static_mode = row[15]  # Phase 1.5 sub-project B
```

Find the profile dict initializer (line 534-555). Add `'static_modes': set(),` next to other set fields:

```python
        p = self._forge_profiles.setdefault(oid, {
            'verbs': set(), 'triggers': set(), 'keywords': set(),
            'counter_types': set(), 'targets': set(), 'ability_types': set(),
            'trigger_filters': set(), 'required_subtypes': set(),
            'granted_keywords': set(), 'conditions': set(),
            'duration': set(), 'combat_damage': False,
            'effect_zones': set(),
            'damage_amount': None,
            'cards_drawn': None, 'life_amount': None,
            'is_secondary': False, 'gain_control': False,
            'produces_mana': False, 'grants_abilities': False,
            'token_amount_variable': False,
            'excluded_subtypes': set(),
            'counters_on_lands': False,
            'counter_trigger_themes': set(), 'has_p1p1': False,
            'opponent_only_events': set(),
            'affected_self_count': 0, 'affected_opp_count': 0,
            'granted_ability_names': set(), 'granted_triggers': set(),
            'changes_type': set(), 'grants_all_creature_types': False,
            'max_pump_power': 0, 'pump_is_variable': False,
            'cost_types': set(),
            'raw_trigger_filters': set(),
            'static_modes': set(),  # Phase 1.5 sub-project B: S: line Mode$ values
        })
```

After the existing field-population logic (after `if ability_type: p['ability_types'].add(ability_type)`), add static_mode population. Find a stable insertion point near the top of the field-population section and add:

```python
        # Phase 1.5 sub-project B: static modes from S: lines
        if static_mode:
            p['static_modes'].add(static_mode)
```

(Place this immediately after the `if ability_type:` line so the logic flows naturally with other field assignments.)

- [ ] **Step 6: Update `_compact_forge_profiles` to compact the new set**

In `forge_features.py` `_compact_forge_profiles` (line 443-483), add `'static_modes'` to the compaction list. Find the existing list of fields being converted to frozenset and append:

The current compaction code looks something like (based on existing patterns):

```python
    def _compact_forge_profiles(self):
        """Replace mutable sets with deduplicated frozensets to save RSS."""
        # ... existing code that iterates fields and converts to frozenset
```

Read the actual implementation (lines 443-483) and add `'static_modes'` to whatever list of field names it uses. If the implementation uses an explicit list of fields, append it. If it uses `isinstance(v, set)` to detect sets, no change is needed because the new field is already a `set` and will be auto-detected.

- [ ] **Step 7: Run the profile-loading tests, confirm they pass**

```bash
uv run pytest tests/test_forge_features_phase15_static_modes.py::TestStaticModeProfileLoading -v
```

Expected: 4 passed. If `test_static_mode_compaction_to_frozenset` fails because the field is still a `set`, the compaction step (Task 5 step 6) needs explicit handling — add `'static_modes'` to the compaction field list.

- [ ] **Step 8: Run the full test suite**

```bash
uv run pytest tests/ --tb=short 2>&1 | tail -30
```

Expected: 188+ tests pass (183 baseline from Phase 1 + 5 from Task 2 + 4 from this task = 192). If any existing test in `test_forge_features.py` fails because of the row tuple shape change, audit it carefully — the SELECT explicitly names columns so positional indices should be preserved for indices 0-14, but a test that constructed a fake row tuple with only 15 elements would now fail because index 15 is missing.

- [ ] **Step 9: Commit**

```bash
git add packages/mtg-synergy/src/mtg_synergy/recommend/forge_features.py \
        tests/test_forge_features_phase15_static_modes.py
git commit -F - <<'EOF'
feat(forge): wire static_mode through ForgeFeatureContext

Phase 1.5 sub-project B layer 2: forge_features.py changes.

Adds:
  - _check_schema sanity guard at __init__ time. Raises a clear
    RuntimeError pointing to scripts/import_forge.py --import if the
    DB is stale (missing static_mode column). Cheap (single PRAGMA).
  - _load_forge_profiles SELECT extended with fa.static_mode at the
    end (now 16 columns). Existing positional indices for indices
    0-14 are preserved because the SELECT explicitly names columns;
    only index 15 is new.
  - _raw_abilities tuple shape extended from 13 to 14 elements:
    row[:12] + (row[14], row[15]). The new index 13 is static_mode,
    consumed by mechanics_vectors.py (next task).
  - _process_forge_ability_row destructures static_mode = row[15]
    and populates p['static_modes'] for S: rows.
  - ForgeProfile gains static_modes: set[str] field, compacted to
    frozenset by _compact_forge_profiles.

Adds 4 new tests in TestStaticModeProfileLoading covering:
  - profile.static_modes populated from S: rows
  - frozenset compaction
  - schema sanity check raises on stale DB
  - verb-pollution regression: A:/T:/R: rows do NOT add to static_modes
EOF
git log --oneline -4
```

---

## Task 6: Auto-tag synthesis in `_load_deck_tags` (TDD)

**Files:**
- Modify: `packages/mtg-synergy/src/mtg_synergy/recommend/forge_features.py:880-940` (`_load_deck_tags` auto-derive loop)
- Modify: `tests/test_forge_features_phase15_static_modes.py` (add auto-tag test)

- [ ] **Step 1: Write the failing auto-tag test**

Append to `tests/test_forge_features_phase15_static_modes.py` (still in `TestStaticModeProfileLoading` class, or add a new `TestStaticModeAutoTags` class if you prefer separation):

```python
class TestStaticModeAutoTags:
    """forge_features.py — static_modes flow into _deck_has as Static$<mode> tags."""

    def test_static_mode_creates_deck_has_tag(self, tmp_path):
        db_path = str(tmp_path / "test_autotag.db")
        _build_test_db_with_static_mode(db_path)
        conn = sqlite3.connect(db_path)
        ctx = ForgeFeatureContext(conn, preload_edges=False)
        tags = ctx._deck_has.get("test-panharmonicon", set())
        assert "Static$Panharmonicon" in tags, (
            f"Expected Static$Panharmonicon in deck_has tags, got: {sorted(tags)}"
        )

    def test_static_tag_in_deck_has_providers_reverse_index(self, tmp_path):
        """The Static$<mode> tag must also appear in _deck_has_providers
        (the tag → set-of-oids reverse index used by needs_rarity F50)."""
        db_path = str(tmp_path / "test_providers.db")
        _build_test_db_with_static_mode(db_path)
        conn = sqlite3.connect(db_path)
        ctx = ForgeFeatureContext(conn, preload_edges=False)
        providers = ctx._deck_has_providers.get("Static$Panharmonicon", set())
        assert "test-panharmonicon" in providers, (
            f"Card not in providers index for Static$Panharmonicon: {providers}"
        )
```

- [ ] **Step 2: Run the test, confirm it fails**

```bash
uv run pytest tests/test_forge_features_phase15_static_modes.py::TestStaticModeAutoTags -v
```

Expected: 2 failures with `assert "Static$Panharmonicon" in ...` because the auto-derive loop in `_load_deck_tags` doesn't yet emit `Static$` tags.

- [ ] **Step 3: Update `_load_deck_tags` auto-derive loop**

In `forge_features.py` `_load_deck_tags` (around line 880-940), find the auto-derive loop that iterates `self._forge_profiles.items()` and adds `Ability$<verb>` tags. Add a new sibling block for `Static$<mode>` tags:

```python
        # Auto-derive deck tags from forge profiles for cards missing them.
        # verb → has (card provides this ability), trigger → hints (card wants this event).
        # Uses verb/trigger names directly as tags (e.g., Ability$Sacrifice, Ability$Token).
        # Trigger normalization map _TRIGGER_STEM_TO_VERB is module-level — see top
        # of file for the maintenance contract.
        #
        # The reverse index self._deck_has_providers is built once in
        # _build_deck_has_providers() AFTER _enrich_deck_tags_from_tokens() has
        # added token Type$ tags. Do not build it here — that would force a
        # second incremental update later and create a hidden ordering bug if
        # the call sequence in __init__ ever changes.
        for oid, profile in self._forge_profiles.items():
            for verb in profile.get('verbs', set()):
                tag = f"Ability${verb}"
                self._deck_has.setdefault(oid, set()).add(tag)
            for trig in profile.get('triggers', set()):
                stem = _TRIGGER_STEM_TO_VERB.get(trig)
                if stem:
                    tag = f"Ability${stem}"
                    self._deck_hints.setdefault(oid, set()).add(tag)
            # Phase 1.5 sub-project B: Static$<mode> tags from S: line Mode$ values.
            # Static modes are properties a card HAS (provides), not properties
            # a card WANTS — they go in _deck_has, not _deck_hints.
            for mode in profile.get('static_modes', set()):
                self._deck_has.setdefault(oid, set()).add(f"Static${mode}")
            # Type$ tags from trigger_filters (card triggers on specific types)
            for tf in profile.get('trigger_filters', set()):
                if tf not in _GENERIC_TRIGGER_FILTER_TYPES and len(tf) > 2:
                    self._deck_hints.setdefault(oid, set()).add(f"Type${tf.title()}")
```

The new lines are the `# Phase 1.5 sub-project B:` block in the middle. Add it between the existing trigger-stem block and the trigger_filters block.

- [ ] **Step 4: Run the auto-tag tests, confirm they pass**

```bash
uv run pytest tests/test_forge_features_phase15_static_modes.py::TestStaticModeAutoTags -v
```

Expected: 2 passed.

- [ ] **Step 5: Run the full test suite**

```bash
uv run pytest tests/ --tb=short 2>&1 | tail -10
```

Expected: 190+ passed, no regressions.

- [ ] **Step 6: Commit**

```bash
git add packages/mtg-synergy/src/mtg_synergy/recommend/forge_features.py \
        tests/test_forge_features_phase15_static_modes.py
git commit -F - <<'EOF'
feat(forge): auto-derive Static$<mode> deck tags from S: line modes

Phase 1.5 sub-project B layer 2 (continued): _load_deck_tags now
auto-derives Static$<mode> tags from each card's profile.static_modes
set, mirroring the existing Ability$<verb> auto-tag pattern.

Static modes flow through 5 existing aggregate features:
  - deck_has_overlap (cmdr ↔ card shared static modes)
  - deck_has_to_hints
  - deck_hints_to_has
  - card_needs_satisfied
  - cmdr_needs_to_card_has

Plus needs_rarity (F50) via _deck_has_providers reverse index, which
is built in _build_deck_has_providers() AFTER all tag enrichment is
complete. The Static$<mode> tags are picked up automatically by the
single-pass index build.

No new GBM feature columns. Vocabulary collision check: Static$ is
unused by existing tag prefixes (Ability$, Type$, Strategy$, Theme$).

Adds 2 new tests in TestStaticModeAutoTags.
EOF
git log --oneline -5
```

---

## Task 7: `mechanics_vectors.py` integration (TDD)

**Files:**
- Modify: `packages/mtg-synergy/src/mtg_synergy/recommend/mechanics_vectors.py:123-150` (`_EVENT_CATEGORY` dict), `:395-405` (category dispatch elif chain), `:430-505` (`build_mechanics_vectors` row processing — both code paths)
- Modify: `tests/test_forge_features_phase15_static_modes.py` (add mechanics vectors test class)

- [ ] **Step 1: Write the failing mechanics vectors test**

Append to `tests/test_forge_features_phase15_static_modes.py`:

```python
class TestStaticModeMechanicsVectors:
    """mechanics_vectors.py — static_mode produces synthetic event tuples."""

    def test_static_mode_event_tuple_in_produces(self, tmp_path):
        from mtg_synergy.recommend.mechanics_vectors import build_mechanics_vectors
        db_path = str(tmp_path / "test_mech.db")
        _build_test_db_with_static_mode(db_path)
        conn = sqlite3.connect(db_path)

        # Build via the preloaded path (mirrors how forge_features.py calls it)
        ctx = ForgeFeatureContext(conn, preload_edges=False)
        from mtg_synergy.recommend.mechanics_vectors import build_mechanics_vectors
        produces, consumes, dim, subtype_idx, category_dims = build_mechanics_vectors(
            conn, preloaded_abilities=ctx._raw_abilities, quiet=True
        )
        # The vector for the test card must have a non-zero entry corresponding
        # to ("static_mode", "panharmonicon", None, None)
        vec = produces.get("test-panharmonicon")
        assert vec is not None, "Card has no produces vector"
        assert vec.sum() > 0, "Produces vector is all zeros — static_mode tuple not added"

    def test_static_mode_qualifier_lowercased(self, tmp_path):
        """The qualifier in the synthetic tuple must be lowercased
        (Panharmonicon → 'panharmonicon')."""
        from mtg_synergy.recommend.mechanics_vectors import build_mechanics_vectors
        db_path = str(tmp_path / "test_lower.db")
        _build_test_db_with_static_mode(db_path)
        conn = sqlite3.connect(db_path)
        ctx = ForgeFeatureContext(conn, preload_edges=False)
        produces, consumes, dim, subtype_idx, category_dims = build_mechanics_vectors(
            conn, preloaded_abilities=ctx._raw_abilities, quiet=True
        )
        # The "themes" category must contain a non-zero dim for our test card
        themes_dims = category_dims.get("themes", [])
        assert themes_dims, "themes category has no dims registered"
        vec = produces["test-panharmonicon"]
        themes_sum = sum(vec[i] for i in themes_dims)
        assert themes_sum > 0, (
            f"static_mode panharmonicon should add to themes category, "
            f"but vec sums to {themes_sum} across themes dims {themes_dims}"
        )

    def test_static_mode_not_in_consumes(self, tmp_path):
        """Static modes are always self-produced; never consumes."""
        from mtg_synergy.recommend.mechanics_vectors import build_mechanics_vectors
        db_path = str(tmp_path / "test_no_consumes.db")
        _build_test_db_with_static_mode(db_path)
        conn = sqlite3.connect(db_path)
        ctx = ForgeFeatureContext(conn, preload_edges=False)
        produces, consumes, dim, subtype_idx, category_dims = build_mechanics_vectors(
            conn, preloaded_abilities=ctx._raw_abilities, quiet=True
        )
        # The card may or may not have a consumes vector (depending on whether
        # it has any other consume-side events). If it does, the static_mode
        # contribution must be zero. Easiest check: the consumes vector should
        # not have grown because of the static mode.
        cvec = consumes.get("test-panharmonicon")
        if cvec is not None:
            # The S: line has no other event-class signal, so consumes should be
            # entirely zero for a card whose only ability is a static mode.
            assert cvec.sum() == 0, (
                f"static_mode card has non-zero consumes vector: {cvec.sum()}"
            )

    def test_static_mode_category_in_event_category_dict(self):
        """_EVENT_CATEGORY dict must include 'static_mode' → 'themes'."""
        from mtg_synergy.recommend.mechanics_vectors import _EVENT_CATEGORY
        assert _EVENT_CATEGORY.get("static_mode") == "themes", (
            f"Expected _EVENT_CATEGORY['static_mode'] == 'themes', "
            f"got {_EVENT_CATEGORY.get('static_mode')!r}"
        )
```

- [ ] **Step 2: Run the tests, confirm they fail**

```bash
uv run pytest tests/test_forge_features_phase15_static_modes.py::TestStaticModeMechanicsVectors -v
```

Expected: 4 failures. The first three fail because `_raw_abilities` is now 14-element but `build_mechanics_vectors` only unpacks 13 elements (or because the synthetic tuple isn't added). The last fails because `_EVENT_CATEGORY` doesn't have `"static_mode"`.

- [ ] **Step 3: Update `_EVENT_CATEGORY` dict**

In `mechanics_vectors.py` (line 123-150), add `"static_mode": "themes"` at the end of the dict:

```python
_EVENT_CATEGORY = {
    "enters":        "board",
    "zone_change":   "board",
    "destroy":       "board",
    "sacrifice":     "board",
    "token_created": "board",
    "damage":        "resource",
    "draw":          "resource",
    "counter_add":   "resource",
    "counter_remove": "resource",
    "life_gain":     "resource",
    "life_lose":     "resource",
    "discard":       "disruption",
    "mill":          "disruption",
    "target":        "disruption",
    "counter_spell": "disruption",
    "spell_cast":    "tempo",
    "attacks":       "tempo",
    "blocks":        "tempo",
    "tap":           "utility",
    "untap":         "utility",
    "pump":          "utility",
    "mana":          "utility",
    "equip":         "themes",
    "attach":        "themes",
    "etb_doubled":   "themes",
    "phase":         "utility",
    "static_mode":   "themes",  # Phase 1.5 sub-project B
}
```

- [ ] **Step 4: Update the category dispatch elif chain**

In `mechanics_vectors.py` (line 395-405), find the elif chain that promotes specific event_classes to themes:

```python
        ec = t[0]
        from_z, to_z = t[2], t[3]
        if ec == "zone_change" and (from_z or to_z):
            category_dims["zones"].append(idx)
        elif ec in ("equip", "attach", "etb_doubled", "defender"):
            category_dims["themes"].append(idx)
        elif ec == "available":
            category_dims["board"].append(idx)
        else:
            cat = _EVENT_CATEGORY.get(ec, "utility")
            category_dims[cat].append(idx)
```

Add `"static_mode"` to the themes elif tuple (belt-and-suspenders with the dict entry above):

```python
        elif ec in ("equip", "attach", "etb_doubled", "defender", "static_mode"):
            category_dims["themes"].append(idx)
```

- [ ] **Step 5: Update `build_mechanics_vectors` preloaded path tuple shape**

In `mechanics_vectors.py` `build_mechanics_vectors` (line 410+), the function consumes ability tuples. The preloaded path is straightforward — it just iterates whatever is passed in. The fallback DB path constructs tuples from a SELECT.

Find the row processing loop (around line 478+):

```python
    for ab in abilities:
        oid = ab[0]
        verb, trig_mode, trig_filter = ab[1], ab[2], ab[3]
        keyword = ab[5]
        token_script = ab[6]
        raw_line = ab[8] or ""
        trig_origin = ab[10] if len(ab) > 10 else None
```

Add destructuring for the new static_mode element. Since the tuple is now 14 elements (from forge_features.py's `row[:12] + (row[14], row[15])`), the `defined` field is at index 12 and `static_mode` is at index 13:

```python
    for ab in abilities:
        oid = ab[0]
        verb, trig_mode, trig_filter = ab[1], ab[2], ab[3]
        keyword = ab[5]
        token_script = ab[6]
        raw_line = ab[8] or ""
        trig_origin = ab[10] if len(ab) > 10 else None
        # Phase 1.5 sub-project B: defined is index 12, static_mode is index 13
        # (forge_features.py builds row[:12] + (row[14], row[15]) → 14-element tuple)
        defined = ab[12] if len(ab) > 12 else None
        static_mode = ab[13] if len(ab) > 13 else None
```

(Note: the existing code may already destructure `defined` from a different position; preserve whatever it does. Add `static_mode` with the safe length guard so the code still works for legacy 13-element tuples in case any test uses the old format.)

Then at the end of the row-processing loop, add the synthetic event tuple:

```python
        # ... existing event-tuple extraction logic ...

        # Phase 1.5 sub-project B: static modes from S: lines
        if static_mode:
            mode_qualifier = static_mode.lower()
            tup = ("static_mode", mode_qualifier, None, None)
            _add_tuple(_ensure_p(oid), tup)
```

(The exact location depends on the structure of the existing loop. Place it after all other tuple-emission logic so static modes are an additive contribution.)

- [ ] **Step 6: Update `build_mechanics_vectors` fallback DB path**

In `mechanics_vectors.py` (line 437-448), update the fallback DB SELECT and tuple construction. The current code:

```python
        abilities = []
        for row in conn.execute(
            "SELECT card_name, verb, trigger_mode, trigger_filter, cost, "
            "keyword, token_script, counter_type, raw_line, amount, "
            "trigger_origin, trigger_destination, defined "
            "FROM forge_abilities"
        ):
            oid = forge_to_oid.get(row[0])
            if oid:
                abilities.append((oid, row[1], row[2], row[3], row[4],
                                  row[5], row[6], row[7], row[8], row[9],
                                  row[10], row[11], row[12]))
```

Add `static_mode` to the SELECT and the tuple:

```python
        abilities = []
        for row in conn.execute(
            "SELECT card_name, verb, trigger_mode, trigger_filter, cost, "
            "keyword, token_script, counter_type, raw_line, amount, "
            "trigger_origin, trigger_destination, defined, static_mode "
            "FROM forge_abilities"
        ):
            oid = forge_to_oid.get(row[0])
            if oid:
                abilities.append((oid, row[1], row[2], row[3], row[4],
                                  row[5], row[6], row[7], row[8], row[9],
                                  row[10], row[11], row[12], row[13]))
```

The result is a 14-element tuple matching the preloaded path.

- [ ] **Step 7: Update `_build_concept_vocabulary` to include static modes**

The vocabulary build is at `_build_concept_vocabulary` (called from `build_mechanics_vectors`). It iterates abilities and collects event tuples to build the concept dict. Verify it also processes the new static_mode field — likely it iterates the same row tuples as the row-processing loop. If it doesn't already extract static_mode tuples, add the same `if static_mode:` block to it.

The key is that `tuple_to_idx` (built by `_build_concept_vocabulary`) must contain `("static_mode", <mode>, None, None)` for the row-processing loop's `_add_tuple` call to actually add the contribution. If the vocabulary doesn't include the tuple, `_add_tuple`'s `tuple_to_idx.get(t)` returns `None` and the contribution is silently dropped.

Read `_build_concept_vocabulary` and verify it processes ability tuples in the same shape as the row-processing loop. Add the same `static_mode` handling there.

- [ ] **Step 8: Run the mechanics vectors tests, confirm they pass**

```bash
uv run pytest tests/test_forge_features_phase15_static_modes.py::TestStaticModeMechanicsVectors -v
```

Expected: 4 passed. If `test_static_mode_event_tuple_in_produces` fails because the vector is all zeros, the vocabulary build (step 7) is missing — go back and audit.

- [ ] **Step 9: Run the full test suite**

```bash
uv run pytest tests/ --tb=short 2>&1 | tail -10
```

Expected: 194+ passed, no regressions. If `test_mechanics_vectors.py` has any tests that compare expected vector dimensions, they may fail because the dim count grew by ~60 (one per Mode$). Update those tests to either tolerate the new dim count or assert on the specific events they care about.

- [ ] **Step 10: Commit**

```bash
git add packages/mtg-synergy/src/mtg_synergy/recommend/mechanics_vectors.py \
        tests/test_forge_features_phase15_static_modes.py
git commit -F - <<'EOF'
feat(forge): mechanics_vectors integration for static_mode

Phase 1.5 sub-project B layer 3: mechanics_vectors.py changes.

Adds:
  - _EVENT_CATEGORY['static_mode'] = 'themes'
  - Category dispatch elif chain promotes 'static_mode' to themes
    (belt-and-suspenders with the dict entry)
  - build_mechanics_vectors preloaded path destructures static_mode
    from index 13 of the 14-element tuple
  - build_mechanics_vectors fallback DB path SELECT extended with
    static_mode and tuple grown to 14 elements
  - _build_concept_vocabulary processes static_mode entries so the
    synthetic tuples ("static_mode", lowercased_mode, None, None)
    are registered in tuple_to_idx
  - Synthetic event tuple emitted to produces vector only (never
    consumes — static modes are self-produced properties)

All static_mode contributions land in the produces vector and the
'themes' category. They flow into:
  - mech_cosine (F3, highest single feature importance)
  - co_producer_score (F31, parallel-mechanic synergy)
  - mech_themes_fwd (F83), mech_themes_rev (F84)

No new GBM feature columns. Vector space grows by ~60 dims (one per
distinct Mode$ value in the corpus).

Adds 4 new tests in TestStaticModeMechanicsVectors.
EOF
git log --oneline -6
```

---

## Task 8: Smoke check on real data

**Files:** none (one-off diagnostic script, NOT committed)

- [ ] **Step 1: Build the smoke-check script**

Create a temporary script `/tmp/phase15b_smoke.py`:

```python
#!/usr/bin/env python3
"""Phase 1.5 sub-project B smoke check.

Verifies the new column flows end-to-end against the real DB:
1. ForgeFeatureContext builds without schema errors
2. Number of cards with non-empty static_modes matches expectations
3. Top 10 modes by card count are reasonable
4. Static$<mode> tags appear in _deck_has for the expected cards
5. mechanics_vectors includes static_mode tuples for known cards

If any check fails, STOP and bisect before retraining.
"""
import sqlite3
from collections import Counter

from mtg_synergy.recommend.forge_features import ForgeFeatureContext
from mtg_synergy.recommend.mechanics_vectors import build_mechanics_vectors


def main() -> None:
    conn = sqlite3.connect("data/tags.db")

    print("Building ForgeFeatureContext (this may take ~5s)...")
    ctx = ForgeFeatureContext(conn, preload_edges=False)
    print("OK — context built without schema errors\n")

    # 1. Cards with static_modes populated
    cards_with_modes = sum(
        1 for p in ctx._forge_profiles.values()
        if p.get('static_modes')
    )
    print(f"Cards with non-empty static_modes: {cards_with_modes}")
    assert 4000 < cards_with_modes < 12000, (
        f"Expected 4000-12000 cards with static_modes, got {cards_with_modes}"
    )
    print("OK — count is in expected range\n")

    # 2. Top 10 Mode$ values by card count
    mode_counts: Counter[str] = Counter()
    for p in ctx._forge_profiles.values():
        for m in p.get('static_modes', frozenset()):
            mode_counts[m] += 1
    print("Top 10 Mode$ values by card count:")
    for mode, n in mode_counts.most_common(10):
        print(f"  {mode:25s}: {n}")
    print()

    # 3. Static$<mode> deck tags
    static_tag_count = sum(
        1 for tags in ctx._deck_has.values()
        if any(t.startswith("Static$") for t in tags)
    )
    print(f"Cards with at least one Static$<mode> tag: {static_tag_count}")
    assert static_tag_count == cards_with_modes, (
        f"Mismatch: {cards_with_modes} cards have static_modes but only "
        f"{static_tag_count} have Static$ tags"
    )
    print("OK — every card with static_modes has a corresponding Static$ tag\n")

    # 4. Spot-check Panharmonicon
    panharm_oid = None
    for row in conn.execute(
        "SELECT oracle_id FROM cards WHERE name = 'Panharmonicon'"
    ):
        panharm_oid = row[0]
        break
    if panharm_oid:
        prof = ctx._forge_profiles.get(panharm_oid)
        if prof:
            print(f"Panharmonicon profile.static_modes: {sorted(prof.get('static_modes', []))}")
            tags = ctx._deck_has.get(panharm_oid, set())
            static_tags = sorted(t for t in tags if t.startswith("Static$"))
            print(f"Panharmonicon Static$ tags: {static_tags}")
        else:
            print("WARNING: Panharmonicon has no Forge profile")
    else:
        print("WARNING: Panharmonicon not in DB")

    # 5. mechanics_vectors integration
    print("\nBuilding mechanics_vectors (this may take ~10s)...")
    produces, consumes, dim, subtype_idx, category_dims = build_mechanics_vectors(
        conn, preloaded_abilities=ctx._raw_abilities, quiet=True
    )
    print(f"Vector dimension: {dim}")
    print(f"Themes category dims: {len(category_dims.get('themes', []))}")
    if panharm_oid and panharm_oid in produces:
        vec = produces[panharm_oid]
        themes_sum = sum(vec[i] for i in category_dims.get("themes", []))
        print(f"Panharmonicon themes contribution: {themes_sum}")
        assert themes_sum > 0, "Panharmonicon should have non-zero themes contribution"
    print("\nAll smoke checks passed.")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run the smoke check**

```bash
uv run python3 /tmp/phase15b_smoke.py 2>&1 | tee logs/2026-04-06-task8-smoke.log
```

Expected output: every assertion passes, "All smoke checks passed." prints at the end. Read the top 10 mode list and confirm it matches your Task 1 step 4 corpus query.

If any assertion fails:
- "schema errors" → `_check_schema` raised; data wasn't re-imported. Re-run Task 3.
- "Expected 4000-12000 cards" out of range → Task 5's static_mode population is broken. Audit `_process_forge_ability_row`.
- "Mismatch" between static_modes count and Static$ tag count → Task 6's auto-tag synthesis is incomplete.
- "Panharmonicon should have non-zero themes contribution" → Task 7's mechanics_vectors integration is broken. Audit the synthetic tuple emission and `_build_concept_vocabulary`.

- [ ] **Step 3: Delete the smoke-check script**

```bash
rm /tmp/phase15b_smoke.py
```

Do NOT commit it.

---

## Task 9: Retrain + validate

**Files:** none (training pipeline, model is gitignored)

- [ ] **Step 1: Run training with --rebuild-features and tee**

```bash
EDHREC_FREE=1 uv run python3 scripts/train_fusion_model.py \
    --rebuild-features --validate \
    2>&1 | tee logs/2026-04-06-task9-phase15b-retrain.log
```

Expected runtime: ~8 minutes. The full feature rebuild is mandatory because the static_mode signal changes the feature matrix even though the column count stays at 98. Use `tee` to capture the full log per `feedback_training_workflow`.

- [ ] **Step 2: Extract NDCG numbers**

```bash
grep -E "Fold |Mean NDCG|Forge-only NDCG|Validation:" logs/2026-04-06-task9-phase15b-retrain.log | tail -10
```

Record the numbers in your notes.

- [ ] **Step 3: Apply the decision matrix**

Compare against the post-Phase-1 baseline (~0.5691). Decision matrix from the spec:

| Result | Action |
|---|---|
| ≥ 0.568 | GREEN. Proceed to per-commander validation (Task 10). |
| 0.566–0.568 | YELLOW. Within variance. Proceed but flag in commit message. |
| 0.561–0.566 | ORANGE. Real small regression. STOP. Surface log to controller. |
| < 0.561 | RED. ROLLBACK. `cp ~/mtg-synergy-backups/2026-04-06-pre-phase15b/fusion_model_forge.lgb data/` and `cp ~/mtg-synergy-backups/2026-04-06-pre-phase15b/fusion_model_forge.lgb.meta.json data/`. Bisect by reverting layers (mechanics_vectors integration vs deck-tag synthesis vs schema migration). |

- [ ] **Step 4: If GREEN or YELLOW, snapshot the new model state**

```bash
PHASE15_DONE="$HOME/mtg-synergy-backups/2026-04-06-post-phase15b"
mkdir -p "$PHASE15_DONE"
cp data/forge_features_cache.npz             "$PHASE15_DONE/forge_features_cache.npz"
cp data/fusion_model_forge.lgb               "$PHASE15_DONE/fusion_model_forge.lgb"
cp data/fusion_model_forge.lgb.meta.json     "$PHASE15_DONE/fusion_model_forge.lgb.meta.json"
echo "POST_PHASE15B_NDCG: <number from step 2>" > "$PHASE15_DONE/BASELINE.txt"
ls -lh "$PHASE15_DONE"
```

This becomes the rollback target for any future Phase 1.5 sub-projects (A, C, D).

- [ ] **Step 5: No commit (data files are gitignored)**

The retrained model is on disk only. Decisions about whether to ship are made in Task 10 + Task 11 based on per-commander validation. No git commit in this task.

---

## Task 10: Per-commander validation (5 archetype commanders)

**Files:** none (read-only `compare_edhrec.py` runs)

- [ ] **Step 1: Save the current (Phase 1.5b) model and restore the post-Phase-1 baseline**

```bash
cp data/fusion_model_forge.lgb data/fusion_model_forge.lgb.phase15b_tmp
cp data/fusion_model_forge.lgb.meta.json data/fusion_model_forge.lgb.meta.json.phase15b_tmp
cp ~/mtg-synergy-backups/2026-04-06-pre-phase15b/fusion_model_forge.lgb data/fusion_model_forge.lgb
cp ~/mtg-synergy-backups/2026-04-06-pre-phase15b/fusion_model_forge.lgb.meta.json data/fusion_model_forge.lgb.meta.json
md5 data/fusion_model_forge.lgb data/fusion_model_forge.lgb.phase15b_tmp
```

The two MD5 hashes must differ. If they're the same, the swap didn't happen — investigate.

- [ ] **Step 2: Run compare_edhrec against the 5 target commanders with the BASELINE model**

```bash
for cmdr in "Panharmonicon" "Urza, Lord High Artificer" "Adriana, Captain of the Guard" "Yidris, Maelduke of Chaos" "Sun Quan, Lord of Wu"; do
  echo "=== BASELINE :: $cmdr ==="
  EDHREC_FREE=1 uv run python3 scripts/compare_edhrec.py --commander "$cmdr" 2>&1 | grep -E "Hi-Syn|^    Top |OnPage|NotEDH" | head -4
  echo ""
done | tee logs/2026-04-06-task10-baseline-compare.log
```

Record the Hi-Syn / Top / OnPage / NotEDH counts for each commander.

- [ ] **Step 3: Restore the Phase 1.5b model and rerun**

```bash
cp data/fusion_model_forge.lgb.phase15b_tmp data/fusion_model_forge.lgb
cp data/fusion_model_forge.lgb.meta.json.phase15b_tmp data/fusion_model_forge.lgb.meta.json
rm data/fusion_model_forge.lgb.phase15b_tmp data/fusion_model_forge.lgb.meta.json.phase15b_tmp
md5 data/fusion_model_forge.lgb
```

The MD5 should match the Phase 1.5b model from Task 9 step 4. If it matches the baseline, the swap was reversed incorrectly — investigate.

- [ ] **Step 4: Run compare_edhrec against the 5 target commanders with the PHASE 1.5B model**

```bash
for cmdr in "Panharmonicon" "Urza, Lord High Artificer" "Adriana, Captain of the Guard" "Yidris, Maelduke of Chaos" "Sun Quan, Lord of Wu"; do
  echo "=== PHASE15B :: $cmdr ==="
  EDHREC_FREE=1 uv run python3 scripts/compare_edhrec.py --commander "$cmdr" 2>&1 | grep -E "Hi-Syn|^    Top |OnPage|NotEDH" | head -4
  echo ""
done | tee logs/2026-04-06-task10-phase15b-compare.log
```

- [ ] **Step 5: Apply acceptance criteria**

Build a side-by-side comparison of the two logs. For each commander, compute Δ(Hi-Syn) and Δ(OnPage) between baseline and Phase 1.5b.

Acceptance:
- ≥ 3 of 5 commanders show improvement (or hold) on Hi-Syn or OnPage → GREEN, ship
- 2 of 5 improve, 3 hold → YELLOW, ship with caveats
- < 2 improve OR ≥ 1 regression beyond noise → STOP and surface to controller

Record the comparison in your task report.

---

## Task 11: Documentation update

**Files:**
- Modify: `CLAUDE.md` (add `static_mode` column to forge_abilities schema notes, add `Static$` tag prefix to deck-tag conventions)

- [ ] **Step 1: Update the `forge_abilities` schema description in CLAUDE.md**

In `CLAUDE.md`, find the table `### DB Schema (data/tags.db)` (around line 150 area). Update the `forge_abilities` row description:

```markdown
| forge_abilities | ~72k | Raw Forge ability data + SubAbility chain expansions ... 21 columns: 19 consumed in features ... static_mode column (Phase 1.5 sub-project B): S: line Mode$ values, NULL for non-S: rows. Replaces the previous verb-column pollution where Mode$ was silently stored in the verb column. ... |
```

(Adjust the wording to match the existing style of the row.)

- [ ] **Step 2: Add the `Static$<mode>` tag prefix to the deck-tag conventions section**

Find the section where existing tag prefixes are documented (around the auto-derived deck tags description, near line 220-230). Add a note:

```markdown
- Auto-derived deck tag prefixes (built by forge_features.py
  _load_deck_tags from forge profiles):
  - Ability$<verb> — has-tag, from profile.verbs
  - Ability$<stem> — hint-tag, from profile.triggers via _TRIGGER_STEM_TO_VERB
  - Type$<subtype> — hint-tag from trigger_filters; has-tag from token_subtypes
  - Static$<mode> — has-tag from profile.static_modes (Phase 1.5 sub-project B,
    e.g., Static$Panharmonicon, Static$ReduceCost, Static$Continuous)
```

- [ ] **Step 3: Update the "Forge profiles extract ALL raw_line fields" section**

Find the existing list of extracted fields (around line 198-210). Append a note about static_modes:

```markdown
... existing list ...,
+ static_modes (Phase 1.5 sub-project B): set of S: line Mode$ values
  (Continuous, Panharmonicon, ReduceCost, RaiseCost, CantBlock, MayPlay, ...)
  flowing into Static$<mode> deck tags and the mechanics_vectors concept
  space (synthetic event tuple ("static_mode", lowercased_mode, None, None)
  in the produces vector, themes category)
```

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md
git commit -F - <<'EOF'
docs(CLAUDE): Phase 1.5 sub-project B — static_mode column + Static$ tags

Documents the new forge_abilities.static_mode column, the verb-column
pollution fix, the Static$<mode> auto-tag prefix, and the
profile.static_modes field added in Phase 1.5 sub-project B.

Per-commander validation results (5 archetype-targeted commanders) and
NDCG@30 delta vs the post-Phase-1 baseline are recorded in the task
log: logs/2026-04-06-task9-phase15b-retrain.log and
logs/2026-04-06-task10-phase15b-compare.log.
EOF
git log --oneline -7
```

---

## Task 12: Final verification

**Files:** none (test runs)

- [ ] **Step 1: Run the full test suite one more time**

```bash
uv run pytest tests/ -v --tb=short 2>&1 | tail -30
```

Expected: ~196+ tests passing (183 baseline + 5 from Task 2 + 4 from Task 5 + 2 from Task 6 + 4 from Task 7 = 198). Zero regressions.

- [ ] **Step 2: Verify branch state is clean**

```bash
git status --short
git log --oneline cd278d3..HEAD
```

Expected: working tree clean (only `logs/` and `scripts/diagnose_features.py` should be untracked, per the long-standing decision). The branch should have ~6 new commits on top of the Phase 1 branch tip:

```
<sha> docs(CLAUDE): Phase 1.5 sub-project B — static_mode column + Static$ tags
<sha> feat(forge): mechanics_vectors integration for static_mode
<sha> feat(forge): auto-derive Static$<mode> deck tags from S: line modes
<sha> feat(forge): wire static_mode through ForgeFeatureContext
<sha> feat(forge): add static_mode column + S: branch parser fix
e69f429 docs(spec): Phase 1.5 sub-project B — Static ability Mode$ semantics
63b7ca9 chore: address python-review medium and low issues
... (Phase 1 commits)
```

- [ ] **Step 3: Report Phase 1.5 sub-project B complete**

Surface to the controller:
- NDCG@30 delta vs Phase 1 baseline (from Task 9)
- Per-commander validation results (from Task 10)
- Test count (from step 1)
- Branch state (from step 2)
- Decision: ship / hold / rollback

---

## Self-review checklist (run before declaring plan complete)

**Spec coverage:**
- [x] Schema migration → Task 2
- [x] forge_import.py S: branch fix → Task 2
- [x] INSERT statement update → Task 2
- [x] Re-import → Task 3
- [x] Audit FROM forge_abilities SELECTs → Task 4
- [x] forge_features.py SELECT update + row destructuring → Task 5
- [x] ForgeProfile.static_modes field → Task 5
- [x] _compact_forge_profiles update → Task 5
- [x] Schema sanity check → Task 5
- [x] _load_deck_tags auto-tag synthesis → Task 6
- [x] mechanics_vectors _EVENT_CATEGORY → Task 7
- [x] mechanics_vectors category dispatch elif → Task 7
- [x] mechanics_vectors build_mechanics_vectors row processing → Task 7
- [x] _build_concept_vocabulary handles static_mode → Task 7
- [x] Smoke check on real data → Task 8
- [x] Retrain + validate → Task 9
- [x] Per-commander validation (5 commanders) → Task 10
- [x] CLAUDE.md docs update → Task 11
- [x] Test infrastructure (conftest.py + test_schema.py) → Task 2
- [x] Pre-flight backup → Task 1
- [x] Corpus verification (Task 1 lesson) → Task 1
- [x] Strict equality assertions on load-bearing tests → Tasks 2/5/6/7
- [x] Phase 1 lesson: corpus verification before tests → Task 1

**Placeholder scan:** No "TBD", no "implement later", no "similar to Task N", no "add appropriate error handling". Each step has actual code or commands.

**Type / name consistency:**
- `static_mode` (singular) is the column name and the row destructuring local variable ✓
- `static_modes` (plural) is the profile set field name ✓
- `Static$<mode>` is the deck tag prefix ✓
- `_check_schema` is the sanity check method name ✓
- `_TRIGGER_STEM_TO_VERB` is unchanged from Phase 1 ✓
- `_GENERIC_TRIGGER_FILTER_TYPES` is unchanged from Phase 1 ✓
- `_EVENT_CATEGORY['static_mode'] == 'themes'` is consistent across spec, plan, and tests ✓
- `("static_mode", lowercased_mode, None, None)` is the synthetic tuple shape used in spec, plan, and tests ✓

**Project rule compliance:**
- General mechanical patterns, not per-card rules ✓ (single category mapping, no per-Mode lookup)
- No code duplication between training and inference ✓ (single source: forge_import.py extracts, forge_features.py reads)
- Feature cache backed up before training ✓ (Task 1 step 1)
- Single training invocation with `tee`, no greppy rerun loops ✓ (Task 9 step 1)
- TDD: every code change has a failing test written first ✓ (Tasks 2, 5, 6, 7)
- Corpus-verified test inputs ✓ (Task 1 + reused throughout)
- No new GBM feature columns ✓ (98 → 98, FORGE_FEATURE_NAMES unchanged)
- Phase 1 lesson: smoke check on real DB before retraining ✓ (Task 8)
