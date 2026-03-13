# MTG Synergy Graph

Analyze Magic: The Gathering EDH/Commander deck synergies using LLM-generated card tags and a graph-based scoring engine.

The system tags cards with semantic roles (`provides`/`wants`) via local LLMs, stores them in a SQLite database, and builds synergy graphs to find combos, recommend cards, suggest swaps, and visualize deck relationships.

## Quick Start

```bash
# 1. Download Scryfall bulk data (~150MB, required)
python3 download_cards.py

# 2. Extract top cards by EDHREC popularity
python3 top_cards_filter.py --top 10000

# 3. Tag cards with a local LLM (requires Ollama running)
python3 batch_tagger.py --candidates data/top10000_candidates.json \
    --provider ollama --model phi4:14b

# 4. Import tags into SQLite database
python3 tag_db.py import data/top10000_tags.json

# 5. Analyze a deck
python3 synergy_graph.py --deck kyler --validate
python3 synergy_graph.py --deck kyler --deck-view
python3 synergy_graph.py --deck kyler --recommend
```

## Requirements

- Python 3.10+
- [Ollama](https://ollama.ai) with `phi4:14b` model (recommended for local tagging)
- No pip dependencies required (stdlib only)

```bash
# Install Ollama model
ollama pull phi4:14b
```

## Architecture

```
Scryfall API
    |
    v
download_cards.py --> data/oracle_cards.json (bulk card data)
    |
    v
top_cards_filter.py --> data/top10000_candidates.json (top EDH cards)
    |
    v
batch_tagger.py --> data/top10000_tags.json (LLM-generated tags)
    |
    v
tag_db.py import --> data/tags.db (SQLite, single source of truth)
    |
    v
synergy_graph.py --deck <name>
    |
    +---> --deck-view     (synergy network within the deck)
    +---> --recommend     (recommend cards from the 10k pool)
    +---> --combos        (detect 3-4 card combos)
    +---> --swaps         (suggest card swaps)
    +---> --visualize     (interactive HTML graph)
    +---> --validate      (test against curated synergy pairs)
```

## Commands Reference

### Data Pipeline

#### `download_cards.py` -- Download Scryfall Data

Downloads the complete Scryfall oracle card database (~150MB).

```bash
python3 download_cards.py
```

#### `top_cards_filter.py` -- Extract Top Cards

Extracts the top N cards by EDHREC rank from Scryfall data. Produces a candidates file for batch tagging.

```bash
python3 top_cards_filter.py --top 10000
python3 top_cards_filter.py --top 5000 --output data/custom_candidates.json
```

#### `batch_tagger.py` -- LLM Card Tagger

Tags cards with semantic roles using LLM APIs. Supports Ollama (local), OpenAI, and Anthropic. Crash-safe with resume support.

```bash
# Tag from candidates file (preferred)
python3 batch_tagger.py --candidates data/top10000_candidates.json \
    --provider ollama --model phi4:14b

# Tag deck-specific candidates
python3 batch_tagger.py --deck kyler --provider ollama --model phi4:14b

# Cost estimate without calling API
python3 batch_tagger.py --candidates data/top10000_candidates.json --dry-run

# Use cloud API
python3 batch_tagger.py --candidates data/top10000_candidates.json \
    --provider openai --model gpt-4o
```

Each card gets tagged with:
- **role**: enabler, threat, removal, ramp, draw, protection, utility
- **provides**: what the card offers (e.g., `token-generation`, `counter-placement`)
- **wants**: what board state benefits the card (e.g., `creature-etb`, `sacrifice-events`)

#### `tag_db.py` -- SQLite Tag Database

Imports tags into SQLite for fast querying. This is the single source of truth used by `synergy_graph.py`.

```bash
# Import tags
python3 tag_db.py import data/top10000_tags.json

# Show database stats
python3 tag_db.py stats

# Query cards by tag
python3 tag_db.py query --provides token-generation
python3 tag_db.py query --wants creature-etb
```

### Synergy Analysis

#### `synergy_graph.py` -- Build and Query Synergy Graphs

The main analysis tool. Loads deck cards from the SQLite DB and builds a synergy graph based on `provides`/`wants` relationships.

```bash
# Show top synergy edges
python3 synergy_graph.py --deck kyler

# Show synergies for a specific card
python3 synergy_graph.py --deck kyler --card "Hardened Scales"

# Synergy network within the deck
python3 synergy_graph.py --deck kyler --deck-view

# Recommend cards to add from the 10k pool
python3 synergy_graph.py --deck krenko --recommend

# Detect 3- and 4-card combos
python3 synergy_graph.py --deck kyler --combos

# Suggest card swaps
python3 synergy_graph.py --deck kyler --swaps

# Interactive HTML visualization (D3.js force graph)
python3 synergy_graph.py --deck kyler --visualize

# Validate against hand-curated synergy pairs
python3 synergy_graph.py --deck kyler --validate

# Export graph as JSON
python3 synergy_graph.py --deck kyler --export

# Combine flags
python3 synergy_graph.py --deck krenko --deck-view --recommend --top 10

# Override: use a JSON file instead of DB
python3 synergy_graph.py --deck kyler --input data/custom_merged.json
```

### Tag Vocabulary

#### `tags.py` -- Tag Registry CLI

Manage the canonical tag vocabulary in `tag_registry.json`.

```bash
# List all tags by usage count
python3 tags.py list

# Search for tags
python3 tags.py search protection

# Show full details for a tag
python3 tags.py show board-protection

# Find similar/duplicate tags
python3 tags.py similar protection

# Rebuild counts from the 10k tags file
python3 tags.py sync

# Add a new tag
python3 tags.py add flash-grant "Gives flash to spells or permanents"
```

#### `normalize_tags.py` -- Tag Normalization

Maps freeform LLM tags to canonical vocabulary. Runs automatically during DB import.

```bash
# Show normalization stats
python3 normalize_tags.py --stats

# Show unmapped tags
python3 normalize_tags.py --unmapped

# Normalize a specific file
python3 normalize_tags.py --input data/top10000_tags.json --stats
```

### New Set Updates

#### `update_cards.py` -- New-Set Update Workflow

Detects new cards from set releases and oracle text changes (errata), tags them, and updates the database. Run this whenever a new MTG set is released.

```bash
# Check what changed (dry run)
python3 update_cards.py --check

# Check with verbose errata diff
python3 update_cards.py --check --verbose

# Full pipeline: download fresh data, diff, tag new cards, import
python3 update_cards.py --update

# Expand the DB to top 15k cards
python3 update_cards.py --update --top 15000

# Skip download if you already updated oracle_cards.json
python3 update_cards.py --update --skip-download

# Show only errata changes
python3 update_cards.py --errata-only

# Resume tagging after a crash
python3 update_cards.py --tag-only

# Merge an external tags file into the DB
python3 update_cards.py --merge data/custom_tags.json
```

**Typical new-set workflow:**
```bash
# 1. Download fresh Scryfall data (includes new set)
python3 download_cards.py

# 2. Check what's new
python3 update_cards.py --check -v

# 3. Run the full update
python3 update_cards.py --update --skip-download

# 4. Verify decks still work
python3 synergy_graph.py --deck kyler --validate
```

### Optional Tools

#### `scryfall_tagger.py` -- Scryfall Community Tags

Fetches community-curated function tags from tagger.scryfall.com via GraphQL.

```bash
python3 scryfall_tagger.py              # fetch tags for candidates
python3 scryfall_tagger.py --dry-run    # preview without fetching
```

#### `rules_fetcher.py` / `rules_index.py` -- RAG Pipeline

Downloads MTG rules, chunks them, and builds an embedding index for rule-aware tagging.

```bash
python3 rules_fetcher.py    # download + chunk MTG rules
python3 rules_index.py      # embed chunks via Ollama
```

#### `llm_compare.py` -- Model Comparison

Benchmark local LLM models against each other on tagging quality.

```bash
python3 llm_compare.py --models qwen3:8b gemma3:12b phi4:14b
```

#### `regression_test.py` -- Regression Tests

Validate tag quality against a golden dataset of manually verified cards.

```bash
python3 regression_test.py --mode static    # validate tags file vs golden
python3 regression_test.py --mode scryfall  # validate Scryfall tags vs golden
python3 regression_test.py --mode live      # call API + validate
```

## Adding a New Deck

1. Create a deck config in `decks/`:

```python
# decks/newdeck.py
COMMANDER = "Commander Name"
EDHREC_SLUG = "commander-name"
COLOR_IDENTITY = {"W", "U"}

DECKLIST = [
    "Card Name 1",
    "Card Name 2",
    # ... 99 cards
]

SYNERGY_PAIRS = [
    ("Card A", "Card B", "why they synergize"),
    # 20-25 pairs for validation
]

SUPPLEMENT_FILTERS = []
```

2. Register it in `decks/__init__.py`

3. Ensure deck cards are in the DB (check and tag missing ones):

```bash
# Check coverage
python3 -c "
from decks import load_deck
from tag_db import get_cards_by_names
deck = load_deck('newdeck')
cards = get_cards_by_names(deck.DECKLIST + [deck.COMMANDER])
print(f'{len(cards)}/{len(deck.DECKLIST)+1} cards in DB')
"

# Tag any missing cards, then re-import
python3 tag_db.py import data/top10000_tags.json
```

4. Run analysis:

```bash
python3 synergy_graph.py --deck newdeck --validate
python3 synergy_graph.py --deck newdeck --deck-view
python3 synergy_graph.py --deck newdeck --recommend
```

## Growing the Card Database

The 10k card database covers the most popular EDH cards. To expand:

```bash
# Extract more candidates
python3 top_cards_filter.py --top 15000

# Tag the new cards (resume-safe, skips already-tagged)
python3 batch_tagger.py --candidates data/top15000_candidates.json \
    --provider ollama --model phi4:14b

# Re-import (upserts, safe to run multiple times)
python3 tag_db.py import data/top15000_tags.json
```

## Project Structure

```
.
├── README.md
├── CLAUDE.md                  # AI assistant instructions
├── .gitignore
│
├── download_cards.py          # Scryfall bulk data downloader
├── top_cards_filter.py        # Extract top N cards by EDHREC rank
├── batch_tagger.py            # LLM batch card tagger
├── tag_db.py                  # SQLite tag database
├── synergy_graph.py           # Synergy graph engine + CLI
│
├── update_cards.py            # New-set update workflow
├── card_db.py                 # Scryfall card lookup (oracle_cards.json)
├── prompt_builder.py          # LLM prompt construction
├── normalize_tags.py          # Tag vocabulary normalization
├── tags.py                    # Tag registry CLI
│
├── scryfall_tagger.py         # Scryfall community tag fetcher
├── rules_fetcher.py           # MTG rules downloader + chunker
├── rules_index.py             # Rules embedding index builder
├── rules_retriever.py         # Rules RAG retrieval
├── llm_compare.py             # LLM model benchmarking
├── regression_test.py         # Tag quality regression tests
│
├── corrections.json           # LLM prompt corrections for known errors
├── golden_cards.json          # Golden dataset for validation
├── tag_registry.json          # Canonical tag vocabulary
│
├── data/
│   ├── top10000_tags.json     # Canonical tagged cards (tracked in git)
│   ├── tags.db                # SQLite database (gitignored, regenerable)
│   ├── oracle_cards.json      # Scryfall bulk data (gitignored, ~150MB)
│   └── ...                    # Other generated data files
│
└── decks/
    ├── __init__.py            # Deck loader
    ├── kyler.py               # Kyler GW Humans/Counters
    ├── krenko.py              # Krenko mono-R Goblins/Combo
    └── yshtola.py             # Y'Shtola WUB Spellslinger/Drain
```

## Current Validation Scores

| Deck | Score | Archetype |
|------|-------|-----------|
| Kyler | 24/25 (96%) | GW Humans / +1/+1 Counters |
| Krenko | 24/25 (96%) | Mono-R Goblins / Combo |
| Y'Shtola | 20/25 (80%) | WUB Spellslinger / Drain |

## License

MIT
