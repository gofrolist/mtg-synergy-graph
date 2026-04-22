---
date: 2026-04-21
topic: forge-second-oracle
seed: docs/ideation/2026-04-21-recommendation-model-ideation.md (Survivor 4)
status: draft (brainstorm 4 of 7)
depends_on:
  - 2026-04-21-unified-eval-harness-requirements.md (bench.py as the host CLI)
  - optional: 2026-04-21-typed-port-graph-requirements.md (typed substrate makes diff analysis cleaner, not required)
---

# Requirements: Forge BoosterDraftAI as Design-Time Oracle + Forge-Precon Co-Occurrence

## Problem Statement

The rule-authoring workflow today relies on `gap_report.py` to prioritize "which coverage gap to close next." The gap report ranks uncovered `(commander_port_shape, cand_event)` cells by `reach × (1 − covered_rate)`. That's a good volume heuristic, but it has no signal about which gaps actually *matter* for synergy — every uncovered cell counts equally.

Two Forge-internal signals that are categorically distinct from EDHREC popularity can answer "which gap matters most":

1. **BoosterDraftAI pair scoring** — Forge's own synergy heuristics, authored by game designers for the Booster-Draft AI. Hundreds of hand-coded rules evaluating card synergy. Using this as a *design-time oracle* gives a second opinion on "should rule X exist?" without touching the inference path.
2. **Forge precon deck co-occurrence** — hundreds of AI-playable precon decks ship in Forge's `cardsfolder`. Port-pair co-occurrence inside those decks, lineup-adjusted via RAPM-style math, is a designer-curated "these mechanics go together" signal uncontaminated by popularity.

Both signals are Forge-internal, Forge-DSL-adjacent, and zero-EDHREC by construction. They feed the rule-authoring workflow, not the scoring path.

## Goals

1. Port Forge's BoosterDraftAI pair-scoring heuristics to Python (minimal scope: pair scoring only — no full game-state AI).
2. Build a `forge_precon_ppmi` offline table of port-pair PPMI from Forge's bundled precon decks.
3. Integrate both into `gap_report.py` so "next rule to author" is prioritized by designer signal, not just coverage volume.
4. Add a `bench.py audit --vs-forge-oracle` sidecar report that shows where our scorer agrees / disagrees with BoosterDraftAI on the golden set — useful as a second verdict axis.
5. Preserve the invariant that inference-time scoring is purely our rule corpus; BoosterDraftAI is never consulted at query time.

## Non-Goals

- Running BoosterDraftAI at inference. The whole point is that it's a design-time oracle.
- Full port of Forge's draft AI (deck-building, archetype classification, mana-curve simulation). Minimal pair-scoring only.
- Using EDHREC at any stage in this brainstorm — the signals here are Forge-internal by design.
- Training anything. Both oracles produce static lookup tables from a one-time ingest.
- Auto-merging BoosterDraftAI-generated rule proposals. Every proposed rule goes through the existing scaffold → audit → human-review workflow.

## Users and Scenarios

| Scenario | Who | Expected experience |
|---|---|---|
| Prioritize next rule to author | Dev running `gap_report.py` | Report shows gaps ranked by `reach × (1 − covered_rate) × forge_oracle_score` where `forge_oracle_score` = designer-signal weight from BoosterDraftAI + precon PPMI. Gaps that are "big but Forge doesn't care" drop; gaps that are "small but Forge strongly signals" rise. |
| Verify a newly-authored rule | Dev after audit | `bench.py audit --vs-forge-oracle` reports NDCG delta + Forge-agreement delta. A rule that lifts NDCG but drops Forge-agreement meaningfully flags a CONTENTIOUS verdict for extra review. |
| Investigate a Forge-flagged port pair | Dev | `scripts/forge_oracle.py inspect <cmdr_port> <cand_port>` prints BoosterDraftAI score + precon PPMI + list of precons where the pair co-occurs. |
| Ingest a new Forge release | Forge-integrator | Re-run import; `scripts/forge_oracle.py rebuild` regenerates precon PPMI + re-runs BoosterDraftAI over known card pairs. Diff surfaces new signals. |

## Functional Requirements

### FR1 — BoosterDraftAI pair-scoring port

Identify the minimal Java method (candidate: `forge.deck.BoosterDraftAI.rateCard` or `forge.deck.CardSynergy.getSynergy` — exact symbol to be discovered in planning) that answers "given two cards, return a numeric synergy score." Port to Python at `src/mtg_synergy_graph/forge_oracle/pair_scorer.py`. Input: two card names (joined against local `cards` + `card_ports` tables); output: float score.

No game-state context. No deck-state context. No mana-curve simulation. If the Java method requires those, scope is out of bounds — mark the effort as "needs bigger port" and escalate to user before proceeding.

### FR2 — Forge precon PPMI table

One-time ingest of every precon deck shipped in `data/forge/res/decks/**` (or wherever Forge keeps them). For each deck, extract card-pair co-occurrence. For each `(port_signature_A, port_signature_B)` pair, compute PPMI with RAPM-style lineup adjustment (control for the rest of the deck so "mana rocks co-occur with everything" doesn't dominate). Store to `forge_precon_ppmi` SQLite table:

```
(port_signature_a TEXT, port_signature_b TEXT, ppmi FLOAT, decks_count INTEGER, last_updated TEXT)
```

Port signature format: same canonical form used by the rest of the scorer. If Survivor 2 lands first, use the canonical node kind; otherwise use the raw `(port_type, event_class, valid_filter_key)` tuple.

### FR3 — `gap_report.py` re-ranking

Extend the gap-report pipeline to weight each uncovered cell by:

```
gap_score = reach × (1 − covered_rate) × forge_oracle_weight
```

where `forge_oracle_weight = max(normalized_boosterdraft_signal, normalized_precon_ppmi)` if either signal exists; `1.0` fallback if neither. Output format stays Markdown; new column `forge_signal` shows which oracle(s) fired.

### FR4 — `bench.py audit --vs-forge-oracle` sidecar

Add a subcommand to Survivor 1's CLI that, for each commander in the golden set, computes:
- Our top-30 rank for each card the commander cares about.
- BoosterDraftAI's top-30 rank over the same candidate pool (using the ported pair scorer as a pairwise-to-ranking aggregator).
- **Forge agreement score** = Kendall-τ between the two rankings, averaged across commanders.

Report includes:
- Aggregate Forge-agreement number, its delta vs pinned baseline.
- Per-commander Forge-agreement delta, sorted by magnitude.
- Top 10 (commander, candidate) pairs where Forge says "high synergy" but our scorer says "low" — each is a candidate rule proposal.

### FR5 — Rule-proposal report

New subcommand `scripts/forge_oracle.py propose-rules --top 20` reads the current gap report + Forge signals and emits 20 proposed rule scaffolds ranked by expected lift. Each proposal includes:
- The `(commander_port_shape, cand_event)` gap.
- Sample commanders that would benefit.
- Sample candidate cards Forge flags as synergistic.
- A scaffold template (one of the generators registered in `scripts/scaffold_rule.py`) pre-filled with the gap's port shape.

Humans accept, reject, or edit before running `_audit_rule_impact.py`.

### FR6 — Forge-version pinning

BoosterDraftAI behavior can change across Forge releases. Pin a specific Forge commit SHA in `data/forge/` and record it in `forge_oracle/version.txt`. The ported pair scorer must produce identical output against this pinned version. `scripts/forge_oracle.py upgrade` regenerates against a new Forge version and emits a diff report.

## Success Criteria

1. **Rule discovery.** At least one rule authored during the first month after landing this survivor was prioritized by the Forge oracle and subsequently passed the NDCG audit. Demonstrates the discovery loop closes.
2. **Gap-report utility.** After re-ranking, the top-10 gaps in `gap_report.py` are qualitatively different from the pre-change top-10 on at least 5 entries. Humans find the new top-10 more actionable (informal assessment; no metric).
3. **Forge-agreement stability.** Aggregate Forge-agreement number is logged by `bench.py audit --vs-forge-oracle`. During normal rule-authoring, agreement should trend up or stay flat. A sudden agreement drop flags a potential scoring regression not caught by NDCG alone.
4. **Scoring invariant.** Inference-time `recommend.py` output is bitwise-identical before and after landing this survivor. Verified via Survivor 1 FR7 `--expect-identity`. If any scoring code path reads from `forge_precon_ppmi` or `forge_oracle/` at inference, it is a bug.
5. **Port scope discipline.** The ported Java code at `src/mtg_synergy_graph/forge_oracle/pair_scorer.py` stays under 500 LOC. If the port grows beyond that, escalate — the minimal scope has been breached.

## Constraints

- Java port uses no JVM at runtime. Port the algorithm, not the runtime.
- Zero runtime dependency on Forge from our inference path. `forge_oracle/` is imported only by offline scripts.
- Precon deck parser lives alongside `import_cardsfolder.py` — reuse extractor infrastructure.
- `forge_precon_ppmi` table size estimate: ~10 k distinct port signatures squared × sparsity ~0.5% = ~500 k rows. Fits in SQLite trivially.
- The oracle's output is always advisory. A rule can be accepted on pure NDCG grounds even if Forge disagrees.

## Risks

- **Java port scope blowup.** If `BoosterDraftAI.rateCard` turns out to require `GameState`, `PlayerAI`, or card-rules-engine context, the minimal-port scope is invalid. Mitigation: spike the port first, get a ≤ 2-day time-box; escalate if it overshoots.
- **Precon corpus too small.** Hundreds of decks may not be enough for statistically stable PPMI on rare port pairs. Mitigation: Laplace smoothing; require `decks_count ≥ 3` before a pair's PPMI is non-null; the rest fall back to the BoosterDraftAI signal.
- **Oracle overfitting to Forge's designer biases.** Forge's own AI can be wrong or quirky (e.g., undervalues modern archetype tech it never sees). Mitigation: the oracle never auto-accepts — every proposal goes through audit.

## Open Questions (For Planning Phase)

- Exact Forge symbol to port — `CardSynergy.getSynergy`? `BoosterDraftAI.rateCard`? `DeckgenUtil`? Needs a spike.
- Whether to version the Forge data as a git submodule or snapshot-copy.
- Deck parser format — Forge decks use `.dck` files; parser may or may not already exist in our codebase.
- Handling of double-faced / adventure / split cards in pair counts — treat as one entity or as separate pairs?
- Whether `forge_precon_ppmi` merges with Scryfall-tagger `otag:*` signals (stretch goal; separate brainstorm if pursued).

## Out of Scope for This Brainstorm

- Scryfall Tagger `otag:*` ingest (adjacent idea, cut from ideation; revisit later).
- Running BoosterDraftAI at inference.
- Full Forge draft-AI port.
- PPMI on EDHREC decklists (explicitly rejected in ideation — popularity leak).

## Related

- Seed idea: `docs/ideation/2026-04-21-recommendation-model-ideation.md` Survivor 4.
- Memory: `memory/reference_forge_java_engine.md` — Forge Java engine has 775 files at `data/forge/`, 202 ApiTypes, 80+ TriggerTypes.
- Memory alignment: `memory/feedback_forge_data_direct.md` — use Forge data directly, no indirection layers.
- Memory alignment: `memory/feedback_edhrec_not_goal.md` — finding hidden gems from mechanics; Forge oracle is a mechanics-side proxy.
- Integration: `2026-04-21-unified-eval-harness-requirements.md` hosts the `--vs-forge-oracle` sidecar.
- Future: `2026-04-21-typed-port-graph-requirements.md` — once canonical nodes exist, port signatures get cleaner and PPMI becomes denser (same pair signatures across more cards).
