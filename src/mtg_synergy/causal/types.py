"""Causal graph dataclasses for the interaction graph."""
from dataclasses import dataclass


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
