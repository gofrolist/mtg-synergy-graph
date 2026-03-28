# New Features + Zone-Aware Mechanics Vectors Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Improve forge model NDCG@30 (currently 0.5257) by adding ~8 new discrete features from underexploited Forge data and expanding mechanics vectors with zone-aware concepts.

**Architecture:** Two independent improvements: (1) add 8 new GBM features extractable from existing forge_abilities data (ability counts, token complexity, zone interaction, efficiency), (2) expand the 107-dim mechanics vectors with 5 zone-aware game concepts using trigger_origin/destination data. Both feed into the existing `compute_card_features()` pipeline.

**Tech Stack:** Python, NumPy, LightGBM, SQLite

---

## Current State

- 63 features in `FORGE_FEATURE_NAMES` (train_fusion_model.py:47-111)
- 107-dim mechanics vectors: 27 game concepts + 80 subtypes (mechanics_vectors.py)
- NDCG@30 = 0.5257, 3-fold CV, LambdaRank
- Feature cache at `data/forge_features_cache.npz`

## Target

- 71 features (63 + 8 new)
- ~112-dim mechanics vectors (107 + 5 zone concepts)
- NDCG@30 improvement (any positive delta is success)

---

### Task 1: Expand mechanics vectors with zone-aware concepts

The mechanics vectors currently have 27 game concepts that don't distinguish WHERE things happen. Adding zone-aware concepts uses the trigger_origin/destination data (12% of abilities) to refine the synergy signal. This task is independent of Task 2.

**Files:**
- Modify: `src/mtg_synergy/recommend/mechanics_vectors.py:22-50` (GAME_CONCEPTS list)
- Modify: `src/mtg_synergy/recommend/mechanics_vectors.py:53-109` (VERB_TO_CONCEPTS, TRIGGER_TO_CONCEPTS)
- Modify: `src/mtg_synergy/recommend/mechanics_vectors.py:123-307` (build_mechanics_vectors function)
- Test: `tests/test_mechanics_vectors.py` (new file)

- [ ] **Step 1: Write tests for zone-aware mechanics vectors**

Create `tests/test_mechanics_vectors.py`:

```python
"""Tests for zone-aware mechanics vector expansion."""

import os
import sqlite3

import numpy as np
import pytest

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "tags.db")


def _build_vectors():
    if not os.path.exists(DB_PATH):
        pytest.skip("tags.db not found")
    from mtg_synergy.recommend.mechanics_vectors import (
        build_mechanics_vectors, GAME_CONCEPTS, N_CONCEPTS,
    )
    conn = sqlite3.connect(DB_PATH)
    produces, consumes, dim, subtype_idx = build_mechanics_vectors(conn)
    conn.close()
    return produces, consumes, dim, subtype_idx, GAME_CONCEPTS, N_CONCEPTS


def test_zone_concepts_exist():
    """New zone-aware concepts should be in GAME_CONCEPTS."""
    from mtg_synergy.recommend.mechanics_vectors import GAME_CONCEPTS
    zone_concepts = [
        "enters_from_graveyard",
        "enters_from_exile",
        "enters_from_hand",
        "goes_to_graveyard",
        "goes_to_exile",
    ]
    for concept in zone_concepts:
        assert concept in GAME_CONCEPTS, f"Missing zone concept: {concept}"


def test_dimension_increased():
    """Vector dimension should increase by 5 (zone concepts)."""
    produces, consumes, dim, subtype_idx, concepts, n_concepts = _build_vectors()
    # 27 old + 5 new = 32 concepts, plus ~80 subtypes
    assert n_concepts == 32, f"Expected 32 concepts, got {n_concepts}"
    assert dim == n_concepts + len(subtype_idx)


def test_graveyard_producer_has_zone_signal():
    """A card with ChangeZone verb + trigger_destination=Graveyard should produce goes_to_graveyard."""
    produces, consumes, dim, subtype_idx, concepts, n_concepts = _build_vectors()
    from mtg_synergy.recommend.mechanics_vectors import _concept_idx
    gy_idx = _concept_idx.get("goes_to_graveyard")
    assert gy_idx is not None
    # At least some cards should produce graveyard events
    gy_producers = [oid for oid, vec in produces.items() if vec[gy_idx] > 0]
    assert len(gy_producers) > 0, "No cards produce goes_to_graveyard signal"


def test_graveyard_consumer_has_zone_signal():
    """A card with ChangesZone trigger + trigger_origin=Graveyard should consume enters_from_graveyard."""
    produces, consumes, dim, subtype_idx, concepts, n_concepts = _build_vectors()
    from mtg_synergy.recommend.mechanics_vectors import _concept_idx
    efg_idx = _concept_idx.get("enters_from_graveyard")
    assert efg_idx is not None
    gy_consumers = [oid for oid, vec in consumes.items() if vec[efg_idx] > 0]
    assert len(gy_consumers) > 0, "No cards consume enters_from_graveyard signal"


def test_vectors_still_normalized():
    """All vectors should still be L2-normalized after expansion."""
    produces, consumes, dim, subtype_idx, concepts, n_concepts = _build_vectors()
    for oid, vec in list(produces.items())[:100]:
        norm = np.linalg.norm(vec)
        assert abs(norm - 1.0) < 1e-5, f"Produce vector for {oid} not normalized: {norm}"
    for oid, vec in list(consumes.items())[:100]:
        norm = np.linalg.norm(vec)
        assert abs(norm - 1.0) < 1e-5, f"Consume vector for {oid} not normalized: {norm}"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_mechanics_vectors.py -v`

Expected: `test_zone_concepts_exist` and `test_dimension_increased` fail (concepts don't exist yet).

- [ ] **Step 3: Add zone concepts to GAME_CONCEPTS**

In `src/mtg_synergy/recommend/mechanics_vectors.py`, add 5 new concepts after `"artifact_available"` (line 50):

```python
GAME_CONCEPTS = [
    "creature_enters",      # Token/ChangeZone → ChangesZone+Battlefield
    "artifact_enters",      # Token(artifact)/ChangeZone → ChangesZone+Battlefield
    "enchantment_enters",   # Attach/ChangeZone → ChangesZone+Battlefield
    "permanent_enters",     # Any permanent → ChangesZone+Battlefield
    "damage_dealt",         # DealDamage → DamageDone
    "card_drawn",           # Draw → Drawn
    "counter_added",        # PutCounter → CounterAdded
    "life_gained",          # GainLife → LifeGained
    "life_lost",            # LoseLife/DealDamage → LifeLost
    "creature_dies",        # Sacrifice/Destroy → ChangesZone to graveyard
    "permanent_destroyed",  # Destroy → Destroyed
    "card_discarded",       # Discard → Discarded
    "card_milled",          # Mill → Milled
    "spell_cast",           # Being a spell → SpellCast
    "creature_attacks",     # Being a creature → Attacks
    "creature_blocks",      # Being a creature → Blocks
    "target_chosen",        # Target abilities → BecomesTarget
    "creature_tapped",      # Tap → Taps
    "creature_untapped",    # Untap → Untaps
    "creature_pumped",      # Pump → power/toughness change
    "mana_produced",        # Mana → enables costs
    "token_created",        # Token → TokenCreated
    "creature_sacrificed",  # Sacrifice → Sacrificed
    "counter_removed",      # RemoveCounter → CounterRemoved
    "phase_trigger",        # Phase-based abilities
    "creature_available",   # Creature exists → can be tapped/sacrificed/counted
    "artifact_available",   # Artifact exists → can be sacrificed/tapped
    # Zone-aware concepts (from trigger_origin/destination data)
    "enters_from_graveyard",  # ChangesZone from Graveyard → Battlefield (reanimation)
    "enters_from_exile",      # ChangesZone from Exile → Battlefield (blink return)
    "enters_from_hand",       # ChangesZone from Hand → Battlefield (cheat into play)
    "goes_to_graveyard",      # ChangesZone to Graveyard (death/discard/mill)
    "goes_to_exile",          # ChangesZone to Exile (exile removal/blink)
]
```

- [ ] **Step 4: Update VERB_TO_CONCEPTS and TRIGGER_TO_CONCEPTS**

These mappings are intentionally left as-is — they map verbs/triggers to game concepts generically. The zone-aware concepts are populated from the actual trigger_origin/trigger_destination data in `build_mechanics_vectors`, not from static mappings. This is because only 12% of abilities have zone data, and the zone information is per-ability, not per-verb.

Add the zone-aware concepts to the TRIGGER_TO_CONCEPTS for specific zone-relevant triggers:

```python
TRIGGER_TO_CONCEPTS = {
    "ChangesZone":     ["creature_enters", "artifact_enters", "enchantment_enters", "permanent_enters", "creature_dies"],
    # ... existing entries unchanged ...
}
```

No changes needed to VERB_TO_CONCEPTS or TRIGGER_TO_CONCEPTS — zone-aware concepts are populated from trigger_origin/destination in the build function (Step 5).

- [ ] **Step 5: Update build_mechanics_vectors to use trigger_origin/destination**

The function signature already accepts `preloaded_abilities`. We need to:
1. Update the DB query (fallback path) to also fetch trigger_origin and trigger_destination.
2. Update the preloaded_abilities tuple format (add 2 new fields at the end).
3. Add zone-aware concept population in the main ability loop.

First, change the `preloaded_abilities` format. In `ForgeFeatureContext.__init__` (forge_features.py), update the `_raw_abilities` append to include trigger_origin and trigger_destination:

In `src/mtg_synergy/recommend/forge_features.py`, line 109-117, the SQL query already fetches from `forge_abilities`. We need to also fetch `trigger_origin` and `trigger_destination`. Update the query:

```python
        for row in conn.execute(
            "SELECT fnm.oracle_id, fa.verb, fa.trigger_mode, fa.keyword, "
            "fa.counter_type, fa.target, fa.ability_type, fa.trigger_filter, "
            "fa.cost, fa.defined, fa.raw_line, fa.token_script, fa.amount, "
            "fa.trigger_origin, fa.trigger_destination "
            "FROM forge_abilities fa "
            "JOIN forge_name_map fnm ON fnm.forge_name = fa.card_name"
        ):
```

And update the `_raw_abilities` tuple to append trigger_origin and trigger_destination at indices 10 and 11 (shifting raw_line to 8, amount to 9 — wait, the current mapping is complex, let me trace it).

Current `_raw_abilities` tuple format (line 117):
```
(oid, verb, trig_mode, trig_filter, cost, kw, token_script, counter, raw_line, amount)
```
Mapped from row indices: `(row[0], row[1], row[2], row[7], row[8], row[3], row[11], row[4], row[10], row[12])`

Add trigger_origin and trigger_destination at the end:
```python
self._raw_abilities.append((row[0], row[1], row[2], row[7], row[8], row[3], row[11], row[4], row[10], row[12], row[13], row[14]))
```

Now the tuple has 12 elements: indices 0-9 same as before, 10=trigger_origin, 11=trigger_destination.

Then in `build_mechanics_vectors`, update the DB fallback path to also fetch trigger_origin/trigger_destination, and process zone data in the main loop:

```python
def build_mechanics_vectors(conn, preloaded_abilities=None):
    # ... existing subtype counting code unchanged ...

    for ab in abilities:
        oid = ab[0]
        verb, trig_mode, trig_filter = ab[1], ab[2], ab[3]
        cost, token_script = ab[4], ab[6]
        raw_line = ab[8] or ""
        # New: zone data (indices 10, 11 in preloaded; fetched from DB otherwise)
        trig_origin = ab[10] if len(ab) > 10 else None
        trig_dest = ab[11] if len(ab) > 11 else None

        # --- PRODUCES: effect verb → game concepts ---
        if verb and verb in VERB_TO_CONCEPTS:
            p = produces.setdefault(oid, np.zeros(dim, dtype=np.float32))
            for concept in VERB_TO_CONCEPTS[verb]:
                p[_concept_idx[concept]] += 1.0

        # Zone-aware PRODUCES: verb + trigger_destination → zone concept
        if verb and trig_dest:
            p = produces.setdefault(oid, np.zeros(dim, dtype=np.float32))
            if "Graveyard" in trig_dest:
                p[_concept_idx["goes_to_graveyard"]] += 1.0
            if "Exile" in trig_dest:
                p[_concept_idx["goes_to_exile"]] += 1.0

        # Token with subtype → produces that subtype
        # ... existing token code unchanged ...

        # --- CONSUMES: trigger mode → game concepts ---
        if trig_mode and trig_mode in TRIGGER_TO_CONCEPTS:
            c = consumes.setdefault(oid, np.zeros(dim, dtype=np.float32))
            for concept in TRIGGER_TO_CONCEPTS[trig_mode]:
                c[_concept_idx[concept]] += 1.0

        # Zone-aware CONSUMES: trigger + trigger_origin → zone concept
        if trig_mode and trig_origin:
            c = consumes.setdefault(oid, np.zeros(dim, dtype=np.float32))
            if "Graveyard" in trig_origin:
                c[_concept_idx["enters_from_graveyard"]] += 1.0
            if "Exile" in trig_origin:
                c[_concept_idx["enters_from_exile"]] += 1.0
            if trig_origin == "Hand":
                c[_concept_idx["enters_from_hand"]] += 1.0

        # Also: verbs that specifically move things to graveyard/exile
        # ChangeZone destination tells us where things go
        if verb in ("ChangeZone", "ChangeZoneAll") and trig_dest:
            p = produces.setdefault(oid, np.zeros(dim, dtype=np.float32))
            if "Battlefield" in trig_dest and trig_origin:
                if "Graveyard" in trig_origin:
                    p[_concept_idx["enters_from_graveyard"]] += 1.0
                if "Exile" in trig_origin:
                    p[_concept_idx["enters_from_exile"]] += 1.0
                if trig_origin == "Hand":
                    p[_concept_idx["enters_from_hand"]] += 1.0

        # ... rest of existing code unchanged (trigger filter subtypes, costs, etc.)
```

For the DB fallback path in `build_mechanics_vectors`, update the query:

```python
        abilities = []
        for row in conn.execute(
            "SELECT fnm.oracle_id, fa.verb, fa.trigger_mode, fa.trigger_filter, fa.cost, "
            "fa.keyword, fa.token_script, fa.counter_type, fa.raw_line, fa.amount, "
            "fa.trigger_origin, fa.trigger_destination "
            "FROM forge_abilities fa "
            "JOIN forge_name_map fnm ON fnm.forge_name = fa.card_name"
        ):
            oid = forge_to_oid.get(row[0])
            if oid:
                abilities.append((oid, row[1], row[2], row[3], row[4],
                                  row[5], row[6], row[7], row[8], row[9],
                                  row[10], row[11]))
```

- [ ] **Step 6: Run mechanics vector tests**

Run: `uv run pytest tests/test_mechanics_vectors.py -v`

Expected: All 6 tests pass.

- [ ] **Step 7: Verify full test suite still passes**

Run: `uv run pytest tests/test_forge_features.py -v`

Expected: All existing tests pass (mechanics vector dim change is handled by the dynamic `dim` variable).

- [ ] **Step 8: Commit**

```bash
git add src/mtg_synergy/recommend/mechanics_vectors.py src/mtg_synergy/recommend/forge_features.py tests/test_mechanics_vectors.py
git commit -m "feat: expand mechanics vectors with 5 zone-aware concepts (graveyard, exile, hand)"
```

---

### Task 2: Add 8 new discrete features

Add features from underexploited Forge data that provide distinct signals from existing features. These use data already loaded in `ForgeFeatureContext.__init__`.

**Files:**
- Modify: `src/mtg_synergy/recommend/forge_features.py:46-53` (ForgeFeatureContext.__init__ — add new pre-computed data)
- Modify: `src/mtg_synergy/recommend/forge_features.py:852-1265` (compute_card_features — add 8 features)
- Modify: `train_fusion_model.py:47-111` (FORGE_FEATURE_NAMES — add 8 names)
- Test: `tests/test_forge_features.py` (add tests for new features)

#### New features:

| # | Name | Signal | Source |
|---|------|--------|--------|
| F63 | `total_ability_count` | Combo potential — more abilities = more interaction points | Count of forge_abilities rows per card |
| F64 | `triggered_ability_count` | Ordinal triggered count (replaces binary F32) | Count of ability_type='T' per card |
| F65 | `token_power_toughness` | Token quality — 1/1 goblin vs 6/6 demon | Parse P/T from token_script |
| F66 | `token_keyword_count` | Token complexity — tokens with keywords are stronger | Parse keyword count from token_script |
| F67 | `zone_graveyard_interact` | Card + commander both interact with graveyard | trigger_origin/destination containing "Graveyard" |
| F68 | `zone_exile_interact` | Card + commander both interact with exile | trigger_origin/destination containing "Exile" |
| F69 | `ability_density` | Efficiency — abilities per mana cost | total_ability_count / max(cmc, 1) |
| F70 | `mech_zone_fwd` | Card produces zone events commander consumes | Dot product of zone-subset of mechanics vectors |

- [ ] **Step 1: Add pre-computed counts to ForgeFeatureContext.__init__**

In `src/mtg_synergy/recommend/forge_features.py`, after the `self._activated_counts` block (line 367-375), add:

```python
        # Pre-load total ability counts per card (for F63)
        self._total_ability_counts = {}
        for row in conn.execute(
            "SELECT fnm.oracle_id, COUNT(*) FROM forge_abilities fa "
            "JOIN forge_name_map fnm ON fnm.forge_name = fa.card_name "
            "GROUP BY fnm.oracle_id"
        ):
            self._total_ability_counts[row[0]] = row[1]

        # Pre-load triggered ability counts per card (for F64)
        self._triggered_counts = {}
        for row in conn.execute(
            "SELECT fnm.oracle_id, COUNT(*) FROM forge_abilities fa "
            "JOIN forge_name_map fnm ON fnm.forge_name = fa.card_name "
            "WHERE fa.ability_type = 'T' "
            "GROUP BY fnm.oracle_id"
        ):
            self._triggered_counts[row[0]] = row[1]

        # Pre-load token stats per card (for F65, F66)
        # Parse token_script format: color_P_T_subtypes_keywords
        self._token_max_pt = {}      # oid -> max P+T across all token scripts
        self._token_max_kw = {}      # oid -> max keyword count across all token scripts
        _kw_skip = {"sac", "draw"}   # not real keywords
        for row in conn.execute(
            "SELECT fnm.oracle_id, fa.token_script FROM forge_abilities fa "
            "JOIN forge_name_map fnm ON fnm.forge_name = fa.card_name "
            "WHERE fa.token_script IS NOT NULL"
        ):
            oid, ts = row
            parts = ts.lower().split("_")
            if len(parts) >= 3:
                try:
                    p = int(parts[1]) if parts[1] not in ("x", "a") else 0
                    t = int(parts[2]) if parts[2] not in ("x", "a") else 0
                    pt = p + t
                    self._token_max_pt[oid] = max(self._token_max_pt.get(oid, 0), pt)
                except ValueError:
                    pass
                # Count keywords (parts after subtypes that aren't card types)
                if len(parts) > 3:
                    kws = [p for p in parts[3:] if p and p not in _kw_skip and len(p) > 1]
                    kw_count = len(kws)
                    self._token_max_kw[oid] = max(self._token_max_kw.get(oid, 0), kw_count)

        # Pre-load zone interaction flags per card (for F67, F68)
        # A card "interacts with graveyard" if any of its abilities have
        # trigger_origin or trigger_destination containing "Graveyard"
        self._zone_graveyard = set()   # oids that interact with graveyard
        self._zone_exile = set()       # oids that interact with exile
        for row in conn.execute(
            "SELECT fnm.oracle_id, fa.trigger_origin, fa.trigger_destination "
            "FROM forge_abilities fa "
            "JOIN forge_name_map fnm ON fnm.forge_name = fa.card_name "
            "WHERE fa.trigger_origin IS NOT NULL OR fa.trigger_destination IS NOT NULL"
        ):
            oid, origin, dest = row
            if origin and "Graveyard" in origin:
                self._zone_graveyard.add(oid)
            if dest and "Graveyard" in dest:
                self._zone_graveyard.add(oid)
            if origin and "Exile" in origin:
                self._zone_exile.add(oid)
            if dest and "Exile" in dest:
                self._zone_exile.add(oid)
        # Also mark cards with graveyard/exile-related verbs
        for oid, p in self._forge_profiles.items():
            if p['verbs'] & {"Mill", "Sacrifice", "Destroy", "DestroyAll", "Discard"}:
                self._zone_graveyard.add(oid)
            if "exile" in p.get('effect_zones', set()):
                self._zone_exile.add(oid)
```

- [ ] **Step 2: Add CmdrFeatureContext zone flags**

In `CmdrFeatureContext.__init__` (or `_init_from_index`), after existing setup, add:

```python
        # Zone interaction flags for this commander
        self.cmdr_zone_graveyard = cmdr_oid in ctx._zone_graveyard
        self.cmdr_zone_exile = cmdr_oid in ctx._zone_exile
```

This needs to go into both code paths: the SQL path in `__init__` and the `_init_from_index` path. Add it to the common `_init_cmdr_exact_and_deck_edges` method at the end (or after calling it in both paths), since it only depends on `ctx` lookups, not SQL.

Find the `_init_cmdr_exact_and_deck_edges` method and add at the end:

```python
        # Zone interaction flags
        self.cmdr_zone_graveyard = self.cmdr_oid in ctx._zone_graveyard
        self.cmdr_zone_exile = self.cmdr_oid in ctx._zone_exile
```

- [ ] **Step 3: Add 8 new features to compute_card_features**

In `src/mtg_synergy/recommend/forge_features.py`, after the `token_amt_var` line (line 1198), add:

```python
    # ── New features: ability counts, token complexity, zone interaction ──

    # F63: total_ability_count — ordinal count of all abilities (combo potential)
    total_abilities = float(min(ctx._total_ability_counts.get(card_oid, 0), 15))

    # F64: triggered_ability_count — ordinal triggered abilities (replaces binary F32)
    triggered_count = float(min(ctx._triggered_counts.get(card_oid, 0), 10))

    # F65: token_power_toughness — max P+T of tokens this card creates
    token_pt = float(min(ctx._token_max_pt.get(card_oid, 0), 20))

    # F66: token_keyword_count — max keywords on tokens this card creates
    token_kw = float(min(ctx._token_max_kw.get(card_oid, 0), 5))

    # F67: zone_graveyard_interact — both card and commander interact with graveyard
    zone_gy = 1.0 if (card_oid in ctx._zone_graveyard and cmdr.cmdr_zone_graveyard) else 0.0

    # F68: zone_exile_interact — both card and commander interact with exile
    zone_ex = 1.0 if (card_oid in ctx._zone_exile and cmdr.cmdr_zone_exile) else 0.0

    # F69: ability_density — abilities per mana cost (efficiency signal)
    raw_count = ctx._total_ability_counts.get(card_oid, 0)
    ability_dens = float(raw_count) / max(card_cmc, 1.0) if raw_count > 0 else 0.0
    ability_dens = min(ability_dens, 5.0)  # cap at 5

    # F70: mech_zone_fwd — zone-specific mechanics synergy
    # Uses the new zone concept dimensions from expanded mechanics vectors
    mech_zone_fwd = 0.0
    if cmdr.cmdr_consumes is not None and card_prod is not None:
        # Zone concepts are the last 5 dimensions before subtypes
        from mtg_synergy.recommend.mechanics_vectors import N_CONCEPTS
        zone_start = N_CONCEPTS - 5  # last 5 game concepts are zone-aware
        zone_end = N_CONCEPTS
        zone_card = card_prod[zone_start:zone_end]
        zone_cmdr = cmdr.cmdr_consumes[zone_start:zone_end]
        mech_zone_fwd = float(np.dot(zone_cmdr, zone_card))
```

Then update the return list to append these 8 values after `token_amt_var`:

```python
    return [
        # ... existing 63 features unchanged ...
        token_amt_var,                                   # F62 token_amount_variable
        total_abilities,                                 # F63 total_ability_count
        triggered_count,                                 # F64 triggered_ability_count
        token_pt,                                        # F65 token_power_toughness
        token_kw,                                        # F66 token_keyword_count
        zone_gy,                                         # F67 zone_graveyard_interact
        zone_ex,                                         # F68 zone_exile_interact
        ability_dens,                                    # F69 ability_density
        mech_zone_fwd,                                   # F70 mech_zone_fwd
    ]
```

- [ ] **Step 4: Update FORGE_FEATURE_NAMES in train_fusion_model.py**

In `train_fusion_model.py`, add to `FORGE_FEATURE_NAMES` after `"token_amount_variable"` (line 110):

```python
    "total_ability_count",   # [63] total abilities per card (combo potential)
    "triggered_ability_count", # [64] triggered ability count (ordinal)
    "token_power_toughness", # [65] max P+T of tokens created
    "token_keyword_count",   # [66] max keywords on tokens created
    "zone_graveyard_interact", # [67] both card+cmdr interact with graveyard
    "zone_exile_interact",   # [68] both card+cmdr interact with exile
    "ability_density",       # [69] abilities per mana cost (efficiency)
    "mech_zone_fwd",         # [70] zone-specific mechanics synergy
```

Also update the docstring at the top of `forge_features.py` to say 71 features instead of 63.

- [ ] **Step 5: Write tests for new features**

Add to `tests/test_forge_features.py`:

```python
def test_feature_count_71():
    """compute_card_features should return 71 features."""
    ctx, conn = _make_ctx()
    try:
        from mtg_synergy.recommend.forge_features import (
            compute_card_features, CmdrFeatureContext,
        )
        krenko_oid = KRENKO_OID
        cmdr_ctx = CmdrFeatureContext(ctx, krenko_oid, set())
        # Get any card oid
        card_oid = next(oid for oid in ctx._forge_profiles if oid != krenko_oid)
        card_meta = conn.execute(
            "SELECT type_line, cmc FROM cards WHERE oracle_id = ?", (card_oid,)
        ).fetchone()
        feats = compute_card_features(
            card_oid, card_meta[0] or "", float(card_meta[1] or 0),
            ctx, cmdr_ctx,
        )
        assert len(feats) == 71, f"Expected 71 features, got {len(feats)}"
    finally:
        conn.close()


def test_total_ability_count_positive():
    """Cards with forge abilities should have total_ability_count > 0."""
    ctx, conn = _make_ctx()
    try:
        assert ctx._total_ability_counts.get(KRENKO_OID, 0) > 0, (
            "Krenko should have abilities"
        )
    finally:
        conn.close()


def test_triggered_counts_loaded():
    """Triggered ability counts should be loaded."""
    ctx, conn = _make_ctx()
    try:
        assert hasattr(ctx, "_triggered_counts")
        assert len(ctx._triggered_counts) > 0
    finally:
        conn.close()


def test_token_stats_loaded():
    """Token P/T and keyword counts should be loaded for token-creating cards."""
    ctx, conn = _make_ctx()
    try:
        assert len(ctx._token_max_pt) > 0, "Should have token P/T stats"
        # Krenko creates goblin tokens (1/1) so PT should be 2
        assert ctx._token_max_pt.get(KRENKO_OID, 0) == 2, (
            f"Krenko creates 1/1 goblins, expected PT=2, got {ctx._token_max_pt.get(KRENKO_OID)}"
        )
    finally:
        conn.close()


def test_zone_interaction_sets():
    """Zone interaction sets should be populated."""
    ctx, conn = _make_ctx()
    try:
        assert len(ctx._zone_graveyard) > 100, (
            f"Expected >100 graveyard-interacting cards, got {len(ctx._zone_graveyard)}"
        )
        assert len(ctx._zone_exile) > 10, (
            f"Expected >10 exile-interacting cards, got {len(ctx._zone_exile)}"
        )
    finally:
        conn.close()
```

- [ ] **Step 6: Run tests**

Run: `uv run pytest tests/test_forge_features.py -v`

Expected: All tests pass including new ones. Feature count test confirms 71 features.

- [ ] **Step 7: Commit**

```bash
git add src/mtg_synergy/recommend/forge_features.py train_fusion_model.py tests/test_forge_features.py
git commit -m "feat: add 8 new forge features (ability counts, token complexity, zone interaction)"
```

---

### Task 3: Rebuild feature cache and train

The feature cache format changed (63→71 features). Delete the old cache, rebuild, and validate.

**Files:**
- No code changes — just pipeline execution

- [ ] **Step 1: Delete stale caches**

The feature cache has the wrong shape (63 columns, need 71). The edge index cache also needs rebuilding due to the new trigger_origin/destination data in `_raw_abilities`.

```bash
rm -f data/forge_features_cache.npz
rm -f data/edge_index_cache.npz
```

- [ ] **Step 2: Rebuild features and train**

Run: `uv run python3 train_fusion_model.py --forge-only --rebuild-features 2>&1`

Expected:
- Feature matrix shape: `(372896, 71)` (was 63)
- Mechanics vectors dim: ~112 (was 107)
- NDCG@30 should be reported for each fold
- No errors

Watch for:
- New features having nonzero values (check the per-feature statistics output)
- Training completing without errors

- [ ] **Step 3: Record NDCG result**

Note the Mean NDCG@30. Compare to baseline 0.5257. Any improvement is success.

- [ ] **Step 4: Run EDHREC comparison**

Run: `uv run python3 compare_edhrec.py --forge --quiet 2>&1 | tail -20`

Expected: AVERAGE row should show results. Small changes are normal — we're not optimizing for EDHREC overlap.

- [ ] **Step 5: Run full test suite**

Run: `uv run pytest tests/ -v --timeout=60 2>&1 | tail -20`

Expected: All tests pass.

- [ ] **Step 6: Commit model and update CLAUDE.md**

```bash
git add data/fusion_model_forge.lgb
git commit -m "feat: retrain forge model with 71 features + zone-aware mechanics vectors"
```

Then update CLAUDE.md to reflect:
- 71 features (was 63)
- ~112-dim mechanics vectors (was 107-dim) with 32 game concepts + 80 subtypes
- New NDCG@30 score
- Updated feature list mentioning the 8 new features

```bash
git add CLAUDE.md
git commit -m "docs: update CLAUDE.md — 71 features, 32 game concepts, zone-aware mechanics"
```
