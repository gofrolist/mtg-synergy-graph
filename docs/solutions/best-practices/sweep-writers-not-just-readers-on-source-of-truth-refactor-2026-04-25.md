---
last_updated: 2026-04-25
module: scoring_weights
title: Sweep all writers (not just readers) when externalizing a source-of-truth — code review covers reads but routinely misses regex/codegen writes
tags:
  - refactor
  - source-of-truth
  - code-review-checklist
  - scaffold-tooling
  - sidecar-json
  - silent-failure
  - regex-patcher
  - systemic-pattern
problem_type: best_practice
symptoms:
  - scaffold tool's regex-based patcher silently no-ops after the literal it edits is deleted
  - new rule registers in code but its weight multiplier defaults to 1.0 with no audit signal
  - code review covered every reader of the migrated dict but missed the regex writer in scripts/
  - bug surfaces 1 day after the refactor commit, on first scaffold attempt
  - the SAME blind spot already bit a prior externalization three days earlier — systemic, not one-off
resolution_type: workflow_improvement
applies_when:
  - Externalizing a Python literal (dict, list, frozenset, constant) to an on-disk artifact (JSON, YAML, TOML, SQLite, env file).
  - Renaming or restructuring a module-level config dict that any tool may grep for by name.
  - Deleting a previously-public binding even when it appears unused at the call-graph level — writers/patchers/scaffolders frequently access by string match, not by import.
  - The downstream consumer silently degrades on missing data (default value of 1.0, empty list, None) instead of raising.
created: 2026-04-25
spec_ref: docs/solutions/best-practices/extract-python-dict-to-json-sidecar-2026-04-25.md
---

# Sweep all writers when externalizing a source-of-truth

When externalizing a Python-literal source-of-truth to an on-disk
artifact (JSON / YAML / SQLite / etc.), code review must enumerate
**both** sides of the access set:

- **Readers** — every callsite that imports or reads the binding.
  Typical code-review focus.
- **Writers** — every script, generator, scaffold, or migration tool
  that programmatically *mutates* the binding. **Frequently missed.**

This is a systemic blind spot in this codebase, not a one-off miss.
It has bit twice in four days against `scripts/scaffold_rule.py`
specifically (see "Why This Matters" below).

## Context

On 2026-04-25 a structural refactor (commit `7fc4a95`) externalized
two long-lived inline literals from
`src/mtg_synergy_graph/universal_scorer.py` into
`data/scoring_weights.json`:

- `_RULE_QUALITY_MULTIPLIER` (471 lines)
- `_FLAT_WEIGHT_OVERRIDES` (6 entries)

The work was preceded by a spec (`fdba92b`), followed by cleanup
(`7338d48`), an audit re-pin (`2f04567`), and a thorough
`ce-code-review` LFG pass (`79edc18`) that produced 25 findings
across eight reviewer personas (correctness, testing,
maintainability, project-standards, reliability, kieran-python,
agent-native, learnings).

Every reviewer enumerated the *readers* of the dict —
`get_scoring_config_inputs`, `compute_config_hash`, the `score()`
callsite at line 1002 — and verified each was migrated to the JSON
loader. None of them grepped for the *writers*.

`scripts/scaffold_rule.py::_patch_scorer` quietly survived the
refactor still pointing at a Python literal that no longer existed.
The latent bug surfaced one day later (commit `72e0447`) when
scaffolding `damage_prevention_voltron` (rule #15 from
`gap_report`). `_insert_before_container_close(src,
"_RULE_QUALITY_MULTIPLIER", …)` returned `None` because the dict
was gone from source; under the existing short-circuit logic the
whole patch returned `False` ("already-present"). The new rule
would have shipped with the `.get(rule_id, 1.0)` fallback instead
of the generator's chosen `1.5` — silently misweighted, no error,
no audit signal.

## Guidance

**Reviewer checklist snippet** (drop into future `ce-code-review`
runs on source-of-truth refactors):

```
[ ] All readers of <REMOVED_BINDING> migrated to <NEW_ARTIFACT>
[ ] Grep repo for tools that *write* to <REMOVED_BINDING>:
      rg -nF '<REMOVED_BINDING>' scripts/ tools/ migrations/
[ ] Each writer updated to mutate <NEW_ARTIFACT> instead
[ ] Auto-revert / rollback / snapshot lists include <NEW_ARTIFACT>
[ ] Pre-commit / audit / config-hash guardrails still fire on
    edits to <NEW_ARTIFACT> (not just on the old .py file)
[ ] At least one end-to-end run of every writer tool on a throwaway
    input before merging
```

**`_patch_scorer` BEFORE → AFTER** (essence; full diff at commit
`72e0447`):

```python
# BEFORE: both writes targeted Python literals in universal_scorer.py
new_src = _insert_before_container_close(src, "_RULE_TO_BUCKET", ..., "{", "}")
new_src = _insert_before_container_close(new_src, "_RULE_QUALITY_MULTIPLIER", ..., "{", "}")
if new_src is None: return False        # silently False after _RULE_QUALITY_MULTIPLIER was deleted
SCORER_PATH.write_text(new_src)
```

```python
# AFTER: split — bucket stays inline, multiplier goes to JSON
new_src = _insert_before_container_close(src, "_RULE_TO_BUCKET", ..., "{", "}")
SCORER_PATH.write_text(new_src)
return _patch_scoring_weights_json(art)  # loads JSON, inserts rule_id, writes back
# _affected_paths(art) now also includes SCORING_WEIGHTS_PATH
```

## Why This Matters

The cost of missing a writer is **asymmetric and silent**:

- **No exception, no warning** — `_insert_before_container_close`
  returning `None` was already a normal control-flow signal; the
  patcher saw it as "already present."
- **Misweighted scoring with no audit trail** — the rule would have
  gone live at default multiplier `1.0` instead of `1.5`. Per
  `memory/feedback_audit_every_change.md`, every scoring-path change
  must be NDCG-audit-gated; a tool that fails to register a
  scoring-affecting entry **indirectly bypasses that guarantee**
  because the audit measures what's actually in the weights table,
  not what the generator intended to put there.
- **Auto-revert blast-radius drift** — without `SCORING_WEIGHTS_PATH`
  in `_affected_paths`, a failed scaffold validation rolls back the
  `.py` edits but leaves an orphan JSON entry, corrupting the
  source-of-truth in a way that's invisible until the next
  `compute_config_hash` mismatch.

### This is a systemic pattern, not a one-off (session history)

The same blind spot already manifested **three days earlier** on a
structurally identical externalization. Plan 003 (session
`f5647951`, 2026-04-22) migrated:

- `EVENT_MATCH_MAP` (Python dict in `graph_engine.py`) →
  `data/event_match_seed.json`
- 16 Python rule files in `complement_rules/generated/` →
  declarative rows in `data/rules_seed.json`

That refactor's reader-side rigor was extreme: `bench.py audit
--expect-identity` ran after every unit, per-rule migration tests
asserted byte-for-byte equivalence, and multiple `ce-code-review`
persona passes generated dozens of findings. Yet the plan
**explicitly deferred the `scaffold_rule.py` rewrite as a follow-up**:

> "Substrate + 2-rule POC lands here; full 30-rule migration +
> `scaffold_rule.py` rewrite becomes a follow-up plan gated on this
> one's green `bench.py audit --expect-identity`."

That follow-up was never tracked as a coupling risk. `git log`
confirms `scripts/scaffold_rule.py` had **zero edits** between
`247605b` (Apr 21) and today's fix `72e0447` (Apr 25). The blind
spot recurred on the very next externalization.

The lesson: **when a plan defers a writer-side rewrite, that
deferral is itself a coupling risk that needs an explicit tracker —
a TODO in `docs/reviews/`, a GitHub issue, or a follow-up ticket.
Without it, the next externalization in the same area inherits the
same blind spot.**

### Pre-commit hook coverage gap

The pre-commit `bench.py audit (advisory)` hook in
`.pre-commit-config.yaml` fires on edits to `complement_rules/`,
`universal_scorer.py`, `graph_engine.py`, `embeddings/`, and
`data/scoring_weights.json` (the last added in commit `79edc18`).
It does **not** fire on edits to `scripts/scaffold_rule.py`. So a
broken patcher has no automated tripwire — the only signal is the
next `--apply` attempt.

Two reasonable mitigations:
1. Add `scripts/scaffold_rule.py` to the bench-audit hook trigger
   list (small surface, high signal: any edit to the scaffolder
   is a potential coupling risk).
2. Add a smoke-test step in the scaffolder itself that runs against
   a no-op throwaway template before every `--apply`, asserting
   that all integration writes succeeded.

## When to Apply

Apply this checklist whenever a change:

- Moves a Python literal (dict, list, frozenset, dataclass instance)
  to an on-disk artifact (JSON, YAML, TOML, SQLite, env file).
- Renames a module-level config dict / constant that any tool may
  grep for by name.
- Deletes a previously-public binding even when it appears unused
  at the call-graph level — writers / patchers / scaffolders
  frequently access by string match, not by import.
- Changes the storage shape of a registry that is mutated by
  code-generation tools.
- Defers a writer-side rewrite as a follow-up — explicitly track
  the deferral.

## Examples

### Incident 1 — `_RULE_QUALITY_MULTIPLIER` (2026-04-25)

- **Refactor**: `_RULE_QUALITY_MULTIPLIER` (literal) →
  `data["rule_quality_multiplier"]` in `data/scoring_weights.json`
  (commits `fdba92b` … `79edc18`).
- **Reader migration**: clean — loader + 12 tests in `7fc4a95`;
  ce-code-review `20260425-013937-70eb5420` verified every reader.
- **Missed writer**: `scripts/scaffold_rule.py::_patch_scorer`,
  which accesses the binding by *string-search inside source text*
  (`_insert_before_container_close(src, "_RULE_QUALITY_MULTIPLIER",
  …)`). Neither static analysis nor reader-focused review surfaces
  this.
- **Fix**: commit `72e0447`. Split the patcher (`_RULE_TO_BUCKET`
  stays inline, multiplier goes to JSON via
  `_patch_scoring_weights_json`) and extended `_affected_paths` to
  include `SCORING_WEIGHTS_PATH` so the auto-revert snapshot stays
  consistent.

### Incident 2 — Plan 003 (2026-04-22, session history)

- **Refactor**: `EVENT_MATCH_MAP` and 16 `complement_rules/generated/*.py`
  files migrated to JSON sidecars (`data/event_match_seed.json`,
  `data/rules_seed.json`).
- **Reader migration**: extreme rigor — identity audit, per-rule
  migration tests, multiple ce-code-review passes.
- **Missed writer**: `scripts/scaffold_rule.py` was the original
  generator of the now-deprecated `complement_rules/generated/*.py`
  files. The plan **explicitly deferred** the scaffolder rewrite
  as a follow-up. The deferral was never tracked.
- **Consequence**: latent until incident 1 surfaced it three days
  later on the very next externalization.

## Related

- `docs/solutions/best-practices/extract-python-dict-to-json-sidecar-2026-04-25.md`
  — the *pattern* doc for this kind of refactor (how to do it).
  This finding is the *meta-lesson* (failure mode the pattern
  should warn against).
- `docs/solutions/best-practices/verify-from-stored-config-not-code-defaults-2026-04-23.md`
  — sibling failure mode: same root structure (asymmetric audit
  during refactor) but on the verifier-vs-builder side rather than
  reader-vs-writer.
- `docs/RULE_PLANNING.md` §6.6 — workflow checkpoint that should
  reference this checklist for any future scaffolder/generator
  refactor.
- `memory/feedback_audit_every_change.md` — the discipline this
  finding upholds; a writer that silently no-ops bypasses the audit
  guarantee.

## Reference commits

- `fdba92b` — spec: externalize scoring weights to
  `data/scoring_weights.json` (M3+M4)
- `7fc4a95` — refactor: 471-line dict → JSON + loader + 12 tests
- `7338d48` — chore: remove one-shot migration artifacts
- `2f04567` — chore(bench): re-pin `golden_set_run.json` to current
  scoring config
- `79edc18` — refactor: apply ce-code-review LFG fixes (19 findings;
  reader-side only)
- **`72e0447`** — fix(scaffold): patch `data/scoring_weights.json`
  instead of removed dict literal *(primary commit for this
  finding)*
