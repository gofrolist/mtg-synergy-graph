# Switch Training to edhrec_card_synergy Labels + CLI Simplification

## Goal

Replace binary EDHREC average-deck membership labels with richer edhrec_card_synergy data (367k rows with continuous synergy scores and section labels). Simplify CLI to commander-only interface, remove `decks/` folder.

## Training Labels

**Current:** `edhrec_average_decks` → binary in-deck/not-in-deck, quantized to 0-9 grades via synergy lookup. 93k positives + 280k random negatives = 372k rows.

**New:** `edhrec_card_synergy` directly. All 367k entries become training data with section-based grades:

| Grade | Source | Count | Description |
|-------|--------|-------|-------------|
| 5 | section = "High Synergy Cards" | 13,799 | EDHREC's curated high-synergy picks |
| 4 | section = "Top Cards" | 13,787 | Popular + synergistic |
| 3 | Other sections, synergy > 0.1 | ~101k | Moderate synergy |
| 2 | Other sections, synergy 0-0.1 | ~150k | Low/minimal synergy |
| 1 | Any section, synergy < 0 | ~75k | Negative synergy (anti-synergy) |
| 0 | Not in table | ~367k | Random negatives (1:1 ratio) |

Total: ~734k training rows. LambdaRank label_gain: `[0, 1, 3, 6, 15, 30]` (6 grades, steeper curve to emphasize High Synergy).

**Negative sampling:** Same as current (color-identity filtered, 50% hard negatives via strategy/subtype overlap), but 1:1 ratio with total positives instead of 3:1.

**Staple filtering:** Continue filtering generic staples (>30% deck frequency) from grade 5/4 to avoid learning "Sol Ring is good in everything."

## CLI Simplification

**Remove:**
- `decks/` folder (15 deck configs)
- `--deck` CLI argument
- All deck config loading code in `cli.py`
- Deck-specific code paths in engine.py, swaps.py

**Keep:**
- `--commander "Name" --recommend` (already implemented)
- `--commander "Name" --combos` (detect combos for commander's color identity)

**Defer (not in this spec):**
- `--swaps` needs a decklist input mechanism (file/stdin) — separate concern
- `--deck-view` — remove (requires deck)

## Evaluation

Update `compare_edhrec.py` to work with `--commander` instead of `--deck`:

```bash
compare_edhrec.py --commander "Kyler, Sigardian Emissary"  # single commander
compare_edhrec.py --all                                      # all commanders with synergy data
```

Comparison metrics:
- **High-Synergy Recall@30:** How many of our top 30 are in EDHREC's "High Synergy Cards" section?
- **Top Cards Recall@30:** How many are in "Top Cards" section?
- **NDCG@30:** Using the section-based grades as ground truth

## Files Changed

| File | Change |
|------|--------|
| `train_fusion_model.py` | New `_load_synergy_pairs()` replacing `_load_pairs_for_features()`. New grading logic. |
| `src/mtg_synergy/cli.py` | Remove `--deck`, deck config loading. Keep `--commander --recommend/--combos`. |
| `compare_edhrec.py` | Switch to `--commander` interface, compare against High Synergy section. |
| `decks/` | Delete entire folder |
| `src/mtg_synergy/recommend/engine.py` | Remove deck-config-specific code |
| `CLAUDE.md` | Update commands and architecture |

## Success Criteria

- NDCG@30 on the new labels (leave-commander-out CV) — establish new baseline
- High-Synergy Recall@30 reported per commander
- `--commander "Name" --recommend` works for any commander
- No references to `decks/` or `--deck` remain
