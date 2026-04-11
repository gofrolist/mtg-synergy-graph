# CLAUDE.md

Guidance for Claude Code working in this repo.

## Project Overview

MTG Synergy Graph — analyzes Magic: The Gathering EDH/Commander deck synergies
using a deterministic, rule-based Forge-DSL Graph Engine.

Rule-based scorer joining Forge DSL ports. No training, no EDHREC at
inference, fully explainable. Current aggregate NDCG@30 ~ 0.162 on the
25-commander golden set; avg Hi-Syn 16.7%, OnPage 40.5% (top-50).

```
Forge card DSL -> importer.py -> synergy.db (cards + card_ports)
                                         |
           SynergyEngine(db).page(commander, limit=N)
                                         |
  score_all_candidates() buckets:
    port_match, cost_synergy, catch_all, scaling, deck_hints,
    chain_match, lord, amplifier, etb_self, graph_metrics,
    strategic, resource_density, effect_resonance,
    replacement_resonance, sacrifice_synergy, staple, replacement
  -> penalties.py applies hard filters / multipliers
```

- **Chains**: branches resolved at parse time (`ChainNode.branch_kind`),
  discounted in scoring via `BRANCH_MULTIPLIER`. See `docs/SPEC.md` v1.2.2.
- **Resource-density layer** (S7.5): `_extract_commander_anchors()` maps
  each anchor type -> cmc cap based on channel (consuming cost / trigger ->
  cap=3; non-consuming cost / cost-reduction -> uncapped). Recursion bonus
  for Undying/Persist/Unearth/... and any `ChangeZone|Graveyard->Battlefield`.
- **Warm-path perf**: Meren `computed` ~1.5s, test suite (322) ~64s,
  golden set check (25) ~21s. Bottleneck: `sqlite3.Cursor.fetchall`.

## Common Commands

```bash
# Import fresh DB
uv run python scripts/import_cardsfolder.py

# Recommend
uv run python scripts/recommend.py --commander "Korvold, Fae-Cursed King" --top 30 --explain

# Compare vs EDHREC / golden-set regression
uv run python scripts/compare_edhrec.py --commanders tests/fixtures/golden_set.json
uv run python scripts/golden_set_track.py --baseline tests/fixtures/golden_set_run.json

# Tests
uv run pytest tests/                # 322 tests, ~64s
```

## DB Schema (`data/tags.db`)

| Table | Rows | Purpose |
|---|---|---|
| cards | ~36k | Scryfall metadata |
| edhrec_card_synergy | ~733k | EDHREC synergy for 2,761 commanders (87%) |
| forge_abilities | ~72k | Raw Forge ability data + SubAbility chain expansions. `static_mode` column holds `Mode$` from S: lines (Continuous, Panharmonicon, ReduceCost...). R: replacements store `ValidPlayer$` in `target`, `verb` NULL. |
| forge_deck_tags | ~14k | Forge AI: has/hints/needs, 9,868 cards |
| forge_name_map | ~31k | Forge name -> oracle_id (prefers non-token) |

## Project Structure

```
src/mtg_synergy_graph/    # Library source
scripts/                  # CLI tools (import, recommend, compare, golden-set)
tests/                    # 322 tests + fixtures
docs/                     # SPEC.md design document
```

### Key files -- `mtg_synergy_graph`

- `engine.py` -- `SynergyEngine`, `score_all_candidates()`
- `importer.py` -- Forge DSL parser, DB importer
- `penalties.py` -- hard filters and multipliers
- `scoring.py` -- bucket scorers orchestration
- `graph_metrics.py` -- causal graph: PageRank, hub scores, Jaccard
- `graph_cache.py` -- precomputed graph cache
- `validate.py` -- EDHREC comparison utilities

## Conventions

- Cards keyed by Scryfall `oracle_id`. `data/oracle_cards.json` is gitignored.
- Security: SQL fragment interpolation guarded by `_VALID_*_EXPRS` frozensets +
  `ValueError` (never `assert` -- stripped by `python -O`); external URLs
  validated via `urlparse`.
