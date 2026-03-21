# TODO — MTG Synergy Graph Improvements

## Tag Quality

### Fix tribal tags for wants (high priority)
The provides tribal tags were fixed (76 false positives removed) but wants tribal tags haven't been validated yet. Same approach: cross-reference `wants: X-tribal` with oracle text — if oracle doesn't mention the creature type, remove the tag.

```sql
SELECT w.oracle_id, w.tag, c.name, c.oracle_text
FROM wants w JOIN cards c ON w.oracle_id = c.oracle_id
WHERE w.tag LIKE '%-tribal'
```

### Re-tag with stricter tribal instructions
Current gpt-4.1-mini tags assign `X-tribal` too broadly. For the next tagging round, add to the prompt: "Only use X-tribal if the card's oracle text explicitly references creature type X. Being a creature of type X does NOT mean it provides X-tribal."

### Distinguish static vs triggered provides
Cards with triggered abilities (Sanguine Bond: "whenever you gain life") vs static effects (Anthem: "creatures you control get +1/+1") behave differently for combos. Add `trigger_provides` field or a boolean `is_triggered` flag per tag. This would dramatically improve combo detection accuracy.

### Cross-validate tags against Scryfall community tags
Scryfall tags are now in the DB (10k cards, 71k assignments). Build agreement metrics:
- Map Scryfall tags to our vocabulary (e.g. `anthem` → `creature-pump`, `mana rock` → `mana-acceleration`)
- Measure precision/recall for each mapping
- Cards where Scryfall and our tags disagree are candidates for review

### Cross-validate tags against EDHREC themes
`data/edhrec_theme_cards.json` has 100 themes with card lists + synergy scores. Map themes to our tags:
- `tokens` → `token-generation` / `token-events`
- `+1/+1-counters` → `counter-placement` / `counter-placement-events`
- `lifegain` → `life-gain` / `life-gain-events`
- High EDHREC synergy + missing our tag = false negative
- Our tag + zero EDHREC synergy = potential false positive

## Combo Detection

### Integrate Commander Spellbook API
Ground-truth combo database. API: `https://backend.commanderspellbook.com/`

```python
# Search combos by card name
GET /variants/?q={card_name}&limit=10&format=json
# Find all combos in a deck
POST /find-my-combos/
# Combo outcome taxonomy
GET /features/?format=json
```

Implementation:
1. Create `fetch_combos.py` — fetch combos for top 10k EDHREC cards, cache to `data/commander_spellbook_combos.json`
2. Rate limit: 1 req/sec
3. Index by oracle_id for cross-reference
4. A combo is "infinite" if any produce feature contains "Infinite"
5. Annotate `find_combos()` output: known combos get Spellbook labels, unknown ones labeled "synergy"

### Improve combo scoring
Current: `len(a_to_b) + len(b_to_a)` * combo_potential * commander bonus

Better approach:
- Weight bridged matches lower than exact (0.7x vs 1.0x)
- Add oracle text loop detection ("whenever A happens, do B" + "whenever B happens, do A")
- Separate scoring tiers: "confirmed infinite" (from Spellbook), "likely combo" (high combo_potential + cycle), "synergy" (low combo_potential cycle)

## Recommendations

### Mana cost awareness
Scion of Draco (CMC 12 in 2-color deck) still gets recommended. Add penalties:
- Cards with CMC > average deck CMC + 3 get score penalty
- Domain/Devotion cards penalized in low-color decks
- X-cost cards evaluated at typical X value

### Strategy-aware filtering
Commander build mode detects strategies from provides/wants. Use this to filter:
- If commander wants `human-tribal`, weight Human creatures higher (done via tribal boost)
- If commander wants `sacrifice-events`, recommend sacrifice outlets even if they don't directly connect
- Detect "anti-synergy" — card wants X but deck doesn't produce X at all

### Show oracle text in recommendations
Add `--verbose` flag to show oracle text for each recommended card. Helps user quickly judge relevance without looking up each card.

## Infrastructure

### Update CLAUDE.md
Reflect current 34k-card DB, 3-field schema, commander build mode, tribal tag fixes.

### Clean up model_output/
Contains 15+ experiment directories (~60GB). Keep only:
- `iter-9-baseline` (best model, 65.3%)
- Delete intermediate checkpoints and GGUFs for other experiments

### Update tag registry from full 34k dataset
Current registry v3.1 was built from 10k cards. Rebuild with all 34k for better coverage:
```bash
python3 -c "... merge all tags, filter to 3+ occurrences, update synergy_tag_registry.json"
```

### Automate new-set workflow
When a new MTG set releases:
1. `python3 download_cards.py` — refresh Scryfall bulk
2. Diff against existing DB to find new/changed cards
3. Auto-tag new cards via gpt-4.1-mini (~$0.01/set)
4. Import to DB + backfill Scryfall metadata
5. Re-validate tribal tags on new cards
6. Rebuild embeddings

### OpenAI fine-tuning
Train gpt-4.1-mini on our clean 19k training data via OpenAI supervised fine-tuning API. The fine-tuned model would produce better tags for new sets. Cost: ~$5/hr training, min 50 examples.
