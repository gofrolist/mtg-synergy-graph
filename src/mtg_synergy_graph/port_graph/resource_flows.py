"""Strict loader for the provisional resource-flow pairing seed.

Loads ``src/mtg_synergy_graph/data/resource_flows_seed.json`` — the
pre-pinned five-flow demand→supply pairing table for the Stage 0
demand-coverage instrument (plan 2026-07-02-005, Unit 1).

STATUS: measurement input ONLY. This seed is deliberately NOT part of
``ScoringConfigInputs`` / ``compute_config_hash`` — editing it must
never flip the config hash. There is NO scoring wiring here: the only
consumer is the read-side Stage 0 instrument (plan Unit 2). Promotion
to a scoring seed (digest field + hash block + re-pin) happens only at
the Phase B flip commit, if the Stage 0 routing funds the mechanism.

Validation mirrors ``portfolio.load_family_map`` strictness:
frozenset-validated keys at every level, ``ValueError`` with a
dotted-path pointer on any unknown/missing/malformed entry (never
``assert`` — stripped by ``python -O``), and the prose fields
(``_readme``, per-flow ``comment``) excluded from the functional view.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ._paths import default_seed_path

_RESOURCE_FLOWS_FILENAME = "resource_flows_seed.json"

#: Legal top-level keys of the seed document.
_VALID_TOP_LEVEL_KEYS: frozenset[str] = frozenset({"_readme", "flows"})

#: Legal keys of a flow object; ``comment`` is prose (dropped from the
#: functional view), the other two are required.
_VALID_FLOW_KEYS: frozenset[str] = frozenset({"consumers", "suppliers", "comment"})
_REQUIRED_FLOW_KEYS: frozenset[str] = frozenset({"consumers", "suppliers"})

#: card_ports.port_type vocabulary (schema comment in the importer:
#: ``trigger|effect|static|replacement|cost|scales_with|keyword``).
_VALID_PORT_TYPES: frozenset[str] = frozenset(
    {
        "trigger",
        "effect",
        "static",
        "replacement",
        "cost",
        "scales_with",
        "keyword",
    }
)

#: Closed shape-grammar key set (documented in the seed's ``_readme``).
_VALID_SHAPE_KEYS: frozenset[str] = frozenset(
    {
        "port_type",
        "event_class",
        "event_class_prefix",
        "cost_target",
        "cost_target_not",
        "zone_origin",
        "zone_destination",
        "valid_filter",
        "attr_kind",
        "attr_value_not_in",
        "card_types_not_contains",
    }
)

#: Shape keys whose value must be a non-empty string.
_STRING_SHAPE_KEYS: frozenset[str] = frozenset(
    {
        "port_type",
        "event_class",
        "event_class_prefix",
        "cost_target",
        "cost_target_not",
        "zone_origin",
        "zone_destination",
        "valid_filter",
        "attr_kind",
    }
)

#: Shape keys whose value must be a non-empty list of non-empty strings.
_LIST_SHAPE_KEYS: frozenset[str] = frozenset({"attr_value_not_in", "card_types_not_contains"})


@dataclass(frozen=True)
class PortShape:
    """One demand- or supply-side port-shape predicate (immutable).

    ``None`` means "field not constrained". Exactly one of
    ``event_class`` / ``event_class_prefix`` is always set (validated
    at load). ``attr_kind`` / ``attr_value_not_in`` are always set
    together: the port must carry a ``port_attributes`` row of that
    kind whose value is NOT in the list.
    """

    port_type: str
    event_class: str | None = None
    event_class_prefix: str | None = None
    cost_target: str | None = None
    cost_target_not: str | None = None
    zone_origin: str | None = None
    zone_destination: str | None = None
    valid_filter: str | None = None
    attr_kind: str | None = None
    attr_value_not_in: tuple[str, ...] | None = None
    card_types_not_contains: tuple[str, ...] | None = None


@dataclass(frozen=True)
class ResourceFlow:
    """One resource flow: commander-side demand → candidate-side supply."""

    name: str
    consumers: tuple[PortShape, ...]
    suppliers: tuple[PortShape, ...]


def _parse_shape(raw: object, *, where: str, path: Path) -> PortShape:
    """Validate one shape object; ``where`` is the dotted-path pointer."""
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: {where}: shape must be a JSON object, got {type(raw).__name__}")

    extra = set(raw) - _VALID_SHAPE_KEYS
    if extra:
        raise ValueError(f"{path}: {where}: unknown shape keys {sorted(extra)} (valid: {sorted(_VALID_SHAPE_KEYS)})")

    if "port_type" not in raw:
        raise ValueError(f"{path}: {where}.port_type: missing (required)")

    fields: dict[str, object] = {}
    for key, value in raw.items():
        if key in _STRING_SHAPE_KEYS:
            if not isinstance(value, str) or not value:
                raise ValueError(
                    f"{path}: {where}.{key}: must be a non-empty string, got {value!r} ({type(value).__name__})"
                )
            fields[key] = value
        elif key in _LIST_SHAPE_KEYS:
            if not isinstance(value, list) or not value or not all(isinstance(v, str) and v for v in value):
                raise ValueError(f"{path}: {where}.{key}: must be a non-empty list of non-empty strings, got {value!r}")
            fields[key] = tuple(value)

    port_type = fields["port_type"]
    if port_type not in _VALID_PORT_TYPES:
        raise ValueError(
            f"{path}: {where}.port_type: {port_type!r} is not a valid card_ports "
            f"port_type (valid: {sorted(_VALID_PORT_TYPES)})"
        )

    has_ec = "event_class" in fields
    has_prefix = "event_class_prefix" in fields
    if has_ec == has_prefix:  # both or neither
        raise ValueError(
            f"{path}: {where}: exactly one of 'event_class' / 'event_class_prefix' "
            f"is required (got {'both' if has_ec else 'neither'})"
        )

    if ("attr_kind" in fields) != ("attr_value_not_in" in fields):
        raise ValueError(f"{path}: {where}: 'attr_kind' and 'attr_value_not_in' must be declared together")

    return PortShape(**fields)  # type: ignore[arg-type]


def _parse_flow(name: str, raw: object, *, path: Path) -> ResourceFlow:
    where = f"flows.{name}"
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: {where}: flow must be a JSON object, got {type(raw).__name__}")

    extra = set(raw) - _VALID_FLOW_KEYS
    if extra:
        raise ValueError(f"{path}: {where}: unknown flow keys {sorted(extra)} (valid: {sorted(_VALID_FLOW_KEYS)})")
    missing = _REQUIRED_FLOW_KEYS - set(raw)
    if missing:
        raise ValueError(f"{path}: {where}: missing required flow keys {sorted(missing)}")

    comment = raw.get("comment")
    if comment is not None and not isinstance(comment, str):
        raise ValueError(f"{path}: {where}.comment: must be a string when present, got {type(comment).__name__}")

    shapes: dict[str, tuple[PortShape, ...]] = {}
    for side in ("consumers", "suppliers"):
        entries = raw[side]
        if not isinstance(entries, list):
            raise ValueError(f"{path}: {where}.{side}: must be a list of shape objects, got {type(entries).__name__}")
        if not entries:
            raise ValueError(f"{path}: {where}.{side}: must not be empty (a flow with no {side} is meaningless)")
        shapes[side] = tuple(
            _parse_shape(entry, where=f"{where}.{side}[{i}]", path=path) for i, entry in enumerate(entries)
        )

    return ResourceFlow(name=name, consumers=shapes["consumers"], suppliers=shapes["suppliers"])


def load_resource_flows(path: Path | None = None) -> dict[str, ResourceFlow]:
    """Read the resource-flows seed into a ``flow_name -> ResourceFlow`` dict.

    ``path`` defaults to the packaged seed
    (``default_seed_path("resource_flows_seed.json")``); tests pass an
    explicit path to exercise the validation error paths against
    tmp_path artifacts.

    Strict shape validation — missing file, malformed JSON, non-object
    top level, unknown/missing keys at any level, empty
    ``consumers``/``suppliers``, unknown ``port_type`` values, or
    malformed shape fields all raise ``ValueError`` with a dotted-path
    pointer. The prose fields (``_readme``, per-flow ``comment``) are
    validated for type but excluded from the returned structure.
    """
    p = Path(path) if path is not None else default_seed_path(_RESOURCE_FLOWS_FILENAME)
    try:
        with p.open(encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError as exc:
        raise ValueError(
            f"{_RESOURCE_FLOWS_FILENAME} missing at {p}; this file is the pre-pinned "
            "provisional pairing table for the Stage 0 demand-coverage instrument "
            "and must exist. Restore it from git."
        ) from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"{p}: malformed JSON ({exc})") from exc

    if not isinstance(raw, dict):
        raise ValueError(f"{p}: top level must be a JSON object, got {type(raw).__name__}")

    extra_keys = set(raw) - _VALID_TOP_LEVEL_KEYS
    if extra_keys:
        raise ValueError(f"{p}: unknown top-level keys {sorted(extra_keys)} (valid: {sorted(_VALID_TOP_LEVEL_KEYS)})")
    missing_keys = _VALID_TOP_LEVEL_KEYS - set(raw)
    if missing_keys:
        raise ValueError(f"{p}: missing required top-level keys {sorted(missing_keys)}")

    readme = raw["_readme"]
    if not isinstance(readme, list) or not all(isinstance(line, str) for line in readme):
        raise ValueError(f"{p}: '_readme' must be a list of strings")

    flows_raw = raw["flows"]
    if not isinstance(flows_raw, dict):
        raise ValueError(f"{p}: 'flows' must be an object mapping flow name to flow, got {type(flows_raw).__name__}")
    if not flows_raw:
        raise ValueError(f"{p}: 'flows' must not be empty")

    flows: dict[str, ResourceFlow] = {}
    for name, flow_raw in flows_raw.items():
        if not isinstance(name, str) or not name:
            raise ValueError(f"{p}: flows: flow names must be non-empty strings, got {name!r}")
        flows[name] = _parse_flow(name, flow_raw, path=p)
    return flows
