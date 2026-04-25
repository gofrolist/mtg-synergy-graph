---
last_updated: 2026-04-25
module: gap_report
title: Gap-report impact does not correlate with golden-set evaluability — pre-check coverage before investing in a new generator template
tags: [gap-report, scaffold-rule, golden-set, audit, evaluation, workflow]
problem_type: best_practice
symptoms:
  - new generator template scaffolded successfully but produces zero per-rule audit signal
  - bench.py audit --rule <new_id> reports "no tensor rows" after re-pin
  - gap_report ranks the proposal by raw "commanders × forge_signal" but the commanders never appear in the 100-cmdr golden fixture
resolution_type: workflow_check
applies_when:
  - Adding a new generator template to scripts/scaffold_rule.py for a previously-unsupported gap proposal.
  - The proposal scores high on impact (≥10) but the exemplar commanders are not blue-chip EDHREC top-100 picks.
created: 2026-04-25
---

# Pre-check golden-set coverage before investing in a new generator template

`gap_report.py`'s impact metric is `commanders_carrying_signature *
(1 - covered_rate) * forge_signal`. It ranks the *universe* of
commanders, not the project's *evaluation surface* (the 100-cmdr
golden set in `tests/fixtures/golden_set_run.json`). When these
diverge, a high-impact proposal can be completely untestable.

## How this manifests

The 2026-04-25 attempt at `damage_prevention_voltron` (gap report #15,
impact 12.2):

- Gap report claimed reach of 10 commanders carrying
  `replacement.DamageDone[Prevent]` (Iroas, Frodo, Rune-Tail,
  Emmara, Rem Karolus, Diamond Weapon, Goldbug, Losheel, Emmara
  Tandris, Diamond Weapon).
- Built a new generator (~250 lines), scaffolded the rule, ran
  `bench.py audit --repin --yes` to populate the tensor.
- `bench.py audit --rule damage_prevention_payoff` returned
  `no tensor rows`.
- Direct check: of the 100 golden commanders, **zero** carry the
  prevention gate. The exemplar commanders are popular EDH commanders
  but none are in the project's evaluation fixture.
- Net: the rule was untestable on the project's audit infrastructure
  and shipping it would have violated `memory/feedback_audit_every_change.md`
  ("Every scoring-path change gated by NDCG audit; no exceptions
  except pure-infra refactors"). Reverted entirely.

## Pre-check before scaffolding

Before writing a new generator function, verify the proposal's gate
fires on at least one golden commander. One-liner against the live DB:

```python
import sqlite3, json
golden = [e['commander'] for e in json.load(open('tests/fixtures/golden_set_run.json'))['entries']]
conn = sqlite3.connect('data/synergy.db')
conn.row_factory = sqlite3.Row
hits = []
for cmdr in golden:
    rows = conn.execute(
        "SELECT raw_line FROM card_ports WHERE card_name=? AND <gate-shape>",
        (cmdr,)
    ).fetchall()
    if any(<gate-condition>(r) for r in rows):
        hits.append(cmdr)
print(f'golden cmdrs hitting the gate: {len(hits)}')
```

If `hits == 0`: the gap proposal is structurally valid but
**untestable on the current evaluation surface**. Two paths forward:

1. **Defer the rule.** Don't write the generator; flag the proposal
   as "untestable" in the attempt log so future runs skip it. Cheaper
   alternative: extend the golden set to cover the archetype (separate
   ce-plan cycle).
2. **Pivot to a clean candidate that DOES hit the golden set.** The
   gap report's table is sorted by raw impact, not by golden-set
   coverage. Walk the table looking for the first proposal where
   the pre-check returns ≥1 hit. Lower nominal impact, but actually
   measurable.

## Why this gap exists

`gap_report.py` is intentionally evaluation-agnostic — it surveys the
*entire* card/commander universe to surface real coverage gaps, not
just gaps that affect the chosen evaluation set. That's correct: the
golden set is a *proxy* for general scoring quality, not a complete
specification of where the scorer matters. But it means the impact
metric over-promises: "10 commanders carry this signature" is
tantalizing but useless if `bench.py audit` has no observable signal.

A future enhancement: add a `golden_coverage` column to `gap_report.md`
showing how many of the 100 golden commanders carry each signature,
and demote proposals where the count is 0 to a separate
"untestable" section. Until then, the pre-check is manual.

## Anti-patterns

- **Trusting the impact column.** Impact 12.2 with 0 golden coverage
  is worse than impact 7 with 5 golden coverage — the latter is
  measurable and shippable.
- **Building the generator first, then checking coverage.** Wasted
  ~1-2 hours on a generator that became dead code. Pre-check is one
  SQL query.
- **"This commander is in EDHREC top 100, surely it's in our golden
  set."** No. The golden set's 100 commanders are project-specific
  picks; popular EDH commanders like Iroas may not be included.

## Related

- `memory/feedback_audit_every_change.md` — the discipline this
  finding upholds (every scoring-path change must be NDCG-audit-gated).
- `docs/RULE_PLANNING.md` — the gap_report → scaffold → audit
  workflow this finding adds a pre-check to.
- `docs/solutions/best-practices/rule-quality-gates-2026-04-24.md`
  — the post-scaffold quality gates (Gate A/B/C) that this pre-check
  complements.
