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
    port_match, cost_synergy, catch_all, scaling, deck_hints,
    chain_match, lord, amplifier, etb_self, graph_metrics,
    strategic, resource_density, effect_resonance,
    replacement_resonance, sacrifice_synergy, staple, replacement
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

# Tests
uv run pytest tests/ -q
```

## Project Structure

```
src/mtg_synergy_graph/
  engine.py          # SynergyEngine, score_all_candidates()
  importer.py        # Forge DSL parser, DB importer
  penalties.py       # Hard filters and multipliers
  scoring.py         # Bucket scorers orchestration
  graph_metrics.py   # Causal graph: PageRank, hub scores, Jaccard
  graph_cache.py     # Precomputed graph cache
  validate.py        # EDHREC comparison utilities
  schema.sql         # SQLite schema
scripts/             # CLI tools (import, recommend, compare)
tests/               # 322 tests
```

## Key Design Decisions

- **100% Forge-native**: All synergy signals derived from Forge game engine DSL — no oracle text, no embeddings, no neural networks.
- **Deterministic**: No model training. Same inputs always produce same outputs.
- **Day-1 new cards**: Works for any card with a Forge DSL definition, including unreleased sets.
- **Fully explainable**: Every score breaks down into named buckets with per-bucket explanations.
- **Cards keyed by `oracle_id`**: Scryfall UUID deduplicates across reprints.

## License

MIT
