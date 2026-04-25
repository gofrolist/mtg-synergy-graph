---
last_updated: 2026-04-25
module: scoring_weights
title: Extract Python-literal dict to JSON sidecar with hash-gated identity preservation
tags: [refactor, config, sidecar-json, config-hash, identity-preservation, scoring]
problem_type: best_practice
symptoms:
  - large dict literal accumulates inline narrative comments per entry
  - tunable values are buried under prose, hard to grep/jq
  - sweep history grows unbounded inside source files
  - per-value diffs require re-reading multi-paragraph rationales
resolution_type: pattern
applies_when:
  - A Python-literal dict has grown into a value-with-prose blob (~hundreds of lines, mostly narrative).
  - The dict's values flow through a config-hash mechanism (e.g., compute_config_hash) and a re-pin discipline exists.
  - The dict is hand-tuned config, not derived data — so an external sidecar is appropriate.
created: 2026-04-25
spec_ref: docs/superpowers/specs/2026-04-25-scoring-weights-externalization-design.md
---

# Extract Python-literal dict to JSON sidecar

When a tunable Python-literal dict grows unbounded narrative around its
entries, extract the values to a JSON sidecar with a strict-shape
loader. The shape is `{section: {key: {value: ..., comment: ""}}}`
where `comment` is metadata for human readers and is intentionally
excluded from the hash. Sweep history continues to live in git log +
`docs/RULE_HISTORY.md` — three places to maintain the same data is
the anti-pattern this avoids, not solves.

## Why

- **Grep/jq-friendly**: `jq '.rule_quality_multiplier.cost_reducer.value'`
  beats reading 20 lines of inline prose.
- **One-line value diffs**: a tuning commit edits `"value": 1.2` →
  `"value": 1.6`, not a 30-line block of sweep tables and prose.
- **Bounded source files**: scorer modules stay focused on scoring,
  not on accumulating per-entry rationale.
- **Single source of truth per concern**: prior approaches that
  bundled `value` + `rationale` + `sweep_history` into the same file
  recreated the original problem at a structured level. RULE_HISTORY.md
  + git log already exist for archaeology.

## Pattern

### 1. JSON shape

Single file, two-level nesting, `{value, comment}` per entry. Strict
shape — both fields required, `comment` may be `""`.

```json
{
  "rule_quality_multiplier": {
    "cost_reducer": {"value": 1.2, "comment": ""},
    "panharmonicon": {"value": 2.0, "comment": "Boosted 2026-04-24..."}
  },
  "flat_weight_overrides": { ... }
}
```

### 2. Loader at module import

```python
def _load_scoring_weights() -> ScoringWeights:
    with _SCORING_WEIGHTS_PATH.open(encoding="utf-8") as f:
        raw = json.load(f)
    # validate top-level sections (reject unknown)
    # for each section: validate per-entry shape (value required, comment required)
    # explicitly reject bool (since bool is int subclass)
    # coerce int → float
    return ScoringWeights(...)

_LOADED = _load_scoring_weights()
_RULE_QUALITY_MULTIPLIER = _LOADED.rule_quality_multiplier
_FLAT_WEIGHT_OVERRIDES = _LOADED.flat_weight_overrides
```

Strict-shape validation at import catches typos like `"valeu"` that
would silently load as a default value otherwise. Module-import-time
fail-fast is intentional: a malformed config breaking every consumer
at import is preferable to silently degraded scoring.

### 3. Hash provenance

`compute_config_hash` reads the live module-level dicts (which ARE
the same Python objects as the loaded JSON values). Editing a
`value` in the JSON flips the hash; editing a `comment` does not.
Verify via:

```python
assert _LOADED.rule_quality_multiplier is _RULE_QUALITY_MULTIPLIER
```

### 4. Dead-key test

Every key in the JSON must correspond to a real, registered rule.
Build the universe from three sources: regex scrape of `rule_id="..."`
literals (with both quote styles), the registered rule tuple, and any
declarative-rule registry. Add a sanity floor (`assert len(literals)
> N`) so a future scraper-breaking refactor fails loudly.

### 5. Identity audit as load-bearing verification

After the migration, run `bench.py audit --expect-identity` to assert
that scores are bit-identical to the pinned tensor. If the hash AND
all 100 commanders' top-30 reproduce exactly, the refactor was
lossless. This is the single contract that proves the JSON is a
faithful representation of the prior literals.

### 6. .gitignore allowlist

The `data/` dir is typically gitignored except for explicit
allowlists. Add an explicit `!data/scoring_weights.json` line and
verify with `git ls-files data/scoring_weights.json` (must return
the file path) before committing.

## Anti-patterns

- **Carrying `sweep_history[]` in the JSON**: recreates the
  three-places-for-one-fact problem the refactor was meant to solve.
  Defer to git log + RULE_HISTORY.md.
- **Auto-summarizing the prose into a `comment` at migration time**:
  produces uneven, often-cryptic one-liners. Better to start with
  empty comments and fill them organically as rules are touched.
- **Conflating extraction with re-pin in one commit**: erases the
  bit-identity proof. Run `--expect-identity` against the pre-refactor
  pin first; commit the refactor; then re-pin separately for any
  pre-existing post-pin tunings.
- **Loading from a Python module-level constant for hash input
  instead of the loaded value**: same class of bug as the embeddings
  pipeline incident (`docs/solutions/best-practices/verify-from-stored-config-not-code-defaults-2026-04-23.md`).
  The hash MUST read what was loaded, not what the source code says.
- **Auditing only readers, not writers, during code review.** This
  refactor's `ce-code-review` covered every reader of
  `_RULE_QUALITY_MULTIPLIER` (8 reviewer personas, 25 findings) but
  missed `scripts/scaffold_rule.py:_patch_scorer`'s regex insertion
  into the now-deleted Python literal. Surfaced one day later on the
  next scaffold attempt; would have shipped a new rule with default
  multiplier 1.0 instead of the generator's chosen value, with no
  audit signal. Same blind spot bit plan 003 three days earlier
  (`EVENT_MATCH_MAP` / `complement_rules/generated/*.py` migrations
  deferred the scaffolder rewrite as untracked follow-up). See
  `docs/solutions/best-practices/sweep-writers-not-just-readers-on-source-of-truth-refactor-2026-04-25.md`
  for the full checklist.

## Related

- **`docs/solutions/best-practices/sweep-writers-not-just-readers-on-source-of-truth-refactor-2026-04-25.md`**
  — the meta-lesson distilled from this refactor's blind spot:
  enumerate writers, not just readers, when externalizing a
  source-of-truth. Adopt its checklist for any future externalization.
- `docs/solutions/best-practices/offline-oracle-hash-pattern-2026-04-23.md`
  — sidecar-with-hash-enforcement pattern (different mechanism: built
  SQLite vs hand-edited JSON; same principle).
- `docs/solutions/best-practices/verify-from-stored-config-not-code-defaults-2026-04-23.md`
  — hash-from-loaded-not-from-defaults discipline.
- `docs/solutions/best-practices/flag-gated-multi-port-rule-pattern-2026-04-23.md`
  §10 — `--expect-identity` as the load-bearing contract for
  hash-invariant refactors.
- `docs/solutions/build-errors/gitignore-negation-under-ignored-parent-2026-04-23.md`
  — the `.gitignore` allowlist gotcha that affects all `data/` sidecars.
