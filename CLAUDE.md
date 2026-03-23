# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

MTG Synergy Graph — a tool for analyzing Magic: The Gathering EDH/Commander deck synergies. The system uses multiple signal layers to find card synergies: LLM scoring, a trained two-tower neural model, structured game mechanics extraction, and a tag-based provides/wants graph.

### Signal Architecture (recommendation pipeline)

```
For any commander, recommendations use 4 signal layers:

1. LLM SCORES (best quality, cached in synergy_scores table)
   - Pre-scored via score_synergies.py (OpenAI gpt-5.4-mini or local gemma3:12b)
   - 33 commanders scored, ~99k pairs
   - Integer 1-10 scale, PRIMARY ranking signal when available
   - Tiebreaker: tower model prediction + EDHREC rank

2. TOWER MODEL (instant, any commander, trained on LLM scores)
   - Two-tower neural net: commander_embedding × card_embedding → synergy
   - Trained on 99k LLM-scored pairs, corr=0.75 with LLM scores
   - Scores ALL cards for any commander in <100ms
   - Used as Tier 2 for unseen commanders + tiebreaker within same LLM score

3. MECHANICS ENGINE (filter-aware event chain matching)
   - 7105 cards with structured mechanics extracted via LLM
   - 6 matching modes: event chain, card-IS-event, shared trigger,
     modifier, enabler, self-sacrifice
   - max(graph, mechanics) scoring prevents mechanics cards from being buried

4. TAG GRAPH (provides/wants edges, baseline signal)
   - 34k cards tagged with role/provides/wants
   - Composite edges: provides→wants + peer-enabler + shared-wants + embedding
   - Commander 5x edge weight, keyword-only creature penalty
   - Strategy multiplier, tribal boost, combo completion bonus
```

### Current Performance

Average EDHREC alignment: **13.4/30** (up from 2.8/30 baseline, 4.8x improvement)

| Commander | Score | Signal source |
|---|---|---|
| Sram (equipment/aura) | 24/30 | LLM + mechanics |
| Syr Konrad (graveyard) | 20/30 | LLM + mechanics |
| Edgar (vampire tribal) | 17/30 | LLM + tribal tags |
| Krenko (goblin tribal) | 15/30 | LLM + tribal tags |
| Urza (artifacts) | 15/30 | LLM + mechanics |
| Tatyova (landfall) | 15/30 | LLM + tower model |
| Sauron (amass/ring) | 13/30 | LLM (gemma3 local) |
| Pantlaza (dinosaur) | 12/30 | LLM + type matching |
| Kyler (human tribal) | 11/30 | LLM + tribal tags |
| Niv-Mizzet (draw/damage) | 10/30 | LLM + event chains |

## Common Commands

```bash
# === EXISTING PIPELINE ===
python3 download_cards.py                  # Refresh Scryfall data (~150MB)
python3 batch_tagger.py --candidates data/remaining_candidates.json --provider openai --model gpt-4.1-mini
python3 tag_db.py import data/all_tags_gpt41mini.json
python3 tag_db.py backfill && python3 tag_db.py fix-tribal && python3 tag_db.py rebuild-registry
python3 ability_parser.py                  # Parse oracle text into abilities
python3 fetch_spellbook.py                 # Fetch 82k combos
python3 strategy_detector.py --populate    # Assign strategies

# === NEW: Synergy scoring pipeline ===
# Score a commander with OpenAI (best quality, ~$0.50/commander, ~2 min)
python3 score_synergies.py --commander "Krenko, Mob Boss"
python3 score_synergies.py --commander "Krenko, Mob Boss" --batch-api  # 50% cheaper

# Score a commander locally with Ollama (free, ~90 min)
python3 score_synergies.py --commander "Krenko, Mob Boss" --provider ollama --model gemma3:12b

# Score all deck commanders
python3 score_synergies.py --all-decks
python3 score_synergies.py --all-decks --batch-api

# Score new set cards against all known commanders
python3 score_synergies.py --new-cards data/new_set.json

# Check scoring progress
python3 score_synergies.py --stats

# === NEW: Mechanics extraction ===
python3 extract_mechanics.py --batch 1000   # Extract next 1000 cards by EDHREC rank
python3 extract_mechanics.py --card "Krenko, Mob Boss"  # Single card
python3 extract_mechanics.py --stats        # Check coverage
python3 extract_mechanics.py --validate     # Test matching quality

# === NEW: Tower model training ===
python3 train_tower_model.py               # Train from LLM scores (~45s)
python3 train_tower_model.py --predict "Any Commander"  # Score all cards (<100ms)
python3 train_tower_model.py --stats       # Check training data

# === Recommendations ===
python3 synergy_graph.py --deck krenko --recommend   # Top 30 recommendations
python3 synergy_graph.py --deck krenko --swaps       # Card swap suggestions
python3 synergy_graph.py --deck krenko --combos      # 3-tier combo detection
python3 synergy_graph.py --deck krenko --deck-view   # Deck analysis

# === Comparison & validation ===
python3 compare_edhrec.py --refresh --quiet    # Compare all decks vs EDHREC
python3 compare_edhrec.py --deck krenko --fast  # Single deck (cached)
python3 compare_edhrec.py --fast --quiet        # Summary only (0.07s)

# Tests
python3 -m pytest tests/ -v                    # Run all 63 tests
```

## Architecture

### Enrichment Pipeline

```
Scryfall API → download_cards.py → data/oracle_cards.json (36k cards)
                                        ↓
                              batch_tagger.py (gpt-4.1-mini)
                                        ↓
                              tag_db.py import → data/tags.db
                                        ↓
                    tag_db.py backfill → fix-tribal → rebuild-registry
                                        ↓
                    ability_parser.py → abilities table
                                        ↓
                    fetch_spellbook.py → spellbook_combos table (82k combos)
                                        ↓
                    strategy_detector.py → card_strategies table
                                        ↓
                    extract_mechanics.py → card_mechanics table (7105 cards)
                                        ↓
                    score_synergies.py → synergy_scores table (99k pairs, 33 commanders)
                                        ↓
                    train_tower_model.py → data/tower_model.npz
                                        ↓
                              synergy_graph.py --deck <name>
                              (4-layer scoring: LLM → tower → mechanics → graph)
```

### New-set update workflow

```bash
python3 download_cards.py                               # 1. Refresh Scryfall
python3 batch_tagger.py --candidates <new>              # 2. Tag new cards
python3 tag_db.py import <new_tags>                     # 3. Import tags
python3 tag_db.py backfill && fix-tribal && rebuild-registry  # 4. Enrich
python3 ability_parser.py                               # 5. Parse abilities
python3 strategy_detector.py --populate                 # 6. Strategies
python3 extract_mechanics.py --batch 1000               # 7. Extract mechanics for new cards
python3 score_synergies.py --new-cards data/new.json    # 8. Score vs known commanders (~$0.10)
python3 train_tower_model.py                            # 9. Retrain tower model (~45s)
```

### DB Schema (data/tags.db)

| Table | Rows | Purpose |
|---|---|---|
| cards | 33,930 | Card metadata from Scryfall |
| provides | 105k+ | Card provides tags (incl. auto-tribal from type_line) |
| wants | 89,356 | Card wants tags |
| abilities | 76,601 | Parsed oracle text abilities |
| card_strategies | 88,304 | Strategy assignments |
| spellbook_combos | 82,103 | Commander Spellbook combos |
| spellbook_combo_cards | 288,973 | Combo ↔ card junction |
| card_mechanics | ~17k | Structured game mechanics (LLM-extracted) |
| synergy_scores | ~180k | Commander × card synergy scores (LLM + auto) |

### Tag Schema (3-field)

Each card is tagged with:
- **role**: ramp, draw, removal, protection, enabler, threat, utility, land
- **provides**: what the card gives (e.g. `card-draw`, `token-generation`, `goblin-tribal`)
- **wants**: what conditions benefit it (e.g. `creature-death`, `wide-board`, `spell-cast`)

Tribal tags (human-tribal, goblin-tribal, etc.) are auto-assigned from creature type_line.

### Mechanics Schema (card_mechanics table)

Each card's abilities are extracted as structured JSON:
- **mechanic_type**: triggered, activated, static, replacement, keyword
- **trigger_event**: creature-enters, spell-cast, creature-dies, etc. (27 canonical events)
- **trigger_filter**: JSON filter (e.g., `{"subtype": "Human", "controller": "you"}`)
- **effect_action**: create-token, deal-damage, draw-card, etc. (35 canonical actions)
- **cost**: activation cost for activated abilities

Matching modes in mechanics_matcher.py:
1. **Event chain**: A produces events → B triggers on those events
2. **Card-IS-event**: B being cast/entering satisfies A's trigger filter (tribal matching)
3. **Shared trigger**: A and B both respond to same event type
4. **Modifier**: A amplifies/doubles what B does
5. **Enabler**: A's static scope applies to B's creature types
6. **Self-sacrifice**: Cards with sacrifice-self cost produce creature-dies event

### Synergy Scoring (synergy_scores table)

Pre-computed commander × card synergy scores on 1-10 scale:
- **Scoring providers**: OpenAI (gpt-5.4-mini), local Ollama (gemma3:12b)
- **Pre-filtering**: Top 2000 candidates per commander (by tag overlap + mechanics + EDHREC rank)
- **Auto-scoring**: Cards with zero synergy signals auto-scored as 2
- **Spellbook boost**: Cards in confirmed combos with commander boosted to 9
- **Resume-safe**: Every batch committed immediately, re-run picks up where it stopped
- **Batch API**: `--batch-api` flag for 50% cheaper OpenAI processing

### Tower Model (data/tower_model.npz)

Two-tower neural network trained on LLM synergy scores:
- **Input**: Commander embedding (768-dim) × Card embedding (768-dim) + 10 structural features
- **Architecture**: Projection (768→128) → element-wise product → MLP (138→64→32→1)
- **Training**: 99k pairs from 33 commanders, Adam optimizer, 100 epochs, ~45s
- **Performance**: correlation=0.75, MAE=1.01 with LLM scores
- **Inference**: <100ms for ALL cards for any commander
- **Training data filter**: Excludes auto-scored and spellbook-boosted pairs

### Recommendation Pipeline (synergy_graph.py --recommend)

```
1. Build graph (provides→wants edges + embeddings + peer-enabler)
2. Inject LLM≥7 candidates from synergy_scores (bypasses graph candidate pool)
3. Inject mechanics≥1.5 candidates from card_mechanics
4. Score candidates: tag graph × tribal × strategy × quality × CMC × popularity × affinity
5. Apply mechanics boost: max(graph_score, mechanics_as_graph)
6. Apply LLM/tower scoring: LLM_score × 1000 + tower_score × 10 + rank_tiebreak × 0.1
7. Unscored cards get tower model prediction or graph-only score
8. Sort and output top 30
```

### Swap System (synergy_graph.py --swaps)

Suggests card swaps with multi-layer protection:
- **Infrastructure protection**: Cards providing removal/protection/ramp/draw → never cut
- **Tribal protection**: Creatures matching deck's dominant type → never cut
- **Changeling/Shapeshifter**: Always protected in tribal decks
- **Commander synergy protection**: Top 20 cards by commander graph edge score → never cut
- **Mechanics protection**: Cards with mechanics score ≥2.0 with commander → never cut
- **Combo protection**: Cards in Spellbook combos with commander → never cut

### Combo Detection (3-tier)

| Tier | Label | Detection |
|------|-------|-----------|
| Confirmed Infinite | `infinite-confirmed` | All combo cards match a Commander Spellbook entry |
| Likely Combo | `combo-likely` | Provides→wants cycle + circular trigger chain |
| Synergy | `synergy` | Provides→wants cycle without trigger chain |

## Key Files

| File | Purpose |
|---|---|
| `synergy_graph.py` | Main entry point: --recommend, --swaps, --combos, --deck-view |
| `score_synergies.py` | LLM synergy scoring (OpenAI + Ollama + Batch API) |
| `extract_mechanics.py` | Structured mechanics extraction from oracle text |
| `mechanics_matcher.py` | Filter-aware event chain matching engine |
| `train_tower_model.py` | Two-tower neural synergy model training |
| `compare_edhrec.py` | Fast EDHREC comparison tool (parallel, cached) |
| `batch_tagger.py` | Card tagging (provides/wants/role) via LLM |
| `ability_parser.py` | Deterministic oracle text parser |
| `strategy_detector.py` | Rule-based strategy detection |
| `tag_db.py` | SQLite DB management |
| `fetch_spellbook.py` | Commander Spellbook API fetcher |
| `train_synergy_model.py` | GBT synergy model (legacy, replaced by tower model) |

## Key Conventions

- Cards keyed by `oracle_id` (Scryfall UUID) for dedup across reprints
- `data/oracle_cards.json` is gitignored (~150MB); must run `download_cards.py` first
- API calls use `urllib.request` (no `requests` dependency)
- Tags use kebab-case (e.g., `mana-acceleration`, `creature-death`)
- Tribal tags auto-assigned from type_line (e.g., Human creature → provides `human-tribal`)
- Deck configs live in `decks/` (15 decks: kyler, krenko, yshtola, atraxa, edgar, kaalia, niv_mizzet, pantlaza, sram, syr_konrad, tatyova, ur_dragon, urza, sauron)
- LLM scoring uses gpt-5.4-mini (requires `max_completion_tokens` not `max_tokens`)
- Local scoring uses gemma3:12b via Ollama (best quality/speed local model)
- Qwen3 models need `think: false` in Ollama payload to disable thinking
- Fine-tuning uses `.venv` with unsloth + torch (Python 3.12, not system Python 3.14)
- Tests: 63 tests in `tests/`
- Spellbook combo boosts must check color identity (fixed: 364 wrong-color boosts deleted)
