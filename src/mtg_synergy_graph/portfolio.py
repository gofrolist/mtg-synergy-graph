"""Portfolio-selection support (plan 2026-07-02-004).

Unit 1 scope: the committed ``rule_id -> family`` map artifact
(``src/mtg_synergy_graph/data/family_map.json``) and its strict loader.
The map is the single family authority for the selection layer
(``rules_seed.json``'s per-row ``family`` field is untouched — see the
plan's "Family-map authority" decision). Later units add the shared
score-decomposition helper and the greedy assembler here; keep this
module free of scoring imports so it can be consumed by both the sim
and the live layer without cycles.

The artifact is inert at this unit: nothing on the scoring path reads
it, and it is not yet folded into ``compute_config_hash`` (Unit 6).
Coverage of the full rule universe is enforced by
``tests/test_family_map.py``, NOT at load time — an unmapped rule_id
must not brick flag-OFF engine load (origin R4); the flag-ON selection
layer will hard-error on unmapped rule_ids it encounters (Unit 5).
"""

from __future__ import annotations

import json
from pathlib import Path

from .port_graph._paths import default_seed_path

_FAMILY_MAP_FILENAME = "family_map.json"

#: ``_readme`` is prose for human readers (excluded from the seed
#: digest when hash wiring lands in Unit 6, mirroring the
#: ``scoring_weights.json`` / ``rules_seed.json`` convention).
_VALID_TOP_LEVEL_KEYS = frozenset({"_readme", "families"})


def load_family_map(path: Path | None = None) -> dict[str, str]:
    """Read the family-map seed JSON into a ``rule_id -> family`` dict.

    ``path`` defaults to the packaged seed
    (``default_seed_path("family_map.json")``); tests pass an explicit
    path to exercise the validation error paths against tmp_path
    artifacts.

    Strict shape validation — missing file, malformed JSON, non-object
    top level, unknown top-level keys, missing ``_readme`` /
    ``families``, non-list-of-strings ``_readme``, non-object
    ``families``, or non-string / empty family values all raise
    ``ValueError`` (never ``assert`` — stripped by ``python -O``).
    Every message names the offending path so the failure is actionable
    without grep.
    """
    p = Path(path) if path is not None else default_seed_path(_FAMILY_MAP_FILENAME)
    try:
        with p.open(encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError as exc:
        raise ValueError(
            f"family_map.json missing at {p}; this file is the single "
            "rule_id->family authority for portfolio selection and must "
            "exist. Restore it from git."
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

    families_raw = raw["families"]
    if not isinstance(families_raw, dict):
        raise ValueError(
            f"{p}: 'families' must be an object mapping rule_id to family, got {type(families_raw).__name__}"
        )

    families: dict[str, str] = {}
    for rule_id, family in families_raw.items():
        if not isinstance(family, str) or not family:
            raise ValueError(
                f"{p}: families.{rule_id}: family must be a non-empty string, got {family!r} ({type(family).__name__})"
            )
        families[rule_id] = family
    return families


#: Loaded once at import, mirroring ``_LOADED_SCORING_WEIGHTS``. A
#: malformed committed artifact fails the import loudly rather than
#: surfacing as silent flag-ON misbehavior later
#: (verify-from-stored-config learning, 2026-04-23).
FAMILY_MAP: dict[str, str] = load_family_map()
