# CLAUDE.md

Guidance for Claude Code working in this repo.

## Project Overview

MTG Synergy Graph — analyzes Magic: The Gathering EDH/Commander deck synergies.

Two parallel inference engines:

1. **LightGBM LambdaRank model** (legacy, production): 98 Forge-native features
   trained on EDHREC synergy labels. `EDHREC_FREE=1` → pure Forge-native inference
   (day-1 new card support, ~4% lower NDCG). Current NDCG@30 ≈ 0.571.
2. **Deterministic Forge-DSL Graph Engine** (`packages/mtg-synergy-graph`):
   rule-based scorer joining Forge DSL ports. No training, no EDHREC at
   inference, fully explainable. Current aggregate NDCG@30 ≈ 0.162 on the
   25-commander golden set; avg Hi-Syn 16.7%, OnPage 40.5% (top-50).

## Signal Architecture (LightGBM pipeline)

```
Color filter → all legal cards → Forge LambdaRank GBM (98 features)
                                → post-scoring penalties + mechanical bonus (±15%)
                                → top N
```

- **Features** (`FORGE_FEATURE_NAMES` in `scripts/train_fusion_model.py`,
  implemented in `packages/mtg-synergy/src/mtg_synergy/recommend/forge_compute.py`):
  mech_cosine, graph_neighbor_overlap, graph_pagerank, card_hub_score,
  per-category mech sub-products (8 categories × fwd/rev), functional
  fingerprints (33-dim), forge_deck_tags, cost/trigger features, etc.
- **Mechanics vectors** (`recommend/mechanics_vectors.py`): auto-derived
  257 concepts + 80 subtypes (dim=337). Event tuples from Forge DSL.
  All cards produce concepts; opponent-only handling is post-scoring.
- **Causal graph**: 21.7M edges, 30+ event types (verb_event_map from
  Forge Java source), SubAbility chains, IDF weighting. Synthetic +
  entity-presence + continuous pump edges.
- **Training labels**: `edhrec_card_synergy` (703k rows, 2,724 commanders,
  continuous grading). Grade 5≥0.30, 4≥0.15, 3≥0.05, 2∈[0,0.05), 1<0,
  0=random neg. `label_gain=[0,1,3,8,20,30]`. 2:1 negative ratio, 2-tier
  sampling (⅓ subtype / ⅓ tag / ⅓ random). Grade 5 weight ×3, grade 4 ×2.
- **GBM**: LambdaRank, num_leaves=767, lr=0.025, n_estimators=1000
  (early_stop=40), bagging_freq=5.

## Deterministic Forge-DSL Graph Engine

`packages/mtg-synergy-graph` — rule-based, deterministic, day-1-ready.

```
Forge card DSL → importer.py → synergy.db (cards + card_ports)
                                         ↓
           SynergyEngine(db).page(commander, limit=N)
                                         ↓
  score_all_candidates() buckets:
    port_match, cost_synergy, catch_all, scaling, deck_hints,
    chain_match, lord, amplifier, etb_self, graph_metrics,
    strategic, resource_density, effect_resonance,
    replacement_resonance, sacrifice_synergy, staple, replacement
  → penalties.py applies hard filters / multipliers
```

- **Chains**: branches resolved at parse time (`ChainNode.branch_kind`),
  discounted in scoring via `BRANCH_MULTIPLIER`. See `docs/SPEC.md` v1.2.2.
- **Resource-density layer** (§7.5): `_extract_commander_anchors()` maps
  each anchor type → cmc cap based on channel (consuming cost / trigger →
  cap=3; non-consuming cost / cost-reduction → uncapped). Recursion bonus
  for Undying/Persist/Unearth/… and any `ChangeZone|Graveyard→Battlefield`.
- **Warm-path perf**: Meren `computed` ~1.5s, test suite (227) ~48s,
  golden set check (25) ~21s. Bottleneck: `sqlite3.Cursor.fetchall`.

## Common Commands

### LightGBM pipeline

```bash
# Data pipeline (new-set update workflow)
python3 scripts/download_cards.py                                 # 1. Scryfall (~150MB)
python3 scripts/import_forge.py --download --import               # 2. Forge abilities
python3 scripts/build_graph.py --rebuild                          # 3. Causal graph (~21M edges)
python3 scripts/fetch_edhrec_all.py --max 2000 --refresh-top 200  # 4. EDHREC refresh
python3 scripts/train_fusion_model.py --rebuild-features --validate  # 5. Train + validate (~8 min)
python3 scripts/export_inference_db.py                            # 6. Export inference (~646 MB)

# Training variants
python3 scripts/train_fusion_model.py              # cached features, parallel folds (~3 min)
python3 scripts/train_fusion_model.py --quick      # single fold (~2 min)
python3 scripts/train_fusion_model.py --tune       # parallel HP search (~12 min)

# Recommendations
uv run mtg-synergy --commander "Krenko, Mob Boss" --recommend
uv run mtg-synergy --commander "Krenko, Mob Boss" --gems     # pure mechanical, no popularity

# Validation
python3 scripts/compare_edhrec.py --commander "Krenko, Mob Boss"
python3 scripts/compare_edhrec.py --top 100
python3 scripts/validate_recommendations.py --top 100
uv run pytest tests/                                         # 148 tests, ~15s
```

### Deterministic engine

```bash
# Import fresh DB
uv run python packages/mtg-synergy-graph/scripts/import_cardsfolder.py \
    --db /tmp/synergy_full.db --cards-folder data/forge/res/cardsfolder

# Recommend
uv run python packages/mtg-synergy-graph/scripts/recommend.py \
    --db /tmp/synergy_full.db --commander "Korvold, Fae-Cursed King" --top 30 --explain

# Compare vs EDHREC / golden-set regression
uv run python packages/mtg-synergy-graph/scripts/compare_edhrec.py \
    --db /tmp/synergy_full.db --edhrec-db data/tags.db \
    --commanders packages/mtg-synergy-graph/tests/fixtures/golden_set.json --top 50
uv run python packages/mtg-synergy-graph/scripts/golden_set_track.py \
    --db /tmp/synergy_full.db --edhrec-db data/tags.db \
    --baseline packages/mtg-synergy-graph/tests/fixtures/golden_set_run.json

uv run pytest packages/mtg-synergy-graph/tests/                # 227 tests, ~48s
```

## DB Schema (`data/tags.db`)

| Table | Rows | Purpose |
|---|---|---|
| cards | ~36k | Scryfall metadata |
| interaction_edges | ~21.7M | Causal graph: 30+ event types + synthetic + entity-presence + continuous pump + theme edges |
| edhrec_card_synergy | ~733k | EDHREC synergy for 2,761 commanders (87%) |
| forge_abilities | ~72k | Raw Forge ability data + SubAbility chain expansions. `static_mode` column holds `Mode$` from S: lines (Continuous, Panharmonicon, ReduceCost…). R: replacements store `ValidPlayer$` in `target`, `verb` NULL. |
| forge_deck_tags | ~14k | Forge AI: has/hints/needs, 9,868 cards |
| forge_name_map | ~31k | Forge name → oracle_id (prefers non-token) |

## Monorepo Structure

```
packages/
  mtg-synergy/          # Inference library (numpy + lightgbm)
  mtg-synergy-train/    # Training + pipelines (depends on mtg-synergy)
  mtg-synergy-graph/    # Deterministic engine (independent)
```

### Key files — `mtg_synergy` (inference)

| Module | Purpose |
|---|---|
| `config.py` | Paths via env vars `MTG_SYNERGY_{DATA_DIR,DB_PATH,ARTIFACT_DIR}`, `extract_subtypes()` (DFC-aware) |
| `protocol.py` | `CardProvider` protocol + `SqliteCardProvider` — abstracts card data access |
| `recommend/engine.py` | `recommend_cards()` entry point |
| `recommend/scoring.py` | `color_identity_filter`, `score_forge_candidates`, `_apply_penalties`, `_apply_mechanical_bonus`, `_load_gbm` |
| `recommend/hidden_gems.py` | `find_hidden_gems()` — pure mechanical engine |
| `recommend/forge_features.py` | `ForgeFeatureContext`, `CSRIndex`, `ForgeProfile` |
| `recommend/forge_compute.py` | `CmdrFeatureContext` — per-commander features, partner pairs |
| `recommend/mechanics_vectors.py` | Auto-derived concept space, `summarize_commander()` |
| `recommend/cmdr_patterns.py` | `detect_cmdr_patterns()` |
| `model_meta.py` | `.meta.json` sidecar + `model_registry.jsonl` versioning |

### Key files — `mtg_synergy_train`

| Module | Purpose |
|---|---|
| `db.py`, `tag_db.py`, `cli.py` | DB factory, schema utils, CLI dispatcher |
| `causal/` | Interaction graph builder, edge types, verb_event_map |
| `parse/` | Forge import, filter parser, types |

### Scripts

| File | Purpose |
|---|---|
| `synergy_graph.py` | CLI entry (re-exports both packages) |
| `train_fusion_model.py` | GBM training + feature cache + `--validate` |
| `sweep_hyperparams.py` | Two-phase HP sweep |
| `compare_edhrec.py` | Recs vs EDHREC High Synergy |
| `validate_recommendations.py` | End-to-end pipeline validation |
| `build_graph.py`, `import_forge.py`, `fetch_edhrec_all.py`, `download_cards.py` | Data pipeline |
| `export_inference_db.py` | Inference artifact bundle |

## Post-scoring Penalties (`_apply_penalties()`)

- Hard filters (score=-1e9): "Choose a Background" / "Doctor's companion",
  wrong-color needs.
- Multipliers: required_subtype mismatch ×0.4, wrong token type ×0.5,
  wrong counter type ×0.4, niche counter ×0.4, counters-on-lands ×0.4,
  unmet Type$ needs ×0.3, unmet Ability$ needs ×0.85, opponent-only
  replacement for self-mill commander ×0.3.

## API-Ready Inference (warm server)

```python
ctx = ForgeFeatureContext(synergy_conn, preload_edges=True, card_provider=provider)
gbm = _load_gbm()

# ~0.5s per request; ~490 MB RSS
score_forge_candidates(candidate_scores, cards, synergy_conn, commander, deck_cards,
                       forge_ctx=ctx, gbm_model=gbm, card_provider=provider)

# Partner pairs
cmdr_ctx = CmdrFeatureContext(ctx, cmdr_oids=["oid1", "oid2"], deck_oids=deck_oids)
```

Minimum deployment: 1 GB RAM VPS + persistent volume for ~650 MB artifacts.

## Conventions

- Cards keyed by Scryfall `oracle_id`. `data/oracle_cards.json` is gitignored.
- Runtime deps: `numpy` + `lightgbm`. Stdlib `urllib.request` for HTTP.
- Monorepo via `uv` workspace. Inference imports from `mtg_synergy`, training
  from `mtg_synergy_train`. Inference never touches `cards` table directly —
  always via `CardProvider`.
- `score_forge_candidates()` does not mutate caller's `cards` list.
- `_apply_penalties()` uses `cmdr_ctx.cmdr_profile` (merged for partners).
- Security: `np.load(allow_pickle=False)` is enforced project-wide (safe
  deserialization only); SQL fragment interpolation guarded by
  `_VALID_*_EXPRS` frozensets + `ValueError` (never `assert` — stripped by
  `python -O`); external URLs validated via `urlparse`. Adjacency cache
  uses `np.savez`.
- After training: run `--validate`, then `export_inference_db.py`.
