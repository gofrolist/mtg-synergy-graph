---
last_updated: 2026-04-25
module: gap_report
title: Gap-report queue is dry on the current 100-cmdr evaluation surface — the next-rule-to-add pipeline has stalled until the golden set expands
tags: [gap-report, scaffold-rule, golden-set, audit, evaluation, workflow, queue-state]
problem_type: best_practice
symptoms:
  - every gap-report proposal that reaches the scaffolder gets reverted or marked trivial
  - bench.py audit --rule <new_id> reports "no tensor rows" or zero rank impact
  - --walk N drains zero attempts because all viable templates are BLOCKED in the attempt log
  - the next-rule-to-add automation feels like it's spinning without progress
resolution_type: workflow_state
applies_when:
  - You're considering scaffolding a new rule from the gap-report queue.
  - You've already pre-checked golden-set coverage per gap-report-impact-vs-golden-set-coverage-2026-04-25.md.
created: 2026-04-25
---

# Gap-report queue is dry on the current evaluation surface

The 2026-04-25 sweep across the entire `docs/gap_report.md` queue
found **zero proposals that are both untried and have measurable
golden-set signal**. The gap_report → scaffold → audit pipeline is
the project's primary mechanism for adding rules, but it can no
longer make forward progress against the current 100-commander
evaluation fixture.

## Empirical state of the queue (2026-04-25)

| Bucket | Count |
|---|---|
| Total entries shown in gap_report.md | 50 |
| With prior revert / template caution / already-shipped flag | 24 |
| With `needs_template` (no generator exists) | 19 |
| Untried with existing generator | **7** |
| ...with at least 1 golden-set hit | **1** (#39 keyword.CARDNAME) |
| ...with no prior trivial flag | **0** |

The 1 entry with golden coverage (#39, Lord Windgrace) was tried
2026-04-19 and marked trivial: "rule fires on 3 commanders but never
lands in top-30."

The 7 untried-with-generator entries (#15, #24, #25, #26, #35, #39,
#40) all involve archetypes the golden set doesn't cover:
damage-prevention voltron (Iroas/Frodo), Bushido tribal,
etbCounter:P1P1:X commanders, Partner:Character pairings.

## Structural diagnosis

`gap_report.py` ranks proposals by
`commanders_carrying_signature * (1 - covered_rate) * forge_signal`
— a survey of the *entire* card universe. The 100-cmdr golden set
is a *proxy* for general scoring quality, deliberately curated to
cover popular archetypes for `bench.py audit` to produce a
reproducible NDCG signal. The two are inherently misaligned: the
gap report finds gaps in the universe; the golden set is a fixed
subset of that universe.

When the golden set has captured most archetypes that align with
existing rule templates, the *remaining* gap-report proposals are
necessarily for archetypes the golden set was never designed to
cover. The queue is structurally exhausted, not a pipeline bug.

## Two real next steps

### A. Expand the golden set (long-term unlock)

Curate 20–30 new commanders from the gap-report exemplars whose
archetypes aren't currently represented:

- **Damage-prevention voltron** — Iroas, God of Victory; Frodo,
  Determined Hero; Rune-Tail, Kitsune Ascendant; Emmara Tandris;
  Rem Karolus, Stalwart Slayer (#15)
- **Bushido / Samurai tribal** — Isamaru, Hound of Konda; Toshiro
  Umezawa; Kyodai, Soul of Kamigawa (#25)
- **Counter-X-as-cost** — Ghave, Guru of Spores; Karlov of the
  Ghost Council (#3 — also unlocks #24, #26)
- **Wide-creature scaler** — Adeline, Resplendent Cathar; Ghave;
  Marath, Will of the Wild (#1)

Each new commander needs EDHREC-derived ground truth populated in
`data/edhrec.db`. After the expansion, the `bench.py audit` signal
returns and the gap_report queue refills with previously-untestable
proposals.

This is bigger work than a single session — needs a `ce-plan` cycle.
But it unlocks a year's worth of rule-addition runway.

### B. Pivot to existing-rule tuning (short-term gains)

Stop adding rules; retune the multipliers in
`data/scoring_weights.json` for measurable golden NDCG gains. Tools:

```bash
# Rank rules by aggregate contribution
uv run scripts/bench.py audit --collinearity

# Inspect a specific rule's top contributions
uv run scripts/bench.py audit --inspect <rule_id> --limit 20

# Sweep a multiplier value
# (manual: edit data/scoring_weights.json + run audit)
uv run scripts/bench.py audit --rule <rule_id>
```

Any value tuned in `scoring_weights.json` flips the hash, so re-pin
is required after each verdict. Per the established discipline:

1. Edit one value
2. `bench.py audit --rule <rule_id>` — verdict
3. If POSITIVE: update `_PRODUCTION_HASH` in
   `tests/test_scoring_weights.py` + `bench.py audit --repin --yes`
4. If NEUTRAL or HARMFUL: revert the value edit

This produces incremental gains (the recent edict_feeder/token_producer/
evasion sweeps in commits `b0266c4`, `d9f2826`, `10d8ac0` were all
this kind of work) but doesn't address the underlying queue drought.

## Anti-patterns

- **Cherry-picking the highest-impact gap entry without pre-checking
  golden coverage.** Wastes a session on a rule that can't be
  audited. See
  `docs/solutions/best-practices/gap-report-impact-vs-golden-set-coverage-2026-04-25.md`
  for the per-rule pre-check.
- **Running `--walk N --apply` without checking
  `--show-template-stats` first.** The walker will silently drain
  zero attempts. See section 6.6 of `docs/RULE_PLANNING.md`.
- **Filing more `--force` re-attempts on prior-reverted signatures
  without changing the template / multiplier / tier mix.** Same input
  produces the same output; the attempt log will block the second
  apply too.

## Re-check triggers

This finding was true on 2026-04-25 against the golden set captured
in `tests/fixtures/golden_set_run.json` (config_hash `6ef7f9d…`).
Re-run the sweep when any of the following changes:

- Golden set is expanded — new commanders added to the fixture.
- New generator template is added to `scripts/scaffold_rule.py`
  (e.g., `damage_prevention_voltron`, `damage_amp_doubler`).
- `gap_report.py`'s impact metric is reformulated to weight by
  golden-set overlap.
- A scoring-config change (multiplier tuning, new rule) shifts which
  commanders' ranking is sensitive to which rules.

The sweep itself:

```python
# (full script in this finding's commit history; one-liner is:)
import sqlite3, json, re
text = open('docs/gap_report.md').read()
entries = re.split(r'^### ', text, flags=re.M)[1:]
golden = [e['commander'] for e in json.load(open('tests/fixtures/golden_set_run.json'))['entries']]
conn = sqlite3.connect('data/synergy.db')
# For each entry, parse signature, query for golden hits, surface anything > 0
# AND not flagged reverted/trivial/cautioned/already-shipped/needs_template.
```

## Related

- `docs/solutions/best-practices/gap-report-impact-vs-golden-set-coverage-2026-04-25.md`
  — per-rule pre-check; this finding is the aggregate version.
- `docs/solutions/best-practices/scaffold-queue-generator-exhaustion-2026-04-24.md`
  — sibling failure mode; that one is about generator-catalog
  exhaustion, this one is about evaluation-surface exhaustion.
- `memory/feedback_audit_every_change.md` — the discipline that
  forces this finding (untestable changes are not shippable).
- `docs/RULE_PLANNING.md` §6.6 — workflow checkpoint where this
  finding belongs.
