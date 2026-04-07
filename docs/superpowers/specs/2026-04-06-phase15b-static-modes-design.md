# Phase 1.5 Sub-project B — Static Ability `Mode$` Semantics

**Date:** 2026-04-06
**Status:** Design approved, awaiting implementation plan
**Predecessor:** Phase 1 (`feat/forge-dsl-phase1`, branch tip `63b7ca9`)
**Predecessor baseline NDCG@30:** ~0.5691 (EDHREC_FREE)

## Context

Phase 1 of the Forge DSL extraction roadmap (branch `feat/forge-dsl-phase1`)
extracted three narrow data sources — combat trigger filters
(`ValidAttacker$/ValidBlocker$`), 4 new cost categories, and abandoned the
`ReplaceWith$` substitute-verb work after corpus inspection showed the audit
had specified the wrong DSL format. NDCG@30 moved by −0.0016 (within
variance), but per-commander EDHREC overlap on 5 archetype-targeted
commanders improved by +60% Hi-Syn / +24% OnPage. Phase 1 was a small,
narrow change that produced a small, narrow signal.

The original Forge DSL audit identified **static ability `Mode$` semantics**
as the single largest unaddressed blind spot in the project's data pipeline:
~6,000 cards (19% of the 31k corpus) use S: lines with ~60 distinct `Mode$`
values that the model currently sees as either nothing at all or as
verb-column pollution (the existing `forge_import.py` S: branch silently
dumps `Mode$` into the `verb` column, so `Continuous`, `Panharmonicon`, and
`ReduceCost` are mixed with actual verbs like `Tap` and `Sacrifice`).

This sub-project extracts `Mode$` as a structured column on `forge_abilities`,
fixes the verb-column pollution bug, and exposes the new data through both
auto-derived deck tags and the mechanics vectors concept space — all without
adding new GBM feature columns.

## Goals

1. Stop the existing verb-column pollution where Mode$ values are silently
   stored in the `verb` column.
2. Extract every Mode$ value from S: lines into a new structured column on
   `forge_abilities` (~60 distinct values, ~6,000 cards).
3. Surface the new data through TWO existing feature pathways:
   - Auto-derived `Static$<mode>` deck tags → 5 existing aggregate features
     (`deck_has_overlap`, `deck_has_to_hints`, `deck_hints_to_has`,
     `card_needs_satisfied`, `cmdr_needs_to_card_has`)
   - Mechanics vectors concept space → `mech_cosine` (F3, highest
     single-feature importance) and `co_producer_score` (F31)
4. Add zero new GBM feature columns. The 98-column feature matrix stays at
   98 columns. Phase 1's lesson — flow new data through existing aggregate
   features instead of adding narrow features — is preserved.
5. Hold or improve NDCG@30 against the post-Phase-1 baseline (~0.5691).
6. Improve per-commander recommendations on 5 archetype-targeted commanders
   that exercise specific Mode$ values (Panharmonicon, Urza, Adriana,
   Yidris, Sun Quan).

## Non-Goals

- This sub-project does NOT cover the other Phase 1.5 sub-projects:
  - Sub-project A: `ReplaceWith$` SVar resolution (queued separately)
  - Sub-project C: `IsPresent$/CheckSVar$` conditional handling (queued)
  - Sub-project D: `AddAbility$/AddTrigger$` granted-ability detail (queued)
- This sub-project does NOT add new GBM feature columns. If the auto-tag
  and mechanics-vector pathways are insufficient to surface signal, that
  is a separate decision for a follow-up.
- This sub-project does NOT change any scoring penalties in
  `forge_compute.py` `_apply_penalties()`.
- This sub-project does NOT touch the causal interaction graph
  (`scripts/build_graph.py`). The graph reads `forge_abilities` for verbs
  and triggers but does not need to know about `static_mode`.
- This sub-project does NOT implement a denylist or allowlist of Mode$
  values. Per `feedback_extract_all_forge` and `feedback_no_individual_rules`,
  ALL Mode$ values are extracted faithfully and downstream feature code
  decides what to do with each.

## Architecture overview

Three layers of change, isolated to four files plus tests:

```
Layer 1 — Parse layer (mtg-synergy-train package)
  packages/mtg-synergy-train/src/mtg_synergy_train/parse/forge_import.py
    ├── Schema migration: forge_abilities gains static_mode TEXT column
    ├── ensure_forge_schema() detects stale schema and DROP+CREATE on demand
    ├── extract_ability_fields() S: branch sets static_mode (NOT verb)
    ├── INSERT statement: 20 placeholders → 21
    └── Re-import required: scripts/import_forge.py --import (~1-2 min)

Layer 2 — Profile + feature layer (mtg-synergy package)
  packages/mtg-synergy/src/mtg_synergy/recommend/forge_features.py
    ├── _process_forge_ability_row reads static_mode from row tuple
    ├── ForgeProfile.static_modes: set[str] (compacted to frozenset)
    ├── _load_deck_tags auto-derives Static$<mode> tags into _deck_has
    ├── ForgeFeatureContext.__init__ schema sanity check (clear error on stale DB)
    └── _raw_abilities tuple shape extends by one element

  packages/mtg-synergy/src/mtg_synergy/recommend/mechanics_vectors.py
    ├── build_mechanics_vectors reads static_mode from tuple
    ├── Synthetic event tuple: ("static_mode", lowercased_mode, None, None)
    ├── Always added to produces_tuples (asymmetric — never consumes)
    └── _EVENT_CATEGORY gains "static_mode" → "themes"

Layer 3 — Tests + validation
  tests/test_forge_features_phase15_static_modes.py (NEW, ~15 tests)
  tests/test_schema.py (1 new assertion)
  tests/conftest.py (test DB schema mirror)
  tests/test_forge_import.py (likely needs an S: branch test update)

Re-train and validate
  EDHREC_FREE=1 scripts/train_fusion_model.py --rebuild-features --validate
  scripts/compare_edhrec.py --commander "<5 target commanders>"
```

**What does NOT change:**

- `forge_compute.py` — no new GBM feature columns
- `train_fusion_model.py` — `FORGE_FEATURE_NAMES` is unchanged
- `scoring.py` — no new penalties
- `scripts/build_graph.py` — graph doesn't read `static_mode`
- `scripts/download_cards.py` / `scripts/fetch_edhrec_all.py` — Scryfall +
  EDHREC data unchanged

**Re-import scope:**

- ✅ `scripts/import_forge.py --import` (mandatory — schema change)
- ✅ `scripts/train_fusion_model.py --rebuild-features --validate` (mandatory)
- ❌ `scripts/build_graph.py --rebuild` (NOT needed)
- ❌ `scripts/download_cards.py` (NOT needed)
- ❌ `scripts/fetch_edhrec_all.py` (NOT needed)

## Schema migration

### Current `forge_abilities` schema (20 columns)

```sql
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
```

### New `forge_abilities` schema (21 columns)

```sql
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
    static_mode TEXT,           -- NEW: Mode$ value from S: lines (NULL for non-S: rows)
    raw_line TEXT NOT NULL,
    PRIMARY KEY (card_name, ability_index)
)
```

**Column position:** `static_mode` is the second-to-last positional column,
immediately before `raw_line`. Order matters because `forge_features.py`
reads rows by positional index in `_process_forge_ability_row`.

### Migration strategy: drop and rebuild on detect

The project has no migration system. The current pattern in `import_all()`
is `DELETE FROM forge_abilities` followed by full re-import.

Updated pattern: in `ensure_forge_schema()`, detect stale schema via
`PRAGMA table_info(forge_abilities)`. If `static_mode` is not present in
the column list, `DROP TABLE IF EXISTS forge_abilities` and recreate.
Otherwise no-op.

```python
def ensure_forge_schema(conn):
    """Create Forge tables. Migrates stale schemas by dropping and recreating
    forge_abilities if the static_mode column (Phase 1.5) is missing."""
    existing_cols = {r[1] for r in conn.execute("PRAGMA table_info(forge_abilities)")}
    if existing_cols and "static_mode" not in existing_cols:
        conn.execute("DROP TABLE forge_abilities")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS forge_abilities (
            ...
            static_mode TEXT,
            raw_line TEXT NOT NULL,
            PRIMARY KEY (card_name, ability_index)
        )
    """)
    # ... rest of ensure_forge_schema unchanged
```

### Re-import is destructive but recoverable

The `import_all()` function will:
1. Detect the stale schema and drop forge_abilities
2. Recreate it with the new column
3. Re-populate from `data/forge/forge-gui/res/cardsfolder/` (no network call)

This takes 1-2 minutes on a developer machine. The
`data/forge/forge-gui/res/cardsfolder/` files are not modified, so the
operation is fully repeatable. Pre-flight backup of `data/tags.db` is
mandatory in the implementation plan.

## Parse layer details (`forge_import.py`)

### S: branch fix — stop polluting `verb`

Current code (line 161 area):

```python
elif prefix == "S":
    result["verb"] = fields.get("SP") or fields.get("Mode")
```

This stores Mode$ in the verb column, mixing modes and verbs. New code:

```python
elif prefix == "S":
    # Mode$ goes to its own structured column. SP$ on an S: line is rare
    # and was previously dumped into verb regardless of meaning — drop the
    # SP fallback to keep verb semantically pure (verbs only).
    result["static_mode"] = fields.get("Mode")
```

The result dict initializer gains a new entry:

```python
result = {
    "ability_type": prefix,
    "raw_line": f"{prefix}:{line}",
    "verb": None,
    ...
    "unless_cost": None,
    "static_mode": None,    # NEW
}
```

### INSERT statement update

Current `import_card_to_db` INSERT (20 placeholders):

```python
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
```

New (21 placeholders):

```python
conn.execute(
    "INSERT OR REPLACE INTO forge_abilities VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
    (name, ab["ability_index"], ab["ability_type"], ab.get("verb"),
     ab.get("trigger_mode"), ab.get("trigger_filter"),
     ab.get("trigger_origin"), ab.get("trigger_destination"),
     ab.get("trigger_phase"), ab.get("trigger_zones"),
     ab.get("target"), ab.get("defined"), ab.get("amount"),
     ab.get("cost"), ab.get("keyword"), ab.get("token_script"),
     ab.get("counter_type"), ab.get("sub_ability"),
     ab.get("unless_cost"), ab.get("static_mode"), ab.get("raw_line", "")),
)
```

`static_mode` is positional argument 20 (0-indexed 19), immediately before
`raw_line`.

## Profile + feature layer details (`forge_features.py`)

### SELECT update

Current SELECT in `_load_forge_profiles`:

```python
for row in conn.execute(
    "SELECT card_name, ability_index, ability_type, verb, trigger_mode, "
    "trigger_filter, trigger_origin, trigger_destination, trigger_phase, "
    "trigger_zones, target, defined, amount, cost, keyword, token_script, "
    "counter_type, sub_ability, unless_cost, raw_line FROM forge_abilities"
):
```

New:

```python
for row in conn.execute(
    "SELECT card_name, ability_index, ability_type, verb, trigger_mode, "
    "trigger_filter, trigger_origin, trigger_destination, trigger_phase, "
    "trigger_zones, target, defined, amount, cost, keyword, token_script, "
    "counter_type, sub_ability, unless_cost, static_mode, raw_line FROM forge_abilities"
):
```

`raw_line` moves from positional index 19 to index 20.
`static_mode` is the new index 19.

### Audit of all `FROM forge_abilities` SELECTs

Before merging, the implementer runs:

```bash
grep -rn "FROM forge_abilities" packages/ scripts/
```

For each hit, the SELECT must either (a) include `static_mode` explicitly
in the column list OR (b) explicitly omit it. SELECTs that use `*` expansion
break silently if column ordering changes; verify none exist. Expected hits:
`forge_features.py`, `mechanics_vectors.py`, possibly
`packages/mtg-synergy-train/.../causal/forge_indexer.py`,
`scripts/compare_strategy_vs_mech.py`,
`scripts/strategy_detector.py`, etc.

### `_process_forge_ability_row` row destructuring

The method currently destructures rows by positional index. Update to read
the new column. The new positional layout:

```
0  card_name
1  ability_index
2  ability_type
3  verb
4  trigger_mode
5  trigger_filter
6  trigger_origin
7  trigger_destination
8  trigger_phase
9  trigger_zones
10 target
11 defined
12 amount
13 cost
14 keyword
15 token_script
16 counter_type
17 sub_ability
18 unless_cost
19 static_mode    NEW
20 raw_line       moved from 19 → 20
```

The implementer audits every `row[N]` access in `_process_forge_ability_row`
and shifts `row[19]` references to `row[20]` for `raw_line`. Adds
`static_mode = row[19]` near the top of the method.

### `ForgeProfile` field

The profile dict initializer in `_process_forge_ability_row` adds:

```python
'static_modes': set(),
```

The S: branch processing adds:

```python
if static_mode:
    p['static_modes'].add(static_mode)
```

### `_compact_forge_profiles` update

The compaction method converts mutable sets to frozensets to save ~60 MiB.
Add `'static_modes'` to its compaction list. Bounded vocabulary (~60 distinct
strings) means dedupe yields meaningful savings.

### Auto-tag synthesis in `_load_deck_tags`

The existing auto-derive loop (added in commit `ce8ca96`) creates
`Ability$<verb>` tags. Extend it with a sibling block:

```python
for oid, profile in self._forge_profiles.items():
    for verb in profile.get('verbs', set()):
        self._deck_has.setdefault(oid, set()).add(f"Ability${verb}")
    for trig in profile.get('triggers', set()):
        stem = _TRIGGER_STEM_TO_VERB.get(trig)
        if stem:
            self._deck_hints.setdefault(oid, set()).add(f"Ability${stem}")
    # Phase 1.5: Static$<mode> tags from S: line Mode$ values
    for mode in profile.get('static_modes', set()):
        self._deck_has.setdefault(oid, set()).add(f"Static${mode}")
    for tf in profile.get('trigger_filters', set()):
        if tf not in _GENERIC_TRIGGER_FILTER_TYPES and len(tf) > 2:
            self._deck_hints.setdefault(oid, set()).add(f"Type${tf.title()}")
```

Note: the new block adds tags to `_deck_has`, NOT `_deck_hints`. Static
modes are properties a card HAS, not properties a card WANTS. The
distinction matters for `deck_has_to_hints` vs `deck_hints_to_has`.

### Vocabulary collision check

Existing tag prefixes used in the codebase: `Ability$`, `Type$`, `Strategy$`
(legacy), `Theme$`. `Static$` is unused. Verified by grepping the test
fixtures and the live `forge_deck_tags` table.

### Schema sanity check at `__init__`

```python
def _check_schema(self, conn):
    """Verify the DB schema includes Phase 1.5 columns. Surfaces a clear
    error message if the developer forgot to re-run scripts/import_forge.py."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(forge_abilities)")}
    if "static_mode" not in cols:
        raise RuntimeError(
            "forge_abilities is missing the 'static_mode' column (Phase 1.5). "
            "Re-import Forge data: python3 scripts/import_forge.py --import"
        )
```

Called once from `ForgeFeatureContext.__init__` immediately after the conn
is stored. Cheap (single PRAGMA query).

## Mechanics vectors integration (`mechanics_vectors.py`)

### Synthetic event tuple shape

Static modes don't fit the existing event tuple shape because they aren't
events — they're persistent properties. Use a synthetic event_class
`"static_mode"` with the lowercased Mode$ value as the type qualifier.
Examples:

```python
("static_mode", "continuous",     None, None)
("static_mode", "panharmonicon",  None, None)
("static_mode", "reducecost",     None, None)
("static_mode", "raisecost",      None, None)
("static_mode", "cantblock",      None, None)
("static_mode", "cantattack",     None, None)
("static_mode", "mayplay",        None, None)
("static_mode", "maylookat",      None, None)
("static_mode", "prevent",        None, None)
```

Zones are always `None`. Static effects don't have a zone-of-origin or
zone-of-destination in the event-tuple sense.

### Produces vs consumes assignment

Static modes are always self-produced, never consumed. A card with
`Mode$ Panharmonicon` provides ETB doubling — it doesn't "want" Panharmonicon
from elsewhere. So all `static_mode` tuples land in the `produces` vector,
none in `consumes`.

This means:
- `mech_cosine` (F3) picks up parallel-mode cards (e.g., Panharmonicon +
  Roon both produce the panharmonicon tuple → high cosine similarity).
- `co_producer_score` (F31) picks up commander + card pairs that share a
  static mode in their produces vectors. F31 was added in commit `ce8ca96`
  specifically for "parallel mechanic synergy" cases like Atraxa +
  Evolution Sage both proliferating. Static modes flow through F31 for free.
- `mech_fwd_synergy` (F29) and `mech_rev_synergy` (F30) are unaffected
  because static modes never enter `consumes`.

### Row tuple shape extension

The current `_raw_abilities` tuple format (constructed in
`_process_forge_ability_row` and consumed by `build_mechanics_vectors`):

```
(oid, verb, trig_mode, trig_filter, cost, kw, token_script,
 trig_origin, trig_destination, ability_type, raw_line)
```

New shape:

```
(oid, verb, trig_mode, trig_filter, cost, kw, token_script,
 trig_origin, trig_destination, ability_type, raw_line, static_mode)
```

`static_mode` is appended at the end to minimize the diff to existing
unpackers (any consumer that uses positional unpacking with explicit names
sees no change for the existing fields).

### `build_mechanics_vectors` extraction

Inside the row-processing loop, after the existing event-tuple extraction,
add:

```python
# Phase 1.5: static modes from S: lines
if static_mode:
    mode_qualifier = static_mode.lower()
    produces_tuples.add(("static_mode", mode_qualifier, None, None))
```

The `produces_tuples` set is what feeds the dynamic vocabulary build.
Adding to it means new Mode$ values appear in the sorted vocabulary
automatically. No manual vocabulary management.

### Category assignment for the 8-category sub-products

`forge_compute.py` has 16 features computed as `(category × forward/reverse)`
dot products spanning F71-F86 across 8 categories. The category assignment
lives in `mechanics_vectors.py`'s `_EVENT_CATEGORY` dict.

Add `"static_mode"` to it:

```python
_EVENT_CATEGORY = {
    "enters":        "board",
    ...
    "static_mode":   "themes",   # NEW
}
```

Reasoning: static modes are heterogeneous (cost reducers, anthems,
restrictions, ETB doublers), but most are theme-defining ("the deck plays
around Panharmonicon" / "the deck plays around cost reduction"). Forcing
all 60 Mode$ values into the "themes" category is a single mapping
decision, not 60. Per `feedback_no_individual_rules`, a per-Mode category
lookup table is exactly the anti-pattern to avoid. `mech_themes_fwd`
(F83) and `mech_themes_rev` (F84) pick up the new dims for free.

## Test strategy

### Unit tests — new file

`tests/test_forge_features_phase15_static_modes.py` with three test classes:

#### `TestStaticModeColumnExtraction` (forge_import.py — 5 tests)

- `test_extracts_continuous_mode` — verbatim S: row from corpus, assert
  `result["static_mode"] == "Continuous"`
- `test_extracts_panharmonicon_mode` — verbatim row, assert
  `result["static_mode"] == "Panharmonicon"`
- `test_extracts_reducecost_mode` — verbatim row, assert
  `result["static_mode"] == "ReduceCost"`
- `test_non_s_lines_have_null_static_mode` — A:/T:/R: rows must have
  `result["static_mode"] is None`
- `test_s_line_no_mode_returns_null` — defensive: malformed S: line
  without `Mode$` field returns `None`

These call `extract_ability_fields(line, "S", svars)` directly.

#### `TestStaticModeProfileLoading` (forge_features.py — 5-7 tests)

- `test_static_mode_populates_profile_set` — build a tiny in-memory DB
  with one S: row, run a minimal `ForgeFeatureContext`, assert
  `profile.static_modes == frozenset({"Panharmonicon"})` (after compaction)
- `test_multiple_s_modes_on_one_card` — card with 2+ S: lines, assert
  both modes captured
- `test_static_mode_creates_deck_has_tag` — assert `_deck_has[oid]`
  contains `"Static$Panharmonicon"`
- `test_static_mode_compaction_to_frozenset` — assert `static_modes` is
  `frozenset` after `_compact_forge_profiles`
- `test_schema_check_raises_on_missing_column` — sanity check the safety net
- `test_non_s_rows_dont_pollute_static_modes` — verb-pollution regression test

#### `TestStaticModeMechanicsVectors` (mechanics_vectors.py — 3-5 tests)

- `test_static_mode_event_tuple_in_produces` — feed a row to
  `build_mechanics_vectors`, assert
  `("static_mode", "panharmonicon", None, None)` lands in produces
- `test_static_mode_lowercased` — assert qualifier is lowercased
- `test_static_mode_not_in_consumes` — asymmetry: never in consumes vector
- `test_static_mode_category_themes` — assert
  `_EVENT_CATEGORY["static_mode"] == "themes"`

### Schema test additions

`tests/test_schema.py`:

```python
def test_forge_abilities_has_static_mode_column(tmp_db):
    cols = [r[1] for r in tmp_db.execute("PRAGMA table_info(forge_abilities)").fetchall()]
    assert "static_mode" in cols
```

`tests/conftest.py`: update the test DB CREATE TABLE for forge_abilities
to include `static_mode TEXT,` before `raw_line TEXT NOT NULL`.

### Corpus verification step (Task 1 lesson)

Before writing any test, the implementer MUST sample real S: rows from the
live `data/tags.db` to confirm the DSL format:

```bash
sqlite3 data/tags.db "SELECT raw_line FROM forge_abilities WHERE ability_type='S' LIMIT 20;"
sqlite3 data/tags.db "SELECT verb, COUNT(*) FROM forge_abilities WHERE ability_type='S' GROUP BY verb ORDER BY COUNT(*) DESC LIMIT 30;"
sqlite3 data/tags.db "SELECT COUNT(DISTINCT card_name) FROM forge_abilities WHERE ability_type='S';"
```

The implementer records:
- Top 30 Mode$ values by frequency (test inputs come from this list)
- Total S: row count and distinct card count (sanity check vs ~6,000 estimate)

### Smoke check on real data

After wiring is in place but before retraining, run a one-off Python
diagnostic that confirms:

1. Building a `ForgeFeatureContext` against the real DB succeeds
2. Number of cards with non-empty `profile.static_modes` matches the
   expected ~6,000
3. Top 10 Mode$ values by card count match the corpus query from above
4. `Static$<mode>` tags appear in `_deck_has` for the expected cards
5. `build_mechanics_vectors` produces vector includes static_mode tuples
   in its vocabulary (assert `("static_mode", "panharmonicon", None, None)`
   appears for Panharmonicon's oid)

The script is written, run, and discarded (not committed). Same pattern as
the Phase 1 Task 4 diagnostic.

If any of the 5 checks fail, **STOP and bisect** before retraining.

### Per-commander validation

After retraining, run `compare_edhrec.py` against 5 archetype-targeted
commanders:

| Commander | Target Mode$ | Expected signal |
|---|---|---|
| Panharmonicon | Panharmonicon | Higher rank for Roon, Brago, Yarok, Conjurer's Closet |
| Urza, Lord High Artificer | ReduceCost (artifacts) | Higher rank for Etherium Sculptor, Foundry Inspector, Cloud Key |
| Adriana, Captain of the Guard | Continuous + AddPower | Higher rank for Honor of the Pure, Glorious Anthem, Marshal's Anthem |
| Yidris, Maelduke of Chaos | MayPlay / cast from exile | Higher rank for Bolas's Citadel, Etali, Primal Storm |
| Sun Quan, Lord of Wu | CantBlock / can't be blocked | Higher rank for Whispersilk Cloak, Aqueous Form, Rogue's Passage |

**Acceptance:** per-commander Hi-Syn or OnPage count must improve (or
hold) on ≥3 of the 5 commanders. 4-5 improvements is a clear win. 2-3 is
marginal. <2 is neutral or harmful and needs investigation.

### NDCG@30 acceptance criteria

Same decision matrix as Phase 1 Task 5, against the **post-Phase-1 baseline**
(branch tip `63b7ca9`, NDCG ≈ 0.5691):

| Result | Action |
|---|---|
| ≥ 0.568 | GREEN. Ship. |
| 0.566–0.568 | YELLOW. Within variance. Ship if per-commander signal is positive. |
| 0.561–0.566 | ORANGE. Real small regression. Stop, surface to user, decide whether to bisect. |
| < 0.561 | RED. Rollback to Phase 1 model from `~/mtg-synergy-backups/2026-04-06-postwip/`, bisect by reverting layers (mechanics_vectors integration vs deck-tag synthesis vs schema migration). |

### Pre-flight backup

Mandatory before re-import:

```bash
PHASE15_BACKUP="$HOME/mtg-synergy-backups/2026-04-06-pre-phase15b"
mkdir -p "$PHASE15_BACKUP"
cp data/tags.db                              "$PHASE15_BACKUP/tags.db"
cp data/forge_features_cache.npz             "$PHASE15_BACKUP/forge_features_cache.npz" 2>/dev/null || true
cp data/fusion_model_forge.lgb               "$PHASE15_BACKUP/fusion_model_forge.lgb"
cp data/fusion_model_forge.lgb.meta.json     "$PHASE15_BACKUP/fusion_model_forge.lgb.meta.json"
echo "PRE_PHASE15B_BASELINE_NDCG: ~0.5691 (Phase 1 final, branch 63b7ca9)" \
    > "$PHASE15_BACKUP/BASELINE.txt"
```

The `tags.db` backup is the critical one because re-import mutates
`forge_abilities`. If the schema migration fails partway, restore tags.db
and retry without re-downloading Forge.

## Implementation order

The implementation plan (next step, generated by `writing-plans` skill)
will sequence the work as:

1. **Pre-flight backup** (snapshot tags.db, model, meta, feature cache)
2. **Schema migration** (forge_import.py: ensure_forge_schema, INSERT
   placeholder, S: branch fix, extract_ability_fields init)
3. **Re-import** (`scripts/import_forge.py --import`)
4. **Schema test update** (test_schema.py + conftest.py + test_forge_import.py)
5. **forge_features.py SELECT update + row destructuring**
6. **forge_features.py audit of all `FROM forge_abilities` SELECTs**
7. **forge_features.py ForgeProfile.static_modes field + S: branch population**
8. **forge_features.py auto-tag synthesis in _load_deck_tags**
9. **forge_features.py compaction + schema sanity check**
10. **mechanics_vectors.py tuple extension + extraction + category**
11. **forge_features.py `_raw_abilities` tuple extension to pass static_mode through**
12. **TDD: TestStaticModeColumnExtraction (5 tests)**
13. **TDD: TestStaticModeProfileLoading (5-7 tests)**
14. **TDD: TestStaticModeMechanicsVectors (3-5 tests)**
15. **Smoke check on real data**
16. **Retrain + validate** (`EDHREC_FREE=1 train_fusion_model.py --rebuild-features --validate`)
17. **Per-commander validation** (5 archetype-targeted commanders)
18. **Documentation** (CLAUDE.md update for the new column + auto-tag prefix)

Steps 2–11 are mutually entangled (a row destructuring change in
forge_features.py without the SELECT update breaks the load), so they
will be grouped into a single commit. Steps 12–14 are TDD and run as
RED → GREEN cycles.

Total expected commits: ~5
- feat(forge): schema migration + S: branch fix (forge_import.py)
- feat(forge): static_mode profile field + auto-tags + mechanics_vectors integration
- test: Phase 1.5 sub-project B static modes test suite
- docs: CLAUDE.md update for static_mode column + Static$ tag prefix
- (optional 5th: chore for spec/plan doc commit if not already in branch)

The pre-flight backup is filesystem-only — not a code change, so it does
not produce a commit. The re-import (`scripts/import_forge.py --import`) is
a runtime action that mutates `data/tags.db`, which is gitignored, so it
also produces no commit. The retrained model artifacts (`fusion_model_forge.lgb`,
`*.meta.json`) are also gitignored — no commit, just an entry in
`data/model_registry.jsonl` (also gitignored).

## Risks and mitigations

| Risk | Likelihood | Mitigation |
|---|---|---|
| Re-import fails partway | Low | tags.db backup, re-runnable |
| `verb` column audit misses a consumer | Medium | Grep `FROM forge_abilities`, audit each |
| Static$ tag prefix collides | Very low | Verified vs existing prefixes |
| Mechanics vector dim explosion noise | Low | All in produces only, themes category |
| NDCG regresses | Medium | Same decision matrix as Phase 1 |
| Schema sanity check breaks tests | Low | Test fixtures already maintained alongside tag_db.SCHEMA |
| Re-import takes longer than expected | Low | Local files, no network |

## Open questions deferred to implementation

- Exact list of `FROM forge_abilities` SELECTs requiring audit (only known after grep)
- Whether `ForgeProfile.static_modes` needs to enter the `_func_fingerprints` (functional fingerprint vectors) — probably not, but verify during smoke check
- Whether `mechanics_vectors.py` event tuple consumers handle `None` values for both zone fields gracefully — the existing tuples almost always have at least one zone populated. The implementer must verify by reading the vocabulary build code path before adding the synthetic static_mode tuples, and must add a defensive test case in `TestStaticModeMechanicsVectors` if necessary.

## References

- Phase 1 final review: `.claude/PRPs/reviews/branch-feat-forge-dsl-phase1-review.md`
- Phase 1 plan: `docs/superpowers/plans/2026-04-06-forge-dsl-phase1.md`
- Phase 1 branch: `feat/forge-dsl-phase1` at `63b7ca9`
- Original Forge DSL audit: `/tmp/forge_audit_report.md` (session-local)
- Project rules: `CLAUDE.md`, `~/.claude/rules/python/`
- User feedback memories:
  `~/.claude/projects/-Users-evgenii-vasilenko-gofrolist-mtg-synergy-graph/memory/feedback_*.md`
