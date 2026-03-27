# Creature-Enters Synthetic Edges + has_cmdr_edge Feature

**Date:** 2026-03-26
**Status:** Approved

## Problem

The forge model has two related weaknesses:

1. **Sparse causal graph for tribal/cast-trigger commanders.** 9,018 creatures (50%) produce no ChangesZone event. When you cast a Human creature, it enters the battlefield and triggers Kyler — but the graph doesn't model this. Kyler has only 79 incoming edges (token creators only). Champion of the Parish, Thalia's Lieutenant, and Hamlet Captain have zero edges to Kyler despite being core synergy cards.

2. **Text-matching false positives.** Semantic features (oracle_similarity 16%, embedding_cosine 16%, tower_forge 19%) account for 51% of GBM importance. Cards mentioning keywords incidentally rank high without mechanical connection. Examples: Oran-Rief Survivalist (Ally trigger, not Human) ranks #8 for Kyler; Secret Tunnel, Cave of Frost Dragon, Ice Floe (random lands) rank #2/#4/#6 for Sram.

## Solution

### Part 1: Expand synthetic edges — ChangesZone+Battlefield

Extend `_build_synthetic_edges` in `build_graph.py` to add a new implicit event: every permanent card produces a `ChangesZone` event with `destination=Battlefield` when entering the battlefield.

**Producers (cards that implicitly enter the battlefield when cast):**

| Type | Cards without ChangesZone edge | SQL filter |
|---|---|---|
| Creature | 13,794 | `type_line LIKE '%Creature%'` |
| Artifact | 2,899 | `type_line LIKE '%Artifact%'` |
| Enchantment | 2,836 | `type_line LIKE '%Enchantment%'` |
| Planeswalker | 123 | `type_line LIKE '%Planeswalker%'` |

**Responders (708 abilities across 40 filter patterns):**

Top responder filters for ChangesZone+Battlefield (excluding Self):
- `Land.YouCtrl` (209) — landfall, already covered by LandPlayed synthetic
- `Creature.Other+YouCtrl` (87) — generic creature ETB
- `Enchantment.YouCtrl` (47) — enchantress triggers
- `Creature.YouCtrl` (39) — creature ETB
- `Artifact.YouCtrl` (32) — artifact ETB
- `Human.Other+YouCtrl` (6) — Kyler and friends
- `Dragon.YouCtrl` (11), `Zombie.Other+YouCtrl` (5), `Ally.Other+YouCtrl` (46), etc.

**Design decisions:**
- Reuse existing precision logic: exact (subtype match) = strength 1.0, broad (card type match) = 0.6, IDF-dampened
- Relax the current subtype-only restriction — also include card_type-only responders (like `Creature.Other+YouCtrl`) matched as "broad" precision
- Keep `Card.Self` exclusion (triggers on itself, not synergy)
- Add entries to existing `SYNTHETIC_EVENTS` config list, one per card type
- Stream edges to DB in chunks (same pattern as existing synthetic edges)

**Estimated new edges:** ~250-500k (subtype-exact matches are selective, broad matches add volume but at lower strength).

### Part 2: Add `has_cmdr_edge` feature (F20) to forge GBM

Binary feature: 1.0 if any causal edge exists between commander and card (either direction), 0.0 otherwise.

**Implementation:** Data already pre-loaded in `score_forge_candidates` as `cmdr_out` and `cmdr_in` dicts.

```python
has_edge = 1.0 if (cmdr_out.get(oid, 0) > 0 or cmdr_in.get(oid, 0) > 0) else 0.0
```

Brings forge GBM from 20 to 21 features.

**Expected impact after graph expansion:**
- Kyler: `has_cmdr_edge=1` for all Human creatures. Oran-Rief Survivalist stays 0 (Ally, not Human).
- Sram: `has_cmdr_edge=1` for Aura/Equipment (via SpellCast edges). Random lands stay 0.
- GBM learns: `has_cmdr_edge=0` + `type_land=1` = push down. `has_cmdr_edge=0` + high text similarity = discount.

### Part 3: Rebuild + retrain

```bash
python3 build_graph.py --forge --rebuild      # Rebuild with new synthetic edges
python3 train_fusion_model.py --forge-only    # Retrain GBM with 21 features
```

## Files Changed

| File | Change |
|---|---|
| `build_graph.py` | Expand `_build_synthetic_edges` — add ChangesZone+Battlefield for creatures/artifacts/enchantments/planeswalkers, relax subtype-only filter to include card_type-only responders |
| `mtg_synergy/recommend/scoring.py` | Add F20 `has_cmdr_edge` in `score_forge_candidates` feature vector |
| `train_fusion_model.py` | Add F20 `has_cmdr_edge` to forge training feature builder |

## Evaluation

After rebuild + retrain:
1. Run `--recommend --forge` for Kyler, Sram, Krenko, Atraxa, Syr Konrad
2. Check: random lands gone from Sram top 30
3. Check: Oran-Rief Survivalist dropped from Kyler top 30
4. Check: Human synergy cards (Champion of the Parish, Thalia's Lieutenant) appear for Kyler
5. Run `compare_edhrec.py --refresh --quiet` for overall sanity check
6. Inspect feature importance — `has_cmdr_edge` should rank meaningfully
