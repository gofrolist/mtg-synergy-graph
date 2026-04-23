---
date: 2026-04-23
spike: BoosterDraft pair-scorer Python port feasibility
plan: docs/plans/2026-04-23-002-feat-forge-second-oracle-plan.md (Unit 2 gate)
forge_sha: ed97d9bb77f03d9681aba59186416bcf7923d5dd
verdict: GO
confidence: high
---

# BoosterDraftAI Port Feasibility Spike — GO

## Verdict

**GO, high confidence.** Unit 3 (Python port) can proceed.

**Chosen method:** `forge.gamemodes.limited.CardRanker.getScoreForDeckHints(PaperCard card, Iterable<PaperCard> otherCards)` at `data/forge/forge-gui/src/main/java/forge/gamemodes/limited/CardRanker.java:175-206`.

**Estimated Python LOC:** 100-150 (hard cap from origin FR5: 500).

**Blockers:** None.

## Why not the originally hypothesized symbols

The brainstorm hypothesized `BoosterDraftAI.rateCard`, `CardSynergy.getSynergy`, or `DeckgenUtil.*` as the candidate methods, and pointed at `forge-ai/` as the module. Reality:

- **`BoosterDraftAI.java`** exists at `forge-gui/src/main/java/forge/gamemodes/limited/BoosterDraftAI.java` (not `forge-ai/`), and is only 110 LOC — a thin wrapper delegating all ranking to `CardRanker.rankCardsInPack()`.
- **`CardSynergy.java`** does not exist anywhere in upstream Forge.
- **`DeckgenUtil.java`** exists at `forge-gui/src/main/java/forge/deck/DeckgenUtil.java` but contains only deck-generation methods (`buildCardGenDeck`, `getRandomPreconDeck`, etc.) — no pair-scoring surface.
- **`CardRanker.java`** (223 LOC total) is the real home of the pair-scoring logic.

## The actual pair scorer

`CardRanker.getScoreForDeckHints` takes a card A and an iterable of "other cards," and returns a synergy contribution score. The core formula:

```
score = 0
for each other card B:
    if B has valid DeckHints:
        matches_by_type = B.DeckHints.filterByType([A])
        for each (type, matches) in matches_by_type:
            score += |matches| * typeFactors[type]

if A has valid DeckNeeds:
    matches_by_type = A.DeckNeeds.filterByType(otherCards)
    for each (type, matches) in matches_by_type:
        shortfall = max(typeThresholds[type] - |matches|, 0)
        score -= (shortfall / typeThresholds[type]) * typeFactors[type]

return score
```

Where:

| Enum type | `typeFactors` | `typeThresholds` |
|---|---|---|
| ABILITY | 3 | 5 |
| COLOR | 1 | 10 |
| KEYWORD | 3 | 8 |
| NAME | 10 | 2 |
| TYPE | 3 | 8 |

Both are `ImmutableMap` instances at the top of `CardRanker.java:19-35`.

## Dependency classification

| Java dependency | Purpose | Python equivalent | Our DB has it? |
|---|---|---|---|
| `card.getRules().getAiHints().getDeckHints()` | card's DeckHints SVar as a parsed object | `card_hints(kind='hints', category=T, value=V)` rows | **Yes** — `src/mtg_synergy_graph/importer.py` populates `card_hints` from Forge's `DeckHints:` annotation. |
| `card.getRules().getAiHints().getDeckNeeds()` | card's DeckNeeds SVar | `card_hints(kind='needs', category=T, value=V)` rows | **Yes** — same importer path. |
| `DeckHints.filterByType(cardList)` | filter a card list against one hint pattern | Straight port: `_filter_by_type(card_rules, hint_rows) -> dict[Type, list[oracle_id]]` | Data already decomposed — no `SVar$param` parsing needed in our port. |
| `CardRulesPredicates.deckHas(type, ability)` | predicate matching "card contributes to type/ability" | SQL `card_hints` join by `(kind='has', category, value)` OR direct lookup into `cards.{keywords,types,color_identity}` | **Yes** — columns populated. |
| `ColorSet.fromNames` / `.isColor` | color predicate | `cards.color_identity` field + a small `_color_matches(ci, target)` helper | **Yes** — `color_identity` column populated. |
| `CardRulesPredicates.hasKeyword(p)` | keyword presence | `cards.keywords LIKE '%p%'` or list-contains helper | **Yes** — `keywords` column is a JSON array (per existing parser). |
| `CardType.isACreatureType(p)` + `hasKeyword("Changeling")` | tribal-creature-type detection | Use a stable local creature-type list + keyword lookup | **Yes** — subtypes + keywords columns. |
| `PaperCard` / `CardRules` types | data classes (not runtime `Card` subclasses) | Pass `oracle_id`-keyed dict rows | Not needed as a class; our port works on oracle_id strings + DB rows. |
| `TokenDb` / `StaticData.instance()` | token resolution inside `rulesWithTokens` | Optional — FR1 MVP skips token recursion. Phase-2 tuning can add it via `port_attributes` (attr_kind='token_subtype'/'token_color'). | Deferred to implementation. |
| `getRawScore` (per-card base via `DraftRankCache`) | per-card CSV ranking, not pair-scoring | Out of scope for this unit. Optional phase-2 additive signal. | CSVs exist in `forge-gui/res/draft/` — can be parsed later if needed. |

**Nothing on this list requires `GameState`, `PlayerAI`, or the runtime `Card` class.** The port is pure-function over local DB rows.

## Bonus finding — pair-scorer data is already in our DB

`CLAUDE.md` describes `card_hints` as populated but "not yet used by any complement rule." Unit 3 becomes its first consumer, and the importer's decomposition (`kind`, `category`, `value` columns) matches Forge's `DeckHints.Type` enum 1:1 (`ABILITY|COLOR|KEYWORD|NAME|TYPE`), so **the port does not need to re-parse Forge's raw `TYPE$param` SVar format**. The cost of adopting `CardRanker` semantics is strictly the scoring math, not the annotation parsing.

## Fidelity-fixture strategy for Unit 3

Unit 3's test-first discipline requires ≥ 10 hand-computed Java reference pair-scores to drive the port. Two viable sources:

1. **Hand-computed from published `DeckHints` values.** Pick 10 well-known card pairs (e.g., Goblin Guide / Goblin King, Counterspell / Jace the Mind Sculptor, Panharmonicon / Mulldrifter) and compute `getScoreForDeckHints` by hand against Forge's `.txt` card annotations. Deterministic, no JVM required. **Recommended for MVP.**
2. **Extract from `forge-gui-desktop/src/test/java/forge/CardRankerTest.java`** — Forge's own unit tests for `CardRanker`. Existing at `forge-gui-desktop/src/test/java/forge/CardRankerTest.java` (noted during sparse-checkout enumeration). Read it to see whether it already publishes `(cardA, cardB, expected_score)` tuples we can re-use.

Plan: Unit 3 uses option 1 primarily; cross-checks against option 2 if `CardRankerTest` is tractable to read without running the JVM.

## Scope confirmation

| Origin success criterion (FR5) | This spike's finding |
|---|---|
| Port ≤ 500 LOC | **Estimated 100-150 LOC** — well under cap. |
| No JVM at runtime | **Confirmed** — no Java/JNI required. |
| No game-state context | **Confirmed** — operates on data-class `PaperCard` only. |
| Deterministic | **Confirmed** — method has no I/O, RNG, or time-dependence. |

## Remaining implementation-time decisions for Unit 3

- How to materialize `PaperCard`-equivalent rows in Python — propose: a small `@dataclass(frozen=True)` loaded from `cards` + `card_hints` via a single JOIN query per `(oracle_id_a, oracle_id_b)` call, with an LRU cache if profiling shows a hotspot (same pattern as `pathway.py`'s `_type_token_set`).
- Whether to include `rulesWithTokens` token recursion — propose: skip in MVP, add as phase-2 tuning flag.
- Output scale — propose: preserve Java's raw scale (values land in roughly -100 to +100 depending on deck context) since downstream consumers (`--vs-forge-oracle` Kendall-τ, `gap_report.py` normalization) are rank-preserving; absolute scale doesn't affect correctness.
- Deferred for phase 2: `getRawScore` per-card CSV signal (optional additive component; decouples cleanly from pair scoring).

## Unblocked units

With this GO verdict, the plan's gated units can proceed:

- Unit 3 (Python port) — gated ✅ unblocked
- Unit 7 (`bench.py audit --vs-forge-oracle`) — depended on Unit 3's `pair_scorer.rate_pair` ✅ unblocked

No changes required to the plan's scope boundaries or sequencing. The degenerate NO-GO path (drop Unit 3 + rescope Unit 7 to `--vs-forge-precon`) is **not** needed.
