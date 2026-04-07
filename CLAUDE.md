# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

MTG Synergy Graph — a tool for analyzing Magic: The Gathering EDH/Commander deck synergies. The system uses a LightGBM LambdaRank model trained on EDHREC labels. Set EDHREC_FREE=1 for pure Forge-native inference (day-1 new card evaluation, ~4% lower NDCG). Model versioning via sidecar `.meta.json` + `model_registry.jsonl`.

### Signal Architecture (recommendation pipeline)

```
FORGE MODEL (--recommend): Pure Forge mechanical synergy
  1. Color-identity filter → all legal cards scored directly by GBM
  2. Forge LambdaRank GBM: 98 features (92 active + 6 zeroed), EDHREC labels,
     fully general (no archetype names). Authoritative feature list:
     `FORGE_FEATURE_NAMES` in `scripts/train_fusion_model.py`. Implementation:
     `packages/mtg-synergy/src/mtg_synergy/recommend/forge_compute.py`.
     + mech_cosine (F3): full commander-to-card mechanics vector cosine
       (replaced strategy_cosine — no EDHREC strategy dependency)
     + forge_deck_tags (Forge's deck-building AI: has/hints/needs theme signals)
     + ability counts, token complexity, zone interaction, ability density
     + deck tag expansion (cmdr_needs_to_card_has, card_needs_satisfied, needs_rarity)
     + counter/anthem distinction (put_counter_ratio, cmdr_counter_x_put_counter,
       cmdr_p1p1_card_no_counters)
     + functional fingerprints (33-dim semantic vectors, 4 dot-product features)
     + general demand features (2): verb_demand_match, type_demand_match
     + per-category mech sub-products (16 features, 8 categories × fwd/rev):
       board (creature/permanent events), resource (counters/draw/life/damage),
       disruption (discard/mill/target), tempo (spell_cast/attacks/blocks),
       utility (tap/untap/pump/mana), zones (graveyard/exile/hand),
       themes (equipment/defender/etb), tribal (80 subtypes)
     + Affected$ scope (F89): who benefits from effects (YouCtrl vs OppCtrl)
     + pump magnitude (F90-F91): NumAtt$/AddPower$ values + variable detection
     + granted ability match (F44): AddAbility$/AddTrigger$ detail vs cmdr
     + type change tribal (F92): ChangeType$ matching commander subtypes
     + graph_neighbor_overlap (F93): Jaccard-like overlap of causal graph neighborhoods
     + cost_feeds_cmdr (F94): card ability costs match commander's resource production
     + trigger_specificity (F95): IDF-weighted trigger filter match with commander
     + mech_density (F96): mechanical complexity per mana (mech_nonzero / cmc)
     + graph_pagerank (F97): PageRank centrality on causal graph
     + 6 zeroed features (F25-F27, F29-F31): redundant workarounds kept as
       columns for index stability, always output 0.0
     Top features (EDHREC_FREE): graph_neighbor_overlap 9.2%, mech_cosine 6.5%,
     graph_pagerank 6.2%, card_hub_score 5.3%, mech_density 5.2%,
     ability_density 4.2%, cmdr_2hop_ratio 4.8%
  3. Forge mechanics vectors: auto-derived concept space (257 concepts + 80 subtypes,
     dim=337). Effects and triggers map to same dimensions via event tuples
     (event_class, type_qualifier, from_zone, to_zone). Built dynamically from Forge DSL.
     Zone-aware concepts: enters_from_graveyard/exile/hand, goes_to_graveyard/exile
     Theme concepts: equipment_enters, equipment_equipped, defender_available, etb_doubled
     ALL cards produce concepts regardless of targeting (opponent-only cards included).
     Opponent-only handling moved to post-scoring penalties in _apply_penalties().
     R: replacement effects add to CONSUMES vector (Bruvac consumes mill events).
     RepeatSubAbility$ parsed for sub-ability verbs (e.g., DBMill → mill concept).
  4. Can evaluate new cards day-1 without playtesting data
  5. Works for any of 3,141+ commanders (not just 1,361 with EDHREC)
  6. NDCG@30 = 0.571 on leave-commander-out CV (2:1 negatives, sample-weighted, early_stop=40)
     EDHREC_FREE NDCG@30 = 0.569 (pure Forge-native, no edhrec_deck_pct feature)
     Training labels: edhrec_card_synergy (703k rows, continuous synergy grading)
     Grade 5=synergy≥0.30, 4=synergy≥0.15, 3=synergy≥0.05, 2=synergy 0-0.05, 1=negative, 0=random neg
     label_gain=[0, 1, 3, 8, 20, 30] (less steep curve between mid/high grades)
     EDHREC synergy = deck_inclusion% - color_baseline% (commander-specific affinity)
     Training: 2:1 negative ratio (1.4M negatives), 2-tier sampling:
       1/3 subtype overlap, 1/3 tag overlap, 1/3 random (strategy sampling removed)
     Per-grade sample weights: grade 5→3x, grade 4→2x
     2,724 commanders with EDHREC data (87% of legal commanders)
     (variance ±0.3 between runs due to GBM non-determinism)

CAUSAL GRAPH:
  - 21.7M edges across 30+ event types (verb_event_map extracted from Forge Java source)
  - SubAbility chains followed: 72k abilities (12.7k from secondary effects)
  - IDF weighting, chain scoring, anti-synergy detection
  - Synthetic edges (6.3M): SpellCast, Attacks, LandPlayed,
    ChangesZone+Battlefield (creatures, artifacts, enchantments, planeswalkers entering)
  - Entity presence edges (2.6M): token creators → "for each [Type]" scaling,
    sacrifice outlets, tap abilities. Connects Krenko → Brightstone Ritual.
  - Continuous pump edges (54k): subtype-specific lord/anthem effects → matching creatures.
    E.g., Kyler → all Humans (ContinuousPump event). Enables feedback loop detection.
  - Exact (subtype match, 0.3) and broad (card_type match, 0.15) precision levels
  - Materialized filter_precision column for fast queries
```

### Current Performance

Forge model finds mechanical synergies from Forge data alone (no EDHREC at inference).
Evaluation: compare our top 30 recommendations against EDHREC's "High Synergy Cards" section.

Use `scripts/compare_edhrec.py --commander "Name"`, `--top 100`, or `--all` to evaluate.

## Common Commands

```bash
# === DATA PIPELINE ===
python3 scripts/download_cards.py                  # Refresh Scryfall data (~150MB)
python3 scripts/import_forge.py --download --import  # Update Forge ability data
python3 scripts/build_graph.py --rebuild   # Build causal interaction graph from Forge (~17M edges)
python3 scripts/build_graph.py --stats             # Graph stats
# python3 scripts/strategy_detector.py --populate  # (legacy, no longer used by model)

# === EDHREC data ===
python3 scripts/fetch_edhrec_all.py                    # Fetch next 500 new commanders (synergy + avg decks)
python3 scripts/fetch_edhrec_all.py --max 2000         # Fetch up to 2000 new commanders
python3 scripts/fetch_edhrec_all.py --refresh-top 200  # Re-fetch top 200 popular commanders (stale data)
python3 scripts/fetch_edhrec_all.py --refresh          # Re-fetch ALL existing commanders
python3 scripts/fetch_edhrec_all.py --stats            # Show coverage stats

# === Forge model (LightGBM LambdaRank) ===
python3 scripts/train_fusion_model.py    # Train forge GBM (cached features, parallel folds, ~3 min)
python3 scripts/train_fusion_model.py --rebuild-features  # Rebuild features (shared ctx, 8 workers) + train (~7 min)
python3 scripts/train_fusion_model.py --quick             # Single-fold fast iteration (~2 min)
python3 scripts/train_fusion_model.py --tune              # Parallel HP search + train (~12 min)

# === Recommendations ===
uv run mtg-synergy --commander "Krenko, Mob Boss" --recommend     # Recommend cards (GBM + mechanical bonus)
uv run mtg-synergy --commander "Krenko, Mob Boss" --recommend --top 10
uv run mtg-synergy --commander "Krenko, Mob Boss" --recommend --deck deck.txt  # With deck context
uv run mtg-synergy --commander "Krenko, Mob Boss" --gems         # Hidden gems (pure mechanical, no popularity)

# === Comparison & validation ===
python3 scripts/compare_edhrec.py --commander "Krenko, Mob Boss"  # Single commander vs EDHREC
python3 scripts/compare_edhrec.py --top 100                        # Top 100 by popularity (quiet)
python3 scripts/compare_edhrec.py --all --quiet                    # All commanders summary
python3 scripts/validate_recommendations.py --top 100              # Pipeline validation (model + scoring)
python3 scripts/train_fusion_model.py --validate      # Train + validate in one step

# === Export inference artifacts ===
python3 scripts/export_inference_db.py                # Build data/inference/ (~646 MB)
python3 scripts/export_inference_db.py --output /path # Custom output directory

# Tests
uv run pytest tests/ -v                        # Run all 148 tests
uv run pytest tests/test_recommendation_quality.py -v              # Pipeline quality tests only
```

## Architecture

### Enrichment Pipeline

```
Scryfall API → scripts/download_cards.py → data/oracle_cards.json (36k cards)
                                                ↓
                    scripts/import_forge.py → forge_abilities + forge_name_map tables
                                                ↓
                    scripts/build_graph.py --rebuild → interaction_edges table (17.1M causal edges, 30 event types)
                                                ↓
                    scripts/fetch_edhrec_all.py → edhrec_card_synergy table (733k pairs, 2,761 cmdrs)
                                                ↓
                    scripts/train_fusion_model.py → data/fusion_model_forge.lgb
                                                ↓
                              uv run mtg-synergy --commander "Name" <name>
                              (color filter → forge GBM → causal graph)
```

### New-set update workflow

```bash
python3 scripts/download_cards.py                               # 1. Refresh Scryfall
python3 scripts/import_forge.py --download --import             # 2. Update Forge data
python3 scripts/build_graph.py --rebuild                # 3. Rebuild causal graph
python3 scripts/fetch_edhrec_all.py --max 2000 --refresh-top 200  # 4. Refresh EDHREC (new + top 200 stale)
python3 scripts/train_fusion_model.py --rebuild-features --validate  # 5. Retrain + validate (~8 min, $0)
python3 scripts/export_inference_db.py                          # 6. Export inference artifacts (~646 MB)
```

### DB Schema (data/tags.db)

| Table | Rows | Purpose |
|---|---|---|
| cards | ~36k | Card metadata from Scryfall |
| card_strategies | ~88k | Strategy assignments |
| interaction_edges | ~21.7M | Causal edges from Forge: 30+ event types + 6.5M synthetic + 2.7M entity-presence + 60k continuous pump + 922k theme synergy edges |
| edhrec_card_synergy | ~733k | EDHREC synergy scores for 2,761 commanders (87% coverage) |
| forge_abilities | ~72k | Raw Forge ability data + SubAbility chain expansions (12.7k expanded rows). 20 columns: 19 consumed in features or during import, 1 unused (unless_cost). sub_ability column is resolved during import by expanding chains into separate rows. R: replacement abilities: target stores ValidPlayer$ (e.g., Player.Opponent), verb stays NULL to avoid polluting forge_profiles. |
| forge_deck_tags | ~14k | Forge deck-building AI: has (what card provides), hints (what card wants), needs (what card requires). 9,868 unique cards. |
| forge_name_map | ~31k | Forge card name → oracle_id mapping (prefers non-token versions) |

### Forge Model

**Forge model** (data/fusion_model_forge.lgb):
- LambdaRank GBM on 98 features (92 active + 6 zeroed), shared via
  `forge_features.py` + `forge_compute.py`. 100% Forge-native, fully general.
  Authoritative feature list: `FORGE_FEATURE_NAMES` in `scripts/train_fusion_model.py`.
  causal (2), mech_cosine, forge_ability_cosine, phase (2), tribal, cmc,
  deck edges (3), causal_composite, card_hub_score,
  forge type/mechanics (6), counter/zone/target/keyword matching (6),
  ability profile flags (3 active + 6 zeroed), deck tag overlaps (5+3 needs),
  scaling flags (6), ability/token stats (6), counter/anthem (3),
  functional fingerprints (4), 2-hop graph (2), card quality (4),
  tribal depth (3), general demand (2),
  per-category mech sub-products (16): 8 categories × fwd/rev,
  field features (4): affected_scope_ratio, pump_magnitude,
  pump_is_variable, type_change_tribal_match,
  graph features (2): graph_neighbor_overlap, graph_pagerank,
  cost/trigger features (3): cost_feeds_cmdr, trigger_specificity, mech_density
- Functional fingerprints (`ForgeFeatureContext._func_fingerprints`): 33-dim semantic
  vectors per card encoding produces/requires/amplifies/targets. Dot products between
  commander and card fingerprints capture synergy without hand-coded rules.
- Training data: commander-illegal cards filtered from negative pool
- Forge profiles extract ALL raw_line fields: granted_keywords, conditions,
  duration, effect_zones, combat_damage, is_secondary,
  gain_control, damage_amount, cards_drawn, life_amount, required_subtypes,
  produces_mana, grants_abilities, token_amount_variable, counters_on_lands,
  counter_trigger_themes, has_p1p1, opponent_only_events,
  affected_self_count, affected_opp_count, affected_scope_ratio,
  granted_ability_names, granted_triggers, changes_type,
  grants_all_creature_types, max_pump_power, pump_is_variable,
  cost_types (9 categories: sacrifice/tap/discard/exile/paylife +
  subcounter/exilegrave/taptype/return — Phase 1 additions feed
  cost_feeds_cmdr F94 transparently; exilegrave is additive with exile
  to preserve pre-Phase-1 baseline counts), raw_trigger_filters
  (from cost, defined, ValidCards$, ValidAttacker$/ValidBlocker$
  (Phase 1 — combat triggers: Attacks/Blocks/AttackerBlockedByCreature),
  Affected$, Produced$, AddAbility$, AddTrigger$, ChangeType$, NumAtt$,
  AddPower$, NumDef$, AddToughness$, TokenAmount$, Event$, ReplaceWith$,
  ValidPlayer$ fields)
- forge_deck_tags: Forge's deck-building AI (has/hints/needs) for 9,868 cards
  Maps what a card provides, wants, and requires in a deck
- Mechanics vectors (`src/mtg_synergy/recommend/mechanics_vectors.py`): auto-derived
  concept space (257 concepts + 80 subtypes, dim=337). Event tuples extracted
  from all Forge abilities, vocabulary built dynamically and sorted deterministically.
  ALL cards produce concepts (opponent-only gate removed, handled by penalties).
  R: replacement effects add to consumes vector via Event$ field.
  RepeatSubAbility$ parsed for sub-ability verbs (DBMill → mill concept).
  Type-based produces: non-land cards produce spell_cast, creatures produce creature_attacks.
  Zone-aware: raw_line fallback for Origin$/Destination$ when column values are empty.
  Theme concepts: equipment_enters, equipment_equipped, defender_available, etb_doubled
  `summarize_commander()`: mechanics-derived labels for CLI display (replaces strategy_detector)
- Training data: edhrec_card_synergy (703k rows, 2,724 commanders), continuous synergy grading
  Grade 5=synergy>=0.30, 4=synergy>=0.15, 3=synergy>=0.05, 2=synergy 0-0.05, 1=negative, 0=random neg
  label_gain=[0, 1, 3, 8, 20, 30] (less steep curve between mid/high grades)
  EDHREC synergy = deck_inclusion% - color_baseline% (commander-specific affinity)
  Generic staples (>30% frequency) demoted from grade 4/5 to 3
  Commander-illegal cards filtered from negative pool
  2:1 negative ratio, 2-tier sampling: 1/3 subtype overlap, 1/3 tag overlap, 1/3 random
  (strategy-based sampling removed — no EDHREC strategy dependency)
- Training: `python3 scripts/train_fusion_model.py --rebuild-features` (~7 min)
  Feature build: shared ForgeFeatureContext via fork pool (8 workers, ~17s)
  CV folds: 3 folds trained in parallel via ProcessPoolExecutor (thread-pinned)
  Batch features: vectorized array indexing for ~50 features, loop for ~35
  Vectorized NDCG@30, CV splits, weight arrays, group arrays
  Use `--tune` for parallel HP search (~12 min). Default uses cached best HP.
- Feature importance (EDHREC_FREE): graph_neighbor_overlap 9.2%, mech_cosine 6.5%,
  graph_pagerank 6.2%, card_hub_score 5.3%, mech_density 5.2%,
  ability_density 4.2%, cmdr_2hop_ratio 4.8%, cmc 3.9%, forge_ability_cosine 3.8%
- EDHREC_FREE=1 env var disables edhrec_deck_pct for pure Forge-native inference
- Model versioning: `model_meta.py` saves `.meta.json` sidecar + `model_registry.jsonl`
  Tracks NDCG, hyperparameters, feature importance, git commit, MD5 hash
- Edge index: two-layer cache (npz raw edges + CSR adjacency cache)
  Warm path: adj cache loaded directly (~0.1s), raw edge arrays never touched
  Cold path: npz ~0.1s + adj build ~11s (auto-saved to adj cache)
  Auto-invalidates on MAX(rowid), card count, or strength mode change
  (uses MAX(rowid) instead of COUNT(*) to avoid 26s full table scan)
- CSRIndex: memory-efficient adjacency using sorted keys + offsets + values arrays.
  O(log n) lookup via np.searchsorted. Saves ~100 MiB vs Python dict unpacking.
- Ability vectors: sparse format (uint16 indices + float32 values per card).
  99.7% sparse (avg 2.6 nonzero per 754-dim), saves ~89 MiB vs dense arrays.
- ForgeProfile: __slots__-based profile objects replace per-card dicts.
  312 bytes vs 832 bytes per profile, frozenset dedup (8830 unique across 570K refs).
- Edge index pre-loaded at inference: CmdrFeatureContext uses in-memory CSR adjacency (~3s total)
- Per-grade sample weights: grade 5 x3, grade 4 x2
- HP sweep tool: `scripts/sweep_hyperparams.py` — two-phase sweep with pipeline validation
- Post-scoring penalties (scoring.py `_apply_penalties()`):
  - required_subtype mismatch (×0.4): card requires creature types the commander doesn't have
  - excluded_subtypes (×0.3): card excludes commander's creature type from effects
  - wrong token type (×0.5): tribal commander, card creates wrong token type
  - wrong counter type (×0.4): card puts non-P1P1 counters (M1M1, TIME) for P1P1 commander
  - non-counter creatures for counter commanders (×0.6): no has_p1p1 + no P1P1 counter verbs
  - niche counter penalty (×0.4): TIME/EXPERIENCE/ENERGY-only cards when commander doesn't use them
  - counters on lands for counter commanders (×0.4): Earthbend, land-targeting PutCounter
  - "Choose a Background" / "Doctor's companion" hard filter (score=-1e9)
  - wrong-color needs hard filter (score=-1e9): e.g., Pearl Medallion in mono-G
  - unmet Type$ needs/hints (×0.3): e.g., needs=Type$Dinosaur in Human deck
  - unmet Ability$ needs (×0.85): card needs Ability$LifeGain but commander only has
    Ability$Counters. Uses cmdr_has only (not cmdr_hints — hints=wants, not provides).
    171 cards with Ability$ needs across 11 ability types.
  - opponent-only replacement for self-mill commander (×0.3): R: ability with
    ValidPlayer$Player.Opponent for Mill event when commander self-mills
    (has Mill verb or Milled trigger). E.g., Bruvac penalized for Sidisi.
  - excluded_subtypes penalty REMOVED — absorbed by forge_anti_tribal feature (F16)
    + affected_scope_ratio (F89)
- DFC-aware subtype extraction: `config.extract_subtypes()` parses both faces
- Pipeline validation: `--validate` flag or `test_recommendation_quality.py` (7 tests)
- GBM: LambdaRank, num_leaves=767, lr=0.025, n_estimators=1000 (early_stop=40),
    label_gain=[0,1,3,8,20,30], bagging_freq=5, colsample_bytree=0.6, feature_fraction_bynode=0.9

### Recommendation Pipeline (scripts/synergy_graph.py --commander "Name" --recommend)

```
1. Candidate selection: Color-identity filter → ALL legal cards
2. Score all candidates with GBM (batch predict, ~0.5s for 13k cards):
   89 Forge-native features (LambdaRank)
3. Post-scoring: anti-synergy penalties + mechanical synergy bonus (±15%)
   Bonus: produces↔consumes dot product, verb→trigger alignment,
   creature ETB / sacrifice outlet / spellcast pattern matches
4. Sort and output top N with clickable Scryfall hyperlinks (OSC 8)
Total time: ~0.5s per commander with warm ForgeFeatureContext (pass forge_ctx=
  to score_forge_candidates to reuse). Cold start: ~3.3s CLI wall time (warm adj cache).
```

### API-Ready Inference (warm server pattern)

```python
# Load context once at startup (~4s with warm adj cache)
# With CardProvider: card data comes from external DB (e.g., Neon Postgres)
ctx = ForgeFeatureContext(synergy_conn, preload_edges=True, card_provider=provider)
gbm = _load_gbm()  # cached at module level after first call

# Per-request: ~0.5s per commander (was 60-73s before context reuse)
score_forge_candidates(candidate_scores, cards, synergy_conn, commander, deck_cards,
                       forge_ctx=ctx, gbm_model=gbm, card_provider=provider)

# Partner pair support: pass cmdr_oids list to CmdrFeatureContext
cmdr_ctx = CmdrFeatureContext(ctx, cmdr_oids=["oid1", "oid2"], deck_oids=deck_oids)
```

Runtime footprint (warm server): ~490 MB RSS, ~650 MB disk (inference artifacts).
Minimum deployment: 1 GB RAM VPS with persistent volume.

### Hidden Gem Engine (scripts/synergy_graph.py --commander "Name" --gems)

Pure mechanical reasoning — no GBM model, no popularity bias.
Scores cards by: mechanics vector dot products (produces↔consumes),
causal graph edge strength (direct + 2-hop), verb→trigger alignment,
ETB/sacrifice/spellcast pattern matching, functional fingerprint cosine.
Filters OUT cards appearing in >5% of EDHREC decks → surfaces
mechanically-synergistic cards that nobody plays.

## Key Files

### Monorepo Package Structure

```
packages/
  mtg-synergy/          # Package A: inference library (pip install mtg-synergy)
    src/mtg_synergy/    # numpy + lightgbm only
  mtg-synergy-train/    # Package B: training + pipelines (pip install mtg-synergy-train)
    src/mtg_synergy_train/  # depends on mtg-synergy
```

### Package A: `mtg_synergy` (inference library)

| Module | Purpose |
|---|---|
| `config.py` | Configurable paths (env vars: MTG_SYNERGY_DATA_DIR, MTG_SYNERGY_DB_PATH, MTG_SYNERGY_ARTIFACT_DIR), `extract_subtypes()` |
| `constants.py` | STAPLE_ROLES |
| `protocol.py` | `CardProvider` protocol + `SqliteCardProvider` — abstracts card data access for external DB integration |
| `recommend/engine.py` | `recommend_cards()` — forge model recommendation pipeline |
| `recommend/scoring.py` | `color_identity_filter()`, `score_forge_candidates(card_provider=)`, `batch_recommend(card_provider=)`, `_apply_penalties()`, `_apply_mechanical_bonus()`, `_load_gbm()` |
| `recommend/hidden_gems.py` | `find_hidden_gems(card_provider=, cmdr_oids=)` — pure mechanical synergy engine |
| `recommend/forge_features.py` | `ForgeFeatureContext(conn, card_provider=)` — data loading, pre-computation. `CSRIndex` (memory-efficient adjacency), `ForgeProfile` (__slots__-based card profiles) |
| `recommend/forge_compute.py` | `CmdrFeatureContext(ctx, cmdr_oids=[...])` — per-commander features, partner pair support |
| `model_meta.py` | Model versioning: `save_model_meta()`, `load_model_meta()`, `append_to_registry()`, `build_meta()` |
| `recommend/mechanics_vectors.py` | Auto-derived forge mechanics vectors: shared game concept space (257 concepts + 80 subtypes), `summarize_commander()` |
| `recommend/cmdr_patterns.py` | `detect_cmdr_patterns()` — shared commander mechanical flag detection |

### Package B: `mtg_synergy_train` (training + pipelines)

| Module | Purpose |
|---|---|
| `db.py` | Centralized DB connection factory |
| `tag_db.py` | SQLite tag DB utilities (schema, queries, import) |
| `cli.py` | CLI dispatcher (argparse + command routing) |
| `causal/` | Interaction graph: DB storage, edge types, forge indexer, graph builder, IDF, verb_event_map |
| `parse/` | Forge data import: forge_import, forge_filter_parser, forge_types |
| `analysis/strategy.py` | `_detect_deck_types()` — tribal type detection |

### `scripts/` directory (pipelines + entry points)

| File | Purpose |
|---|---|
| `scripts/synergy_graph.py` | CLI entry point — re-exports from both packages |
| `scripts/export_inference_db.py` | Build inference artifact bundle (synergy.db + caches + model) |
| `scripts/train_fusion_model.py` | Forge LambdaRank GBM training + feature cache rebuild + `--validate` |
| `scripts/sweep_hyperparams.py` | Two-phase HP sweep with pipeline validation |
| `scripts/compare_edhrec.py` | Compare recommendations vs EDHREC High Synergy section |
| `scripts/validate_recommendations.py` | End-to-end pipeline validation (model + scoring + penalties) |
| `scripts/strategy_detector.py` | VESTIGIAL — populates `card_strategies` table from EDHREC + tribal oracle text. No longer used by inference; kept for `compare_strategy_vs_mech.py` |
| `scripts/build_graph.py` | Causal interaction graph builder CLI (--rebuild, --stats) |
| `scripts/import_forge.py` | Forge ability data importer |
| `scripts/fetch_edhrec_all.py` | Fetch EDHREC synergy + avg decks (concurrent, refresh support) |
| `scripts/download_cards.py` | Scryfall bulk data downloader |

## Key Conventions

- Cards keyed by `oracle_id` (Scryfall UUID) for dedup across reprints
- `data/oracle_cards.json` is gitignored (~150MB); must run `scripts/download_cards.py` first
- API calls use `urllib.request` (stdlib only, no external HTTP dependency)
- Dependencies: `numpy` + `lightgbm` (runtime), `vulture` (dev)
- Paths configurable via env vars: `MTG_SYNERGY_DATA_DIR`, `MTG_SYNERGY_DB_PATH`, `MTG_SYNERGY_ARTIFACT_DIR`
- Tribal subtypes extracted via `config.extract_subtypes()` (DFC-aware, both faces)
- CLI uses `--commander "Name"` or `uv run mtg-synergy --commander "Name"`
- Monorepo: uv workspace with `packages/mtg-synergy` (inference) + `packages/mtg-synergy-train` (training)
- Inference code: `from mtg_synergy...`, training code: `from mtg_synergy_train...`
- CardProvider protocol (`protocol.py`) abstracts card data — inference never queries `cards` table directly
- Partner pair support: `CmdrFeatureContext(ctx, cmdr_oids=["oid1", "oid2"])` merges profiles/vectors/edges
- Tests: 148 tests in `tests/`, run with `uv run pytest tests/` (~15s)
  - 7 end-to-end pipeline quality tests (`test_recommendation_quality.py`)
  - Requires trained model + populated DB (auto-skipped if missing)
- After training, run `--validate` then export: `python3 scripts/export_inference_db.py` (~646 MB)
- Adjacency cache uses np.savez (not legacy serialization) for security
- All np.load() calls use allow_pickle=False — enforced project-wide
- SQL fragment interpolation guarded by _VALID_*_EXPRS frozensets + ValueError
  (never use assert for security — stripped by python -O)
- External download URLs validated via urlparse (scheme + netloc)
- Scoring penalties in `_apply_penalties()` use `cmdr_ctx.cmdr_profile` (merged for partners)
- `score_forge_candidates()` does not mutate the caller's `cards` list — builds local `card_data` dict
