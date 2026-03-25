# Deterministic Stack Optimization — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make causal graph + mechanics + tags produce high-quality recommendations for any commander at $0 cost by adding IDF edge weighting, chain scoring, commander profiles, and Recall@K validation.

**Architecture:** Event IDF weighting in the causal indexer dampens common triggers and boosts rare ones. Chain scoring in CausalContext rewards multi-card interaction paths. Commander profiles auto-detect archetypes from oracle text. Recall@K against EDHREC average decks replaces top-30 overlap as the primary metric.

**Tech Stack:** Python 3, SQLite, pytest, numpy (existing), urllib.request (EDHREC API)

**Spec:** `docs/superpowers/specs/2026-03-25-deterministic-stack-optimization-design.md`

---

## File Structure

| File | Responsibility | Status |
|------|---------------|--------|
| `mtg_synergy/causal/indexer.py` | Event indexing + IDF frequency counts | Modify |
| `mtg_synergy/causal/graph_builder.py` | Edge building with IDF-weighted strengths | Modify |
| `mtg_synergy/causal/__init__.py` | CausalContext chain bonus + forward map | Modify |
| `mtg_synergy/recommend/commander_profile.py` | CommanderProfile inference + DB storage | Create |
| `mtg_synergy/recommend/scoring.py` | DeckContext fallback to commander profiles | Modify |
| `optimize_weights.py` | Recall@K, --no-llm, --novelty evaluation | Modify |
| `fetch_edhrec_decks.py` | Fetch EDHREC average decklists | Create |
| `tests/test_event_idf.py` | IDF computation + edge weighting tests | Create |
| `tests/test_chain_scoring.py` | Chain bonus computation tests | Create |
| `tests/test_commander_profile.py` | Profile inference tests | Create |
| `tests/test_recall_evaluation.py` | Recall@K metric tests | Create |

---

## Task 1: Event IDF in CardIndex

**Files:**
- Modify: `mtg_synergy/causal/indexer.py:9-55`
- Test: `tests/test_event_idf.py` (create)

- [ ] **Step 1: Write failing test for producer/responder counts**

Create `tests/test_event_idf.py`:

```python
"""Tests for event IDF computation in CardIndex."""
import math
import pytest
from mtg_synergy.causal.indexer import CardIndex, build_index
from mtg_synergy.parse.ast_types import (
    Ability, Trigger, Effect, Amount, ObjectFilter, TokenDef, Cost
)


def _make_krenko():
    """Creates Goblin tokens → produces creature_enters + goblin-specific events."""
    return ("krenko", [Ability(
        kind="activated", cost=Cost(tap=True),
        effects=[Effect(verb="create", amount=Amount(value="X"),
                        token=TokenDef(card_type="creature", subtype="Goblin",
                                       power=1, toughness=1, keywords=[], color="red"))],
    )])


def _make_purphoros():
    """Triggers on creature entering → responds to enters_the_battlefield."""
    return ("purphoros", [Ability(
        kind="triggered",
        trigger=Trigger(event="enters_the_battlefield",
                        subject=ObjectFilter(card_type="creature", controller="you")),
        effects=[Effect(verb="deal_damage", amount=Amount(value=2),
                        target=ObjectFilter(controller="opponent"))],
    )])


def _make_cathars():
    """Triggers on creature entering → responds to enters_the_battlefield."""
    return ("cathars", [Ability(
        kind="triggered",
        trigger=Trigger(event="enters_the_battlefield",
                        subject=ObjectFilter(card_type="creature", controller="you")),
        effects=[Effect(verb="put_counter", amount=Amount(value=1),
                        target=ObjectFilter(card_type="creature", controller="you"))],
    )])


def _make_goblin_sharpshooter():
    """Triggers on creature dying → responds to dies."""
    return ("sharpshooter", [Ability(
        kind="triggered",
        trigger=Trigger(event="dies",
                        subject=ObjectFilter(card_type="creature")),
        effects=[Effect(verb="untap", target=ObjectFilter(name="self"))],
    )])


def test_producer_counts():
    cards = dict([_make_krenko()])
    idx = build_index(cards)
    # Krenko produces creature_enters (and possibly enters_the_battlefield)
    assert hasattr(idx, 'producer_counts')
    assert idx.producer_counts.get("creature_enters", 0) >= 1


def test_responder_counts():
    cards = dict([_make_purphoros(), _make_cathars()])
    idx = build_index(cards)
    assert hasattr(idx, 'responder_counts')
    # Both purphoros and cathars respond to enters_the_battlefield
    assert idx.responder_counts.get("enters_the_battlefield", 0) == 2


def test_compute_event_idf():
    cards = dict([_make_krenko(), _make_purphoros(), _make_cathars(),
                  _make_goblin_sharpshooter()])
    idx = build_index(cards)
    idf = idx.compute_event_idf()
    # "enters_the_battlefield" has 2 responders out of 4 cards → lower IDF
    # "dies" has 1 responder out of 4 cards → higher IDF
    assert "enters_the_battlefield" in idf["responder"]
    assert "dies" in idf["responder"]
    assert idf["responder"]["dies"] > idf["responder"]["enters_the_battlefield"]


def test_idf_range():
    """IDF values must be in 0.3-3.0 range."""
    cards = dict([_make_krenko(), _make_purphoros(), _make_cathars(),
                  _make_goblin_sharpshooter()])
    idx = build_index(cards)
    idf = idx.compute_event_idf()
    for side in ("producer", "responder"):
        for event, value in idf[side].items():
            assert 0.3 <= value <= 3.0, f"{side} IDF for {event} = {value} out of range"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_event_idf.py -v`
Expected: FAIL — `CardIndex` has no `producer_counts` attribute or `compute_event_idf` method.

- [ ] **Step 3: Implement producer/responder counts and IDF in CardIndex**

Modify `mtg_synergy/causal/indexer.py`. Add to `CardIndex` dataclass:

```python
producer_counts: dict = field(default_factory=dict)   # {event: num_unique_cards}
responder_counts: dict = field(default_factory=dict)  # {event: num_unique_cards}
total_cards: int = 0

def compute_event_idf(self) -> dict:
    """Compute IDF multipliers for producer and responder events.

    Rare events get higher weight (up to 3.0x), common events get lower (down to 0.3x).
    """
    import math
    result = {"producer": {}, "responder": {}}
    n = max(self.total_cards, 1)
    max_idf = math.log(n) if n > 1 else 1.0
    min_idf = math.log(2) if n > 2 else 0.1
    span = max_idf - min_idf if max_idf > min_idf else 1.0

    for side, counts in [("producer", self.producer_counts),
                         ("responder", self.responder_counts)]:
        for event, count in counts.items():
            raw = math.log(max(n / max(count, 1), 1))
            normalized = 0.3 + 2.7 * (raw - min_idf) / span
            result[side][event] = round(max(0.3, min(3.0, normalized)), 3)
    return result
```

In `build_index()`, after the main loop, add count computation:

```python
# Compute unique card counts per event
for event, entries in idx._producers.items():
    idx.producer_counts[event] = len({cid for cid, _, _ in entries})
for event, entries in idx._responders.items():
    idx.responder_counts[event] = len({cid for cid, _, _ in entries})
idx.total_cards = len(cards)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_event_idf.py -v`
Expected: All 4 tests PASS.

- [ ] **Step 5: Run full test suite to verify no regressions**

Run: `python3 -m pytest tests/ -v --tb=short`
Expected: All 307+ tests PASS.

- [ ] **Step 6: Commit**

```bash
git add mtg_synergy/causal/indexer.py tests/test_event_idf.py
git commit -m "feat(causal): add event IDF computation to CardIndex"
```

---

## Task 2: IDF-Weighted Edge Building

**Files:**
- Modify: `mtg_synergy/causal/graph_builder.py:61-96` (trigger edges), `193-251` (amplifies edges)
- Modify: `mtg_synergy/causal/graph_builder.py:406-436` (`build_causal_edges` signature)
- Test: `tests/test_event_idf.py` (append)

- [ ] **Step 1: Write failing test for IDF-weighted trigger edges**

Append to `tests/test_event_idf.py`:

```python
from mtg_synergy.causal.graph_builder import build_causal_edges


def test_trigger_edges_use_idf():
    """Rare event edges should have higher strength than common event edges."""
    # Krenko produces creature_enters (common — responded to by 2 cards)
    # Sharpshooter responds to dies (rare — only 1 responder)
    # Purphoros and Cathars both respond to enters_the_battlefield (common)
    cards = dict([_make_krenko(), _make_purphoros(), _make_cathars(),
                  _make_goblin_sharpshooter()])
    edges = build_causal_edges(cards)
    # Find edges from krenko
    kr_to_purph = [e for e in edges if e.source == "krenko" and e.target == "purphoros"
                   and e.edge_type == "triggers"]
    kr_to_cathars = [e for e in edges if e.source == "krenko" and e.target == "cathars"
                     and e.edge_type == "triggers"]
    # Both should exist
    assert len(kr_to_purph) >= 1
    assert len(kr_to_cathars) >= 1
    # Their strengths should NOT be the old fixed 0.6 — they should be IDF-adjusted
    # Since both respond to the same common event, their IDF-adjusted strength
    # should be less than the non-IDF 0.6
    for e in kr_to_purph:
        # creature_enters is common (multiple responders), so IDF dampens it below 0.6
        assert e.strength < 0.6, f"Edge strength {e.strength} should be IDF-dampened below 0.6"


def test_combined_idf_capped():
    """Combined producer × responder IDF product must not exceed 3.0."""
    cards = dict([_make_krenko(), _make_purphoros(), _make_cathars(),
                  _make_goblin_sharpshooter()])
    edges = build_causal_edges(cards)
    for e in edges:
        if e.edge_type == "triggers":
            # precision_strength is max 1.0, combined_idf is capped at 3.0
            # so max possible strength is 3.0
            assert e.strength <= 3.0, f"Edge {e.source}->{e.target} strength {e.strength} exceeds cap"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_event_idf.py::test_trigger_edges_use_idf -v`
Expected: FAIL — edge strengths are still the old fixed values.

- [ ] **Step 3: Modify edge builders to use IDF**

In `mtg_synergy/causal/graph_builder.py`:

**a)** Change `build_causal_edges()` to compute IDF and pass to edge builders:

```python
def build_causal_edges(cards, oracle_texts=None, type_lines=None):
    global _all_cards
    _all_cards = cards
    index = build_index(cards)
    event_idf = index.compute_event_idf()
    edges = []
    edges.extend(_build_trigger_edges(index, event_idf))
    edges.extend(_build_feeds_edges(index))
    if oracle_texts:
        edges.extend(_build_amplifies_edges(index, cards, oracle_texts, event_idf))
    edges.extend(_build_enables_edges(index, cards))
    if type_lines:
        edges.extend(_build_tribal_edges(cards, type_lines))
    return _dedup_edges(edges)
```

**b)** Modify `_build_trigger_edges(index, event_idf)`:

After `strength = _precision_to_strength(precision)`, add:

```python
# Apply combined IDF: dampen common events, boost rare ones
p_idf = event_idf["producer"].get(event, 1.0)
r_idf = event_idf["responder"].get(event, 1.0)
combined_idf = min(p_idf * r_idf, 3.0)
strength *= combined_idf
```

**c)** Modify `_build_amplifies_edges(index, cards, oracle_texts, event_idf)`:

IDF applies **only to the `trigger_modifier` branch** (which references event types). The `replacement` branch (which matches on verb types like "create", "put_counter") keeps its fixed 0.9 strength — verbs aren't events and don't have IDF.

In the `elif kind == "trigger_modifier"` branch, replace the fixed `strength=0.9`:

```python
r_idf = event_idf["responder"].get(modified_event, 1.0)
strength = 0.9 * min(r_idf, 3.0)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_event_idf.py -v`
Expected: All 6 tests PASS.

- [ ] **Step 5: Run full test suite**

Run: `python3 -m pytest tests/ -v --tb=short`
Expected: All tests PASS. Some existing tests in `test_causal_integration.py` and `test_graph_builder.py` may need strength assertion updates if they checked exact values — fix if needed.

- [ ] **Step 6: Commit**

```bash
git add mtg_synergy/causal/graph_builder.py tests/test_event_idf.py
git commit -m "feat(causal): apply IDF weighting to trigger and amplifies edges"
```

---

## Task 3: Chain Scoring in CausalContext

**Files:**
- Modify: `mtg_synergy/causal/__init__.py:182-338` (CausalContext class)
- Test: `tests/test_chain_scoring.py` (create)

- [ ] **Step 1: Write failing test for chain bonus**

Create `tests/test_chain_scoring.py`:

```python
"""Tests for chain bonus scoring in CausalContext."""
import sqlite3
import json
import pytest
from mtg_synergy.causal import (
    build_and_store_graph, CausalContext, ensure_causal_schema
)
from mtg_synergy.parse import parse_card, save_parsed, ensure_parse_schema


def _setup_chain_scenario(conn):
    """Set up: Krenko → creates Goblins → Purphoros triggers → deals damage.

    Chain: Krenko → (creature_enters) → Purphoros → (deal_damage) → [end]
    A candidate that links Krenko to deck cards should get a chain bonus.
    """
    ensure_parse_schema(conn)
    ensure_causal_schema(conn)

    cards_data = [
        ("krenko", "Krenko, Mob Boss",
         "{T}: Create X 1/1 red Goblin creature tokens, where X is the number of Goblins you control.",
         "Legendary Creature — Goblin Warrior"),
        ("purphoros", "Purphoros, God of the Forge",
         "Whenever a creature enters the battlefield under your control, Purphoros deals 2 damage to each opponent.",
         "Legendary Enchantment Creature — God"),
        ("sharpshooter", "Goblin Sharpshooter",
         "Whenever a creature dies, untap Goblin Sharpshooter.\n{T}: Goblin Sharpshooter deals 1 damage to any target.",
         "Creature — Goblin"),
        ("impact", "Impact Tremors",
         "Whenever a creature enters the battlefield under your control, Impact Tremors deals 1 damage to each opponent.",
         "Enchantment"),
    ]
    parsed = {}
    for oid, name, oracle, type_line in cards_data:
        abilities = parse_card(oracle, type_line)
        save_parsed(conn, oid, abilities)
        parsed[oid] = abilities
    conn.commit()
    build_and_store_graph(conn, parsed)
    return parsed


def test_chain_bonus_exists(tmp_db):
    """A candidate that the commander connects to AND that connects to deck cards gets bonus."""
    conn = sqlite3.connect(tmp_db)
    _setup_chain_scenario(conn)
    # Commander: krenko, deck: {purphoros, sharpshooter}
    # Candidate: impact (krenko→impact via creature_enters, impact is a chain link)
    ctx = CausalContext(conn, "krenko", {"purphoros", "sharpshooter"})
    score_impact = ctx.causal_score("impact")
    # Impact should get a chain bonus because:
    # 1. Krenko → impact (creature_enters trigger)
    # 2. impact is connected to deck cards (same event trigger)
    assert score_impact > 0
    conn.close()


def test_chain_bonus_absent_for_unlinked(tmp_db):
    """A candidate with no commander link gets no chain bonus."""
    conn = sqlite3.connect(tmp_db)
    _setup_chain_scenario(conn)
    ctx = CausalContext(conn, "krenko", {"purphoros"})
    # sharpshooter responds to "dies", not "creature_enters" — no direct Krenko link
    # (unless Krenko produces dies events, which it doesn't)
    # The chain bonus specifically should be 0 if no cmdr forward map entry
    bonus = ctx._chain_bonus("sharpshooter")
    # Sharpshooter has no direct commander link via creature_enters, so bonus should be 0
    assert bonus == 0.0
    conn.close()


def test_forward_map_built(tmp_db):
    """CausalContext should build a forward map from commander's outgoing edges."""
    conn = sqlite3.connect(tmp_db)
    _setup_chain_scenario(conn)
    ctx = CausalContext(conn, "krenko", {"purphoros"})
    assert hasattr(ctx, '_cmdr_forward_map')
    assert isinstance(ctx._cmdr_forward_map, dict)
    # Krenko should have outgoing edges to at least purphoros
    assert len(ctx._cmdr_forward_map) > 0
    conn.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_chain_scoring.py -v`
Expected: FAIL — `CausalContext` has no `_chain_bonus` method or `_cmdr_forward_map`.

- [ ] **Step 3: Implement chain bonus in CausalContext**

Modify `mtg_synergy/causal/__init__.py`:

In `CausalContext.__init__`, after building `_incoming`/`_outgoing` (around line 192), add:

```python
# Build commander forward map: {card_id: strength} for direct cmdr outgoing
self._cmdr_forward_map = {}
for edge in self._outgoing.get(commander_id, []):
    mid = edge.target
    if mid not in self._cmdr_forward_map or edge.strength > self._cmdr_forward_map[mid]:
        self._cmdr_forward_map[mid] = edge.strength
```

Add new method to `CausalContext`:

```python
def _chain_bonus(self, candidate_id: str) -> float:
    """Score chain paths: commander → candidate → deck cards.

    Only fires if the candidate is directly linked FROM the commander.
    """
    cmdr_link = self._cmdr_forward_map.get(candidate_id, 0)
    if cmdr_link == 0:
        return 0.0
    bonus = 0.0
    for edge in self._outgoing.get(candidate_id, []):
        if edge.target in self.deck_oids:
            bonus += cmdr_link * edge.strength * 0.5
    return bonus
```

In `causal_score()`, replace `chain_bonus = self._chain_bonus.get(candidate_id, 0.0)` with:

```python
chain_bonus = self._chain_bonus(candidate_id)
```

Remove the old `self._chain_bonus = {}` dict from `__init__`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_chain_scoring.py -v`
Expected: All 3 tests PASS.

- [ ] **Step 5: Run full test suite**

Run: `python3 -m pytest tests/ -v --tb=short`
Expected: All tests PASS.

- [ ] **Step 6: Commit**

```bash
git add mtg_synergy/causal/__init__.py tests/test_chain_scoring.py
git commit -m "feat(causal): add chain bonus scoring to CausalContext"
```

---

## Task 4: Commander Profile Inference

**Files:**
- Create: `mtg_synergy/recommend/commander_profile.py`
- Modify: `mtg_synergy/recommend/scoring.py:56-175` (DeckContext.__init__)
- Test: `tests/test_commander_profile.py` (create)

- [ ] **Step 1: Write failing test for profile inference**

Create `tests/test_commander_profile.py`:

```python
"""Tests for commander archetype inference."""
import sqlite3
import json
import pytest
from mtg_synergy.recommend.commander_profile import (
    CommanderProfile, infer_profile, ensure_profile_schema
)


def test_profile_from_oracle_text_tokens():
    """Krenko's text mentions creating tokens → detected as 'tokens' strategy."""
    profile = infer_profile(
        oracle_text="{T}: Create X 1/1 red Goblin creature tokens, where X is the number of Goblins you control.",
        type_line="Legendary Creature — Goblin Warrior",
        parsed_events_produced={"creature_enters"},
        parsed_events_consumed=set(),
        parsed_effects={"create"},
    )
    assert isinstance(profile, CommanderProfile)
    assert "tokens" in profile.strategies or "go-wide" in profile.strategies
    assert profile.tribal_type == "Goblin"


def test_profile_from_oracle_text_aristocrats():
    """Syr Konrad triggers on creature dying → aristocrats."""
    profile = infer_profile(
        oracle_text="Whenever a creature dies, or a creature card is put into a graveyard from anywhere other than the battlefield, or a creature card leaves your graveyard, Syr Konrad, the Grim deals 1 damage to each opponent.",
        type_line="Legendary Creature — Human Knight",
        parsed_events_produced=set(),
        parsed_events_consumed={"dies"},
        parsed_effects={"deal_damage"},
    )
    assert "aristocrats" in profile.strategies


def test_profile_tribal_from_type_line():
    """Type line with creature subtypes → tribal detection."""
    profile = infer_profile(
        oracle_text="Some ability text.",
        type_line="Legendary Creature — Elf Druid",
        parsed_events_produced=set(),
        parsed_events_consumed=set(),
        parsed_effects=set(),
    )
    # Should detect Elf or Druid as potential tribal type
    assert profile.tribal_type in ("Elf", "Druid")


def test_profile_no_false_positives():
    """Generic commander text shouldn't match every strategy."""
    profile = infer_profile(
        oracle_text="Flying, vigilance",
        type_line="Legendary Creature — Angel",
        parsed_events_produced=set(),
        parsed_events_consumed=set(),
        parsed_effects=set(),
    )
    assert len(profile.strategies) <= 2  # at most voltron or similar


def test_profile_db_roundtrip(tmp_db):
    """Profile can be stored and retrieved from DB."""
    conn = sqlite3.connect(tmp_db)
    ensure_profile_schema(conn)
    profile = CommanderProfile(
        strategies={"tokens", "tribal-goblin"},
        tribal_type="Goblin",
        key_events_produced={"creature_enters"},
        key_events_consumed=set(),
        key_effects={"create"},
    )
    from mtg_synergy.recommend.commander_profile import save_profile, load_profile
    save_profile(conn, "krenko-001", profile)
    loaded = load_profile(conn, "krenko-001")
    assert loaded is not None
    assert loaded.strategies == {"tokens", "tribal-goblin"}
    assert loaded.tribal_type == "Goblin"
    conn.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_commander_profile.py -v`
Expected: FAIL — module `commander_profile` doesn't exist.

- [ ] **Step 3: Implement commander_profile.py**

Create `mtg_synergy/recommend/commander_profile.py`:

```python
"""Commander archetype inference from oracle text + parsed abilities + type line.

Detects strategies, tribal type, and event profiles for any of the 3,141 legal
commanders without needing EDHREC data. Profiles are precomputed and stored in
the commander_profiles table for O(1) lookup at recommendation time.
"""
import json
import re
from dataclasses import dataclass, field
from mtg_synergy.recommend.scoring import STRATEGY_KEYWORDS


@dataclass
class CommanderProfile:
    strategies: set = field(default_factory=set)
    tribal_type: str | None = None
    key_events_produced: set = field(default_factory=set)
    key_events_consumed: set = field(default_factory=set)
    key_effects: set = field(default_factory=set)


# Event → strategy mapping (what consuming these events implies)
_EVENT_TO_STRATEGY = {
    "dies": "aristocrats",
    "enters_graveyard": "reanimator",
    "life_gained": "lifegain",
    "creature_enters": "tokens",  # often — refined by effects
    "attacks": "voltron",
}

# Effect → strategy mapping (what producing these effects implies)
_EFFECT_TO_STRATEGY = {
    "create": "tokens",
    "put_counter": "+1/+1-counters",
    "deal_damage": "burn",
    "mill": "mill",
    "gain_life": "lifegain",
}

# Subtypes that indicate tribal deck potential
_TRIBAL_SUBTYPES = {
    "goblin", "elf", "human", "zombie", "vampire", "dragon", "angel",
    "merfolk", "wizard", "warrior", "knight", "dinosaur", "elemental",
    "demon", "cat", "spirit", "beast", "pirate", "faerie", "rat",
    "sliver", "ally", "cleric", "rogue", "shaman", "soldier", "bird",
    "insect", "fungus", "skeleton", "sphinx", "giant", "werewolf",
}


def infer_profile(
    oracle_text: str,
    type_line: str,
    parsed_events_produced: set[str] | None = None,
    parsed_events_consumed: set[str] | None = None,
    parsed_effects: set[str] | None = None,
) -> CommanderProfile:
    """Infer commander archetype from text and parsed data."""
    strategies = set()
    events_produced = parsed_events_produced or set()
    events_consumed = parsed_events_consumed or set()
    effects = parsed_effects or set()

    # 1. Strategy keywords from oracle text
    oracle_lower = oracle_text.lower()
    for strat, keywords in STRATEGY_KEYWORDS.items():
        hits = sum(1 for kw in keywords if kw in oracle_lower)
        if hits >= 2:
            strategies.add(strat)

    # 2. Event-based strategy detection
    for event, strat in _EVENT_TO_STRATEGY.items():
        if event in events_consumed:
            strategies.add(strat)

    # 3. Effect-based strategy detection
    for eff, strat in _EFFECT_TO_STRATEGY.items():
        if eff in effects:
            strategies.add(strat)

    # 4. Tribal detection from type line
    tribal_type = None
    if type_line and "—" in type_line:
        try:
            subtypes = type_line.split("—")[1].strip().split()
            for st in subtypes:
                if st.lower() in _TRIBAL_SUBTYPES:
                    tribal_type = st
                    break
        except (IndexError, AttributeError):
            pass

    # Also check oracle text for tribal references
    if not tribal_type and tribal_type is None:
        for st in _TRIBAL_SUBTYPES:
            # Pattern: "Goblin creatures you control" or "whenever a Goblin"
            if re.search(rf"\b{st}\b.*\b(you control|enters|dies|gets)\b",
                         oracle_lower, re.IGNORECASE):
                tribal_type = st.title()
                strategies.add(f"tribal-{st}")
                break

    if tribal_type:
        strategies.add(f"tribal-{tribal_type.lower()}")

    return CommanderProfile(
        strategies=strategies,
        tribal_type=tribal_type,
        key_events_produced=events_produced,
        key_events_consumed=events_consumed,
        key_effects=effects,
    )


def ensure_profile_schema(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS commander_profiles (
            oracle_id TEXT PRIMARY KEY,
            strategies TEXT NOT NULL,
            tribal_type TEXT,
            events_produced TEXT NOT NULL,
            events_consumed TEXT NOT NULL,
            key_effects TEXT NOT NULL
        )
    """)
    conn.commit()


def save_profile(conn, oracle_id: str, profile: CommanderProfile):
    conn.execute(
        "INSERT OR REPLACE INTO commander_profiles VALUES (?,?,?,?,?,?)",
        (oracle_id,
         json.dumps(sorted(profile.strategies)),
         profile.tribal_type,
         json.dumps(sorted(profile.key_events_produced)),
         json.dumps(sorted(profile.key_events_consumed)),
         json.dumps(sorted(profile.key_effects))),
    )
    conn.commit()


def load_profile(conn, oracle_id: str) -> CommanderProfile | None:
    row = conn.execute(
        "SELECT strategies, tribal_type, events_produced, events_consumed, key_effects "
        "FROM commander_profiles WHERE oracle_id = ?",
        (oracle_id,)
    ).fetchone()
    if not row:
        return None
    return CommanderProfile(
        strategies=set(json.loads(row[0])),
        tribal_type=row[1],
        key_events_produced=set(json.loads(row[2])),
        key_events_consumed=set(json.loads(row[3])),
        key_effects=set(json.loads(row[4])),
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_commander_profile.py -v`
Expected: All 5 tests PASS.

- [ ] **Step 5: Integrate into DeckContext**

Modify `mtg_synergy/recommend/scoring.py` in `DeckContext.__init__` (after line 165, after `self.llm_scores` loading). Add profile fallback:

```python
# Commander profile fallback (when no strategies provided by caller)
if not self.active_strategies and self.cmdr_oid:
    try:
        from mtg_synergy.recommend.commander_profile import load_profile
        profile = load_profile(conn, self.cmdr_oid)
        if profile:
            self.active_strategies = profile.strategies
            if profile.tribal_type and not self.deck_types:
                self.deck_types = {profile.tribal_type}
                self.is_tribal = True
    except Exception:
        pass
```

- [ ] **Step 6: Run full test suite**

Run: `python3 -m pytest tests/ -v --tb=short`
Expected: All tests PASS.

- [ ] **Step 7: Commit**

```bash
git add mtg_synergy/recommend/commander_profile.py mtg_synergy/recommend/scoring.py tests/test_commander_profile.py
git commit -m "feat: add commander archetype inference from oracle text"
```

---

## Task 5: EDHREC Average Deck Fetcher

**Files:**
- Create: `fetch_edhrec_decks.py`
- Test: `tests/test_recall_evaluation.py` (create)

- [ ] **Step 1: Write failing test for Recall@K computation**

Create `tests/test_recall_evaluation.py`:

```python
"""Tests for Recall@K evaluation metric."""
import pytest


def test_recall_at_k_perfect():
    """If our top K perfectly matches EDHREC, recall = 1.0."""
    from optimize_weights import compute_recall_at_k
    our_top = ["A", "B", "C", "D", "E"]
    edhrec_deck = {"A", "B", "C", "D", "E"}
    assert compute_recall_at_k(our_top, edhrec_deck, k=5) == 1.0


def test_recall_at_k_zero():
    """If our top K has no overlap, recall = 0.0."""
    from optimize_weights import compute_recall_at_k
    our_top = ["X", "Y", "Z"]
    edhrec_deck = {"A", "B", "C"}
    assert compute_recall_at_k(our_top, edhrec_deck, k=3) == 0.0


def test_recall_at_k_partial():
    """Partial overlap."""
    from optimize_weights import compute_recall_at_k
    our_top = ["A", "B", "X", "Y", "Z"]
    edhrec_deck = {"A", "B", "C", "D"}
    # 2 out of 4 edhrec cards found in our top 5
    assert compute_recall_at_k(our_top, edhrec_deck, k=5) == 0.5


def test_recall_at_k_respects_limit():
    """Only considers top K of our list."""
    from optimize_weights import compute_recall_at_k
    our_top = ["X", "Y", "A", "B", "C"]  # A,B,C at positions 3,4,5
    edhrec_deck = {"A", "B", "C"}
    assert compute_recall_at_k(our_top, edhrec_deck, k=2) == 0.0  # only X,Y
    assert compute_recall_at_k(our_top, edhrec_deck, k=5) == 1.0  # all found
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_recall_evaluation.py -v`
Expected: FAIL — `compute_recall_at_k` doesn't exist in `optimize_weights`.

- [ ] **Step 3: Add compute_recall_at_k to optimize_weights.py**

Add to `optimize_weights.py`:

```python
def compute_recall_at_k(our_ranked: list[str], edhrec_deck: set[str], k: int = 100) -> float:
    """Compute Recall@K: fraction of EDHREC deck cards found in our top K.

    Args:
        our_ranked: our cards sorted by score (best first)
        edhrec_deck: set of card names in the EDHREC average deck (non-basic)
        k: how many of our top cards to consider
    """
    if not edhrec_deck:
        return 0.0
    our_top_k = set(our_ranked[:k])
    found = len(our_top_k & edhrec_deck)
    return found / len(edhrec_deck)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_recall_evaluation.py -v`
Expected: All 4 tests PASS.

- [ ] **Step 5: Create fetch_edhrec_decks.py**

Create `fetch_edhrec_decks.py`:

```python
#!/usr/bin/env python3
"""Fetch EDHREC average decklists for top commanders.

Fetches average deck JSON from EDHREC's API and stores card lists
in the edhrec_average_decks table. Rate-limited to 1 req/sec.

Usage:
    python3 fetch_edhrec_decks.py                   # Fetch top 1000
    python3 fetch_edhrec_decks.py --max 100          # Fetch top 100
    python3 fetch_edhrec_decks.py --stats            # Show fetch progress
"""
import argparse
import json
import sqlite3
import time
import urllib.request
import urllib.error

from mtg_synergy.db import get_connection

BASIC_LANDS = {"Plains", "Island", "Swamp", "Mountain", "Forest",
               "Wastes", "Snow-Covered Plains", "Snow-Covered Island",
               "Snow-Covered Swamp", "Snow-Covered Mountain", "Snow-Covered Forest"}

# NOTE: EDHREC JSON structure may change without notice. This was verified 2026-03-25.
# If parsing breaks, inspect the JSON response and update the parsing logic.
API_URL = "https://json.edhrec.com/pages/average-decks/{slug}.json"


def ensure_avg_deck_schema(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS edhrec_average_decks (
            commander_slug TEXT NOT NULL,
            card_name TEXT NOT NULL,
            category TEXT,
            PRIMARY KEY (commander_slug, card_name)
        )
    """)
    conn.commit()


def get_top_slugs(conn, max_slugs=1000):
    """Get top commander slugs by number of synergy entries (most popular first)."""
    rows = conn.execute(
        "SELECT commander_slug, COUNT(*) as cnt FROM edhrec_card_synergy "
        "GROUP BY commander_slug ORDER BY cnt DESC LIMIT ?",
        (max_slugs,)
    ).fetchall()
    return [r[0] for r in rows]


def fetch_average_deck(slug):
    """Fetch average deck from EDHREC JSON API. Returns list of (card_name, category)."""
    url = API_URL.format(slug=slug)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "MTG-Synergy-Graph/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError):
        return None

    cards = []
    # EDHREC JSON structure: data.cardlists[].cardviews[].name
    cardlists = data.get("container", {}).get("json_dict", {}).get("cardlists", [])
    for section in cardlists:
        category = section.get("header", "unknown")
        for card_view in section.get("cardviews", []):
            name = card_view.get("name", "")
            if name and name not in BASIC_LANDS:
                cards.append((name, category))
    return cards


def fetch_all(conn, max_slugs=1000, resume=True):
    """Fetch average decks for top commanders."""
    ensure_avg_deck_schema(conn)
    slugs = get_top_slugs(conn, max_slugs)

    # Skip already-fetched slugs if resuming
    already = set()
    if resume:
        for r in conn.execute("SELECT DISTINCT commander_slug FROM edhrec_average_decks"):
            already.add(r[0])

    remaining = [s for s in slugs if s not in already]
    print(f"Fetching average decks: {len(remaining)} remaining "
          f"({len(already)} already cached) out of {len(slugs)} total")

    fetched = 0
    errors = 0
    for i, slug in enumerate(remaining):
        cards = fetch_average_deck(slug)
        if cards is None:
            errors += 1
            if errors % 10 == 0:
                print(f"  {errors} errors so far...")
            time.sleep(1)
            continue

        for name, category in cards:
            conn.execute(
                "INSERT OR IGNORE INTO edhrec_average_decks VALUES (?,?,?)",
                (slug, name, category)
            )
        conn.commit()
        fetched += 1

        if (i + 1) % 50 == 0:
            print(f"  Fetched {fetched}/{len(remaining)} ({errors} errors)...")

        time.sleep(1)  # Rate limit

    print(f"\nDone: {fetched} fetched, {errors} errors")
    return fetched


def show_stats(conn):
    ensure_avg_deck_schema(conn)
    total = conn.execute("SELECT COUNT(DISTINCT commander_slug) FROM edhrec_average_decks").fetchone()[0]
    cards = conn.execute("SELECT COUNT(*) FROM edhrec_average_decks").fetchone()[0]
    print(f"EDHREC average decks: {total} commanders, {cards} total card entries")
    if total > 0:
        avg = cards / total
        print(f"Average cards per deck: {avg:.0f}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--max", type=int, default=1000)
    parser.add_argument("--stats", action="store_true")
    args = parser.parse_args()

    conn = get_connection()
    if args.stats:
        show_stats(conn)
    else:
        fetch_all(conn, max_slugs=args.max)
    conn.close()


if __name__ == "__main__":
    main()
```

- [ ] **Step 6: Commit**

```bash
git add fetch_edhrec_decks.py optimize_weights.py tests/test_recall_evaluation.py
git commit -m "feat: add EDHREC average deck fetcher + Recall@K metric"
```

---

## Task 6: Recall@K Evaluation in optimize_weights.py

**Files:**
- Modify: `optimize_weights.py:180-230` (main function + evaluation modes)

- [ ] **Step 1: Write failing test for --no-llm evaluation mode**

Append to `tests/test_recall_evaluation.py`:

```python
def test_evaluate_weights_no_llm():
    """Weights with LLM=0 should still produce scores."""
    from optimize_weights import evaluate_weights
    # Minimal precomputed data
    precomputed = {
        "test-commander": [
            ("Card A", 0.5, 3.0, 8),  # (name, edhrec_syn, causal, llm)
            ("Card B", 0.3, 1.0, 6),
            ("Card C", 0.1, 5.0, 0),  # No LLM score
        ]
    }
    score, n = evaluate_weights({"LLM": 0, "CAUSAL": 1.0}, precomputed)
    assert n == 1
    assert isinstance(score, float)
```

- [ ] **Step 2: Run test to verify it passes** (should already work)

Run: `python3 -m pytest tests/test_recall_evaluation.py -v`

- [ ] **Step 3: Extend optimize_weights.py with new evaluation modes**

Add to `optimize_weights.py`:

**a)** Add `--no-llm` and `--novelty` and `--deck` arguments to argparse:

```python
parser.add_argument("--no-llm", action="store_true", help="Evaluate without LLM scores")
parser.add_argument("--novelty", action="store_true", help="Show novel picks not in EDHREC")
parser.add_argument("--deck", type=str, help="Single commander deep dive")
```

**b)** Add `evaluate_recall` function:

```python
def evaluate_recall(conn, precomputed, commander_info, weights, k_values=(30, 50, 100)):
    """Evaluate Recall@K against EDHREC average decks."""
    has_avg = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='edhrec_average_decks'"
    ).fetchone()[0]
    if not has_avg:
        print("No edhrec_average_decks table. Run: python3 fetch_edhrec_decks.py")
        return

    recalls = {k: [] for k in k_values}
    for slug, scored_cards in precomputed.items():
        # Get EDHREC average deck
        avg_deck = set(r[0] for r in conn.execute(
            "SELECT card_name FROM edhrec_average_decks WHERE commander_slug = ?",
            (slug,)))
        if len(avg_deck) < 20:
            continue

        # Rank by our scores
        ranked = sorted(scored_cards, key=lambda x: -(
            x[3] * weights.get("LLM", 0) + x[2] * weights.get("CAUSAL", 0)))
        our_ranked = [name for name, _, _, _ in ranked]

        for k in k_values:
            recalls[k].append(compute_recall_at_k(our_ranked, avg_deck, k))

    for k in k_values:
        if recalls[k]:
            avg = sum(recalls[k]) / len(recalls[k])
            print(f"  Recall@{k}: {avg:.1%} ({len(recalls[k])} commanders)")
```

**c)** Add `novelty_report` function:

```python
def novelty_report(conn, precomputed, weights, slug_filter=None, top_k=100):
    """Show cards we recommend that EDHREC average deck doesn't include."""
    has_avg = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='edhrec_average_decks'"
    ).fetchone()[0]
    if not has_avg:
        print("No edhrec_average_decks table. Run: python3 fetch_edhrec_decks.py")
        return

    slugs = [slug_filter] if slug_filter else list(precomputed.keys())[:10]
    for slug in slugs:
        if slug not in precomputed:
            continue
        avg_deck = set(r[0] for r in conn.execute(
            "SELECT card_name FROM edhrec_average_decks WHERE commander_slug = ?", (slug,)))
        if len(avg_deck) < 20:
            continue

        scored = precomputed[slug]
        ranked = sorted(scored, key=lambda x: -(
            x[3] * weights.get("LLM", 0) + x[2] * weights.get("CAUSAL", 0)))
        our_top = [name for name, _, _, _ in ranked[:top_k]]

        novel = [c for c in our_top if c not in avg_deck]
        in_edhrec = [c for c in our_top if c in avg_deck]
        recall = len(in_edhrec) / len(avg_deck) if avg_deck else 0

        print(f"\n{'='*60}")
        print(f"{slug}: Recall@{top_k}={recall:.0%} | {len(novel)} novel picks")
        print(f"  Novel (not in EDHREC avg deck):")
        for c in novel[:15]:
            match = next((s for s in scored if s[0] == c), None)
            if match:
                print(f"    {c}  (causal={match[2]:.1f}, llm={match[3]})")
```

**d)** Wire into `main()`:

```python
if args.evaluate:
    weights = {"LLM": 0 if args.no_llm else SCORING_WEIGHTS.get("LLM", 10),
               "CAUSAL": SCORING_WEIGHTS.get("CAUSAL", 2)}
    mode = "no-LLM" if args.no_llm else "all signals"
    print(f"\nEvaluating ({mode}): {weights}")
    score, n = evaluate_weights(weights, precomputed)
    print(f"Top-30 overlap: {score:.1f}/30 ({n} commanders)")
    evaluate_recall(conn, precomputed, commander_info, weights)

    if args.novelty:
        novelty_report(conn, precomputed, weights, slug_filter=args.deck)
    elif args.deck:
        novelty_report(conn, precomputed, weights, slug_filter=args.deck)
```

- [ ] **Step 4: Run full test suite**

Run: `python3 -m pytest tests/ -v --tb=short`
Expected: All tests PASS.

- [ ] **Step 5: Commit**

```bash
git add optimize_weights.py tests/test_recall_evaluation.py
git commit -m "feat: add Recall@K evaluation + --no-llm mode to optimizer"
```

---

## Task 7: Expand Parsing + Rebuild Graph + Evaluate

This task is operational (running pipelines), not code changes.

- [ ] **Step 1: Back up current graph**

```bash
python3 -c "
from mtg_synergy.db import get_connection
conn = get_connection()
conn.execute('CREATE TABLE IF NOT EXISTS interaction_edges_v1 AS SELECT * FROM interaction_edges')
conn.commit()
print('Backed up', conn.execute('SELECT COUNT(*) FROM interaction_edges_v1').fetchone()[0], 'edges')
conn.close()
"
```

- [ ] **Step 2: Expand parsing to 15k cards**

```bash
python3 oracle_parser.py --parse-all --top 15000
```

Expected: ~15000 cards parsed, 0 or near-0 failures. Monitor output for failure count.

- [ ] **Step 3: Rebuild graph with IDF-weighted edges**

```bash
python3 build_graph.py --rebuild
```

Expected: More edges than before (~3-5M), but IDF-weighted strengths.

- [ ] **Step 4: Run evaluation — compare old vs new graph**

```bash
# Evaluate new IDF graph
python3 optimize_weights.py --evaluate
# Evaluate with no LLM
python3 optimize_weights.py --evaluate --no-llm
```

Expected: Top-30 overlap should not regress below 0.675 (LLM mode). Causal-only score should improve from 0.571.

- [ ] **Step 5: If new graph is worse, rollback**

Only if evaluation shows regression:
```bash
python3 -c "
from mtg_synergy.db import get_connection
conn = get_connection()
conn.execute('DROP TABLE interaction_edges')
conn.execute('ALTER TABLE interaction_edges_v1 RENAME TO interaction_edges')
conn.commit()
print('Rolled back')
conn.close()
"
```

- [ ] **Step 6: Fetch EDHREC average decks**

```bash
python3 fetch_edhrec_decks.py --max 1000
```

Expected: ~1000 average decklists fetched (~20 min). Some errors expected (missing pages).

- [ ] **Step 7: Run Recall@K evaluation**

```bash
python3 optimize_weights.py --evaluate
python3 optimize_weights.py --evaluate --no-llm
```

Record baseline Recall@100 numbers for both modes.

- [ ] **Step 8: Commit any config/weight changes**

```bash
git add mtg_synergy/config.py data/
git commit -m "feat: expand parsing to 15k + rebuild IDF graph + baseline Recall@K"
```

---

## Task 8: Build Commander Profiles for All Commanders

- [ ] **Step 1: Write a profile builder CLI**

Add to `mtg_synergy/recommend/commander_profile.py`:

```python
def build_all_profiles(conn):
    """Build profiles for all legal commanders in the DB."""
    ensure_profile_schema(conn)

    # Get all legendary creatures that can be commanders
    commanders = conn.execute("""
        SELECT c.oracle_id, c.name, c.oracle_text, c.type_line
        FROM cards c
        WHERE c.type_line LIKE '%Legendary%Creature%'
           OR c.oracle_text LIKE '%can be your commander%'
        ORDER BY c.edhrec_rank ASC NULLS LAST
    """).fetchall()

    built = 0
    for oid, name, oracle, type_line in commanders:
        if not oracle:
            oracle = ""

        # Get parsed events if available
        events_produced = set()
        events_consumed = set()
        effects = set()
        for row in conn.execute(
            "SELECT ast_json FROM parsed_abilities WHERE oracle_id = ?", (oid,)
        ).fetchall():
            try:
                import json as _j
                d = _j.loads(row[0])
                for eff in d.get("effects", []):
                    verb = eff.get("verb", "")
                    if verb:
                        effects.add(verb)
                trigger = d.get("trigger")
                if trigger:
                    events_consumed.add(trigger.get("event", ""))
            except Exception:
                pass

        # Infer from verb resolvers what events are produced
        # (simplified: use parsed ability effects → state changes)
        try:
            from mtg_synergy.parse.verb_resolvers import resolve_effect
            from mtg_synergy.parse.ast_types import Effect, Amount
            for eff_verb in effects:
                dummy = Effect(verb=eff_verb, amount=Amount(value=1))
                for sc in resolve_effect(dummy):
                    if sc.event:
                        events_produced.add(sc.event)
        except Exception:
            pass

        profile = infer_profile(oracle, type_line or "",
                                events_produced, events_consumed, effects)
        save_profile(conn, oid, profile)
        built += 1

    print(f"Built {built} commander profiles")
    return built
```

- [ ] **Step 2: Run profile builder**

```bash
python3 -c "
from mtg_synergy.db import get_connection
from mtg_synergy.recommend.commander_profile import build_all_profiles
conn = get_connection()
n = build_all_profiles(conn)
conn.close()
print(f'Done: {n} profiles')
"
```

Expected: ~3000+ profiles built.

- [ ] **Step 3: Verify profiles for known commanders**

```bash
python3 -c "
from mtg_synergy.db import get_connection
from mtg_synergy.recommend.commander_profile import load_profile
conn = get_connection()
# Check Krenko
row = conn.execute(\"SELECT oracle_id FROM cards WHERE name = 'Krenko, Mob Boss'\").fetchone()
if row:
    p = load_profile(conn, row[0])
    print(f'Krenko: strategies={p.strategies}, tribal={p.tribal_type}')
# Check Syr Konrad
row = conn.execute(\"SELECT oracle_id FROM cards WHERE name LIKE 'Syr Konrad%'\").fetchone()
if row:
    p = load_profile(conn, row[0])
    print(f'Syr Konrad: strategies={p.strategies}')
conn.close()
"
```

Expected: Krenko → tokens + tribal-goblin, Syr Konrad → aristocrats.

- [ ] **Step 4: Commit**

```bash
git add mtg_synergy/recommend/commander_profile.py
git commit -m "feat: build commander profiles for all 3k+ legal commanders"
```

---

## Task 9: Weight Rebalancing + Final Evaluation

- [ ] **Step 1: Run grid search with IDF graph**

```bash
python3 optimize_weights.py --quick  # Fast mode first (50 commanders)
```

Record the new best weights.

- [ ] **Step 2: Full optimization**

```bash
python3 optimize_weights.py  # All 502 commanders
```

Expected: New optimal CAUSAL weight should be > 0 (IDF made causal useful).

- [ ] **Step 3: Update SCORING_WEIGHTS in config.py**

Based on grid search results, update the CAUSAL weight in `mtg_synergy/config.py:31`:

```python
"CAUSAL": <new_best_weight>,  # Updated from optimizer
```

- [ ] **Step 4: Final Recall@K evaluation**

```bash
python3 optimize_weights.py --evaluate           # All signals
python3 optimize_weights.py --evaluate --no-llm  # Coverage mode
```

Record final numbers. Compare against success criteria:
- Recall@100 (all signals): target > 45%
- Recall@100 (no LLM): target > 30%

- [ ] **Step 5: Run full test suite**

```bash
python3 -m pytest tests/ -v --tb=short
```

Expected: All tests PASS.

- [ ] **Step 6: Commit final weights**

```bash
git add mtg_synergy/config.py
git commit -m "feat: rebalance weights after IDF graph optimization"
```

---

## Task 10: Cleanup + Documentation

- [ ] **Step 1: Update CLAUDE.md with new pipeline steps**

Add oracle_parser 15k, IDF graph, commander profiles, Recall@K to the pipeline docs and architecture section.

- [ ] **Step 2: Run tests one final time**

```bash
python3 -m pytest tests/ -v
```

- [ ] **Step 3: Commit documentation updates**

```bash
git add CLAUDE.md
git commit -m "docs: update CLAUDE.md with IDF graph, profiles, and Recall@K"
```
