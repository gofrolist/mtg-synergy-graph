---
last_updated: 2026-04-24
module: complement_rules
title: All existing rule gates live in the golden-set bubble; long-tail rules ship unvalidated
tags:
  - complement-rules
  - rule-validation
  - ndcg-audit
  - golden-set
  - long-tail
  - ward-2-tribal
  - methodology
problem_type: best_practice
resolution_type: pattern
applies_when:
  - Shipping a new complement rule whose target commanders are outside the golden 100.
  - Shipping a rule whose target commanders lack EDHREC Hi-Syn data.
  - Evaluating a tribal peer-match rule that may be mechanically vacuous.
  - Adding a rule via `data/rules_seed.json` with no code change and expecting automated gates to catch quality issues.
---

## Context — what went wrong

On 2026-04-24 the session shipped `ward_2_tribal` (commit `ac2a8fa`),
a declarative peer-match on the `keyword.Ward:2` signature that fired
on 85 long-tail commanders. All pre-commit gates passed:

- `bench.py audit` — Δ = +0.000000 on the 100 golden commanders; histogram `no_change = 100`.
- `tests/test_rules_migration_peer_tribal.py::test_keyword_tribal_rule_emits_partner[ward_2_tribal-Ward:2]` — green.
- `find_all_complements` spot-check on 4 sample commanders — rule fires, 94 emissions each.

The rule was committed. Twenty minutes later, the user asked "show me
the commanders that improved." Direct measurement revealed:

- **867 new cards entered top-30** across 69 of 85 target commanders —
  but "Top 3 by lift" were commanders whose ENTIRE top-30 (30 of 30)
  got replaced.
- **Uniform +0.16 score band** across all 94 Ward:2 peer emissions —
  coefficient of variation ≈ 0.01 on the affected commanders.
- **No mechanical interaction** between Ward:2 creatures. Ward is a
  defensive cost on being targeted; two Ward:2 creatures do not
  synergize by sharing the keyword.

The rule was technically correct, passed every existing gate, and was
mechanically vacuous. Follow-on attempt to reduce impact via
`weight_hint: 2.0 → 0.5` did nothing because **`weight_hint` is
stored and loaded but never consumed by the scoring pipeline** —
another silent dead field. The rule and follow-on weight-change were
both reverted.

## Why existing gates missed it

| Gate | Measures | Why it didn't catch Ward:2 |
|------|----------|---------------------------|
| `bench.py audit` (golden 100) | Aggregate NDCG on popular commanders | None of the 100 carry Ward:2 |
| `--expect-identity` | Bitwise score equality vs pinned fixture | Same reason |
| `--rule RULE_ID` | Per-rule ablation over persisted tensor | Tensor is built from golden 100; rule has zero tensor rows |
| `--inspect RULE_ID` | Top contribution rows | Same — no rows |
| `--collinearity` | Rule-pair VIF / correlation | Requires tensor presence |
| `_validate_rule` / `classify_impact` | Hi-Syn Δ on touched commanders | Needs EDHREC Hi-Syn data — most Ward:2 commanders lack it |
| `find_all_complements` emission count | Does the rule fire? | Confirms firing but says nothing about whether emissions are meaningful |
| Unit test `test_keyword_tribal_rule_emits_partner` | Synthetic 2-card fixture → 1 complement | Same — binary "fires or not" |

**Root cause:** Every automated gate lives in the golden-set bubble.
A rule whose full population of target commanders sits in the long
tail is structurally invisible to every pre-commit check.

## Guidance — the new gate

Added `scripts/rule_quality_gate.py` on 2026-04-24 (same commit as
this doc). Two checks per rule, entirely from live scoring on the
full commander universe, no tensor required:

**Gate A — pre-existing coverage.** For each commander the rule fires
on, count distinct *other* rule_ids already firing. Aggregate median:
- `>= 3`: PASS on this axis — targets are well-covered; the rule is
  amplifying existing signal.
- `1..3`: WARN — targets are thinly covered; the rule may dominate
  their recommendations.
- `< 1`: flag toward REJECT — targets have essentially no other rules
  firing; the rule is filling a mechanical vacuum.

**Gate B — top-30 score dispersion on target commanders.** Score each
target's top-30, compute coefficient of variation (`stdev / |mean|`)
of the total scores. Aggregate median:
- `>= 0.05`: PASS — recommendations are meaningfully differentiated.
- `0.02..0.05`: WARN — weakly differentiated (near-tiebreaker order).
- `< 0.02`: flag toward REJECT — flat noise.

**Verdict:** REJECT when both axes fall in their reject bands; WARN
when either falls in its warn band; PASS otherwise.

### Usage

```bash
uv run scripts/rule_quality_gate.py --rule <rule_id>          # single
uv run scripts/rule_quality_gate.py --all-declarative         # all rules_seed.json rules
uv run scripts/rule_quality_gate.py --all                     # + RULE_GATES rules
uv run scripts/rule_quality_gate.py --all-declarative --sample 20  # fast mode
```

Exit codes: `0 = all PASS`, `1 = at least one WARN`, `2 = at least
one REJECT`.

### Verification the gate catches Ward:2

Temporarily re-adding `ward_2_tribal` to the `rules` table produces:

```
ward_2_tribal    85 targets    cov=2.0  cv=0.189  WARN
    - median pre-existing coverage 2.0 < 3 (targets are thinly covered — rule may dominate)
```

Not a hard REJECT (CV is high because the gate also fires on some
well-covered commanders; the Aboleth-Spawn-class tail is masked by
the median). But the WARN is sufficient — a reviewer sees the signal
and asks "is this vacuum-filling intentional?" That conversation
would have saved the commit.

Tighter threshold schemes (e.g., report fraction of targets below CV
threshold rather than median) are possible and worth revisiting once
there's enough gate-output history to calibrate.

## Why This Matters

The combination of "test passes + golden-set Δ=0 + rule fires" is
**insufficient evidence** to ship a new rule. It only proves:

1. The rule is syntactically correct.
2. The rule doesn't regress anything inside the golden 100.
3. The rule emits *some* complements.

It does not prove:

1. The rule is mechanically meaningful.
2. The rule amplifies signal rather than filling a vacuum.
3. The rule's emissions have differentiated scores.

Long-tail rules must pass `rule_quality_gate.py` before commit in
addition to the existing gates. For rules that target commanders
*inside* the golden 100, the existing gates are sufficient — this
new check is additive, not a replacement.

## When to Apply

- **Required:** any new declarative rule in `data/rules_seed.json`
  before commit.
- **Required:** any new Python rule in `complement_rules/` before
  commit when its `RuleGate` predicate matches more than ~10
  commanders outside the golden 100.
- **Recommended:** periodically run `--all-declarative` as part of
  the scaffold→audit workflow to surface existing rules that drifted
  toward vacuum-filling as the rule set evolved.

## Examples

### Ward:2 (the failure case)

Before revert:

```
ward_2_tribal    85 targets    cov=2.0  cv=0.189  WARN
```

Target commanders like Aboleth Spawn, Armguard Familiar, Sleep-Cursed
Faerie have no other rules firing. The rule contributed 30 new cards
at uniform +0.16 to those commanders' pages. After revert (2026-04-24)
the declarative count returns to 20, and `test_declarative_set_size_matches_seed`
pins `"ward_2_tribal" not in DECLARATIVE_RULE_IDS` so it can't be
silently re-added.

### prowess_tribal (the healthy case)

```
prowess_tribal    76 targets    cov=4.0  cv=0.217  PASS
```

Target commanders have a median of 4 other rules firing (amplifying
signal rather than filling vacuum), and top-30 CV = 0.217 (well-
differentiated scores). Prowess is a genuinely shared strategic
mechanic — decks running Prowess creatures want more Prowess
creatures as a synergy.

### Full baseline audit (2026-04-24)

Running `--all-declarative --sample 20` across the 20 active
declarative rules produced 6 PASS, 14 WARN, 0 REJECT. The WARN list
flags thin pre-existing coverage for tribal / stax rules whose target
populations are naturally mechanically sparse. These aren't
necessarily bad — but they warrant explicit "is this vacuum-filling
intentional?" reasoning before the next similar rule lands.

**WARN rules flagged for follow-up review** (combined low-coverage
AND near-low-CV signature — same pathology family as Ward:2):

- `etbreplacement_copy_dbcopy_optional_tribal` (cov=1.0, cv=0.091)
- `etbreplacement_other_choosect_tribal` (cov=1.0, cv=0.090)

Neither is as bad as Ward:2 was (both just above CV threshold) but
they're the closest-to-reject rules in the current set. Filing for
investigation.

## Related

- `docs/solutions/best-practices/flag-gated-multi-port-rule-pattern-2026-04-23.md`
  — identity-clean + flag-gated contract for new rules.
- `docs/solutions/best-practices/rule-consolidation-null-result-2026-04-24.md`
  — a different angle on rule-set hygiene (pairwise redundancy).
- `scripts/_audit_rule_impact.py` — pre-existing per-rule audit,
  partially superseded but still used by `/rule-validate`.
- `memory/feedback_audit_every_change.md` — every scoring-path change
  must be audit-gated; this doc extends the contract to "audit must
  cover the rule's actual target population, not just the golden 100."
