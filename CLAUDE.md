# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

MTG Synergy Graph — a tool for analyzing Magic: The Gathering EDH/Commander deck synergies. Supports multiple decks via `decks/` config modules (currently: Kyler GW Humans/counters, Krenko mono-R Goblins/combo, Y'Shtola WUB spellslinger/drain). The system uses a unified SQLite tag database built from OpenAI-tagged cards, enriched with parsed abilities, Commander Spellbook ground-truth combos, and strategy detection.

1. **OpenAI tagger** — uses gpt-4.1-mini to produce structured JSON tags (role, provides, wants) for 34k cards
2. **SQLite tag database** — `data/tags.db` stores tagged cards with provides/wants/abilities/strategies
3. **Ability parser** — deterministic oracle text parser extracts structured abilities (triggered, activated, static, replacement, keyword, mana) with effect/trigger tagging
4. **Commander Spellbook** — 82k combos cached locally for ground-truth infinite combo validation
5. **Strategy detector** — rule-based + oracle text + EDHREC mapping assigns strategies to cards
6. **Synergy graph** — builds edges from provides→wants relationships, 3-tier combo detection (confirmed/likely/synergy), strategy-weighted recommendations

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
python3 tag_db.py backfill                                 # backfill Scryfall metadata (oracle text, type line)
python3 tag_db.py stats                                    # show DB stats
python3 tag_db.py query --provides token-generation        # find cards providing a tag
python3 tag_db.py query --wants creature-etb               # find cards wanting a tag
python3 tag_db.py fix-tribal                               # remove false positive tribal wants tags
python3 tag_db.py fix-tribal --dry-run                     # preview tribal fixes
python3 tag_db.py rebuild-registry                         # rebuild tag vocabulary from all DB cards

# Parse oracle text into structured abilities
python3 ability_parser.py                                  # parse all cards in DB
python3 ability_parser.py --card "Kyler, Sigardian Emissary"  # inspect single card

# Fetch Commander Spellbook combos (one-time, ~8 min)
python3 fetch_spellbook.py                                 # fetch all ~82k combos
python3 fetch_spellbook.py --import-only                   # import from cached file
python3 fetch_spellbook.py --stats                         # show combo statistics

# Strategy detection
python3 strategy_detector.py --commander "Kyler, Sigardian Emissary"  # detect strategies for a commander
python3 strategy_detector.py --populate                    # populate strategies for all cards
python3 strategy_detector.py --stats                       # show strategy distribution

# Build synergy graph (reads from DB by default)
python3 synergy_graph.py --deck kyler                          # build + print top synergies
python3 synergy_graph.py --deck kyler --card "Hardened Scales"  # show synergies for one card
python3 synergy_graph.py --deck kyler --validate                # compare vs hand-curated pairs
python3 synergy_graph.py --deck krenko --deck-view              # synergy network + strategy analysis
python3 synergy_graph.py --deck krenko --recommend              # strategy-weighted recommendations
python3 synergy_graph.py --deck krenko --combos                 # 3-tier combo detection (Spellbook-validated)
python3 synergy_graph.py --deck krenko --deck-view --recommend  # both at once
python3 synergy_graph.py --deck krenko --export                 # export graph as JSON
python3 synergy_graph.py --deck kyler --visualize               # interactive HTML graph
python3 synergy_graph.py --deck kyler --swaps                   # suggest card swaps

# Strategy override
python3 synergy_graph.py --deck kyler --recommend --strategies humans,counters
python3 synergy_graph.py --deck kyler --recommend --exclude-strategies blink

# New-set update workflow
python3 update_cards.py --check                    # dry run: show new/errata'd cards
python3 update_cards.py --update                   # full pipeline: download, diff, tag, import

# Full enrichment pipeline (run after importing new tags)
python3 tag_db.py fix-tribal
python3 tag_db.py rebuild-registry
python3 ability_parser.py
python3 strategy_detector.py --populate

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

# Tests
python3 -m pytest tests/ -v                        # run all 48 tests
```

## Architecture

### Enrichment Pipeline

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
                    tag_db.py backfill → enriches with Scryfall metadata
                                        ↓
                    tag_db.py fix-tribal → removes false positive tribal tags
                                        ↓
                    tag_db.py rebuild-registry → synergy_tag_registry.json v4.0
                                        ↓
                    ability_parser.py → abilities table (structured oracle text)
                                        ↓
                    fetch_spellbook.py → spellbook_combos table (82k ground-truth combos)
                                        ↓
                    strategy_detector.py → card_strategies table (28k strategy assignments)
                                        ↓
                              synergy_graph.py --deck <name>
                              (provides→wants edges + 3-tier combos + strategies)
                                        ↓
                              deck-view / recommend / combos / swaps / visualize
```

### Tag Schema (3-field)

Each card is tagged with:
- **role**: ramp, draw, removal, protection, enabler, threat, utility, land
- **provides**: what the card gives to the deck (e.g. `card-draw`, `token-generation`, `sacrifice-outlet`)
- **wants**: what conditions make the card better (e.g. `creature-death`, `wide-board`, `spell-cast`)

Provides→wants edges form the synergy graph. 2-card cycles (A provides what B wants AND B provides what A wants) are potential infinite combos.

### Ability Schema (parsed from oracle text)

Each card's oracle text is parsed into structured abilities:
- **ability_type**: triggered, activated, static, replacement, keyword, mana
- **trigger_condition** + **trigger_tags**: what triggers the ability (e.g. "creature enters" → `creature-etb`)
- **effect** + **effect_tags**: what the ability does (e.g. "create token" → `token-generation`)
- **cost**: activation cost for activated/mana abilities
- Used for trigger chain detection in combo analysis

### Combo Detection (3-tier)

| Tier | Label | Detection |
|------|-------|-----------|
| Confirmed Infinite | `infinite-confirmed` | All combo cards in deck match a Commander Spellbook entry |
| Likely Combo | `combo-likely` | Provides→wants cycle + circular trigger chain (from abilities table) |
| Synergy | `synergy` | Provides→wants cycle without trigger chain or Spellbook match |

### Strategy Detection

Strategies are auto-detected from three sources:
1. **Provides tags** → STRATEGY_RULES (35 rules, confidence 0.5-1.0)
2. **Wants tags** → WANTS_STRATEGY_RULES (11 rules, confidence 0.5-0.7)
3. **Oracle text** → creature type references in tribal-relevant context (confidence 0.8)
4. **Deck composition** → `_detect_deck_types` feeds tribal strategies from creature type distribution
5. **EDHREC themes** → `data/edhrec_theme_cards.json` synergy scores (confidence = synergy)

User can override with `--strategies humans,counters` or `--exclude-strategies blink`.

### Key Components

- **`data/tags.db`** — SQLite database with tagged cards (cards, provides, wants, abilities, card_strategies, spellbook_combos, spellbook_combo_cards, scryfall_tags)
- **`data/all_tags_gpt41mini.json`** — all OpenAI-tagged cards (canonical source, gitignored)
- **`data/commander_spellbook.json`** — cached Commander Spellbook combos (82k, gitignored)
- **`golden_cards.json`** — 500 cards with verified expected tags. Used for evaluation only, excluded from training.
- **`synergy_tag_registry.json`** — canonical vocabulary v4.0: rebuilt from all DB cards (min 3 occurrences)
- **`ability_parser.py`** — 3-phase oracle text parser: keyword extraction → pattern matching → effect/trigger tagging. 88% of cards get high-confidence parses.
- **`strategy_detector.py`** — rule-based strategy detection with STRATEGY_RULES, WANTS_STRATEGY_RULES, oracle text tribal scanning, and EDHREC enrichment
- **`fetch_spellbook.py`** — Commander Spellbook API fetcher with pagination, caching, and DB import
- **`synergy_graph.py`** — builds composite-scored edges from 4 signal types: provides→wants (with semantic bridges + IDF weighting), peer-enabler (shared provides), shared-wants, embedding similarity. Features: `--deck-view` (with strategy analysis), `--recommend` (strategy-weighted + combo completion bonus + mana cost penalty), `--combos` (3-tier Spellbook-validated), `--swaps`, `--visualize`
- **`benchmark_models.py`** — benchmarks Ollama models on golden set. Scores: provides recall, wants recall, role accuracy.
- **`auto_finetune.py`** — iterative fine-tuning loop: train → export GGUF → Ollama → eval → save.

### Adding New Cards (new set release)

1. `python3 download_cards.py` — refresh Scryfall data
2. Prepare candidates for new cards only
3. `python3 batch_tagger.py --candidates <new> --provider openai --model gpt-4.1-mini` (~$0.01-0.05/set)
4. `python3 tag_db.py import <new_tags>` — merge into DB
5. `python3 tag_db.py backfill` — enrich with Scryfall metadata
6. `python3 tag_db.py fix-tribal` — clean tribal tags
7. `python3 ability_parser.py` — parse abilities for new cards
8. `python3 strategy_detector.py --populate` — update strategies
9. All decks automatically benefit

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
- Tests: 48 tests in `tests/` covering parser, strategy detector, combo detection, anti-synergy, schema, tribal cleanup, registry rebuild, integration
