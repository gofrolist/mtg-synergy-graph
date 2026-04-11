# CLAUDE.md

Guidance for Claude Code working in this repo.

## Project Overview

MTG Synergy Graph — deterministic, rule-based EDH/Commander synergy scorer
using Forge DSL ports. No training, no EDHREC at inference.
Current aggregate NDCG@30 ~ 0.130 on the 100-commander golden set.

## Common Commands

```bash
uv run python scripts/import_cardsfolder.py                              # Import fresh DB
uv run python scripts/recommend.py --commander "Korvold, Fae-Cursed King" --top 30 --explain
uv run python scripts/compare_edhrec.py --commanders tests/fixtures/golden_set.json
uv run python scripts/golden_set_track.py --baseline tests/fixtures/golden_set_run.json
uv run pytest tests/                                                     # 318 tests, ~46s
```

## Scoring Architecture

Bucket-based scoring in `scoring.py` → `score_all_candidates()`:

| Bucket | Weight | Signal |
|--------|--------|--------|
| port_match | 10 | Trigger feeder: cmdr trigger ↔ candidate effect |
| lord | 12 | Tribal lord/anthem matches |
| amplifier | 10 | Token/counter doublers (Doubling Season) |
| effect_resonance | 10 | Same effect class resonance (Proliferate, Mill, DigUntil) |
| replacement_resonance | 10 | Replacement doublers (AddCounter, CreateToken) |
| opponent_forcing | 12 | Opponent-forcing effects for opp-trigger commanders |
| resource_density | 8 | Card-type density for cost-anchored commanders |
| trigger_resonance | 8 | Shared trigger event (Sacrificed, Taps) |
| cost_synergy | 6 | Cost resource matching |
| scaling | 6 | scales_with matches |
| sacrifice_synergy | 6 | Outlet ↔ payoff cluster + token-loop |
| graveyard_synergy | 6 | Grave filler ↔ reanimator (library_to_grave ×2.0) |
| counter_ecosystem | 6 | Counter-payoff cards for counter-producer cmdr |
| untap_synergy | 6 | Untap sources for tap-activated commanders |
| token_etb_payoff | 6 | ETB payoff for token-producing commanders |
| stat_scaling | 4 | High-stat candidates (toughness for Phenax) |
| spellcast_density | 4 | Spell-type density (instants for Talrand) |
| counter_synergy | 4 | +1/+1 counter producer → payoff commander |
| etb_value | 4 | ETB/death value creatures for recursion commanders |
| replacement_producer | 4 | Producer cards for replacement doublers |
| deck_hints | 4 | Forge AI annotations |
| chain | 3 | 2-hop indirect matches |
| strategic | 2 | Heuristic rules (evasion, mass pump, mana sink) |
| staple | 1 | Format staples |
| catchall | 1 | Weak per-card color/type match |
| replacement | -10 | Conflicting replacement effects |

Penalties in `penalties.py`: 12 rules (wrong color identity, wrong token
type, non-counter creatures for counter cmdrs, etc.).

## Key Implementation Details

- **Broad ETB-self dampening**: `Creature.Other+YouCtrl` triggers match 17k
  cards. Dampened ×0.15 (or ×0.5 for Purphoros-class: only-trigger + payoff).
- **Branch weighting**: `BRANCH_MULTIPLIER` discounts conditional/branched
  abilities. See `docs/SPEC.md`.
- **Death-signature gate**: `_commander_death_signature` skips `Card.Self`
  and opponent-scoped (`OppCtrl`/`OppOwn`) triggers to avoid false positives
  (Locust God, Tergrid).
- **SpellCast exclusion**: broad types (Creature, Historic, Permanent) excluded
  from `spellcast_density` to prevent flooding.

## Conventions

- Cards keyed by Scryfall `oracle_id`.
- SQL fragment interpolation guarded by `_VALID_*_EXPRS` frozensets +
  `ValueError` (never `assert` — stripped by `python -O`).
