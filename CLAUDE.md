# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

MTG Synergy Graph — a tool for analyzing Magic: The Gathering EDH/Commander deck synergies. Supports multiple decks via `decks/` config modules (currently: Kyler GW Humans/counters, Krenko mono-R Goblins/combo, Y'Shtola WUB spellslinger/drain). The system uses a unified SQLite tag database built from LLM-tagged cards.

1. **LLM-based tagger** — uses Claude/GPT/Ollama APIs to produce structured JSON tags (role, provides, wants)
2. **SQLite tag database** — `data/tags.db` stores 10k+ tagged cards, queried per-deck at runtime
3. **Synergy graph** — builds edges from provides→wants relationships, semantic bridges, shared tags

## Common Commands

```bash
# Download Scryfall bulk data (required first step, ~150MB)
python3 download_cards.py

# Extract top N cards by EDHREC rank for tagging
python3 top_cards_filter.py --top 10000

# Batch-tag cards via LLM API (role/provides/wants)
python3 batch_tagger.py --candidates data/top10000_candidates.json --provider ollama --model phi4:14b
python3 batch_tagger.py --deck kyler --provider ollama --model phi4:14b    # deck-specific candidates
python3 batch_tagger.py --candidates data/top10000_candidates.json --dry-run  # cost estimate

# Import tags into SQLite DB (the single source of truth for card tags)
python3 tag_db.py import data/top10000_tags.json     # import/update from tags JSON
python3 tag_db.py stats                               # show DB stats
python3 tag_db.py query --provides token-generation   # find cards providing a tag
python3 tag_db.py query --wants creature-etb          # find cards wanting a tag

# Build synergy graph (reads from DB by default)
python3 synergy_graph.py --deck kyler                          # build + print top synergies
python3 synergy_graph.py --deck kyler --card "Hardened Scales"  # show synergies for one card
python3 synergy_graph.py --deck kyler --validate                # compare vs hand-curated pairs
python3 synergy_graph.py --deck krenko --deck-view              # synergy network within the deck
python3 synergy_graph.py --deck krenko --recommend              # recommend cards from DB
python3 synergy_graph.py --deck krenko --deck-view --recommend  # both at once
python3 synergy_graph.py --deck krenko --export                 # export graph as JSON
python3 synergy_graph.py --deck kyler --visualize               # interactive HTML graph
python3 synergy_graph.py --deck kyler --combos                  # detect 3-4 card combos
python3 synergy_graph.py --deck kyler --swaps                   # suggest card swaps
python3 synergy_graph.py --deck kyler --input data/custom.json  # override: use JSON instead of DB

# New-set update workflow
python3 update_cards.py --check                    # dry run: show new/errata'd cards
python3 update_cards.py --update                   # full pipeline: download, diff, tag, import
python3 update_cards.py --update --top 15000       # expand DB to 15k cards
python3 update_cards.py --errata-only              # show oracle text changes
python3 update_cards.py --tag-only                 # resume tagging after crash

# Fetch Scryfall tagger function tags (optional, enriches synergy_tags)
python3 scryfall_tagger.py                        # fetch all 500 candidates
python3 scryfall_tagger.py --dry-run              # show plan without fetching

# RAG pipeline: download rules, chunk, embed, index
python3 rules_fetcher.py                          # download + chunk MTG rules
python3 rules_index.py                            # embed chunks via Ollama

# Normalize provides/wants vocabulary (standalone)
python3 normalize_tags.py --stats
python3 normalize_tags.py --unmapped --dry-run     # show unmapped tags

# Card embeddings (768-dim vectors via gte-modernbert-base)
python3 card_embeddings.py                    # embed all cards in tags.db → data/embeddings.npy
python3 card_embeddings.py --query "Sol Ring"  # find similar cards by embedding
python3 card_embeddings.py --stats             # show embedding stats

# Regression tests against golden dataset
python3 regression_test.py --mode scryfall        # validate Scryfall tags vs golden
python3 regression_test.py --mode static          # validate card_tags.json vs golden
python3 regression_test.py --mode live            # call API + validate

# Automated fine-tuning experiments (autoresearch-style)
python3 auto_finetune.py --dry-run              # show experiment plan
python3 auto_finetune.py                         # run all experiments overnight
python3 auto_finetune.py --experiments 3         # run first 3 only
python3 auto_finetune.py --eval-only mtg-tagger  # evaluate existing model

# Compare local LLM models (Ollama) against GPT-4o
python3 llm_compare.py --models qwen3:8b gemma3:12b

# Tag registry management
python3 tags.py list                              # all tags by usage count
python3 tags.py sync                              # rebuild from card_tags.json
```

## Architecture

### Unified Tag Database

```
Scryfall API → download_cards.py → data/oracle_cards.json
                                        ↓
                              card_db.py (CARD_DB + NAME_INDEX)
                                        ↓
                    top_cards_filter.py → data/top10000_candidates.json
                                        ↓
                              batch_tagger.py (Ollama phi4:14b)
                                        ↓
                              data/top10000_tags.json
                                        ↓
                    tag_db.py import → data/tags.db (SQLite, single source of truth)
                                        ↓                     ↓
                              card_embeddings.py        synergy_graph.py --deck <name>
                              (gte-modernbert-base)     (5 signal types + hybrid recommend)
                                        ↓                     ↓
                              data/embeddings.npy → deck-view / recommend / combos / swaps / visualize
```

- **`data/tags.db`** is the single source of truth for card tags (10k+ cards)
- **`data/top10000_tags.json`** is the canonical tags file — grow this to add more cards
- `tag_registry.json` defines canonical vocabulary: `kind` (provides/wants/both/synergy), aliases, definitions, graph edges (inherits/amplifies/requires)
- `normalize_tags.py` loads PROVIDES_MAP and WANTS_MAP from the registry, maps freeform LLM tags to canonical terms
- `synergy_graph.py` loads deck cards from DB, builds composite-scored edges from 4 active signal types: provides→wants (with semantic bridges + IDF weighting), peer-enabler (shared provides), shared-wants, embedding similarity (cosine sim from gte-modernbert-base vectors). Shared-tag signal is dormant (reserved for future LLM-generated synergy_tags). Scryfall community tags are kept separate as a validation signal only, not used for graph edges. Fan-out caps scale with card count to prevent O(n²) explosion. Features: `--deck-view`, `--recommend` (hybrid: tag + embedding candidates), `--combos`, `--swaps`, `--visualize`
- `card_embeddings.py` serializes cards as prettified JSON and embeds with `gte-modernbert-base` (768-dim, L2-normalized). Embeddings stored in `data/embeddings.npy` + `data/embeddings_index.json`. Used as 5th signal in synergy graph and for hybrid recommendation candidate generation
- Validates at 96% Kyler (24/25), 96% Krenko (24/25), 80% Y'Shtola (20/25)

### Adding New Cards to the DB

1. Extract candidates: `python3 top_cards_filter.py --top 15000` (or add specific cards)
2. Tag them: `python3 batch_tagger.py --candidates <file> --provider ollama --model phi4:14b`
3. Import to DB: `python3 tag_db.py import data/top10000_tags.json`
4. All decks automatically benefit from the expanded card pool

### RAG Pipeline (rules_fetcher → rules_index → rules_retriever)

- `rules_fetcher.py`: Downloads MTG Comprehensive Rules from WotC + Scryfall rulings. Parses into section-aware chunks (~1600 chunks)
- `rules_index.py`: Embeds chunks via Ollama nomic-embed-text (768-dim). Brute-force cosine similarity over JSON
- `rules_retriever.py`: Three retrieval strategies — semantic search, keyword→section lookup, oracle_id match for card-specific rulings

### Key Data Files

- `data/embeddings.npy`: Card embeddings (N×768 float32 array, ~30MB for 10k cards)
- `data/embeddings_index.json`: Oracle ID index mapping rows to card identities
- `data/tags.db`: SQLite database with 10k+ tagged cards (provides, wants, synergy_tags)
- `data/top10000_tags.json`: LLM-generated tags for top 10k EDHREC cards (canonical, grow this file)
- `data/top10000_candidates.json`: Card data extracted from Scryfall for tagging
- `golden_cards.json`: 14 EDH staples with manually verified expected tags
- `corrections.json`: Rules injected into LLM prompts to fix known tagging errors
- `data/scryfall_function_tags.json`: Community function tags from tagger.scryfall.com GraphQL
- `tag_registry.json`: Living registry of synergy tags with definitions, aliases, inheritance

## Key Conventions

- Cards are keyed by `oracle_id` (Scryfall UUID) for dedup across reprints
- `data/oracle_cards.json` is gitignored (~150MB); must run `download_cards.py` first
- API calls use `urllib.request` (no `requests` dependency)
- Scryfall tagger tags use spaces (e.g., `mana rock`, `spot removal`); our LLM tags use kebab-case (e.g., `mana-acceleration`)
- The tag registry has three edge types: `inherits` (is-a), `amplifies` (synergy), `requires` (dependency)
- Deck configs live in `decks/` (commander, EDHREC slug, color identity, decklist, synergy pairs, supplement filters)
- All pipeline scripts accept `--deck <name>` to select which deck to process
- RTX 5080 (16GB VRAM): phi4:14b is optimal local model for tagging; 30B+ models cause CPU spillover
- `synergy_graph.py` reads from SQLite DB by default; use `--input <file>` to override with JSON
