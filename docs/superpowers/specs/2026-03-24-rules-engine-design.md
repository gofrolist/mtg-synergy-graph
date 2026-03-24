# MTG Rules Engine — Design Spec

> **Goal:** Build a deterministic, offline rules engine that parses MTG oracle text into structured ASTs, builds a universal causal interaction graph, and discovers N-card combo chains — replacing the current LLM-based mechanics extraction with a zero-cost, infinitely scalable parser.

## Design Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Primary goal | Both combo discovery AND synergy precision | Engine should improve pairwise scoring AND find multi-card combos |
| Cost model depth | Full resource model (CMC + activation + alternative costs + reductions) | Loop detection requires knowing if a chain is resource-positive |
| Parse accuracy vs coverage | Coverage-gated by EDHREC rank (top 5000 first) | Most obscure cards never appear in recommendations |
| Scaling representation | Qualitative tiers (linear/multiplicative/exponential) | Enough to rank Hardened Scales vs Doubling Season without full simulation |
| Graph storage | Universal edges + commander-specific scoring at query time | Causal relationships are card-to-card facts; commander context is a filter |
| Parsing approach | Hybrid regex pipeline + template library | Regex handles 95% regular oracle text; templates handle the 5% weird cases |
| Existing mechanics data | Clean slate — delete LLM-extracted mechanics, re-parse everything deterministically | One system, no ambiguity |

## Architecture Overview

```
Oracle text (Scryfall)
    → Oracle Parser (deterministic, regex + templates)
        → AST per ability (triggers, effects, costs, filters)
    → Rules Engine (verb resolvers)
        → StateChanges per effect (what game events occur)
    → Graph Builder (indexed matching)
        → interaction_edges table (triggers/feeds/amplifies/enables)
    → Chain Discoverer (DFS + resource tracking)
        → N-card chains + infinite loops
    → Recommendation Pipeline (existing, enhanced)
        → causal_score integrated into scoring formula
```

All layers are deterministic, offline, and $0 cost. Parse once per set release (~300 new cards), rebuild graph in seconds, query in milliseconds.

---

## Layer 1: Oracle Text Parser

Converts raw oracle text into structured ASTs via a 4-pass pipeline:

```
Raw oracle text
    → Pass 1: SPLIT (separate abilities by newline/chapter/loyalty)
    → Pass 2: CLASSIFY (triggered/activated/static/replacement/keyword)
    → Pass 3: EXTRACT (trigger event + filter, effect verb + target + amount, cost breakdown)
    → Pass 4: RESOLVE (cross-references: "that creature", "it", "those cards")
    → AST per ability
```

### AST Schema

```python
@dataclass
class Trigger:
    event: str              # "enters_the_battlefield", "dies", "deals_damage", ...
    subject: ObjectFilter   # WHO/WHAT triggers it
    condition: Condition | None   # structured when possible, raw string fallback

@dataclass
class Condition:
    kind: str               # "count_threshold", "zone_check", "this_turn", "life_threshold", "raw"
    what: str | None        # "creatures you control", "life total"
    comparator: str | None  # ">=", "<=", "=="
    value: int | None       # 3
    raw: str | None         # unparsed fallback for complex conditions
    restrictiveness: str    # "none", "mild", "severe" — used by chain finder to penalize/skip

@dataclass
class ObjectFilter:
    card_type: str | None       # "creature", "artifact", "permanent"
    subtype: str | None         # "Goblin", "Aura", "Equipment"
    controller: str | None      # "you", "opponent", "any"
    is_token: bool | None
    is_another: bool | None
    power_cmp: tuple | None     # (">=", 4)
    has_keyword: str | None     # "flying"
    zone: str | None            # "battlefield", "graveyard"
    # extensible — new fields for new set mechanics

@dataclass
class Cost:
    mana: str | None        # "{2}{G}"
    tap: bool               # {T}
    sacrifice: ObjectFilter | None   # "sacrifice a creature" → what kind
    pay_life: int | None    # "pay 3 life"
    discard: ObjectFilter | None     # "discard a card"
    exile: ObjectFilter | None       # "exile a card from graveyard"
    other: str | None       # unparsed cost text

@dataclass
class Effect:
    verb: str               # "create", "destroy", "draw", "deal_damage", ...
    target: ObjectFilter | None
    amount: Amount          # fixed int, "X", or ScalesWith
    token: TokenDef | None  # for "create" effects
    keyword: str | None     # for "grant_keyword" effects
    destination: str | None # zone: "hand", "battlefield", "exile"

@dataclass
class Amount:
    value: int | str        # 2, "X", "*"
    scales_with: ScalesWith | None

@dataclass
class ScalesWith:
    what: str               # "Goblins you control", "cards in graveyard"
    how: str                # "linear", "multiplicative", "exponential"

@dataclass
class TokenDef:
    card_type: str          # "creature", "artifact"
    subtype: str | None     # "Goblin", "Treasure"
    power: int | None
    toughness: int | None
    keywords: list[str]
    color: str | None

@dataclass
class Ability:
    kind: str               # "triggered", "activated", "static", "replacement", "keyword", "trigger_modifier"
    trigger: Trigger | None
    cost: Cost | None
    effects: list[Effect]
    replacement_of: Effect | None   # what it replaces (for replacement effects)
    scope: ObjectFilter | None      # who it applies to (for static abilities)
    scaling: ScalesWith | None
    restrictions: Restrictions | None

@dataclass
class Restrictions:
    once_per_turn: bool     # "activate only once each turn"
    sorcery_speed: bool     # "activate only as a sorcery"
    once_per_game: bool     # "use this ability only once"
    your_turn_only: bool    # "only during your turn"
```

### Pass 1-2: Split and Classify

Extends existing `ability_parser.py` patterns:
- Split on `\n`, detect Saga chapters (`I —`, `II —`), planeswalker loyalty (`+1:`, `-3:`)
- Classify by leading pattern: `When/Whenever/At` → triggered, `{cost}:` → activated, `If...would...instead` → replacement, standalone keyword → keyword, else → static
- Strip reminder text in parentheses, but parse it for keyword decomposition

### Pass 3: Extract

Regex layers for each component:

**Trigger extraction** (~25 event patterns):
```
"Whenever a Goblin creature enters the battlefield under your control"
→ event: "enters_the_battlefield"
→ subject: ObjectFilter(card_type="creature", subtype="Goblin", controller="you")
```

1. Match event phrase ("enters the battlefield", "dies", "deals combat damage to a player", etc.)
2. Extract noun phrase before the event → parse into ObjectFilter
3. Extract trailing conditions ("under your control", "from your graveyard")

**Effect extraction** (~40 verb patterns):
```
"create two 1/1 red Goblin creature tokens"
→ verb: "create", amount: 2, token: TokenDef(subtype="Goblin", power=1, toughness=1)
```

1. Match verb ("create", "destroy", "draw", "deal", "exile", etc.)
2. Extract amount (number word or digit, or "X")
3. Extract target/object after verb → parse into ObjectFilter or TokenDef

**Cost extraction:**
```
"{2}, {T}, Sacrifice a creature:"
→ mana: "{2}", tap: True, sacrifice: ObjectFilter(card_type="creature")
```

Split on `,` within cost portion, classify each segment by pattern (mana symbols, {T}/{Q}, "sacrifice", "pay N life", "discard", "exile").

### Pass 4: Resolve Cross-References

- Track the most recent ObjectFilter from trigger or previous effect
- Replace "that creature", "it", "those tokens" with the referenced filter
- Example: "When a creature dies, return **it** to the battlefield" → returned object inherits trigger subject's filter

### Template Library

For patterns too complex for regex, match full ability text against pre-built templates:

- **Modal:** `"Choose one —"` → split and parse each mode as separate effect
- **Scaling:** `"for each X you control"` → ScalesWith(what=X, how="linear")
- **Conditional:** `"As long as X, Y"` → static with condition
- **Reminder text:** `"(reminder text)"` → decompose keyword into base verbs
- **Variable cost:** `"where X is the number of..."` → Amount with ScalesWith

Templates are tried after the regex pipeline. Extensible: new set mechanics just need a new template entry.

### Planeswalker Example

```
+1: Create a 3/3 Kavu creature token with trample that's all colors.
−3: Choose up to two target creatures. For each of them, put a number of
    +1/+1 counters on it equal to the number of colors it is.
−6: Return target multicolored card from your graveyard to your hand.
    If that card was all colors, draw a card and create two Treasure tokens.
```

Pass 1 splits on loyalty prefix. Pass 2 classifies all as activated. Pass 3:

- Ability 0: `cost=Cost(loyalty=+1)`, `effects=[Effect(verb="create", token=TokenDef(subtype="Kavu", power=3, toughness=3, keywords=["trample"]))]`
- Ability 1: `cost=Cost(loyalty=-3)`, `effects=[Effect(verb="put_counter", counter_type="+1/+1", amount=Amount(value="X", scales_with=ScalesWith(what="colors of target", how="linear")), max_targets=2)]`
- Ability 2: `cost=Cost(loyalty=-6)`, `effects=[Effect(verb="return", destination="hand", source_zone="graveyard"), Effect(verb="draw", amount=1, condition="target was all colors"), Effect(verb="create", amount=2, token=TokenDef(subtype="Treasure", card_type="artifact"), condition="target was all colors")]`

---

## Layer 2: Rules Engine

Maps parsed Effects to game StateChanges. This is where MTG rules knowledge lives.

### StateChange

```python
@dataclass
class StateChange:
    event: str              # "enters_the_battlefield", "dies", "card_drawn", etc.
    object: ObjectFilter    # WHAT entered/died/was drawn
    zone_from: str | None   # where it came from
    zone_to: str | None     # where it went
    quantity: Amount        # how many times this event fires
    controller: str         # "you", "opponent", "any"
```

### Verb Resolvers

~40 functions mapping Effect verbs to StateChanges:

| Verb | Primary StateChange | Secondary (SBA) |
|---|---|---|
| `create` | `enters_the_battlefield(token)` | `creature_enters` if creature token, `artifact_enters` if artifact |
| `destroy` | `leaves_the_battlefield(target)` | `dies` if creature, `enters_graveyard` |
| `sacrifice` | `leaves_the_battlefield(target)` | `dies` if creature (controller=you), `enters_graveyard` |
| `deal_damage` | `damage_dealt(target)` | `life_lost` if player, `may_die` if creature |
| `draw` | `card_drawn` | `hand_size_increases` |
| `discard` | `card_discarded` | `enters_graveyard` |
| `exile` | `leaves_the_battlefield` | `enters_exile` |
| `return` | zone-dependent | `enters_the_battlefield` if dest=battlefield, `card_drawn`-equivalent if dest=hand |
| `put_counter` | `counter_placed(type)` | — |
| `mill` | `card_enters_graveyard` (×N) | — |
| `gain_life` | `life_gained` | — |
| `lose_life` | `life_lost` | — |
| `search` | — (no state change until card is found) | zone transition when placed |
| `untap` | `untapped` | — |
| `copy` | inherits from copied object | `enters_the_battlefield` if permanent |

### Replacement Effect Registry

Replacement effects don't produce StateChanges — they modify other cards' StateChanges:

```python
@dataclass
class ReplacementRule:
    replaces: str           # which verb/event: "put_counter", "create", etc.
    condition: ObjectFilter  # what objects it applies to
    transform: Callable     # how it modifies the state change
```

Examples:
- **Hardened Scales**: replaces `put_counter` on creatures you control → quantity + 1
- **Doubling Season**: replaces `put_counter` (yours) → quantity × 2; replaces `create` tokens (yours) → quantity × 2
- **Rest in Peace**: replaces `enters_graveyard` → `enters_exile` instead

For graph building, replacement effects are stored as **amplifies** edges, not resolved in real-time. When multiple amplifiers apply to the same effect (e.g., Hardened Scales + Doubling Season on a counter placement), edges are independent and additive for scoring. The chain finder does NOT compute exact stacked replacement math — it flags that multiple amplifiers exist and reflects this in edge count.

### Trigger Modifiers (distinct from replacement effects)

Cards like Panharmonicon, Yarok, Teysa Karlov double triggers rather than replacing events. These are NOT replacement effects — they say "ability triggers an additional time" rather than "if X would happen, instead Y." The parser classifies these as `kind="trigger_modifier"`:

```python
# Panharmonicon: "If a permanent entering the battlefield causes a triggered
# ability of a permanent you control to trigger, that ability triggers
# an additional time."
Ability(
    kind="trigger_modifier",
    scope=ObjectFilter(controller="you"),
    trigger=Trigger(event="enters_the_battlefield"),
    effects=[Effect(verb="double_trigger")]
)
```

In the graph, trigger modifiers create `amplifies` edges to all cards that trigger on the modified event.

### Resource Tracking

Every ability's cost and production are resolved into:

```python
@dataclass
class ManaAmount:
    total: int              # total mana value
    colors: dict            # {"G": 1, "generic": 4} or {"any": 1} for "one mana of any color"
    is_any_color: bool      # True for "add one mana of any color" (Phyrexian Altar, etc.)

@dataclass
class ResourceCost:
    mana: ManaAmount        # structured mana cost
    tap: bool
    sacrifice: ObjectFilter | None
    pay_life: int
    discard: int
    exile_from: str | None
    loyalty: int | None

@dataclass
class ResourceProduction:
    mana: ManaAmount | Amount   # Amount when variable ("add X mana")
    tokens: Amount
    cards: Amount
    life: Amount
    untaps: Amount
```

### Variable Quantity Resolution in Loops

Many productions are `Amount(value="X", scales_with=...)` — not fixed integers. The loop detector uses **symbolic comparison**, not exact arithmetic:

- If production amount is variable and cost is fixed: check if `scales_with` guarantees growth. E.g., Krenko produces "X tokens where X = Goblins you control" and the loop adds Goblins each iteration → **exponential growth**, marked infinite.
- If production ≥ cost symbolically (e.g., "produces N creatures, costs 1 creature, N ≥ 2 given any board state with 2+ Goblins"): mark as **"infinite given sufficient board state"** with a `min_board_requirement` field.
- If production is truly unknown or conditional: mark as **"potential loop, unverified"** — flagged for manual review but not reported as confirmed infinite.

```python
@dataclass
class LoopAnalysis:
    is_infinite: str        # "confirmed", "conditional", "potential"
    min_board_requirement: str | None   # "2+ Goblins on battlefield"
    resource_deltas: dict   # per-resource net change per cycle
    growth_pattern: str     # "exponential", "linear", "fixed"
```

### Trigger Matching

Core function — does a StateChange match a Trigger?

```python
def trigger_matches(trigger: Trigger, state_change: StateChange) -> bool:
    if trigger.event != state_change.event:
        return False
    if not filter_matches(trigger.subject, state_change.object):
        return False
    if trigger.from_zone and trigger.from_zone != state_change.zone_from:
        return False
    return True
```

Zone-aware: `dies` = battlefield→graveyard specifically, not exile→graveyard. Filter-aware: "whenever a Goblin enters" doesn't fire on Elf tokens.

---

## Layer 3: Interaction Graph Builder

Builds the universal edge set from parsed abilities. Precomputed, stored in SQLite.

### Edge Types

| Edge | Meaning | Example |
|---|---|---|
| `triggers` | A's effect produces StateChange matching B's trigger | Krenko creates Goblins → Purphoros triggers on creature-enters |
| `feeds` | A produces resource that B consumes as cost | Phyrexian Altar produces mana → Krenko uses mana |
| `amplifies` | A's replacement/static multiplies B's output | Doubling Season doubles Krenko's tokens |
| `enables` | A's static makes B's ability usable or better | Thousand-Year Elixir gives Krenko haste for tap ability |

### Edge Schema

```python
@dataclass
class Edge:
    source: str             # oracle_id of card A
    target: str             # oracle_id of card B
    edge_type: str          # "triggers", "feeds", "amplifies", "enables"
    ability_index_a: int    # which ability on card A
    ability_index_b: int    # which ability on card B
    strength: float         # 0.0 - 1.0 base confidence
    detail: EdgeDetail

@dataclass
class EdgeDetail:
    event: str | None           # for trigger edges: what event
    resource: str | None        # for feed edges: what resource flows
    verb_modified: str | None   # for amplify edges: what gets amplified
    scaling: str | None         # "linear", "multiplicative", "exponential"
    filter_precision: str       # "exact", "broad", "partial"
```

### Filter Precision Scoring

Not all trigger matches are equal:

| Precision | Example | Strength |
|---|---|---|
| `exact` | Trigger says "Goblin", producer makes Goblin | 1.0 |
| `broad` | Trigger says "creature", producer makes Goblin | 0.6 |
| `unfiltered` | Trigger says "permanent", producer makes anything | 0.3 |
| `partial` | Trigger says "nontoken creature", producer makes token | 0.0 (no match) |

### Build Algorithm

1. **Index pass**: For all parsed cards, index by what they produce (event type), respond to (trigger event), modify (replacement verb), and consume (cost resource type)
2. **Match pass**: For each event type, cross-match producers × responders with filter checking. O(N) indexing + O(edges) matching.
3. **Dedup + merge**: Combine multiple edges between same card pair into strongest per type.

### Storage

```sql
CREATE TABLE interaction_edges (
    source_id    TEXT NOT NULL,
    target_id    TEXT NOT NULL,
    edge_type    TEXT NOT NULL,
    ability_a    INTEGER NOT NULL,
    ability_b    INTEGER NOT NULL,
    strength     REAL NOT NULL,
    detail       TEXT NOT NULL,     -- JSON
    PRIMARY KEY (source_id, target_id, edge_type, ability_a, ability_b)
);

CREATE INDEX idx_edges_source ON interaction_edges(source_id);
CREATE INDEX idx_edges_target ON interaction_edges(target_id);
CREATE INDEX idx_edges_type ON interaction_edges(edge_type);

CREATE TABLE parsed_abilities (
    oracle_id      TEXT NOT NULL,
    ability_index  INTEGER NOT NULL,
    kind           TEXT NOT NULL,
    ast_json       TEXT NOT NULL,
    cost_json      TEXT NOT NULL,
    production_json TEXT NOT NULL,
    PRIMARY KEY (oracle_id, ability_index)
);
```

### Size Estimate

Top 5000 cards: ~250k-500k edges, ~50-100MB in SQLite. Build time: seconds.

### Incremental Update

New set (~300 cards): parse new cards, compute edges against existing pool (300 × 5000 = 1.5M pair checks, indexed), insert new edges. Seconds, not minutes.

---

## Layer 4: Chain Discovery & Combo Detection

### Chain Types

```python
@dataclass
class Chain:
    cards: list[str]        # oracle_ids in causal order
    edges: list[Edge]       # connecting edges
    chain_type: str         # "linear", "loop", "amplified"
    output: str             # "infinite_damage", "infinite_tokens", etc.
    resource_delta: ResourceDelta
    bottleneck: str | None  # weakest link

@dataclass
class ResourceDelta:
    mana: int | str
    cards: int
    life: int
    creatures: int
    is_positive: bool       # can this sustain itself?
```

### Algorithm 1: Linear Chains

DFS from commander through `triggers`/`feeds` edges, up to depth 5. A chain is valuable when it terminates in a high-impact effect (deal damage to opponents, draw many cards, create many tokens).

### Algorithm 2: Loop Detection

DFS with back-edge detection. When a cycle is found, compute resource flow using `LoopAnalysis`:

- Fixed quantities: simple arithmetic (production - cost per cycle)
- Variable quantities: symbolic comparison (see "Variable Quantity Resolution" above)
- Abilities with `restrictions.once_per_turn=True`: immediately disqualify from infinite loops (can only fire once per turn, so the cycle cannot repeat unboundedly)
- Abilities with `restrictions.sorcery_speed=True`: flag the loop as requiring main phase + empty stack
- Conditions with `restrictiveness="severe"`: penalize loop confidence or mark as "potential"

**Example — Krenko + Phyrexian Altar + Thornbite Staff:**

| Step | Cost | Production | Event |
|---|---|---|---|
| Krenko activates | tap | X Goblin tokens | creature_enters × X |
| Sacrifice Goblin to Altar | 1 creature | 1 mana | dies |
| Thornbite Staff triggers on dies | — | untap Krenko | untapped |

Net per cycle: creatures +(X-1), mana +1, Krenko untapped → infinite (X grows each iteration).

### Loop Output Classification

Based on what grows unbounded: `infinite_tokens`, `infinite_mana`, `infinite_damage`, `infinite_draw`, `infinite_life_drain`, or combinations.

Side-effect detection: if any card in the loop deals damage or drains life as part of its trigger/effect, the loop includes that output even though it's not the primary resource flow.

### Chain Ranking

```
score = (6 - card_count) × 3.0          # fewer cards = better
      + overlap_with_deck × 5.0          # pieces already owned
      + (4.0 if loop else 0)             # loops > linear chains
      + output_power × 3.0              # damage > tokens > mana
      + avg_edge_strength × 2.0          # precise connections > broad
      + max(0, 3.0 - total_cmc / 5.0)   # cheaper = more practical
```

### Spellbook Validation

Cross-reference discovered loops against the 82k Commander Spellbook combos. Combos found by the engine but NOT in Spellbook are flagged as **novel discoveries**.

---

## Layer 5: Integration with Existing Pipeline

### What Replaces What

| Current | New | Notes |
|---|---|---|
| `ability_parser.py` | `mtg_synergy/parse/oracle_parser.py` | Deeper regex + templates |
| `extract_mechanics.py` | DELETED | Parser replaces LLM extraction |
| `mechanics_matcher.py` | `mtg_synergy/causal/graph_query.py` | Graph queries replace pattern matching |
| `card_mechanics` table | `parsed_abilities` + `interaction_edges` tables | Richer schema |

### What Stays Unchanged

- Tag graph (provides/wants edges) — complementary signal
- LLM scores (`synergy_scores` table) — top-tier signal
- Tower model — instant scoring for unseen commanders
- EDHREC comparison — validation benchmark
- All deck configs, Spellbook data, strategy detection
- CLI entry point (`synergy_graph.py`)

### Updated Scoring Formula

```
score = LLM × 1000
      + causal × CAUSAL_WEIGHT   ← NEW (start at 50, tune upward after validation)
      + EDHREC_syn × 200
      + overlap × 20
      + tower × 10
      + rank × 0.1
```

**Weight tuning protocol:** Start `CAUSAL_WEIGHT` at 50 (below EDHREC at 200). Run `compare_edhrec.py` across all 14 decks. Only promote the weight after confirming the causal signal does not regress the 14.9/30 EDHREC average. Target: increase to 200-500 only when the signal demonstrably improves alignment.

Where `causal` combines:
- Direct edges to/from commander (weighted by edge type: triggers=2.0, amplifies=1.8, feeds=1.5, enables=1.0)
- Chain participation (chain score / chain length)
- Deck interaction count (how many existing deck cards have edges to candidate)
- Capped at 10.0

### Hardened Scales vs Doubling Season

The causal signal naturally handles this:
- **Hardened Scales** (CMC 1): many `amplifies` edges to counter-placing cards, low cost → high causal score in aggro counter decks
- **Doubling Season** (CMC 5): fewer but stronger `amplifies` edges (doubles counters AND tokens), high cost → higher causal score in slower, token-heavy decks
- Commander context at query time shifts the balance based on what the commander produces

### File Structure

```
mtg_synergy/
├── parse/                    ← NEW
│   ├── __init__.py
│   ├── oracle_parser.py      # Pass 1-4 pipeline
│   ├── templates.py          # Template library for complex patterns
│   ├── ast_types.py          # Ability, Effect, Trigger, Cost dataclasses
│   └── verb_resolvers.py     # Effect → StateChange mapping
├── causal/                   ← NEW
│   ├── __init__.py
│   ├── graph_builder.py      # Build interaction_edges from parsed abilities
│   ├── graph_query.py        # Query edges, find chains, filter by commander
│   ├── chain_finder.py       # DFS chain discovery + loop detection
│   └── resource_flow.py      # ResourceCost/Production tracking
├── graph/                    # UNCHANGED
├── recommend/
│   ├── engine.py             # MODIFIED — adds causal_score
│   ├── scoring.py            # MODIFIED — replaces mechanics with causal
│   └── ...
└── ...
```

### CLI Commands

```bash
# Parse top 5000 cards
python3 oracle_parser.py --parse-all --top 5000

# Parse single card (testing)
python3 oracle_parser.py --card "Hardened Scales" --verbose

# Build interaction graph
python3 build_graph.py --rebuild

# Update graph with new set
python3 build_graph.py --update --new-cards data/new_set.json

# Find chains for a commander
python3 chain_finder.py --commander "Krenko, Mob Boss" --max-depth 5

# Existing recommendation (now uses causal signal)
python3 synergy_graph.py --deck krenko --recommend
```

### Migration Path

1. Build `parse/` and `causal/` as new packages — zero changes to existing code
2. **Parse coverage gate:** Run parser on top 5000 cards, measure coverage. Must reach ≥85% abilities parsed correctly before proceeding. If not, iterate parser until threshold met.
3. **Transition period:** Keep LLM mechanics (`card_mechanics` table) for cards the deterministic parser cannot handle. Add `source` column ("deterministic" vs "llm") to distinguish. The causal graph uses deterministic data where available, falls back to LLM mechanics otherwise.
4. Add `causal_score()` to `engine.py` behind `--use-causal` flag
5. Run `compare_edhrec.py` with/without causal to measure improvement. Minimum bar: do not regress 14.9/30 average across 14 decks.
6. Once validated AND deterministic coverage exceeds 95% of top 5000: make causal the default, delete LLM mechanics data

### Known Limitations (explicitly out of scope)

- **Split-second, "can't be countered"**: Not modeled. Relevant for competitive play but not for synergy/combo discovery.
- **Stack interaction order (APNAP)**: Not simulated. The engine finds causal chains but does not verify optimal stack ordering.
- **Layer system (timestamps, dependencies)**: Static ability interactions in the layer system are too complex for deterministic modeling. The engine treats static abilities as always-on within their scope.
- **DFS depth limit of 5**: Some combos involve 5+ non-commander cards. Depth 5 from the commander covers up to 4-card combos where the commander participates, or 5-card combos where the commander is the root. Combos where the commander is not directly involved need all pieces within depth 5 of each other. Known limitation, acceptable as starting point.

---

## Testing Strategy

### Unit Tests

**Parser (`tests/test_oracle_parser.py`):** ~50 cards covering all ability types:
- Triggered (Purphoros, Blood Artist, Rhystic Study, Syr Konrad)
- Activated (Krenko, Phyrexian Altar, Birthing Pod, Necropotence)
- Static (Hardened Scales, Doubling Season)
- Replacement (Anointed Procession, Rest in Peace)
- Trigger modifier (Panharmonicon, Yarok, Teysa Karlov)
- Keywords with reminder text (discover, connive, amass)
- Planeswalker (Jared Carthalion, Nissa)
- Saga (Urza's Saga, The Eldest Reborn)
- Modal (Austere Command, Cryptic Command)
- DFC (Delver of Secrets, Fable of the Mirror-Breaker)
- Complex costs (K'rrik, Bolas's Citadel)
- Scaling (Krenko=exponential, Purphoros=linear, Cathar's Crusade=linear)
- Restrictions (once_per_turn, sorcery_speed abilities)
- Conditions (count thresholds, "this turn" qualifiers)

**Rules engine (`tests/test_rules_engine.py`):**
- Verb resolvers produce correct StateChanges
- Trigger matching with exact/broad/no-match filters
- Zone-aware matching (dies vs exiled vs bounced)
- Replacement effect interaction
- Trigger modifiers (Panharmonicon creates amplifies edges, not trigger edges)
- Mana color tracking (any-color vs colorless vs specific colors)

**Graph builder (`tests/test_graph_builder.py`):**
- Known synergistic pairs produce correct edge types
- Non-synergistic pairs produce NO edges

**Chain finder (`tests/test_chain_finder.py`):**
- Known combos are rediscovered (Krenko + Altar + Staff, Prossh + Food Chain, Niv-Mizzet + Curiosity)
- Resource flow computed correctly (fixed and variable quantities)
- False combos (resource-negative cycles) rejected
- "Once per turn" abilities do NOT form infinite loops
- Conditional triggers with `restrictiveness="severe"` flagged as "potential" not "confirmed"
- Mana color requirements validated (colored cost needs matching colored production)

### Integration Tests

**Spellbook validation:** For combos involving the 15 deck commanders, measure recall (what % rediscovered) and review novel discoveries.

**EDHREC regression:** `compare_edhrec.py` before/after integration. Target: improve 14.9/30 average or at minimum not regress.

### Coverage Tracking

```bash
python3 oracle_parser.py --stats
# Shows: cards parsed, parse rate, accuracy, abilities extracted, edges built, loops found, novel combos
```
