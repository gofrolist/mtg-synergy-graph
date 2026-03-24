# Signal Quality Improvements Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Improve EDHREC alignment from 13.4/30 to 18-22/30 by fixing mechanics engine bugs, adding missing semantic bridges, improving tower model training, and switching LLM scoring to float scale.

**Architecture:** Four layers of fixes applied bottom-up: (1) Fix mechanics engine filter bugs for correct matching, (2) Add missing semantic bridges for tag coverage, (3) Switch LLM scoring to float 1.0-10.0 scale for better granularity, (4) Improve tower model training with focal loss and provider normalization. Each layer is validated independently via `compare_edhrec.py`.

**Tech Stack:** Python 3.12+, SQLite3, NumPy, OpenAI API

**Validation baseline:** Run `python3 compare_edhrec.py --fast --quiet` before starting and record scores for all decks. Every task must re-run this comparison and report before/after.

---

## File Structure

```
Files modified:
  mechanics_matcher.py           — Fix has_keyword, add filter keys, fix two-step chains
  mtg_synergy/constants.py       — Add ~15 missing semantic bridges
  score_synergies.py             — Float scoring prompt (1.0-10.0)
  train_tower_model.py           — Focal loss, provider normalization, new features

Files created:
  tests/test_mechanics_matching.py — New tests for mechanics engine fixes
  tests/test_semantic_bridges.py   — New tests for bridge coverage

Files unchanged:
  mtg_synergy/recommend/engine.py  — Already handles float scores (score_val * 1000.0)
  extract_mechanics.py             — No changes (extraction schema unchanged)
```

---

## Task 1: Fix `has_keyword` filter in mechanics engine

The `has_keyword` filter in `filter_matches()` checks if a producer's output contains the required keyword. But `card_produces_events()` never populates keyword data in the output dict — so keyword-triggered abilities (e.g., "Whenever a creature with flying attacks") never match creatures that have flying. This is a systemic false negative.

**Files:**
- Modify: `mechanics_matcher.py:185-233` (card_produces_events)
- Test: `tests/test_mechanics_matching.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_mechanics_matching.py
from mechanics_matcher import card_produces_events, filter_matches

def test_card_produces_events_includes_keywords():
    """Tokens created with keywords should populate has_keyword in output."""
    mechs = [{
        "type": "triggered",
        "trigger_event": "creature-enters",
        "filter": None,
        "action": "create-token",
        "detail": {"token": {"subtype": "Spirit", "power": "1", "keywords": ["flying"]}},
        "modifier_target": None, "modifier_how": None, "scope": None, "cost": None,
    }]
    events = card_produces_events(mechs)
    token_events = [e for e in events if e["event"] == "creature-enters"]
    assert len(token_events) > 0
    # The output should carry keywords from the token
    assert "has_keyword" in token_events[0]["output"]
    assert "flying" in token_events[0]["output"]["has_keyword"]

def test_filter_matches_keyword_with_populated_output():
    """filter_matches should pass when producer output has matching keyword."""
    trigger_filter = {"has_keyword": "flying"}
    producer_output = {"controller": "you", "has_keyword": ["flying"]}
    assert filter_matches(trigger_filter, producer_output) is True

def test_filter_matches_keyword_mismatch():
    """filter_matches should fail when producer output has wrong keyword."""
    trigger_filter = {"has_keyword": "flying"}
    producer_output = {"controller": "you", "has_keyword": ["haste"]}
    assert filter_matches(trigger_filter, producer_output) is False

def test_filter_matches_keyword_missing():
    """filter_matches should fail when producer output has no keywords."""
    trigger_filter = {"has_keyword": "flying"}
    producer_output = {"controller": "you"}
    assert filter_matches(trigger_filter, producer_output) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_mechanics_matching.py -v`
Expected: `test_card_produces_events_includes_keywords` FAILS (no has_keyword in output)

- [ ] **Step 3: Fix `card_produces_events()` to populate keywords from token detail**

In `mechanics_matcher.py`, inside `card_produces_events()`, after the token subtype/power extraction (lines 208-217), add keyword extraction:

```python
# After line 217 (output["is_token"] = True):
            if isinstance(token, dict) and token.get("keywords"):
                kw = token["keywords"]
                output["has_keyword"] = kw if isinstance(kw, list) else [kw]
```

Also, for non-token events from `grant-keyword` actions, populate the keyword:

```python
        # After line 217, before the for event loop:
        if action == "grant-keyword" and isinstance(detail, dict):
            kw = detail.get("keyword") or detail.get("keywords")
            if kw:
                output["has_keyword"] = [kw] if isinstance(kw, str) else kw
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_mechanics_matching.py -v`
Expected: All 4 PASS

- [ ] **Step 5: Run full suite + EDHREC comparison**

```bash
python3 -m pytest tests/ -v
python3 compare_edhrec.py --fast --quiet
```
Record scores.

- [ ] **Step 6: Commit**

```bash
git add mechanics_matcher.py tests/test_mechanics_matching.py
git commit -m "fix: populate has_keyword in card_produces_events output

Keyword-triggered abilities now match creatures with those keywords.
Previously all keyword filter checks were false negatives.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Implement missing filter keys in mechanics engine

11 filter keys are silently ignored in `filter_matches()`. Implement the 5 most common: `is_equipped`, `counter_type`, `power`, `tapped`, `condition`.

**Files:**
- Modify: `mechanics_matcher.py:96-110` (filter_matches)
- Test: `tests/test_mechanics_matching.py` (append)

- [ ] **Step 1: Write failing tests**

```python
# Append to tests/test_mechanics_matching.py

def test_filter_is_equipped_passes_when_set():
    trigger_filter = {"is_equipped": True}
    producer_output = {"controller": "you", "is_equipped": True}
    assert filter_matches(trigger_filter, producer_output) is True

def test_filter_is_equipped_fails_when_not_set():
    trigger_filter = {"is_equipped": True}
    producer_output = {"controller": "you"}
    assert filter_matches(trigger_filter, producer_output) is False

def test_filter_counter_type_matches():
    trigger_filter = {"counter_type": "+1/+1"}
    producer_output = {"controller": "you", "counter_type": "+1/+1"}
    assert filter_matches(trigger_filter, producer_output) is True

def test_filter_counter_type_mismatches():
    trigger_filter = {"counter_type": "+1/+1"}
    producer_output = {"controller": "you", "counter_type": "charge"}
    assert filter_matches(trigger_filter, producer_output) is False

def test_filter_power_threshold():
    trigger_filter = {"power": ">=3"}
    producer_output = {"controller": "you", "power": 4}
    assert filter_matches(trigger_filter, producer_output) is True

def test_filter_power_below_threshold():
    trigger_filter = {"power": ">=3"}
    producer_output = {"controller": "you", "power": 2}
    assert filter_matches(trigger_filter, producer_output) is False
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_mechanics_matching.py::test_filter_is_equipped_fails_when_not_set -v`
Expected: PASS (currently silently accepts — the filter is ignored so returns True!)

Actually, the current code ignores unknown keys and returns True. So `test_filter_is_equipped_fails_when_not_set` will PASS incorrectly (bug). The test documents the desired behavior.

- [ ] **Step 3: Implement filter keys**

In `mechanics_matcher.py`, replace the comment at line 108 with:

```python
        elif key == "is_equipped":
            if required_val and not producer_output.get("is_equipped"):
                return False
        elif key == "counter_type":
            prod_ct = str(producer_output.get("counter_type", "")).lower()
            if str(required_val).lower() != prod_ct:
                return False
        elif key == "power":
            # Handle threshold strings like ">=3" or numeric values
            prod_power = producer_output.get("power")
            if prod_power is None:
                return False
            try:
                if isinstance(required_val, str) and required_val.startswith(">="):
                    if int(prod_power) < int(required_val[2:]):
                        return False
                elif isinstance(required_val, str) and required_val.startswith("<="):
                    if int(prod_power) > int(required_val[2:]):
                        return False
                else:
                    if int(prod_power) != int(required_val):
                        return False
            except (ValueError, TypeError):
                return False  # Non-numeric power (e.g., *) — can't verify
        elif key == "tapped":
            if required_val and not producer_output.get("tapped"):
                return False
        elif key == "condition":
            pass  # Complex conditions still ignored (would need game-state simulation)
        # Ignore remaining unknown filter keys (amount, name, source, etc.)
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_mechanics_matching.py -v`
Expected: All PASS

- [ ] **Step 5: Run full suite**

```bash
python3 -m pytest tests/ -v
python3 compare_edhrec.py --fast --quiet
```

- [ ] **Step 6: Commit**

```bash
git add mechanics_matcher.py tests/test_mechanics_matching.py
git commit -m "feat: implement is_equipped, counter_type, power, tapped filter keys

Reduces false positives in mechanics matching. Complex conditions
still ignored (would need game-state simulation).

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Fix two-step chain filter verification

Two-step chain detection (`compute_deck_synergies`) awards bonus when candidate → deck_card → commander, but doesn't verify that the deck card's trigger filter is compatible with the candidate's event output.

**Files:**
- Modify: `mechanics_matcher.py:585-637` (compute_deck_synergies, two-step section)
- Test: `tests/test_mechanics_matching.py` (append)

- [ ] **Step 1: Write failing test**

```python
# Append to tests/test_mechanics_matching.py

def test_two_step_chain_respects_filter():
    """Two-step chain should NOT match when deck card filter rejects candidate output."""
    from mechanics_matcher import compute_deck_synergies

    # Commander responds to creature-enters
    cmdr_oid = "cmdr"
    all_mechanics = {
        "cmdr": [{"type": "triggered", "trigger_event": "creature-enters",
                  "filter": None, "action": "draw-card", "detail": None,
                  "modifier_target": None, "modifier_how": None, "scope": None, "cost": None}],
        # Deck card: responds to creature-enters with subtype=Goblin, produces creature-enters
        "deck1": [{"type": "triggered", "trigger_event": "creature-enters",
                   "filter": {"subtype": "Goblin"}, "action": "create-token",
                   "detail": {"token": {"subtype": "Goblin", "power": "1"}},
                   "modifier_target": None, "modifier_how": None, "scope": None, "cost": None}],
        # Candidate: creates Elf token (NOT Goblin) — should NOT chain through deck1
        "cand1": [{"type": "activated", "trigger_event": None,
                   "filter": None, "action": "create-token",
                   "detail": {"token": {"subtype": "Elf", "power": "1"}},
                   "modifier_target": None, "modifier_how": None, "scope": None, "cost": None}],
    }
    card_types = {"cmdr": "", "deck1": "Creature", "cand1": "Creature"}
    scores = compute_deck_synergies(cmdr_oid, ["cand1"], all_mechanics, card_types,
                                     deck_oids={"deck1"})
    # cand1 creates Elf tokens, but deck1 wants Goblins — no chain
    assert scores.get("cand1", 0) == 0 or "cand1" not in scores
```

- [ ] **Step 2: Run to verify current behavior**

Run: `python3 -m pytest tests/test_mechanics_matching.py::test_two_step_chain_respects_filter -v`

If it passes, the fix already works (the bridge precomputation may already filter). If it fails, proceed.

- [ ] **Step 3: Fix bridge precomputation to include filter**

The current code at line 604 already stores `{"oid": deck_oid, "filter": deck_resp["filter"]}` in `deck_bridges` and checks `filter_matches(bridge["filter"], cev["output"])` at line 630. Verify this is working correctly by reading the actual code. If the test already passes, mark as done.

If the test fails, the issue is that `filter_matches` is being called but the candidate's output doesn't populate the subtype from the token. This was fixed in Task 1 — verify the test passes after Task 1's changes.

- [ ] **Step 4: Run full suite**

```bash
python3 -m pytest tests/ -v
```

- [ ] **Step 5: Commit (if changes were needed)**

```bash
git add mechanics_matcher.py tests/test_mechanics_matching.py
git commit -m "test: add two-step chain filter verification test

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: Add missing semantic bridges

Add ~15 missing bridges that cause high-synergy EDHREC cards to be missed. Focus on tribal-combat, damage-token, and sacrifice-graveyard connections.

**Files:**
- Modify: `mtg_synergy/constants.py` (SEMANTIC_BRIDGES dict)
- Test: `tests/test_semantic_bridges.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_semantic_bridges.py
from mtg_synergy.constants import SEMANTIC_BRIDGES

def test_damage_token_bridge():
    """damage-dealing should connect to token-events for Impact Tremors pattern."""
    assert ("damage-dealing", "token-events") in SEMANTIC_BRIDGES

def test_combat_enabler_tribal_bridges():
    """combat-enabler should connect to tribal strategies."""
    assert ("combat-enabler", "attack-events") in SEMANTIC_BRIDGES

def test_sacrifice_graveyard_bridge():
    """sacrifice-outlet should connect to graveyard-filling."""
    assert ("sacrifice-outlet", "graveyard-filling") in SEMANTIC_BRIDGES

def test_mana_tribal_bridge():
    """mana-acceleration should connect to tap-combo for commanders with tap abilities."""
    assert ("mana-acceleration", "tap-combo") in SEMANTIC_BRIDGES or \
           ("untap", "tap-combo") in SEMANTIC_BRIDGES

def test_token_sacrifice_bridge():
    """token-generation should connect to sacrifice-events."""
    assert ("token-generation", "sacrifice-events") in SEMANTIC_BRIDGES

def test_bridge_weights_in_range():
    """All bridge weights must be between 0 and 1."""
    for (p, w), weight in SEMANTIC_BRIDGES.items():
        assert 0 < weight <= 1.0, f"Bridge ({p}, {w}) has invalid weight {weight}"
```

- [ ] **Step 2: Run to verify failures**

Run: `python3 -m pytest tests/test_semantic_bridges.py -v`
Expected: Several FAIL (missing bridges)

- [ ] **Step 3: Add bridges to `mtg_synergy/constants.py`**

Add these entries to the `SEMANTIC_BRIDGES` dict (find appropriate location by category):

```python
    # Damage dealing ↔ token events (Impact Tremors + Krenko pattern)
    ("damage-dealing", "token-events"): 0.7,
    ("direct-damage", "token-events"): 0.6,
    ("group-damage", "token-events"): 0.6,

    # Token generation → sacrifice enablement
    # NOTE: ("token-generation", "sacrifice-events") already exists at 0.8 — skip
    ("token-generation", "sacrifice-outlet"): 0.6,

    # Sacrifice → graveyard filling
    ("sacrifice-outlet", "graveyard-filling"): 0.7,
    ("sacrifice-outlet", "creature-death"): 0.9,

    # Mana/untap → tap-combo (for commanders with tap abilities)
    ("untap", "tap-combo"): 0.9,
    ("mana-acceleration", "tap-combo"): 0.5,

    # Combat enabler → board-wide effects
    ("combat-enabler", "wide-board"): 0.6,
    ("haste-grant", "wide-board"): 0.5,

    # Board pump → attack events
    # NOTE: ("creature-pump", "attack-events") already exists at 0.5 — skip
    ("board-wide-pump", "attack-events"): 0.7,

    # Graveyard payoff connections
    # NOTE: ("graveyard-filling", "graveyard-recursion") already exists at 0.7 — skip
    ("mill", "graveyard-filling"): 0.8,
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_semantic_bridges.py -v`
Expected: All PASS

- [ ] **Step 5: Run full suite + EDHREC comparison**

```bash
python3 -m pytest tests/ -v
python3 compare_edhrec.py --fast --quiet
```
Record before/after and confirm improvement.

- [ ] **Step 6: Commit**

```bash
git add mtg_synergy/constants.py tests/test_semantic_bridges.py
git commit -m "feat: add 15 missing semantic bridges for tribal-combat and sacrifice patterns

Adds damage→token-events, sacrifice→graveyard, untap→tap-combo,
token→sacrifice-events bridges. Targets Krenko, Edgar, Syr Konrad gaps.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: Switch LLM scoring to float scale (1.0-10.0)

Integer 1-10 scores create 92+ ties per score bin. Float scoring reduces ties from ~92 to ~5 per bin, making the tower model tiebreaker much more effective.

**Files:**
- Modify: `score_synergies.py:140-174` (build_system_prompt)
- Modify: `score_synergies.py:302-346` (parse_response — accept floats)
- Test: manual validation (re-score 1 commander, compare distribution)

- [ ] **Step 1: Update system prompt for float scores**

In `score_synergies.py`, modify `build_system_prompt()` line 142:

```python
# Change:
"Your task: rate how synergistic each card is with the given commander on a scale of 1-10."

# To:
"Your task: rate how synergistic each card is with the given commander on a scale of 1.0-10.0. Use decimal precision (e.g., 7.5, 3.2) to differentiate between similar cards."
```

Update scoring guide (lines 152-157):
```python
# Change:
"- 10: Essential combo piece or perfect synergy."
# To:
"- 9.5-10.0: Essential combo piece or perfect synergy."
"- 8.0-9.4: Strong synergy. Directly enables or benefits."
"- 6.0-7.9: Good synergy. Fits the deck's strategy."
"- 4.0-5.9: Moderate. Generically useful but not specifically synergistic."
"- 2.0-3.9: Weak. Technically legal but doesn't advance."
"- 1.0-1.9: No synergy or anti-synergy."
```

Add emphasis:
```python
# After line 170, add:
"Use the FULL range of decimal scores. Avoid rounding to integers. Two cards that are both 'good synergy' might be 6.2 vs 7.8 — capture that difference."
```

- [ ] **Step 2: Update `parse_response()` to handle float scores**

In `score_synergies.py`, line 341:

```python
# Change:
if isinstance(score, (int, float)) and 1 <= score <= 10:
    valid.append({"name": name, "score": int(round(score)), "reason": reason})

# To:
if isinstance(score, (int, float)) and 1 <= score <= 10:
    valid.append({"name": name, "score": round(float(score), 1), "reason": reason})
```

- [ ] **Step 3: Update the INSERT to store float scores**

Check if the `synergy_scores` table schema stores INTEGER or REAL. Read `score_synergies.py` to find the CREATE TABLE. If INTEGER, change to REAL.

```sql
-- In score_synergies.py, find the CREATE TABLE and ensure score is REAL:
score REAL NOT NULL
```

- [ ] **Step 4: Test with a single commander**

```bash
# Score 1 commander with the new float prompt (small batch for testing)
python3 score_synergies.py --commander "Krenko, Mob Boss" --dry-run
# Verify the prompt looks correct, then:
python3 score_synergies.py --commander "Krenko, Mob Boss"
```

Check the output for decimal scores (should see 6.5, 7.2, etc. instead of 6, 7).

- [ ] **Step 5: Verify the recommendation pipeline handles floats**

```bash
python3 synergy_graph.py --deck krenko --recommend 2>&1 | head -30
```
Expected: Recommendations work (engine.py already does `score_val * 1000.0` which handles floats).

- [ ] **Step 6: Run EDHREC comparison**

```bash
python3 compare_edhrec.py --deck krenko --fast
```

- [ ] **Step 7: Commit**

```bash
git add score_synergies.py
git commit -m "feat: switch LLM scoring to float 1.0-10.0 scale

Reduces tiebreaker ties from ~92 to ~5 per score bin.
parse_response now stores float scores (round to 1 decimal).
Existing integer scores remain compatible.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: Add focal loss and provider normalization to tower model

Class imbalance (53% at scores 3-5, only 8% at 8-10) makes the model optimize for the middle. Focal loss upweights high-synergy pairs. Provider normalization fixes gemma3's 1.9-point scoring bias.

**Files:**
- Modify: `train_tower_model.py:175-337` (train function)
- Test: retrain model + compare correlation metrics

- [ ] **Step 1: Add provider normalization before training**

In `train_tower_model.py`, after loading pairs (line 187), add normalization:

```python
    # Normalize scores by provider to fix gemma3 scoring bias (+1.9 vs gpt-5.4-mini)
    provider_means = {}
    for row in conn.execute(
        "SELECT model, AVG(score) FROM synergy_scores "
        "WHERE model NOT LIKE 'auto%' AND model NOT LIKE 'spellbook%' AND model NOT LIKE 'manual%' "
        "GROUP BY model"
    ).fetchall():
        provider_means[row[0]] = row[1]

    # Get per-pair provider info
    pair_providers = {}
    for row in conn.execute(
        "SELECT commander_oid, card_oid, model FROM synergy_scores "
        "WHERE model NOT LIKE 'auto%' AND model NOT LIKE 'spellbook%' AND model NOT LIKE 'manual%'"
    ).fetchall():
        pair_providers[(row[0], row[1])] = row[2]

    # Target mean: gpt-5.4-mini (largest provider)
    target_mean = provider_means.get("gpt-5.4-mini", 4.5)
    normalized_pairs = []
    for cmdr_oid, card_oid, score in pairs:
        provider = pair_providers.get((cmdr_oid, card_oid), "")
        provider_mean = provider_means.get(provider, target_mean)
        adjusted = score - (provider_mean - target_mean)
        adjusted = max(1.0, min(10.0, adjusted))
        normalized_pairs.append((cmdr_oid, card_oid, adjusted))
    pairs = normalized_pairs
    conn.close()

    print(f"Provider normalization: target_mean={target_mean:.2f}, "
          f"adjusted {len([m for m in provider_means if provider_means[m] != target_mean])} providers")
```

- [ ] **Step 2: Add focal-style loss weighting**

In `train_tower_model.py`, inside the training loop, replace the uniform MSE loss (lines 257-260):

```python
            # Focal-style weighting: upweight high-synergy pairs (8-10)
            # and very low pairs (1-2) since they're rare but important
            sample_weights = np.ones(len(batch), dtype=np.float32)
            batch_labels = y[batch]
            sample_weights[batch_labels >= 8] = 3.0   # High synergy (8% of data)
            sample_weights[batch_labels >= 9] = 5.0   # Very high synergy (rare)
            sample_weights[batch_labels <= 2] = 2.0   # Clear non-synergy

            error = pred_clipped - batch_labels
            loss = np.mean(sample_weights * error ** 2)
            epoch_loss += loss
            n_batches += 1

            # Backward — apply sample weights to gradient
            N = len(batch)
            grad_out = 2 * sample_weights * error / N
```

- [ ] **Step 3: Add new structural features**

In `train_tower_model.py`, expand `compute_struct_features()` from 10 to 12 features:

```python
def compute_struct_features(cmdr_oid, card_oid, provides, wants, strats, types, oracles, ranks, mech_data):
    f = np.zeros(12, dtype=np.float32)  # Changed from 10 to 12
    # ... existing features f[0]-f[9] unchanged ...

    # NEW f[10]: Commander EDHREC rank (commander quality/popularity)
    f[10] = np.log10(max(ranks.get(cmdr_oid, 50000), 1))

    # NEW f[11]: Is card in Spellbook combo with commander
    if "_combos" in mech_data:
        cmdr_combos = mech_data["_combos"].get(cmdr_oid, set())
        f[11] = 1.0 if card_oid in cmdr_combos else 0.0

    return f
```

Update model dimensions: change MLP input from 138 to 140 (128 interaction + 12 structural):

```python
# In init_model(), line 144:
"W1": np.random.randn(140, 64)  # Changed from 138 to 140
```

**CRITICAL:** The old `tower_model.npz` (W1 shape 138×64) will crash with the new code (expects 140×64). Add a version check in `mtg_synergy/recommend/engine.py` where the model is loaded (~line 379-384):

```python
# After loading tower model, verify dimensions match:
if tm["W1"].shape[0] != 140:
    print("  Warning: Tower model has old dimensions, skipping model scoring. Retrain with: python3 train_tower_model.py")
    raise ValueError("Tower model dimension mismatch")
```

The model MUST be retrained after this change (Step 4 does this).

Update `load_structural_features()` to also load Spellbook combos:

```python
    # At end of load_structural_features(), add:
    combo_partners = {}  # cmdr_oid -> set of card_oids in combos
    try:
        for row in conn.execute(
            "SELECT sc.card_oracle_ids FROM spellbook_combos sc"
        ).fetchall():
            oids = json.loads(row[0])
            for oid in oids:
                partners = set(oids) - {oid}
                combo_partners.setdefault(oid, set()).update(partners)
    except Exception:
        pass
    mech_data["_combos"] = combo_partners
```

- [ ] **Step 4: Retrain the model**

```bash
python3 train_tower_model.py
```
Expected: Training completes in ~45-60s. Check:
- Correlation should be >= 0.75 (ideally 0.78+)
- High-synergy separation should improve
- Print output shows provider normalization applied

- [ ] **Step 5: Run EDHREC comparison**

```bash
python3 compare_edhrec.py --fast --quiet
```
Compare before/after scores.

- [ ] **Step 6: Commit**

```bash
git add train_tower_model.py
git commit -m "feat: focal loss + provider normalization + 2 new structural features

- Focal loss upweights rare high-synergy (8-10) and clear non-synergy (1-2)
- Provider normalization adjusts gemma3 scores (-1.9 bias) to gpt-5.4-mini baseline
- New features: commander EDHREC rank, Spellbook combo membership
- MLP input expanded from 138 to 140 dimensions

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: Final validation — full pipeline comparison

Run the complete validation suite to measure cumulative improvement.

**Files:**
- No files modified — validation only

- [ ] **Step 1: Run full test suite**

```bash
python3 -m pytest tests/ -v
```
Expected: All tests pass (71+ existing + new tests from Tasks 1-4)

- [ ] **Step 2: Run EDHREC comparison for all decks**

```bash
python3 compare_edhrec.py --fast --quiet
```
Record final scores per deck.

- [ ] **Step 3: Run individual deck smoke tests**

```bash
python3 synergy_graph.py --deck krenko --recommend 2>&1 | tail -10
python3 synergy_graph.py --deck sram --recommend 2>&1 | tail -10
python3 synergy_graph.py --deck syr_konrad --recommend 2>&1 | tail -10
python3 synergy_graph.py --deck edgar --recommend 2>&1 | tail -10
```
Verify recommendations look reasonable.

- [ ] **Step 4: Document results in CLAUDE.md**

Update the "Current Performance" table with new scores. Update the average.

- [ ] **Step 5: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: update performance metrics after signal quality improvements

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Summary

| Task | Component | Expected Impact | Risk |
|------|-----------|----------------|------|
| 1 | Fix `has_keyword` filter | +1-2 EDHREC points | Low |
| 2 | Implement missing filter keys | +0.5-1 points | Low |
| 3 | Fix two-step chain filters | +0.5-1 points | Low |
| 4 | Add 15 semantic bridges | +2-3 points | Low |
| 5 | Float LLM scoring | +1-2 points (after re-score) | Medium (needs API calls) |
| 6 | Focal loss + normalization | +0.5-1 points | Medium (retrain needed) |
| 7 | Final validation | 0 (measurement only) | None |

**Total estimated: 13.4/30 → 18-22/30 EDHREC alignment**

**Note on Task 5:** Float scoring requires re-scoring commanders. For validation, re-score just Krenko (cheapest test). Full re-score of all 33 commanders costs ~$25 with Batch API. The improvement won't show in EDHREC comparison until commanders are re-scored.

**Dependency chain:** Tasks 1-4 are independent. Task 5 is independent. Task 6 depends on Tasks 1-4 (mechanics fixes improve structural features) and ideally Task 5 (float scores give tower model better targets). Task 7 runs after all others.
