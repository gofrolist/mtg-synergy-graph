"""AST dataclass definitions for structured oracle text representation."""
from __future__ import annotations

from dataclasses import dataclass, field, fields, asdict
from typing import Any, Optional, Union


@dataclass
class ObjectFilter:
    card_type: Optional[str] = None
    subtype: Optional[str] = None
    controller: Optional[str] = None
    is_token: Optional[bool] = None
    is_another: Optional[bool] = None
    color: Optional[str] = None
    power: Optional[str] = None
    toughness: Optional[str] = None
    name: Optional[str] = None
    zone: Optional[str] = None


@dataclass
class ScalesWith:
    what: str = ""
    how: str = "linear"


@dataclass
class Amount:
    value: Union[int, str] = 0
    scales_with: Optional[ScalesWith] = None


@dataclass
class ManaAmount:
    total: int = 0
    colors: dict = field(default_factory=dict)
    is_any_color: bool = False


@dataclass
class Condition:
    kind: str = ""
    what: Optional[str] = None
    comparator: Optional[str] = None
    value: Optional[int] = None
    raw: Optional[str] = None
    restrictiveness: Optional[str] = None


@dataclass
class TokenDef:
    card_type: Optional[str] = None
    subtype: Optional[str] = None
    power: Optional[int] = None
    toughness: Optional[int] = None
    keywords: list = field(default_factory=list)
    color: Optional[str] = None


@dataclass
class Cost:
    mana: Optional[ManaAmount] = None
    tap: bool = False
    sacrifice: Optional[ObjectFilter] = None
    pay_life: Optional[int] = None
    discard: Optional[ObjectFilter] = None
    exile: Optional[ObjectFilter] = None
    loyalty: Optional[int] = None
    other: Optional[str] = None


@dataclass
class Trigger:
    event: str = ""
    subject: Optional[ObjectFilter] = None
    condition: Optional[Condition] = None


@dataclass
class Effect:
    verb: str = ""
    target: Optional[ObjectFilter] = None
    amount: Optional[Amount] = None
    token: Optional[TokenDef] = None
    keyword: Optional[str] = None
    destination: Optional[str] = None
    condition: Optional[Condition] = None
    unresolved_ref: Optional[str] = None


@dataclass
class Restrictions:
    once_per_turn: bool = False
    sorcery_speed: bool = False
    once_per_game: bool = False
    your_turn_only: bool = False


@dataclass
class Ability:
    kind: str = ""
    trigger: Optional[Trigger] = None
    cost: Optional[Cost] = None
    effects: list = field(default_factory=list)
    replacement_of: Optional[str] = None
    scope: Optional[ObjectFilter] = None
    scaling: Optional[str] = None
    restrictions: Optional[Restrictions] = None

    def to_dict(self) -> dict:
        """Recursively convert to a JSON-serializable dict."""
        return _to_dict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Ability":
        """Reconstruct an Ability tree from a dict."""
        return _from_dict(cls, d)


# ---- recursive serialization helpers ----

_DATACLASS_TYPES = {
    "ObjectFilter": ObjectFilter,
    "ScalesWith": ScalesWith,
    "Amount": Amount,
    "ManaAmount": ManaAmount,
    "Condition": Condition,
    "TokenDef": TokenDef,
    "Cost": Cost,
    "Trigger": Trigger,
    "Effect": Effect,
    "Restrictions": Restrictions,
    "Ability": Ability,
}

# Map (parent_class, field_name) -> target dataclass for reconstruction
_FIELD_TYPE_MAP: dict[tuple[type, str], type] = {}


def _init_field_type_map():
    """Build a mapping from (dataclass, field_name) to the nested dataclass type."""
    import typing
    for dc in _DATACLASS_TYPES.values():
        for f in fields(dc):
            hint = f.type
            # Resolve string annotations
            if isinstance(hint, str):
                # Strip Optional wrapper — match longest name first
                # to avoid "Amount" matching before "ManaAmount"
                sorted_types = sorted(_DATACLASS_TYPES.items(),
                                       key=lambda x: len(x[0]), reverse=True)
                for name, t in sorted_types:
                    if name in hint:
                        _FIELD_TYPE_MAP[(dc, f.name)] = t
                        break
                # Check for list of dataclasses (effects: list)
            else:
                origin = getattr(hint, "__origin__", None)
                if origin is Union or (hasattr(origin, "__name__") and origin.__name__ == "Union"):
                    for arg in hint.__args__:
                        if arg in _DATACLASS_TYPES.values():
                            _FIELD_TYPE_MAP[(dc, f.name)] = arg
                            break
                elif hint in _DATACLASS_TYPES.values():
                    _FIELD_TYPE_MAP[(dc, f.name)] = hint


_init_field_type_map()

# Hard-code the list element types
_LIST_ELEMENT_TYPE: dict[tuple[type, str], type] = {
    (Ability, "effects"): Effect,
}


def _to_dict(obj: Any) -> Any:
    if obj is None:
        return None
    if isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, list):
        return [_to_dict(item) for item in obj]
    if isinstance(obj, dict):
        return {k: _to_dict(v) for k, v in obj.items()}
    # Dataclass
    if hasattr(obj, "__dataclass_fields__"):
        result = {}
        for f in fields(obj):
            val = getattr(obj, f.name)
            if val is None:
                continue
            # Skip default-valued booleans and empty collections for cleaner output
            result[f.name] = _to_dict(val)
        return result
    return obj


def _from_dict(dc_type: type, d: Any) -> Any:
    if d is None:
        return None
    if not isinstance(d, dict):
        return d
    kwargs = {}
    for f in fields(dc_type):
        if f.name not in d:
            continue
        val = d[f.name]
        # Check if this field maps to a nested dataclass
        nested_type = _FIELD_TYPE_MAP.get((dc_type, f.name))
        list_elem = _LIST_ELEMENT_TYPE.get((dc_type, f.name))
        if nested_type and isinstance(val, dict):
            kwargs[f.name] = _from_dict(nested_type, val)
        elif list_elem and isinstance(val, list):
            kwargs[f.name] = [_from_dict(list_elem, item) if isinstance(item, dict) else item for item in val]
        else:
            kwargs[f.name] = val
    return dc_type(**kwargs)
