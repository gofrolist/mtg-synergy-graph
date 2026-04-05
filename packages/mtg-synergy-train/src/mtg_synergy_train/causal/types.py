"""Causal graph dataclasses for the interaction graph."""
from dataclasses import dataclass, fields


@dataclass
class EdgeDetail:
    event: str | None = None
    resource: str | None = None
    verb_modified: str | None = None
    scaling: str | None = None
    filter_precision: str = "broad"

    def to_dict(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)
                if getattr(self, f.name) is not None}


@dataclass
class Edge:
    source: str
    target: str
    edge_type: str
    ability_a: int
    ability_b: int
    strength: float
    detail: EdgeDetail

