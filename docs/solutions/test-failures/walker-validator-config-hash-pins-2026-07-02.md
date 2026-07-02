---
title: Walker validation false-fails on every rule attempt — config-hash pin tests fire mid-attempt by design
date: 2026-07-02
category: test-failures
module: scaffold_rule
problem_type: test_failure
component: testing_framework
symptoms:
  - "scripts/scaffold_rule.py --apply reverts every attempt with 'Full pytest suite failed (exit 1)' regardless of rule quality"
  - "tests/bench/test_fixture_freshness.py::test_committed_fixture_config_hash_is_fresh fails: pinned config_hash != live"
  - "tests/test_scoring_weights.py::test_compute_config_hash_pinned_to_known_value fails on the hardcoded _PRODUCTION_HASH literal"
  - "The same suite is fully green on a clean tree; only fails with a freshly-applied scaffold in the working tree"
root_cause: design_flaw
resolution_type: code_fix
severity: high
related_components:
  - scaffold_rule
  - bench
  - scoring_weights
tags:
  - config-hash
  - fixture-freshness
  - walker
  - autonomous-loop
  - false-failure
applies_when:
  - "scaffold_rule.py walker or single-attempt --apply validation reverts with a pytest failure"
  - "adding a new guard test that pins compute_config_hash to a literal or fixture value"
plan_ref: none
---

# Walker validation false-fails on config-hash pin tests

## Problem

Every `scaffold_rule.py --apply` attempt auto-reverted with
`Full pytest suite failed (exit 1)` — including rules that would have
passed the NDCG gates. The walker (`--walk N`) was structurally unable
to ship ANY rule.

## Root cause

A generated rule always adds a `_RULE_QUALITY_MULTIPLIER` entry to
`src/mtg_synergy_graph/data/scoring_weights.json`, which flips
`compute_config_hash()`. Two guard tests added after the walker last
ran (2026-06-09 config-hash discipline) assert pinned hash == live
hash:

- `tests/bench/test_fixture_freshness.py::test_committed_fixture_config_hash_is_fresh`
- `tests/test_scoring_weights.py::test_compute_config_hash_pinned_to_known_value`

Both are **correct for committed states** (they catch a scoring-config
change landing without a re-pin) but **false by design mid-attempt**:
the walker's validation flow is apply → pytest → golden NDCG → broad
NDCG → impact check, and the hash mismatch exists precisely because
the change under adjudication has not (and must not) be re-pinned yet.
Stage 1 therefore failed before the real adjudicators (stages 2–4)
ever ran.

## Fix

`scripts/_validate_rule.py` stage-1 pytest now excludes exactly those
two guards:

```python
rc = pytest.main([
    "tests/", "-q", "--no-cov", "--tb=short",
    "--ignore=tests/bench/test_fixture_freshness.py",
    "--deselect",
    "tests/test_scoring_weights.py::test_compute_config_hash_pinned_to_known_value",
])
```

Commit-time protection is unchanged — both guards still run in
pre-commit and CI, so a kept rule cannot land without a re-pin.

## Ledger hygiene

The two false-failure rows the buggy validator appended to
`docs/rule_attempts.jsonl` (2026-07-02T22:51 / 22:54, reason "Full
pytest suite failed") were removed before commit — they would have
depressed `creature_count_scaler`'s template pass rate with reverts
the rule never earned. The two genuine NDCG verdicts from the same
session were kept.

## Post-fix re-adjudication (both blocks confirmed on merit)

With the validator fixed, the two highest-impact April-blocked
templates were force-retried (`--force`) on the current ruleset:

- `creature_count_scaler` (impact 135.2): golden NDCG drop +0.0039
  (0.2461 → 0.2422) — reverted. The creature-count axis is already
  covered by scaling/lord/token rules; the broad rule displaces
  better picks.
- `x_cost_scaler` (impact 115.1): golden NDCG drop +0.0006
  (0.2461 → 0.2456) — reverted. Nearly neutral (April: −0.0021) but
  still a regression at the zero-tolerance gate.

Conclusion: the walker's queue exhaustion is genuine — the remaining
gap-report capital needs new authoring (axis_feeder tier definitions
for `token`/`tapped`/`untapped`/`blocking`/`counters_GE`; templates
for `needs_template` gaps), not re-runs of blocked templates.

## Prevention

When adding any new test that pins `compute_config_hash` (or a
downstream artifact of it) to a committed value, add it to the
stage-1 exclusion list in `scripts/_validate_rule.py` — or better,
give such tests a shared marker so the validator can deselect by
marker instead of by path.
