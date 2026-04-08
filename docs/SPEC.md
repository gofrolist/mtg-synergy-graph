# MTG Synergy Graph: Architecture Specification

**Version:** 1.2.2
**Date:** 2026-04-07
**Author:** Evgenii + Claude architectural session
**Purpose:** Complete specification for reimplementing mtg-synergy-graph as a deterministic graph engine that recommends Commander cards by parsing Forge DSL mechanics, replacing the current LightGBM model pipeline.

**Changelog from v1.0:**
- Branch-aware SVar chain walking (preserves `True/False/Win/OtherwiseSubAbility$` provenance).
- New `card_ports` columns: `is_conditional`, `branch_kind`, `branch_parent`, `source_svar`, `chain_depth`.
- Expanded `Count$` scaling vocabulary (Power, Toughness, ManaCost, CardManaCostLKI, Remembered, Triggered).
- New §4.5 Penalty Rules, §4.6 Attribute Normalization, §6.8 Causal Graph Metrics, §6.9 Partner/Background Commanders, §7.2 Branch-Aware Weighting, §7.5 Penalty Layer, §10.4 Golden Set regression suite.
- New strategic rules: `mana_sink`, `lki_scaling`, `tribal_density`, `anti_stax_activated_abilities`.
- Realistic validation target reconciled with current LightGBM baseline (§10.1).
- §13 adds an explicit pivot-risk decision record.

**Changelog from v1.2.1:**
- No top-N cap. The engine ranks **every legal card** for every commander
  and exposes a paginated query API. mtg-edh-builder (primary consumer)
  imports this project as an external Python package and requests windows
  via `offset` / `limit`. Per-commander JSON output is still produced for
  debugging, but is no longer the product surface.
- Plain-English explanations are **optional metadata**, not a headline
  feature. The required output is score + contributing ports/triggers/
  abilities; prose strings are generated only when explicitly requested.
  §8 renamed to "Score Breakdown and Optional Explanations."
- §10.4 explanation audit softened: fails only on non-empty explanations
  that are trivial, not on missing explanations.
- New §14 "External Package Contract" documenting the pip-installable API
  surface for mtg-edh-builder.

**Changelog from v1.2:**
- §4.6 `port_attributes` table — Forge filters are exploded into indexed
  attribute rows at Layer 3, so §6.1.3 joins become pure SQL instead of
  Python UDFs inside a 150k-row × 25k-card 2-hop graph traversal.
- §6.1.3 rewritten as an indexed SQL filter-satisfaction query; Python
  `filter_matches()` demoted to unit-test helper.
- §6.9 split into 6.9.1 Colour Identity Union (hard filter), 6.9.2 Port-Set
  Union, and **6.9.3 Internal Synergy Detection** — two-card commanders now
  get an explicit `internal_synergy_score` bucket for cards that feed the
  engine between the two partners (e.g. Tymna ↔ Thrasios combat-damage → draw).
- §7.1 adds `internal_synergy` bucket; §4.3 `synergy_edges` adds
  `internal_synergy_score` column.
- §10.4 Golden Set regression suite — frozen 50-commander list + frozen
  EDHREC snapshot + NDCG tracker + explanation audit + top-10 jitter CI
  check, to prevent "fix one, break five" regressions.

**v1.1 draft regressions rejected in v1.2:**
- v1.1's `parse_forge_line` used `=` instead of `$` — reverted.
- v1.1's line-prefix offsets were off-by-one (`Name` → `line[4:]` instead of `Name:` → `line[5:]`) — reverted.
- v1.1's `parse_deck_hints` dropped the `&`/`$` Forge syntax — reverted.
- v1.1's `filter_matches` replaced `+` with `,` — reverted (Forge uses `+` to separate restrictions).
- v1.1's explanation section tested `replacement > 0` for an anti-synergy warning that is always `<= 0` — v1.2 keeps v1.0's correct `< 0` check.
- v1.1 removed SubAbility recursion from `extract_effect_ports`, silently dropping activated-ability chains — reverted; recursion is retained and now carries branch metadata.
- v1.1 flagged replacement effects as conditional whenever `ValidPlayer$` was set (a scope qualifier, not a gate) — only `Condition$` drives `is_conditional` on R: lines in v1.2.
- v1.1 stripped underscores from SQL/Python identifiers inconsistently — v1.2 keeps snake_case throughout.

---

## 1. Project Goal

Build a system that ranks every legal card for any given Commander by mechanical synergy derived from the Forge DSL rather than popularity data, and exposes the ranking as a paginated query API consumed by mtg-edh-builder (and any future consumer) as an external Python package. The system should:

- Rank **all** legal cards per commander (no hard top-N cap); consumers request arbitrary windows via `offset`/`limit` pagination (see §14).
- Match or exceed the current LightGBM model's top-100 EDHREC overlap on the same 100-commander test set (`scripts/compare_edhrec.py --top 100`). Top-100 remains the evaluation metric for comparability with the current baseline; it is NOT a product-level truncation.
- Surface "hidden gems" — mechanically synergistic cards that EDHREC underranks due to low popularity.
- Detect anti-synergies (cards that fight the commander's strategy).
- For each recommendation, return the **score breakdown** and the concrete contributing ports (triggers / effects / static modes / replacement events) that produced the score. Optional human-readable explanation strings are generated on demand but are not required.
- Work fully offline with zero runtime API calls.
- Support new cards immediately when their Forge script is added (no retraining).

---

## 2. Architecture Overview

The system has 6 layers, executed sequentially at build time. The final output is precomputed JSON per commander.

```
Layer 1: Data Ingestion
   Forge cardsfolder/*.txt → raw card scripts
   Scryfall bulk data → types, colors, CMC, color identity

Layer 2: DSL Parser (Python)
   Parse all T:/A:/S:/R:/K:/SVar lines into structured records

Layer 3: Port Extraction
   Convert parsed records into typed CardPort rows in SQLite

Layer 4: Graph Engine (SQL joins)
   Port matching: trigger↔effect, static↔type, cost↔enabler, scaling↔producer
   Chain detection: 2-hop synergies via intermediate cards
   Anti-synergy: replacement effect conflicts

Layer 5: Score Aggregation
   Weighted formula combining graph scores + strategic heuristics + staples + EDHREC tiebreaker

Layer 6: Output Generation
   Precomputed ranked edges (every legal card) per commander in
   synergy_edges, queryable via paginated package API (§14).
   Optional JSON dumps for debugging are written to output/{slug}.json
   but are not the product surface.
```

### Technology Stack

| Component | Tool | Rationale |
|-----------|------|-----------|
| Language | Python 3.12+ | Existing codebase, uv for package management |
| Storage | SQLite | Offline, zero-ops, fast indexed joins, already in use |
| Parser | Python stdlib `re` + `str.split` | Forge DSL is flat key-value, no grammar library needed |
| Graph queries | SQL joins on `card_ports` table | Port matching is a relational join, not a traversal |
| Cycle detection | NetworkX (optional) | Only for N-card combo discovery feature |
| ML / LLM | None | Not needed — synergy is deterministic from DSL |
| Output | JSON files | Precomputed at build time, zero runtime cost |
| Validation | EDHREC comparison | Measuring overlap, not training |
| Package manager | uv | Already in use |

### What to Remove from Current Codebase

- **LightGBM model** and all training pipeline code
- **mechanics_vectors.py** — replaced by direct port types from DSL
- **forge_features.py / ForgeProfile** — replaced by `card_ports` table queries
- **The ~19M edge training dataset** — not needed
- **EDHREC scraping for training data** — keep only for validation

---

## 3. Data Sources

### 3.1 Forge Card Scripts (Primary)

**Source:** `Card-Forge/forge` repository, `forge-gui/res/cardsfolder/` (zipped as `cardsfolder.zip`)
**Format:** Plain text files, one per card, pipe-delimited key-value DSL
**Coverage:** ~25,000 cards with full mechanical scripting
**Update frequency:** Continuous (new cards added during preview seasons)

The Forge DSL is a closed formal grammar with a bounded vocabulary:
- 146 trigger types (TriggerType.java enum)
- 204 effect factories (AbilityFactory effect classes)
- 68 static ability modes (StaticAbilityMode.java enum)
- ~15 replacement event types
- ~15 cost types

### 3.2 Scryfall Bulk Data (Supplementary)

**Source:** `https://api.scryfall.com/bulk-data`
**Purpose:** Card metadata not in Forge scripts: color identity, legality, EDHREC rank, rarity, set
**Format:** JSON bulk download
**Usage:** Enrichment only — Forge DSL is the primary source for mechanics

### 3.3 Commander Spellbook (Validation/Bonus)

**Source:** `https://commanderspellbook.com/api/`
**Purpose:** Known combo validation — if the graph discovers a combo that Commander Spellbook confirms, boost confidence
**Usage:** Optional bonus signal, not required for core functionality

### 3.4 EDHREC (Validation Only)

**Source:** EDHREC top-cards-for-commander pages
**Purpose:** Validation target — measure recommendation quality
**Usage:** Never used for training or as a scoring input beyond a weak tiebreaker

---

## 4. Database Schema

All tables in a single SQLite database: `synergy.db`

### 4.1 `cards` Table

Stores card identity and metadata. One row per card.

```sql
CREATE TABLE cards (
    name            TEXT PRIMARY KEY,
    mana_cost       TEXT,           -- "2 G G" format
    cmc             REAL,
    types           TEXT,           -- space-separated: "Creature Human Cleric"
    supertypes      TEXT,           -- "Legendary"
    subtypes        TEXT,           -- "Human Cleric"
    card_types      TEXT,           -- "Creature"
    colors          TEXT,           -- "W,U" comma-separated
    color_identity  TEXT,           -- "W,U,B" comma-separated
    power           TEXT,           -- "3" or "*"
    toughness       TEXT,
    loyalty         TEXT,
    keywords        TEXT,           -- JSON array: ["Flying","Haste"]
    oracle_text     TEXT,
    is_commander    BOOLEAN DEFAULT FALSE,
    -- Forge AI synergy hints (free data from card scripting community)
    deck_hints      TEXT,           -- JSON: {"Type": ["Zombie"], "Keyword": ["Flying"]}
    deck_needs      TEXT,           -- JSON: {"Type": ["Human","Warrior"]}
    deck_has         TEXT,           -- JSON: {"Ability": ["Token","Counters"]}
    -- Scryfall enrichment
    edhrec_rank     INTEGER,
    rarity          TEXT,
    set_code        TEXT
);
```

### 4.2 `card_ports` Table

The core table. Stores every mechanical "port" (input/output) of every card. A card with 3 triggers, 2 effects, and 1 static ability has 6 rows.

```sql
CREATE TABLE card_ports (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    card_name       TEXT NOT NULL REFERENCES cards(name),
    port_type       TEXT NOT NULL,   -- 'trigger', 'effect', 'static', 'replacement', 'cost', 'scales_with'
    -- What mechanical primitive this port represents
    event_class     TEXT NOT NULL,   -- Forge enum value: 'ChangesZone', 'DealDamage', 'Continuous', 'ReduceCost', etc.
    -- Filters and qualifiers (nullable — not all ports have all qualifiers)
    valid_filter    TEXT,            -- 'Creature.YouCtrl', 'Card.Green+Elf', etc.
    zone_origin     TEXT,            -- 'Graveyard', 'Library', 'Any'
    zone_destination TEXT,           -- 'Battlefield', 'Hand', 'Exile'
    phase           TEXT,            -- 'Upkeep', 'End of Turn', etc.
    affected_scope  TEXT,            -- For S: lines: 'Card.Self', 'Creature.YouCtrl', 'Creature.Goblin'
    effect_zone     TEXT,            -- Where effect applies: 'Battlefield', 'Graveyard', 'All'
    cost_subtype    TEXT,            -- For costs: 'SubCounter', 'ExileFromGrave', 'TapXType<Cleric>'
    -- Quantitative parameters
    amount          TEXT,            -- NumDmg, NumCards, LifeAmount, CounterNum — stored as text (may be SVar ref)
    counter_type    TEXT,            -- 'P1P1', 'CHARGE', 'LOYALTY'
    -- Granted capabilities (for S: lines that grant keywords/abilities/triggers)
    granted_keyword TEXT,            -- 'Flying', 'Haste', etc.
    granted_ability TEXT,            -- SVar name of granted ability
    -- Chain references
    execute_ref     TEXT,            -- SVar name this trigger/replacement executes
    sub_ability_ref TEXT,            -- SubAbility chain reference
    -- Branch provenance (v1.2): preserved losslessly at parse time so
    -- scoring can discount unreliable branches without discarding them.
    is_conditional  BOOLEAN DEFAULT FALSE,
    branch_kind     TEXT DEFAULT 'root',  -- 'root', 'execute', 'subability', 'true', 'false',
                                          -- 'win', 'otherwise', 'repeat', 'change_zone_table',
                                          -- 'static_condition', 'replacement_condition'
    branch_parent   TEXT,            -- parent SVar name (NULL for root)
    source_svar     TEXT,            -- originating SVar name (NULL for inline ports)
    chain_depth     INTEGER DEFAULT 0,
    -- Scaling (for SVar:Count$ patterns)
    scaling_expression TEXT,         -- 'Count$Valid Creature.Black.YouCtrl', 'Count$CardInGraveyard'
    -- Flags
    is_optional     BOOLEAN DEFAULT FALSE,  -- OptionalDecider present
    is_combat       BOOLEAN DEFAULT FALSE,  -- CombatDamage$ True
    is_curse        BOOLEAN DEFAULT FALSE,  -- IsCurse$ True (affects opponents)
    -- For replacement effects
    replacement_event   TEXT,        -- Event$ value: 'DamageDone', 'Mill', 'Draw'
    replacement_result  TEXT,        -- ReplaceWith$ SVar name, or 'Prevent'
    replacement_player  TEXT,        -- ValidPlayer$ scope
    -- Duration
    duration        TEXT,            -- 'EndOfTurn', 'Permanent', 'UntilHostLeavesPlay'
    -- Raw line for debugging
    raw_line        TEXT
);

CREATE INDEX idx_ports_card ON card_ports(card_name);
CREATE INDEX idx_ports_type ON card_ports(port_type);
CREATE INDEX idx_ports_event ON card_ports(event_class);
CREATE INDEX idx_ports_filter ON card_ports(valid_filter);
CREATE INDEX idx_ports_type_event ON card_ports(port_type, event_class);
-- v1.2: support branch-aware queries and scaling lookups
CREATE INDEX idx_ports_conditional ON card_ports(is_conditional, branch_kind);
CREATE INDEX idx_ports_scaling_expr ON card_ports(scaling_expression);
```

### 4.3 `synergy_edges` Table

Precomputed synergy scores. Populated by the graph engine.

```sql
CREATE TABLE synergy_edges (
    commander       TEXT NOT NULL REFERENCES cards(name),
    card            TEXT NOT NULL REFERENCES cards(name),
    total_score     REAL NOT NULL,
    -- Score breakdown for explainability
    port_match_score       REAL DEFAULT 0,
    reverse_match_score    REAL DEFAULT 0,
    internal_synergy_score REAL DEFAULT 0,  -- §6.9.3, two-card commanders only
    chain_score            REAL DEFAULT 0,
    lord_score          REAL DEFAULT 0,
    cost_synergy_score  REAL DEFAULT 0,
    scaling_score       REAL DEFAULT 0,
    replacement_score   REAL DEFAULT 0,  -- negative for anti-synergy
    amplifier_score     REAL DEFAULT 0,  -- Panharmonicon-class effects
    strategic_score     REAL DEFAULT 0,
    staple_score        REAL DEFAULT 0,
    edhrec_score        REAL DEFAULT 0,
    -- Human-readable explanation
    explanation     TEXT,               -- JSON array of reason strings
    PRIMARY KEY (commander, card)
);

CREATE INDEX idx_synergy_commander ON synergy_edges(commander, total_score DESC);
```

### 4.4 `card_svars` Table

Stores SVar definitions for chain-walking.

```sql
CREATE TABLE card_svars (
    card_name   TEXT NOT NULL REFERENCES cards(name),
    svar_name   TEXT NOT NULL,
    svar_value  TEXT NOT NULL,       -- The full SVar line content
    PRIMARY KEY (card_name, svar_name)
);
```

### 4.5 Penalty Rules (post-scoring)

The current LightGBM pipeline applies ~12 post-scoring penalties in
`_apply_penalties()` (`packages/mtg-synergy/src/mtg_synergy/recommend/scoring.py`).
The deterministic engine inherits this rule set unchanged — penalties are NOT
additive score components and are NOT replaced by port matching. They run
after the §7.1 sum and before the final sort. Each rule inspects the
`card_ports` / `cards` rows for the commander and candidate.

| # | Rule | Condition | Multiplier |
|---|------|-----------|------------|
| 1 | Wrong color identity | Candidate has colour pip outside commander's colour identity | hard filter (-1e9) |
| 2 | Background / Doctor's companion | Candidate is a Background but commander has no "Choose a Background", or Doctor's companion without The Doctor | hard filter (-1e9) |
| 3 | Required subtype mismatch | Candidate has static scope `Affected$ Creature.<X>` where `<X>` is not in commander subtypes | ×0.4 |
| 4 | Excluded subtypes | Candidate excludes commander's tribe from its effect (`Creature.non<Tribe>`) | ×0.3 |
| 5 | Wrong token type | Commander is tribal, candidate produces tokens of a different type | ×0.5 |
| 6 | Wrong counter type | Commander pays off `P1P1`, candidate puts `M1M1` / `TIME` / `EXPERIENCE` counters only | ×0.4 |
| 7 | Niche counter penalty | Candidate only handles `TIME` / `EXPERIENCE` / `ENERGY` counters and commander does not use them | ×0.4 |
| 8 | Counters on lands | Counter-commander, candidate puts counters on lands (Earthbend, etc.) | ×0.4 |
| 9 | Non-counter creatures for counter commanders | Counter-commander, candidate creature has no P1P1 signal (no `has_p1p1` and no P1P1 verb) | ×0.6 |
| 10 | Opponent-only replacement for self-mill commander | Candidate has `R:` with `ValidPlayer$ Player.Opponent` for Mill, commander self-mills | ×0.3 |
| 11 | Unmet Type$ need/hint | Candidate `needs` or `hints` a type (e.g. `Type$Dinosaur`) absent from commander profile | ×0.3 |
| 12 | Unmet Ability$ need | Candidate `needs` an ability (e.g. `Ability$LifeGain`) not present in commander's `has` set. Uses `has` only, not `hints` | ×0.85 |

Penalties 1 and 2 are hard filters (the candidate is removed from the
recommendation set). Penalties 3–12 are multiplicative on the aggregated
score from §7.1.

Cross-reference: the existing implementation is the authoritative source for
edge cases. When reimplementing, port `_apply_penalties()` verbatim before
simplifying.

### 4.6 `port_attributes` Table (attribute normalization)

**Problem v1.2 fixes:** v1.0/v1.1 described `filter_matches()` as a Python
function called from inside SQL joins. Calling a Python UDF across the
~150,000-row port table during a 2-hop join is a performance wall — every
join row deserializes the string, runs the regex, and allocates. Profiling
the existing Forge importer shows filter parsing is already the dominant
cost at import time; doing it again at query time is prohibitive.

**Fix:** Explode Forge filters into a many-to-many attribute table at Layer
3, so Layer 4 joins use indexed integer comparisons rather than string
parsing.

```sql
CREATE TABLE port_attributes (
    port_id        INTEGER NOT NULL REFERENCES card_ports(id) ON DELETE CASCADE,
    attr_kind      TEXT NOT NULL,   -- 'type', 'subtype', 'color', 'keyword',
                                    -- 'supertype', 'cmc_cmp', 'power_cmp',
                                    -- 'controller', 'zone', 'counter_type'
    attr_value     TEXT NOT NULL,   -- 'Creature', 'Goblin', 'Red', 'Flying',
                                    -- 'YouCtrl', 'Battlefield', 'P1P1'
    is_negated     BOOLEAN DEFAULT FALSE,  -- filter starts with '!'
    PRIMARY KEY (port_id, attr_kind, attr_value, is_negated)
);

CREATE INDEX idx_port_attr_lookup   ON port_attributes(attr_kind, attr_value, is_negated);
CREATE INDEX idx_port_attr_by_port  ON port_attributes(port_id);
```

Filter explosion rule (Layer 3, during import):

```python
def explode_filter(port_id: int, valid_filter: str) -> list[dict]:
    """Convert a Forge ValidCard$ filter string into port_attributes rows.

    Input : 'Creature.Goblin.YouCtrl+!Black+cmcLE3'
    Output: [
        (port_id, 'type',          'Creature', False),
        (port_id, 'subtype',       'Goblin',   False),
        (port_id, 'controller',    'YouCtrl',  False),
        (port_id, 'color',         'Black',    True),
        (port_id, 'cmc_cmp',       'LE3',      False),
    ]
    """
    if not valid_filter:
        return []
    rows = []
    for segment in valid_filter.replace("+", ".").split("."):
        if not segment:
            continue
        negated = segment.startswith("!")
        token = segment[1:] if negated else segment
        kind = classify_attr_token(token)  # small lookup: types, subtypes,
                                           # colors, keywords, comparators
        rows.append({
            "port_id":    port_id,
            "attr_kind":  kind,
            "attr_value": token,
            "is_negated": negated,
        })
    return rows
```

`classify_attr_token()` uses a precomputed set loaded once at import start
(all Scryfall types, subtypes, colours, keyword abilities). This keeps
Layer 3 deterministic — unknown tokens get classified as `'unknown'` and
logged, never silently dropped.

See §6.1.3 for how Layer 4 consumes this table.

---

## 5. DSL Parser Specification

### 5.1 Input Format

Each card is a `.txt` file with lines in this format:
```
Name:Cathars' Crusade
ManaCost:3 W W
Types:Enchantment
T:Mode$ ChangesZone | ValidCard$ Creature.YouCtrl | Origin$ Any | Destination$ Battlefield | Execute$ TrigPut | TriggerDescription$ ...
SVar:TrigPut:DB$ PutCounter | CounterType$ P1P1 | CounterNum$ 1 | Defined$ Valid Creature.YouCtrl | SubAbility$ DBCleanup
SVar:DBCleanup:DB$ Cleanup | ClearRemembered$ True
Oracle:Whenever a creature enters the battlefield under your control, put a +1/+1 counter on each creature you control.
```

### 5.2 Line Type Routing

```python
def parse_card_file(filepath: str) -> dict:
    """Parse a single Forge card .txt file into structured data."""
    card = {"name": "", "abilities": [], "svars": {}, "keywords": []}

    for line in open(filepath):
        line = line.strip()
        if not line:
            continue

        if line.startswith("Name:"):
            card["name"] = line[5:]
        elif line.startswith("ManaCost:"):
            card["mana_cost"] = line[9:]
        elif line.startswith("Types:"):
            card["types"] = line[6:]
        elif line.startswith("PT:"):
            card["pt"] = line[3:]
        elif line.startswith("Colors:"):
            card["colors"] = line[7:]
        elif line.startswith("Loyalty:"):
            card["loyalty"] = line[8:]
        elif line.startswith("Oracle:"):
            card["oracle"] = line[7:]
        elif line.startswith("DeckHints:"):
            card["deck_hints"] = parse_deck_hints(line[10:])
        elif line.startswith("DeckNeeds:"):
            card["deck_needs"] = parse_deck_hints(line[10:])
        elif line.startswith("DeckHas:"):
            card["deck_has"] = parse_deck_hints(line[8:])
        elif line.startswith("T:"):
            card["abilities"].append(("trigger", parse_forge_line(line[2:])))
        elif line.startswith("A:"):
            card["abilities"].append(("ability", parse_forge_line(line[2:])))
        elif line.startswith("S:"):
            card["abilities"].append(("static", parse_forge_line(line[2:])))
        elif line.startswith("R:"):
            card["abilities"].append(("replacement", parse_forge_line(line[2:])))
        elif line.startswith("K:"):
            card["keywords"].append(line[2:])
        elif line.startswith("SVar:"):
            # Format: SVar:Name:Value
            parts = line[5:].split(":", 1)
            if len(parts) == 2:
                card["svars"][parts[0]] = parts[1]

    return card
```

### 5.3 Core Parser Function

```python
def parse_forge_line(line: str) -> dict:
    """Parse pipe-delimited Forge DSL line into key-value dict.

    Input:  'Mode$ ChangesZone | ValidCard$ Creature.YouCtrl | Origin$ Any'
    Output: {'Mode': 'ChangesZone', 'ValidCard': 'Creature.YouCtrl', 'Origin': 'Any'}
    """
    result = {}
    for part in line.split("|"):
        part = part.strip()
        if "$" in part:
            key, val = part.split("$", 1)
            result[key.strip()] = val.strip()
        elif part:
            # Handle prefix like "AB" or "SP" or "DB" before the $ verb
            # e.g., "AB$ Pump" or "SP$ DealDamage"
            for prefix in ("AB", "SP", "DB", "ST"):
                if part.startswith(prefix + "$ "):
                    result["_prefix"] = prefix
                    result["_verb"] = part[len(prefix) + 2:]
                    break
    return result
```

### 5.4 SVar Chain Walker (branch-aware)

Critical: Forge chains effects via `Execute$` and `SubAbility$` references to SVars.
A card's full effect profile requires recursively following these chains.

Forge chains are not linear: `TrueSubAbility$` / `FalseSubAbility$` branch on
`Condition$`, `WinSubAbility$` / `OtherwiseSubAbility$` branch on Clash
outcomes, and `RepeatSubAbility$` re-enters the same effect list. The walker
must preserve this provenance so later scoring can discount contingent
branches (§7.2) instead of treating them as if they always fire.

```python
from dataclasses import dataclass

# SVar reference keys that can point to sub-abilities, paired with the
# branch_kind label written to card_ports rows.
CHAIN_KEYS = {
    "Execute":             "execute",
    "SubAbility":          "subability",
    "TrueSubAbility":      "true",
    "FalseSubAbility":     "false",
    "WinSubAbility":       "win",
    "OtherwiseSubAbility": "otherwise",
    "RepeatSubAbility":    "repeat",
    "ChangeZoneTable":     "change_zone_table",
}

# Branches whose ports are contingent on a runtime decision.
CONDITIONAL_BRANCH_KINDS = {"true", "false", "win", "otherwise"}


@dataclass
class ChainNode:
    svar_name:      str
    parsed:         dict
    branch_kind:    str
    branch_parent:  str | None
    source_svar:    str
    chain_depth:    int
    is_conditional: bool


def walk_svar_chain(
    entry_name: str,
    all_svars: dict[str, str],
    visited: set[tuple[str, str, str]] | None = None,
    branch_kind: str = "root",
    branch_parent: str | None = None,
    chain_depth: int = 0,
) -> list[ChainNode]:
    """Recursively follow SVar chains, preserving branch metadata.

    Returns one ChainNode per traversed SVar. `visited` tracks the current
    recursion path (keyed by (svar, parent, branch)) instead of a flat set,
    so the same SVar may legitimately be reached via multiple distinct
    branches while still preventing infinite cycles.
    """
    if visited is None:
        visited = set()

    path_key = (entry_name, branch_parent or "", branch_kind)
    if entry_name not in all_svars or path_key in visited:
        return []

    next_visited = set(visited)
    next_visited.add(path_key)

    parsed = parse_forge_line(all_svars[entry_name])
    node = ChainNode(
        svar_name=entry_name,
        parsed=parsed,
        branch_kind=branch_kind,
        branch_parent=branch_parent,
        source_svar=entry_name,
        chain_depth=chain_depth,
        is_conditional=branch_kind in CONDITIONAL_BRANCH_KINDS,
    )
    results = [node]

    for key, child_branch_kind in CHAIN_KEYS.items():
        ref_name = parsed.get(key)
        if not ref_name:
            continue
        results.extend(
            walk_svar_chain(
                ref_name,
                all_svars,
                visited=next_visited,
                branch_kind=child_branch_kind,
                branch_parent=entry_name,
                chain_depth=chain_depth + 1,
            )
        )

    return results
```

### 5.4.1 Conditional Branch Semantics

Ports reached through `TrueSubAbility$`, `FalseSubAbility$`, `WinSubAbility$`,
or `OtherwiseSubAbility$` are still real mechanical outputs and must be
extracted. They are flagged with `is_conditional = TRUE` and the appropriate
`branch_kind`, then weighted down at scoring time rather than dropped at
parse time.

This separation is deliberate: the parser's job is lossless provenance,
scoring's job is reliability. See §7.2 for the multiplier table.

### 5.5 Port Extraction Rules

Each parsed ability line becomes one or more `card_ports` rows:

#### Trigger Lines (T:)
```python
def extract_trigger_ports(card_name: str, parsed: dict, svars: dict) -> list[dict]:
    """Convert a parsed T: line into card_port rows."""
    ports = []

    # The trigger itself is an input port (what the card listens for).
    # A top-level trigger is unconditional w.r.t. SVar branching, so
    # branch_kind='root' / is_conditional=False. Runtime trigger conditions
    # (IntrinsicKeyword$, Condition$) are handled by §4.5 penalties.
    trigger_port = {
        "card_name": card_name,
        "port_type": "trigger",
        "event_class": parsed.get("Mode", ""),
        "valid_filter": parsed.get("ValidCard", parsed.get("ValidSource", "")),
        "zone_origin": parsed.get("Origin", ""),
        "zone_destination": parsed.get("Destination", ""),
        "phase": parsed.get("Phase", ""),
        "is_optional": "OptionalDecider" in parsed,
        "is_combat": parsed.get("CombatDamage", "") == "True",
        "execute_ref": parsed.get("Execute", ""),
        "is_conditional": False,
        "branch_kind": "root",
        "branch_parent": None,
        "source_svar": None,
        "chain_depth": 0,
        "raw_line": str(parsed),
    }
    ports.append(trigger_port)

    # Walk the Execute$ chain. Each node already carries branch metadata;
    # extract_effect_ports propagates it onto every emitted effect port.
    if "Execute" in parsed:
        chain = walk_svar_chain(
            parsed["Execute"], svars,
            branch_kind="execute", branch_parent=None, chain_depth=1,
        )
        for node in chain:
            ports.extend(extract_effect_ports(card_name, node, svars))

    return ports
```

#### Ability/Effect Lines (A:)
```python
def extract_effect_ports(card_name, parsed_or_node, svars: dict) -> list[dict]:
    """Convert a parsed A:/DB$/SP$ line into card_port rows.

    Accepts either a raw parsed dict (for top-level A: lines) or a ChainNode
    (when called from the SVar chain walker). Branch metadata propagates
    onto every emitted port, including cost ports and nested sub-ability
    ports, so scoring can apply §7.2 multipliers uniformly.
    """
    if isinstance(parsed_or_node, ChainNode):
        node = parsed_or_node
        parsed = node.parsed
        branch_kind   = node.branch_kind
        branch_parent = node.branch_parent
        source_svar   = node.source_svar
        chain_depth   = node.chain_depth
        is_conditional = node.is_conditional
    else:
        parsed = parsed_or_node
        branch_kind   = "root"
        branch_parent = None
        source_svar   = None
        chain_depth   = 0
        is_conditional = False

    verb = parsed.get("_verb", parsed.get("DB", parsed.get("SP", parsed.get("AB", ""))))

    port = {
        "card_name": card_name,
        "port_type": "effect",
        "event_class": verb,  # 'Token', 'Draw', 'DealDamage', 'PutCounter', etc.
        "valid_filter": parsed.get("ValidTgts", parsed.get("Defined", "")),
        "zone_origin": parsed.get("Origin", ""),
        "zone_destination": parsed.get("Destination", ""),
        "amount": parsed.get("NumDmg", parsed.get("NumCards", parsed.get("LifeAmount",
                  parsed.get("CounterNum", parsed.get("TokenAmount", ""))))),
        "counter_type": parsed.get("CounterType", ""),
        "granted_keyword": parsed.get("KW", ""),
        "duration": parsed.get("Duration", ""),
        "is_curse": parsed.get("IsCurse", "") == "True",
        "is_conditional": is_conditional,
        "branch_kind": branch_kind,
        "branch_parent": branch_parent,
        "source_svar": source_svar,
        "chain_depth": chain_depth,
        "raw_line": str(parsed),
    }

    # Extract cost ports from Cost$ field, carrying the same branch context.
    cost_ports = extract_cost_ports(
        card_name, parsed.get("Cost", ""),
        branch_kind=branch_kind,
        branch_parent=branch_parent,
        source_svar=source_svar,
        chain_depth=chain_depth,
        is_conditional=is_conditional,
    )

    # Follow SubAbility chains directly attached to this A: line. This
    # recursion is REQUIRED — without it, activated-ability chains (A: lines
    # that are not reached from a trigger's Execute$) silently lose their
    # sub-ability ports. The child branch_kind is taken from CHAIN_KEYS so
    # TrueSubAbility$/FalseSubAbility$ etc. inherit the conditional flag.
    sub_ports = []
    for key, child_branch_kind in CHAIN_KEYS.items():
        ref_name = parsed.get(key)
        if not ref_name:
            continue
        chain = walk_svar_chain(
            ref_name, svars,
            branch_kind=child_branch_kind,
            branch_parent=parsed.get("_svar_self"),
            chain_depth=chain_depth + 1,
        )
        for sub_node in chain:
            sub_ports.extend(extract_effect_ports(card_name, sub_node, svars))

    return [port] + cost_ports + sub_ports
```

#### Static Ability Lines (S:)
```python
def extract_static_ports(card_name: str, parsed: dict) -> list[dict]:
    """Convert a parsed S: line into card_port rows."""
    mode = parsed.get("Mode", "")

    # Phase 1.5b: the full Mode$ value becomes the event_class so tags like
    # Static$Panharmonicon, Static$ReduceCost, Static$Continuous are first-class
    # port keys (no longer smuggled through the verb column).
    has_condition = bool(parsed.get("Condition") or parsed.get("IsPresent"))

    port = {
        "card_name": card_name,
        "port_type": "static",
        "event_class": mode,  # 'Continuous', 'ReduceCost', 'Panharmonicon', 'CantAttack', etc.
        "affected_scope": parsed.get("Affected", ""),
        "effect_zone": parsed.get("EffectZone", ""),
        "granted_keyword": parsed.get("AddKeyword", ""),
        "granted_ability": parsed.get("AddAbility", ""),
        "valid_filter": parsed.get("IsPresent", ""),  # Conditional statics
        "amount": parsed.get("AddPower", ""),  # For lords: +1/+1 etc.
        "is_conditional": has_condition,
        "branch_kind": "static_condition" if has_condition else "root",
        "branch_parent": None,
        "source_svar": None,
        "chain_depth": 0,
        "raw_line": str(parsed),
    }
    return [port]
```

#### Replacement Effect Lines (R:)
```python
def extract_replacement_ports(card_name: str, parsed: dict) -> list[dict]:
    """Convert a parsed R: line into card_port rows.

    `ValidPlayer$` is a SCOPE qualifier (which player's events this watches),
    not a conditional gate — it should never flip `is_conditional`. Only a
    real `Condition$` / `CheckSVar$` gate triggers the conditional flag.
    The opponent-only replacement case is handled by the §4.5 penalty table,
    not by parser-side weighting.
    """
    has_condition = bool(parsed.get("Condition") or parsed.get("CheckSVar"))

    port = {
        "card_name": card_name,
        "port_type": "replacement",
        "event_class": parsed.get("Event", ""),
        "replacement_event": parsed.get("Event", ""),
        "replacement_result": parsed.get("ReplaceWith", "Prevent" if "Prevent" in parsed else ""),
        "replacement_player": parsed.get("ValidPlayer", ""),
        "valid_filter": parsed.get("ValidCard", parsed.get("ValidSource", "")),
        "is_conditional": has_condition,
        "branch_kind": "replacement_condition" if has_condition else "root",
        "branch_parent": None,
        "source_svar": None,
        "chain_depth": 0,
        "raw_line": str(parsed),
    }
    return [port]
```

#### Cost Parsing
```python
# Cost types to detect. ORDER MATTERS: the loop uses substring tests, and
# 'Exile' is a substring of 'ExileFromGrave'/'ExileFromHand'/'ExileFromTop'.
# More-specific patterns must come first, and a cost string may legitimately
# emit more than one cost port (e.g. `Sac<1/Creature>` + tap cost).
COST_PATTERNS = [
    ("ExileFromGrave",  "exile_from_grave"),
    ("ExileFromHand",   "exile_from_hand"),
    ("ExileFromTop",    "exile_from_top"),
    ("Exile",           "exile"),
    ("SubCounter",      "remove_counter"),
    ("AddCounter",      "add_counter"),
    ("tapXType",        "tap_type"),     # tap a creature of specific type
    ("untapYType",      "untap_type"),
    ("Sac",             "sacrifice"),
    ("Discard",         "discard"),
    ("Return",          "return"),
    ("Reveal",          "reveal"),
    ("PayLife",         "pay_life"),
    ("PayEnergy",       "pay_energy"),
    ("Mill",            "mill"),
    ("Exert",           "exert"),
]

def extract_cost_ports(
    card_name: str,
    cost_str: str,
    *,
    branch_kind: str = "root",
    branch_parent: str | None = None,
    source_svar: str | None = None,
    chain_depth: int = 0,
    is_conditional: bool = False,
) -> list[dict]:
    """Parse a Cost$ string into cost port rows.

    Branch context is passed by the caller so cost ports that belong to a
    conditional sub-ability inherit the same multiplier in §7.2.
    """
    if not cost_str:
        return []

    ports = []
    for pattern, cost_type in COST_PATTERNS:
        if pattern not in cost_str:
            continue
        # Extract the subtype from angle brackets: Sac<1/Creature.token>
        subtype = ""
        idx = cost_str.find(pattern)
        bracket_start = cost_str.find("<", idx)
        bracket_end = cost_str.find(">", bracket_start)
        if bracket_start != -1 and bracket_end != -1:
            subtype = cost_str[bracket_start + 1:bracket_end]

        ports.append({
            "card_name": card_name,
            "port_type": "cost",
            "event_class": cost_type,
            "cost_subtype": subtype,
            "is_conditional": is_conditional,
            "branch_kind": branch_kind,
            "branch_parent": branch_parent,
            "source_svar": source_svar,
            "chain_depth": chain_depth,
            "raw_line": cost_str,
        })

    # Detect tap cost ("T" as a standalone token in the cost string)
    if "T" in cost_str.split():
        ports.append({
            "card_name": card_name,
            "port_type": "cost",
            "event_class": "tap",
            "is_conditional": is_conditional,
            "branch_kind": branch_kind,
            "branch_parent": branch_parent,
            "source_svar": source_svar,
            "chain_depth": chain_depth,
            "raw_line": cost_str,
        })

    return ports
```

#### SVar Scaling Extraction
```python
def extract_scaling_ports(card_name: str, svars: dict) -> list[dict]:
    """Extract SVar:Count$ patterns as scaling ports.

    These represent cards that scale with board state. v1.2 recognises a
    broader vocabulary than v1.0 so that big-mana, stompy, and death/LKI
    archetypes can be matched:

        SVar:X:Count$Valid Creature.Black.YouCtrl   → ValidCount
        SVar:Y:Count$CardInGraveyard                → GraveyardSize
        SVar:Z:Count$CardManaCostLKI                → LKIManaCost (death payoffs)
        SVar:P:Count$Power                          → OwnPower (stompy)
        SVar:T:Count$Toughness                      → OwnToughness
        SVar:M:Count$ManaCost                       → OwnManaCost (big mana)
        SVar:R:Count$Remembered                     → RememberedCount
        SVar:TR:Count$Triggered                     → TriggeredCount
    """
    ports = []
    for svar_name, svar_value in svars.items():
        if not svar_value.startswith("Count$"):
            continue
        expression = svar_value[6:]  # Strip "Count$" prefix
        filter_str = ""
        metric = expression

        if expression.startswith("Valid "):
            filter_str = expression.split(" ", 1)[1]
            metric = "Valid"
        elif expression.startswith("CountValid "):
            filter_str = expression.split(" ", 1)[1]
            metric = "CountValid"
        elif expression.startswith("Triggered"):
            metric = "Triggered"
        elif expression in (
            "CardInGraveyard", "CardManaCost", "CardManaCostLKI",
            "Power", "Toughness", "ManaCost", "Remembered",
        ):
            metric = expression

        ports.append({
            "card_name": card_name,
            "port_type": "scales_with",
            "event_class": metric,
            "valid_filter": filter_str,
            "scaling_expression": svar_value,
            "source_svar": svar_name,
            "branch_kind": "root",
            "branch_parent": None,
            "is_conditional": False,
            "chain_depth": 0,
            "raw_line": f"SVar:{svar_name}:{svar_value}",
        })

    return ports
```

The v1 scaling vocabulary the engine must recognise at minimum:

- `Count$Valid ...`
- `Count$CountValid ...`
- `Count$CardInGraveyard`
- `Count$CardManaCost`
- `Count$CardManaCostLKI` (last-known-information, for death triggers)
- `Count$Power`
- `Count$Toughness`
- `Count$ManaCost`
- `Count$Remembered`
- `Count$Triggered`

### 5.6 DeckHints/DeckNeeds/DeckHas Parsing

```python
def parse_deck_hints(hint_str: str) -> dict:
    """Parse DeckHints/DeckNeeds/DeckHas strings.

    Input: 'Type$Zombie & Keyword$Flying'
    Output: {'Type': ['Zombie'], 'Keyword': ['Flying']}

    Input: 'Ability$Token|Counters'
    Output: {'Ability': ['Token', 'Counters']}
    """
    result = {}
    for part in hint_str.split("&"):
        part = part.strip()
        if "$" in part:
            key, val = part.split("$", 1)
            key = key.strip()
            values = [v.strip() for v in val.split("|")]
            result.setdefault(key, []).extend(values)
    return result
```

---

## 6. Graph Engine Specification

### 6.1 Port Matching (Direct Synergy)

The core operation: find cards whose effects match the commander's triggers, and vice versa.

#### 6.1.1 Trigger↔Effect Matching

```sql
-- Cards that FEED the commander (their effects trigger the commander)
SELECT
    e.card_name AS candidate,
    t.event_class AS trigger_event,
    e.event_class AS effect_event,
    t.valid_filter AS trigger_filter,
    1.0 AS match_strength
FROM card_ports t
JOIN card_ports e
    ON e.port_type = 'effect'
WHERE t.card_name = :commander
    AND t.port_type = 'trigger'
    AND e.card_name != :commander
    -- Event class matching rules (see 6.1.2)
    AND matches_event(t.event_class, t.zone_origin, t.zone_destination,
                      e.event_class, e.zone_origin, e.zone_destination)
```

#### 6.1.2 Event Matching Rules

Not all trigger↔effect pairs are simple string equality. The matching function must handle:

| Trigger Event | Matches Effect | Condition |
|---------------|---------------|-----------|
| `ChangesZone` | `Token` | Token creation = creature entering battlefield |
| `ChangesZone` | `ChangeZone` | Zone change effect matches trigger's Origin/Destination |
| `ChangesZone` | `Animate` | Becoming a creature on battlefield |
| `CounterAdded` | `PutCounter` | Counter type must match if specified |
| `CounterAdded` | `Proliferate` | Proliferate adds counters to things that have them |
| `SpellCast` | (any spell) | Card being cast is itself a spell cast event |
| `DamageDone` | `DealDamage` | Direct match |
| `LifeGained` | `GainLife` | Direct match |
| `Sacrificed` | `Sacrifice` | Direct match |
| `Discarded` | `Discard` | Direct match |
| `Drawn` | `Draw` | Direct match |
| `Taps` | `Tap` | Direct match |
| `LandPlayed` | (is a land) | Playing a land card triggers LandPlayed |
| `Attacks` | (is a creature) | Creature attacking triggers Attacks |
| `TapsForMana` | `Mana` | Mana production triggers TapsForMana |

Implement as a Python function that returns a match score (0.0 to 1.0):

```python
# Map of trigger events to their matching effect events
EVENT_MATCH_MAP = {
    "ChangesZone": {
        "Token": lambda t, e: t.get("zone_destination") in ("Battlefield", "", None),
        "ChangeZone": lambda t, e: zones_compatible(t, e),
        "CopyPermanent": lambda t, e: t.get("zone_destination") in ("Battlefield", "", None),
    },
    "CounterAdded": {
        "PutCounter": lambda t, e: counters_compatible(t, e),
        "Proliferate": lambda t, e: True,
    },
    "SpellCast": {
        "*": lambda t, e: True,  # Any card being cast is a spell
    },
    # ... complete mappings for all ~35 trigger types
}
```

The `"*"` key under `SpellCast` is a special catch-all: `match_event()` must
check for it explicitly and return `True` against any effect class, rather
than trying to look up an effect row literally named `*`. The same pattern
applies to `LandPlayed` (matches any land played) and `Attacks` (matches any
creature entering combat) — catch-all rows are a match_event shortcut, not
real effect classes.

#### 6.1.3 ValidFilter Compatibility (SQL attribute joins)

When a trigger has `ValidCard$ Creature.YouCtrl+!Black` and an effect
produces something, we need to check if the produced thing satisfies the
filter. v1.0/v1.1 used a Python UDF for this; v1.2 uses the `port_attributes`
table from §4.6 so the check happens inside the SQL engine at indexed-join
speed.

**Equivalent card-level attributes.** Cards also get a `card_attributes`
view over the `cards` table (types, subtypes, colours, keywords) so a
trigger port's filter attributes can be joined directly against candidate
cards:

```sql
CREATE VIEW card_attributes AS
SELECT name AS card_name, 'type'    AS attr_kind, value AS attr_value FROM cards, json_each(types_json)     WHERE value <> ''
UNION ALL
SELECT name,               'subtype',                value                  FROM cards, json_each(subtypes_json)  WHERE value <> ''
UNION ALL
SELECT name,               'color',                  value                  FROM cards, json_each(colors_json)    WHERE value <> ''
UNION ALL
SELECT name,               'keyword',                value                  FROM cards, json_each(keywords)       WHERE value <> '';
```

**Filter-satisfaction query.** A trigger port's non-negated attributes must
ALL be present on the candidate; its negated attributes must NOT be present.
Controller qualifiers (`YouCtrl`, `OppCtrl`, `Self`, `Other`) are skipped at
match time — they are runtime-only and can't be resolved at build time.

```sql
-- Candidates that satisfy trigger port :trigger_id
WITH required AS (
    SELECT attr_kind, attr_value
    FROM port_attributes
    WHERE port_id = :trigger_id
      AND is_negated = FALSE
      AND attr_kind NOT IN ('controller')
),
forbidden AS (
    SELECT attr_kind, attr_value
    FROM port_attributes
    WHERE port_id = :trigger_id
      AND is_negated = TRUE
      AND attr_kind NOT IN ('controller')
)
SELECT c.name
FROM cards c
WHERE NOT EXISTS (                                   -- all required present
    SELECT 1 FROM required r
    WHERE NOT EXISTS (
        SELECT 1 FROM card_attributes ca
        WHERE ca.card_name = c.name
          AND ca.attr_kind = r.attr_kind
          AND ca.attr_value = r.attr_value
    )
)
AND NOT EXISTS (                                     -- no forbidden present
    SELECT 1 FROM card_attributes ca
    JOIN forbidden f USING (attr_kind, attr_value)
    WHERE ca.card_name = c.name
);
```

All joins use `idx_port_attr_lookup` and the `card_attributes` view's
underlying indexes. Profiling target: filter satisfaction for 150k ports ×
25k cards in under 5 seconds on a warm SQLite page cache. If this target is
missed, the fallback is a materialised `port_candidate_cache` table
populated at build time — but in practice the indexed join has been
sufficient at this scale.

**Python fallback** (`filter_matches()`) is kept for unit tests and for the
handful of build-time operations that load a single port and want a quick
boolean — but must NEVER be called from inside a SQL join. A lint check
enforces this: `grep -n "def filter_matches" src/` should find only test
utilities.

### 6.2 Static Ability Matching

#### 6.2.1 Lord Detection

A card with `S:Mode$ Continuous | Affected$ Creature.Goblin.YouCtrl | AddPower$ 1` is a Goblin lord. Match it to any Goblin creature.

```sql
-- Find lords that buff the commander or commander's type
SELECT
    s.card_name AS lord_card,
    s.affected_scope,
    s.granted_keyword,
    s.amount AS buff_amount
FROM card_ports s
WHERE s.port_type = 'static'
    AND s.event_class = 'Continuous'
    AND (
        -- Lord buffs commander's creature type
        affected_scope_matches(s.affected_scope, :commander_types)
        -- Or commander is a lord that buffs candidate's types
    )
```

#### 6.2.2 Cost Reduction Detection

Cards with `S:Mode$ ReduceCost` that affect the commander or cards the commander wants to play.

#### 6.2.3 Panharmonicon Detection

Cards with `S:Mode$ Panharmonicon` amplify ETB triggers. Match to any card with `T:Mode$ ChangesZone | Destination$ Battlefield` triggers.

#### 6.2.4 Combat Restriction Detection

Cards with `CantAttack`, `CantBlock`, `MustAttack`, etc. These are anti-synergy with commanders that want to attack/block, or synergy with stax strategies.

### 6.3 Cost↔Effect Synergy

Cards that have costs which the commander (or the commander's strategy) can enable:

```sql
-- Cards whose costs are enabled by the commander's effects
SELECT
    c.card_name AS candidate,
    c.event_class AS cost_type,
    e.card_name AS enabler
FROM card_ports c
JOIN card_ports e
    ON c.port_type = 'cost'
    AND e.port_type = 'effect'
    AND cost_enabled_by(c.event_class, c.cost_subtype, e.event_class)
WHERE c.card_name != :commander
```

Cost enablement rules:

| Cost Type | Enabled By Effect | Example |
|-----------|------------------|---------|
| `remove_counter` | `PutCounter`, `Proliferate` | Card needs counters removed; commander adds counters |
| `sacrifice` | `Token` | Card needs sacrifice fodder; commander makes tokens |
| `exile_from_grave` | `Mill`, `Discard`, `ChangeZone(dest=Graveyard)` | Card exiles from grave; commander fills graveyard |
| `discard` | `Draw` | Card needs to discard; commander draws cards |
| `tap_type` | `Token` of matching type | Card taps a creature; commander makes creature tokens |

### 6.4 Scaling Synergy

Cards whose `SVar:Count$` expressions scale with what the commander or its strategy produces:

```sql
-- Cards that scale with what the commander produces
SELECT
    sc.card_name AS scaling_card,
    sc.scaling_expression,
    sc.valid_filter AS scales_with_filter
FROM card_ports sc
WHERE sc.port_type = 'scales_with'
    AND scales_with_matches(sc.valid_filter, :commander_strategy_outputs)
```

### 6.5 Replacement Effect Anti-Synergy

Detect cards that conflict with the commander's strategy:

```sql
-- Find replacement effects that prevent the commander's triggers
SELECT
    r.card_name AS anti_synergy_card,
    r.replacement_event,
    t.event_class AS blocked_trigger
FROM card_ports r
JOIN card_ports t
    ON t.card_name = :commander
    AND t.port_type = 'trigger'
    AND replacement_blocks_trigger(r.replacement_event, t.event_class)
WHERE r.port_type = 'replacement'
    AND r.replacement_result = 'Prevent'
```

### 6.6 Chain Detection (2-Hop Synergy)

Find cards that synergize with the commander through an intermediate card:

```
Commander triggers on Event A
→ Intermediate card triggers on Event A and produces Event B
→ Candidate card triggers on Event B
```

```sql
-- 2-hop chain: commander.trigger ← intermediate.effect ← candidate.effect
SELECT
    c_eff.card_name AS candidate,
    inter_trig.card_name AS intermediate,
    cmdr_trig.event_class AS cmdr_event,
    inter_trig.event_class AS inter_event
FROM card_ports cmdr_trig
JOIN card_ports inter_eff
    ON matches_event(cmdr_trig.event_class, ..., inter_eff.event_class, ...)
JOIN card_ports inter_trig
    ON inter_trig.card_name = inter_eff.card_name
    AND inter_trig.port_type = 'trigger'
JOIN card_ports c_eff
    ON matches_event(inter_trig.event_class, ..., c_eff.event_class, ...)
    AND c_eff.port_type = 'effect'
WHERE cmdr_trig.card_name = :commander
    AND cmdr_trig.port_type = 'trigger'
    AND inter_eff.port_type = 'effect'
    AND inter_eff.card_name != :commander
    AND c_eff.card_name != :commander
    AND c_eff.card_name != inter_eff.card_name
```

Limit chain depth to 2 hops. Deeper chains are speculative.

### 6.7 DeckHints/DeckNeeds Matching

Forge's own synergy annotations, written by card scripters:

```sql
-- Cards whose DeckHints match the commander's types/keywords
-- This is free synergy data from the Forge community
SELECT c.name
FROM cards c
WHERE json_extract(c.deck_hints, '$.Type') IS NOT NULL
    AND type_overlap(json_extract(c.deck_hints, '$.Type'), :commander_types) > 0
```

### 6.8 Causal Graph Metrics

The current LightGBM pipeline's top features are graph-derived and cannot be
replaced by pairwise port joins alone:

| Feature | LightGBM importance | What it measures |
|---|---|---|
| `graph_neighbor_overlap` | 9.2% | Jaccard overlap of the commander's and candidate's 1-hop neighbourhoods in the causal graph |
| `graph_pagerank` | 6.2% | Personalised PageRank of the candidate, anchored at the commander |
| `card_hub_score` | 5.3% | Weighted in/out degree in the causal graph |
| `cmdr_2hop_ratio` | 4.8% | Ratio of candidate neighbours reachable from the commander in ≤2 hops |

The v1.2 engine computes these **once at build time** over the full
`card_ports` table and stores them as per-(commander, card) features joined
into the `synergy_edges` score breakdown:

```sql
ALTER TABLE synergy_edges ADD COLUMN graph_neighbor_overlap REAL DEFAULT 0;
ALTER TABLE synergy_edges ADD COLUMN graph_pagerank         REAL DEFAULT 0;
ALTER TABLE synergy_edges ADD COLUMN card_hub_score         REAL DEFAULT 0;
ALTER TABLE synergy_edges ADD COLUMN cmdr_2hop_ratio        REAL DEFAULT 0;
```

Construction rule: the "causal graph" is the bipartite projection of
`card_ports` where each edge `(card_A, card_B)` exists iff any port of
`card_A` matches any port of `card_B` under §6.1.2 rules. NetworkX is
permitted here (build-time only, never at inference). These four features
enter §7.1 as a new `graph_metrics` bucket.

**If the engine chooses NOT to implement this section**, it must publicly
accept an expected ~25% drop in top-100 EDHREC overlap vs the current
LightGBM baseline (the sum of the four feature importances above). This is
not a theoretical concern — the LightGBM ablation numbers in `CLAUDE.md` show
it concretely.

### 6.9 Partner / Background / Doctor's Companion Commanders

The inference contract today (`CmdrFeatureContext(ctx, cmdr_oids=[oid1, oid2])`
in `packages/mtg-synergy/src/mtg_synergy/recommend/forge_compute.py`) merges
profiles for two-card commanders. The deterministic engine must preserve this
and extend it in three specific ways: **colour identity union**, **port-set
union**, and **internal-synergy priority**.

#### 6.9.1 Colour Identity Union (hard filter)

A two-card command zone expands the legal colour-identity pool. Every §6.1
colour filter MUST use the union, not either card's identity in isolation:

```python
def commander_color_identity(cmdr_cards: list[dict]) -> frozenset[str]:
    """UNION of colour identities across all commander cards.

    Examples:
      Tymna (WB) + Thrasios (UG)                 → {W, U, B, G}
      The Doctor (WUG) + Romana II (U)           → {W, U, G}
      Wilson, Refined Grizzly + Hardy Outdoorsman → {G} (Background adds colours)

    Two-card commander identity = set().union(*(card.color_identity for card in cmdr_cards))
    """
    return frozenset().union(*(
        frozenset(c.get("color_identity", "").split(","))
        for c in cmdr_cards
        if c.get("color_identity")
    )) - {""}
```

This union drives (a) the hard colour-identity filter in candidate
selection, (b) the §7.4 `staple_bonus()` lookup (which must match ANY of the
union colours), and (c) the §4.5 rule #1 wrong-colour hard filter.

#### 6.9.2 Port-Set Union

```python
def commander_port_set(cmdr_names: list[str], db) -> list[dict]:
    """Return the UNION of card_ports rows across all commander cards.

    For partner pairs (Tymna + Thrasios), Background pairs, and Doctor's
    companion, the relevant port set for matching is the union — the two
    cards are always in the command zone together, so their triggers,
    statics, and replacements all apply.
    """
    placeholders = ",".join("?" * len(cmdr_names))
    return db.execute(
        f"SELECT * FROM card_ports WHERE card_name IN ({placeholders})",
        tuple(cmdr_names),
    ).fetchall()
```

All `:commander` bind parameters in §6.1–§6.7 queries are replaced by
`card_name IN :commander_set`. Scoring (§7.1) de-duplicates matches so the
same candidate port counted twice (once per partner) only contributes once;
use `SELECT DISTINCT candidate, effect_event, trigger_event` before
aggregating.

#### 6.9.3 Internal Synergy Detection (strongest signal)

The single strongest indicator of a two-card commander's deck engine is
whether the two commanders **synergise with each other**. Tymna draws
whenever a creature deals damage to a player → Thrasios + a combat tutor →
card advantage. That synergy is the deck's spine; everything else supports
it.

Before running the candidate-ranking phase, the engine must:

1. Compute port matches **between the two commander cards** using the same
   §6.1 rules as commander↔candidate matching.
2. Record the internal matches in `synergy_edges` with `commander =
   <canonical pair key>` and `card = <partner card>`, reusing the standard
   score breakdown columns.
3. Use the strongest internal synergy verbs/events as a **boost key** for
   candidate scoring: any candidate whose ports also match the internal
   synergy event class gets a strategic-bucket boost equivalent to a
   hand-written rule (§7.3).

```python
def detect_internal_synergies(cmdr_ports: list[dict], cmdr_names: list[str]) -> list[dict]:
    """Find port matches where both sides belong to commander cards.

    Returns rows of {event_class, producer, consumer, match_strength}
    describing which events one commander generates that the other keys on.
    The top 3 event classes by match_strength become "engine events" — any
    candidate producing or consuming them gets the internal-synergy boost.
    """
    if len(cmdr_names) < 2:
        return []
    triggers = [p for p in cmdr_ports if p["port_type"] == "trigger"]
    effects  = [p for p in cmdr_ports if p["port_type"] == "effect"]
    matches = []
    for t in triggers:
        for e in effects:
            if t["card_name"] == e["card_name"]:
                continue  # same card is self-synergy, not internal
            if match_event(t, e):
                matches.append({
                    "event_class": t["event_class"],
                    "producer":    e["card_name"],
                    "consumer":    t["card_name"],
                    "match_strength": 1.0,
                })
    return matches

def internal_synergy_boost(card_ports: list[dict], engine_events: set[str]) -> float:
    """Extra score for candidates that feed the two-commander engine."""
    if not engine_events:
        return 0
    matches = sum(
        1 for p in card_ports
        if p["port_type"] in ("effect", "trigger")
        and p["event_class"] in engine_events
    )
    return min(matches * 6, 30)  # cap at +30 so it can't dominate
```

The `internal_synergy_boost` is a new bucket in §7.1, added between
`reverse_match` and `chain`, with column `internal_synergy_score` in
`synergy_edges`.

Background / companion legality restrictions are still enforced by the
§4.5 penalty table (rule #2), not by the graph engine.

---

## 7. Scoring Specification

### 7.1 Score Components

```python
def compute_synergy_score(commander: str, card: str, db: sqlite3.Connection) -> dict:
    """Compute the full synergy score with breakdown."""

    scores = {
        # Layer 1: Graph Engine (60% of signal)
        "port_match":       count_direct_matches(commander, card, db) * 10,    # 0-100
        "reverse_match":    count_direct_matches(card, commander, db) * 8,     # 0-80
        "internal_synergy": internal_synergy_boost(                             # 0-30
                                card_ports_for(card, db),
                                engine_events_for(commander, db),
                            ),
        "chain":            count_chain_matches(commander, card, db, depth=2) * 3,  # 0-60
        "lord":             compute_lord_match(commander, card, db) * 6,       # 0-30
        "cost_synergy":     compute_cost_enablement(commander, card, db) * 4,  # 0-20
        "scaling":          compute_scaling_match(commander, card, db) * 5,    # 0-25
        "replacement":      compute_replacement_conflict(commander, card, db) * 10,  # -50 to 0
        "amplifier":        compute_amplifier_match(commander, card, db) * 7,  # 0-35
        "deck_hints":       compute_deck_hints_match(commander, card, db) * 3, # 0-15

        # Layer 2: Staples (10% of signal)
        "staple":           staple_bonus(card, commander_colors(commander, db)),  # 0-15

        # Layer 3: Strategic Heuristics (20% of signal)
        "strategic":        strategic_heuristic_score(commander, card, db),     # 0-25

        # Layer 4: EDHREC Tiebreaker (10% of signal)
        "edhrec":           edhrec_tiebreaker(card, db),                       # 0-10
    }

    scores["total"] = sum(scores.values())
    return scores
```

### 7.2 Branch-Aware Weighting

Any direct match, chain match, cost enablement, or scaling match whose
source port row has `is_conditional = TRUE` is multiplied by a branch
reliability factor before the §7.1 sum. The parser preserves branches
losslessly (§5.4); the scorer decides how much to trust them.

```python
BRANCH_MULTIPLIER = {
    "root":                  1.0,  # top-level T:/A:/S:/R: lines
    "execute":               1.0,  # unconditional Execute$ chain
    "subability":            1.0,  # unconditional SubAbility$ chain
    "true":                  0.5,  # TrueSubAbility$  (requires Condition$)
    "false":                 0.5,  # FalseSubAbility$ (requires !Condition$)
    "win":                   0.5,  # WinSubAbility$   (requires Clash win)
    "otherwise":             0.5,  # OtherwiseSubAbility$ (requires Clash loss)
    "repeat":                1.0,  # RepeatSubAbility$ — loop body usually
                                   #   executes deterministically over an
                                   #   explicit list; treat as unconditional
                                   #   unless tuning shows otherwise
    "change_zone_table":     1.0,  # Multi-permanent ETB tables
    "static_condition":      0.75, # S: with Condition$/IsPresent$
    "replacement_condition": 0.75, # R: with real Condition$ gate
}

def weighted_port_value(port_row: dict, base_value: float = 1.0) -> float:
    return base_value * BRANCH_MULTIPLIER.get(
        port_row.get("branch_kind", "root"), 1.0
    )
```

Every `branch_kind` string the parser writes MUST have a matching key in
`BRANCH_MULTIPLIER`. A unit test enforces this invariant.

### 7.3 Strategic Heuristic Rules

Hand-coded rules that capture strategy-level patterns the graph can't see. Each rule has:
- A condition (checked against the commander's ports)
- A boost function (checked against the candidate card)
- A weight
- An explanation string

```python
STRATEGIC_RULES = [
    {
        # Tymna-class: commander profits from combat damage to opponents
        "name": "evasion_for_combat_damage",
        "condition": lambda cmdr_ports: any(
            p["port_type"] == "trigger" and p["event_class"] == "DamageDone"
            and "Player" in (p.get("valid_filter") or "")
            for p in cmdr_ports
        ),
        "boost": lambda card: card["cmc"] <= 3 and any(
            kw in card.get("keywords", [])
            for kw in ["Flying", "Menace", "Shadow", "Fear", "Intimidate",
                       "Skulk", "Horsemanship"]
        ),
        "weight": 5,
        "reason": "Low-cost evasion enables combat damage triggers",
    },
    {
        # Token commander: boost mass pump / anthem effects
        "name": "mass_pump_for_tokens",
        "condition": lambda cmdr_ports: any(
            p["port_type"] == "effect" and p["event_class"] == "Token"
            for p in cmdr_ports
        ),
        "boost": lambda card: any(
            p["port_type"] == "static" and p["event_class"] == "Continuous"
            and "Creature" in (p.get("affected_scope") or "")
            for p in card.get("ports", [])
        ),
        "weight": 4,
        "reason": "Mass pump closes games with token armies",
    },
    {
        # Graveyard commander: boost self-mill
        "name": "selfmill_for_graveyard",
        "condition": lambda cmdr_ports: any(
            p["port_type"] == "trigger" and p["event_class"] == "ChangesZone"
            and p.get("zone_destination") == "Graveyard"
            for p in cmdr_ports
        ),
        "boost": lambda card: any(
            p["port_type"] == "effect" and p["event_class"] in ("Mill", "Discard")
            and "You" in (p.get("valid_filter") or "Opponent")
            for p in card.get("ports", [])
        ),
        "weight": 4,
        "reason": "Self-mill fills graveyard for commander synergy",
    },
    {
        # Sacrifice commander: boost token producers (sacrifice fodder)
        "name": "tokens_for_sacrifice",
        "condition": lambda cmdr_ports: any(
            p["port_type"] in ("trigger", "cost")
            and p["event_class"] in ("Sacrificed", "sacrifice")
            for p in cmdr_ports
        ),
        "boost": lambda card: any(
            p["port_type"] == "effect" and p["event_class"] == "Token"
            for p in card.get("ports", [])
        ),
        "weight": 5,
        "reason": "Token production provides sacrifice fodder",
    },
    {
        # Counter commander: boost proliferate
        "name": "proliferate_for_counters",
        "condition": lambda cmdr_ports: any(
            p["port_type"] in ("trigger", "effect")
            and p["event_class"] in ("PutCounter", "CounterAdded")
            for p in cmdr_ports
        ),
        "boost": lambda card: any(
            p["port_type"] == "effect" and p["event_class"] == "Proliferate"
            for p in card.get("ports", [])
        ),
        "weight": 4,
        "reason": "Proliferate multiplies counter synergies",
    },
    {
        # Mana-sink commanders (Treasure / Mana / Token generators want
        # repeatable activated abilities to convert excess mana into value)
        "name": "mana_sink",
        "condition": lambda cmdr_ports: any(
            p["port_type"] == "effect"
            and p["event_class"] in ("Mana", "Treasure", "Token")
            for p in cmdr_ports
        ),
        "boost": lambda card: any(
            p["port_type"] == "effect"
            and p.get("cost_subtype") in ("Repeatable", "Activated")
            for p in card.get("ports", [])
        ),
        "weight": 4,
        "reason": "Commander generates excess mana; repeatable sinks convert it into value",
    },
    {
        # Last-known-information scaling stays correct when permanents leave
        # play — crucial for Korvold-class death/sacrifice commanders that
        # reference a permanent's CMC/power after it's already gone.
        "name": "lki_scaling",
        "condition": lambda cmdr_ports: any(
            p["event_class"] in ("Sacrifice", "Sacrificed", "ChangesZone")
            for p in cmdr_ports
        ),
        "boost": lambda card: any(
            p["port_type"] == "scales_with"
            and "LKI" in (p.get("scaling_expression") or "")
            for p in card.get("ports", [])
        ),
        "weight": 4,
        "reason": "LKI scaling stays correct when permanents leave play",
    },
    {
        # Tribal support often lives in noncreature static lines
        # (e.g. Metallic Mimic, Door of Destinies) and the graph cannot see
        # that "AddKeyword" on a static targeting ValidCard is a lord effect.
        "name": "tribal_density",
        "condition": lambda cmdr_ports: any(
            (p.get("valid_filter") or "").startswith("Creature")
            for p in cmdr_ports
        ),
        "boost": lambda card: any(
            p["port_type"] == "static"
            and "ValidCard" in (p.get("raw_line") or "")
            and "AddKeyword" in (p.get("raw_line") or "")
            for p in card.get("ports", [])
        ),
        "weight": 3,
        "reason": "Tribal support from noncreature static lines",
    },
    {
        # Anti-stax self-hose: activated-ability commanders should not run
        # "Can't activate" / "Can't untap" stax pieces that shut themselves
        # down. Negative weight, functioning as a soft penalty.
        "name": "anti_stax_activated_abilities",
        "condition": lambda cmdr_ports: any(
            p["port_type"] == "effect"
            and p["event_class"] in ("Tap", "Untap", "Mana")
            for p in cmdr_ports
        ),
        "boost": lambda card: any(
            p["port_type"] in ("replacement", "static")
            and p["event_class"] in ("CantActivate", "CantTap", "CantUntap")
            for p in card.get("ports", [])
        ),
        "weight": -4,
        "reason": "Avoid self-hosing cards that shut down activated-ability commanders",
    },
    # Add 25-45 more rules covering:
    # - Spell-copy for spellslinger commanders
    # - Ramp for high-CMC commanders
    # - Flash/instant-speed for reactive commanders
    # - Untap effects for tap-ability commanders
    # - Topdeck manipulation for topdeck-matters commanders
    # - Blink/flicker for ETB-matters commanders
    # - Extra combat for attack-trigger commanders
    # - Wheel effects for discard-trigger commanders
]
```

### 7.4 Staples List

A manually curated lookup table of format staples. Sourced from EDHREC's "top cards in X colors" pages and personal expertise. Updated manually, not automatically.

```python
STAPLES = {
    "COLORLESS": [
        "Sol Ring", "Arcane Signet", "Command Tower", "Thought Vessel",
        "Lightning Greaves", "Swiftfoot Boots",
    ],
    "W": [
        "Swords to Plowshares", "Path to Exile", "Smothering Tithe",
        "Teferi's Protection", "Generous Gift",
    ],
    "U": [
        "Rhystic Study", "Cyclonic Rift", "Counterspell",
        "Fierce Guardianship", "Mystic Remora",
    ],
    "B": [
        "Demonic Tutor", "Toxic Deluge", "Necropotence",
        "Feed the Swarm", "Bolas's Citadel",
    ],
    "R": [
        "Dockside Extortionist", "Deflecting Swat", "Chaos Warp",
        "Jeska's Will", "Blasphemous Act",
    ],
    "G": [
        "Beast Within", "Heroic Intervention", "Nature's Lore",
        "Sylvan Library", "Kodama's Reach",
    ],
    # Multi-color staples, land staples, etc.
}

STAPLE_SCORE = 12  # Flat bonus — enough to appear in top 100, below mechanical synergy
```

### 7.5 EDHREC Tiebreaker

```python
def edhrec_tiebreaker(card_name: str, db: sqlite3.Connection) -> float:
    """Weak popularity signal from EDHREC rank. Never overrides graph scores."""
    rank = db.execute(
        "SELECT edhrec_rank FROM cards WHERE name = ?", (card_name,)
    ).fetchone()
    if rank is None or rank[0] is None:
        return 0
    # Log scale: rank 1 → ~10, rank 100 → ~5, rank 1000 → ~2, rank 10000 → ~0
    import math
    return max(0, 10 - 2 * math.log10(max(1, rank[0])))
```

### 7.6 Penalty Layer and Replaced Mechanical Bonus

After the additive §7.1 sum (including branch-weighted graph matches and
strategic boosts) and before the final sort, the engine applies the §4.5
penalty table:

```python
def score_candidate(commander_set: list[str], card: str, db) -> float:
    scores = compute_synergy_score(commander_set, card, db)  # §7.1
    total  = scores["total"]
    total  = apply_penalties(commander_set, card, total, db) # §4.5
    return total
```

This layer intentionally **replaces two mechanisms** from the current
LightGBM pipeline:

1. The GBM probability score — now expressed as the sum of weighted port
   matches, so the output magnitude is interpretable.
2. The `_apply_mechanical_bonus()` ±15% multiplier on the GBM output —
   subsumed by the additive `port_match` + `reverse_match` + `chain` buckets
   in §7.1. The spec deliberately does not carry forward the multiplicative
   bonus; any residual need for it should be re-implemented as an additive
   bucket so the score breakdown stays additive and explainable.

---

## 8. Score Breakdown and Optional Explanations

Every recommendation returned through the package API (§14) carries the
**required** score breakdown (the §7.1 `scores` dict) plus a list of
**contributing ports** — the concrete `card_ports` rows (triggers / effects
/ statics / replacements / scaling expressions) whose matches produced each
non-zero score bucket. This is the structured, machine-readable replacement
for v1.0/v1.1's mandatory prose explanations.

```python
@dataclass
class ContributingPort:
    bucket:       str         # which §7.1 bucket this port contributed to
    port_type:    str         # 'trigger' | 'effect' | 'static' | 'replacement' | ...
    event_class:  str         # 'ChangesZone', 'Token', 'Continuous', 'Panharmonicon', ...
    valid_filter: str | None
    branch_kind:  str
    raw_line:     str         # the Forge DSL line so UIs can render it verbatim
```

Consumers (mtg-edh-builder today) render their own UI from `scores` +
`contributing_ports`: badges for matched events, highlighted keywords,
colour-coded bucket totals, etc. No prose generation is required on the
engine side.

**Plain-English explanations are optional and on-demand.** They are
generated only when the consumer requests them (see §14 `include_explanations=True`),
are strictly derived from the scores + contributing ports (never from an
LLM), and are cached per (commander, card) pair so repeat requests are
free. The generator below is kept so the CLI and debug dumps can produce
them, and so the §10.4 Golden Set explanation audit has something to check:

```python
def generate_explanation(commander: str, card: str, scores: dict, db: sqlite3.Connection) -> list[str]:
    """Generate human-readable reason strings (optional)."""
    reasons = []

    if scores["port_match"] > 0:
        matches = get_port_match_details(commander, card, db)
        for m in matches[:3]:  # Top 3 matches
            reasons.append(
                f"{card} produces {m['effect_event']} events, "
                f"which {commander} triggers on ({m['trigger_event']})"
            )

    if scores["reverse_match"] > 0:
        reasons.append(f"{commander}'s effects trigger {card}'s abilities")

    if scores["lord"] > 0:
        reasons.append(f"Buffs {commander}'s creature types")

    if scores["scaling"] > 0:
        reasons.append(f"Scales with board state that {commander}'s strategy produces")

    if scores["amplifier"] > 0:
        reasons.append(f"Amplifies {commander}'s triggered abilities (Panharmonicon effect)")

    if scores["replacement"] < 0:
        reasons.append(f"WARNING: Has replacement effect that conflicts with {commander}'s strategy")

    if scores["cost_synergy"] > 0:
        reasons.append(f"{commander}'s strategy enables this card's activation costs")

    if scores["strategic"] > 0:
        reasons.append(f"Strategically supports {commander}'s gameplan")

    if scores["staple"] > 0:
        reasons.append(f"Format staple in {commander}'s colors")

    return reasons
```

---

## 9. Output Format

The **product surface** is the paginated package API (§14), backed by the
`synergy_edges` table (§4.3). Per-commander JSON files are **debug
artifacts only** — they dump the full precomputed ranking for a commander
to disk for manual inspection, golden-set comparison, and CI. They are not
consumed by mtg-edh-builder.

### 9.1 Response Envelope (package API)

Every paginated response has the same shape:

```python
@dataclass
class RecommendationPage:
    commander:      list[str]          # commander cards (1 or 2 for partners)
    color_identity: list[str]          # UNION across partner cards (§6.9.1)
    total:          int                # total ranked cards available
    offset:         int                # echoed from request
    limit:          int                # echoed from request
    generated_at:   str                # ISO-8601 build timestamp
    forge_version:  str                # Forge data version used to build
    spec_version:   str                # this file's version (e.g. "1.2.2")
    items:          list[Recommendation]

@dataclass
class Recommendation:
    rank:                 int          # absolute rank across all legal cards
    card:                 str
    total_score:          float
    scores:               dict[str, float]  # §7.1 bucket → value
    contributing_ports:   list[ContributingPort]  # §8, always present
    explanation:          list[str] | None        # §8, only if requested
```

Consumers that want the top 100 call `page(commander, offset=0, limit=100)`.
Consumers that want rank 500–600 call `page(commander, offset=500, limit=100)`.
The engine never truncates; every legal card has a row in `synergy_edges`.

### 9.2 Debug JSON Dump (not the product surface)

For manual inspection, `scripts/dump_commander.py --commander "Korvold"`
writes `output/{commander_slug}.json` with the full ranking. Format:

```json
{
    "commander":      ["Korvold, Fae-Cursed King"],
    "color_identity": ["B", "R", "G"],
    "total":          18423,
    "generated_at":   "2026-04-07T12:00:00Z",
    "forge_version":  "1.6.78",
    "spec_version":   "1.2.2",
    "items": [
        {
            "rank": 1,
            "card": "Dockside Extortionist",
            "total_score": 87.3,
            "scores": {
                "port_match": 30,
                "reverse_match": 16,
                "internal_synergy": 0,
                "chain": 12,
                "lord": 0,
                "cost_synergy": 8,
                "scaling": 5,
                "replacement": 0,
                "amplifier": 0,
                "deck_hints": 3,
                "strategic": 5,
                "staple": 8.3,
                "edhrec": 0
            },
            "contributing_ports": [
                {
                    "bucket":       "port_match",
                    "port_type":    "effect",
                    "event_class":  "Token",
                    "valid_filter": "Treasure",
                    "branch_kind":  "execute",
                    "raw_line":     "AB$ Token | TokenAmount$ X | TokenScript$ c_a_treasure ..."
                },
                {
                    "bucket":       "strategic",
                    "port_type":    "rule",
                    "event_class":  "tokens_for_sacrifice",
                    "valid_filter": null,
                    "branch_kind":  "root",
                    "raw_line":     "Strategic rule: Token production provides sacrifice fodder"
                }
            ]
            // "explanation" only present when dumped with --with-explanations
        }
    ]
}
```

Note: a full dump for a single commander can be large (18k+ rows). The
dumper supports `--limit N` to cap the written file; the package API does
NOT because it paginates.

---

## 10. Validation Specification

### 10.1 EDHREC Overlap Test

For each test commander, compare the system's top-100 with EDHREC's top-100.

```python
def validate_commander(commander: str, our_top_100: list[str], edhrec_top_100: list[str]) -> dict:
    our_set = set(our_top_100)
    edhrec_set = set(edhrec_top_100)
    overlap = our_set & edhrec_set

    return {
        "commander": commander,
        "overlap_count": len(overlap),
        "overlap_pct": len(overlap) / 100,
        "our_unique": sorted(our_set - edhrec_set),     # Hidden gems
        "edhrec_unique": sorted(edhrec_set - our_set),   # What we're missing
    }
```

**Target metrics (release gate):**

Every gate below is **relative to the current LightGBM baseline** measured
against the same harness on the same date. Absolute top-100 EDHREC overlap
targets are not used as gates because the current production model itself
does not hit the aspirational 60-75% figure from v1.0:

| Gate | Baseline (current LightGBM, 2026-04-07) | Required for release |
|---|---|---|
| Top-100 avg OnPage (`compare_edhrec.py --top 100`) | 10.0 / 50 (20.0%) | ≥ baseline − 1 card |
| Top-100 avg Hi-Syn | 1.5 / 50 (3.0%) | ≥ baseline |
| Pipeline validation flags (`validate_recommendations.py --top 100`) | 3 / 3000 (0.1%) | ≤ 5 / 3000 |
| Regression pairings (§10.3) | — | 100% pass |

Soft targets (not gates):
- "Our unique" cards (in our top-100 but not in EDHREC's top-100) should be
  mechanically justifiable on manual review.
- "EDHREC unique" cards (in EDHREC's top-100 but not ours) should mostly be
  generically-good staples or popularity-driven picks that the §7.4 staples
  list was expected to absorb.

**Why the gates are relative, not absolute:** per `CLAUDE.md`, the current
LightGBM model achieves ~20% top-100 OnPage overlap on popular commanders.
A deterministic engine that drops the causal-graph features (§6.8) has
limited room to improve on this, and a 60-75% absolute overlap would require
the engine to essentially reproduce EDHREC, which contradicts §1's hidden-gem
goal. The gate is "don't regress," not "match EDHREC."

### 10.2 Expert Audit Protocol

For 10 commanders the developer knows well:
1. Generate top-100 list
2. Manually classify each card: "obviously correct", "interesting find", "makes no sense"
3. Target: <10% "makes no sense", >5% "interesting find"

### 10.3 Regression Test Cards

Maintain a set of known-good and known-bad pairings:

```python
REGRESSION_TESTS = [
    # (commander, card, expected_direction)
    ("Korvold, Fae-Cursed King", "Dockside Extortionist", "high"),
    ("Korvold, Fae-Cursed King", "Phyrexian Altar", "high"),
    ("Atraxa, Praetors' Voice", "Doubling Season", "high"),
    ("Atraxa, Praetors' Voice", "Wrath of God", "low"),     # No mechanical synergy
    ("Prosper, Tome Bound", "Possibility Storm", "medium"),  # Exile synergy
    ("Bruvac the Grandiloquent", "Rest in Peace", "negative"),  # Anti-synergy
]
```

### 10.4 Golden Set Regression Suite

§10.1 guards against absolute regression vs the LightGBM baseline. §10.3
guards against breaking specific known-good pairs. Neither catches **jitter**
— the failure mode where tweaking a scoring weight to fix Commander A
silently breaks Commanders B, C, D, E. The Golden Set closes that gap.

**Composition.**

| Component | Purpose |
|---|---|
| **Golden Set** | Exactly 50 curated commanders spanning archetypes (tribal, spellslinger, combo, sacrifice, counters, graveyard, voltron, stax, big-mana, aristocrats). Each has high-quality EDHREC data (≥1000 decks). Frozen in `tests/fixtures/golden_set.json`. |
| **Frozen EDHREC snapshot** | EDHREC top-100 lists per commander captured at spec freeze time. Never re-fetched during CI — a moving target cannot regress-test anything. Refreshed manually on a cadence with a deliberate snapshot bump. |
| **Overlap + NDCG tracker** | `scripts/golden_set_track.py` runs the engine over all 50 commanders, computes per-commander overlap, per-commander NDCG@30 (against the frozen EDHREC ranks as graded labels), and aggregate mean + stddev. |
| **Score-breakdown audit** | Fails if any top-10 recommendation has all `scores[bucket] == 0` (meaningless ranking) or has `contributing_ports == []` (no mechanical evidence for the score). Plain-English explanations are optional (§8), so the audit checks the **structured** breakdown, not the prose. |
| **Explanation sanity (optional)** | When the golden-set tracker is run with `--with-explanations`, the audit additionally fails if any non-empty explanation list contains only the generic "Format staple in X's colors" string — but missing explanations are NOT a failure. |
| **Diff report** | After every change to §4.5, §6, or §7, the tracker diffs the run against the last committed `golden_set_run.json` and reports per-commander delta (gained cards, lost cards, rank shifts). Any commander with >5 rank shifts in the top-10 blocks CI. |

**CI integration.**

```python
# tests/test_golden_set.py
def test_golden_set_no_jitter():
    prev = json.load(open("tests/fixtures/golden_set_run.json"))  # last committed baseline
    curr = run_engine_over_golden_set()

    regressed = []
    for cmdr in prev:
        top10_prev = set(prev[cmdr]["top_100"][:10])
        top10_curr = set(curr[cmdr]["top_100"][:10])
        shifted = len(top10_prev - top10_curr)
        if shifted > 5:
            regressed.append((cmdr, shifted))

    assert not regressed, f"Top-10 jitter in {len(regressed)} commanders: {regressed}"

def test_golden_set_ndcg_no_regression():
    prev_ndcg = json.load(open("tests/fixtures/golden_set_run.json"))["mean_ndcg"]
    curr_ndcg = run_engine_over_golden_set()["mean_ndcg"]
    # Allow 0.005 noise but block hard drops
    assert curr_ndcg >= prev_ndcg - 0.005, f"NDCG regressed: {prev_ndcg} -> {curr_ndcg}"

def test_top10_has_score_breakdown():
    """Every top-10 card must have at least one non-zero bucket AND at
    least one contributing port. Prose explanations are optional."""
    runs = run_engine_over_golden_set()
    for cmdr, run in runs.items():
        for rec in run["top_100"][:10]:
            nonzero = [k for k, v in rec["scores"].items() if v]
            assert nonzero, f"{cmdr}/{rec['card']} has all-zero score breakdown"
            assert rec["contributing_ports"], \
                f"{cmdr}/{rec['card']} has no contributing ports (no mechanical evidence)"
            # Prose explanation check only when present
            if rec.get("explanation"):
                assert not all(e.startswith("Format staple") for e in rec["explanation"]), \
                    f"{cmdr}/{rec['card']} has only generic staple reason"
```

**Workflow rule:** changes to §4.5 (penalties), §6 (graph engine), or §7
(scoring) MUST be accompanied by a rerun of `golden_set_track.py` and a
diff review in the PR description. If a regression is intentional (e.g.
removing a card that was always wrong), the new `golden_set_run.json` is
committed in the same PR with an explicit justification.

---

## 11. Project Structure

```
mtg-synergy-graph/
├── pyproject.toml                # uv project config
├── SPEC.md                       # This document
├── data/
│   ├── forge_scripts/            # Extracted cardsfolder .txt files (gitignored)
│   ├── scryfall_bulk.json        # Scryfall bulk data (gitignored)
│   └── staples.json              # Curated staples list
├── src/mtg_synergy_graph/        # pip-installable package (§14)
│   ├── __init__.py               # exports SynergyEngine, Recommendation, RecommendationPage
│   ├── engine.py                 # SynergyEngine public API (page, score_one, legal_cards, metadata)
│   ├── parser.py                 # Forge DSL parser + SVar chain walker
│   ├── importer.py               # Populate SQLite from parsed cards
│   ├── graph_engine.py           # Port matching SQL queries
│   ├── scoring.py                # Score aggregation + §4.5 penalties
│   ├── heuristics.py             # Strategic heuristic rules (§7.3)
│   ├── explain.py                # Optional prose explanation generator (§8)
│   ├── validate.py               # EDHREC comparison + regression tests
│   └── build.py                  # Orchestrator: parse → import → score → synergy_edges
├── scripts/
│   ├── dump_commander.py         # Debug JSON dump (§9.2), NOT the product surface
│   ├── compare_edhrec.py         # Evaluation harness
│   ├── golden_set_track.py       # §10.4 regression tracker
│   └── validate_recommendations.py
├── tests/
│   ├── test_parser.py            # Unit tests for DSL parsing
│   ├── test_chain_walker.py      # SVar chain walking tests
│   ├── test_port_matching.py     # Event matching logic tests
│   ├── test_scoring.py           # Scoring formula tests
│   ├── test_regression.py        # Known-good/bad pairing tests
│   ├── test_engine_api.py        # Pagination contract, score_one, partner pairs
│   ├── test_golden_set.py        # §10.4 Golden Set tests
│   └── fixtures/
│       ├── cathars_crusade.txt
│       ├── korvold.txt
│       ├── panharmonicon.txt
│       ├── rhystic_study.txt
│       ├── scute_swarm.txt
│       ├── golden_set.json       # 50-commander fixture
│       └── golden_set_run.json   # last-committed baseline
├── output/                       # Debug dumps only (gitignored, not the product surface)
│   └── {commander_slug}.json
└── synergy.db                    # SQLite database (built artifact, shipped with wheel or downloaded)
```

---

## 12. Implementation Order

### Phase 1: Parser + Schema (Week 1-2)

1. Create SQLite schema (Section 4)
2. Implement `parser.py` with `parse_card_file()` and `parse_forge_line()` (Section 5.2-5.3)
3. Implement SVar chain walker (Section 5.4)
4. Implement all port extraction functions (Section 5.5)
5. Implement `importer.py` to populate database from card files
6. Write unit tests against 5 complex reference cards:
   - Cathars' Crusade (ETB trigger → PutCounter chain)
   - Korvold (sacrifice trigger + draw + counter)
   - Panharmonicon (static Panharmonicon mode)
   - Rhystic Study (SpellCast trigger + UnlessCost + Draw)
   - Scute Swarm (ChangesZone trigger + Token + conditional Copy)

**Acceptance criteria:** All 5 reference cards parse correctly with complete port signatures.

### Phase 2: Graph Engine (Week 2-3)

7. Implement event matching rules (Section 6.1.2)
8. Implement ValidFilter compatibility checking (Section 6.1.3)
9. Implement direct port matching queries (Section 6.1.1)
10. Implement static ability matching — lords, cost reduction, Panharmonicon (Section 6.2)
11. Implement cost↔effect synergy (Section 6.3)
12. Implement scaling synergy (Section 6.4)
13. Implement replacement anti-synergy (Section 6.5)
14. Implement DeckHints/DeckNeeds matching (Section 6.7)

**Acceptance criteria:** For Korvold, the engine correctly identifies Dockside Extortionist, Phyrexian Altar, and Tireless Tracker as high-synergy; Wrath of God as low-synergy.

### Phase 3: Scoring + Output (Week 3-4)

15. Implement scoring formula (Section 7.1)
16. Implement strategic heuristic rules (Section 7.2) — start with 10 rules, expand to 30+
17. Implement staples list (Section 7.3)
18. Implement EDHREC tiebreaker (Section 7.4)
19. Implement explanation generation (Section 8)
20. Implement JSON output generation (Section 9)
21. Implement build orchestrator (Section 11)

**Acceptance criteria:** For 5 commanders, `engine.page(commander, offset=0, limit=100)` returns a ranked window whose first 100 rows pass the expert audit (< 10% nonsense). The same call with `limit=1_000_000` must return every legal card for the commander with `total` equal to `len(items)`.

### Phase 4: Validation + Tuning (Week 4-5)

22. Implement EDHREC overlap validation (Section 10.1)
23. Run overlap test on 50 commanders
24. Tune scoring weights based on overlap analysis
25. Implement chain detection for 2-hop synergies (Section 6.6)
26. Expand strategic heuristic rules based on gap analysis
27. Build regression test suite (Section 10.3)

**Acceptance criteria:** §10.1 release gates met on 100 commanders (no regression vs LightGBM baseline on OnPage/Hi-Syn, ≤ 5/3000 pipeline validation flags), and §10.3 regression tests pass.

---

## 13. Key Design Decisions (For Reference)

1. **No ML/LLM** — The Forge DSL is a closed formal grammar with ~140 primitives. Synergy is a deterministic port-matching function, not a statistical pattern.

2. **SQLite over Neo4j/graph DB** — The graph is static and read-heavy. Built once, queried at build time. SQLite with indexed joins handles ~25K cards at sub-millisecond latency.

3. **No vector embeddings** — The demand/supply matching (what a commander wants vs. what a card provides) is exactly what port-joining does, with perfect precision instead of approximate cosine similarity.

4. **EDHREC as validation, not training** — Training on popularity data conflates what's popular with what's synergistic. The system should find mechanically correct cards that may be unpopular.

5. **Staples list is manually curated** — "Generically good" cards (Sol Ring, Cyclonic Rift) can't be identified by mechanical synergy. A small curated list (~200 cards) is more reliable than heuristics.

6. **Strategic heuristics are hand-coded rules, not learned** — There are ~30-50 strategy-level patterns (e.g., "combat damage commander wants evasive creatures"). These are few enough to enumerate and stable enough to hardcode.

7. **SVar:Count$ is a distinct synergy signal** — Cards that scale with board state (Count$Valid Creature.Black.YouCtrl) represent linear scaling relationships that trigger/effect matching can't capture.

8. **SubAbility$ chains must be walked recursively** — Forge nests the important effects 2-3 levels deep via SVar references. The parser must follow Execute$, SubAbility$, TrueSubAbility$, FalseSubAbility$, WinSubAbility$, OtherwiseSubAbility$, and RepeatSubAbility$ references with cycle detection.

9. **DeckHints/DeckNeeds/DeckHas are free synergy data** — Forge's card scripting community has manually annotated thousands of cards with synergy hints for the AI deck builder. Use these as a bonus signal.

10. **Precomputed output, paginated delivery** — All synergy computation happens at build time and lands in `synergy_edges`. Consumers query the precomputed ranking through the paginated package API (§14); the build step is the only moment where runtime cost exists. Zero API dependency at query time.

11. **Branch provenance is a parser concern, reliability is a scorer concern** (v1.2) — The SVar chain walker preserves every branch (including `TrueSubAbility$` / `FalseSubAbility$` / clash outcomes) with `branch_kind` metadata. Reliability discounting happens once, centrally, via `BRANCH_MULTIPLIER` in §7.2. This avoids the failure mode where each extraction function invents its own "is this reliable?" heuristic.

12. **Pivot risk is acknowledged, not ignored** (v1.2) — This spec replaces a
    working LightGBM pipeline (NDCG@30 = 0.5707 as of 2026-04-07, 98 features,
    ~21.7M causal edges, see `CLAUDE.md`). The deterministic engine gives up
    the learned interactions between those features — top features by
    importance are `graph_neighbor_overlap` (9.2%), `mech_cosine` (6.5%),
    `graph_pagerank` (6.2%), `card_hub_score` (5.3%), `mech_density` (5.2%).
    §6.8 mitigates this by computing the four graph-derived features at build
    time, but the learned GBM interaction effects across all 98 features
    cannot be reproduced by additive weights. The trade-off is accepted for:
    (a) day-1 new-card support without retraining, (b) fully explainable
    scores, (c) zero runtime EDHREC dependency, (d) ~646 MB → much smaller
    deployment footprint, (e) simpler ops. The §10.1 release gates are set
    to "don't regress vs current model," NOT "match an aspirational absolute
    target," precisely because this pivot has real downside risk.

13. **Penalty system is ported verbatim before simplifying** (v1.2) — The
    §4.5 penalty table is derived from the current `_apply_penalties()` in
    `packages/mtg-synergy/src/mtg_synergy/recommend/scoring.py`. Every rule
    exists because a real card (Bruvac, Sidisi, Earthbend, Background
    creatures, TIME-counter cards, etc.) broke the recommendations at some
    point. The initial implementation MUST port all 12 rules unchanged; any
    rule removal requires a regression-test justification.

14. **No top-N truncation; pagination is the contract** (v1.2.2) — The
    engine ranks every legal card. mtg-edh-builder pulls whatever window it
    needs (top 20, top 100, rank 200–300, full tail for hidden-gem panels)
    via offset/limit. Truncating at 100 in the engine would force every
    future consumer to refetch or rebuild to change the window, which is
    exactly the kind of rigidity this rewrite is meant to remove.

15. **Explanations are optional, score breakdowns are mandatory** (v1.2.2) —
    The required output is structured: `scores` (§7.1 buckets) +
    `contributing_ports` (the exact Forge DSL lines that drove each bucket).
    Plain-English prose is an extractive render of that structure, generated
    on demand by §8's `generate_explanation`, and is never required for the
    API contract. This lets consumers build richer UIs than a sentence list
    (badges, highlighted keywords, drill-down panels) without the engine
    taking an opinion on presentation.

---

## 14. External Package Contract (mtg-edh-builder consumer)

This project is distributed as a pip-installable Python package consumed by
mtg-edh-builder (and any future consumer). The package surface is small,
stable, and fully offline.

### 14.1 Installation

```bash
# From the monorepo workspace in mtg-edh-builder:
uv add mtg-synergy-graph

# Or pinned by commit from a git URL:
uv add "mtg-synergy-graph @ git+https://github.com/.../mtg-synergy-graph@<sha>"
```

The package bundles the precomputed `synergy.db` (or downloads it from a
release asset on first use, configurable via `MTG_SYNERGY_DB_PATH`). No
network calls at runtime.

### 14.2 Public API

```python
from mtg_synergy_graph import SynergyEngine, RecommendationPage, Recommendation

engine = SynergyEngine(db_path="synergy.db")        # ~0.5s warm load

# Single commander — paginated
page: RecommendationPage = engine.page(
    commander="Korvold, Fae-Cursed King",
    offset=0,
    limit=100,
    include_explanations=False,                     # default: score breakdown only
)

# Partner pair — pass a list, see §6.9
page = engine.page(
    commander=["Tymna the Weaver", "Thrasios, Triton Hero"],
    offset=0,
    limit=50,
)

# Rank window deep in the tail (hidden-gem panel)
tail = engine.page(commander="Krenko, Mob Boss", offset=500, limit=100)

# Single-card lookup against a commander (used by mtg-edh-builder's
# "why is this card in my deck?" panel)
rec: Recommendation = engine.score_one(
    commander="Korvold, Fae-Cursed King",
    card="Dockside Extortionist",
    include_explanation=True,                       # opt in to prose
)

# Colour-identity filter / legality — already applied inside synergy_edges,
# but a helper is exposed for consumers that need it standalone
legal: set[str] = engine.legal_cards(commander="Korvold, Fae-Cursed King")

# Metadata for cache invalidation in the consumer
meta = engine.metadata()
# -> {"spec_version": "1.2.2", "forge_version": "1.6.78",
#     "generated_at": "2026-04-07T12:00:00Z", "db_sha256": "..."}
```

### 14.3 Guarantees

- **No truncation**: `engine.page(commander, offset=0, limit=1_000_000)`
  returns every legal card. `total` always equals the full count.
- **Deterministic ranking**: for a given `(spec_version, forge_version,
  commander)`, the order is stable. Consumers can cache by `db_sha256`.
- **Stable envelope**: adding new score buckets appends to the `scores`
  dict; removing or renaming a bucket is a breaking change and bumps the
  minor version.
- **Offline**: every public method is pure-local SQLite; no network calls,
  no EDHREC, no Scryfall API at query time.
- **Thread-safe reads**: `SynergyEngine` instances can be shared across
  threads (read-only SQLite connections). Not fork-safe without re-opening.

### 14.4 Performance targets

| Operation | Warm target | Cold target |
|---|---|---|
| `SynergyEngine(db_path)` constructor | — | < 1s |
| `engine.page(commander, offset=0, limit=100)` | < 30ms | < 200ms |
| `engine.page(commander, offset=500, limit=100)` | < 30ms | < 200ms |
| `engine.score_one(commander, card)` | < 5ms | < 50ms |
| `engine.score_one(commander, card, include_explanation=True)` | < 10ms | < 60ms |

All targets assume the `synergy_edges` table is precomputed and indexed by
`(commander, total_score DESC)` (see §4.3).

### 14.5 Versioning

`spec_version` in the API response is the version of this document that
was in force when `synergy.db` was built. Consumers should treat a bump in
the minor version as a signal to invalidate their own caches; a bump in
the patch version is compatible.

### 14.6 mtg-edh-builder integration notes

- **Deck-building UI**: fetches `engine.page(commander, offset=0, limit=200)`
  to populate the "Recommended" panel, then calls `engine.score_one()` for
  every card already in the user's deck to render per-card justification.
- **Hidden-gem panel**: fetches `engine.page(commander, offset=500, limit=100)`
  and filters client-side by EDHREC rank threshold.
- **Cache key**: `(commander, spec_version, forge_version)`; no need to
  include `offset`/`limit` because the underlying ranking is deterministic.
- **Partner commanders**: always pass both cards as a list; the builder
  never has to compute the union itself (handled by §6.9).
