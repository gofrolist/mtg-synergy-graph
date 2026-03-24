# MTG Rules Engine — Implementation Plan (Part 2: Graph Builder + Chain Discovery + Integration)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the causal interaction graph from parsed abilities, discover N-card combo chains and infinite loops, and integrate the causal signal into the existing recommendation pipeline.

**Architecture:** The graph builder indexes what each card produces/responds-to from the parsed ASTs (Part 1), then cross-matches to create trigger/feeds/amplifies/enables edges stored in SQLite. The chain finder runs DFS with resource tracking to discover linear chains and infinite loops. The causal score integrates as a new feature in the existing `compute_dynamic_score()`.

**Tech Stack:** Python 3.14, SQLite, pytest. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-03-24-rules-engine-design.md` (Layers 3-5)

**Depends on:** Part 1 complete — `mtg_synergy/parse/` package with `parse_card()`, `verb_resolvers.resolve_effect()`, `StateChange`, and `parsed_abilities` table with 5000 cards parsed.

---

## File Structure

```
mtg_synergy/
├── causal/                        ← NEW PACKAGE
│   ├── __init__.py                # Public API: build_graph(), find_chains(), causal_score()
│   ├── types.py                   # Edge, EdgeDetail, Chain, ResourceDelta, LoopAnalysis dataclasses
│   ├── indexer.py                 # Index cards by events produced/consumed
│   ├── graph_builder.py           # Match producers × responders → edges
│   ├── chain_finder.py            # DFS chain discovery + loop detection
│   └── resource_flow.py           # Cost/production tracking for loop validation
├── recommend/
│   ├── scoring.py                 # MODIFIED — add _compute_causal_score()
│   └── ...
├── config.py                      # MODIFIED — add CAUSAL weight
build_graph.py                     # NEW — CLI entry point for graph building
tests/
├── test_causal_types.py           # Edge/Chain dataclass tests
├── test_indexer.py                # Indexing tests
├── test_graph_builder.py          # Edge building tests (known pairs)
├── test_chain_finder.py           # Chain/loop discovery tests
├── test_resource_flow.py          # Resource tracking tests
└── test_causal_integration.py     # Integration with recommendation pipeline
```

---

### Task 1: Causal Type Definitions

**Files:**
- Create: `mtg_synergy/causal/__init__.py`
- Create: `mtg_synergy/causal/types.py`
- Test: `tests/test_causal_types.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_causal_types.py
"""Tests for causal graph dataclasses."""
import json
import pytest


def test_edge_detail():
    from mtg_synergy.causal.types import EdgeDetail
    d = EdgeDetail(event="enters_the_battlefield", filter_precision="exact")
    assert d.event == "enters_the_battlefield"
    assert d.filter_precision == "exact"
    assert d.resource is None


def test_edge():
    from mtg_synergy.causal.types import Edge, EdgeDetail
    e = Edge(
        source="card-a", target="card-b", edge_type="triggers",
        ability_a=0, ability_b=0, strength=1.0,
        detail=EdgeDetail(event="dies", filter_precision="exact"),
    )
    assert e.source == "card-a"
    assert e.edge_type == "triggers"
    assert e.detail.event == "dies"


def test_resource_delta():
    from mtg_synergy.causal.types import ResourceDelta
    rd = ResourceDelta(mana=1, creatures=-1, cards=0, life=0)
    assert rd.is_positive  # mana positive, creatures negative but mana drives the loop


def test_resource_delta_negative():
    from mtg_synergy.causal.types import ResourceDelta
    rd = ResourceDelta(mana=-2, creatures=0, cards=0, life=0)
    assert not rd.is_positive


def test_loop_analysis():
    from mtg_synergy.causal.types import LoopAnalysis
    la = LoopAnalysis(
        is_infinite="confirmed",
        min_board_requirement="2+ Goblins",
        resource_deltas={"mana": 1, "creatures": 1},
        growth_pattern="exponential",
    )
    assert la.is_infinite == "confirmed"
    assert la.growth_pattern == "exponential"


def test_chain():
    from mtg_synergy.causal.types import Chain, Edge, EdgeDetail, ResourceDelta
    c = Chain(
        cards=["commander", "card-a", "card-b"],
        edges=[
            Edge("commander", "card-a", "triggers", 0, 0, 1.0,
                 EdgeDetail(event="enters_the_battlefield", filter_precision="exact")),
            Edge("card-a", "card-b", "triggers", 0, 0, 0.8,
                 EdgeDetail(event="dies", filter_precision="broad")),
        ],
        chain_type="linear",
        output="damage",
        resource_delta=ResourceDelta(mana=0, creatures=0, cards=0, life=0),
    )
    assert len(c.cards) == 3
    assert c.chain_type == "linear"


def test_edge_to_dict():
    from mtg_synergy.causal.types import Edge, EdgeDetail
    e = Edge("a", "b", "triggers", 0, 1, 0.9,
             EdgeDetail(event="dies", filter_precision="exact"))
    d = e.to_dict()
    assert d["source"] == "a"
    assert d["detail"]["event"] == "dies"
    j = json.dumps(d)
    assert "dies" in j
```

- [ ] **Step 2: Run tests, verify fail**

Run: `python3 -m pytest tests/test_causal_types.py -v`

- [ ] **Step 3: Implement types**

Create `mtg_synergy/causal/__init__.py` (empty) and `mtg_synergy/causal/types.py`:

```python
from dataclasses import dataclass, field

@dataclass
class EdgeDetail:
    event: str | None = None
    resource: str | None = None
    verb_modified: str | None = None
    scaling: str | None = None
    filter_precision: str = "broad"

    def to_dict(self):
        return {k: v for k, v in self.__dict__.items() if v is not None}

@dataclass
class Edge:
    source: str
    target: str
    edge_type: str
    ability_a: int
    ability_b: int
    strength: float
    detail: EdgeDetail

    def to_dict(self):
        d = {k: v for k, v in self.__dict__.items() if k != "detail"}
        d["detail"] = self.detail.to_dict()
        return d

@dataclass
class ResourceDelta:
    mana: int = 0
    creatures: int = 0
    cards: int = 0
    life: int = 0

    @property
    def is_positive(self) -> bool:
        """A loop is sustainable if no resource goes negative.
        All tracked resources must be >= 0 for a confirmed infinite loop.
        A loop that produces creatures but costs mana each cycle will drain out."""
        resources = [self.mana, self.creatures, self.cards]
        return any(r > 0 for r in resources) and all(r >= 0 for r in resources)

@dataclass
class LoopAnalysis:
    is_infinite: str           # "confirmed", "conditional", "potential"
    min_board_requirement: str | None = None
    resource_deltas: dict = field(default_factory=dict)
    growth_pattern: str = "fixed"

@dataclass
class Chain:
    cards: list[str]
    edges: list[Edge]
    chain_type: str            # "linear", "loop", "amplified"
    output: str = ""
    resource_delta: ResourceDelta = field(default_factory=ResourceDelta)
    loop_analysis: LoopAnalysis | None = None
    bottleneck: str | None = None
    score: float = 0.0
```

- [ ] **Step 4: Run tests, verify pass**
- [ ] **Step 5: Commit**

```bash
git add mtg_synergy/causal/__init__.py mtg_synergy/causal/types.py tests/test_causal_types.py
git commit -m "feat(causal): add type definitions for interaction graph"
```

---

### Task 2: Event Indexer

**Files:**
- Create: `mtg_synergy/causal/indexer.py`
- Test: `tests/test_indexer.py`

The indexer pre-processes all parsed cards into lookup structures: what events each card produces, what events each card responds to, what resources each card consumes/produces.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_indexer.py
"""Tests for event indexing from parsed abilities."""
import pytest
from mtg_synergy.causal.indexer import CardIndex, build_index
from mtg_synergy.parse.ast_types import (
    Ability, Trigger, Effect, Amount, ObjectFilter, TokenDef, Cost, ManaAmount
)
from mtg_synergy.parse.verb_resolvers import resolve_effect


def _make_purphoros():
    """Triggered: creature enters → deal 2 damage."""
    return ("purphoros", [Ability(
        kind="triggered",
        trigger=Trigger(event="enters_the_battlefield",
                        subject=ObjectFilter(card_type="creature", controller="you")),
        effects=[Effect(verb="deal_damage", amount=Amount(value=2),
                        target=ObjectFilter(controller="opponent"))],
    )])


def _make_krenko():
    """Activated: tap → create X Goblin tokens."""
    return ("krenko", [Ability(
        kind="activated",
        cost=Cost(tap=True),
        effects=[Effect(verb="create", amount=Amount(value="X"),
                        token=TokenDef(card_type="creature", subtype="Goblin",
                                       power=1, toughness=1, keywords=[], color="red"))],
    )])


def _make_phyrexian_altar():
    """Activated: sacrifice creature → add mana."""
    return ("altar", [Ability(
        kind="activated",
        cost=Cost(sacrifice=ObjectFilter(card_type="creature")),
        effects=[Effect(verb="add_mana", amount=Amount(value=1))],
    )])


def test_build_index_producers():
    """Krenko produces creature_enters and enters_the_battlefield."""
    cards = dict([_make_krenko()])
    idx = build_index(cards)
    events = idx.events_produced_by("krenko")
    event_names = {e.event for e in events}
    assert "enters_the_battlefield" in event_names
    assert "creature_enters" in event_names


def test_build_index_responders():
    """Purphoros responds to enters_the_battlefield."""
    cards = dict([_make_purphoros()])
    idx = build_index(cards)
    assert "purphoros" in idx.cards_responding_to("enters_the_battlefield")


def test_build_index_consumers():
    """Phyrexian Altar consumes creatures."""
    cards = dict([_make_phyrexian_altar()])
    idx = build_index(cards)
    assert "altar" in idx.cards_consuming("creature")


def test_producers_for_event():
    """Find all cards that produce a given event type."""
    cards = dict([_make_krenko(), _make_purphoros()])
    idx = build_index(cards)
    # Krenko produces creature_enters; Purphoros produces damage_dealt
    producers = idx.cards_producing("creature_enters")
    assert "krenko" in producers
    assert "purphoros" not in producers


def test_index_empty():
    idx = build_index({})
    assert idx.cards_producing("anything") == []
    assert idx.cards_responding_to("anything") == []
```

- [ ] **Step 2: Run tests, verify fail**
- [ ] **Step 3: Implement indexer**

Create `mtg_synergy/causal/indexer.py`:

```python
"""Index parsed cards by what events they produce and respond to."""
from collections import defaultdict
from dataclasses import dataclass, field
from mtg_synergy.parse.ast_types import Ability
from mtg_synergy.parse.verb_resolvers import resolve_effect, StateChange


@dataclass
class CardIndex:
    """Pre-computed lookup structures for fast graph building."""
    # event_type → [(card_id, ability_index, StateChange)]
    _producers: dict = field(default_factory=lambda: defaultdict(list))
    # event_type → [(card_id, ability_index, Trigger)]
    _responders: dict = field(default_factory=lambda: defaultdict(list))
    # resource_type → [(card_id, ability_index, Cost)]
    _consumers: dict = field(default_factory=lambda: defaultdict(list))
    # verb → [(card_id, ability_index, Ability)] for replacement/modifier matching
    _modifiers: dict = field(default_factory=lambda: defaultdict(list))
    # card_id → [StateChange] cached
    _card_events: dict = field(default_factory=lambda: defaultdict(list))

    def events_produced_by(self, card_id: str) -> list[StateChange]:
        return self._card_events.get(card_id, [])

    def cards_producing(self, event_type: str) -> list[str]:
        return list({cid for cid, _, _ in self._producers.get(event_type, [])})

    def cards_responding_to(self, event_type: str) -> list[str]:
        return list({cid for cid, _, _ in self._responders.get(event_type, [])})

    def cards_consuming(self, resource_type: str) -> list[str]:
        return list({cid for cid, _, _ in self._consumers.get(resource_type, [])})

    def producers_for(self, event_type: str):
        return self._producers.get(event_type, [])

    def responders_for(self, event_type: str):
        return self._responders.get(event_type, [])


def build_index(cards: dict[str, list[Ability]]) -> CardIndex:
    """Build lookup index from parsed abilities.

    Args:
        cards: {oracle_id: [Ability, ...]} from parsed_abilities table
    """
    idx = CardIndex()
    for card_id, abilities in cards.items():
        for ab_idx, ability in enumerate(abilities):
            # Index what this card produces (via verb resolvers)
            for effect in ability.effects:
                state_changes = resolve_effect(effect)
                for sc in state_changes:
                    idx._producers[sc.event].append((card_id, ab_idx, sc))
                    idx._card_events[card_id].append(sc)

            # Index what this card responds to (triggers)
            if ability.trigger:
                idx._responders[ability.trigger.event].append(
                    (card_id, ab_idx, ability.trigger))

            # Index what this card consumes (costs)
            if ability.cost:
                if ability.cost.sacrifice:
                    resource = ability.cost.sacrifice.card_type or "permanent"
                    idx._consumers[resource].append((card_id, ab_idx, ability.cost))
                if ability.cost.mana and ability.cost.mana.total > 0:
                    idx._consumers["mana"].append((card_id, ab_idx, ability.cost))

            # Index modifiers (replacement effects, trigger modifiers)
            if ability.kind in ("replacement", "trigger_modifier"):
                # Index by what verb/event they modify
                idx._modifiers[ability.kind].append((card_id, ab_idx, ability))

    return idx
```

- [ ] **Step 4: Run tests, verify pass**
- [ ] **Step 5: Commit**

```bash
git add mtg_synergy/causal/indexer.py tests/test_indexer.py
git commit -m "feat(causal): add event indexer for parsed abilities"
```

---

### Task 3: Graph Builder (Trigger + Feeds Edges)

**Files:**
- Create: `mtg_synergy/causal/graph_builder.py`
- Test: `tests/test_graph_builder.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_graph_builder.py
"""Tests for causal edge building between card pairs."""
import pytest
from mtg_synergy.causal.graph_builder import build_causal_edges
from mtg_synergy.causal.types import Edge
from mtg_synergy.parse.ast_types import (
    Ability, Trigger, Effect, Amount, ObjectFilter, TokenDef, Cost
)


def _make_card(name, abilities):
    return (name, abilities)


def test_trigger_edge_krenko_purphoros():
    """Krenko creates Goblins → Purphoros triggers on creature enters."""
    cards = dict([
        _make_card("krenko", [Ability(
            kind="activated", cost=Cost(tap=True),
            effects=[Effect(verb="create", amount=Amount(value=2),
                            token=TokenDef("creature", "Goblin", 1, 1, [], "red"))],
        )]),
        _make_card("purphoros", [Ability(
            kind="triggered",
            trigger=Trigger(event="enters_the_battlefield",
                            subject=ObjectFilter(card_type="creature", controller="you")),
            effects=[Effect(verb="deal_damage", amount=Amount(value=2),
                            target=ObjectFilter(controller="opponent"))],
        )]),
    ])
    edges = build_causal_edges(cards)
    trigger_edges = [e for e in edges if e.edge_type == "triggers"
                     and e.source == "krenko" and e.target == "purphoros"]
    assert len(trigger_edges) >= 1
    assert trigger_edges[0].detail.event == "enters_the_battlefield"
    assert trigger_edges[0].strength >= 0.5


def test_trigger_edge_exact_subtype():
    """Tribal trigger: 'whenever a Goblin enters' + Goblin creator = exact match (1.0)."""
    cards = dict([
        _make_card("producer", [Ability(
            kind="activated",
            effects=[Effect(verb="create", amount=Amount(value=1),
                            token=TokenDef("creature", "Goblin", 1, 1, [], "red"))],
        )]),
        _make_card("responder", [Ability(
            kind="triggered",
            trigger=Trigger(event="enters_the_battlefield",
                            subject=ObjectFilter(card_type="creature", subtype="Goblin")),
            effects=[Effect(verb="draw", amount=Amount(value=1))],
        )]),
    ])
    edges = build_causal_edges(cards)
    trigger_edges = [e for e in edges if e.edge_type == "triggers"]
    assert len(trigger_edges) >= 1
    assert trigger_edges[0].detail.filter_precision == "exact"
    assert trigger_edges[0].strength == 1.0


def test_trigger_edge_broad_match():
    """Broad: 'whenever a creature enters' + Goblin creator = broad (0.6)."""
    cards = dict([
        _make_card("producer", [Ability(
            kind="activated",
            effects=[Effect(verb="create", amount=Amount(value=1),
                            token=TokenDef("creature", "Goblin", 1, 1, [], "red"))],
        )]),
        _make_card("responder", [Ability(
            kind="triggered",
            trigger=Trigger(event="enters_the_battlefield",
                            subject=ObjectFilter(card_type="creature")),
            effects=[Effect(verb="draw", amount=Amount(value=1))],
        )]),
    ])
    edges = build_causal_edges(cards)
    trigger_edges = [e for e in edges if e.edge_type == "triggers"]
    assert len(trigger_edges) >= 1
    assert trigger_edges[0].detail.filter_precision == "broad"
    assert trigger_edges[0].strength == pytest.approx(0.6, abs=0.1)


def test_no_self_edge():
    """Cards don't create edges to themselves."""
    cards = dict([
        _make_card("card", [
            Ability(kind="activated",
                    effects=[Effect(verb="create", amount=Amount(value=1),
                                    token=TokenDef("creature", "Goblin", 1, 1, [], "red"))]),
            Ability(kind="triggered",
                    trigger=Trigger(event="enters_the_battlefield",
                                    subject=ObjectFilter(card_type="creature")),
                    effects=[Effect(verb="draw", amount=Amount(value=1))]),
        ]),
    ])
    edges = build_causal_edges(cards)
    assert all(e.source != e.target for e in edges)


def test_no_match_nontoken_filter():
    """Nontoken trigger doesn't match token producer."""
    cards = dict([
        _make_card("producer", [Ability(
            kind="activated",
            effects=[Effect(verb="create", amount=Amount(value=1),
                            token=TokenDef("creature", "Goblin", 1, 1, [], "red"))],
        )]),
        _make_card("responder", [Ability(
            kind="triggered",
            trigger=Trigger(event="enters_the_battlefield",
                            subject=ObjectFilter(card_type="creature", is_token=False)),
            effects=[Effect(verb="draw", amount=Amount(value=1))],
        )]),
    ])
    edges = build_causal_edges(cards)
    trigger_edges = [e for e in edges if e.edge_type == "triggers"]
    assert len(trigger_edges) == 0


def test_feeds_edge_altar():
    """Phyrexian Altar consumes creatures; Krenko produces them."""
    cards = dict([
        _make_card("krenko", [Ability(
            kind="activated", cost=Cost(tap=True),
            effects=[Effect(verb="create", amount=Amount(value=2),
                            token=TokenDef("creature", "Goblin", 1, 1, [], "red"))],
        )]),
        _make_card("altar", [Ability(
            kind="activated",
            cost=Cost(sacrifice=ObjectFilter(card_type="creature")),
            effects=[Effect(verb="add_mana", amount=Amount(value=1))],
        )]),
    ])
    edges = build_causal_edges(cards)
    feeds = [e for e in edges if e.edge_type == "feeds"
             and e.source == "krenko" and e.target == "altar"]
    assert len(feeds) >= 1
    assert feeds[0].detail.resource == "creature"


def test_no_edges_unrelated():
    """Unrelated cards produce no edges."""
    cards = dict([
        _make_card("ramp", [Ability(
            kind="activated",
            effects=[Effect(verb="add_mana", amount=Amount(value=1))],
        )]),
        _make_card("draw", [Ability(
            kind="triggered",
            trigger=Trigger(event="enters_the_battlefield",
                            subject=ObjectFilter(card_type="creature")),
            effects=[Effect(verb="draw", amount=Amount(value=1))],
        )]),
    ])
    edges = build_causal_edges(cards)
    # add_mana produces no events, so no trigger edges
    trigger_edges = [e for e in edges if e.edge_type == "triggers"]
    assert len(trigger_edges) == 0


def test_bidirectional_edges():
    """If A triggers B and B triggers A, both edges exist."""
    cards = dict([
        _make_card("a", [
            Ability(kind="triggered",
                    trigger=Trigger(event="dies", subject=ObjectFilter(card_type="creature")),
                    effects=[Effect(verb="create", amount=Amount(value=1),
                                    token=TokenDef("creature", "Zombie", 2, 2, [], "black"))]),
        ]),
        _make_card("b", [
            Ability(kind="triggered",
                    trigger=Trigger(event="enters_the_battlefield",
                                    subject=ObjectFilter(card_type="creature")),
                    effects=[Effect(verb="sacrifice", amount=Amount(value=1),
                                    target=ObjectFilter(card_type="creature"))]),
        ]),
    ])
    edges = build_causal_edges(cards)
    a_to_b = [e for e in edges if e.source == "a" and e.target == "b"]
    b_to_a = [e for e in edges if e.source == "b" and e.target == "a"]
    assert len(a_to_b) >= 1  # a creates zombie → b triggers on ETB
    assert len(b_to_a) >= 1  # b sacrifices → creature dies → a triggers on dies
```

- [ ] **Step 2: Run tests, verify fail**
- [ ] **Step 3: Implement graph builder**

Create `mtg_synergy/causal/graph_builder.py`:

```python
"""Build causal interaction edges between cards."""
from mtg_synergy.causal.indexer import build_index
from mtg_synergy.causal.types import Edge, EdgeDetail
from mtg_synergy.parse.ast_types import Ability, ObjectFilter


def build_causal_edges(cards: dict[str, list[Ability]]) -> list[Edge]:
    """Build all causal edges from parsed abilities.

    Edge types:
    - triggers: A's effect produces event that B's trigger responds to
    - feeds: A produces resource that B consumes as cost
    """
    idx = build_index(cards)
    edges = []

    # 1. TRIGGER edges: match producers × responders per event type
    all_events = set(idx._producers.keys())
    for event_type in all_events:
        producers = idx.producers_for(event_type)
        responders = idx.responders_for(event_type)
        for prod_card, prod_ab, state_change in producers:
            for resp_card, resp_ab, trigger in responders:
                if prod_card == resp_card:
                    continue  # no self-edges
                precision = _compute_filter_precision(trigger.subject, state_change.object)
                if precision == "none":
                    continue  # filter rejects match
                strength = _precision_to_strength(precision)
                edges.append(Edge(
                    source=prod_card, target=resp_card,
                    edge_type="triggers",
                    ability_a=prod_ab, ability_b=resp_ab,
                    strength=strength,
                    detail=EdgeDetail(event=event_type, filter_precision=precision),
                ))

    # 2. FEEDS edges: match resource producers × consumers
    # 2a. Creature feeds: cards that create creatures → cards that sacrifice creatures
    creature_producers = idx.cards_producing("creature_enters")
    creature_consumers = idx.cards_consuming("creature")
    for prod_card in creature_producers:
        for cons_card in creature_consumers:
            if prod_card == cons_card:
                continue
            edges.append(Edge(
                source=prod_card, target=cons_card,
                edge_type="feeds",
                ability_a=0, ability_b=0,
                strength=0.8,
                detail=EdgeDetail(resource="creature", filter_precision="broad"),
            ))

    # 2b. Mana feeds: cards that produce mana → cards that consume mana
    # Note: add_mana produces no StateChanges (it's a resource, not an event),
    # so we check effect verbs directly rather than going through the index.
    mana_producers = set()
    mana_consumers = idx.cards_consuming("mana")
    for card_id, abilities in cards.items():
        for ability in abilities:
            for effect in ability.effects:
                if effect.verb == "add_mana":
                    mana_producers.add(card_id)
    for prod_card in mana_producers:
        for cons_card_id in mana_consumers:
            cons_card = cons_card_id  # already a card_id from index
            if prod_card == cons_card:
                continue
            edges.append(Edge(
                source=prod_card, target=cons_card,
                edge_type="feeds",
                ability_a=0, ability_b=0,
                strength=0.5,  # mana feeds are weaker signal than creature feeds
                detail=EdgeDetail(resource="mana", filter_precision="broad"),
            ))

    # Deduplicate: keep strongest edge per (source, target, type)
    return _deduplicate(edges)


def _compute_filter_precision(trigger_subject: ObjectFilter | None,
                               state_change_object: ObjectFilter | None) -> str:
    """How precisely does the state change match the trigger filter?"""
    if trigger_subject is None:
        return "unfiltered"
    if state_change_object is None:
        return "broad"

    # Check token filter
    if trigger_subject.is_token is False and state_change_object.is_token is True:
        return "none"  # nontoken filter rejects tokens

    # Check subtype
    if trigger_subject.subtype:
        if state_change_object.subtype:
            if trigger_subject.subtype.lower() == state_change_object.subtype.lower():
                return "exact"
            else:
                return "none"  # wrong subtype
        else:
            return "broad"  # trigger wants specific, producer is generic

    # Check card type
    if trigger_subject.card_type:
        if state_change_object.card_type:
            if trigger_subject.card_type == state_change_object.card_type:
                return "broad"  # type matches but no subtype specificity
            elif trigger_subject.card_type == "permanent":
                return "unfiltered"
            else:
                return "none"  # wrong type
        return "broad"

    return "unfiltered"


def _precision_to_strength(precision: str) -> float:
    return {"exact": 1.0, "broad": 0.6, "unfiltered": 0.3, "none": 0.0}[precision]


def _deduplicate(edges: list[Edge]) -> list[Edge]:
    """Keep strongest edge per (source, target, edge_type, ability_b).

    Preserves which responder ability fires (needed for resource flow),
    but deduplicates when multiple producer abilities trigger the same responder.
    """
    best = {}
    for edge in edges:
        key = (edge.source, edge.target, edge.edge_type, edge.ability_b)
        if key not in best or edge.strength > best[key].strength:
            best[key] = edge
    return list(best.values())
```

- [ ] **Step 4: Run tests, verify pass**
- [ ] **Step 5: Commit**

```bash
git add mtg_synergy/causal/graph_builder.py tests/test_graph_builder.py
git commit -m "feat(causal): add graph builder with trigger + feeds edges"
```

---

### Task 4: Resource Flow Tracking

**Files:**
- Create: `mtg_synergy/causal/resource_flow.py`
- Test: `tests/test_resource_flow.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_resource_flow.py
"""Tests for resource cost/production tracking in loops."""
import pytest
from mtg_synergy.causal.resource_flow import compute_ability_resources, compute_cycle_delta
from mtg_synergy.causal.types import ResourceDelta
from mtg_synergy.parse.ast_types import (
    Ability, Effect, Amount, TokenDef, Cost, ObjectFilter, ManaAmount
)


def test_krenko_resources():
    """Krenko: costs tap, produces X creatures."""
    ability = Ability(
        kind="activated", cost=Cost(tap=True),
        effects=[Effect(verb="create", amount=Amount(value="X"),
                        token=TokenDef("creature", "Goblin", 1, 1, [], "red"))],
    )
    cost, prod = compute_ability_resources(ability)
    assert cost.tap is True
    assert cost.mana == 0
    assert prod.creatures == "X"


def test_altar_resources():
    """Phyrexian Altar: costs 1 creature, produces 1 mana."""
    ability = Ability(
        kind="activated",
        cost=Cost(sacrifice=ObjectFilter(card_type="creature")),
        effects=[Effect(verb="add_mana", amount=Amount(value=1))],
    )
    cost, prod = compute_ability_resources(ability)
    assert cost.creatures == 1
    assert prod.mana == 1


def test_cycle_delta_positive():
    """Krenko + Altar cycle: net positive creatures and mana."""
    krenko = Ability(
        kind="activated", cost=Cost(tap=True),
        effects=[Effect(verb="create", amount=Amount(value=3),
                        token=TokenDef("creature", "Goblin", 1, 1, [], "red"))],
    )
    altar = Ability(
        kind="activated",
        cost=Cost(sacrifice=ObjectFilter(card_type="creature")),
        effects=[Effect(verb="add_mana", amount=Amount(value=1))],
    )
    delta = compute_cycle_delta([krenko, altar])
    assert delta.creatures > 0  # 3 produced - 1 sacrificed = +2
    assert delta.mana > 0       # 1 produced - 0 cost = +1


def test_cycle_delta_negative():
    """Cycle that costs more than it produces."""
    expensive = Ability(
        kind="activated",
        cost=Cost(mana=ManaAmount(total=5, colors={"generic": 5})),
        effects=[Effect(verb="create", amount=Amount(value=1),
                        token=TokenDef("creature", "Goblin", 1, 1, [], "red"))],
    )
    altar = Ability(
        kind="activated",
        cost=Cost(sacrifice=ObjectFilter(card_type="creature")),
        effects=[Effect(verb="add_mana", amount=Amount(value=1))],
    )
    delta = compute_cycle_delta([expensive, altar])
    assert delta.mana < 0  # 1 produced - 5 cost = -4


def test_draw_production():
    ability = Ability(
        kind="triggered",
        effects=[Effect(verb="draw", amount=Amount(value=2))],
    )
    _, prod = compute_ability_resources(ability)
    assert prod.cards == 2


def test_life_cost():
    ability = Ability(
        kind="activated",
        cost=Cost(pay_life=3),
        effects=[Effect(verb="draw", amount=Amount(value=1))],
    )
    cost, prod = compute_ability_resources(ability)
    assert cost.life == 3
    assert prod.cards == 1
```

- [ ] **Step 2: Run tests, verify fail**
- [ ] **Step 3: Implement resource flow**

Create `mtg_synergy/causal/resource_flow.py`:

- `compute_ability_resources(ability: Ability) -> tuple[ResourceCost, ResourceProduction]` — extract what an ability costs and produces
- `compute_cycle_delta(abilities: list[Ability]) -> ResourceDelta` — sum production - cost around a cycle
- `ResourceCostSimple` and `ResourceProductionSimple` — simplified dataclasses for tracking (mana, creatures, cards, life, tap as integers)

- [ ] **Step 4: Run tests, verify pass**
- [ ] **Step 5: Commit**

```bash
git add mtg_synergy/causal/resource_flow.py tests/test_resource_flow.py
git commit -m "feat(causal): add resource flow tracking for loop validation"
```

---

### Task 5: Chain Finder (Linear + Loop Detection)

**Files:**
- Create: `mtg_synergy/causal/chain_finder.py`
- Test: `tests/test_chain_finder.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_chain_finder.py
"""Tests for N-card chain discovery and loop detection."""
import pytest
from mtg_synergy.causal.chain_finder import find_chains, find_loops
from mtg_synergy.causal.types import Edge, EdgeDetail, Chain
from mtg_synergy.parse.ast_types import (
    Ability, Trigger, Effect, Amount, ObjectFilter, TokenDef, Cost, Restrictions
)


def _build_edge(src, tgt, event="enters_the_battlefield", strength=1.0,
                edge_type="triggers", precision="exact"):
    return Edge(src, tgt, edge_type, 0, 0, strength,
                EdgeDetail(event=event, filter_precision=precision))


def test_linear_chain_2_cards():
    """Commander → card A (simple 2-card chain)."""
    edges = [_build_edge("commander", "card_a", "enters_the_battlefield")]
    chains = find_chains("commander", edges, max_depth=3)
    assert len(chains) >= 1
    assert chains[0].cards == ["commander", "card_a"]
    assert chains[0].chain_type == "linear"


def test_linear_chain_3_cards():
    """Commander → A → B (3-card chain)."""
    edges = [
        _build_edge("commander", "card_a", "enters_the_battlefield"),
        _build_edge("card_a", "card_b", "dies"),
    ]
    chains = find_chains("commander", edges, max_depth=3)
    chain_3 = [c for c in chains if len(c.cards) == 3]
    assert len(chain_3) >= 1
    assert chain_3[0].cards == ["commander", "card_a", "card_b"]


def test_depth_limit():
    """Chains beyond max_depth are not found."""
    edges = [
        _build_edge("c", "a"),
        _build_edge("a", "b"),
        _build_edge("b", "d"),
        _build_edge("d", "e"),
    ]
    chains = find_chains("c", edges, max_depth=3)
    assert all(len(c.cards) <= 3 for c in chains)


def test_no_duplicate_cards_in_chain():
    """Linear chains don't revisit cards."""
    edges = [
        _build_edge("c", "a"),
        _build_edge("a", "c"),  # back to commander
    ]
    chains = find_chains("c", edges, max_depth=5)
    linear = [c for c in chains if c.chain_type == "linear"]
    for chain in linear:
        assert len(chain.cards) == len(set(chain.cards))


def test_loop_detection():
    """A → B → C → A is a loop."""
    edges = [
        _build_edge("a", "b"),
        _build_edge("b", "c"),
        _build_edge("c", "a"),
    ]
    abilities = {
        "a": [Ability(kind="activated", cost=Cost(tap=True),
                       effects=[Effect(verb="create", amount=Amount(value=3),
                                       token=TokenDef("creature", "Goblin", 1, 1, [], "red"))])],
        "b": [Ability(kind="activated",
                       cost=Cost(sacrifice=ObjectFilter(card_type="creature")),
                       effects=[Effect(verb="add_mana", amount=Amount(value=1))])],
        "c": [Ability(kind="triggered",
                       effects=[Effect(verb="untap", amount=Amount(value=1),
                                       target=ObjectFilter(card_type="creature"))])],
    }
    loops = find_loops(edges, abilities, max_loop_size=4)
    assert len(loops) >= 1
    assert loops[0].chain_type == "loop"


def test_once_per_turn_blocks_loop():
    """Abilities with once_per_turn cannot form infinite loops."""
    edges = [
        _build_edge("a", "b"),
        _build_edge("b", "a"),
    ]
    abilities = {
        "a": [Ability(kind="activated", cost=Cost(tap=True),
                       effects=[Effect(verb="create", amount=Amount(value=1),
                                       token=TokenDef("creature", "Goblin", 1, 1, [], "red"))],
                       restrictions=Restrictions(once_per_turn=True))],
        "b": [Ability(kind="triggered",
                       effects=[Effect(verb="untap", amount=Amount(value=1),
                                       target=ObjectFilter(card_type="creature"))])],
    }
    loops = find_loops(edges, abilities, max_loop_size=4)
    confirmed = [l for l in loops if l.loop_analysis and l.loop_analysis.is_infinite == "confirmed"]
    assert len(confirmed) == 0


def test_chain_ranking():
    """Shorter chains score higher than longer ones."""
    edges = [
        _build_edge("c", "a", strength=1.0),
        _build_edge("c", "b", strength=1.0),
        _build_edge("a", "d", strength=1.0),
    ]
    chains = find_chains("c", edges, max_depth=5)
    # 2-card chains should score higher than 3-card chains
    short = [c for c in chains if len(c.cards) == 2]
    long = [c for c in chains if len(c.cards) == 3]
    if short and long:
        assert max(c.score for c in short) >= max(c.score for c in long)
```

- [ ] **Step 2: Run tests, verify fail**
- [ ] **Step 3: Implement chain finder**

Create `mtg_synergy/causal/chain_finder.py`:

- `find_chains(commander_id, edges, max_depth=5) -> list[Chain]` — DFS from commander through edges, returns linear chains
- `find_loops(edges, abilities, max_loop_size=5) -> list[Chain]` — DFS with back-edge detection, resource flow validation
- `_rank_chains(chains) -> list[Chain]` — score chains by card count, edge strength, output power
- Uses `compute_cycle_delta()` from resource_flow for loop validation
- Checks `restrictions.once_per_turn` to disqualify infinite loops

Build an adjacency list from edges for efficient DFS. For loops, start DFS from each card in the neighborhood. When a back-edge is found (current card already in path), extract the cycle, compute resource delta, and classify as "confirmed" / "conditional" / "potential".

- [ ] **Step 4: Run tests, verify pass**
- [ ] **Step 5: Commit**

```bash
git add mtg_synergy/causal/chain_finder.py tests/test_chain_finder.py
git commit -m "feat(causal): add chain finder with linear + loop detection"
```

---

### Task 6: DB Storage + CLI for Graph and Chains

**Files:**
- Modify: `mtg_synergy/causal/__init__.py`
- Create: `build_graph.py` (root-level CLI)
- Create: `chain_finder_cli.py` (root-level CLI)
- Test: `tests/test_causal_integration.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_causal_integration.py
"""Integration tests for causal graph: build, store, query."""
import sqlite3
import json
import pytest
from mtg_synergy.causal import build_and_store_graph, load_edges, causal_score
from mtg_synergy.parse import parse_card, save_parsed, ensure_parse_schema
from mtg_synergy.parse.ast_types import Ability


def _setup_cards(conn):
    """Parse and store a few test cards."""
    ensure_parse_schema(conn)
    from mtg_synergy.causal import ensure_causal_schema
    ensure_causal_schema(conn)

    test_cards = [
        ("krenko", "Krenko, Mob Boss",
         "{T}: Create X 1/1 red Goblin creature tokens, where X is the number of Goblins you control.",
         "Legendary Creature — Goblin Warrior"),
        ("purphoros", "Purphoros, God of the Forge",
         "Whenever a creature enters the battlefield under your control, Purphoros deals 2 damage to each opponent.",
         "Legendary Enchantment Creature — God"),
        ("altar", "Phyrexian Altar",
         "Sacrifice a creature: Add one mana of any color.",
         "Artifact"),
    ]
    for oid, name, oracle, type_line in test_cards:
        abilities = parse_card(oracle, type_line)
        save_parsed(conn, oid, abilities)
    conn.commit()
    return {oid: parse_card(oracle, type_line) for oid, _, oracle, type_line in test_cards}


def test_build_and_store(tmp_db):
    conn = sqlite3.connect(tmp_db)
    cards = _setup_cards(conn)
    build_and_store_graph(conn, cards)
    edges = load_edges(conn)
    assert len(edges) > 0
    # Krenko → Purphoros trigger edge should exist
    kr_pu = [e for e in edges if e.source == "krenko" and e.target == "purphoros"]
    assert len(kr_pu) >= 1
    conn.close()


def test_causal_score_direct_edge(tmp_db):
    conn = sqlite3.connect(tmp_db)
    cards = _setup_cards(conn)
    build_and_store_graph(conn, cards)
    score = causal_score("purphoros", "krenko", set(), conn)
    assert score > 0  # direct trigger edge
    conn.close()


def test_causal_score_no_edge(tmp_db):
    conn = sqlite3.connect(tmp_db)
    cards = _setup_cards(conn)
    build_and_store_graph(conn, cards)
    # A card with no edges to krenko should score 0
    score = causal_score("nonexistent", "krenko", set(), conn)
    assert score == 0
    conn.close()
```

- [ ] **Step 2: Run tests, verify fail**
- [ ] **Step 3: Implement causal package API**

Update `mtg_synergy/causal/__init__.py`:

```python
"""Causal interaction graph — deterministic synergy analysis."""
import json
import sqlite3
from collections import defaultdict
from mtg_synergy.causal.types import Edge, EdgeDetail
from mtg_synergy.causal.graph_builder import build_causal_edges
from mtg_synergy.parse.ast_types import Ability


def ensure_causal_schema(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS interaction_edges (
            source_id TEXT NOT NULL,
            target_id TEXT NOT NULL,
            edge_type TEXT NOT NULL,
            ability_a INTEGER NOT NULL,
            ability_b INTEGER NOT NULL,
            strength  REAL NOT NULL,
            detail    TEXT NOT NULL,
            PRIMARY KEY (source_id, target_id, edge_type, ability_a, ability_b)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_edges_source ON interaction_edges(source_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_edges_target ON interaction_edges(target_id)")
    conn.commit()


def build_and_store_graph(conn, cards: dict[str, list[Ability]]):
    edges = build_causal_edges(cards)
    conn.execute("DELETE FROM interaction_edges")
    for e in edges:
        conn.execute(
            "INSERT OR REPLACE INTO interaction_edges VALUES (?,?,?,?,?,?,?)",
            (e.source, e.target, e.edge_type, e.ability_a, e.ability_b,
             e.strength, json.dumps(e.detail.to_dict()))
        )
    conn.commit()
    return len(edges)


def load_edges(conn, source_id=None, target_id=None) -> list[Edge]:
    query = "SELECT source_id, target_id, edge_type, ability_a, ability_b, strength, detail FROM interaction_edges"
    params = []
    conditions = []
    if source_id:
        conditions.append("source_id = ?")
        params.append(source_id)
    if target_id:
        conditions.append("target_id = ?")
        params.append(target_id)
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    rows = conn.execute(query, params).fetchall()
    return [Edge(r[0], r[1], r[2], r[3], r[4], r[5],
                 EdgeDetail(**json.loads(r[6]))) for r in rows]


EDGE_WEIGHTS = {"triggers": 2.0, "feeds": 1.5, "amplifies": 1.8, "enables": 1.0}


class CausalContext:
    """Pre-loaded edge data for fast per-candidate scoring.

    Load once per recommendation run (like DeckContext), then score
    each candidate with in-memory dict lookups — no per-candidate DB queries.
    """
    def __init__(self, conn, commander_id: str, deck_oids: set[str]):
        self.commander_id = commander_id
        self.deck_oids = deck_oids
        # Load ALL edges in one query, build adjacency dicts
        all_edges = load_edges(conn)
        # {source_id: [Edge, ...]}
        self._outgoing = defaultdict(list)
        # {target_id: [Edge, ...]}
        self._incoming = defaultdict(list)
        for e in all_edges:
            self._outgoing[e.source].append(e)
            self._incoming[e.target].append(e)

    def causal_score(self, candidate_id: str) -> float:
        """Score a candidate using pre-loaded edges. O(1) dict lookups."""
        score = 0.0
        # Direct edges: candidate → commander and commander → candidate
        for edge in self._outgoing.get(candidate_id, []):
            if edge.target == self.commander_id:
                score += edge.strength * EDGE_WEIGHTS.get(edge.edge_type, 1.0)
        for edge in self._incoming.get(candidate_id, []):
            if edge.source == self.commander_id:
                score += edge.strength * EDGE_WEIGHTS.get(edge.edge_type, 1.0)
        # Deck interaction count (in-memory)
        deck_interactions = 0
        for edge in self._outgoing.get(candidate_id, []):
            if edge.target in self.deck_oids:
                deck_interactions += 1
        for edge in self._incoming.get(candidate_id, []):
            if edge.source in self.deck_oids:
                deck_interactions += 1
        score += deck_interactions * 0.3
        return min(score, 10.0)


# Convenience function for simple usage (wraps CausalContext)
def causal_score(candidate_id: str, commander_id: str,
                 deck_cards: set[str], conn) -> float:
    """Single-candidate scoring. For batch use, create CausalContext instead."""
    ctx = CausalContext(conn, commander_id, deck_cards)
    return ctx.causal_score(candidate_id)
```

Create `build_graph.py` (root CLI):

```python
#!/usr/bin/env python3
"""Build the causal interaction graph from parsed abilities."""
import argparse
from mtg_synergy.db import get_connection
from mtg_synergy.parse import load_parsed, ensure_parse_schema
from mtg_synergy.causal import build_and_store_graph, ensure_causal_schema


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rebuild", action="store_true")
    parser.add_argument("--stats", action="store_true")
    args = parser.parse_args()

    conn = get_connection()
    ensure_parse_schema(conn)
    ensure_causal_schema(conn)

    if args.rebuild:
        # Load all parsed abilities
        rows = conn.execute("SELECT DISTINCT oracle_id FROM parsed_abilities").fetchall()
        cards = {}
        for (oid,) in rows:
            cards[oid] = load_parsed(conn, oid)
        print(f"Building graph from {len(cards)} cards...")
        n_edges = build_and_store_graph(conn, cards)
        print(f"Done: {n_edges} edges built")
    elif args.stats:
        n_edges = conn.execute("SELECT COUNT(*) FROM interaction_edges").fetchone()[0]
        n_cards = conn.execute("SELECT COUNT(DISTINCT source_id) FROM interaction_edges").fetchone()[0]
        print(f"Edges: {n_edges}")
        print(f"Cards with edges: {n_cards}")

    conn.close()


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests, verify pass**
- [ ] **Step 5: Run on real data**

```bash
python3 build_graph.py --rebuild
python3 build_graph.py --stats
```

- [ ] **Step 6: Commit**

```bash
git add mtg_synergy/causal/__init__.py build_graph.py tests/test_causal_integration.py
git commit -m "feat(causal): add graph DB storage, CLI, and causal_score()"
```

---

### Task 7: Integrate causal_score into Recommendation Pipeline

**Files:**
- Modify: `mtg_synergy/config.py` — add CAUSAL weight
- Modify: `mtg_synergy/recommend/scoring.py` — add causal feature
- Test: manual — run `compare_edhrec.py` before/after

- [ ] **Step 1: Add CAUSAL weight to config**

In `mtg_synergy/config.py`, add to `SCORING_WEIGHTS`:

```python
"CAUSAL": 2.0,  # Start low, tune upward after validation
```

- [ ] **Step 2: Add causal feature to scoring.py**

In `mtg_synergy/recommend/scoring.py`, in `compute_dynamic_score()`:

In `DeckContext.__init__()` (~line 55-151), add pre-loading of causal context:

```python
# Pre-load causal graph edges (one query, used for all candidates)
self.causal_ctx = None
try:
    from mtg_synergy.causal import CausalContext
    deck_oids = {self.card_oid.get(c) for c in self.deck_cards if c in self.card_oid}
    self.causal_ctx = CausalContext(conn, self.cmdr_oid, deck_oids)
except Exception:
    pass  # causal graph not built yet — graceful degradation
```

In `compute_dynamic_score()` (~line 371), add after the mechanics block:

```python
# Causal graph score (pre-loaded, O(1) per candidate)
causal = 0.0
if ctx.causal_ctx:
    causal = ctx.causal_ctx.causal_score(card_oid)
```

Add to the total formula:

```python
+ causal * w.get("CAUSAL", 0)
```

Add `causal` to the returned feature dict. This ensures zero DB queries per candidate — all edge data is pre-loaded once in `DeckContext`.

- [ ] **Step 3: Run EDHREC baseline**

```bash
python3 compare_edhrec.py --fast --quiet
```

Record current score (should be 14.9/30).

- [ ] **Step 4: Build graph and run EDHREC comparison**

```bash
python3 build_graph.py --rebuild
python3 compare_edhrec.py --fast --quiet
```

Compare new score vs baseline. The causal signal should not regress the score. If it does, reduce the CAUSAL weight.

- [ ] **Step 5: Commit**

```bash
git add mtg_synergy/config.py mtg_synergy/recommend/scoring.py
git commit -m "feat: integrate causal_score into recommendation pipeline"
```

---

### Task 8: Spellbook Validation + Final Stats

**Files:**
- No new code files — validation only

- [ ] **Step 1: Run chain finder on known combos**

Test if the engine rediscovers known Commander Spellbook combos for Krenko:

```bash
python3 -c "
from mtg_synergy.db import get_connection
from mtg_synergy.causal import load_edges
conn = get_connection()
edges = load_edges(conn, source_id=None)
print(f'Total edges: {len(edges)}')
# Show edges involving Krenko
krenko_oid = conn.execute(\"SELECT oracle_id FROM cards WHERE name = 'Krenko, Mob Boss'\").fetchone()
if krenko_oid:
    kr_edges = [e for e in edges if e.source == krenko_oid[0] or e.target == krenko_oid[0]]
    print(f'Krenko edges: {len(kr_edges)}')
    for e in kr_edges[:10]:
        name = conn.execute('SELECT name FROM cards WHERE oracle_id = ?',
            (e.target if e.source == krenko_oid[0] else e.source,)).fetchone()
        print(f'  {e.edge_type}: {name[0] if name else \"?\"} (strength={e.strength:.1f})')
conn.close()
"
```

- [ ] **Step 2: Print final stats**

```bash
python3 oracle_parser.py --stats
python3 build_graph.py --stats
python3 compare_edhrec.py --fast --quiet
```

Document: parse coverage, edge count, EDHREC score change.

- [ ] **Step 3: Commit any fixes**

```bash
git add mtg_synergy/
git commit -m "feat: complete causal graph + chain discovery + pipeline integration"
```

---

## What Comes Next (Part 3 — future)

- Add `amplifies` edges for replacement effects (Hardened Scales, Doubling Season)
- Add `enables` edges for static abilities (Thousand-Year Elixir, haste granters)
- Expand chain finder with Spellbook cross-validation
- Tune CAUSAL weight based on EDHREC results
- Parse all 33k cards (not just top 5000)
