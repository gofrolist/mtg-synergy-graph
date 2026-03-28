# Synergy Labels + CLI Simplification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace binary EDHREC average-deck labels with richer edhrec_card_synergy data (367k rows, section-based grades), simplify CLI to commander-only interface, remove `decks/` folder.

**Architecture:** Three independent changes: (1) rewrite `_load_pairs_for_features()` to load from edhrec_card_synergy with section-based grading, (2) remove `decks/` and `--deck` from CLI, (3) rewrite `compare_edhrec.py` to use `--commander` and compare against High Synergy section.

**Tech Stack:** Python, SQLite, LightGBM

---

### Task 1: Rewrite training labels to use edhrec_card_synergy

Replace `_load_pairs_for_features()` in `train_fusion_model.py`. Currently loads from `edhrec_average_decks` (binary membership) then looks up synergy for grading. New version loads directly from `edhrec_card_synergy` (367k rows with section and synergy score).

**Files:**
- Modify: `train_fusion_model.py:564-679` (`_load_pairs_for_features` function)
- Modify: `train_fusion_model.py:460-463` (`label_gain` in `train_forge_gbm`)

- [ ] **Step 1: Rewrite `_load_pairs_for_features`**

Replace the entire function (lines 564-679) with:

```python
def _load_pairs_for_features(conn):
    """Load training pairs from edhrec_card_synergy with section-based grading.

    Uses EDHREC's card synergy data directly (367k rows with synergy scores
    and section labels) instead of binary average-deck membership.

    Grades:
        5: "High Synergy Cards" section
        4: "Top Cards" section
        3: Other sections, synergy > 0.1
        2: Other sections, synergy 0-0.1
        1: Any section, synergy < 0 (anti-synergy)
        0: Not in table (random negatives)

    Returns dict[cmdr_oid -> list[(card_oid, grade)]].
    """
    # Resolve commander slugs to oracle_ids
    slug_to_oid, name_to_oid = _resolve_slugs_to_oids(conn, "edhrec_card_synergy")
    oid_to_slug = {v: k for k, v in slug_to_oid.items()}

    # Build card name -> oracle_id lookup (prefer non-token)
    card_name_to_oid = {}
    for row in conn.execute(
        "SELECT name, oracle_id, type_line FROM cards "
        "ORDER BY CASE WHEN type_line LIKE '%Token%' THEN 1 ELSE 0 END"
    ):
        if row[0] not in card_name_to_oid:
            card_name_to_oid[row[0]] = row[1]

    # Load all synergy data with section info
    print("\nLoading EDHREC card synergy data...")
    positives_by_cmdr = {}  # cmdr_oid -> list[(card_oid, grade)]
    n_by_grade = {5: 0, 4: 0, 3: 0, 2: 0, 1: 0}

    # Count card frequency across commanders for staple filtering
    card_cmdr_count = {}  # card_oid -> number of commanders it appears for
    cmdr_count = len(slug_to_oid)

    for row in conn.execute(
        "SELECT commander_slug, card_name, synergy, section FROM edhrec_card_synergy"
    ):
        slug, card_name, synergy, section = row
        cmdr_oid = slug_to_oid.get(slug)
        card_oid = card_name_to_oid.get(card_name)
        if cmdr_oid is None or card_oid is None:
            continue

        card_cmdr_count[card_oid] = card_cmdr_count.get(card_oid, 0) + 1

        # Section-based grading
        if section == "High Synergy Cards":
            grade = 5
        elif section == "Top Cards":
            grade = 4
        elif synergy is not None and synergy < 0:
            grade = 1
        elif synergy is not None and synergy > 0.1:
            grade = 3
        else:
            grade = 2

        positives_by_cmdr.setdefault(cmdr_oid, []).append((card_oid, grade))
        n_by_grade[grade] += 1

    total_pairs = sum(len(v) for v in positives_by_cmdr.values())
    print(f"  Synergy pairs: {total_pairs:,} across {len(positives_by_cmdr)} commanders")
    for g in sorted(n_by_grade.keys(), reverse=True):
        print(f"    Grade {g}: {n_by_grade[g]:,}")

    # Filter generic staples from top grades (>30% commander frequency)
    staple_threshold = 0.30
    staple_oids = {oid for oid, cnt in card_cmdr_count.items()
                   if cnt / cmdr_count > staple_threshold}
    n_filtered = 0
    for cmdr_oid in positives_by_cmdr:
        filtered = []
        for card_oid, grade in positives_by_cmdr[cmdr_oid]:
            if card_oid in staple_oids and grade >= 4:
                grade = 3  # demote staples from High Synergy/Top Cards
                n_filtered += 1
            filtered.append((card_oid, grade))
        positives_by_cmdr[cmdr_oid] = filtered
    if n_filtered:
        print(f"  Filtered {n_filtered} staple pairs (demoted from grade 4/5 to 3)")

    # Sample negatives (cards not in edhrec_card_synergy for this commander)
    card_colors = {}
    for row in conn.execute("SELECT oracle_id, color_identity FROM cards WHERE legal_commander = 1"):
        card_colors[row[0]] = set(json.loads(row[1] or "[]"))

    type_lines = {}
    for row in conn.execute("SELECT oracle_id, type_line FROM cards"):
        type_lines[row[0]] = row[1] or ""

    basic_land_names = {"Plains", "Island", "Swamp", "Mountain", "Forest",
                        "Snow-Covered Plains", "Snow-Covered Island",
                        "Snow-Covered Swamp", "Snow-Covered Mountain",
                        "Snow-Covered Forest", "Wastes"}
    basic_land_oids = set()
    for name in basic_land_names:
        row = conn.execute("SELECT oracle_id FROM cards WHERE name = ?", (name,)).fetchone()
        if row:
            basic_land_oids.add(row[0])

    card_pool = {r[0] for r in conn.execute(
        "SELECT oracle_id FROM cards WHERE legal_commander = 1")}
    all_card_oids = [oid for oid in card_pool
                     if oid not in basic_land_oids
                     and "Token" not in type_lines.get(oid, "")]

    card_strats = {}
    for oid, s in conn.execute(
        "SELECT oracle_id, strategy FROM card_strategies WHERE confidence >= 0.3"
    ):
        card_strats.setdefault(oid, set()).add(s)

    card_subtypes = {}
    for row in conn.execute(
        "SELECT oracle_id, type_line FROM cards WHERE type_line LIKE '%\u2014%'"
    ):
        try:
            subs = {s.lower() for s in row[1].split("\u2014")[1].strip().split()}
            if subs:
                card_subtypes[row[0]] = subs
        except (IndexError, AttributeError):
            pass

    # Build positives_for_neg: cmdr_oid -> set of card_oids (for exclusion during sampling)
    positives_for_neg = {cmdr: {oid for oid, _ in pairs}
                         for cmdr, pairs in positives_by_cmdr.items()}

    print("\nSampling negatives (ratio=1, 50% hard)...")
    neg_pairs = sample_negatives(
        positives_for_neg, all_card_oids, card_colors, card_colors, ratio=1,
        hard_ratio=0.5, card_strats=card_strats, card_subtypes=card_subtypes,
    )
    print(f"  Negative pairs: {len(neg_pairs)}")

    for cmdr_oid, card_oid, label in neg_pairs:
        positives_by_cmdr.setdefault(cmdr_oid, []).append((card_oid, 0))

    # Final grade distribution
    grade_counts = {}
    for pairs in positives_by_cmdr.values():
        for _, g in pairs:
            grade_counts[g] = grade_counts.get(g, 0) + 1
    print(f"\n  Final grade distribution:")
    for g in sorted(grade_counts.keys(), reverse=True):
        print(f"    Grade {g}: {grade_counts[g]:,}")

    return positives_by_cmdr
```

- [ ] **Step 2: Update label_gain in train_forge_gbm**

In `train_forge_gbm` (around line 460), change the label_gain from 10 grades to 6:

```python
        "label_gain": [0, 1, 3, 6, 15, 30],  # 6 grades: neg, anti-syn, low, moderate, top, high-syn
```

Also update the NDCG computation (around line 506) which references `params["label_gain"][int(label_slice[j])]` — this still works since grades are 0-5 and label_gain has 6 entries.

- [ ] **Step 3: Update the `load_edhrec_membership` function**

The old `load_edhrec_membership()` function (used by `_load_pairs_for_features`) loaded from `edhrec_average_decks`. It's no longer called. Check if anything else uses it:

```bash
grep -rn "load_edhrec_membership" train_fusion_model.py src/
```

If only `_load_pairs_for_features` used it, remove the function entirely.

- [ ] **Step 4: Delete stale feature cache and train**

```bash
rm -f data/forge_features_cache.npz
uv run python3 train_fusion_model.py --forge-only --rebuild-features
```

Expected: Feature matrix shape changes (more rows due to 367k synergy pairs + negatives). NDCG@30 on the new labels should be reported.

- [ ] **Step 5: Commit**

```bash
git add train_fusion_model.py
git commit -m "feat: switch training labels from average-deck to edhrec_card_synergy (section-based grading)"
```

---

### Task 2: Remove decks/ folder and --deck CLI

**Files:**
- Delete: `decks/` folder (entire directory)
- Modify: `src/mtg_synergy/cli.py` — remove `--deck`, deck loading, deck-dependent features
- Modify: `src/mtg_synergy/recommend/engine.py` — remove `edhrec_slug` parameter

- [ ] **Step 1: Simplify cli.py**

The CLI currently has two paths: `--deck` (loads deck config) and `--commander` (just added). Remove `--deck` and make `--commander` required for `--recommend` and `--combos`. Remove `--swaps`, `--deck-view`, `--validate`, `--export` (all require a decklist).

The new `run()` function should:
1. Keep `--commander`, `--recommend`, `--combos`, `--top`, `--strategies`, `--exclude-strategies`
2. Remove `--deck`, `--build`, `--input`, `--forge`, `--swaps`, `--deck-view`, `--validate`, `--export`
3. For `--recommend`: use the commander-only path (already implemented)
4. For `--combos`: find combos for commander's color identity

Read the current cli.py first to understand all the code paths, then rewrite.

- [ ] **Step 2: Delete decks/ folder**

```bash
git rm -r decks/
```

- [ ] **Step 3: Remove decks import from any remaining files**

```bash
grep -rn "from decks import\|import decks" src/ *.py
```

Fix any remaining references.

- [ ] **Step 4: Verify CLI works**

```bash
uv run python3 synergy_graph.py --commander "Krenko, Mob Boss" --recommend --top 10
uv run python3 synergy_graph.py --commander "Atraxa, Praetors' Voice" --recommend --top 10
```

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "refactor: remove decks/ folder and --deck CLI, commander-only interface"
```

---

### Task 3: Rewrite compare_edhrec.py for commander-only interface

Replace the deck-based comparison with a commander-based one. Compare our recommendations against the "High Synergy Cards" section from edhrec_card_synergy.

**Files:**
- Modify: `compare_edhrec.py`

- [ ] **Step 1: Rewrite compare_edhrec.py**

The new script should:

1. Accept `--commander "Name"` for single commander, or `--all` for all commanders with synergy data
2. Run `synergy_graph.py --commander "Name" --recommend --top 30` to get our recommendations
3. Compare against `edhrec_card_synergy` data from DB (no need to fetch from EDHREC API — data is already in DB)
4. Report:
   - High-Synergy Recall@30: how many "High Synergy Cards" are in our top 30?
   - Top Cards Recall@30: how many "Top Cards" are in our top 30?
   - Our recs NOT on EDHREC: how many of our top 30 aren't in the synergy table at all?

Key changes:
- Replace `from decks import list_decks, load_deck` with DB-based commander lookup
- Replace `fetch_edhrec()` API calls with DB queries on `edhrec_card_synergy`
- Replace `get_our_recommendations()` subprocess call to use `--commander` instead of `--deck`
- Use `_resolve_slugs_to_oids()` from train_fusion_model for slug→oid resolution (or inline it)

The `--fast` and `--refresh` cache logic can stay (cache keyed by commander slug instead of deck name).

- [ ] **Step 2: Test single commander comparison**

```bash
uv run python3 compare_edhrec.py --commander "Krenko, Mob Boss"
```

- [ ] **Step 3: Test all-commander comparison**

```bash
uv run python3 compare_edhrec.py --all --quiet
```

- [ ] **Step 4: Commit**

```bash
git add compare_edhrec.py
git commit -m "refactor: compare_edhrec.py uses --commander, compares against High Synergy section"
```

---

### Task 4: Update CLAUDE.md and run tests

**Files:**
- Modify: `CLAUDE.md`
- Modify: tests as needed

- [ ] **Step 1: Update CLAUDE.md**

Key changes:
- Training section: describe edhrec_card_synergy labels with 6 grades
- Remove all references to `decks/` folder and `--deck` option
- Update command examples to use `--commander`
- Update compare_edhrec.py examples
- Update feature count if changed
- Remove deck config list from conventions

- [ ] **Step 2: Run tests and fix failures**

```bash
uv run pytest tests/ -v 2>&1 | tail -30
```

Fix any test failures from removed decks/ imports or changed interfaces.

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md tests/
git commit -m "docs: update CLAUDE.md for synergy labels + commander-only CLI"
```
