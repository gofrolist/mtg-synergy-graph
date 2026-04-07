# Forge DSL Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract three currently-invisible Forge DSL field categories — `ReplaceWith$` substitute-effect verbs, combat trigger filters (`ValidAttacker$` / `ValidBlocker$`), and 7 missing cost-type detections — and feed them into the existing GBM feature pipeline. Retrain the Forge LambdaRank model and validate that NDCG@30 holds or improves against the **post-WIP baseline** measured in Task B (the prior 0.569 EDHREC_FREE figure is no longer the relevant comparison because uncommitted WIP changes the feature set and negative sampling).

**Important:** This branch starts from a dirty working tree. The session began with substantial uncommitted WIP that overlaps with Phase 1: R: replacement-event verb extraction, 6 new GBM features in F25-F31 slots, auto-derived deck tags, |EXEC| SVar resolution, reverse cost-feeds, and a 2:1 → 4:1 negative-sampling change with a new popular-card hard-negative tier, plus an unrelated `strategy_detector.py` cleanup. Per user direction this WIP is committed and evaluated FIRST (Tasks A and B), then Phase 1 layers strictly additive extensions on top.

**Architecture:** All extraction lives in `forge_features.py` (`ForgeFeatureContext._enrich_profile_from_row` / sibling helpers) following the existing raw_line regex pattern. **No schema migration**, **no new GBM feature columns**, **no new hand-coded penalty rules**. The new data flows into existing aggregate features:
- `ReplaceWith$` verbs → `ForgeProfile.verbs` set → automatically picked up by `mech_cosine` (F3), `verb_alignment`, `mech_density` (F96), and the auto-derived mechanics_vectors PRODUCES space.
- `ValidAttacker$` / `ValidBlocker$` → `raw_trigger_filters` set → automatically picked up by `trigger_specificity` (F95).
- Expanded `cost_types` → existing `cost_feeds_cmdr` (F94) feature; matches expand transparently.

This honors the project rules:
- "Don't add game rules one-by-one — need general mechanical understanding" (`feedback_no_individual_rules`)
- "Think general, not per-archetype" (`feedback_general_not_specific`)
- "Use forge data directly, don't add indirection layers" (`feedback_forge_data_direct`)
- "Don't duplicate code between training and inference" (`feedback_code_duplication`)
- "Back up feature cache before experiments" (`feedback_cache_management`)
- "Run training ONCE with --tee" (`feedback_training_workflow`, `feedback_tee_training_output`)

**Tech Stack:** Python 3.11+, sqlite3, lightgbm 4.x, numpy, pytest. Existing packages: `mtg-synergy` (inference), `mtg-synergy-train` (training). Test command: `uv run pytest tests/ -v`.

**Verified corpus coverage (Grep over `data/forge/forge-gui/res/cardsfolder/`):**
- `ReplaceWith$`: 1,476 cards (4.7% of 31k)
- `ValidAttacker$` / `ValidBlocker$`: 669 cards (2.1%)
- `SubCounter` cost: 668 cards (2.1%)
- All three combined: ~2,500 unique cards (~8% of corpus) — meaningful net-new signal

**Pre-existing state (verified by reading source, do NOT re-implement):**
- `Affected$` is already extracted (`affected_scope_ratio`, F89, line 470 of `forge_features.py`).
- `EffectZone$` / `ActiveZones$` / `AffectedZone$` already extracted (line 668-675).
- `Event$` already parsed for `opponent_only_events` and **forward-mapped through `_R_EVENT_TO_VERB`** (introduced by the WIP committed in Task A). The 7 mapped events are CreateToken, AddCounter, Mill, DamageDone, Draw, GainLife, LoseLife. Phase 1 Task 1 extends this with `ReplaceWith$ DB$/SP$` substitute-effect verbs — strictly additive, does not duplicate the WIP work.

**Pre-existing WIP (committed in Task A — do NOT re-implement in Tasks 1-3):**
- `_R_EVENT_TO_VERB` map for non-opponent R: lines (forge_features.py)
- `_verb_counts` per-oid Counter for verb concentration (forge_features.py)
- Auto-derived deck tags from forge profiles: `verb→has`, `trigger→hints`, `trigger_filter→Type$ hints`, `token_subtype→Type$ has` (forge_features.py)
- `|EXEC|` SVar resolution appended to raw_line (forge_import.py)
- 6 new GBM features filling F25-F31: `shared_verb_count`, `shared_trigger_count`, `cmdr_verb_concentration`, `mech_fwd_synergy`, `mech_rev_synergy`, `co_producer_score` (forge_compute.py)
- Reverse cost-feeds: cmdr triggers on Sacrificed/Discarded/LifeLost/Taps add to `cmdr_feeds` (forge_compute.py)
- `cmdr_verb_freq` per-commander verb concentration distribution (forge_compute.py)
- 4-tier negative sampling (subtype + tag + popular hard-neg + random) at ratio 4:1 (train_fusion_model.py)
- Auto-backup `.prev` sidecar of previous model before overwrite (train_fusion_model.py)

---

## File Structure

| File | Role | Change kind |
|---|---|---|
| `packages/mtg-synergy/src/mtg_synergy/recommend/forge_features.py` | Profile builder, raw_line regex extraction | EDIT (3 surgical sites) |
| `tests/test_forge_features_phase1.py` | New test module — pure unit tests on `_enrich_profile_from_row` and helpers | CREATE |
| `tests/test_forge_phase1_integration.py` | Integration test — load real cards, assert profiles contain expected fields | CREATE |
| `docs/FEATURE_REFERENCE.md` | Update notes for the 3 affected aggregate features | EDIT (one paragraph each) |
| `CLAUDE.md` | Update extraction list under "Forge profiles extract ALL raw_line fields" | EDIT |

**Files explicitly NOT touched:**
- `packages/mtg-synergy-train/.../parse/forge_import.py` — no schema migration
- `packages/mtg-synergy/src/mtg_synergy/recommend/forge_compute.py` — no new feature columns
- `packages/mtg-synergy/src/mtg_synergy/recommend/mechanics_vectors.py` — change is to data feeding it, not its logic
- `data/tags.db` schema — no migration

---

## Task A: Commit existing WIP in two logical chunks

**Files:** see git status at session start. WIP modified:
- `packages/mtg-synergy-train/src/mtg_synergy_train/parse/forge_import.py`
- `packages/mtg-synergy/src/mtg_synergy/recommend/forge_features.py`
- `packages/mtg-synergy/src/mtg_synergy/recommend/forge_compute.py`
- `scripts/train_fusion_model.py`
- `scripts/strategy_detector.py`
- `tests/test_strategy_detector.py`
- `uv.lock`

Untracked: `scripts/diagnose_features.py` (per user direction: leave untracked, do NOT `git add` it).

**Goal of this task:** create a clean two-commit history that the rest of Phase 1 can build on. No code changes — pure git plumbing.

- [ ] **Step 1: Snapshot the current trained model and data BEFORE any code change**

```bash
BACKUP_DIR="$HOME/mtg-synergy-backups/2026-04-06-pre-phase1"
mkdir -p "$BACKUP_DIR"
cp data/fusion_model_forge.lgb "$BACKUP_DIR/fusion_model_forge.lgb"
cp data/fusion_model_forge.lgb.meta.json "$BACKUP_DIR/fusion_model_forge.lgb.meta.json" 2>/dev/null || true
cp data/feature_cache.npz "$BACKUP_DIR/feature_cache.npz" 2>/dev/null || echo "no feature_cache.npz to back up"
cp data/tags.db "$BACKUP_DIR/tags.db"
ls -lh "$BACKUP_DIR"
echo "PRE_WIP_BASELINE_NDCG: 0.569 (from CLAUDE.md, EDHREC_FREE)" > "$BACKUP_DIR/BASELINE.txt"
```

Expected: 4 files in backup dir, total ~700 MB - 1.5 GB. Note: we are NOT measuring the pre-WIP baseline by retraining — the WIP also touches feature extraction, so a fresh measurement would still come out tainted. We trust the 0.569 figure recorded in CLAUDE.md as the historical baseline.

- [ ] **Step 2: Create the working branch from main with WIP intact**

```bash
git checkout -b feat/forge-dsl-phase1
git status --short
```

Expected: branch created; working tree still shows the same 7 modified + 1 untracked file.

- [ ] **Step 3: Commit 1 — strategy_detector cleanup (separate prep commit per user direction)**

```bash
git add scripts/strategy_detector.py tests/test_strategy_detector.py uv.lock
git status --short
```

Expected: 3 files staged; 4 files (forge_import, forge_features, forge_compute, train_fusion_model) still unstaged; diagnose_features.py still untracked.

```bash
git commit -m "refactor: simplify strategy_detector

Removes 358 lines of unused detection logic from
scripts/strategy_detector.py. Strategy detection is no longer used by
the forge model (replaced by mech_cosine + summarize_commander in
mechanics_vectors.py per the cd278d3 architecture change).

Tests updated to match the slimmer surface."
```

- [ ] **Step 4: Verify the strategy_detector tests still pass after the cleanup commit**

```bash
uv run pytest tests/test_strategy_detector.py -v 2>&1 | tail -20
```

Expected: all tests in `test_strategy_detector.py` pass. If any fail, STOP — the cleanup is incomplete; revert with `git reset HEAD~1` and surface to user.

- [ ] **Step 5: Commit 2 — Forge intelligence WIP (single bundle for evaluation)**

```bash
git add packages/mtg-synergy-train/src/mtg_synergy_train/parse/forge_import.py \
        packages/mtg-synergy/src/mtg_synergy/recommend/forge_features.py \
        packages/mtg-synergy/src/mtg_synergy/recommend/forge_compute.py \
        scripts/train_fusion_model.py
git status --short
```

Expected: 4 files staged; only `scripts/diagnose_features.py` remains as untracked (and stays untracked).

```bash
git commit -m "feat(forge): R-event verbs, 6 new GBM features, hard-negative sampling

Bundles three related improvements that must be evaluated together:

1. Data extraction (forge_import.py + forge_features.py):
   - Append \$EXEC\$ SVar content to raw_line so downstream parsers see
     the effect's zone/type from the Execute\$ chain
   - Extract effective verbs from non-opponent R: lines via
     _R_EVENT_TO_VERB map (CreateToken→Token, AddCounter→PutCounter,
     Mill, DamageDone, Draw, GainLife, LoseLife)
   - Track _verb_counts per oid for verb concentration features
   - Auto-derive deck tags from forge profiles: verb→has, trigger→hints,
     trigger_filter→Type\$ hints, token_subtype→Type\$ has. Eliminates
     dependence on Forge's hand-curated DeckHas/Hints for cards missing
     them.

2. New GBM features filling F25-F31 slots (forge_compute.py):
   - shared_verb_count: |cmdr_verbs ∩ card_verbs|
   - shared_trigger_count: |cmdr_triggers ∩ card_triggers|
   - cmdr_verb_concentration: max(cmdr_verb_freq[v] for v in card_verbs)
   - mech_fwd_synergy: cmdr_produces · card_consumes
   - mech_rev_synergy: card_produces · cmdr_consumes
   - co_producer_score: cmdr_produces · card_produces (parallel
     mechanics, e.g., Atraxa + Evolution Sage both proliferate)
   - Reverse cost-feeds: cmdr triggers on Sacrificed/Discarded/LifeLost/
     Taps adds those costs to cmdr_feeds (Korvold + sacrifice cards)
   - cmdr_verb_freq: per-commander verb frequency distribution

3. Negative sampling (train_fusion_model.py):
   - Ratio bumped 2:1 → 4:1
   - 4-tier sampling: 1/4 subtype + 1/4 tag + 1/4 popular hard-negatives
     (cards in top-25% by EDHREC commander count, but not on this cmdr's
     list) + 1/4 random
   - Auto-backup .prev sidecar of previous model before overwrite for
     one-step rollback

Per project rules these are committed together so Task B can measure
their joint NDCG impact in a single training run."
```

- [ ] **Step 6: Verify clean state**

```bash
git status --short
git log --oneline -5
```

Expected: working tree shows only `?? scripts/diagnose_features.py`. Recent commits show the 2 new commits (cleanup + feat) on top of `cd278d3`.

---

## Task B: Measure post-WIP baseline (single training run)

**Files:** none (training pipeline only)

This is the **single most important measurement** in the plan. The 0.569 EDHREC_FREE figure from CLAUDE.md is no longer comparable because the WIP changed the feature set. We need a fresh number that becomes the Phase 1 baseline.

- [ ] **Step 1: Run unit tests against the committed WIP**

```bash
uv run pytest tests/ -v --tb=short 2>&1 | tail -40
```

Expected: 148+ existing tests pass. If any fail, STOP and surface — the WIP is broken and Phase 1 cannot proceed on top.

- [ ] **Step 2: Rebuild features and train with EDHREC_FREE in one tee'd shot**

```bash
mkdir -p logs
EDHREC_FREE=1 uv run python3 scripts/train_fusion_model.py \
    --rebuild-features --validate \
    2>&1 | tee logs/2026-04-06-task-b-postwip-baseline.log
```

Expected runtime: ~8 minutes. Expected output (last lines): `NDCG@30 = 0.5XX ± ~0.005` and `Validation: PASSED` or similar.

- [ ] **Step 3: Record the new baseline and decide whether to continue**

```bash
grep -E "NDCG@30|Validation" logs/2026-04-06-task-b-postwip-baseline.log | tail -10
```

Decision matrix vs the historical 0.569:

| Result | Action |
|---|---|
| New NDCG ≥ 0.569 | GREEN. WIP is a real improvement. Record number, proceed to Task 1. |
| New NDCG in [0.564, 0.569) | YELLOW. Within ±0.005 variance. Proceed but flag in commit messages and final report. |
| New NDCG in [0.555, 0.564) | ORANGE. Real but small regression. STOP, surface to user with the log; do NOT proceed without explicit go-ahead. The WIP may have a bug or the negative-sampling change may need rollback. |
| New NDCG < 0.555 | RED. Substantial regression. STOP. Surface log to user. Likely candidates: 4:1 + popular hard negatives is over-penalizing legitimate staples, OR one of the 6 new features is noise. |

Record the chosen baseline:

```bash
echo "POST_WIP_BASELINE_NDCG: <number>" >> "$BACKUP_DIR/BASELINE.txt"
cat "$BACKUP_DIR/BASELINE.txt"
```

- [ ] **Step 4: Commit the post-WIP trained model artifacts**

```bash
git add data/fusion_model_forge.lgb data/fusion_model_forge.lgb.meta.json data/model_registry.jsonl
git status data/
git commit -m "chore(model): retrain Forge GBM with WIP bundle

Post-WIP NDCG@30 (EDHREC_FREE): <new>
Pre-WIP NDCG@30 (historical, CLAUDE.md): 0.569
Delta: <+/-N>

Bundle includes 6 new F25-F31 features, R-event verb extraction,
auto-derived deck tags, and 4:1 popular-hard-negative sampling.
Becomes the baseline for Phase 1 Tasks 1-3."
```

---

## Task 0: Pre-Phase-1 final snapshot

**Files:** none (filesystem operations only)

After Task B has produced the post-WIP baseline, snapshot it before Phase 1 mutates anything.

- [ ] **Step 1: Snapshot the post-WIP feature cache and model**

```bash
POST_WIP_BACKUP="$HOME/mtg-synergy-backups/2026-04-06-postwip"
mkdir -p "$POST_WIP_BACKUP"
cp data/feature_cache.npz "$POST_WIP_BACKUP/feature_cache.npz"
cp data/fusion_model_forge.lgb "$POST_WIP_BACKUP/fusion_model_forge.lgb"
cp data/fusion_model_forge.lgb.meta.json "$POST_WIP_BACKUP/fusion_model_forge.lgb.meta.json"
cp "$BACKUP_DIR/BASELINE.txt" "$POST_WIP_BACKUP/BASELINE.txt"
ls -lh "$POST_WIP_BACKUP"
```

Expected: 4 files copied. This is the rollback target if any of Tasks 1-3 break the model.

---

## Task 1: ABANDONED — `ReplaceWith$` substitute-effect verbs

**Status:** Abandoned 2026-04-06 after code-quality review of commit `3a012f4`.

**Reason:** The plan's assumed DSL format `ReplaceWith$ DB$ <Verb>` (with embedded `$` separator) does not exist in the Forge corpus. Real format is a single token that is either (a) an SVar reference like `DBTap`, `DBPutP1P1`, `ZealousDmg` requiring resolution against the card's SVar table, or (b) a built-in Forge engine replacement name like `Exile`, `ETBTapped`, `LandTapped`. The implemented regex matched zero rows in production. Commit `3a012f4` was reset.

**Future work (deferred to a Phase 1.5):** Extract these verbs properly by extending `packages/mtg-synergy-train/src/mtg_synergy_train/parse/forge_import.py` to resolve `ReplaceWith$ <svar>` into the raw_line via the same mechanism as `|EXEC|` (Execute$ SVar resolution). This touches both packages, requires re-importing Forge data (`scripts/import_forge.py --import`), and is properly grouped with other "extend forge_import.py structured extraction" improvements from the original DSL audit (static ability Mode$ semantics, structured R: Event$/ReplaceWith$ columns, `IsPresent$`/`CheckSVar$` conditional handling). Phase 1 proceeds with Tasks 2 and 3 only.

---

### Original Task 1 description (retained for reference)

**Task 1: Extract `ReplaceWith$ DB$/SP$` substitute-effect verbs into `ForgeProfile.verbs`**

**Background:** After Task A, R: lines contribute via the WIP's `_R_EVENT_TO_VERB` map of the Event$ being replaced (CreateToken, AddCounter, Mill, DamageDone, Draw, GainLife, LoseLife → 7 mapped events). This catches the broad event class but **misses the substitute effect's verb** — the thing the replacement effect actually produces in place of the original event. For example:
- Doubling Season — `R:Event$ CreateToken | ReplaceWith$ DBTokenDouble` — the WIP catches `Token` from Event$, but DBTokenDouble's actual verb (Token doubling, often a `PutCounter` or another `Token` for paired counter doublers) is invisible.
- Strionic Resonator — `R:Event$ Trigger | ReplaceWith$ DBCopyTrigger` — Event$ `Trigger` is NOT in `_R_EVENT_TO_VERB` so today it produces zero signal; this task surfaces the `CopyTrigger` substitute verb.
- Verified corpus coverage: 1,476 cards (4.7%) use `ReplaceWith$`. The WIP's `_R_EVENT_TO_VERB` covers 7 of the ~30+ distinct Event$ values; ReplaceWith$ DB$/SP$ catches the substitute side regardless of whether Event$ is mapped, and frequently adds a second verb when it is.

**Approach:** Parse `ReplaceWith$ DB$ <Verb>` (and `ReplaceWith$ SP$ <Verb>`) from `raw_line` for any R: row. Add the resolved verb to `p['verbs']`. Increment `_verb_counts` so the WIP's `cmdr_verb_concentration` and `verb_freq` features see it. This is strictly ADDITIVE on top of the WIP — does not change any existing branch. Both the Event$ verb (from WIP) and the ReplaceWith$ verb (from this task) coexist in `verbs`, and the auto-derived `Ability$<verb>` deck tags pick both up.

**Files:**
- Modify: `packages/mtg-synergy/src/mtg_synergy/recommend/forge_features.py:537-552`
- Create: `tests/test_forge_features_phase1.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_forge_features_phase1.py`:

```python
"""Phase 1 Forge DSL extraction tests — ReplaceWith$, ValidAttacker/Blocker, cost_types."""
from __future__ import annotations

import pytest

from mtg_synergy.recommend.forge_features import ForgeFeatureContext


def _empty_profile() -> dict:
    """Return a fresh profile dict matching ForgeFeatureContext._enrich_profile_from_row default."""
    return {
        'verbs': set(), 'triggers': set(), 'keywords': set(),
        'counter_types': set(), 'targets': set(), 'ability_types': set(),
        'trigger_filters': set(), 'required_subtypes': set(),
        'granted_keywords': set(), 'conditions': set(),
        'duration': set(), 'combat_damage': False,
        'effect_zones': set(),
        'damage_amount': None, 'cards_drawn': None, 'life_amount': None,
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
    }


class TestReplaceWithExtraction:
    """ReplaceWith$ DB$/SP$ verbs must enter ForgeProfile.verbs."""

    def test_doubling_season_token_replacement(self):
        # Doubling Season — when a token would be created, instead create twice as many
        raw = ("R:Event$ CreateToken | ActiveZones$ Battlefield | "
               "ValidPlayer$ You | ReplaceWith$ DBTokenDouble | "
               "Description$ ...")
        # The SVar resolution path means raw_line for an R: row produced by
        # forge_import will already have DBTokenDouble resolved into the
        # raw_line via |EXEC|, OR we parse ReplaceWith$ directly here.
        # We test the direct ReplaceWith$ DB$ form first:
        raw_direct = ("R:Event$ CreateToken | ValidPlayer$ You | "
                      "ReplaceWith$ DB$ Token | TokenAmount$ 2")
        verbs = ForgeFeatureContext._parse_replacewith_verbs(raw_direct)
        assert "Token" in verbs

    def test_replacewith_sp_form(self):
        raw = "R:Event$ DamageDone | ReplaceWith$ SP$ DealDamage | NumDmg$ 2"
        verbs = ForgeFeatureContext._parse_replacewith_verbs(raw)
        assert "DealDamage" in verbs

    def test_no_replacewith_returns_empty(self):
        raw = "R:Event$ Mill | ValidPlayer$ Player.Opponent"
        verbs = ForgeFeatureContext._parse_replacewith_verbs(raw)
        assert verbs == set()

    def test_ignores_non_r_lines(self):
        raw = "T:Mode$ ChangesZone | ReplaceWith$ DB$ Token"  # malformed but defensive
        # Helper is called only on R: rows in the caller; helper itself is permissive.
        verbs = ForgeFeatureContext._parse_replacewith_verbs(raw)
        # Helper does not check prefix — it just parses. The CALLER is responsible.
        assert "Token" in verbs
```

- [ ] **Step 2: Run test to confirm it fails**

```bash
uv run pytest tests/test_forge_features_phase1.py::TestReplaceWithExtraction -v
```

Expected: `AttributeError: type object 'ForgeFeatureContext' has no attribute '_parse_replacewith_verbs'` for all 4 tests.

- [ ] **Step 3: Add the helper to `forge_features.py`**

Open `packages/mtg-synergy/src/mtg_synergy/recommend/forge_features.py`. Find the existing `_RE_EVENT` definition (search for `_RE_EVENT = `) and add a sibling regex below it:

```python
# Phase 1: parse ReplaceWith$ DB$ <Verb> or ReplaceWith$ SP$ <Verb>
_RE_REPLACEWITH = re.compile(r'ReplaceWith\$\s*(?:DB|SP)\$\s*(\w+)')
```

Then add a static helper method on `ForgeFeatureContext` (place it next to other small static helpers, e.g., right above `_extract_subtypes_from_raw_line`):

```python
@staticmethod
def _parse_replacewith_verbs(raw_line: str) -> set[str]:
    """Extract verbs from ReplaceWith$ DB$/SP$ <Verb> in an R: line.

    Replacement effects substitute one event with another. The substitute
    effect's verb is the actual mechanical contribution of the card and
    must enter the profile so mech_cosine, verb_alignment, and mech_density
    can see it. Returns empty set if no ReplaceWith$ DB$/SP$ pattern found.
    """
    if not raw_line or "ReplaceWith$" not in raw_line:
        return set()
    return {m.group(1) for m in _RE_REPLACEWITH.finditer(raw_line)}
```

- [ ] **Step 4: Run test to confirm it passes**

```bash
uv run pytest tests/test_forge_features_phase1.py::TestReplaceWithExtraction -v
```

Expected: 4 passed.

- [ ] **Step 5: Wire helper into the profile-build loop**

In `forge_features.py`, find the R: branch (`if ability_type == 'R' and raw_line_val:`). After the existing `_R_EVENT_TO_VERB` block (which the WIP added in Task A), append a new sibling block at the same indentation level so it runs unconditionally for any R: line, not gated by `is_opp_only`:

```python
            # Phase 1: ReplaceWith$ DB$/SP$ substitute-effect verbs.
            # Runs for both opp-only and non-opp R: lines because the substitute
            # effect verb is always 'self produces' regardless of whose event was
            # being replaced. E.g., Bruvac's Mill replacement still has a "self
            # mills more" substitute effect that we want in 'verbs'.
            for rw_verb in ForgeFeatureContext._parse_replacewith_verbs(raw_line_val):
                p['verbs'].add(rw_verb)
                vc = self._verb_counts.setdefault(oid, {})
                vc[rw_verb] = vc.get(rw_verb, 0) + 1
```

Note: this block sits at the **outer** indentation of the `if ability_type == 'R'` branch (a sibling of the inner `if m:` block), so it executes whether or not Event$ matched the regex. That is intentional — ReplaceWith$ can appear without an Event$ regex match, and we want the verb regardless of opp_only status.

- [ ] **Step 6: Add an integration test that exercises the full path**

Append to `tests/test_forge_features_phase1.py`:

```python
class TestReplaceWithIntegration:
    """End-to-end: build a ForgeFeatureContext from a real card and check verbs."""

    @pytest.fixture
    def synergy_conn(self):
        import os
        import sqlite3
        path = os.environ.get("MTG_SYNERGY_DB_PATH", "data/tags.db")
        if not os.path.exists(path):
            pytest.skip(f"DB not found at {path}")
        return sqlite3.connect(path)

    def test_doubling_season_has_token_verb(self, synergy_conn):
        ctx = ForgeFeatureContext(synergy_conn, preload_edges=False)
        # Doubling Season is a stable card. Find its oracle_id.
        row = synergy_conn.execute(
            "SELECT oracle_id FROM cards WHERE name = 'Doubling Season'"
        ).fetchone()
        if row is None:
            pytest.skip("Doubling Season not in DB")
        oid = row[0]
        prof = ctx._forge_profiles.get(oid)
        assert prof is not None, "Doubling Season has no Forge profile"
        # ReplaceWith$ contributes Token and PutCounter or similar verbs
        assert prof['verbs'], f"verbs empty for Doubling Season: {prof}"
```

- [ ] **Step 7: Run integration test**

```bash
uv run pytest tests/test_forge_features_phase1.py::TestReplaceWithIntegration -v
```

Expected: passes (or skipped if DB missing).

- [ ] **Step 8: Run full unit-test suite to catch regressions**

```bash
uv run pytest tests/ -v --tb=short 2>&1 | tail -40
```

Expected: 148+ tests pass (existing 148 + 5 new).

- [ ] **Step 9: Commit**

```bash
git add packages/mtg-synergy/src/mtg_synergy/recommend/forge_features.py \
        tests/test_forge_features_phase1.py
git commit -m "feat(forge): extract ReplaceWith\$ verbs into ForgeProfile.verbs

ReplaceWith\$ DB\$/SP\$ <Verb> in R: lines names the substitute effect's
verb (e.g., Doubling Season → Token, Strionic Resonator → Trigger).
These verbs were previously invisible to mech_cosine, verb_alignment,
and mech_density. Adds them through the existing 'verbs' set so all
aggregate features pick them up automatically.

Coverage: 1,476 cards (4.7%) with ReplaceWith\$ in raw_line."
```

---

## Task 2: Add `ValidAttacker$` / `ValidBlocker$` to `raw_trigger_filters`

**Background:** Combat triggers (Attacks, Blocks, AttackersDeclared) use `ValidAttacker$` / `ValidBlocker$` instead of `ValidCard$` to filter the trigger source. The current `_enrich_profile_from_row` only reads `trigger_filter` (which comes from `ValidCard$`). For 669 cards (2.1%) with combat-restricted triggers, the filter is invisible to `trigger_specificity` (F95) and `raw_trigger_filters` IDF weighting.

**Approach:** Inside the existing `if trig_filter:` block (line 553-559), also pull `ValidAttacker$` / `ValidBlocker$` out of `raw_line_val` and feed both into `raw_trigger_filters`. This is symmetric to how `ValidCard$` is treated today.

**Files:**
- Modify: `packages/mtg-synergy/src/mtg_synergy/recommend/forge_features.py:553-559`
- Modify: `tests/test_forge_features_phase1.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_forge_features_phase1.py`:

```python
class TestCombatTriggerFilters:
    """ValidAttacker$/ValidBlocker$ from T: lines must enter raw_trigger_filters."""

    def test_extracts_valid_attacker(self):
        raw = ("T:Mode$ Attacks | ValidAttacker$ Creature.YouCtrl+Goblin | "
               "Execute$ TrigPump | Description$ ...")
        result = ForgeFeatureContext._parse_combat_trigger_filters(raw)
        assert "Creature.YouCtrl+Goblin" in result

    def test_extracts_valid_blocker(self):
        raw = "T:Mode$ Blocks | ValidBlocker$ Creature.YouCtrl"
        result = ForgeFeatureContext._parse_combat_trigger_filters(raw)
        assert "Creature.YouCtrl" in result

    def test_extracts_both_when_present(self):
        raw = ("T:Mode$ AttackerBlockedByCreature | "
               "ValidAttacker$ Creature.Self | ValidBlocker$ Creature.OppCtrl")
        result = ForgeFeatureContext._parse_combat_trigger_filters(raw)
        assert "Creature.Self" in result
        assert "Creature.OppCtrl" in result

    def test_returns_empty_for_non_combat(self):
        raw = "T:Mode$ ChangesZone | ValidCard$ Creature.YouCtrl | Origin$ Any | Destination$ Battlefield"
        result = ForgeFeatureContext._parse_combat_trigger_filters(raw)
        assert result == set()
```

- [ ] **Step 2: Run test to confirm it fails**

```bash
uv run pytest tests/test_forge_features_phase1.py::TestCombatTriggerFilters -v
```

Expected: 4 fails with `AttributeError: ... _parse_combat_trigger_filters`.

- [ ] **Step 3: Add the helper and the regex**

Near `_RE_REPLACEWITH` (added in Task 1) add:

```python
_RE_VALID_ATTACKER = re.compile(r'ValidAttacker\$\s*([^|]+?)(?:\s*\||$)')
_RE_VALID_BLOCKER  = re.compile(r'ValidBlocker\$\s*([^|]+?)(?:\s*\||$)')
```

Add the static helper near `_parse_replacewith_verbs`:

```python
@staticmethod
def _parse_combat_trigger_filters(raw_line: str) -> set[str]:
    """Extract ValidAttacker$ / ValidBlocker$ filters from a T: combat trigger line.

    Combat triggers (Attacks, Blocks, AttackerBlockedByCreature, etc.) use
    these filters in place of ValidCard$. Without this extraction, ~669 cards
    contribute zero signal to trigger_specificity (F95).
    """
    if not raw_line:
        return set()
    out: set[str] = set()
    for rx in (_RE_VALID_ATTACKER, _RE_VALID_BLOCKER):
        for m in rx.finditer(raw_line):
            val = m.group(1).strip()
            if val and val != "Card.Self":
                out.add(val)
    return out
```

- [ ] **Step 4: Run helper tests to confirm they pass**

```bash
uv run pytest tests/test_forge_features_phase1.py::TestCombatTriggerFilters -v
```

Expected: 4 passed.

- [ ] **Step 5: Wire helper into the profile-build loop**

Find the existing block (around line 553):

```python
        if trig_filter:
            if trig_filter != "Card.Self":
                p['raw_trigger_filters'].add(trig_filter)
            for part in trig_filter.split(","):
                main = part.split(".")[0].strip()
                if main and main != "Card" and main[0].isupper():
                    p['trigger_filters'].add(main.lower())
```

Immediately after this block (still inside the row-processing loop, only for `T:` rows so guard with `ability_type == 'T'`), add:

```python
        # Phase 1: combat trigger filters (ValidAttacker$ / ValidBlocker$)
        if ability_type == 'T' and raw_line_val:
            for combat_filter in ForgeFeatureContext._parse_combat_trigger_filters(raw_line_val):
                p['raw_trigger_filters'].add(combat_filter)
                # Also feed coarse type into trigger_filters set
                main = combat_filter.split(",")[0].split(".")[0].strip()
                if main and main != "Card" and main[0].isupper():
                    p['trigger_filters'].add(main.lower())
```

- [ ] **Step 6: Run unit + integration tests**

```bash
uv run pytest tests/ -v --tb=short 2>&1 | tail -40
```

Expected: 152+ tests pass.

- [ ] **Step 7: Commit**

```bash
git add packages/mtg-synergy/src/mtg_synergy/recommend/forge_features.py \
        tests/test_forge_features_phase1.py
git commit -m "feat(forge): extract ValidAttacker\$/ValidBlocker\$ into raw_trigger_filters

Combat triggers (Attacks/Blocks/AttackerBlockedByCreature) use
ValidAttacker\$ and ValidBlocker\$ filters in place of ValidCard\$.
Previously these 669 cards (2.1%) contributed zero signal to
trigger_specificity (F95). Adds them through the existing
raw_trigger_filters set; trigger_specificity feature picks them up
automatically via IDF weighting."
```

---

## Task 3: Expand `cost_types` with 7 missing cost categories

**Background:** Today `cost_types` recognizes only 5 categories (sacrifice, tap, discard, exile, paylife). Cost-effect synergy (cost_feeds_cmdr, F94) cannot see these 7 important cost shapes:
- `SubCounter<...>` — counter removal as cost (Hangarback Walker, Walking Ballista, +1/+1 commanders that pay counters)
- `AddCounter<...>` — counter placement as cost (rare but exists, e.g., Persist creatures)
- `ExileFromGrave<...>` — graveyard exile as cost (delve, escape, Tormod's Crypt activation)
- `Return<...>` — bounce as cost (Ninjutsu, return-to-hand activation)
- `Reveal<...>` — reveal as cost (cycling-style reveals)
- `Mill<N>` — self-mill as cost (rare; relevant for self-mill commanders)
- `tapXType<...>` (typed tap) — distinct from bare `Tap` (convoke, improvise)

**Approach:** Expand the cost-detection block in `forge_features.py` (lines 561-571). New tokens go into the same `cost_types` set so `cost_feeds_cmdr` (F94) picks them up without modification. Use case-sensitive substring matches mirroring the existing style.

**Files:**
- Modify: `packages/mtg-synergy/src/mtg_synergy/recommend/forge_features.py:561-571`
- Modify: `tests/test_forge_features_phase1.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_forge_features_phase1.py`:

```python
class TestCostTypesExpansion:
    """Phase 1: cost_types must recognize 7 additional cost categories."""

    @pytest.mark.parametrize("cost_str,expected", [
        ("SubCounter<1/P1P1>", "subcounter"),
        ("AddCounter<1/P1P1>", "addcounter"),
        ("ExileFromGrave<2/Card>", "exilegrave"),
        ("Return<1/CARDNAME>", "return"),
        ("Reveal<1/Card>", "reveal"),
        ("Mill<3>", "mill"),
        ("tapXType<1/Cleric>", "taptype"),
    ])
    def test_cost_type_detected(self, cost_str, expected):
        types = ForgeFeatureContext._parse_cost_types(cost_str)
        assert expected in types, f"{cost_str} → {types}"

    def test_existing_types_still_detected(self):
        # Regression: the original 5 must still work.
        for cost_str, expected in [
            ("Sac<1/CARDNAME>", "sacrifice"),
            ("T", "tap"),
            ("Discard<1/Card>", "discard"),
            ("Exile<1/CARDNAME>", "exile"),
            ("PayLife<2>", "paylife"),
        ]:
            types = ForgeFeatureContext._parse_cost_types(cost_str)
            assert expected in types, f"REGRESSION: {cost_str} → {types}"

    def test_combined_cost(self):
        # Real card pattern: "T Sac<1/CARDNAME> SubCounter<1/P1P1>"
        types = ForgeFeatureContext._parse_cost_types("T Sac<1/CARDNAME> SubCounter<1/P1P1>")
        assert {"tap", "sacrifice", "subcounter"} <= types
```

- [ ] **Step 2: Run test to confirm it fails**

```bash
uv run pytest tests/test_forge_features_phase1.py::TestCostTypesExpansion -v
```

Expected: all parameterized cases fail with `AttributeError: ... _parse_cost_types`.

- [ ] **Step 3: Extract cost-type detection into a static helper**

In `forge_features.py`, replace the inline block at lines 561-571:

```python
        # Extract cost types for cost-effect alignment
        cost_str = cost or ""
        if "Sac" in cost_str:
            p['cost_types'].add('sacrifice')
        if "T" in cost_str.split() or cost_str == "T":
            p['cost_types'].add('tap')
        if "Discard" in cost_str:
            p['cost_types'].add('discard')
        if "Exile" in cost_str:
            p['cost_types'].add('exile')
        if "PayLife" in cost_str:
            p['cost_types'].add('paylife')
```

…with a call to the new helper:

```python
        # Extract cost types for cost-effect alignment
        p['cost_types'] |= ForgeFeatureContext._parse_cost_types(cost or "")
```

Add the helper near the other static helpers:

```python
@staticmethod
def _parse_cost_types(cost_str: str) -> set[str]:
    """Tokenize a Forge cost string into mechanical cost categories.

    Returns a set of category labels consumed by cost_feeds_cmdr (F94).
    Categories are intentionally generic (no card-specific rules).
    """
    if not cost_str:
        return set()
    out: set[str] = set()
    # Existing 5 categories (preserve exact prior semantics)
    if "Sac" in cost_str:
        out.add('sacrifice')
    if "T" in cost_str.split() or cost_str == "T":
        out.add('tap')
    if "Discard" in cost_str:
        out.add('discard')
    # Order matters: ExileFromGrave is more specific than Exile
    if "ExileFromGrave" in cost_str:
        out.add('exilegrave')
    if "Exile" in cost_str:
        out.add('exile')
    if "PayLife" in cost_str:
        out.add('paylife')
    # Phase 1: 7 new categories
    if "SubCounter" in cost_str:
        out.add('subcounter')
    if "AddCounter" in cost_str:
        out.add('addcounter')
    if "Return" in cost_str:
        out.add('return')
    if "Reveal" in cost_str:
        out.add('reveal')
    if "Mill<" in cost_str:  # avoid matching MillFor and substrings
        out.add('mill')
    if "tapXType" in cost_str:
        out.add('taptype')
    return out
```

- [ ] **Step 4: Run helper tests to confirm they pass**

```bash
uv run pytest tests/test_forge_features_phase1.py::TestCostTypesExpansion -v
```

Expected: 13 passed (7 parametrized new + 5 regression + 1 combined).

- [ ] **Step 5: Run full unit-test suite**

```bash
uv run pytest tests/ -v --tb=short 2>&1 | tail -40
```

Expected: 165+ tests pass. **Pay attention to any test in `test_recommendation_quality.py` that asserts on cost_types size or cost_feeds_cmdr distribution** — if a test was hardcoded to 5 categories, update it to ≥5.

- [ ] **Step 6: Commit**

```bash
git add packages/mtg-synergy/src/mtg_synergy/recommend/forge_features.py \
        tests/test_forge_features_phase1.py
git commit -m "feat(forge): expand cost_types with 7 new categories

Adds detection for SubCounter, AddCounter, ExileFromGrave, Return,
Reveal, Mill<N>, and tapXType to cost_types set. Coverage: 668+ cards
with SubCounter alone (counter-removal commanders previously could not
see these as cost-feeds candidates). All new categories flow into the
existing cost_feeds_cmdr (F94) feature with no schema or feature change.

Refactors the inline cost detection into a static helper
_parse_cost_types() for testability."
```

---

## Task 4: Sanity check — confirm features changed for known cards

**Files:** none (read-only verification)

- [ ] **Step 1: Build a fresh ForgeFeatureContext and dump diagnostic counts**

Create a one-off script `scripts/diagnose_phase1.py`:

```python
"""One-off Phase 1 diagnostic. Prints how many profiles changed vs baseline expectations."""
from __future__ import annotations

import sqlite3

from mtg_synergy.recommend.forge_features import ForgeFeatureContext


def main() -> None:
    conn = sqlite3.connect("data/tags.db")
    ctx = ForgeFeatureContext(conn, preload_edges=False)

    # 1) ReplaceWith$ verbs reaching profiles
    rw_cards = 0
    for oid, prof in ctx._forge_profiles.items():
        # We can't directly tell which verbs came from ReplaceWith$ now that
        # they're merged into 'verbs'. Instead, count cards whose raw_lines
        # contain ReplaceWith$ AND have non-empty verbs.
        pass  # see below

    # Easier diagnostic: query raw forge_abilities for ReplaceWith$ presence
    rw_card_names = {
        row[0] for row in conn.execute(
            "SELECT DISTINCT card_name FROM forge_abilities "
            "WHERE raw_line LIKE '%ReplaceWith$ DB$%' "
            "   OR raw_line LIKE '%ReplaceWith$ SP$%'"
        )
    }
    print(f"Cards with ReplaceWith$ DB$/SP$ in DB: {len(rw_card_names)}")

    # 2) ValidAttacker$ / ValidBlocker$ presence
    vb_card_names = {
        row[0] for row in conn.execute(
            "SELECT DISTINCT card_name FROM forge_abilities "
            "WHERE raw_line LIKE '%ValidAttacker$%' OR raw_line LIKE '%ValidBlocker$%'"
        )
    }
    print(f"Cards with ValidAttacker$/ValidBlocker$ in DB: {len(vb_card_names)}")

    # 3) Cost-types expansion: how many profiles now have new categories
    new_cats = {'subcounter', 'addcounter', 'exilegrave', 'return', 'reveal', 'mill', 'taptype'}
    cnt_per_cat = {c: 0 for c in new_cats}
    for prof in ctx._forge_profiles.values():
        for cat in (prof.get('cost_types') or set()) & new_cats:
            cnt_per_cat[cat] += 1
    print("Cards per new cost category:")
    for cat, n in sorted(cnt_per_cat.items()):
        print(f"  {cat:12s}: {n}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run the diagnostic**

```bash
uv run python3 scripts/diagnose_phase1.py
```

Expected output (approximate, based on Grep counts):
```
Cards with ReplaceWith$ DB$/SP$ in DB: 1000-1500
Cards with ValidAttacker$/ValidBlocker$ in DB: 600-700
Cards per new cost category:
  addcounter  : 30-100
  exilegrave  : 200-500
  mill        : 5-30
  reveal      : 50-200
  return      : 500-1500
  subcounter  : 600-700
  taptype     : 50-200
```

If all values are 0 → extraction is broken; STOP and investigate before retraining.
If any number is wildly off (e.g., subcounter < 100 or > 5000) → investigate the regex/substring rule.

- [ ] **Step 3: Delete the diagnostic script**

```bash
rm scripts/diagnose_phase1.py
```

It is intentionally not committed — one-off scratch only.

---

## Task 5: Rebuild feature cache, retrain, validate

**Files:** none (training pipeline)

This is the single expensive run. Per project rules: ONE invocation, with `tee` to a log file, no rerun unless something is broken.

- [ ] **Step 1: Rebuild features and train with EDHREC_FREE in one shot**

```bash
mkdir -p logs
EDHREC_FREE=1 uv run python3 scripts/train_fusion_model.py \
    --rebuild-features --validate \
    2>&1 | tee logs/2026-04-06-phase1-train.log
```

Expected runtime: ~8 minutes (per CLAUDE.md, --rebuild-features is ~7 min + validate ~1 min).
Expected output (last lines): `NDCG@30 = 0.56X ± 0.005` and `Validation: PASSED` (or similar).

- [ ] **Step 2: Compare against the post-WIP baseline (NOT the historical 0.569)**

```bash
echo "BASELINES:"
cat $HOME/mtg-synergy-backups/2026-04-06-postwip/BASELINE.txt
grep -E "NDCG@30|Validation" logs/2026-04-06-phase1-train.log | tail -10
```

Decision matrix vs **`POST_WIP_BASELINE_NDCG`** recorded in Task B:

| Result | Action |
|---|---|
| New NDCG ≥ post-WIP baseline − 0.003 | PROCEED to Task 6 |
| New NDCG in [post-WIP baseline − 0.005, post-WIP baseline − 0.003) | Acceptable (within variance), proceed but flag in commit message |
| New NDCG < post-WIP baseline − 0.005 | ROLLBACK to post-WIP state: `cp $HOME/mtg-synergy-backups/2026-04-06-postwip/feature_cache.npz data/ && cp $HOME/mtg-synergy-backups/2026-04-06-postwip/fusion_model_forge.lgb data/ && cp $HOME/mtg-synergy-backups/2026-04-06-postwip/fusion_model_forge.lgb.meta.json data/`. Then bisect: revert Tasks 1, 2, 3 individually (`git revert <sha>`), rerun training after each, find the offender. |
| Validation FAILED on pipeline tests | STOP. Read the failure, fix root cause (likely a hardcoded count in `test_recommendation_quality.py` that needs to know about the new cost categories or the auto-derived deck tags), recommit, rerun this single step. |

- [ ] **Step 3: Run comparison against EDHREC for a sanity-check commander**

```bash
EDHREC_FREE=1 uv run python3 scripts/compare_edhrec.py --commander "Krenko, Mob Boss" --top 30
```

Expected: top-30 overlap with EDHREC's High Synergy Cards section is ≥ baseline (record both numbers in your local notes).

- [ ] **Step 4: If all green, commit retrained model**

```bash
git add data/fusion_model_forge.lgb data/fusion_model_forge.lgb.meta.json
# (registry append is automatic via model_meta.py — verify)
git status data/model_registry.jsonl
git add data/model_registry.jsonl
git commit -m "chore(model): retrain Forge GBM with Phase 1 DSL extractions

EDHREC_FREE NDCG@30: <baseline> → <new> (Δ +<delta>)
Coverage gains: +1,476 ReplaceWith\$ cards, +669 combat-filter cards,
+668 SubCounter-cost cards. No new feature columns; gains flow through
mech_cosine, trigger_specificity, and cost_feeds_cmdr."
```

---

## Task 6: Update documentation

**Files:**
- Modify: `docs/FEATURE_REFERENCE.md`
- Modify: `CLAUDE.md`

- [ ] **Step 1: Update `docs/FEATURE_REFERENCE.md`**

For each of `mech_cosine` (F3), `trigger_specificity` (F95), and `cost_feeds_cmdr` (F94), append a "Phase 1 inputs" note explaining the new sources:

For `mech_cosine`:
```markdown
**Phase 1 (2026-04-06):** ReplaceWith$ DB$/SP$ verbs from R: lines now flow into
ForgeProfile.verbs and the auto-derived produces vector. Coverage: +1,476 cards.
```

For `trigger_specificity`:
```markdown
**Phase 1 (2026-04-06):** ValidAttacker$ and ValidBlocker$ from combat triggers
(Attacks/Blocks/AttackerBlockedByCreature) now feed raw_trigger_filters. Coverage:
+669 cards.
```

For `cost_feeds_cmdr`:
```markdown
**Phase 1 (2026-04-06):** cost_types vocabulary expanded from 5 to 12 categories:
+subcounter, +addcounter, +exilegrave, +return, +reveal, +mill, +taptype.
Coverage: +668 cards via SubCounter alone.
```

- [ ] **Step 2: Update CLAUDE.md "Forge profiles extract ALL raw_line fields" section**

Find the bullet list (search for `granted_keywords, conditions,` near line containing "Forge profiles extract") and append:

```
  +ReplaceWith$ DB$/SP$ verbs (substitute effect verbs from R: lines)
  +ValidAttacker$ / ValidBlocker$ (combat trigger filters)
  +expanded cost_types (subcounter, addcounter, exilegrave, return, reveal, mill, taptype)
```

Also update the "cost_types (sacrifice/tap/discard/exile/paylife)" parenthetical to read "cost_types (12 categories: sacrifice/tap/discard/exile/paylife/subcounter/addcounter/exilegrave/return/reveal/mill/taptype)".

- [ ] **Step 3: Commit documentation**

```bash
git add docs/FEATURE_REFERENCE.md CLAUDE.md
git commit -m "docs: note Phase 1 Forge DSL extractions in FEATURE_REFERENCE and CLAUDE.md"
```

- [ ] **Step 4: Push branch and open PR (if user has approved)**

The user must explicitly approve a push. **Do NOT push without confirmation.** When approved:

```bash
git push -u origin feat/forge-dsl-phase1
gh pr create --title "feat(forge): Phase 1 DSL extractions (ReplaceWith\$, combat filters, cost types)" \
    --body "$(cat <<'EOF'
## Summary
- Extracts `ReplaceWith$ DB$/SP$` verbs from R: lines into ForgeProfile.verbs (+1,476 cards)
- Extracts `ValidAttacker$` / `ValidBlocker$` from combat triggers into raw_trigger_filters (+669 cards)
- Expands cost_types vocabulary from 5 → 12 categories (+668 cards via SubCounter alone)

No new feature columns. No new penalty rules. New data flows into existing
aggregate features (mech_cosine F3, trigger_specificity F95, cost_feeds_cmdr F94)
to honor the "general not specific" project rule.

## Test plan
- [ ] Unit tests for each helper (`tests/test_forge_features_phase1.py`)
- [ ] Integration test loads Doubling Season profile and asserts non-empty verbs
- [ ] Full pytest suite green (165+ tests)
- [ ] Diagnostic script confirms expected per-category card counts
- [ ] Retrained model NDCG@30 within ±0.005 of baseline (0.569)
- [ ] `compare_edhrec.py --commander "Krenko, Mob Boss"` overlap ≥ baseline
EOF
)"
```

---

## Self-review checklist (run before declaring plan complete)

**Spec coverage:**
- [x] ReplaceWith$ extraction → Task 1
- [x] ValidAttacker$/ValidBlocker$ extraction → Task 2
- [x] Expanded cost_types → Task 3
- [x] Affected$ → SKIPPED (already done — verified at line 470)
- [x] EffectZone$ → SKIPPED (already done — verified at line 668-675)
- [x] Backup before training → Task 0
- [x] Retrain + validate → Task 5
- [x] Documentation update → Task 6

**Placeholder scan:** No "TBD", no "implement later", no "similar to Task N". Each step has actual code or commands.

**Type / name consistency:**
- `_parse_replacewith_verbs` (Task 1) ↔ used in Task 1 step 5 wiring ✓
- `_parse_combat_trigger_filters` (Task 2) ↔ used in Task 2 step 5 wiring ✓
- `_parse_cost_types` (Task 3) ↔ used in Task 3 step 3 wiring ✓
- `_RE_REPLACEWITH`, `_RE_VALID_ATTACKER`, `_RE_VALID_BLOCKER` defined and referenced consistently ✓
- New cost category names (subcounter, addcounter, exilegrave, return, reveal, mill, taptype) used identically in test, helper, diagnostic, and documentation ✓

**Project rule compliance:**
- General mechanical understanding, not per-card rules ✓
- No code duplication between training and inference (single source: `forge_features.py`) ✓
- Feature cache backed up before training ✓
- Single training invocation with `tee`, no greppy rerun loops ✓
- TDD: every code change has a failing test written first ✓
