# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

MTG Synergy Graph — a tool for analyzing Magic: The Gathering EDH/Commander deck synergies. Supports multiple decks via `decks/` config modules (currently: Kyler GW Humans/counters, Krenko mono-R Goblins/combo). The system has two data sources feeding a synergy graph:

1. **Scryfall tagger integration** — fetches community-curated function tags from tagger.scryfall.com via GraphQL API (canonical synergy_tags source)
2. **LLM-based tagger** — uses Claude/GPT/Ollama APIs to produce structured JSON tags (role, provides, wants) for graph relationships

## Common Commands

```bash
# Download Scryfall bulk data (required first step, ~150MB)
python3 download_cards.py

# Filter commander-relevant cards for LLM tagging (~500 cards)
python3 card_filter.py --deck kyler
python3 card_filter.py --deck krenko

# Fetch Scryfall tagger function tags for candidates (canonical synergy_tags)
python3 scryfall_tagger.py                        # fetch all 500 candidates
python3 scryfall_tagger.py --dry-run              # show plan without fetching

# Batch-tag filtered cards via LLM API (role/provides/wants only)
python3 batch_tagger.py --deck kyler --dry-run                              # cost estimate
python3 batch_tagger.py --deck kyler --provider ollama --model phi4:14b     # local model
python3 batch_tagger.py --deck krenko --provider ollama --model phi4:14b    # tag Krenko cards

# RAG pipeline: download rules, chunk, embed, index
python3 rules_fetcher.py                          # download + chunk MTG rules
python3 rules_index.py                            # embed chunks via Ollama

# Merge Scryfall + LLM tags (auto-normalizes provides/wants)
python3 merge_tags.py --deck kyler
python3 merge_tags.py --deck krenko --llm data/krenko_tags_phi4.json

# Build synergy graph from merged profiles
python3 synergy_graph.py --deck kyler                          # build + print top synergies
python3 synergy_graph.py --deck kyler --card "Hardened Scales"  # show synergies for one card
python3 synergy_graph.py --deck kyler --validate                # compare vs hand-curated pairs
python3 synergy_graph.py --deck krenko --deck-view              # synergy network within the deck
python3 synergy_graph.py --deck krenko --recommend              # recommend cards to add
python3 synergy_graph.py --deck krenko --deck-view --recommend  # both at once
python3 synergy_graph.py --deck krenko --export                 # export graph as JSON
python3 synergy_graph.py --deck kyler --visualize               # interactive HTML graph
python3 synergy_graph.py --deck kyler --combos                  # detect 3-4 card combos
python3 synergy_graph.py --deck kyler --swaps                   # suggest card swaps

# Normalize provides/wants vocabulary (standalone)
python3 normalize_tags.py --deck kyler --stats
python3 normalize_tags.py --unmapped --dry-run     # show unmapped tags

# Regression tests against golden dataset
python3 regression_test.py --mode scryfall        # validate Scryfall tags vs golden
python3 regression_test.py --mode static          # validate card_tags.json vs golden
python3 regression_test.py --mode static --tags-file data/kyler_tags_phi4.json
python3 regression_test.py --mode live            # call API + validate

# Compare local LLM models (Ollama) against GPT-4o
python3 llm_compare.py --models qwen3:8b gemma3:12b

# Tag registry management
python3 tags.py list                              # all tags by usage count
python3 tags.py sync                              # rebuild from card_tags.json
```

## Architecture

### Two-Source Tag Architecture

```
Scryfall Tagger (community tags)     LLM (Claude/GPT/Ollama)
         ↓                                    ↓
  scryfall_tagger.py                   batch_tagger.py
         ↓                                    ↓
  synergy_tags (canonical)          role, provides, wants
         ↓                                    ↓
         └──────────────┬─────────────────────┘
                        ↓
                  merge_tags.py
                        ↓
                normalize_tags.py (controlled vocabulary)
                        ↓
              merged card profile
                        ↓
               synergy_graph.py
                        ↓
                 synergy graph edges
```

- **synergy_tags** come from Scryfall's community tagger — standardized vocabulary, crowd-validated
- **role/provides/wants** come from LLM — semantic relationships that Scryfall doesn't capture
- `tag_registry.json` is the single source of truth for canonical vocabulary. Tags have `kind` (provides/wants/both/synergy), aliases, definitions, and graph edges (inherits/amplifies/requires)
- `normalize_tags.py` loads PROVIDES_MAP and WANTS_MAP from the registry, maps freeform LLM tags to canonical terms, infers missing wants from card properties
- `synergy_graph.py` builds composite-scored edges from 4 signal types: provides→wants (with semantic bridges + IDF weighting), shared Scryfall tags, peer-enabler (shared provides), shared-wants. Multi-signal edges score higher (1.1-1.3x bonus). Validates at 100% Kyler (25/25) and 96% Krenko (24/25). Features: `--deck-view`, `--recommend`, `--combos`, `--swaps`, `--visualize`
- `golden_cards.json` validates both sources: synergy_tags against Scryfall vocab, role/provides/wants against LLM output

### Data Flow

```
Scryfall API → download_cards.py → data/oracle_cards.json
                                        ↓
                              card_db.py (loads on import, builds CARD_DB + NAME_INDEX)
                                        ↓
                              card_filter.py → data/<deck>_candidates.json
                                   ↓                    ↓
                        scryfall_tagger.py        batch_tagger.py
                                   ↓                    ↓
                   scryfall_function_tags.json    <deck>_tags.json
                                   ↓                    ↓
                                merge_tags.py + normalize_tags.py
                                        ↓
                              <deck>_merged.json
                                        ↓
                              synergy_graph.py
                                        ↓
               deck-view / recommend / combos / swaps / visualize
```

### RAG Pipeline (rules_fetcher → rules_index → rules_retriever)

- `rules_fetcher.py`: Downloads MTG Comprehensive Rules from WotC + Scryfall rulings. Parses into section-aware chunks (~1600 chunks)
- `rules_index.py`: Embeds chunks via Ollama nomic-embed-text (768-dim). Brute-force cosine similarity over JSON
- `rules_retriever.py`: Three retrieval strategies — semantic search, keyword→section lookup, oracle_id match for card-specific rulings

### Key Data Files

- `golden_cards.json`: 14 EDH staples with manually verified expected tags (v0.3 — Scryfall-aligned vocabulary)
- `corrections.json`: Rules injected into LLM prompts to fix known tagging errors
- `data/scryfall_function_tags.json`: Community function tags for 500 candidates (from tagger.scryfall.com GraphQL)
- `data/kyler_candidates.json`: 500 filtered candidate cards for Kyler deck
- `tag_registry.json`: Living registry of synergy tags with definitions, aliases, inheritance, synergy/dependency edges

## Key Conventions

- Cards are keyed by `oracle_id` (Scryfall UUID) for dedup across reprints
- `data/oracle_cards.json` is gitignored (~150MB); must run `download_cards.py` first
- API calls use `urllib.request` (no `requests` dependency) except `llm_tagger_test.py`
- Scryfall tagger tags use spaces (e.g., `mana rock`, `spot removal`); our LLM tags use kebab-case (e.g., `mana-acceleration`)
- The tag registry has three edge types: `inherits` (is-a), `amplifies` (synergy), `requires` (dependency)
- Deck configs live in `decks/` (commander, EDHREC slug, color identity, decklist, synergy pairs, supplement filters)
- All pipeline scripts accept `--deck <name>` to select which deck to process
- RTX 5080 (16GB VRAM): phi4:14b is optimal local model for tagging; 30B+ models cause CPU spillover
