# Scryfall Bulk Data Loader

## Summary

Replace static `CARD_DB` dict in `card_db.py` with a loader that reads from Scryfall's `oracle_cards.json` bulk data file. Cards keyed by `oracle_id` (UUID) for deduplication across reprints.

## Components

### 1. `download_cards.py` — Download Script

- GET `https://api.scryfall.com/bulk-data` to get bulk data catalog
- Find entry with `type: "oracle_cards"`
- Download file from its `download_uri`
- Save to `data/oracle_cards.json`
- Create `data/` directory if missing
- Uses `requests` library

### 2. `card_db.py` — Loader (replaces static dict)

- On import, loads `data/oracle_cards.json`
- Builds `CARD_DB` dict keyed by `oracle_id` (UUID string)
- Each value: `{"name", "oracle_id", "cmc", "type_line", "oracle_text", "keywords", "color_identity"}`
- Builds `NAME_INDEX` dict: `name.lower()` → `oracle_id` for name-based lookups
- `reversible_card` layout: skip (no top-level `oracle_id`)
- If file missing: raise error with "Run download_cards.py first"

### 3. `enricher.py` — Update `fetch_card()`

- Support lookup by both name (via `NAME_INDEX`) and `oracle_id` (direct)
- Return dict includes `oracle_id` field
- Downstream pipeline unchanged

### 4. `.gitignore`

- Add `data/oracle_cards.json`
