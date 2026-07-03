"""Tests for the provisional resource-flows seed (plan 2026-07-02-005, Unit 1).

Covers the strict loader
(``port_graph.resource_flows.load_resource_flows``), its validation
error paths (dotted-path pointers, mirroring
``tests/test_family_map.py``), and the committed artifact's pinned
shape: exactly the five evident flows, every declared shape referencing
a valid ``card_ports.port_type``, and the prose fields (``_readme``,
per-flow ``comment``) excluded from the functional view.

The seed is a Stage 0 MEASUREMENT input, not scoring config — nothing
here touches ``compute_config_hash`` or any scoring path.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from mtg_synergy_graph.port_graph.resource_flows import (
    _VALID_PORT_TYPES,
    PortShape,
    ResourceFlow,
    load_resource_flows,
)

_EXPECTED_FLOWS = frozenset(
    {
        "sacrifice_fodder",
        "discard_fuel",
        "untap_capacity",
        "life",
        "graveyard_bodies",
    }
)


# ---------------------------------------------------------------------------
# Happy path: the committed artifact
# ---------------------------------------------------------------------------


def test_loader_returns_five_flows() -> None:
    flows = load_resource_flows()
    assert set(flows) == _EXPECTED_FLOWS
    for name, flow in flows.items():
        assert isinstance(flow, ResourceFlow)
        assert flow.name == name
        assert flow.consumers, f"{name}: consumers must be non-empty"
        assert flow.suppliers, f"{name}: suppliers must be non-empty"
        assert all(isinstance(s, PortShape) for s in flow.consumers)
        assert all(isinstance(s, PortShape) for s in flow.suppliers)


def test_every_declared_shape_has_valid_port_type() -> None:
    flows = load_resource_flows()
    for flow in flows.values():
        for shape in (*flow.consumers, *flow.suppliers):
            assert shape.port_type in _VALID_PORT_TYPES, (
                f"{flow.name}: shape declares unknown port_type {shape.port_type!r}"
            )


def test_every_declared_shape_pins_event_class_or_prefix() -> None:
    flows = load_resource_flows()
    for flow in flows.values():
        for shape in (*flow.consumers, *flow.suppliers):
            has_ec = shape.event_class is not None
            has_prefix = shape.event_class_prefix is not None
            assert has_ec != has_prefix, (
                f"{flow.name}: shape must set exactly one of event_class / event_class_prefix: {shape}"
            )


def test_consumer_shapes_are_commander_demand_shapes() -> None:
    """Every consumer in the provisional draft is a cost-side demand
    port — the five flows' demand definitions are all cost shapes
    (registry gate predicates read cost.* columns)."""
    flows = load_resource_flows()
    for flow in flows.values():
        for shape in flow.consumers:
            assert shape.port_type == "cost", f"{flow.name}: consumer shape is not cost-side: {shape}"


# ---------------------------------------------------------------------------
# Loader error paths (explicit tmp_path artifacts; never the packaged seed)
# ---------------------------------------------------------------------------

_MINIMAL_SHAPE: dict[str, Any] = {"port_type": "cost", "event_class": "sacrifice"}


def _minimal_flow() -> dict[str, Any]:
    return {
        "consumers": [dict(_MINIMAL_SHAPE)],
        "suppliers": [{"port_type": "effect", "event_class": "Token"}],
    }


def _minimal_doc() -> dict[str, Any]:
    return {"_readme": ["x"], "flows": {"sacrifice_fodder": _minimal_flow()}}


def _write(path: Path, payload: Any) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_loader_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=r"resource_flows_seed.json missing"):
        load_resource_flows(tmp_path / "does_not_exist.json")


def test_loader_missing_packaged_seed_raises_value_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A broken install (packaged seed absent) must surface as ValueError,
    not a raw FileNotFoundError — demand_coverage's exit-2 precondition
    handling keys off ValueError."""
    import mtg_synergy_graph.port_graph.resource_flows as rf

    def _raise(filename: str) -> Path:
        raise FileNotFoundError(f"packaged seed {filename} not found")

    monkeypatch.setattr(rf, "default_seed_path", _raise)
    with pytest.raises(ValueError, match=r"resource_flows_seed.json missing.*[Rr]estore") as excinfo:
        load_resource_flows()
    assert not isinstance(excinfo.value, FileNotFoundError)


def test_loader_malformed_json_raises(tmp_path: Path) -> None:
    p = tmp_path / "resource_flows_seed.json"
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="malformed JSON"):
        load_resource_flows(p)


def test_loader_non_object_top_level_raises(tmp_path: Path) -> None:
    p = _write(tmp_path / "seed.json", ["not", "a", "dict"])
    with pytest.raises(ValueError, match="top level must be a JSON object"):
        load_resource_flows(p)


def test_loader_unknown_top_level_key_raises(tmp_path: Path) -> None:
    doc = _minimal_doc()
    doc["weights"] = {}
    p = _write(tmp_path / "seed.json", doc)
    with pytest.raises(ValueError, match=r"unknown top-level keys.*weights"):
        load_resource_flows(p)


def test_loader_missing_flows_raises(tmp_path: Path) -> None:
    p = _write(tmp_path / "seed.json", {"_readme": ["x"]})
    with pytest.raises(ValueError, match=r"missing required top-level keys.*flows"):
        load_resource_flows(p)


def test_loader_missing_readme_raises(tmp_path: Path) -> None:
    p = _write(tmp_path / "seed.json", {"flows": {"f": _minimal_flow()}})
    with pytest.raises(ValueError, match=r"missing required top-level keys.*_readme"):
        load_resource_flows(p)


def test_loader_non_list_readme_raises(tmp_path: Path) -> None:
    doc = _minimal_doc()
    doc["_readme"] = "prose"
    p = _write(tmp_path / "seed.json", doc)
    with pytest.raises(ValueError, match="'_readme' must be a list of strings"):
        load_resource_flows(p)


def test_loader_empty_flows_raises(tmp_path: Path) -> None:
    p = _write(tmp_path / "seed.json", {"_readme": ["x"], "flows": {}})
    with pytest.raises(ValueError, match="'flows' must not be empty"):
        load_resource_flows(p)


def test_loader_missing_consumers_raises_with_dotted_path(tmp_path: Path) -> None:
    flow = _minimal_flow()
    del flow["consumers"]
    p = _write(tmp_path / "seed.json", {"_readme": ["x"], "flows": {"life": flow}})
    with pytest.raises(ValueError, match=r"flows\.life: missing required flow keys.*consumers"):
        load_resource_flows(p)


def test_loader_missing_suppliers_raises_with_dotted_path(tmp_path: Path) -> None:
    flow = _minimal_flow()
    del flow["suppliers"]
    p = _write(tmp_path / "seed.json", {"_readme": ["x"], "flows": {"life": flow}})
    with pytest.raises(ValueError, match=r"flows\.life: missing required flow keys.*suppliers"):
        load_resource_flows(p)


def test_loader_empty_suppliers_raises(tmp_path: Path) -> None:
    flow = _minimal_flow()
    flow["suppliers"] = []
    p = _write(tmp_path / "seed.json", {"_readme": ["x"], "flows": {"life": flow}})
    with pytest.raises(ValueError, match=r"flows\.life\.suppliers: must not be empty"):
        load_resource_flows(p)


def test_loader_empty_consumers_raises(tmp_path: Path) -> None:
    flow = _minimal_flow()
    flow["consumers"] = []
    p = _write(tmp_path / "seed.json", {"_readme": ["x"], "flows": {"life": flow}})
    with pytest.raises(ValueError, match=r"flows\.life\.consumers: must not be empty"):
        load_resource_flows(p)


def test_loader_unknown_flow_key_raises(tmp_path: Path) -> None:
    flow = _minimal_flow()
    flow["weight"] = 2.0
    p = _write(tmp_path / "seed.json", {"_readme": ["x"], "flows": {"life": flow}})
    with pytest.raises(ValueError, match=r"flows\.life: unknown flow keys.*weight"):
        load_resource_flows(p)


def test_loader_unknown_shape_key_raises(tmp_path: Path) -> None:
    flow = _minimal_flow()
    flow["consumers"][0]["oracle_text"] = "x"
    p = _write(tmp_path / "seed.json", {"_readme": ["x"], "flows": {"life": flow}})
    with pytest.raises(ValueError, match=r"flows\.life\.consumers\[0\]: unknown shape keys.*oracle_text"):
        load_resource_flows(p)


def test_loader_missing_port_type_raises(tmp_path: Path) -> None:
    flow = _minimal_flow()
    flow["suppliers"] = [{"event_class": "Token"}]
    p = _write(tmp_path / "seed.json", {"_readme": ["x"], "flows": {"life": flow}})
    with pytest.raises(ValueError, match=r"flows\.life\.suppliers\[0\]\.port_type: missing"):
        load_resource_flows(p)


def test_loader_invalid_port_type_raises(tmp_path: Path) -> None:
    flow = _minimal_flow()
    flow["consumers"][0]["port_type"] = "sorcery"
    p = _write(tmp_path / "seed.json", {"_readme": ["x"], "flows": {"life": flow}})
    with pytest.raises(ValueError, match=r"flows\.life\.consumers\[0\]\.port_type: 'sorcery' is not a valid"):
        load_resource_flows(p)


def test_loader_both_event_class_and_prefix_raises(tmp_path: Path) -> None:
    flow = _minimal_flow()
    flow["consumers"][0]["event_class_prefix"] = "sac"
    p = _write(tmp_path / "seed.json", {"_readme": ["x"], "flows": {"life": flow}})
    with pytest.raises(ValueError, match=r"flows\.life\.consumers\[0\]: exactly one of.*got both"):
        load_resource_flows(p)


def test_loader_neither_event_class_nor_prefix_raises(tmp_path: Path) -> None:
    flow = _minimal_flow()
    flow["consumers"] = [{"port_type": "cost"}]
    p = _write(tmp_path / "seed.json", {"_readme": ["x"], "flows": {"life": flow}})
    with pytest.raises(ValueError, match=r"flows\.life\.consumers\[0\]: exactly one of.*got neither"):
        load_resource_flows(p)


def test_loader_attr_kind_without_exclusion_list_raises(tmp_path: Path) -> None:
    flow = _minimal_flow()
    flow["suppliers"][0]["attr_kind"] = "token_subtype"
    p = _write(tmp_path / "seed.json", {"_readme": ["x"], "flows": {"life": flow}})
    with pytest.raises(
        ValueError,
        match=r"flows\.life\.suppliers\[0\]: 'attr_kind' and 'attr_value_not_in' must be declared together",
    ):
        load_resource_flows(p)


def test_loader_empty_string_shape_field_raises(tmp_path: Path) -> None:
    flow = _minimal_flow()
    flow["consumers"][0]["cost_target"] = ""
    p = _write(tmp_path / "seed.json", {"_readme": ["x"], "flows": {"life": flow}})
    with pytest.raises(ValueError, match=r"flows\.life\.consumers\[0\]\.cost_target: must be a non-empty string"):
        load_resource_flows(p)


def test_loader_empty_list_shape_field_raises(tmp_path: Path) -> None:
    flow = _minimal_flow()
    flow["suppliers"][0]["attr_kind"] = "token_subtype"
    flow["suppliers"][0]["attr_value_not_in"] = []
    p = _write(tmp_path / "seed.json", {"_readme": ["x"], "flows": {"life": flow}})
    with pytest.raises(ValueError, match=r"flows\.life\.suppliers\[0\]\.attr_value_not_in: must be a non-empty list"):
        load_resource_flows(p)


def test_loader_non_string_comment_raises(tmp_path: Path) -> None:
    flow = _minimal_flow()
    flow["comment"] = ["not", "a", "string"]
    p = _write(tmp_path / "seed.json", {"_readme": ["x"], "flows": {"life": flow}})
    with pytest.raises(ValueError, match=r"flows\.life\.comment: must be a string"):
        load_resource_flows(p)


# ---------------------------------------------------------------------------
# Edge: prose fields excluded from the functional view
# ---------------------------------------------------------------------------


def test_readme_and_comment_ignored_by_functional_view(tmp_path: Path) -> None:
    """Two seeds differing only in ``_readme`` / ``comment`` prose load
    to EQUAL structures — the prose can never leak into measurement."""
    doc_a = _minimal_doc()
    doc_b = _minimal_doc()
    doc_b["_readme"] = ["totally", "different", "prose"]
    doc_b["flows"]["sacrifice_fodder"]["comment"] = "measured pool: N cards"
    flows_a = load_resource_flows(_write(tmp_path / "a.json", doc_a))
    flows_b = load_resource_flows(_write(tmp_path / "b.json", doc_b))
    assert flows_a == flows_b
    assert not hasattr(flows_a["sacrifice_fodder"], "comment")


def test_returned_structure_is_immutable() -> None:
    flows = load_resource_flows()
    flow = flows["sacrifice_fodder"]
    with pytest.raises(AttributeError):
        flow.name = "renamed"  # type: ignore[misc]
    with pytest.raises(AttributeError):
        flow.consumers[0].port_type = "effect"  # type: ignore[misc]
    assert isinstance(flow.consumers, tuple)
    assert isinstance(flow.suppliers, tuple)
