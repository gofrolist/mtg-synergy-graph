# Scoring weights externalization (M3 + M4)

**Date:** 2026-04-25
**Origin:** ce-code-review run `20260424-175920-1c6eb0da`, findings M3
(`_RULE_QUALITY_MULTIPLIER` is a 471-line dict of inline sweep narratives)
and M4 (`_FLAT_WEIGHT_OVERRIDES` docstring accumulates an unbounded
changelog).
**Status:** Design — awaiting user review before plan.

---

## 1. Goal & scope

Move two scoring-weight dicts from Python source to a JSON sidecar so
their values are queryable, diffable, and decoupled from the prose
that has accumulated around them.

**In scope:**

- `_RULE_QUALITY_MULTIPLIER` (`src/mtg_synergy_graph/universal_scorer.py:471-979`,
  ~471 lines, ~380 of inline sweep narratives).
- `_FLAT_WEIGHT_OVERRIDES` (same file, lines ~447-469; 6 entries with
  a dated migration changelog in the docstring).

**Out of scope:**

- `_SYNERGY_PAIRS` (20 lines, no growth pattern).
- `pathway._ENABLE_PATHWAY_RULES`, embedding knobs (`embedding_w`,
  `embedding_k`, `vectorizer_version`) — different lifecycle, no inline
  prose problem.
- Any tuning of any value. Every float in the new JSON equals its
  prior Python literal exactly.
- Re-pinning the audit fixture. The contract is that
  `bench.py audit --expect-identity` passes after migration; if it
  does, the pin is unchanged.

---

## 2. File: `data/scoring_weights.json`

Single JSON file in the existing `data/` directory (alongside
`event_match_seed.json` and `rules_seed.json`). Two top-level sections,
one per dict; each entry is `{value, comment}`.

```json
{
  "rule_quality_multiplier": {
    "self_bridging_cascade": {"value": 1.5, "comment": ""},
    "edict_feeder":          {"value": 2.0, "comment": ""}
  },
  "flat_weight_overrides": {
    "evasion":        {"value": 0.10, "comment": ""},
    "token_producer": {"value": 0.18, "comment": ""},
    "spell_density":  {"value": 0.30, "comment": ""},
    "tribal_density": {"value": 0.50, "comment": ""},
    "etb_self":       {"value": 0.01, "comment": ""}
  }
}
```

**Per-entry contract:**

- `value`: required; number (int or float, parsed as `float`); fed into
  `compute_config_hash`. Editing flips the hash.
- `comment`: required; string; may be empty. Intended for a one-line
  human note ("Boosted 2026-04-24 after gate narrowing — see commit
  abc123"). Not fed into `compute_config_hash`. Not exposed at
  runtime beyond shape validation.

**Why a single file with two sections** (vs two files): the two dicts
share a release/re-pin lifecycle — when you tune a multiplier, you
re-pin the tensor; same for an override. Splitting implies they can
drift independently, which they cannot. Single file gives one git diff
for related tunings (e.g., the "narrow gate + boost" sessions in
commit `10d8ac0` touch both a flat override and a quality multiplier
together).

**Why JSON** (vs YAML/TOML): consistency with existing committed seed
files (`event_match_seed.json`, `rules_seed.json`); no new runtime
dependency.

**Why `value + comment` only** (vs the reviewer's full
`value + rationale + sweep_history`): the project already has
`docs/RULE_HISTORY.md` and git log for sweep archaeology. Keeping
sweep history in three places is the M4 anti-pattern at larger scale.
The sidecar is the *current truth*; historical context lives in the
existing channels.

---

## 3. Loader (`universal_scorer.py`)

The loader is a module-private function that runs at import time and
populates the existing module-level dicts. Existing call sites
(`score()` at line 1002, `get_scoring_config_inputs()` at 287-294,
any `mock.patch.dict` in tests) are unchanged — they still see live
module-level dicts of the same type.

```python
_SCORING_WEIGHTS_PATH = (
    Path(__file__).parent.parent.parent / "data" / "scoring_weights.json"
)

_TOP_LEVEL_KEYS = frozenset({"rule_quality_multiplier", "flat_weight_overrides"})
_ENTRY_KEYS = frozenset({"value", "comment"})


def _load_scoring_weights() -> tuple[dict[str, float], dict[str, float]]:
    """Read data/scoring_weights.json into the two scoring-weight dicts.

    Strict shape validation: unknown top-level keys, unknown per-entry
    fields, missing 'value', or non-numeric 'value' all raise
    ValueError at module import. Registry-membership is checked
    separately by tests/test_scoring_weights.py to avoid coupling the
    production import path to plugin load order.
    """
    with _SCORING_WEIGHTS_PATH.open(encoding="utf-8") as f:
        raw = json.load(f)
    extra = set(raw) - _TOP_LEVEL_KEYS
    if extra:
        raise ValueError(
            f"unknown top-level sections in scoring_weights.json: {sorted(extra)}"
        )
    sections: dict[str, dict[str, float]] = {}
    for section in _TOP_LEVEL_KEYS:
        section_raw = raw.get(section, {})
        out: dict[str, float] = {}
        for key, entry in section_raw.items():
            if not isinstance(entry, dict):
                raise ValueError(
                    f"{section}.{key}: entry must be an object, got "
                    f"{type(entry).__name__}"
                )
            extra_fields = set(entry) - _ENTRY_KEYS
            if extra_fields:
                raise ValueError(
                    f"{section}.{key}: unknown fields {sorted(extra_fields)}"
                )
            if "value" not in entry:
                raise ValueError(f"{section}.{key}: missing required 'value'")
            if "comment" not in entry:
                raise ValueError(f"{section}.{key}: missing required 'comment'")
            value = entry["value"]
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ValueError(
                    f"{section}.{key}: 'value' must be a number, got "
                    f"{type(value).__name__}"
                )
            if not isinstance(entry["comment"], str):
                raise ValueError(
                    f"{section}.{key}: 'comment' must be a string, got "
                    f"{type(entry['comment']).__name__}"
                )
            out[key] = float(value)
        sections[section] = out
    return sections["rule_quality_multiplier"], sections["flat_weight_overrides"]


_RULE_QUALITY_MULTIPLIER, _FLAT_WEIGHT_OVERRIDES = _load_scoring_weights()
```

**Validation altitudes** (Option C from brainstorming):

| Check | Where | Failure mode |
|---|---|---|
| Malformed JSON | Loader, import time | `json.JSONDecodeError` |
| Unknown top-level section | Loader, import time | `ValueError` |
| Unknown per-entry field (`valeu` typo, etc.) | Loader, import time | `ValueError` |
| Missing required `value` | Loader, import time | `ValueError` |
| Missing required `comment` | Loader, import time | `ValueError` |
| Non-numeric `value` (string, bool) | Loader, import time | `ValueError` |
| Non-string `comment` | Loader, import time | `ValueError` |
| Dead `rule_id` (no longer registered) | `tests/test_scoring_weights.py`, CI only | Test failure |
| Dead bucket key in `flat_weight_overrides` | `tests/test_scoring_weights.py`, CI only | Test failure |

Strict shape at import time catches typos that would silently load as
multiplier=1.0 (a scoring regression with no error). Registry-membership
at test time catches stale entries without coupling production import
to plugin load order.

---

## 4. `compute_config_hash` treatment

**Code:** `src/mtg_synergy_graph/bench/tensor.py:38-89` is **unchanged**.
The hash already reads `cfg.rule_quality_multiplier.items()` and
`cfg.flat_weight_overrides.items()` through `get_scoring_config_inputs()`,
which returns the live module-level dicts. Loading those dicts from
JSON instead of Python literals leaves `repr(sorted(items()))`
byte-identical as long as the float values round-trip cleanly. The
migration script (§5) asserts this; the `--expect-identity` audit
(§5 step 4) verifies it end-to-end.

**Docstring update:** add one line to `tensor.py:41-44`:

> `_RULE_QUALITY_MULTIPLIER` and `_FLAT_WEIGHT_OVERRIDES` are loaded
> from `data/scoring_weights.json` at module import. Editing a `value`
> flips the hash; editing a `comment` does not.

**No new hash inputs.** The `comment` field is intentionally excluded
— including it would invalidate the tensor every time someone wrote
a one-line context note, which is the opposite of the design intent.

**Identity proof:** `bench.py audit --expect-identity` after migration
must pass (asserts every score in the pinned tensor reproduces
bit-identically). This is the single load-bearing verification for
the whole refactor. If it fails, the migration is wrong; do not merge.

---

## 5. Migration mechanics

**One-shot script** `scripts/migrate_scoring_weights.py`, deleted in a
follow-up commit:

```python
"""One-shot extractor: read current _RULE_QUALITY_MULTIPLIER and
_FLAT_WEIGHT_OVERRIDES from universal_scorer.py and emit
data/scoring_weights.json with empty comments.

Asserts float round-trip identity before writing.
"""
from mtg_synergy_graph.universal_scorer import (
    _RULE_QUALITY_MULTIPLIER, _FLAT_WEIGHT_OVERRIDES,
)
import json, sys
from pathlib import Path


def main() -> int:
    payload = {
        "rule_quality_multiplier": {
            k: {"value": v, "comment": ""}
            for k, v in sorted(_RULE_QUALITY_MULTIPLIER.items())
        },
        "flat_weight_overrides": {
            k: {"value": v, "comment": ""}
            for k, v in sorted(_FLAT_WEIGHT_OVERRIDES.items())
        },
    }
    out_path = Path("data/scoring_weights.json")
    out_path.write_text(
        json.dumps(payload, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )

    reloaded = json.loads(out_path.read_text(encoding="utf-8"))
    for section, source in (
        ("rule_quality_multiplier", _RULE_QUALITY_MULTIPLIER),
        ("flat_weight_overrides", _FLAT_WEIGHT_OVERRIDES),
    ):
        for k, v in source.items():
            roundtripped = reloaded[section][k]["value"]
            if repr(float(roundtripped)) != repr(v):
                print(
                    f"FLOAT DRIFT: {section}.{k}: {v!r} -> {roundtripped!r}",
                    file=sys.stderr,
                )
                return 1
    print(
        f"wrote {out_path} "
        f"({len(_RULE_QUALITY_MULTIPLIER)} multipliers, "
        f"{len(_FLAT_WEIGHT_OVERRIDES)} flat overrides)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

**Run order during the migration commit:**

1. Run script against pre-migration `universal_scorer.py` →
   produces `data/scoring_weights.json`.
2. Replace the two dicts in `universal_scorer.py` with the loader
   from §3.
3. Strip the `_FLAT_WEIGHT_OVERRIDES` changelog docstring (per the M4
   `fix_sketch`: "Strip the dated migration trail from the docstring.
   Replace with a single sentence explaining the invariant ('weights
   must stay comparable to IDF median 0.09-0.15')").
4. `uv run pytest tests/` (full suite, ~1230 tests, ~1-2s).
5. `uv run scripts/bench.py audit --expect-identity` — load-bearing.
6. Commit.
7. Follow-up commit: `git rm scripts/migrate_scoring_weights.py
   tests/test_migration_parity.py`.

---

## 6. Tests

**New file `tests/test_scoring_weights.py`** (long-running suite):

| Test | Purpose |
|---|---|
| `test_loader_strict_shape_unknown_top_level` | Unknown section → `ValueError` |
| `test_loader_strict_shape_unknown_entry_field` | `valeu` typo → `ValueError` |
| `test_loader_strict_shape_missing_value` | Missing `value` → `ValueError` |
| `test_loader_strict_shape_missing_comment` | Missing `comment` → `ValueError` |
| `test_loader_strict_shape_non_numeric_value` | `"0.5"` (string) and `true` (bool) → `ValueError` |
| `test_loader_strict_shape_non_string_comment` | `comment: 42` → `ValueError` |
| `test_loader_empty_comment_allowed` | `comment: ""` loads fine |
| `test_no_dead_rule_ids_in_quality_multiplier` | Every key ∈ `COMPLEMENT_RULES` registry |
| `test_no_dead_keys_in_flat_weight_overrides` | Every key ∈ `_RULE_TO_BUCKET` |
| `test_comment_field_does_not_affect_compute_config_hash` | Patch JSON with new comment, reload, hash unchanged |
| `test_value_field_affects_compute_config_hash` | Patch JSON with new value, reload, hash differs |

Loader tests parametrize JSON content via `tmp_path` plus a helper
that monkeypatches `_SCORING_WEIGHTS_PATH` and re-invokes
`_load_scoring_weights()`. The hash tests rebuild the module-level
dicts in place (preserving the `mock.patch.dict` semantics that
existing tests rely on) and call `compute_config_hash()` twice.

**One-shot migration parity test `tests/test_migration_parity.py`** —
asserts the loaded JSON dict equals the prior in-memory dict on the
migration PR. Deleted in the follow-up commit alongside
`scripts/migrate_scoring_weights.py`.

---

## 7. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Float repr drift across the JSON round-trip flips the hash | Migration script's identity check fails before the JSON is written; CI's `--expect-identity` audit catches it after. All current values are hand-tuned decimals — unlikely to be affected. |
| Module import path discovery breaks if package is installed as a wheel | Project convention: `data/` is a sibling of `src/`, run via `uv run` from repo root, never installed as wheel. Documented here as an explicit assumption. |
| Loader tests mutate module-level dicts and don't restore them | Use `monkeypatch.setattr` on the dict objects (not on the bindings) so `get_scoring_config_inputs()` sees the right values during the test. |
| Future contributor adds a new rule with a non-1.0 multiplier and forgets to edit the JSON | `test_no_dead_rule_ids_in_quality_multiplier` only checks the inverse direction (no extra keys). A separate test or pre-commit hook is *not* added — the convention "if you skip the JSON, the multiplier defaults to 1.0" is acceptable behavior. |

---

## 8. Non-goals (explicit)

- No tuning of any value. The migration is structural only.
- No migration of `_SYNERGY_PAIRS`, pathway flag, or embedding knobs.
- No re-pinning of the audit fixture.
- No new abstractions (no Config object, no schema-validation library
  — just stdlib `json` + a hand-rolled validator).
- The `comment` field is not fed into `compute_config_hash`, not
  exposed via `get_scoring_config_inputs()`, and not loaded into any
  runtime data structure beyond what shape validation requires. It
  exists only on disk for human readers and `jq`/grep queries.
