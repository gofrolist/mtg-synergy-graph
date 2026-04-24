---
last_updated: 2026-04-24
module: complement_rules
title: Rule-consolidation check — what "healthy" looks like (62-rule baseline)
tags:
  - collinearity
  - embedding-dedup
  - rule-catalogue
  - consolidation
  - null-result
  - plan-002
  - plan-003
problem_type: best_practice
resolution_type: reference
applies_when:
  - Authoring a new complement rule and checking it doesn't duplicate existing signal.
  - Auditing the rule catalogue for pruning candidates.
  - Deciding whether a cosine-similar rule pair is actually redundant or merely overlapping.
created: 2026-04-24
---

# Rule-consolidation check — what "healthy" looks like

Two diagnostics answer different questions about rule redundancy, and
the current rule catalogue is an example of the distinction done right.
Future rule authors can re-run these commands to confirm their new
rule preserves the property.

## Commands

```bash
uv run scripts/bench.py audit --embedding-dedup          # content-space overlap
uv run scripts/bench.py audit --embedding-dedup --threshold 0.99  # only extreme content overlap
uv run scripts/bench.py audit --collinearity             # score-space correlation
```

## What each measures

### `--embedding-dedup` (content-space)

Computes cosine similarity between rules' candidate-activation
vectors in the 128-dim content embedding space
(`src/mtg_synergy_graph/embeddings/dedup.py`). Flags pairs above the
cosine threshold (default 0.95, min 20 activations per rule).

High cosine = "these two rules fire on the same cards." Content
overlap is EXPECTED — a card that sacrifices creatures for a trigger
legitimately satisfies both `cost_feeds_trigger` and `graveyard_play`.
Content overlap alone is not a reason to consolidate.

### `--collinearity` (score-space)

Pearson correlation + variance-inflation-factor between every pair of
rules' per-candidate score contributions, computed from the persisted
`rule_contributions` tensor
(`src/mtg_synergy_graph/bench/collinearity.py`). Default thresholds:
flag pairs with `|r| > 0.8 AND VIF > 5`.

High r + high VIF = "these two rules score the same dimension of
synergy." A card scoring 1.5 on rule A correlates with it scoring
1.5 on rule B. That IS redundant — you can drop one and the ranking
won't change.

## Interpretation matrix

| `--embedding-dedup` | `--collinearity` | Interpretation |
|---|---|---|
| Flagged | Not flagged | **Healthy.** Rules fire on similar cards but score different mechanical axes. Keep both. |
| Flagged | Flagged | **Redundant.** Consolidation candidate. Refine the gate or drop one. |
| Not flagged | Flagged | Rare. Different activation sets but correlated scores — usually a data bug (e.g., same tensor rows attributed to two rule IDs). |
| Not flagged | Not flagged | **Orthogonal.** No investigation needed. |

## Reference baseline (2026-04-24, 62 rules)

The 62-rule catalogue as of commit `ac38957`:

```
--embedding-dedup threshold=0.95: 17 pairs flagged
--embedding-dedup threshold=0.99:  2 pairs flagged
--collinearity default (|r|>0.8, VIF>5): 0 pairs flagged
--collinearity relaxed (|r|>0.5, VIF>2.0): 1 pair (negative r, VIF~2.5 — not redundant)
```

Zero collinear pairs even at aggressively-relaxed thresholds. The
content-space similar pairs are all axis-orthogonal in score space:

| Pair (cosine) | Why they're not redundant |
|---|---|
| `cost_feeds_trigger` × `graveyard_play` (0.996) | Same sacrifice-trigger cards, but one scores the cost→trigger pathway, the other scores graveyard-as-resource payoff. |
| `etb_self` × `tribal_density` (0.994) | ETB-stacking creatures often share a tribe, but ETB scoring is independent of tribal density. |
| `replacement_producer` × `token_producer` (0.990) | Same token-maker cards, but replacement-stack scoring is independent of raw token count. |

## What a failing consolidation check looks like

A rule that duplicates existing signal would show up in BOTH
diagnostics. Typical red flag: a new `combat_damage_scaler` rule
added when `voltron_payoff` already exists — both fire on combat-
enhancing cards AND both score the "more damage = better" axis.

Mitigations:
- Narrow the activation gate (e.g., restrict to specific keywords the
  existing rule doesn't cover).
- Drop the new rule; extend the existing rule's helper if the new
  mechanic is really a sub-case.
- Keep the new rule but re-label the existing one (e.g., rename
  `voltron_payoff` to `general_combat_payoff` and make the new rule
  genuinely narrower).

## When this changes

Re-run the baseline check after any of these:

- A new rule lands (default workflow per `docs/RULE_PLANNING.md` step 6.5).
- A rule's activation gate is relaxed (broader firing set → may
  collide with another rule).
- The IDF weights change (collinearity uses raw contributions; a
  weight shift can reveal previously-hidden correlation).
- The embedding pipeline changes
  (`TOKEN_FORMAT_VERSION` bump → `--embedding-dedup` cosine values
  shift, but not the `--collinearity` readings).

## References

- `docs/RULE_PLANNING.md` §6.5 — integrated into the rule-authoring workflow.
- `src/mtg_synergy_graph/bench/collinearity.py` — VIF + Pearson implementation.
- `src/mtg_synergy_graph/embeddings/dedup.py` — cosine-space implementation.
- Sibling infrastructure note:
  [`infrastructure-without-scoring-activation-2026-04-24.md`](infrastructure-without-scoring-activation-2026-04-24.md)
  — this diagnostic's value exists independently of the embedding
  scoring flip (which is declined).
