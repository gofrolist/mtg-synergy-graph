# CLAUDE.md

Guidance for Claude Code working in this repo.

## Project Overview

MTG Synergy Graph — deterministic, rule-based EDH/Commander synergy scorer
using Forge DSL ports. No training, no EDHREC at inference.
Current aggregate NDCG@30 ~ 0.256 on the 100-commander golden set.

For user-facing setup / quick-start, see [README.md](README.md).
For the forward-looking rule-planning workflow (gap_report → scaffold →
audit cycle), see [docs/RULE_PLANNING.md](docs/RULE_PLANNING.md).
For a dated log of rule additions, audit verdicts, and per-commander
impact notes, see [docs/RULE_HISTORY.md](docs/RULE_HISTORY.md).

## Common Commands

```bash
uv run python scripts/import_cardsfolder.py                              # Import fresh DB
uv run python scripts/recommend.py --commander "Korvold, Fae-Cursed King" --top 30 --explain
uv run pytest tests/                                                     # ~1230 tests, ~1-2s
uv run python scripts/gap_report.py                                      # Ranked list of coverage gaps — next rule to add

# Unified eval harness (bench.py) — replaces _audit_rule_impact /
# golden_set_track / compare_edhrec / weight_grid_search / broad_set_track.
# See docs/brainstorms/2026-04-21-unified-eval-harness-requirements.md.
uv run scripts/bench.py audit                                            # Compare live scoring vs pinned baseline, emit verdict + .audit/last.md
uv run scripts/bench.py audit --rule RULE_ID                             # Per-rule ablation summary (SQL over persisted tensor; <2s)
uv run scripts/bench.py audit --inspect RULE_ID --limit 20               # Top contribution rows for RULE_ID
uv run scripts/bench.py audit --collinearity                             # Pairwise VIF + Pearson correlation across rules
uv run scripts/bench.py audit --expect-identity                          # Assert bitwise-identical scores (for pure refactors)
uv run scripts/bench.py audit --repin --yes                              # Rebuild pinned fixture from current working tree
```

The bench.py hook also runs advisorily on pre-commit when edits touch
`complement_rules/`, `universal_scorer.py`, or `graph_engine.py`; see
`memory/feedback_audit_every_change.md` for the guardrail.

## Data Model

Port extraction: 108,644 ports from 32,327 cards (GenericChoice +
StaticAbilities$ expansion, deduped after A1's 2^N re-walk fix).

**Extra `port_attributes`** (beyond standard valid_filter explosion):
- `attr_kind='change_type'` for ChangeZone effect ChangeType$ clauses
  (Kaalia's Angel/Demon/Dragon cheat-into-play list).
- `attr_kind='token_color'` + `attr_kind='token_subtype'` for every TokenScript
  produced by a Token effect (multi-color prefixes like `gw`, `all`; and
  artifact-creature format like `c_0_1_a_thopter`).

**`card_hints` table** — normalised projection of Forge's AI annotations
(`DeckNeeds`/`DeckHints`/`DeckHas` → kind `needs`/`hints`/`has`,
`BuffedBy` SVar → kind `buffed_by`). Populated by the importer. Not yet
used by any complement rule — exploratory rules (`deck_hint_match`,
`deck_needs_fulfilled`, `buffed_by_match`) were prototyped and reverted
after each regressed NDCG@30 against EDHREC (curated matches are too
broad and dilute the mechanical-port signal). Retained as data
infrastructure for future work.

**Legality filter** — `cards.legal_commander` is populated from
Scryfall's `legalities.commander` (1 = legal, 0 = not_legal / banned).
Rows with `legal_commander=0` (~1,679, mostly silver-border/acorn/
Unfinity leakage through the Forge cardsfolder) are hard-dropped by
`SynergyEngine.page()` and `legal_cards()` before scoring. Test
fixtures without the column default to 1.

## Scoring Architecture — Universal Port Matcher

Score = count of distinct mechanical interactions between commander ports
and candidate ports, weighted by specificity (IDF). **No hand-tuned weights.
No global penalties.** The commander's ports ARE the query.

### Complement Rules (`complement_rules/`)

Each rule wraps an existing mechanical map and declares when a
(commander_port, candidate_port) pair creates a synergy. Rules group into
primitives (trigger_effect, cost_feeds_trigger, resonance family), density
rules (spell/scaling/tribal/etb_self), archetype rules (voltron, go_wide,
combat_enhancer, graveyard_filler), and gated axis-feeders
(`<axis>_feeder`: counter, modified, cardpower, tap_type, hand_size,
gy_fuel, lifegain, life_total, land_bounce, …).

Full rule catalogue, per-rule gate logic, and IDF weighting details:
see [docs/COMPLEMENT_RULES.md](docs/COMPLEMENT_RULES.md).

### Algorithm

1. Load commander ports (cached)
2. For each complement rule, find matching candidate ports (2 SQL queries)
3. For card-attribute rules, match against cards table
4. Compute IDF weights from candidate frequency
5. Score each candidate = sum of IDF-weighted synergy - anti-synergy + staple bonus
6. Sort by (-score, cmc, edhrec_rank, name)

### Key Files

- `complement_rules/` — rule helpers + registry, `find_all_complements()`
- `universal_scorer.py` — IDF computation, `score_all_universal()`
- `engine.py` — `SynergyEngine.page()`, public API
- `graph_engine.py` — `EVENT_MATCH_MAP`, `COST_FEEDS_TRIGGER`, port matching primitives

## Conventions

- Cards keyed by Scryfall `oracle_id`.
- SQL fragment interpolation guarded by `_VALID_*_EXPRS` frozensets +
  `ValueError` (never `assert` — stripped by `python -O`).
- Rule additions follow the gap_report → scaffold → audit workflow. See
  [docs/RULE_PLANNING.md](docs/RULE_PLANNING.md) for the full pipeline.

## graphify

This project has a graphify knowledge graph at graphify-out/.

Rules:
- Before answering architecture or codebase questions, read graphify-out/GRAPH_REPORT.md for god nodes and community structure
- If graphify-out/wiki/index.md exists, navigate it instead of reading raw files
- After modifying code files in this session, run `graphify update .` to keep the graph current (AST-only, no API cost)
