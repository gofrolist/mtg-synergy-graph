# MTG Synergy Graph

Analyze Magic: The Gathering EDH/Commander deck synergies using a deterministic, rule-based Forge-DSL Graph Engine.

The system parses card abilities from MTG Forge's game engine DSL, builds a port-matching graph, and scores synergies deterministically — no training, no EDHREC at inference, fully explainable, with day-1 support for new cards.

## Quick Start

```bash
# 1. Build a synergy DB from Forge cardsfolder
uv run python scripts/import_cardsfolder.py

# 2. Get recommendations
uv run python scripts/recommend.py --commander "Korvold, Fae-Cursed King" --top 30 --explain
```

## Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) (package manager)
- No external API keys required (100% local)

```bash
uv sync
```

## How It Works

```
Forge card DSL -> importer.py -> synergy.db (cards + card_ports)
                                         |
           SynergyEngine(db).page(commander, limit=N)
                                         |
  score_all_candidates() buckets:
    port_match, cost_synergy, sacrifice_synergy, counter_synergy,
    graveyard_synergy, trigger_resonance, stat_scaling,
    spellcast_density, scaling, deck_hints, chain, lord, amplifier,
    graph_metrics, strategic, resource_density, effect_resonance,
    replacement_resonance, staple, replacement, catchall
  -> penalties.py applies hard filters / multipliers
```

## Commands

```bash
# Import
uv run python scripts/import_cardsfolder.py

# Recommend
uv run python scripts/recommend.py --commander "Korvold, Fae-Cursed King" --top 30 --explain

# Compare vs EDHREC
uv run python scripts/compare_edhrec.py --commanders tests/fixtures/golden_set.json

# Golden-set regression tracking
uv run python scripts/golden_set_track.py --baseline tests/fixtures/golden_set_run.json

# Tests — unit only (this is what CI runs)
uv run pytest tests/ -q      # or: make test

# Tests — integration (needs data/synergy.db + data/forge cardsfolder)
uv run pytest -m integration # or: make test-integration

# Tests — everything
make test-all
```

Integration tests are tagged `@pytest.mark.integration` and excluded by default.
They require artefacts that aren't in the repo:

- `data/synergy.db` — produced by `scripts/import_cardsfolder.py`.
- `data/forge/forge-gui/res/cardsfolder/` — clone of
  [Card-Forge/forge](https://github.com/Card-Forge/forge) into `data/forge`.

CI therefore runs only the unit tier; the integration tier is **local-dev only**.
Run it with `make test-integration` after `make import`.

## Project Structure

```
src/mtg_synergy_graph/
  engine.py          # SynergyEngine, score_all_candidates()
  importer.py        # Forge DSL parser, DB importer
  penalties.py       # Hard filters and multipliers
  scoring.py         # Bucket scorers orchestration
  graph_engine.py    # Port matching, sacrifice/graveyard/trigger matchers
  graph_metrics.py   # Causal graph: PageRank, hub scores, Jaccard
  graph_cache.py     # Precomputed graph cache
  validate.py        # EDHREC comparison utilities
  schema.sql         # SQLite schema
scripts/             # CLI tools (import, recommend, compare, golden-set)
tests/               # 318 tests + fixtures
docs/                # SPEC.md design document
```

## DB Schema

### `synergy.db` (built by importer)

| Table | Purpose |
|---|---|
| cards | Scryfall metadata (~36k) |
| card_ports | Parsed Forge abilities: triggers, effects, costs, keywords, scales_with |
| card_svars | Forge SVars (granted abilities, formulas) |
| synergy_edges | Precomputed pairwise edges |
| graph_cache | Precomputed PageRank / hub scores |
| causal_neighbours | Adjacency list for Jaccard overlap |

### `tags.db` (external data)

| Table | Purpose |
|---|---|
| cards | Scryfall metadata |
| edhrec_card_synergy | EDHREC synergy for 2,761 commanders (87% coverage) |
| forge_abilities | Raw Forge ability data + SubAbility chain expansions |
| forge_deck_tags | Forge AI: has/hints/needs tags |
| forge_name_map | Forge name -> oracle_id |

## Key Design Decisions

- **100% Forge-native**: All synergy signals derived from Forge game engine DSL — no oracle text, no embeddings, no neural networks.
- **Deterministic**: No model training. Same inputs always produce same outputs.
- **Day-1 new cards**: Works for any card with a Forge DSL definition, including unreleased sets.
- **Fully explainable**: Every score breaks down into named buckets with per-bucket explanations.
- **Cards keyed by `oracle_id`**: Scryfall UUID deduplicates across reprints.

## License

MIT
