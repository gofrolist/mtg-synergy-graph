# MTG Synergy Graph — Enrichment Pipeline Design

**Date:** 2026-03-20
**Status:** Approved
**Goal:** Build an offline, self-sufficient synergy/combo detection pipeline that doesn't depend on EDHREC or Scryfall tagger at runtime. Enrich Scryfall data with parsed abilities and strategy classifications. Improve combo detection with ground-truth validation. Enable strategy-aware recommendations with user override.

---

## Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Strategy detection | Auto-detect + user override (`--strategies`) | Flexibility without losing automation |
| Oracle text parser | Hybrid: rule-based + LLM for flagged cards | Independence for 80% of cards, LLM for complex 20% |
| Combo detection | Ground-truth validated (Commander Spellbook) | Proven infinites without building a game simulator |
| Schema evolution | Extend existing (add abilities table alongside provides/wants) | Backward compatible, incremental migration |

---

## Section 1: Tag Cleanup + Registry Rebuild (P0)

### 1.1 Fix wants tribal tags

Query all `wants` tags matching `%-tribal` in `tags.db`. Cross-reference with oracle text: if a card `wants: goblin-tribal` but its oracle text doesn't mention "Goblin", remove the tag. Same validation already done for provides (76 false positives removed).

```sql
SELECT w.oracle_id, w.tag, c.name, c.oracle_text
FROM wants w JOIN cards c ON w.oracle_id = c.oracle_id
WHERE w.tag LIKE '%-tribal'
```

### 1.2 Rebuild registry from 34k cards

Current registry v3.1 was built from 10k cards. Scan all 34k tagged cards in `tags.db`, collect every provides/wants tag with 3+ occurrences, output `synergy_tag_registry.json` v4.0. May surface new tags missing from the 10k subset.

Tags that existed in v3.1 but drop below the 3-occurrence threshold in the full 34k scan are removed from the registry vocabulary. Existing card records in `tags.db` keep those tags (no data deletion), but they will not appear in the canonical vocabulary for future tagging or normalization.

### 1.3 Add strategies table to DB

```sql
card_strategies (
  oracle_id TEXT,
  strategy TEXT,
  confidence REAL,
  PRIMARY KEY (oracle_id, strategy)
)
-- e.g., ("abc-123", "tokens", 0.85)
```

Strategy mapping rules (hardcoded, ~30 rules):
- Card has `provides: token-generation` -> strategy `tokens` (confidence 1.0)
- Card appears in EDHREC theme data (`data/edhrec_theme_cards.json`) with synergy > 0.10 -> strategy from theme (confidence = synergy score)
- Card has `provides: counter-placement` -> strategy `+1/+1-counters`

EDHREC theme data source: `data/edhrec_theme_cards.json` (already scraped, ~100 themes with card names + synergy scores). Cross-reference card names to oracle_ids via `tags.db` name lookup. Cards not found by exact name match are skipped (acceptable loss — EDHREC names should match Scryfall canonical names for 95%+ of cards).

Confidence threshold: strategies with confidence < 0.3 are stored but not surfaced in recommendations by default. Only strategies with confidence >= 0.3 count as "active" for strategy_relevance scoring.

Derives strategies from existing data (provides/wants tags + EDHREC theme memberships). No LLM calls.

---

## Section 2: Oracle Text Ability Parser (P1)

### 2.1 New abilities table

```sql
abilities (
  oracle_id TEXT,
  ability_index INTEGER,
  ability_type TEXT,         -- 'triggered', 'activated', 'static', 'replacement', 'keyword', 'mana'
  trigger_condition TEXT,    -- NULL for non-triggered. e.g., "whenever a creature enters"
  trigger_tags TEXT,         -- JSON: ["creature-etb"]. NULL for non-triggered. Tagged from trigger_condition text.
  cost TEXT,                 -- NULL for non-activated. e.g., "{2}, {T}, Sacrifice a creature"
  effect TEXT,               -- "create a 1/1 white Human creature token"
  effect_tags TEXT,          -- JSON: ["token-generation", "creature-creation"]
  zone TEXT,                 -- "battlefield" (default), "graveyard", "hand", "exile", "any"
  targets TEXT,              -- "creature you control", "any target", NULL
  is_mana_ability BOOLEAN,
  PRIMARY KEY (oracle_id, ability_index)
)
```

### 2.2 Parser implementation — ability_parser.py

Three phases:

**Pre-processing:**
- Strip reminder text in parentheses: `\(.*?\)` — prevents false matches on reminder colons like "Cycling {2} ({2}, Discard this card: Draw a card.)"
- Split oracle text by `\n` (each paragraph = one ability in MTG rules)
- For double-faced cards: oracle_text contains `" // "` separator. Split on `" // "` first, process each face independently with a `face` column or prefix in ability_index

**Phase 1: Keyword extraction** (deterministic, 100% coverage)
- Match ~70 MTG keywords from Scryfall `keywords` field (already in DB, more reliable than regex)
- Each keyword -> one `keyword` ability row

**Phase 2: Pattern matching** (rule-based, targets ~80% of non-keyword abilities)
- Triggered: `^(When(ever)?|At) (.+?), (.+)$` -> trigger_condition + effect. For multi-clause triggers with "if" conditions (e.g., "Whenever a creature enters the battlefield under your control, if it's a Human, put a +1/+1 counter on it"), greedily capture everything before the last comma-delimited effect clause as the trigger_condition
- Activated: `^(.+?): (.+)$` where cost part contains `{` or `{T}` or `Sacrifice` or `Tap` -> cost + effect
- Static: lines with no trigger/cost pattern -> static ability
- Replacement: `If .* would .*, .* instead` -> replacement
- Mana: activated abilities where effect matches `Add \{.\}` -> mana ability

**Phase 3: Effect tagging** (rule-based, ~60 patterns)
- Same patterns applied to BOTH `effect` text AND `trigger_condition` text
- Effect text: `"create.*token"` -> `token-generation`, `"draw.*card"` -> `card-draw`, etc.
- Trigger condition text: `"creature enters"` -> `creature-etb`, `"creature dies"` -> `creature-death`, `"you gain life"` -> `life-gain-events`, `"you cast.*spell"` -> `spell-cast`, etc.
- This produces `effect_tags` (JSON) AND `trigger_tags` (new JSON column) for each ability
- Maps to existing provides/wants vocabulary
- Cards where <50% of abilities got any tags -> flagged `low_confidence` for optional LLM review

**Zone defaults:**
- Keywords: use known zone mapping (cycling -> hand, unearth -> graveyard, most others -> battlefield)
- Triggered/activated/static: default to "battlefield" unless trigger/effect text contains "from your graveyard", "from your hand", "from exile" etc.

### 2.3 Validation

Parse all 34k cards. Manually inspect 10-15 key Kyler deck cards:
- Kyler himself (triggered + static)
- Hardened Scales (replacement)
- Cathars' Crusade (triggered)
- Champion of the Parish (triggered)
- Gavony Township (activated, mana cost)
- Beast Within (instant — effect parsing)

Additionally, validate one card per unusual card type:
- A planeswalker (loyalty abilities use `+N:` / `-N:` format)
- A saga (chapter abilities: "I, II —", "III —")
- A double-faced card (oracle_text split by `" // "`)
- A split/adventure card

Verify trigger/effect/cost extraction is correct. Kyler chosen for diversity of ability types (triggered, static, activated, replacement) and 25+ hand-curated synergy pairs to validate against.

---

## Section 3: Commander Spellbook Integration + Enhanced Combos (P2)

### 3.1 Data fetch — fetch_spellbook.py

Base URL: `https://backend.commanderspellbook.com/`
- Bulk endpoint: `GET /variants/?format=json&limit=100&offset=0` (paginated, 100 per page)
- Response fields per combo: `id`, `uses` (array of card objects with `card.name`), `produces` (array of feature objects with `feature.name` like "Infinite damage"), `of` (prerequisites text), `status` (filter to "OK" only)
- Pagination: follow `next` URL until null. Rate limit: 1 req/sec.
- Total: ~50k combos. One-time fetch, cache to `data/commander_spellbook.json`.
- Cross-reference card names to oracle_ids via `tags.db` exact name lookup. Cards not found are logged and skipped (expect 95%+ match rate). Unresolved names logged to `data/spellbook_unresolved.txt` for manual review.

New DB tables:

```sql
spellbook_combos (
  combo_id TEXT PRIMARY KEY,
  card_oracle_ids TEXT,      -- JSON array
  card_names TEXT,           -- JSON array
  result TEXT,               -- "Infinite damage, Infinite tokens"
  prerequisites TEXT,        -- "All permanents on battlefield, {2} available"
  card_count INTEGER
)

spellbook_combo_cards (
  combo_id TEXT,
  oracle_id TEXT,
  PRIMARY KEY (combo_id, oracle_id)
)
-- INDEX on oracle_id
```

### 3.2 Three-tier combo labeling

| Tier | Label | Detection Method |
|---|---|---|
| Confirmed Infinite | `infinite-confirmed` | All combo cards present in deck, matched to Spellbook |
| Likely Combo | `combo-likely` | Provides->wants cycle + trigger chain from abilities table |
| Synergy | `synergy` | Provides->wants cycle, no trigger chain or Spellbook match |

### 3.3 Trigger chain detection (new algorithm)

Uses the `trigger_tags` and `effect_tags` columns from the abilities table (both are JSON tag arrays, directly intersectable).

For each 2-card pair (A, B) with provides->wants cycle:
1. Get A's triggered abilities and B's triggered abilities from abilities table
2. Collect A's `effect_tags` (union across all abilities) and A's `trigger_tags` (union across triggered abilities). Same for B.
3. Check: A's `effect_tags` intersect B's `trigger_tags`? AND B's `effect_tags` intersect A's `trigger_tags`?
4. If yes -> `combo-likely` (circular trigger chain: A's effect triggers B, B's effect triggers A)
5. Bonus: if either card has a mana ability (is_mana_ability=TRUE) -> escalate confidence

**Hard dependency:** Section 3 (combo-likely tier) requires Section 2 (abilities table with trigger_tags). Sections 3.1 (Spellbook fetch) and 3.2 (confirmed-infinite tier) can proceed independently of Section 2. Only the combo-likely tier needs the parser.

### 3.4 Partial Spellbook matches

If a deck has N-1 of N cards in a known Spellbook combo, flag it: "You're 1 card away from [combo name] — add [missing card]". Feeds into recommendations as combo completions.

### 3.5 Validation

Run on Krenko's deck (known combo-heavy):
- Count Spellbook combos in deck
- Count trigger-chain detections that match independently
- Review false positives: combos our system finds that Spellbook doesn't list

---

## Section 4: Strategy Model + Enhanced Recommendations (P2)

### 4.1 Strategy detection — strategy_detector.py

Given a commander, detect strategies from three signals:

1. **Commander's provides/wants tags**: Kyler provides `counter-placement`, `human-tribal` -> `+1/+1-counters`, `humans`
2. **Commander's parsed abilities**: triggered "Human enters -> +1/+1 counter" + static "Humans get +X/+X" -> `go-wide`, `+1/+1-counters`, `humans`
3. **Strategy mapping rules** (~30 rules): tag clusters -> strategy names

Output per commander:
```python
{
  "commander": "Kyler, Sigardian Emissary",
  "strategies": [
    {"name": "humans", "confidence": 0.95, "signals": [...]},
    {"name": "+1/+1-counters", "confidence": 0.90, "signals": [...]},
    {"name": "go-wide", "confidence": 0.70, "signals": [...]}
  ]
}
```

### 4.2 User override via CLI

```bash
synergy_graph.py --deck kyler --strategies humans,counters    # focus
synergy_graph.py --deck kyler --exclude-strategies go-wide    # skip
synergy_graph.py --deck kyler --strategies auto               # default
```

### 4.3 Strategy-weighted recommendation scoring

Current: `score = sum(edge_weights) * tribal_boost`

New: `score = sum(edge_weights) * strategy_relevance * tribal_boost`

Where `strategy_relevance`:
- Card's strategies overlap with active strategies -> 1.0 + 0.2 per match
- Zero overlap -> 0.5 (penalized, not excluded)
- Card completes a Spellbook combo -> ×2.0 multiplier (not additive — scales with edge weight so it doesn't dominate low-synergy decks)

### 4.4 Enhanced recommendation output

```
=== RECOMMENDATIONS for Kyler (strategies: humans, +1/+1-counters, go-wide) ===

COMBO COMPLETIONS (1 card away from confirmed infinite):
  Cathars' Crusade + Kyler + [missing card] -> result (Spellbook #ID)

BEST FIT (high synergy + strategy match):
  1. Card Name [strategies] score: X.X [tribal]
  ...

ENABLERS: ...
```

### 4.5 Anti-synergy detection

Scan deck for cards with zero strategy overlap AND not staples. Staple detection uses the existing `role` field in `cards` table — cards with role in `{ramp, draw, removal, protection, land}` are exempt from anti-synergy flagging. These are swap-out candidates.

### 4.6 Enhanced deck analysis (--deck-view)

```
=== DECK ANALYSIS: Kyler ===
Detected strategies: humans (38 cards), +1/+1-counters (22 cards), go-wide (15 cards)
Strategy coverage: 72% of non-land cards align with >=1 strategy
Confirmed combos: 2 (Spellbook)
Likely combos: 4 (trigger chain)
High synergy pairs: 31
Anti-synergy cards: 3 (swap candidates)
```

---

## DB Migration

New tables are added via `CREATE TABLE IF NOT EXISTS` in `tag_db.py` SCHEMA string (same pattern as existing tables). No ALTER TABLE needed — all changes are additive new tables. The `abilities` table adds one new column (`trigger_tags`) vs the original abilities design; this is included from the start.

## File Changes Summary

| File | Action | Purpose |
|---|---|---|
| `ability_parser.py` | New | Oracle text -> structured abilities |
| `strategy_detector.py` | New | Commander -> strategy detection |
| `fetch_spellbook.py` | New | Commander Spellbook bulk fetch + cache |
| `tag_db.py` | Modify | Add abilities, card_strategies, spellbook tables + queries |
| `synergy_graph.py` | Modify | 3-tier combo labeling, strategy-weighted recommendations, anti-synergy, new CLI args (`--strategies`, `--exclude-strategies`) |
| `synergy_tag_registry.json` | Rebuild | v4.0 from 34k cards |
| `data/commander_spellbook.json` | New (gitignored) | Cached Spellbook data |
| `data/spellbook_unresolved.txt` | New (gitignored) | Unresolved Spellbook card names for manual review |

## Implementation Order

1. Tag cleanup + registry rebuild (Section 1) — foundation, all downstream improves
2. Oracle text parser (Section 2) — independence layer, enables trigger-chain detection
3. Commander Spellbook integration (Section 3) — ground truth for combo validation
4. Strategy model + recommendations (Section 4) — user-facing improvements

Each step is independently useful and testable. Kyler validates parser accuracy (Section 2), Krenko validates combo detection (Section 3), both validate strategy model (Section 4).
