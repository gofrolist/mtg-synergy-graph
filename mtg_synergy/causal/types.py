"""Causal graph dataclasses for the interaction graph."""
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
        resources = [self.mana, self.creatures, self.cards]
        return any(r > 0 for r in resources) and all(r >= 0 for r in resources)


@dataclass
class LoopAnalysis:
    is_infinite: str
    min_board_requirement: str | None = None
    resource_deltas: dict = field(default_factory=dict)
    growth_pattern: str = "fixed"


@dataclass
class Chain:
    cards: list[str]
    edges: list[Edge]
    chain_type: str
    output: str = ""
    resource_delta: ResourceDelta = field(default_factory=ResourceDelta)
    loop_analysis: LoopAnalysis | None = None
    bottleneck: str | None = None
    score: float = 0.0
