# Forge-Native Feature Replacement Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace 6 oracle-text-regex features (F25-F30) with Forge-structured-data features, add new features from unused Forge fields, and parse scaling patterns from Forge `raw_line` instead of oracle text — eliminating all avoidable oracle text parsing from the recommendation pipeline.

**Architecture:** The 33-feature forge GBM gets upgraded to ~40 features. Features F25-F30 are replaced in-place by Forge-derived equivalents (same feature indices, new computation). New features are appended. `mechanics_vectors.py` "for each" oracle fallback is replaced by parsing `SpellDescription` from Forge `raw_line`. Commander profile inference switches from oracle keyword matching to Forge verb/trigger analysis.

**Tech Stack:** Python, SQLite, numpy, LightGBM, regex on Forge `raw_line` (not oracle text)

**Key constraint:** Feature cache (`data/forge_features_cache.npz`) must be rebuilt after changes. Use `--forge-only --rebuild-features` to retrain. Feature count change requires deleting old cache.

---

## File Structure

| File | Action | Responsibility |
|------|--------|---------------|
| `mtg_synergy/recommend/forge_features.py` | Modify | Replace F25-F30 computation, add new features F33-F39, add Forge ability profile loading |
| `mtg_synergy/recommend/mechanics_vectors.py` | Modify | Replace oracle "for each" fallback with Forge `raw_line` SpellDescription parsing |
| `mtg_synergy/recommend/commander_profile.py` | Modify | Replace oracle keyword strategy detection with Forge verb/trigger profile |
| `train_fusion_model.py` | Modify | Update FORGE_FEATURE_NAMES list with new/renamed features |
| `tests/test_forge_features.py` | Create | Tests for all new and replaced features |

---

### Task 1: Build Forge ability profile loader

Extract per-card Forge ability profiles (verbs, triggers, keywords, counter_types, targets, ability_types) into a reusable lookup loaded once in `ForgeFeatureContext`. This replaces the pattern of loading oracle text and doing regex matching.

**Files:**
- Modify: `mtg_synergy/recommend/forge_features.py:36-109` (ForgeFeatureContext.__init__)
- Test: `tests/test_forge_features.py` (create)

- [ ] **Step 1: Write failing test for Forge ability profile loading**

```python
# tests/test_forge_features.py
"""Tests for Forge-native feature computation."""
import sqlite3
import pytest
from mtg_synergy.config import DB_PATH


@pytest.fixture
def conn():
    c = sqlite3.connect(DB_PATH)
    yield c
    c.close()


def test_forge_profiles_loaded(conn):
    """ForgeFeatureContext loads per-card Forge ability profiles."""
    import numpy as np
    from mtg_synergy.recommend.forge_features import ForgeFeatureContext

    # Minimal embeddings for constructor
    oid_list = [r[0] for r in conn.execute("SELECT oracle_id FROM cards LIMIT 100")]
    normed_emb = np.random.randn(len(oid_list), 768).astype(np.float16)
    oid_to_idx = {oid: i for i, oid in enumerate(oid_list)}

    ctx = ForgeFeatureContext(conn, normed_emb, oid_to_idx)

    # Should have forge profiles dict
    assert hasattr(ctx, '_forge_profiles')
    assert isinstance(ctx._forge_profiles, dict)
    # Should have at least some cards with profiles
    assert len(ctx._forge_profiles) > 0

    # Each profile should have sets of verbs, triggers, keywords, counter_types
    sample_oid = next(iter(ctx._forge_profiles))
    profile = ctx._forge_profiles[sample_oid]
    assert 'verbs' in profile
    assert 'triggers' in profile
    assert 'keywords' in profile
    assert 'counter_types' in profile
    assert 'targets' in profile
    assert 'ability_types' in profile
    assert isinstance(profile['verbs'], set)


def test_forge_profile_krenko(conn):
    """Krenko Mob Boss profile has Token verb and correct keywords."""
    import numpy as np
    from mtg_synergy.recommend.forge_features import ForgeFeatureContext

    # Get Krenko's oracle_id
    row = conn.execute(
        "SELECT oracle_id FROM cards WHERE name = 'Krenko, Mob Boss'"
    ).fetchone()
    if not row:
        pytest.skip("Krenko not in DB")
    krenko_oid = row[0]

    oid_list = [r[0] for r in conn.execute("SELECT oracle_id FROM cards LIMIT 5000")]
    normed_emb = np.random.randn(len(oid_list), 768).astype(np.float16)
    oid_to_idx = {oid: i for i, oid in enumerate(oid_list)}

    ctx = ForgeFeatureContext(conn, normed_emb, oid_to_idx)

    profile = ctx._forge_profiles.get(krenko_oid)
    assert profile is not None, "Krenko should have a forge profile"
    assert "Token" in profile['verbs'], "Krenko creates tokens"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_forge_features.py::test_forge_profiles_loaded -v`
Expected: FAIL — `_forge_profiles` attribute doesn't exist

- [ ] **Step 3: Implement Forge ability profile loading in ForgeFeatureContext**

Add to `ForgeFeatureContext.__init__` (after line 101, before mechanics vectors):

```python
        # Forge ability profiles: per-card structured data from forge_abilities
        # Replaces oracle text regex matching for features F25-F30
        self._forge_profiles = {}  # oid -> {verbs, triggers, keywords, counter_types, targets, ability_types}
        for row in conn.execute(
            "SELECT fnm.oracle_id, fa.verb, fa.trigger_mode, fa.keyword, "
            "fa.counter_type, fa.target, fa.ability_type, fa.trigger_filter "
            "FROM forge_abilities fa "
            "JOIN forge_name_map fnm ON fnm.forge_name = fa.card_name"
        ):
            oid = row[0]
            p = self._forge_profiles.setdefault(oid, {
                'verbs': set(), 'triggers': set(), 'keywords': set(),
                'counter_types': set(), 'targets': set(), 'ability_types': set(),
                'trigger_filters': set(),
            })
            if row[1]: p['verbs'].add(row[1])
            if row[2]: p['triggers'].add(row[2])
            if row[3]: p['keywords'].add(row[3])
            if row[4]: p['counter_types'].add(row[4])
            if row[5]:
                # Extract primary target type (before dots/commas)
                for t in row[5].split(","):
                    main = t.split(".")[0].strip()
                    if main:
                        p['targets'].add(main)
            if row[6]: p['ability_types'].add(row[6])
            if row[7]:
                # Extract subtypes from trigger_filter (e.g., "Human.YouCtrl" → "human")
                for part in row[7].split(","):
                    main = part.split(".")[0].strip()
                    if main and main != "Card" and main[0].isupper():
                        p['trigger_filters'].add(main.lower())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_forge_features.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_forge_features.py mtg_synergy/recommend/forge_features.py
git commit -m "feat: add Forge ability profile loader to ForgeFeatureContext"
```

---

### Task 2: Replace F25 (text_mentions_cmdr_type) with Forge trigger_filter matching

Currently F25 does substring match of commander's creature type in card oracle text. Replace with: does the card's Forge `trigger_filter` reference the commander's creature subtypes?

**Files:**
- Modify: `mtg_synergy/recommend/forge_features.py:508-515`
- Test: `tests/test_forge_features.py`

- [ ] **Step 1: Write failing test**

```python
def test_f25_trigger_filter_type_match(conn):
    """F25 should detect Forge trigger_filter subtype match, not oracle text."""
    import numpy as np
    from mtg_synergy.recommend.forge_features import (
        ForgeFeatureContext, CmdrFeatureContext, compute_card_features
    )

    # Find a card with trigger_filter mentioning "Goblin"
    row = conn.execute("""
        SELECT fnm.oracle_id FROM forge_abilities fa
        JOIN forge_name_map fnm ON fnm.forge_name = fa.card_name
        WHERE fa.trigger_filter LIKE '%Goblin%'
        LIMIT 1
    """).fetchone()
    if not row:
        pytest.skip("No Goblin trigger_filter cards")
    goblin_trigger_oid = row[0]

    # Find Krenko (a Goblin commander)
    krenko = conn.execute(
        "SELECT oracle_id, type_line FROM cards WHERE name = 'Krenko, Mob Boss'"
    ).fetchone()
    if not krenko:
        pytest.skip("Krenko not in DB")
    krenko_oid = krenko[0]

    oid_list = [r[0] for r in conn.execute("SELECT oracle_id FROM cards LIMIT 5000")]
    normed_emb = np.random.randn(len(oid_list), 768).astype(np.float16)
    oid_to_idx = {oid: i for i, oid in enumerate(oid_list)}

    ctx = ForgeFeatureContext(conn, normed_emb, oid_to_idx)
    cmdr = CmdrFeatureContext(ctx, krenko_oid, set())
    cmdr.cmdr_subtypes = {"goblin", "warrior"}

    card_meta = conn.execute(
        "SELECT type_line, cmc FROM cards WHERE oracle_id = ?", (goblin_trigger_oid,)
    ).fetchone()
    tl = card_meta[0] or ""
    cmc = card_meta[1] or 0.0

    feats = compute_card_features(goblin_trigger_oid, tl, cmc, 0.0, ctx, cmdr)
    # F25 (index 25) should be > 0 because card triggers on Goblins
    assert feats[25] > 0, f"F25 should detect Goblin trigger_filter match, got {feats[25]}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_forge_features.py::test_f25_trigger_filter_type_match -v`
Expected: May pass or fail depending on whether old oracle text approach also catches it. The key is replacing the mechanism.

- [ ] **Step 3: Replace F25 computation**

Replace lines 508-515 in `forge_features.py`:

```python
    # F25: forge_type_synergy — card's Forge trigger_filter or target references
    # commander's creature subtypes. Replaces oracle text substring matching.
    # Captures: Kyler triggers on "Human.Other+YouCtrl", Krenko cards trigger on "Goblin"
    card_profile = ctx._forge_profiles.get(card_oid, {})
    card_trigger_types = card_profile.get('trigger_filters', set())
    card_targets = card_profile.get('targets', set())
    forge_type_syn = 0.0
    if cmdr.cmdr_subtypes:
        # Check trigger_filters (e.g., "goblin" from "Goblin.YouCtrl")
        for subtype in cmdr.cmdr_subtypes:
            if subtype in card_trigger_types:
                forge_type_syn += 1.0
            # Also check targets (e.g., card targets "Goblin" creatures)
            if subtype.title() in card_targets:
                forge_type_syn += 0.5
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_forge_features.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add mtg_synergy/recommend/forge_features.py tests/test_forge_features.py
git commit -m "feat: replace F25 oracle text with Forge trigger_filter type matching"
```

---

### Task 3: Replace F26 (cmdr_text_mentions_card_type) with Forge trigger_filter matching

Currently F26 checks if commander oracle text mentions card's subtypes. Replace with: does the commander's Forge `trigger_filter` reference the card's creature subtypes?

**Files:**
- Modify: `mtg_synergy/recommend/forge_features.py:517-533`
- Test: `tests/test_forge_features.py`

- [ ] **Step 1: Write failing test**

```python
def test_f26_cmdr_trigger_filter_matches_card(conn):
    """F26 should detect when commander's trigger_filter matches card's type."""
    import numpy as np
    from mtg_synergy.recommend.forge_features import (
        ForgeFeatureContext, CmdrFeatureContext, compute_card_features
    )

    # Kyler triggers on "Human.Other+YouCtrl" — so Human creatures should score >0
    kyler = conn.execute(
        "SELECT oracle_id FROM cards WHERE name = 'Kyler, Sigardian Emissary'"
    ).fetchone()
    if not kyler:
        pytest.skip("Kyler not in DB")
    kyler_oid = kyler[0]

    # Find a Human creature card
    human = conn.execute(
        "SELECT oracle_id, type_line, cmc FROM cards "
        "WHERE type_line LIKE '%Human%' AND type_line LIKE '%Creature%' LIMIT 1"
    ).fetchone()
    if not human:
        pytest.skip("No Human creatures in DB")

    oid_list = [r[0] for r in conn.execute("SELECT oracle_id FROM cards LIMIT 5000")]
    normed_emb = np.random.randn(len(oid_list), 768).astype(np.float16)
    oid_to_idx = {oid: i for i, oid in enumerate(oid_list)}

    ctx = ForgeFeatureContext(conn, normed_emb, oid_to_idx)
    cmdr = CmdrFeatureContext(ctx, kyler_oid, set())
    cmdr.cmdr_subtypes = {"human"}

    feats = compute_card_features(human[0], human[1] or "", human[2] or 0.0, 0.0, ctx, cmdr)
    # F26 (index 26) should be > 0 because Kyler triggers on Humans
    assert feats[26] > 0, f"F26 should detect Kyler→Human synergy, got {feats[26]}"
```

- [ ] **Step 2: Run test to verify behavior**

Run: `python3 -m pytest tests/test_forge_features.py::test_f26_cmdr_trigger_filter_matches_card -v`

- [ ] **Step 3: Replace F26 computation**

Replace lines 517-533 in `forge_features.py`:

```python
    # F26: cmdr_forge_type_match — commander's Forge trigger_filter or target
    # references card's subtypes. Replaces oracle text substring matching.
    # Captures: Sram triggers on "Aura,Equipment,Vehicle", Kyler on "Human"
    cmdr_profile = ctx._forge_profiles.get(cmdr.cmdr_oid, {})
    cmdr_trigger_types = cmdr_profile.get('trigger_filters', set())
    cmdr_targets = cmdr_profile.get('targets', set())
    cmdr_type_match = 0.0
    if "\u2014" in tl:
        try:
            card_subs = {s.lower() for s in tl.split("\u2014")[1].strip().split()}
            for sub in card_subs:
                if sub in cmdr_trigger_types:
                    cmdr_type_match += 1.0
                if sub.title() in cmdr_targets:
                    cmdr_type_match += 0.5
        except (IndexError, AttributeError):
            pass
    # Also check card type categories (Creature, Artifact, etc.)
    cmdr_verbs = cmdr_profile.get('verbs', set())
    for ctype in ["Creature", "Artifact", "Enchantment", "Instant", "Sorcery",
                  "Equipment", "Aura", "Vehicle", "Planeswalker"]:
        if ctype in tl and ctype.lower() in cmdr_trigger_types:
            cmdr_type_match += 0.5
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_forge_features.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add mtg_synergy/recommend/forge_features.py tests/test_forge_features.py
git commit -m "feat: replace F26 oracle text with Forge trigger_filter for commander type match"
```

---

### Task 4: Replace F27 (shared_keyword_count) with Forge verb/trigger/keyword overlap

Currently F27 counts 25 hardcoded string keywords shared between oracle texts. Replace with: count of shared Forge verbs + trigger_modes + keywords between commander and card.

**Files:**
- Modify: `mtg_synergy/recommend/forge_features.py:535-545`
- Test: `tests/test_forge_features.py`

- [ ] **Step 1: Write failing test**

```python
def test_f27_forge_shared_mechanics(conn):
    """F27 should count shared Forge verbs/triggers/keywords, not oracle text keywords."""
    import numpy as np
    from mtg_synergy.recommend.forge_features import (
        ForgeFeatureContext, CmdrFeatureContext, compute_card_features
    )

    # Find a commander and card that share Forge verbs (e.g., both have Token)
    # Krenko Mob Boss has Token verb
    krenko = conn.execute(
        "SELECT oracle_id FROM cards WHERE name = 'Krenko, Mob Boss'"
    ).fetchone()
    if not krenko:
        pytest.skip("Krenko not in DB")
    krenko_oid = krenko[0]

    # Find another card with Token verb
    token_card = conn.execute("""
        SELECT fnm.oracle_id, c.type_line, c.cmc FROM forge_abilities fa
        JOIN forge_name_map fnm ON fnm.forge_name = fa.card_name
        JOIN cards c ON c.oracle_id = fnm.oracle_id
        WHERE fa.verb = 'Token' AND fnm.oracle_id != ?
        LIMIT 1
    """, (krenko_oid,)).fetchone()
    if not token_card:
        pytest.skip("No other Token cards")

    oid_list = [r[0] for r in conn.execute("SELECT oracle_id FROM cards LIMIT 5000")]
    normed_emb = np.random.randn(len(oid_list), 768).astype(np.float16)
    oid_to_idx = {oid: i for i, oid in enumerate(oid_list)}

    ctx = ForgeFeatureContext(conn, normed_emb, oid_to_idx)
    cmdr = CmdrFeatureContext(ctx, krenko_oid, set())
    cmdr.cmdr_subtypes = {"goblin"}

    feats = compute_card_features(token_card[0], token_card[1] or "", token_card[2] or 0.0, 0.0, ctx, cmdr)
    # F27 (index 27) should be > 0 because both have Token verb
    assert feats[27] > 0, f"F27 should detect shared Token verb, got {feats[27]}"
```

- [ ] **Step 2: Run test**

Run: `python3 -m pytest tests/test_forge_features.py::test_f27_forge_shared_mechanics -v`

- [ ] **Step 3: Replace F27 computation**

Replace lines 535-545:

```python
    # F27: shared_forge_mechanics — count of shared Forge verbs, trigger_modes, and
    # keywords between commander and card. Replaces hardcoded oracle text keyword list.
    cmdr_mechs = (cmdr_profile.get('verbs', set()) |
                  cmdr_profile.get('triggers', set()) |
                  cmdr_profile.get('keywords', set()))
    card_mechs = (card_profile.get('verbs', set()) |
                  card_profile.get('triggers', set()) |
                  card_profile.get('keywords', set()))
    shared_forge = float(len(cmdr_mechs & card_mechs))
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_forge_features.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add mtg_synergy/recommend/forge_features.py tests/test_forge_features.py
git commit -m "feat: replace F27 oracle keyword count with Forge verb/trigger/keyword overlap"
```

---

### Task 5: Replace F29 (anti_tribal_text) with Forge trigger_filter conflict detection

Currently F29 uses regex to find "non-Human" patterns and "whenever you cast a Knight spell" in oracle text. Replace with: Forge `trigger_filter` references a creature subtype that doesn't match the commander's subtypes.

**Files:**
- Modify: `mtg_synergy/recommend/forge_features.py:553-575`
- Test: `tests/test_forge_features.py`

- [ ] **Step 1: Write failing test**

```python
def test_f29_forge_anti_tribal(conn):
    """F29 should detect tribal conflict via Forge trigger_filter, not oracle regex."""
    import numpy as np
    from mtg_synergy.recommend.forge_features import (
        ForgeFeatureContext, CmdrFeatureContext, compute_card_features
    )

    # Find a card that triggers on a specific non-generic type (e.g., "Spirit,Arcane")
    row = conn.execute("""
        SELECT fnm.oracle_id, c.type_line, c.cmc FROM forge_abilities fa
        JOIN forge_name_map fnm ON fnm.forge_name = fa.card_name
        JOIN cards c ON c.oracle_id = fnm.oracle_id
        WHERE fa.trigger_filter LIKE '%Spirit%' AND fa.trigger_mode = 'SpellCast'
        LIMIT 1
    """).fetchone()
    if not row:
        pytest.skip("No Spirit trigger cards")
    spirit_card_oid = row[0]

    # Use a Goblin commander — Spirit trigger should be anti-tribal
    krenko = conn.execute(
        "SELECT oracle_id FROM cards WHERE name = 'Krenko, Mob Boss'"
    ).fetchone()
    if not krenko:
        pytest.skip("Krenko not in DB")

    oid_list = [r[0] for r in conn.execute("SELECT oracle_id FROM cards LIMIT 5000")]
    normed_emb = np.random.randn(len(oid_list), 768).astype(np.float16)
    oid_to_idx = {oid: i for i, oid in enumerate(oid_list)}

    ctx = ForgeFeatureContext(conn, normed_emb, oid_to_idx)
    cmdr = CmdrFeatureContext(ctx, krenko[0], set())
    cmdr.cmdr_subtypes = {"goblin", "warrior"}

    feats = compute_card_features(spirit_card_oid, row[1] or "", row[2] or 0.0, 0.0, ctx, cmdr)
    # F29 (index 29) should be > 0 because card triggers on Spirits, not Goblins
    assert feats[29] > 0, f"F29 should detect Spirit≠Goblin tribal conflict, got {feats[29]}"
```

- [ ] **Step 2: Run test**

Run: `python3 -m pytest tests/test_forge_features.py::test_f29_forge_anti_tribal -v`

- [ ] **Step 3: Replace F29 computation**

Replace lines 553-575:

```python
    # F29: forge_anti_tribal — card's Forge trigger_filter requires a creature subtype
    # that conflicts with the commander's type. Replaces oracle text regex.
    # Captures: Spirit Arcane trigger in a Goblin deck, Knight trigger in Human deck
    anti_tribal = 0.0
    if cmdr.cmdr_subtypes and card_trigger_types:
        # Generic types that don't conflict with tribal identity
        generic_types = {"card", "creature", "permanent", "nontoken",
                        "token", "artifact", "enchantment", "land",
                        "spell", "self", "other", "any"}
        for tf in card_trigger_types:
            if tf not in generic_types and tf not in cmdr.cmdr_subtypes:
                anti_tribal = 1.0
                break
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_forge_features.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add mtg_synergy/recommend/forge_features.py tests/test_forge_features.py
git commit -m "feat: replace F29 oracle regex anti-tribal with Forge trigger_filter conflict"
```

---

### Task 6: Replace F30 (mechanic_match) with Forge verb alignment

Currently F30 matches 14 hardcoded phrases between oracle texts. Replace with: count of Forge verbs in the card that match Forge trigger_modes in the commander (and vice versa), using the verb_event_map as the bridge.

**Files:**
- Modify: `mtg_synergy/recommend/forge_features.py:577-593`
- Test: `tests/test_forge_features.py`

- [ ] **Step 1: Write failing test**

```python
def test_f30_forge_verb_trigger_alignment(conn):
    """F30 should detect verb→trigger alignment via Forge, not oracle text phrases."""
    import numpy as np
    from mtg_synergy.recommend.forge_features import (
        ForgeFeatureContext, CmdrFeatureContext, compute_card_features
    )

    # Find a commander with ChangesZone trigger (ETB trigger)
    # and a card that has Token verb (creates things that enter battlefield)
    # This is a direct mechanical synergy

    kyler = conn.execute(
        "SELECT oracle_id FROM cards WHERE name = 'Kyler, Sigardian Emissary'"
    ).fetchone()
    if not kyler:
        pytest.skip("Kyler not in DB")
    kyler_oid = kyler[0]

    # Find a Token-creating card
    token_card = conn.execute("""
        SELECT fnm.oracle_id, c.type_line, c.cmc FROM forge_abilities fa
        JOIN forge_name_map fnm ON fnm.forge_name = fa.card_name
        JOIN cards c ON c.oracle_id = fnm.oracle_id
        WHERE fa.verb = 'Token'
        LIMIT 1
    """).fetchone()
    if not token_card:
        pytest.skip("No Token cards")

    oid_list = [r[0] for r in conn.execute("SELECT oracle_id FROM cards LIMIT 5000")]
    normed_emb = np.random.randn(len(oid_list), 768).astype(np.float16)
    oid_to_idx = {oid: i for i, oid in enumerate(oid_list)}

    ctx = ForgeFeatureContext(conn, normed_emb, oid_to_idx)
    cmdr = CmdrFeatureContext(ctx, kyler_oid, set())
    cmdr.cmdr_subtypes = {"human"}

    feats = compute_card_features(token_card[0], token_card[1] or "", token_card[2] or 0.0, 0.0, ctx, cmdr)
    # F30 (index 30) should be > 0 — Token verb produces what ChangesZone consumes
    assert feats[30] > 0, f"F30 should detect Token→ChangesZone alignment, got {feats[30]}"
```

- [ ] **Step 2: Run test**

Run: `python3 -m pytest tests/test_forge_features.py::test_f30_forge_verb_trigger_alignment -v`

- [ ] **Step 3: Build verb→trigger alignment mapping and replace F30**

First, add the alignment mapping to `ForgeFeatureContext.__init__` (after forge profiles loading):

```python
        # Verb→trigger alignment: which Forge verbs produce events that
        # which Forge trigger_modes consume. Used for F30.
        self._verb_triggers = {
            "Token": {"ChangesZone", "ChangesZoneAll", "TokenCreated"},
            "ChangeZone": {"ChangesZone", "ChangesZoneAll"},
            "ChangeZoneAll": {"ChangesZone", "ChangesZoneAll"},
            "DealDamage": {"DamageDone", "DamageDoneOnce", "LifeLost"},
            "DamageAll": {"DamageDone", "DamageDoneOnce"},
            "Draw": {"Drawn"},
            "Dig": {"Drawn"},
            "PutCounter": {"CounterAdded", "CounterAddedOnce"},
            "Proliferate": {"CounterAdded", "CounterAddedOnce"},
            "GainLife": {"LifeGained"},
            "LoseLife": {"LifeLost"},
            "Destroy": {"ChangesZone"},  # destroyed → goes to graveyard
            "DestroyAll": {"ChangesZone"},
            "Sacrifice": {"Sacrificed", "ChangesZone"},
            "Discard": {"Discarded"},
            "Mill": {"Milled", "ChangesZone"},
            "Tap": {"Taps", "TapsForMana"},
            "Untap": {"Untaps"},
            "Counter": {"SpellCast"},  # counters respond to spells being cast
            "Mana": {"TapsForMana"},
        }
        # Build reverse mapping: trigger → verbs that feed it
        self._trigger_verbs = {}
        for verb, triggers in self._verb_triggers.items():
            for trig in triggers:
                self._trigger_verbs.setdefault(trig, set()).add(verb)
```

Then replace F30 computation (lines 577-593):

```python
    # F30: forge_verb_alignment — card's Forge verbs produce events that commander's
    # Forge triggers consume, and vice versa. Replaces hardcoded phrase matching.
    verb_align = 0.0
    card_verbs = card_profile.get('verbs', set())
    card_trigs = card_profile.get('triggers', set())
    cmdr_trigs = cmdr_profile.get('triggers', set())
    cmdr_verbs = cmdr_profile.get('verbs', set())
    # Card produces → commander consumes
    for v in card_verbs:
        matching_trigs = ctx._verb_triggers.get(v, set())
        verb_align += len(matching_trigs & cmdr_trigs)
    # Commander produces → card consumes
    for v in cmdr_verbs:
        matching_trigs = ctx._verb_triggers.get(v, set())
        verb_align += len(matching_trigs & card_trigs)
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_forge_features.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add mtg_synergy/recommend/forge_features.py tests/test_forge_features.py
git commit -m "feat: replace F30 oracle phrase matching with Forge verb→trigger alignment"
```

---

### Task 7: Add new features F33-F39 from unused Forge fields

Add 7 new features leveraging Forge data we currently ignore: counter_type match, ability_type profile, zone alignment, target alignment, Forge keyword match, scaling effect detection, and activated ability count.

**Files:**
- Modify: `mtg_synergy/recommend/forge_features.py:425-640` (compute_card_features)
- Modify: `train_fusion_model.py:47-81` (FORGE_FEATURE_NAMES)
- Test: `tests/test_forge_features.py`

- [ ] **Step 1: Write failing tests for new features**

```python
def test_f33_counter_type_match(conn):
    """F33 should detect when card and commander use same counter type."""
    import numpy as np
    from mtg_synergy.recommend.forge_features import (
        ForgeFeatureContext, CmdrFeatureContext, compute_card_features
    )

    # Kyler uses P1P1 counters — find another P1P1 card
    kyler = conn.execute(
        "SELECT oracle_id FROM cards WHERE name = 'Kyler, Sigardian Emissary'"
    ).fetchone()
    if not kyler:
        pytest.skip("Kyler not in DB")

    p1p1_card = conn.execute("""
        SELECT fnm.oracle_id, c.type_line, c.cmc FROM forge_abilities fa
        JOIN forge_name_map fnm ON fnm.forge_name = fa.card_name
        JOIN cards c ON c.oracle_id = fnm.oracle_id
        WHERE fa.counter_type = 'P1P1' AND fnm.oracle_id != ?
        LIMIT 1
    """, (kyler[0],)).fetchone()
    if not p1p1_card:
        pytest.skip("No other P1P1 cards")

    oid_list = [r[0] for r in conn.execute("SELECT oracle_id FROM cards LIMIT 5000")]
    normed_emb = np.random.randn(len(oid_list), 768).astype(np.float16)
    oid_to_idx = {oid: i for i, oid in enumerate(oid_list)}

    ctx = ForgeFeatureContext(conn, normed_emb, oid_to_idx)
    cmdr = CmdrFeatureContext(ctx, kyler[0], set())
    cmdr.cmdr_subtypes = {"human"}

    feats = compute_card_features(p1p1_card[0], p1p1_card[1] or "", p1p1_card[2] or 0.0, 0.0, ctx, cmdr)
    # F33 should be > 0 — both use P1P1 counters
    assert feats[33] > 0, f"F33 should detect P1P1 counter type match, got {feats[33]}"


def test_feature_count_is_40(conn):
    """compute_card_features should return 40 features after adding F33-F39."""
    import numpy as np
    from mtg_synergy.recommend.forge_features import (
        ForgeFeatureContext, CmdrFeatureContext, compute_card_features
    )

    krenko = conn.execute(
        "SELECT oracle_id FROM cards WHERE name = 'Krenko, Mob Boss'"
    ).fetchone()
    if not krenko:
        pytest.skip("Krenko not in DB")
    krenko_oid = krenko[0]

    card = conn.execute(
        "SELECT oracle_id, type_line, cmc FROM cards "
        "WHERE oracle_id != ? LIMIT 1", (krenko_oid,)
    ).fetchone()

    oid_list = [r[0] for r in conn.execute("SELECT oracle_id FROM cards LIMIT 5000")]
    normed_emb = np.random.randn(len(oid_list), 768).astype(np.float16)
    oid_to_idx = {oid: i for i, oid in enumerate(oid_list)}

    ctx = ForgeFeatureContext(conn, normed_emb, oid_to_idx)
    cmdr = CmdrFeatureContext(ctx, krenko_oid, set())
    cmdr.cmdr_subtypes = {"goblin"}

    feats = compute_card_features(card[0], card[1] or "", card[2] or 0.0, 0.0, ctx, cmdr)
    assert len(feats) == 40, f"Expected 40 features, got {len(feats)}"
```

- [ ] **Step 2: Run tests to verify failure**

Run: `python3 -m pytest tests/test_forge_features.py::test_f33_counter_type_match tests/test_forge_features.py::test_feature_count_is_40 -v`
Expected: FAIL — feature index 33 doesn't exist, count is 33

- [ ] **Step 3: Update FORGE_FEATURE_NAMES in train_fusion_model.py**

Replace lines 47-81:

```python
FORGE_FEATURE_NAMES = [
    "tower_forge",              # [0] forge tower (causal graph connectivity)
    "embedding_cosine",         # [1] card2vec embedding cosine(cmdr, card)
    "causal_cmdr_to_card",      # [2] commander → card edge strength
    "causal_card_to_cmdr",      # [3] card → commander edge strength
    "causal_bidirectional",     # [4] 1.0 if both directions have edges
    "causal_event_diversity",   # [5] distinct event types connecting cmdr↔card
    "deck_edge_count",          # [6] deck cards with causal edges to this card
    "strategy_overlap",         # [7] shared strategies count
    "strategy_cosine",          # [8] strategy vector cosine similarity
    "oracle_similarity",        # [9] oracle text TF-IDF cosine similarity
    "phase_match",              # [10] cmdr and card trigger in same phase window
    "has_phase_trigger",        # [11] card has any phase-based trigger
    "tribal_match",             # [12] creature type match
    "type_creature",            # [13] card is a Creature
    "type_instant_sorcery",     # [14] card is Instant or Sorcery
    "type_artifact",            # [15] card is an Artifact
    "type_enchantment",         # [16] card is an Enchantment
    "type_land",                # [17] card is a Land
    "type_planeswalker",        # [18] card is a Planeswalker
    "cmc",                      # [19] mana cost
    "deck_exact_edge_ratio",    # [20] fraction of deck edges with exact filter precision
    "cmdr_exact_edge",          # [21] 1.0 if any exact-precision edge to commander
    "causal_composite",         # [22] combined causal signal (strength × events × exact)
    "card_hub_score",           # [23] total unique causal neighbors (connectedness)
    "deck_exact_count",         # [24] absolute count of exact-precision deck connections
    "forge_type_synergy",       # [25] card's trigger_filter/target refs cmdr subtypes
    "cmdr_forge_type_match",    # [26] cmdr's trigger_filter/target refs card subtypes
    "shared_forge_mechanics",   # [27] shared Forge verbs + triggers + keywords count
    "cmdr_keyword_match",       # [28] commander-specific oracle keywords in card (semantic)
    "forge_anti_tribal",        # [29] card's trigger_filter requires conflicting type
    "forge_verb_alignment",     # [30] card verbs → cmdr triggers + cmdr verbs → card triggers
    "forge_mech_fwd",           # [31] card produces what commander consumes (mechanics vector)
    "forge_mech_rev",           # [32] commander produces what card consumes (mechanics vector)
    "counter_type_match",       # [33] card uses same counter type as commander
    "ability_type_ratio_T",     # [34] fraction of card's abilities that are Triggered
    "ability_type_ratio_A",     # [35] fraction of card's abilities that are Activated
    "zone_alignment",           # [36] card's trigger zones match commander's interaction zones
    "target_alignment",         # [37] card targets what commander produces (types)
    "forge_keyword_synergy",    # [38] card has keywords that synergize with cmdr's mechanics
    "activated_ability_count",  # [39] number of activated abilities (tap/sac outlets scale)
]
```

- [ ] **Step 4: Add CmdrFeatureContext forge profile caching**

In `CmdrFeatureContext.__init__` (after line 232), add:

```python
        # Commander's Forge profile for feature computation
        self.cmdr_profile = ctx._forge_profiles.get(cmdr_oid, {
            'verbs': set(), 'triggers': set(), 'keywords': set(),
            'counter_types': set(), 'targets': set(), 'ability_types': set(),
            'trigger_filters': set(),
        })
```

- [ ] **Step 5: Add F33-F39 computation in compute_card_features**

After the F31/F32 computation (line 604), add before the return statement:

```python
    # F33: counter_type_match — card uses same counter type as commander
    # P1P1 counters synergize with proliferate/counter commanders
    cmdr_counters = cmdr.cmdr_profile.get('counter_types', set())
    card_counters = card_profile.get('counter_types', set())
    counter_match = float(len(cmdr_counters & card_counters)) if cmdr_counters and card_counters else 0.0

    # F34: ability_type_ratio_T — fraction of Triggered abilities
    # Triggered-heavy cards create value from game events (aristocrats, ETB)
    card_atypes = card_profile.get('ability_types', set())
    n_abilities = len(card_atypes) if card_atypes else 1
    ratio_T = 1.0 if 'T' in card_atypes else 0.0

    # F35: ability_type_ratio_A — fraction of Activated abilities
    # Activated-heavy cards provide repeatable value (tap outlets, sacrifice outlets)
    ratio_A = 1.0 if 'A' in card_atypes else 0.0

    # F36: zone_alignment — card's trigger zones match commander's interaction pattern
    # Cards triggering from graveyard align with reanimator commanders, etc.
    cmdr_zones = set()
    card_zones = set()
    for row in ctx.conn.execute(
        "SELECT DISTINCT fa.trigger_zones FROM forge_abilities fa "
        "JOIN forge_name_map fnm ON fnm.forge_name = fa.card_name "
        "WHERE fnm.oracle_id = ? AND fa.trigger_zones IS NOT NULL", (cmdr.cmdr_oid,)):
        if row[0]:
            cmdr_zones.update(z.strip() for z in row[0].split(","))
    for row in ctx.conn.execute(
        "SELECT DISTINCT fa.trigger_zones FROM forge_abilities fa "
        "JOIN forge_name_map fnm ON fnm.forge_name = fa.card_name "
        "WHERE fnm.oracle_id = ? AND fa.trigger_zones IS NOT NULL", (card_oid,)):
        if row[0]:
            card_zones.update(z.strip() for z in row[0].split(","))
    zone_align = float(len(cmdr_zones & card_zones)) if cmdr_zones and card_zones else 0.0

    # F37: target_alignment — card targets what the commander produces
    # A card targeting Creatures synergizes with a commander that creates creature tokens
    cmdr_prod_types = set()
    if cmdr.cmdr_produces is not None:
        # Check mechanics vector concepts for what commander produces
        from mtg_synergy.recommend.mechanics_vectors import GAME_CONCEPTS, _concept_idx
        for concept in ["creature_enters", "artifact_enters", "enchantment_enters",
                       "token_created", "counter_added"]:
            idx = _concept_idx.get(concept)
            if idx is not None and cmdr.cmdr_produces[idx] > 0:
                # Map concept back to target type
                if "creature" in concept: cmdr_prod_types.add("Creature")
                if "artifact" in concept: cmdr_prod_types.add("Artifact")
                if "enchantment" in concept: cmdr_prod_types.add("Enchantment")
                if "token" in concept: cmdr_prod_types.add("Creature")
    card_tgts = card_profile.get('targets', set())
    target_align = float(len(cmdr_prod_types & card_tgts)) if cmdr_prod_types and card_tgts else 0.0

    # F38: forge_keyword_synergy — card has keywords that mechanically synergize
    # with commander's abilities (e.g., Flying + "whenever a creature with flying")
    card_kws = card_profile.get('keywords', set())
    cmdr_filter_kws = cmdr.cmdr_profile.get('trigger_filters', set())
    kw_syn = 0.0
    # Keywords that appear in commander's trigger_filter → direct synergy
    kw_to_filter = {
        "Flying": "flying", "Trample": "trample", "Haste": "haste",
        "Deathtouch": "deathtouch", "Lifelink": "lifelink", "Menace": "menace",
        "First Strike": "firststrike", "Double Strike": "doublestrike",
        "Hexproof": "hexproof", "Indestructible": "indestructible",
        "Vigilance": "vigilance", "Reach": "reach",
    }
    for kw in card_kws:
        filter_form = kw_to_filter.get(kw, kw.lower().replace(" ", ""))
        if any(filter_form in f for f in cmdr_filter_kws):
            kw_syn += 1.0
    # Commander produces creatures/tokens + card has combat keywords = synergy
    if cmdr.cmdr_produces is not None:
        creature_idx = _concept_idx.get("creature_enters")
        if creature_idx is not None and cmdr.cmdr_produces[creature_idx] > 0:
            combat_kws = {"Flying", "Trample", "Haste", "Menace", "Double Strike", "First Strike"}
            kw_syn += float(len(card_kws & combat_kws)) * 0.3

    # F39: activated_ability_count — number of activated abilities
    # More activated abilities = more utility, more synergy with untap effects
    n_activated = 0
    for row in ctx.conn.execute(
        "SELECT COUNT(*) FROM forge_abilities fa "
        "JOIN forge_name_map fnm ON fnm.forge_name = fa.card_name "
        "WHERE fnm.oracle_id = ? AND fa.ability_type = 'A'", (card_oid,)):
        n_activated = row[0]
    activated_count = float(min(n_activated, 5))
```

- [ ] **Step 6: Update the return list**

Replace the return statement to include new features:

```python
    return [
        f0,                                              # F0 tower_forge
        f1,                                              # F1 embedding_cosine
        min(out_s, 10.0),                                # F2 causal_cmdr_to_card
        min(in_s, 10.0),                                 # F3 causal_card_to_cmdr
        1.0 if (out_s > 0 and in_s > 0) else 0.0,       # F4 causal_bidirectional
        float(len(ev_out | ev_in)),                      # F5 causal_event_diversity
        float(min(cmdr.deck_edge_counts.get(card_oid, 0), 20)),  # F6 deck_edge_count
        float(len(cmdr.cmdr_strats & c_strats)),         # F7 strategy_overlap
        strat_cos,                                       # F8 strategy_cosine
        oracle_sim,                                      # F9 oracle_similarity
        phase_m,                                         # F10 phase_match
        1.0 if cp else 0.0,                              # F11 has_phase_trigger
        tribal,                                          # F12 tribal_match
        1.0 if "Creature" in tl else 0.0,                # F13 type_creature
        1.0 if ("Instant" in tl or "Sorcery" in tl) else 0.0,  # F14
        1.0 if "Artifact" in tl else 0.0,                # F15
        1.0 if "Enchantment" in tl else 0.0,             # F16
        1.0 if "Land" in tl else 0.0,                    # F17
        1.0 if "Planeswalker" in tl else 0.0,            # F18
        float(card_cmc),                                 # F19 cmc
        deck_exact_ratio,                                # F20 deck_exact_edge_ratio
        1.0 if card_oid in cmdr.cmdr_exact else 0.0,     # F21 cmdr_exact_edge
        causal_composite,                                # F22 causal_composite
        hub,                                             # F23 card_hub_score
        deck_exact_abs,                                  # F24 deck_exact_count
        forge_type_syn,                                  # F25 forge_type_synergy
        cmdr_type_match,                                 # F26 cmdr_forge_type_match
        shared_forge,                                    # F27 shared_forge_mechanics
        cmdr_kw_match,                                   # F28 cmdr_keyword_match (kept)
        anti_tribal,                                     # F29 forge_anti_tribal
        verb_align,                                      # F30 forge_verb_alignment
        mech_fwd,                                        # F31 forge_mech_fwd
        mech_rev,                                        # F32 forge_mech_rev
        counter_match,                                   # F33 counter_type_match
        ratio_T,                                         # F34 ability_type_ratio_T
        ratio_A,                                         # F35 ability_type_ratio_A
        zone_align,                                      # F36 zone_alignment
        target_align,                                    # F37 target_alignment
        kw_syn,                                          # F38 forge_keyword_synergy
        activated_count,                                 # F39 activated_ability_count
    ]
```

- [ ] **Step 7: Run all tests**

Run: `python3 -m pytest tests/test_forge_features.py -v`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add mtg_synergy/recommend/forge_features.py train_fusion_model.py tests/test_forge_features.py
git commit -m "feat: add 7 new Forge-derived features F33-F39 (counter, ability type, zones, targets, keywords, activated)"
```

---

### Task 8: Replace "for each" oracle fallback in mechanics_vectors.py

Lines 236-262 fall back to oracle text for "for each [Type]" and "tap an untapped [Type]" patterns. Replace by parsing the `SpellDescription` portion of Forge `raw_line` for `Amount$ X` abilities.

**Files:**
- Modify: `mtg_synergy/recommend/mechanics_vectors.py:236-262`
- Test: `tests/test_forge_features.py`

- [ ] **Step 1: Write failing test**

```python
def test_mechanics_vectors_no_oracle_text(conn):
    """mechanics_vectors should not query cards.oracle_text anymore."""
    import numpy as np
    from unittest.mock import patch
    from mtg_synergy.recommend.mechanics_vectors import build_mechanics_vectors

    # Patch conn.execute to track what tables are queried
    original_execute = conn.execute
    queries = []
    def tracking_execute(sql, *args, **kwargs):
        queries.append(sql)
        return original_execute(sql, *args, **kwargs)

    with patch.object(conn, 'execute', side_effect=tracking_execute):
        produces, consumes, dim, subtype_idx = build_mechanics_vectors(conn)

    # Should NOT query cards.oracle_text
    oracle_queries = [q for q in queries if 'oracle_text' in q.lower()]
    assert len(oracle_queries) == 0, (
        f"mechanics_vectors should not query oracle_text, but found: {oracle_queries}"
    )

    # Should still produce/consume vectors
    assert len(produces) > 0
    assert len(consumes) > 0
```

- [ ] **Step 2: Run test to verify failure**

Run: `python3 -m pytest tests/test_forge_features.py::test_mechanics_vectors_no_oracle_text -v`
Expected: FAIL — oracle_text queries found

- [ ] **Step 3: Replace oracle text fallback with Forge raw_line parsing**

Replace lines 236-262 in mechanics_vectors.py:

```python
    # Parse "for each [Type]" scaling from Forge raw_line SpellDescription
    # (replaces oracle text fallback). Amount=X abilities with "for each" in
    # their SpellDescription encode entity-count scaling that Forge doesn't
    # capture in structured fields.
    for row in conn.execute(
        "SELECT fnm.oracle_id, fa.raw_line FROM forge_abilities fa "
        "JOIN forge_name_map fnm ON fnm.forge_name = fa.card_name "
        "WHERE fa.amount = 'X' AND fa.raw_line LIKE '%for each%'"
    ):
        oid, raw = row[0], (row[1] or "").lower()
        idx = raw.find("for each")
        if idx < 0:
            continue
        snippet = raw[idx:]
        for m in re.finditer(r"for each (\w+)", snippet):
            t = m.group(1)
            if t in subtype_idx:
                c = consumes.setdefault(oid, np.zeros(dim, dtype=np.float32))
                c[subtype_idx[t]] += 1.0
            elif t == "creature":
                c = consumes.setdefault(oid, np.zeros(dim, dtype=np.float32))
                c[_concept_idx["creature_available"]] += 1.0
            elif t == "artifact":
                c = consumes.setdefault(oid, np.zeros(dim, dtype=np.float32))
                c[_concept_idx["artifact_enters"]] += 0.5

    # Parse "tap an untapped [Type]" from Forge raw_line (Sac/Tap cost patterns)
    # Instead of oracle text: parse cost field and raw_line for tap costs
    for row in conn.execute(
        "SELECT fnm.oracle_id, fa.raw_line FROM forge_abilities fa "
        "JOIN forge_name_map fnm ON fnm.forge_name = fa.card_name "
        "WHERE fa.raw_line LIKE '%Tap<%'"
    ):
        oid, raw = row[0], (row[1] or "").lower()
        for m in re.finditer(r"tap<\d+/([^/>]+)", raw):
            t = m.group(1).split("/")[0].split(".")[0].lower()
            if t in subtype_idx:
                c = consumes.setdefault(oid, np.zeros(dim, dtype=np.float32))
                c[subtype_idx[t]] += 1.0
                c[_concept_idx["creature_tapped"]] += 0.5
            elif t == "creature":
                c = consumes.setdefault(oid, np.zeros(dim, dtype=np.float32))
                c[_concept_idx["creature_tapped"]] += 1.0
                c[_concept_idx["creature_available"]] += 0.5
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_forge_features.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add mtg_synergy/recommend/mechanics_vectors.py tests/test_forge_features.py
git commit -m "feat: replace oracle text fallback with Forge raw_line parsing in mechanics vectors"
```

---

### Task 9: Upgrade commander_profile.py to use Forge verb/trigger profiles

Currently `infer_profile()` matches oracle text against `STRATEGY_KEYWORDS` dict. Replace with Forge verb/trigger profile analysis — the same approach `strategy_detector.py` already uses.

**Files:**
- Modify: `mtg_synergy/recommend/commander_profile.py:85-145`
- Test: `tests/test_forge_features.py`

- [ ] **Step 1: Write failing test**

```python
def test_commander_profile_uses_forge(conn):
    """infer_profile should detect strategies from Forge verbs, not oracle text."""
    from mtg_synergy.recommend.commander_profile import infer_profile

    # Kyler: has PutCounter verb + ChangesZone trigger on Human
    kyler = conn.execute(
        "SELECT oracle_id, oracle_text, type_line FROM cards WHERE name = 'Kyler, Sigardian Emissary'"
    ).fetchone()
    if not kyler:
        pytest.skip("Kyler not in DB")

    # Get Forge data for Kyler
    forge_verbs = set()
    forge_triggers = set()
    for row in conn.execute(
        "SELECT fa.verb, fa.trigger_mode FROM forge_abilities fa "
        "JOIN forge_name_map fnm ON fnm.forge_name = fa.card_name "
        "WHERE fnm.oracle_id = ?", (kyler[0],)):
        if row[0]: forge_verbs.add(row[0])
        if row[1]: forge_triggers.add(row[1])

    profile = infer_profile(
        oracle_text=kyler[1] or "",
        type_line=kyler[2] or "",
        forge_verbs=forge_verbs,
        forge_triggers=forge_triggers,
    )
    # Should detect +1/+1-counters from PutCounter verb
    assert "+1/+1-counters" in profile.strategies, (
        f"Kyler should be +1/+1-counters from PutCounter verb, got {profile.strategies}"
    )
    # Should detect tribal-human from type line
    assert "tribal-human" in profile.strategies
```

- [ ] **Step 2: Run test to verify failure**

Run: `python3 -m pytest tests/test_forge_features.py::test_commander_profile_uses_forge -v`
Expected: FAIL — `forge_verbs` parameter doesn't exist

- [ ] **Step 3: Update infer_profile to accept and use Forge data**

Replace `infer_profile` function (lines 85-145) in commander_profile.py:

```python
# Forge verb → strategy mapping (replaces STRATEGY_KEYWORDS oracle text matching)
_FORGE_VERB_STRATEGIES = {
    "Token": "tokens",
    "PutCounter": "+1/+1-counters",
    "PutCounterAll": "+1/+1-counters",
    "Proliferate": "+1/+1-counters",
    "Sacrifice": "aristocrats",
    "CopySpellAbility": "spellslinger",
    "GainLife": "lifegain",
    "LoseLife": "lifedrain",
    "DealDamage": "burn",
    "DamageAll": "burn",
    "Mill": "mill",
    "Draw": "card-draw",
    "Dig": "card-draw",
    "Equip": "equipment",
    "Enchant": "enchantress",
    "PumpAll": "go-wide",
    "Mana": "ramp",
}

_FORGE_TRIGGER_STRATEGIES = {
    "Attacks": "voltron",
    "AttackersDeclared": "go-wide",
    "SpellCast": "spellslinger",
    "Sacrificed": "aristocrats",
    "LifeGained": "lifegain",
    "Discarded": "wheels",
    "Drawn": "card-draw",
    "ChangesZone": "blink",  # weak signal — many cards have this
}


def infer_profile(
    oracle_text: str,
    type_line: str,
    parsed_events_produced: set[str] | None = None,
    parsed_events_consumed: set[str] | None = None,
    parsed_effects: set[str] | None = None,
    forge_verbs: set[str] | None = None,
    forge_triggers: set[str] | None = None,
) -> CommanderProfile:
    """Infer commander archetype from Forge ability data + type line.

    Primary strategy detection uses Forge verb/trigger profiles.
    Oracle text keyword matching kept as fallback for cards without Forge data.
    """
    strategies = set()
    events_produced = parsed_events_produced or set()
    events_consumed = parsed_events_consumed or set()
    effects = parsed_effects or set()

    # 1. Primary: Forge verb-based strategy detection
    if forge_verbs:
        for verb in forge_verbs:
            strat = _FORGE_VERB_STRATEGIES.get(verb)
            if strat:
                strategies.add(strat)

    # 2. Forge trigger-based strategy detection
    if forge_triggers:
        for trig in forge_triggers:
            strat = _FORGE_TRIGGER_STRATEGIES.get(trig)
            if strat:
                strategies.add(strat)

    # 3. Fallback: oracle text keywords (only if no Forge data)
    if not forge_verbs and not forge_triggers:
        oracle_lower = oracle_text.lower()
        for strat, keywords in STRATEGY_KEYWORDS.items():
            hits = sum(1 for kw in keywords if kw in oracle_lower)
            if hits >= 2:
                strategies.add(strat)

    # 4. Event-based strategy detection (from parsed abilities)
    for event, strat in _EVENT_TO_STRATEGY.items():
        if event in events_consumed:
            strategies.add(strat)

    # 5. Effect-based strategy detection
    for eff, strat in _EFFECT_TO_STRATEGY.items():
        if eff in effects:
            strategies.add(strat)

    # 6. Tribal detection from type line
    tribal_type = None
    if type_line and "\u2014" in type_line:
        try:
            subtypes = type_line.split("\u2014")[1].strip().split()
            for st in subtypes:
                if st.lower() in _TRIBAL_SUBTYPES:
                    tribal_type = st
                    break
        except (IndexError, AttributeError):
            pass

    # Also check Forge trigger_filter for tribal references
    if tribal_type is None and forge_triggers:
        # If commander triggers on a specific creature type, it's tribal
        pass  # Already handled by trigger_filter in ForgeFeatureContext

    if tribal_type:
        strategies.add(f"tribal-{tribal_type.lower()}")

    return CommanderProfile(
        strategies=strategies,
        tribal_type=tribal_type,
        key_events_produced=events_produced,
        key_events_consumed=events_consumed,
        key_effects=effects,
    )
```

- [ ] **Step 4: Update callers of infer_profile to pass Forge data**

In the same file, find `save_profile` / `load_profile` callers. The main caller is in `train_fusion_model.py` or wherever profiles are populated. Check with:

Run: `grep -rn "infer_profile" mtg_synergy/ train_fusion_model.py`

Update each caller to also query and pass `forge_verbs` and `forge_triggers`. The key caller is in `train_fusion_model.py` around profile population — add Forge data loading:

```python
# When calling infer_profile, also fetch Forge verbs/triggers
forge_verbs = set()
forge_triggers = set()
for row in conn.execute(
    "SELECT fa.verb, fa.trigger_mode FROM forge_abilities fa "
    "JOIN forge_name_map fnm ON fnm.forge_name = fa.card_name "
    "WHERE fnm.oracle_id = ?", (cmdr_oid,)):
    if row[0]: forge_verbs.add(row[0])
    if row[1]: forge_triggers.add(row[1])

profile = infer_profile(
    oracle_text=oracle_text,
    type_line=type_line,
    forge_verbs=forge_verbs,
    forge_triggers=forge_triggers,
)
```

- [ ] **Step 5: Run tests**

Run: `python3 -m pytest tests/test_forge_features.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add mtg_synergy/recommend/commander_profile.py tests/test_forge_features.py
git commit -m "feat: upgrade commander profile inference to use Forge verb/trigger profiles"
```

---

### Task 10: Remove unused oracle text loading from ForgeFeatureContext

After replacing F25-F30, the only oracle text usage remaining is F9 (TF-IDF similarity) and F28 (cmdr keyword match). Remove `_card_oracle` dict loading (line 50-53) since F25/F26/F27/F29/F30 no longer need it.

**Files:**
- Modify: `mtg_synergy/recommend/forge_features.py:49-53` and `508-551`

- [ ] **Step 1: Check which features still use _card_oracle**

After Tasks 2-6, the only remaining users of `_card_oracle` should be F28 (cmdr_keyword_match).
F28 uses `ctx._card_oracle.get(card_oid, "")` for card_words extraction, and cmdr_oracle is loaded from `ctx._card_oracle.get(cmdr.cmdr_oid, "")`.

F28 is intentionally kept as a semantic signal (not mechanical). Oracle text TF-IDF (F9) uses `_card_tokens` not `_card_oracle`.

- [ ] **Step 2: Remove _card_oracle loading since F28 can use _card_tokens**

Replace F28 computation to use TF-IDF tokens instead of raw oracle text:

```python
    # F28: cmdr_keyword_match — commander-specific vocabulary in card text
    # Uses TF-IDF token sets (already loaded) instead of raw oracle text
    cmdr_kw_match = 0.0
    if cmdr.cmdr_keywords:
        card_tokens_set = set(ctx._card_tokens.get(card_oid, {}).keys())
        cmdr_kw_match = float(len(cmdr.cmdr_keywords & card_tokens_set))
```

Then update `CmdrFeatureContext.__init__` (lines 234-261) to also use `_card_tokens`:

```python
        # Commander-specific keywords: top TF-IDF words from commander text
        self.cmdr_keywords = set()
        cmdr_tokens = ctx._card_tokens.get(cmdr_oid, {})
        if cmdr_tokens:
            stop = {"the", "and", "for", "you", "your", "each", "that", "this",
                    "its", "with", "from", "into", "than", "may", "can", "all",
                    "are", "has", "have", "one", "any", "other", "whenever",
                    "target", "card", "cards", "creature", "creatures", "spell",
                    "player", "players", "control", "controller", "opponent",
                    "opponents", "permanent", "permanents", "ability", "turn",
                    "end", "beginning", "step", "phase", "until", "put", "get",
                    "gets", "would", "instead", "also", "number", "choose",
                    "chosen", "where", "another", "then", "there", "their",
                    "they", "them", "already", "first", "give", "those", "been",
                    "being", "does", "only", "when", "more", "less", "don",
                    "among", "onto", "plus", "minus", "equal", "least", "many",
                    "much", "nor", "not", "either", "both", "every", "most",
                    "some", "such", "own", "over", "under", "before", "after"}
            cmdr_name_row = ctx.conn.execute(
                "SELECT name FROM cards WHERE oracle_id = ?", (cmdr_oid,)).fetchone()
            cmdr_name_parts = set()
            if cmdr_name_row:
                cmdr_name_parts = set(re.findall(r"[a-z]{3,}", cmdr_name_row[0].lower()))
            self.cmdr_keywords = {w for w in cmdr_tokens.keys()
                                  if w not in stop and w not in cmdr_name_parts}
```

Then remove `_card_oracle` loading (lines 49-53):

```python
        # _card_oracle removed — F25-F30 now use Forge profiles,
        # F28 uses _card_tokens (TF-IDF token sets)
```

- [ ] **Step 3: Run tests**

Run: `python3 -m pytest tests/test_forge_features.py -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add mtg_synergy/recommend/forge_features.py
git commit -m "refactor: remove _card_oracle dict — all features now use Forge profiles or TF-IDF tokens"
```

---

### Task 11: Pre-load zone data into ForgeFeatureContext to avoid per-card DB queries

F36 (zone_alignment) and F39 (activated_ability_count) currently do per-card DB queries inside `compute_card_features`. This is too slow for training (millions of pairs). Pre-load this data into `ForgeFeatureContext`.

**Files:**
- Modify: `mtg_synergy/recommend/forge_features.py` (ForgeFeatureContext.__init__ and compute_card_features)

- [ ] **Step 1: Add zone and ability count pre-loading to ForgeFeatureContext**

Add to `ForgeFeatureContext.__init__` (after forge profiles loading):

```python
        # Pre-load trigger zones per card (for F36)
        self._card_zones = {}
        for row in conn.execute(
            "SELECT fnm.oracle_id, fa.trigger_zones FROM forge_abilities fa "
            "JOIN forge_name_map fnm ON fnm.forge_name = fa.card_name "
            "WHERE fa.trigger_zones IS NOT NULL"
        ):
            oid, zones = row
            zset = self._card_zones.setdefault(oid, set())
            for z in zones.split(","):
                z = z.strip()
                if z:
                    zset.add(z)

        # Pre-load activated ability counts per card (for F39)
        self._activated_counts = {}
        for row in conn.execute(
            "SELECT fnm.oracle_id, COUNT(*) FROM forge_abilities fa "
            "JOIN forge_name_map fnm ON fnm.forge_name = fa.card_name "
            "WHERE fa.ability_type = 'A' "
            "GROUP BY fnm.oracle_id"
        ):
            self._activated_counts[row[0]] = row[1]
```

Also add to `CmdrFeatureContext.__init__`:

```python
        # Commander zones (for F36)
        self.cmdr_zones = ctx._card_zones.get(cmdr_oid, set())
```

- [ ] **Step 2: Replace per-card DB queries in compute_card_features**

Replace F36 computation:

```python
    # F36: zone_alignment — shared trigger zones between card and commander
    card_zones = ctx._card_zones.get(card_oid, set())
    zone_align = float(len(cmdr.cmdr_zones & card_zones)) if cmdr.cmdr_zones and card_zones else 0.0
```

Replace F39 computation:

```python
    # F39: activated_ability_count
    activated_count = float(min(ctx._activated_counts.get(card_oid, 0), 5))
```

- [ ] **Step 3: Run tests**

Run: `python3 -m pytest tests/test_forge_features.py -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add mtg_synergy/recommend/forge_features.py
git commit -m "perf: pre-load zones and ability counts to avoid per-card DB queries"
```

---

### Task 12: Delete old feature cache, rebuild features, and retrain

The feature count changed from 33 → 40 and feature semantics changed. Delete the old cache, rebuild, and retrain.

**Files:**
- Modify: none (runtime only)

- [ ] **Step 1: Back up old cache**

```bash
cp data/forge_features_cache.npz data/forge_features_cache.npz.bak2
```

- [ ] **Step 2: Delete old cache**

```bash
rm data/forge_features_cache.npz
```

- [ ] **Step 3: Rebuild features and retrain forge model**

```bash
python3 train_fusion_model.py --forge-only --rebuild-features
```

Expected output: Feature matrix with shape (N, 40), per-feature statistics showing non-zero counts for new features F33-F39, then LambdaRank training completing with NDCG metrics.

- [ ] **Step 4: Run comparison against EDHREC**

```bash
python3 compare_edhrec.py --forge --quiet
```

Record: On-EDHREC, Hi-Syn, NotEDH counts. Compare against previous baseline (avg 4.8/30 On-EDHREC, 0.9/30 Hi-Syn).

- [ ] **Step 5: Run single deck tests**

```bash
python3 synergy_graph.py --deck krenko --recommend --forge
python3 synergy_graph.py --deck kyler --recommend --forge
python3 synergy_graph.py --deck atraxa --recommend --forge
```

Verify recommendations look reasonable — cards should have clear mechanical synergy with commander.

- [ ] **Step 6: Run test suite**

```bash
python3 -m pytest tests/ -v --timeout 120
```

Expected: All tests pass

- [ ] **Step 7: Commit the trained model**

```bash
git add data/forge_features_cache.npz data/fusion_model_forge.lgb
git commit -m "feat: retrain forge model with 40 Forge-native features (7 new, 5 replaced)"
```

---

### Task 13: Update CLAUDE.md with new feature architecture

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Update the feature list and counts**

In the `### Fusion Models` section, update the Forge-only model description:

- Change "33 features" to "40 features"
- Update feature list to reflect new names (F25-F30 renamed, F33-F39 added)
- Update the "Top features" importance line after retraining
- Note that oracle text parsing has been eliminated from F25-F30

- [ ] **Step 2: Update the Signal Architecture section**

In `### Signal Architecture`, update:
- "33 features" → "40 features"
- Add note: "Forge-native: all features derived from Forge structured data except F9 (TF-IDF semantic similarity) and F28 (cmdr keyword overlap)"

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: update CLAUDE.md for 40 Forge-native features, oracle text elimination"
```
