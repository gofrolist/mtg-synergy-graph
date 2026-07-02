"""Tests for the data/scoring_weights.json sidecar and its loader.

Covers strict-shape validation at module import, dead-key detection
against the registered rule universe, and the compute_config_hash
invariants (value flips the hash; comment does not).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from mtg_synergy_graph import universal_scorer
from mtg_synergy_graph.bench.tensor import compute_config_hash
from mtg_synergy_graph.complement_rules import COMPLEMENT_RULES
from mtg_synergy_graph.complement_rules.registry import DECLARATIVE_RULE_IDS
from mtg_synergy_graph.universal_scorer import (
    _FLAT_COUNT_RULES,
    _FLAT_WEIGHT_OVERRIDES,
    _RULE_QUALITY_MULTIPLIER,
    _SCORING_WEIGHTS_PATH,
    _load_scoring_weights,
)

# Pin: hash of the production scoring config at the migration commit.
# Edit this only when a value in data/scoring_weights.json (or another
# input to compute_config_hash) legitimately changes — at which point
# bench.py audit --repin --yes is also required.
# 2026-06-09: hash input set extended to cover event_match_seed.json,
# rules_seed.json functional rows, and heuristics.STAPLES (audit
# finding: editing any of the three changed scores without flipping
# the hash). Scores verified bitwise-identical before/after the
# formula change via bench.py audit --expect-identity.
_PRODUCTION_HASH = "0cbdb9e217e7bf92e7100b36f8533fe4facc02ed89fdb8de3d786f9fb00ce0f3"


# ---------------------------------------------------------------------------
# Loader strict-shape validation (parametrized)
# ---------------------------------------------------------------------------


@pytest.fixture
def patched_loader(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point the loader at a tmp_path JSON; tests write the payload."""
    sidecar = tmp_path / "scoring_weights.json"
    monkeypatch.setattr(universal_scorer, "_SCORING_WEIGHTS_PATH", sidecar)
    yield sidecar


def _write_payload(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


_VALID_QUALITY_ENTRY = {"trigger_resonance": {"value": 0.7, "comment": ""}}
_VALID_FLAT_ENTRY = {"evasion": {"value": 0.10, "comment": ""}}


@pytest.mark.parametrize(
    ("label", "payload", "expected_match"),
    [
        (
            "unknown_top_level",
            {
                "rule_quality_multiplier": _VALID_QUALITY_ENTRY,
                "flat_weight_overrides": _VALID_FLAT_ENTRY,
                "rogue_section": {},
            },
            "unknown top-level sections",
        ),
        (
            "missing_top_level_section",
            {"rule_quality_multiplier": _VALID_QUALITY_ENTRY},
            "missing required top-level sections",
        ),
        (
            "unknown_entry_field",
            {
                "rule_quality_multiplier": {
                    "trigger_resonance": {
                        "value": 0.7,
                        "comment": "",
                        "valeu": 0.7,  # typo
                    }
                },
                "flat_weight_overrides": {},
            },
            "unknown fields",
        ),
        (
            "missing_value",
            {
                "rule_quality_multiplier": {"trigger_resonance": {"comment": ""}},
                "flat_weight_overrides": {},
            },
            "missing required 'value'",
        ),
        (
            "missing_comment",
            {
                "rule_quality_multiplier": {"trigger_resonance": {"value": 0.7}},
                "flat_weight_overrides": {},
            },
            "missing required 'comment'",
        ),
        (
            "value_is_string",
            {
                "rule_quality_multiplier": {"trigger_resonance": {"value": "0.7", "comment": ""}},
                "flat_weight_overrides": {},
            },
            "'value' must be a number",
        ),
        (
            "value_is_bool",
            {
                "rule_quality_multiplier": {"trigger_resonance": {"value": True, "comment": ""}},
                "flat_weight_overrides": {},
            },
            "'value' must be a number",
        ),
        (
            "comment_is_int",
            {
                "rule_quality_multiplier": {"trigger_resonance": {"value": 0.7, "comment": 42}},
                "flat_weight_overrides": {},
            },
            "'comment' must be a string",
        ),
        (
            "entry_is_not_object_number",
            {
                "rule_quality_multiplier": {"trigger_resonance": 0.7},
                "flat_weight_overrides": {},
            },
            "entry must be an object",
        ),
        (
            "entry_is_not_object_null",
            {
                "rule_quality_multiplier": {"trigger_resonance": None},
                "flat_weight_overrides": {},
            },
            "entry must be an object",
        ),
    ],
)
def test_loader_strict_shape_rejection(
    patched_loader: Path,
    label: str,
    payload: dict[str, Any],
    expected_match: str,
) -> None:
    _write_payload(patched_loader, payload)
    with pytest.raises(ValueError, match=expected_match):
        _load_scoring_weights()


def test_loader_empty_comment_allowed(patched_loader: Path) -> None:
    _write_payload(
        patched_loader,
        {
            "rule_quality_multiplier": {"trigger_resonance": {"value": 0.7, "comment": ""}},
            "flat_weight_overrides": {"evasion": {"value": 0.10, "comment": ""}},
        },
    )
    weights = _load_scoring_weights()
    assert weights.rule_quality_multiplier == {"trigger_resonance": 0.7}
    assert weights.flat_weight_overrides == {"evasion": 0.1}


def test_loader_int_value_coerces_to_float_with_stable_repr(
    patched_loader: Path,
) -> None:
    """A bare-int ``value`` (JSON ``1``) must produce the same Python float
    representation as an explicit float (JSON ``1.0``) — otherwise
    compute_config_hash would diverge across cosmetically equivalent
    JSON authoring choices.
    """
    _write_payload(
        patched_loader,
        {
            "rule_quality_multiplier": {
                "as_int": {"value": 1, "comment": ""},
                "as_float": {"value": 1.0, "comment": ""},
            },
            "flat_weight_overrides": {},
        },
    )
    weights = _load_scoring_weights()
    assert weights.rule_quality_multiplier["as_int"] == 1.0
    assert isinstance(weights.rule_quality_multiplier["as_int"], float)
    assert repr(weights.rule_quality_multiplier["as_int"]) == repr(weights.rule_quality_multiplier["as_float"])


# ---------------------------------------------------------------------------
# Loader I/O failure modes
# ---------------------------------------------------------------------------


def test_loader_missing_file_raises_actionable_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    nonexistent = tmp_path / "does_not_exist.json"
    monkeypatch.setattr(universal_scorer, "_SCORING_WEIGHTS_PATH", nonexistent)
    with pytest.raises(ValueError) as excinfo:
        _load_scoring_weights()
    msg = str(excinfo.value)
    assert str(nonexistent) in msg, "file path missing from error message"
    assert "missing" in msg or "must exist" in msg


def test_loader_malformed_json_raises_actionable_error(
    patched_loader: Path,
) -> None:
    patched_loader.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(ValueError) as excinfo:
        _load_scoring_weights()
    assert str(patched_loader) in str(excinfo.value)
    assert "malformed JSON" in str(excinfo.value)


def test_production_scoring_weights_loads_cleanly() -> None:
    """The committed data/scoring_weights.json must satisfy the strict-shape
    validator. Catches a malformed-commit before pytest's import machinery
    converts the failure into a confusing collection error.
    """
    weights = _load_scoring_weights()
    assert weights.rule_quality_multiplier, "production quality dict is empty"
    assert weights.flat_weight_overrides, "production flat dict is empty"
    # Sanity: confirm the loaded sections contain real registered rule_ids.
    assert "trigger_resonance" in weights.rule_quality_multiplier
    assert "evasion" in weights.flat_weight_overrides


def test_production_scoring_weights_path_resolves_to_repo() -> None:
    assert _SCORING_WEIGHTS_PATH.exists(), (
        f"{_SCORING_WEIGHTS_PATH} does not exist; loader path resolution is broken or the file was deleted"
    )


# ---------------------------------------------------------------------------
# Dead-key detection against the registered rule universe
# ---------------------------------------------------------------------------


def _emitted_rule_ids() -> set[str]:
    """Universe of rule_ids that any complement helper might emit.

    Combines (a) ``rule_id="..."`` and ``rule_id='...'`` string literals
    scraped from ``complement_rules/*.py`` source, (b) ``ComplementRule.rule_id``
    from the COMPLEMENT_RULES registration tuple, and (c) declarative
    rule ids from ``data/rules_seed.json`` (via DECLARATIVE_RULE_IDS).
    """
    src_dir = Path(universal_scorer.__file__).parent / "complement_rules"
    literals: set[str] = set()
    pattern = re.compile(r"""rule_id=['"]([a-z_0-9]+)['"]""")
    for path in src_dir.rglob("*.py"):
        for match in pattern.finditer(path.read_text(encoding="utf-8")):
            literals.add(match.group(1))
    # Sanity floor: a regex breakage that returns zero matches would
    # silently weaken the dead-key check. The current literal count is
    # ~70+; require at least 30 to fail loudly on accidental rot.
    assert len(literals) >= 30, (
        f"rule_id literal scrape returned only {len(literals)} matches — "
        "regex may be broken or complement_rules layout changed"
    )
    return literals | {rule.rule_id for rule in COMPLEMENT_RULES} | set(DECLARATIVE_RULE_IDS)


def test_no_dead_rule_ids_in_quality_multiplier() -> None:
    """Every key in _RULE_QUALITY_MULTIPLIER must be a rule_id that some
    complement helper actually emits. A dead key indicates either a typo
    or a rule that was renamed/deleted without updating the JSON.
    """
    universe = _emitted_rule_ids()
    dead = set(_RULE_QUALITY_MULTIPLIER) - universe
    assert not dead, (
        f"scoring_weights.json contains rule_quality_multiplier keys that no complement helper emits: {sorted(dead)}"
    )


def test_no_dead_keys_in_flat_weight_overrides() -> None:
    """Every key in _FLAT_WEIGHT_OVERRIDES must be in _FLAT_COUNT_RULES,
    which is the gate that decides whether a rule_id is treated as a
    flat-density bucket at scoring time.
    """
    dead = set(_FLAT_WEIGHT_OVERRIDES) - _FLAT_COUNT_RULES
    assert not dead, (
        f"scoring_weights.json contains flat_weight_overrides keys not in _FLAT_COUNT_RULES: {sorted(dead)}"
    )


# ---------------------------------------------------------------------------
# compute_config_hash invariants
# ---------------------------------------------------------------------------


@pytest.fixture
def reload_into_module(
    patched_loader: Path,
) -> Iterator[Any]:
    """Reload helper that restores the module-level dicts on teardown.

    Tests call ``reload(payload)`` to write JSON, run the loader, and
    swap the loaded values into ``universal_scorer._RULE_QUALITY_MULTIPLIER``
    / ``_FLAT_WEIGHT_OVERRIDES`` in place. The dicts' identity is
    preserved (via ``.clear()`` + ``.update()``) because
    ``get_scoring_config_inputs()`` returns live references and any
    rebind would orphan the consumer-visible dicts.
    """
    saved_quality = dict(universal_scorer._RULE_QUALITY_MULTIPLIER)
    saved_flat = dict(universal_scorer._FLAT_WEIGHT_OVERRIDES)

    def reload(payload: dict[str, Any]) -> None:
        _write_payload(patched_loader, payload)
        weights = _load_scoring_weights()
        universal_scorer._RULE_QUALITY_MULTIPLIER.clear()
        universal_scorer._RULE_QUALITY_MULTIPLIER.update(weights.rule_quality_multiplier)
        universal_scorer._FLAT_WEIGHT_OVERRIDES.clear()
        universal_scorer._FLAT_WEIGHT_OVERRIDES.update(weights.flat_weight_overrides)

    try:
        yield reload
    finally:
        universal_scorer._RULE_QUALITY_MULTIPLIER.clear()
        universal_scorer._RULE_QUALITY_MULTIPLIER.update(saved_quality)
        universal_scorer._FLAT_WEIGHT_OVERRIDES.clear()
        universal_scorer._FLAT_WEIGHT_OVERRIDES.update(saved_flat)


def test_comment_field_does_not_affect_compute_config_hash(
    reload_into_module: Any,
) -> None:
    base = {
        "rule_quality_multiplier": {"trigger_resonance": {"value": 0.7, "comment": "first note"}},
        "flat_weight_overrides": {"evasion": {"value": 0.10, "comment": ""}},
    }
    reload_into_module(base)
    h1 = compute_config_hash()

    edited = json.loads(json.dumps(base))
    edited["rule_quality_multiplier"]["trigger_resonance"]["comment"] = "rewritten note — totally different prose"
    reload_into_module(edited)
    h2 = compute_config_hash()

    assert h1 == h2, "editing only the comment field must not flip the hash"


def test_value_field_affects_compute_config_hash(
    reload_into_module: Any,
) -> None:
    base = {
        "rule_quality_multiplier": {"trigger_resonance": {"value": 0.7, "comment": ""}},
        "flat_weight_overrides": {"evasion": {"value": 0.10, "comment": ""}},
    }
    reload_into_module(base)
    h1 = compute_config_hash()

    edited = json.loads(json.dumps(base))
    edited["rule_quality_multiplier"]["trigger_resonance"]["value"] = 0.8
    reload_into_module(edited)
    h2 = compute_config_hash()

    assert h1 != h2, "editing a value must flip the hash"


def test_compute_config_hash_pinned_to_known_value() -> None:
    """Pin the production hash to catch silent drift in compute_config_hash
    inputs (a new field added to ScoringConfigInputs that's accidentally
    not folded into the hash, a serialization change in the hash, etc.).

    Update _PRODUCTION_HASH only when a scoring-config value legitimately
    changes; doing so without also running ``bench.py audit --repin --yes``
    is a procedural error.
    """
    assert compute_config_hash() == _PRODUCTION_HASH
