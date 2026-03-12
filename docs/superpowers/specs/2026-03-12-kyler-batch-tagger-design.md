# Kyler Card Filter + Batch Tagger

## Summary

Filter ~500 Kyler-relevant cards from Scryfall bulk data and batch-tag them via Claude API (Sonnet). Two-pass filter: core synergy (Humans, +1/+1 counters, Human-referencing cards) then extended synergy (ETB, tokens, tribal enablers, proliferate). Batch 5 cards per API call to reduce cost.

## Components

### 1. `card_filter.py` — Filter Script

Two-pass filter over `data/oracle_cards.json`, output to `data/kyler_candidates.json`.

**Pass 1 — Core synergy (~200-250 cards):**
- GW-legal Human creatures with non-trivial oracle text (>20 chars)
- Any GW-legal card with `+1/+1 counter` in oracle text
- Any GW-legal card mentioning `human` in oracle text
- Deduplicate by `oracle_id`

**Pass 2 — Extended synergy (~150-250 cards):**
- GW-legal non-Human cards with ETB triggers (`enters`, `enters the battlefield`)
- Token generators (`create a`, `create X`)
- Tribal enablers (`choose a creature type`, `changeling`)
- Counter interaction without `+1/+1` specifically (`proliferate`, `counter on`, `double`)
- Exclude cards already in pass 1
- Cap at 250

### 2. `batch_tagger.py` — Batch Tagger

- Reads `data/kyler_candidates.json`
- 5 cards per API call
- Reuses `prompt_builder.py` system prompt, corrections, few-shot examples
- Adapted user prompt for multi-card batches
- Model: `claude-sonnet-4-20250514`
- API key: `ANTHROPIC_API_KEY` env var
- Output: `data/kyler_tags.json`
- Resume: skips already-tagged `oracle_id`s if output file exists
- Retry: exponential backoff, 3 retries per batch on API errors
- JSON parse failure: log raw response, skip batch, continue
- Progress: `[batch 3/100] Tagged: Card A, Card B, ...`
- Summary: X tagged, Y failed, Z skipped

### 3. Cost Estimate

- ~100 API calls (500 cards / 5 per batch)
- ~270K input + ~150K output tokens
- **~$2-3 total**
