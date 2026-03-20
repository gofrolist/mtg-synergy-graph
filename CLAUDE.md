# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

MTG Synergy Graph — a tool for analyzing Magic: The Gathering EDH/Commander deck synergies. Supports multiple decks via `decks/` config modules (currently: Kyler GW Humans/counters, Krenko mono-R Goblins/combo, Y'Shtola WUB spellslinger/drain). The system uses a unified SQLite tag database built from OpenAI-tagged cards.

1. **OpenAI tagger** — uses gpt-4.1-mini to produce structured JSON tags (role, provides, wants) for 34k cards
2. **SQLite tag database** — `data/tags.db` stores 34k tagged cards, queried per-deck at runtime
3. **Synergy graph** — builds edges from provides→wants relationships, 2/3/4-card combo detection

## Common Commands

```bash
# Download Scryfall bulk data (required first step, ~150MB)
python3 download_cards.py

# Extract top N cards by EDHREC rank for tagging
python3 top_cards_filter.py --top 20000

# Batch-tag cards via OpenAI API (role/provides/wants)
python3 batch_tagger.py --candidates data/remaining_candidates.json --provider openai --model gpt-4.1-mini --dry-run
python3 batch_tagger.py --candidates data/remaining_candidates.json --provider openai --model gpt-4.1-mini
python3 batch_tagger.py --deck kyler --provider ollama --model mtg-tagger    # local fine-tuned model

# Import tags into SQLite DB (the single source of truth for card tags)
python3 tag_db.py import data/all_tags_gpt41mini.json    # import/update from tags JSON
python3 tag_db.py stats                                   # show DB stats
python3 tag_db.py query --provides token-generation       # find cards providing a tag
python3 tag_db.py query --wants creature-etb              # find cards wanting a tag

# Build synergy graph (reads from DB by default)
python3 synergy_graph.py --deck kyler                          # build + print top synergies
python3 synergy_graph.py --deck kyler --card "Hardened Scales"  # show synergies for one card
python3 synergy_graph.py --deck kyler --validate                # compare vs hand-curated pairs
python3 synergy_graph.py --deck krenko --deck-view              # synergy network within the deck
python3 synergy_graph.py --deck krenko --recommend              # recommend cards from DB
python3 synergy_graph.py --deck krenko --deck-view --recommend  # both at once
python3 synergy_graph.py --deck krenko --export                 # export graph as JSON
python3 synergy_graph.py --deck kyler --visualize               # interactive HTML graph
python3 synergy_graph.py --deck kyler --combos                  # detect 2/3/4-card combos
python3 synergy_graph.py --deck kyler --swaps                   # suggest card swaps

# New-set update workflow
python3 update_cards.py --check                    # dry run: show new/errata'd cards
python3 update_cards.py --update                   # full pipeline: download, diff, tag, import

# Prepare training data for local model fine-tuning
python3 prepare_training.py --stats                # generate train/val split from OpenAI tags

# Benchmark local models against golden set
python3 benchmark_models.py                        # test all default models on 100 golden cards
python3 benchmark_models.py --models mtg-tagger qwen3:4b  # specific models

# Automated fine-tuning experiments
python3 auto_finetune.py --dry-run                 # show experiment plan
python3 auto_finetune.py                           # run iterative training loop
python3 auto_finetune.py --eval-only mtg-tagger    # evaluate existing model

# RAG pipeline: download rules, chunk, embed, index
python3 rules_fetcher.py                           # download + chunk MTG rules
python3 rules_index.py                             # embed chunks via Ollama

# Card embeddings (768-dim vectors via gte-modernbert-base)
python3 card_embeddings.py                         # embed all cards in tags.db
python3 card_embeddings.py --query "Sol Ring"      # find similar cards by embedding
```

## Architecture

### Tag Pipeline

```
Scryfall API → download_cards.py → data/oracle_cards.json (36k cards)
                                        ↓
                              top_cards_filter.py → candidates JSON
                                        ↓
                              batch_tagger.py (gpt-4.1-mini via OpenAI API)
                                        ↓
                              data/all_tags_gpt41mini.json (34k tagged cards)
                                        ↓
                    tag_db.py import → data/tags.db (SQLite, single source of truth)
                                        ↓
                              synergy_graph.py --deck <name>
                              (provides→wants edges + combo detection)
                                        ↓
                              deck-view / recommend / combos / swaps / visualize
```

### Tag Schema (3-field)

Each card is tagged with:
- **role**: ramp, draw, removal, protection, enabler, threat, utility, land
- **provides**: what the card gives to the deck (e.g. `card-draw`, `token-generation`, `sacrifice-outlet`)
- **wants**: what conditions make the card better (e.g. `creature-death`, `wide-board`, `spell-cast`)

Provides→wants edges form the synergy graph. 2-card cycles (A provides what B wants AND B provides what A wants) are potential infinite combos.

### Key Components

- **`data/tags.db`** — SQLite database with 34k tagged cards (provides, wants, scryfall_tags)
- **`data/all_tags_gpt41mini.json`** — all OpenAI-tagged cards (canonical source, gitignored)
- **`golden_cards.json`** — 500 cards with verified expected tags (function, themes, provides, wants). Used for evaluation only, excluded from training.
- **`synergy_tag_registry.json`** — canonical vocabulary: 240 provides + 213 wants tags (v3.1, built from 10k gpt-4.1-mini tags, min 3 occurrences)
- **`synergy_graph.py`** — builds composite-scored edges from 3 signal types: provides→wants (with semantic bridges + IDF weighting), peer-enabler (shared provides), embedding similarity. Features: `--deck-view`, `--recommend`, `--combos` (2/3/4-card), `--swaps`, `--visualize`
- **`benchmark_models.py`** — benchmarks Ollama models on golden set. Scores: provides recall, wants recall, role accuracy. Saves results incrementally.
- **`auto_finetune.py`** — iterative fine-tuning loop: train → export GGUF → Ollama → eval → save. Supports registry-filtered data, hyperparameter grid, crash-safe resume.

### Adding New Cards (new set release)

1. `python3 download_cards.py` — refresh Scryfall data
2. Prepare candidates for new cards only
3. `python3 batch_tagger.py --candidates <new> --provider openai --model gpt-4.1-mini` (~$0.01-0.05/set)
4. `python3 tag_db.py import <new_tags>` — merge into DB
5. All decks automatically benefit

### RAG Pipeline (rules_fetcher → rules_index → rules_retriever)

- `rules_fetcher.py`: Downloads MTG Comprehensive Rules + Scryfall rulings (~1600 chunks)
- `rules_index.py`: Embeds chunks via Ollama nomic-embed-text (768-dim)
- `rules_retriever.py`: Semantic search, keyword→section lookup, card-specific rulings
- Currently unused by tagging pipeline (gpt-4.1-mini knows MTG rules). Reserved for edge cases.

## Key Conventions

- Cards are keyed by `oracle_id` (Scryfall UUID) for dedup across reprints
- `data/oracle_cards.json` is gitignored (~150MB); must run `download_cards.py` first
- API calls use `urllib.request` (no `requests` dependency)
- Tags use kebab-case (e.g., `mana-acceleration`, `creature-death`)
- Deck configs live in `decks/` (commander, EDHREC slug, color identity, decklist, synergy pairs)
- Fine-tuning uses `.venv` with unsloth + torch (not system Python)
- `synergy_graph.py` reads from SQLite DB by default; use `--input <file>` to override with JSON
- Best local model: `mtg-tagger` (Qwen3-4B fine-tuned, 65.3% composite on golden set)
