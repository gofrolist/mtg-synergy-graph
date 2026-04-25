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
    _load_scoring_weights,
)

# ---------------------------------------------------------------------------
# Loader strict-shape validation
# ---------------------------------------------------------------------------


def _write(tmp_path: Path, payload: dict[str, Any]) -> Path:
    path = tmp_path / "scoring_weights.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


@pytest.fixture
def patched_loader(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point the loader at a tmp_path JSON; caller writes the payload."""
    sidecar = tmp_path / "scoring_weights.json"
    monkeypatch.setattr(universal_scorer, "_SCORING_WEIGHTS_PATH", sidecar)
    yield sidecar


def test_loader_strict_shape_unknown_top_level(patched_loader: Path) -> None:
    patched_loader.write_text(
        json.dumps(
            {
                "rule_quality_multiplier": {},
                "flat_weight_overrides": {},
                "rogue_section": {},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unknown top-level sections"):
        _load_scoring_weights()


def test_loader_strict_shape_unknown_entry_field(patched_loader: Path) -> None:
    patched_loader.write_text(
        json.dumps(
            {
                "rule_quality_multiplier": {
                    "trigger_resonance": {
                        "value": 0.7,
                        "comment": "",
                        "valeu": 0.7,  # typo — would silently default if not caught
                    }
                },
                "flat_weight_overrides": {},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unknown fields"):
        _load_scoring_weights()


def test_loader_strict_shape_missing_value(patched_loader: Path) -> None:
    patched_loader.write_text(
        json.dumps(
            {
                "rule_quality_multiplier": {"trigger_resonance": {"comment": ""}},
                "flat_weight_overrides": {},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="missing required 'value'"):
        _load_scoring_weights()


def test_loader_strict_shape_missing_comment(patched_loader: Path) -> None:
    patched_loader.write_text(
        json.dumps(
            {
                "rule_quality_multiplier": {"trigger_resonance": {"value": 0.7}},
                "flat_weight_overrides": {},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="missing required 'comment'"):
        _load_scoring_weights()


def test_loader_strict_shape_non_numeric_value_string(
    patched_loader: Path,
) -> None:
    patched_loader.write_text(
        json.dumps(
            {
                "rule_quality_multiplier": {"trigger_resonance": {"value": "0.7", "comment": ""}},
                "flat_weight_overrides": {},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="'value' must be a number"):
        _load_scoring_weights()


def test_loader_strict_shape_non_numeric_value_bool(patched_loader: Path) -> None:
    # bool is a subclass of int in Python — explicitly rejected by the loader.
    patched_loader.write_text(
        json.dumps(
            {
                "rule_quality_multiplier": {"trigger_resonance": {"value": True, "comment": ""}},
                "flat_weight_overrides": {},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="'value' must be a number"):
        _load_scoring_weights()


def test_loader_strict_shape_non_string_comment(patched_loader: Path) -> None:
    patched_loader.write_text(
        json.dumps(
            {
                "rule_quality_multiplier": {"trigger_resonance": {"value": 0.7, "comment": 42}},
                "flat_weight_overrides": {},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="'comment' must be a string"):
        _load_scoring_weights()


def test_loader_empty_comment_allowed(patched_loader: Path) -> None:
    patched_loader.write_text(
        json.dumps(
            {
                "rule_quality_multiplier": {"trigger_resonance": {"value": 0.7, "comment": ""}},
                "flat_weight_overrides": {"evasion": {"value": 0.10, "comment": ""}},
            }
        ),
        encoding="utf-8",
    )
    sections = _load_scoring_weights()
    assert sections["rule_quality_multiplier"] == {"trigger_resonance": 0.7}
    assert sections["flat_weight_overrides"] == {"evasion": 0.1}


# ---------------------------------------------------------------------------
# Dead-key detection against the registered rule universe
# ---------------------------------------------------------------------------


def _emitted_rule_ids() -> set[str]:
    """Universe of rule_ids that any complement helper might emit.

    Combines (a) rule_id="..." string literals scraped from
    complement_rules/*.py source, (b) ComplementRule.rule_id from the
    COMPLEMENT_RULES registration tuple, and (c) declarative rule ids
    from data/rules_seed.json (via DECLARATIVE_RULE_IDS).
    """
    src_dir = Path(universal_scorer.__file__).parent / "complement_rules"
    literals: set[str] = set()
    pattern = re.compile(r'rule_id="([a-z_0-9]+)"')
    for path in src_dir.rglob("*.py"):
        for match in pattern.finditer(path.read_text(encoding="utf-8")):
            literals.add(match.group(1))
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


def _reload_into_module(payload: dict[str, Any]) -> None:
    """Rewrite the patched JSON, reload, and replace the module-level dicts
    in place so consumers (get_scoring_config_inputs / compute_config_hash)
    observe the new values.
    """
    universal_scorer._SCORING_WEIGHTS_PATH.write_text(json.dumps(payload), encoding="utf-8")
    sections = _load_scoring_weights()
    universal_scorer._RULE_QUALITY_MULTIPLIER.clear()
    universal_scorer._RULE_QUALITY_MULTIPLIER.update(sections["rule_quality_multiplier"])
    universal_scorer._FLAT_WEIGHT_OVERRIDES.clear()
    universal_scorer._FLAT_WEIGHT_OVERRIDES.update(sections["flat_weight_overrides"])


def _restore_dicts(quality: dict[str, float], flat: dict[str, float]) -> None:
    universal_scorer._RULE_QUALITY_MULTIPLIER.clear()
    universal_scorer._RULE_QUALITY_MULTIPLIER.update(quality)
    universal_scorer._FLAT_WEIGHT_OVERRIDES.clear()
    universal_scorer._FLAT_WEIGHT_OVERRIDES.update(flat)


def test_comment_field_does_not_affect_compute_config_hash(
    patched_loader: Path,
) -> None:
    saved_quality = dict(universal_scorer._RULE_QUALITY_MULTIPLIER)
    saved_flat = dict(universal_scorer._FLAT_WEIGHT_OVERRIDES)
    try:
        base = {
            "rule_quality_multiplier": {"trigger_resonance": {"value": 0.7, "comment": "first note"}},
            "flat_weight_overrides": {"evasion": {"value": 0.10, "comment": ""}},
        }
        _reload_into_module(base)
        h1 = compute_config_hash()

        edited = json.loads(json.dumps(base))
        edited["rule_quality_multiplier"]["trigger_resonance"]["comment"] = "rewritten note — totally different prose"
        _reload_into_module(edited)
        h2 = compute_config_hash()

        assert h1 == h2, "editing only the comment field must not flip the hash"
    finally:
        _restore_dicts(saved_quality, saved_flat)


def test_value_field_affects_compute_config_hash(
    patched_loader: Path,
) -> None:
    saved_quality = dict(universal_scorer._RULE_QUALITY_MULTIPLIER)
    saved_flat = dict(universal_scorer._FLAT_WEIGHT_OVERRIDES)
    try:
        base = {
            "rule_quality_multiplier": {"trigger_resonance": {"value": 0.7, "comment": ""}},
            "flat_weight_overrides": {"evasion": {"value": 0.10, "comment": ""}},
        }
        _reload_into_module(base)
        h1 = compute_config_hash()

        edited = json.loads(json.dumps(base))
        edited["rule_quality_multiplier"]["trigger_resonance"]["value"] = 0.8
        _reload_into_module(edited)
        h2 = compute_config_hash()

        assert h1 != h2, "editing a value must flip the hash"
    finally:
        _restore_dicts(saved_quality, saved_flat)
