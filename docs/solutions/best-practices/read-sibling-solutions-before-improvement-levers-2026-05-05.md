---
title: Read sibling solutions docs before running improvement levers
date: 2026-05-05
category: best-practices
module: development_workflow
problem_type: best_practice
component: development_workflow
severity: high
applies_when:
  - About to run a recommendation-quality improvement lever (per-rule weight optimizer, embedding contribution flip, walker rule-shipping, second-oracle probe, scoring-formula swap)
  - Considering re-running any audit-gated tool whose inputs (fixture, code, data, weights) have not materially changed since its last run
  - Starting a new ce-brainstorm whose subject overlaps an existing docs/solutions/best-practices/ doc
  - Planning a multi-outcome probe with revert risk on a tagged baseline
symptoms:
  - Compute and wall-clock spent re-discovering an outcome already documented in a sibling solutions doc
  - "What's next?" framing that bypasses the doc-search step and goes straight to tool invocation
  - Plan documents that cite no prior solutions docs in their preamble
related_components:
  - tooling
  - documentation
tags:
  - workflow
  - institutional-knowledge
  - audit-gated-probe
  - improvement-levers
  - prior-art
  - null-results
  - revert-discipline
---

# Read sibling solutions docs before running improvement levers

## Context

The repo accumulates `docs/solutions/best-practices/` entries every
time a probe, sweep, or rule-shipping experiment terminates — including
null results and DECLINE verdicts. These docs aren't decorative: they
encode preconditions ("re-sweep only when X, Y, or Z changes"), prior
measured deltas ("+0.0067 NDCG, below shipping threshold"), and explicit
"don't repeat this" instructions. (auto memory [claude]:
`feedback_audit_every_change.md` records the standing rule that every
scoring-path change is audit-gated; this lesson is a procedural
prerequisite to that rule — read sibling docs *before* the audit-gated
change so you're not re-running an audit whose answer is already
captured.)

But the natural agent reflex when handed an "improvement lever"
(optimizer, embedding sweep, walker rule-shipping, IDF reformulation)
is to *run the lever and see what happens*. Each lever costs real
compute (minutes to an hour) and real tokens (thousands per output
digest). Re-running a lever whose null result is already documented is
pure waste — and worse, it can reset the institutional memory ("we just
tried it, got nothing, moved on") even though nothing in the world
changed since the last run.

A 2026-05-04 session contained three back-to-back instances of exactly
that pattern within ~90 minutes:

| Lever | Wasted compute | Prior doc that predicted the outcome |
|---|---|---|
| Optimizer retune (4 sweeps with varied alpha/grid/seed) | ~20 min | `docs/solutions/best-practices/optimizer-fixture-size-2026-04-30.md` + prior `.audit/optimize_history.csv` evidence — the 500-cmdr fixture had already exposed that current weights were near-optimal |
| Embedding contribution flip (10-cell sweep on 500-cmdr) | ~45 min | `docs/solutions/best-practices/infrastructure-without-scoring-activation-2026-04-24.md` — explicitly recorded +0.0067 null and listed three preconditions for re-sweep, none of which had changed |
| Walker rule-shipping pass (`scaffold_rule.py --apply --walk N`) | ~10 min | `docs/solutions/best-practices/scaffold-queue-generator-exhaustion-2026-04-24.md` — explicit verbatim text: *"If [no inputs change], re-running the walker will return the same exhaustion. Don't."* |

In all three cases the prior doc not only predicted the outcome but
*explicitly told the agent not to re-run absent specific deltas* — and
in all three cases the agent re-ran anyway. The deltas the docs
required (new rule families, vocabulary expansion, fixture refresh,
dependency rebuild) had not occurred.

The user's first-person framing of the lesson, after the third
exhaustion: *"I should have read the existing institutional knowledge
BEFORE running each lever."*

## Guidance

**Before invoking any improvement lever, read the sibling docs in
`docs/solutions/best-practices/` for the touched module.**

Concretely, before running:

| Tool / change | Grep `docs/solutions/` for |
|---|---|
| `bench.py audit --optimize` (or any flag that triggers a multi-sweep) | `optimizer`, `weight`, `multiplier`, `fixture-size` |
| `sweep_embedding_weights.py` or flipping `_ENABLE_EMBEDDING_CONTRIBUTION` | `embedding`, `infrastructure-without-scoring`, `flag-gated` |
| `scaffold_rule.py --apply --walk` for an autonomous rule-shipping pass | `walker`, `scaffold`, `queue`, `gap-report` |
| Any reformulation of an IDF / weight / scoring formula | The formula name (`bm25`, `idf`, `log2`), adjacent rule families, `null-result` |
| Building or rebuilding any sidecar oracle (`forge_oracle.py build`, `build_embeddings.py`) | The oracle name + `hash`, `rebuild`, `version` |
| Re-pinning the audit baseline (`bench.py audit --repin --yes`) | `repin`, `pin`, `expect-identity` |

If a sibling doc exists, decide one of:

1. **Skip the lever** — the prior null result still applies (no
   upstream input has changed since the prior run). Cite the doc by
   path in the chat / commit message and move on.
2. **Re-run with a documented delta** — explicitly cite *what changed
   since the prior run* that justifies re-execution (new fixture, new
   rule family, new vocabulary expansion, new dependency version,
   etc.). Note the cited delta in the run output or commit message.
3. **Promote the prior doc to a hard gate** — if the doc says "don't
   re-run unless X" and X hasn't happened, the gate should be enforced
   in tooling: a precommit check, a flag in the script's `--help`, or
   an explicit refusal in the CLI's startup banner. Each manual re-run
   is evidence the gate is missing.

The check is fast (Grep on `docs/solutions/`, ~2 seconds) and pays for
itself the first time it prevents one wasted sweep.

### Why `ce-learnings-researcher` doesn't already cover this (session history)

`ce-learnings-researcher` IS dispatched today, but only by
`ce-brainstorm` and `ce-plan` for **forward planning of new
features** (verified across 3 prior session traces — see Session
History below). When the trigger is "let's run the lever again" or
"what's next?", no brainstorm is invoked, no learnings researcher
fires, and the prior docs go unread. The gap is not in the tools —
it's in the workflow trigger.

The fix is to apply the same `docs/solutions/` lookup discipline at
**lever-invocation time**, not just at feature-planning time.

## Why This Matters

Compound engineering's whole premise is that prior work — including
failed work — accrues value. A failed probe that was documented becomes
negative-cost guidance for the next agent. But that value is only
realized if the next agent **reads the doc before re-running the
experiment**.

The 2026-05-04 session burned an estimated **75 minutes of wall-clock
+ ~25k tokens** re-confirming three null results that were already
documented. None of the re-runs surfaced a new finding; all three
matched the prior docs' predictions to within their reported precision.

Beyond the direct compute cost, re-running null experiments creates
two compounding harms:

- **Reframe contamination**: the latest null measurement becomes "the
  current state" and the prior doc's measurement becomes "stale," even
  though nothing changed. (auto memory [claude]:
  `feedback_edhrec_not_goal.md` was updated this same session with a
  textually-scoped note acknowledging the BM25 reframe applied **only
  to BM25 work**, precisely to prevent this kind of leak — that
  scoping discipline is the consumption-side mirror of this
  production-side rule.)
- **Audit-history pollution**: each lever appends to
  `.audit/history.csv`, `.audit/optimize_history.csv`, etc. Repeated
  null entries dilute the signal-to-noise of the history when later
  agents try to read trends.

A precedent for this kind of "systemic blind spot" lesson exists in
`docs/solutions/best-practices/sweep-writers-not-just-readers-on-source-of-truth-refactor-2026-04-25.md`,
which explicitly notes that **the SAME blind spot already bit a prior
externalization three days earlier — systemic, not one-off**. The
lesson here generalizes one level higher: the writer/reader sweep
discipline applies to refactors; the docs-lookup discipline applies
to lever invocations. Both are about *checking what's already known
before acting*.

## When to Apply

Apply this rule **unconditionally** before any of the following:

- Running `bench.py audit --optimize` (or any flag that triggers a
  multi-sweep)
- Running `sweep_embedding_weights.py` or modifying
  `_ENABLE_EMBEDDING_CONTRIBUTION`
- Running `scaffold_rule.py --apply --walk` for an autonomous
  rule-shipping pass
- Modifying any IDF, weight, or scoring formula in
  `universal_scorer.py` or `complement_rules/`
- Building or rebuilding any sidecar oracle (`forge_oracle.py build`,
  `build_embeddings.py`)
- Re-pinning the audit baseline (`bench.py audit --repin --yes`)

The check is **not required** for:

- Pure-infrastructure refactors (where `bench.py audit --expect-identity`
  will hold by construction)
- Bug fixes responding to a fresh, named test failure
- Adding genuinely new rule kinds (no prior null result can apply to
  a rule family that didn't exist)

## Examples

### Anti-pattern (what happened in the 2026-05-04 session)

```
User: "Try the embedding flip."
Agent: [runs sweep_embedding_weights.py, 10 cells, ~45 min]
Agent: "Best cell w_emb=0.5, k=2.0 → +0.0089 hidden_gem_hit_rate, -0.0021 NDCG. DECLINE."
[Later]
User: "Wait, we already tried this in April."
Agent: [reads infrastructure-without-scoring-activation-2026-04-24.md]
Agent: "Yes — prior doc recorded +0.0067 null and listed three preconditions
for re-sweep, none of which have changed. The 45 minutes was wasted."
```

### Correct pattern

```
User: "Try the embedding flip."
Agent: [Grep docs/solutions/ for "embedding"]
Agent: "Found infrastructure-without-scoring-activation-2026-04-24.md.
It records a +0.0067 null result and says re-sweep is justified only if:
  (a) new card embeddings rebuilt against an updated cardsfolder, OR
  (b) new event_match_seed entries added since 2026-04-24, OR
  (c) the candidate-frequency distribution shifted by >10%.
Checking: cardsfolder unchanged, no new seed entries since 04-24,
no fixture refresh. None of (a)/(b)/(c) holds. Skipping the sweep.
Want me to flag the precondition in sweep_embedding_weights.py --help
so the next agent doesn't re-run either?"
```

The correct pattern costs ~30 seconds of Grep + reasoning and saves
~45 minutes of compute. Even when the doc *doesn't* prevent the re-run
(sometimes a delta does justify it), reading it improves the run: the
new measurement gets compared against the prior one, the cited delta
is logged, and the audit history becomes self-explanatory rather than
a string of unattributed numbers.

### Trigger phrases that should force the lookup

The 2026-05-04 session showed that certain user prompts bypassed the
lookup step and went straight to tool invocation. Future occurrences
of these (or close paraphrases) should *force* the
`docs/solutions/` Grep before any lever runs:

- "What's next for v1.x?" / "What's next to improve the recommendation model?"
- "Let's try the [optimizer | embedding flip | walker | …]."
- "What if we tweak the [IDF | multiplier | weight | …]?"
- "Re-run the [optimizer | sweep | walker]."
- "Try [BM25 | a different formula | a different fixture]."

These framings are the symptom; the underlying cause is the missing
docs-first discipline at lever-invocation time.

## What good looked like in the same session (contrast pair)

The same 2026-05-04 / 2026-05-05 session that exhibited the lever-exhaustion
anti-pattern *also* contained a clean execution under formal discipline:
the BM25 IDF probe. The contrast is instructive because it shows the
same agent + same user can do this right when the workflow forces it:

- `ce-brainstorm` → 2 doc-review passes + scope-stripping surgery
- `ce-plan` → 1 doc-review pass + 5 P1 refinements
- `ce-work` → 5 implementation units shipped cleanly
- 5 outcomes pre-committed (SHIP / INVESTIGATE / INCONCLUSIVE /
  INVESTIGATE-FOR-RETUNE / DECLINE) with explicit routing logic
- `pre-bm25-baseline` git tag pre-positioned for clean revert
- Per-commander prerequisite gate fired correctly (65/500 commanders
  regress >0.05 NDCG@30 → DECLINE)
- Recovery via `git reset --hard pre-bm25-baseline` + cherry-pick of
  Unit 1 (general-purpose audit infra preserved) — clean revert in
  ~5 minutes, `bench.py audit --expect-identity` PASSED post-recovery
- Memory update scoped textually ("for BM25-related work only") to
  prevent reframe contamination

The discipline difference: the BM25 arc started with `ce-brainstorm`,
which by protocol dispatches `ce-learnings-researcher` to scan
`docs/solutions/`. The lever runs started with raw "let's try X"
prompts that skipped the brainstorm and the lookup with it.

The full procedural pattern (pre-baseline tag + N-outcome routing +
bundled-but-separable units + scoped memory) is documented in
`docs/solutions/best-practices/bm25-idf-null-result-2026-05-04.md`
and the plan at `docs/plans/2026-05-04-001-feat-bm25-idf-probe-plan.md`.

## Session History (prior context)

(Section sourced from `ce-session-historian` search across 3 prior
Claude Code sessions on this repo, 2026-04-21 through 2026-05-01.)

All three predicting docs (`optimizer-fixture-size-2026-04-30.md`,
`infrastructure-without-scoring-activation-2026-04-24.md`,
`scaffold-queue-generator-exhaustion-2026-04-24.md`) were written by
`ce-compound` invocations at the END of the sessions that produced the
findings. The docs were discoverable from `CLAUDE.md` as of at least
2026-04-24, but the triggering sessions never instructed the agent to
read sibling docs *before* re-running a lever.

The Apr 21-24 session demonstrated the available discipline: before
**building** the content-embeddings feature, it dispatched a
`ce-learnings-researcher` sub-agent to "search `docs/solutions/` for
prior institutional learnings." This pre-planning lookup was applied
to *new feature design* but not to *re-running an existing lever*. The
distinction was never surfaced as a gap until the 2026-05-04 session
made the cost visible.

**No prior session contained an explicit statement like "I should
check `docs/solutions/` before re-running this lever."** This META
lesson is genuinely novel — prior sessions captured the individual
null-result findings but not the second-order pattern of "read
sibling docs before activating any lever." (session history)

## Related

- `docs/solutions/best-practices/scaffold-queue-generator-exhaustion-2026-04-24.md`
  — predicted the 2026-05-04 walker exhaustion (verbatim "Don't
  re-run" guidance).
- `docs/solutions/best-practices/infrastructure-without-scoring-activation-2026-04-24.md`
  — predicted the 2026-05-04 embedding flip null (with explicit
  re-sweep preconditions).
- `docs/solutions/best-practices/optimizer-fixture-size-2026-04-30.md`
  — established that current weights are near-optimal on the 500-cmdr
  fixture; predicted the 2026-05-04 optimizer near-zero deltas.
- `docs/solutions/best-practices/bm25-idf-null-result-2026-05-04.md`
  — case study of the contrast pattern: full audit-gated probe
  discipline, 5-outcome routing, clean revert via baseline tag.
- `docs/solutions/best-practices/sweep-writers-not-just-readers-on-source-of-truth-refactor-2026-04-25.md`
  — prior art for the "systemic blind spot" framing; the closest
  existing doc to a META "check what's already known before acting"
  rule.
- `docs/plans/2026-05-04-001-feat-bm25-idf-probe-plan.md` — the BM25
  probe's plan; reference for the audit-gated N-outcome probe pattern.
- `memory/feedback_audit_every_change.md` — every scoring-path change
  is audit-gated; this lesson is the procedural prerequisite to that
  rule.
- `memory/feedback_edhrec_not_goal.md` — updated 2026-05-04 with
  textually-scoped reframe; consumption-side mirror of this
  production-side rule.
