# Outlet-Direction Death-Payoff Cohort + rank_bonus-Ablated NDCG Sidecar Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**status:** part A shipped (Task 1); part B declined at pre-registered gates
**plan id:** 2026-07-07-002
**evidence base:** `.superpowers/sdd/diagnostic-forensics-cohorts.md` (cohort hunt, 2026-07-07), `.superpowers/sdd/diagnostic-head-flatness.md` (rank_bonus finding, 2026-07-07), plan 2026-07-07-001 (the subtype-supply playbook this cycle repeats)

**Goal:** (A) Add a read-side rank_bonus-ablated NDCG@30 sidecar so every future SHIP/DECLINE verdict separates mechanical signal from the hidden EDHREC-at-inference credit (−0.0441 measured), and correct CLAUDE.md's false "EDHREC-clean inference" claim; then (B) run the cohort playbook on the outlet-direction death-payoff cohort — a new `death_outlet_feeder` rule connecting sacrifice-OUTLET candidates (`cost.sacrifice` ports: Viscera Seer, Ashnod's Altar, Carrion Feeder, …) to commanders whose death trigger is `ChangesZone`-shaped (Meren, Marchesa, Judith, Titania, Gitrog), a verified zero-coverage gap of 227 miss-rows.

**Architecture:** Part A is read-side only: a forensics-layer ablation that subtracts the formula-derived `rank_bonus` from captured candidate totals and recomputes NDCG (no scoring-path change), plus honest documentation. Part B is a new **formal ComplementRule** (`death_outlet_feeder`) with a zone-shape-gated EventCheck — NOT a seed edit: `ChangesZone ∉ _SCOPED_TRIGGER_EVENTS` (`complement_rules/core.py:157`), so a bare `{"sacrifice": "ChangesZone"}` seed row would compile to an unconditional `_always` check and false-feed every ETB/ChangesZone-anywhere commander. The rule reuses `death_payoff.is_death_event` for the gate, gets its own cohort predicate + separate fixture (the existing archetype-payoff fixture's `_COHORT_PREDICATES` union is deliberately NOT extended — that would mutate the pinned fixture's membership and break `TestCohortUnchanged`), its own whitelist comparator, and pre-pinned gates. Flag-gated default-OFF with the full config-hash choreography from plan 2026-07-07-001, including the post-review lesson: the ship commit registers the flag in `ScoringConfigInputs` with a hash-flip test.

**Tech Stack:** Python 3 / sqlite3, existing complement-rule machinery (formal `ComplementRule` + `EventCheck` pairs, IDF via `(rule_id, cmdr_event, cand_event, filter_group)`), `death_payoff.py` helpers, standing instruments (`context_sim.py` bands + whitelist assembly, `bench.py audit`, forensics).

## Global Constraints

Binding facts and numbers — every task's requirements implicitly include this section.

**Verified mechanics (from the 2026-07-07 diagnostics + explorer, with file:line):**
- `rank_bonus = 0.005 * max(0.0, 1.0 - edhrec_rank / 30000.0)` (NULL rank → sentinel 99999 → ≈0.0 bonus... verify: 99999 > 30000 → max(0,negative)=0.0), set at `universal_scorer.py:1114` (scored) and `:1136` (staple-only), summed into `UniversalScore.score` and `to_legacy_buckets`. Range [0.0, 0.005].
- Ablating it (subtract + re-sort `(-adj_total, cmc, name)`) drops golden-100 mean NDCG@30 **0.2328 → 0.1887 (−0.0441, −19%)**. The existing `--ablate-tiebreak` (`bench/forensics.py:97-118`, `compute_tiebreak_ablation`) ablates only the SORT-KEY tiebreaker — a different EDHREC use; do not conflate them.
- `COST_FEEDS_TRIGGER` seed rows are bare `{cost_event, trigger_event}` string pairs; the check callable is chosen in `_invert_cost_feeds` (`complement_rules/core.py:216-235`) — `_SCOPED_TRIGGER_EVENTS` = {Sacrificed, Discarded, Drawn, LifeLost, LifeGained} get scoped checks, everything else gets `_always`. There is a second `rule_id="cost_feeds_trigger"` emission at `combat.py:441` (Sac<Creature> recursion) — the new rule must NOT touch either path.
- Cost-port matches get `filter_group` enrichment (`free_outlet`/`paid_outlet`/`self_sac`) via `_cost_filter_group` (`core.py:1529/1555/1583`) — a formal rule over cost ports inherits per-group IDF automatically.
- Zone-shape-aware EventCheck precedent: `_changezone_resonance_check` (`core.py:525-554`).

**The cohort (from the hunt, verified by SQL + live tensor):** legal legendary-creature commanders with a death-event trigger that is `ChangesZone`/`ChangesZoneAll`-shaped (battlefield→graveyard per `death_payoff.is_death_event`), non-self-only (`graph_engine._trigger_only_matches_self`), with NO explicit `Sacrificed`/`SacrificedOnce` trigger port (those 22 are partly covered by the existing `cost_feeds_trigger` arm), and NOT in the shipped `subtype_death_payoff` cohort (36). Count at the current data snapshot: **135**; golden-100 members: **Judith, the Scourge Diva; Marchesa, the Black Rose; Meren of Clan Nel Toth; Titania, Protector of Argoth; The Gitrog Monster** (5); golden-500: 22; EDHREC-labeled: 126/135. Supply side: **1,923** legal cards with a `cost.sacrifice` port. Confirmed zero-credit examples: Judith×Viscera Seer, Marchesa×Ashnod's Altar, Meren×Carrion Feeder (all OUTRANKED/staple_only = zero tensor rows). **[CORRECTED 2026-07-08, PR #103 review — this premise was wrong; see the CORRECTION block near the top of `docs/solutions/best-practices/death-outlet-feeder-null-result-2026-07-07.md` for the actual facts (these pairs already earn `cost_feeds_trigger` credit via `combat.py`; the "zero tensor rows" reading was tensor blindness from the pinned fixture's limited coverage, not zero rule credit).]**

**Gates (Part B; measured at the chosen operating point after tuning):**
- **O-noise:** outlet-cohort page-based mean ΔNDCG ≥ the fixture's own noise half-width **H_outlet, pinned in Task 4 BEFORE any mechanism work**. A delta below H_outlet is noise.
- **O-whitelist:** beat the outlet whitelist comparator (Task 5, measured BEFORE the rule ships) at matched side-effect budget: rule ΔNDCG ≥ the best whitelist cell whose cliffs ≤ 1, AND rule gem Δ within the outlet fixture's gem noise band, AND rule cliffs ≤ 1. **PARTIAL** (escalate to human, do not ship or decline unilaterally): all side-effect gates pass and ΔNDCG ∈ [H_outlet, best-whitelist-cell); this band is pre-registered NOW, before measurement.
- **O-500:** golden-500 ΔNDCG ≥ **−0.0136** AND gem Δ ≥ **−0.0235** (standing bands; baselines mean 0.1531 / 0.8189, `.audit/context_sim/PINNED_GATES.md`).
- **O-100:** `bench.py audit` verdict non-NEGATIVE; no hidden-gem stderr warning.
- **O-quality:** `rule_quality_gate.py --rule death_outlet_feeder` PASS (this rule is port-gated formal — the stock CLI works, unlike CARD_LEVEL_RULES); `--collinearity`: no pair where the new rule's VIF > 5 AND |r| > 0.8 — check `cost_feeds_trigger`, `edict_feeder`, `sacrifice_cluster`, `dies_drain`, `graveyard_sac_value`, `gy_fuel_feeder` explicitly; record ACTUAL numbers at the FINAL operating point (lesson from PR #102 F2: no carryover across weight changes — re-measure after any repin).
- **O-clean (new, first consumer of Part A):** the rank_bonus-ablated NDCG sidecar must be reported in the gate table for cohort + golden-100, and the ablated cohort delta must be ≥ H_outlet too — a rule that only reshuffles EDHREC micro-credit is not a win.

**Measurement-path discipline:** whitelist bars and rule deltas are measured through the SAME page-based path (`context_sim.py bands` / whitelist assembly) against baselines recorded in Task 4. The `--per-commander-ndcg` reporter is a sidecar only.

**Config-hash choreography (per plan 2026-07-07-001 + PR #102 review lesson):** Tasks 1–6 are hash-neutral (Task 6's wiring commit: flag False, no weights entry, no ScoringConfigInputs field; `bench.py audit --expect-identity` must PASS). Task 7 measures with uncommitted working-tree flips. Task 8's SHIP commit flips the flag True AND registers it in `ScoringConfigInputs` AND adds the hash-flip test AND adds the weights entry AND re-pins all four fixtures in ONE commit. The DECLINE path reverts to the hash-neutral state.

**Part A honesty decision (pre-made, record as such):** `rank_bonus` is KEPT in the score for now — removing −0.0441 of NDCG is a separate, measured decision for a future cycle. Part A's deliverable is measurement + honest documentation, not removal.

**Repo conventions:** `uv run` everything; tests never pass repo-relative DB paths (in-memory/tmp_path; live-DB tests use `open_db(..., create=False)` + skipif); commit trailers (the two standard lines); `graphify update .` after code changes; `.audit/` is gitignored; new fixture registered in `tests/bench/test_fixture_freshness.py:31-38` `_COMMITTED_GOLDEN_FIXTURES`.

---

## Task 1: rank_bonus-ablated NDCG sidecar + honest documentation (Part A, read-side)

**Files:**
- Modify: `src/mtg_synergy_graph/bench/forensics.py` (new `compute_rank_bonus_ablation` beside `compute_tiebreak_ablation`; wire into the forensics report + `--ablate-tiebreak` companion output)
- Modify: `src/mtg_synergy_graph/bench/cli.py` (surface the sidecar line in forensics md/json output; no new flag needed — compute it whenever `--forensics` runs, it is cheap arithmetic over already-captured candidates)
- Modify: `CLAUDE.md` (honesty correction)
- Test: `tests/bench/test_rank_bonus_ablation.py`

**Interfaces:**
- Produces: `compute_rank_bonus_ablation(ranked: Sequence[RankedCandidate], labels, ...) -> RankBonusAblation` (dataclass: `ndcg_raw`, `ndcg_ablated`, `delta`) — reuse the exact NDCG helper and label plumbing `compute_tiebreak_ablation` uses; read that function first and mirror its signature/style.
- The ablation math: for each captured candidate, `adj_total = total - 0.005 * max(0.0, 1.0 - min(edhrec_rank, 99999) / 30000.0)` where `edhrec_rank` falls back to the same sentinel the scorer uses (99999 → bonus 0.0); re-sort by `(-adj_total, cmc, name)` (note: `edhrec_rank` deliberately REMOVED from the ablated sort key — this sidecar measures total EDHREC-at-inference credit, in-score + in-sort, matching the diagnostic's −0.0441 methodology); NDCG@30 on the re-sorted list.

- [ ] **Step 1: Write the failing test** — synthetic `RankedCandidate` lists (read `forensics.py` for the exact tuple/dataclass shape first) where (a) two candidates with equal rule totals are ordered by rank_bonus in the raw list and flip under ablation, (b) a candidate with `edhrec_rank` at the sentinel gets bonus 0.0 (ablation is a no-op for it), (c) `ndcg_raw` equals the value computed by the existing forensics NDCG helper on the same input. Run: `uv run pytest tests/bench/test_rank_bonus_ablation.py -v` → expect ImportError/AttributeError.
- [ ] **Step 2: Implement** `compute_rank_bonus_ablation` in `forensics.py`, mirroring `compute_tiebreak_ablation`'s structure; add its result to the forensics report rendering (one md line: `rank_bonus-ablated NDCG@30: X.XXXX (raw Y.YYYY, delta −Z.ZZZZ) — EDHREC-at-inference credit`) and to the JSON output. Run the test → PASS.
- [ ] **Step 3: Live smoke** — `uv run scripts/bench.py audit --forensics` (long timeout) and confirm the new line appears with ndcg_ablated ≈ 0.189 (±0.01; record the exact value). Zero scoring-path change: this reads captured candidates only.
- [ ] **Step 4: CLAUDE.md honesty correction.** In the Project Overview / scoring sections, wherever the EDHREC-clean-inference claim appears (grep `EDHREC` in CLAUDE.md; the Scoring Architecture section says "No EDHREC at inference"), correct it to state: mechanical rule scoring uses no EDHREC, BUT a `rank_bonus` micro-term (`0.005·(1−edhrec_rank/30000)`, `universal_scorer.py`) and the sort-key tiebreak inject EDHREC ordering at inference; measured total credit −0.0441 NDCG@30 (2026-07-07 diagnostic); the forensics sidecar reports the ablated metric; kept deliberately pending a future measured removal decision. Also add the sidecar to the forensics command comment block.
- [ ] **Step 5: Full suite + commit.** `uv run pytest tests/ -q`; `graphify update .`; commit: `feat(bench): rank_bonus-ablated NDCG sidecar + EDHREC-at-inference honesty (plan 2026-07-07-002 Task 1)`.

---

## Task 2: `outlet_direction_death_payoff` cohort predicate

**Files:**
- Modify: `src/mtg_synergy_graph/bench/cohorts.py` (new standalone predicate — NOT appended to `_COHORT_PREDICATES`; add a comment explaining why: appending would mutate the pinned archetype-payoff fixture's membership and break `TestCohortUnchanged` + its bands)
- Test: `tests/bench/test_outlet_cohort.py`

**Interfaces:**
- Produces: `outlet_direction_death_payoff(conn) -> set[str]`. Membership = legal legendary creature AND has ≥1 trigger port where `is_death_event(...)` is True with `event_class ∈ {"ChangesZone","ChangesZoneAll"}` (i.e. the ChangesZone-shaped arm specifically) AND that trigger is not self-only (`graph_engine._trigger_only_matches_self(valid_filter)` False) AND the commander has NO trigger port with `event_class ∈ {"Sacrificed","SacrificedOnce"}` AND name ∉ `subtype_death_payoff(conn)`.

- [ ] **Step 1: Failing tests** (in-memory `_make_db` with cards + card_ports, following `tests/test_death_payoff.py`'s helpers): membership case (ChangesZone battlefield→graveyard trigger, non-self filter → in); exclusion cases: ETB-shaped trigger (dest Battlefield) → out; self-only filter (`Card.Self`) → out; commander with an explicit `Sacrificed` trigger → out; commander in the subtype cohort (death trigger whose filter names a token-producible subtype AND matching `port_attributes` vocab row) → out; non-legendary → out. Run → fail.
- [ ] **Step 2: Implement** the predicate (SQL scan shaped like `subtype_death_payoff`, lines 119-128 of cohorts.py, then Python filtering with the imported helpers). Run → pass.
- [ ] **Step 3: Live anchors test** (skipif on `data/synergy.db`): assert all five anchors ∈ cohort (`Judith, the Scourge Diva`, `Marchesa, the Black Rose`, `Meren of Clan Nel Toth`, `Titania, Protector of Argoth`, `The Gitrog Monster`); assert `Slimefoot, the Stowaway` ∉ (subtype cohort) — note Wilhelt is subtype-cohort so also ∉; assert 120 ≤ len(cohort) ≤ 150 (data-snapshot tolerance around the measured 135). If an anchor fails membership, STOP and report — the diagnostic's port-shape evidence needs re-verification, do not loosen the predicate to force the test green.
- [ ] **Step 4: Full suite + commit:** `feat(bench): outlet-direction death-payoff cohort predicate (plan 2026-07-07-002 Task 2)`.

---

## Task 3: Outlet cohort fixture + freshness gate

**Files:**
- Modify: `scripts/bootstrap_archetype_payoff_fixture.py` — parameterize: `_select_cohort_commanders(cohort_fn)` and `main(cohort_fn, output_path)` (defaults preserve current behavior exactly), then a thin new entry `scripts/bootstrap_outlet_payoff_fixture.py` calling it with `outlet_direction_death_payoff` → `tests/fixtures/golden_set_outlet_payoff.json`. `config_hash` stamping and `cohort_members` snapshotting come free via the existing `build_and_write_fixture` path.
- Modify: `tests/bench/test_fixture_freshness.py:31-38` — append the new filename to `_COMMITTED_GOLDEN_FIXTURES`.
- Test: extend `tests/bench/test_outlet_cohort.py` — fixture loads, carries `config_hash` + `cohort_members`, members ⊆ live predicate output (skipif live DB).

- [ ] **Step 1:** Parameterize the bootstrap (failing test: import the new entry point). Verify the EXISTING fixture is untouched: `git diff --stat` must not show `golden_set_archetype_payoff.json`, and `uv run pytest tests/test_death_payoff.py tests/bench/test_fixture_freshness.py -q` stays green BEFORE the new fixture is added.
- [ ] **Step 2:** Build the fixture: `uv run python scripts/bootstrap_outlet_payoff_fixture.py` (long timeout; expect ~126-member cohort filtered to the EDHREC-High-Synergy buildable subset — record the buildable count; the subtype fixture's analog was 36→33).
- [ ] **Step 3:** Register in `_COMMITTED_GOLDEN_FIXTURES`; full suite; commit fixture + code: `feat(bench): outlet-payoff cohort fixture + parameterized bootstrap (plan 2026-07-07-002 Task 3)`.

---

## Task 4: Pin the outlet noise bands (BEFORE any mechanism work — kill-test discipline)

- [ ] **Step 1:** `uv run python scripts/context_sim.py bands --fixture tests/fixtures/golden_set_outlet_payoff.json --output-dir .audit/outlet_cohort/bands` (long timeout). Record from bands.json: NDCG mean, half-width (**H_outlet**), gem mean, gem half-width, per-commander values.
- [ ] **Step 2:** Write `.audit/outlet_cohort/PINNED_GATES.md` in the format of `.audit/context_sim/PINNED_GATES.md`: the outlet bands + the standing golden-500 bands + the O-gates from Global Constraints with H_outlet filled in. This file is the pre-registration record; every later gate evaluation cites it.
- [ ] **Step 3:** Report the numbers (no commit needed for gitignored .audit; commit nothing).

---

## Task 5: Outlet whitelist comparator (the bar, measured BEFORE the rule)

**Files:**
- Modify: `src/mtg_synergy_graph/bench/context_sim.py` — new `outlet_whitelist_scores(conn, commander) -> dict[str, float]`: returns {} unless the commander ∈ `outlet_direction_death_payoff` membership shape (gate via the commander's own ports: load its trigger ports, apply the same is_death_event-ChangesZone / non-self / no-Sacrificed logic — reuse the predicate's port-level helper if Task 2 factored one, else compose from `death_payoff` + `_trigger_only_matches_self`); otherwise flat 1.0 for every card from `SELECT DISTINCT card_name FROM card_ports WHERE port_type='cost' AND event_class='sacrifice'`, commander excluded. Add a `whitelist_fn` parameter to `run_whitelist_baseline` (default = existing `whitelist_scores`, preserving current behavior) and a `--whitelist-baseline-outlet` CLI arm (or `--whitelist-fn outlet` selector — match the existing CLI style).
- Test: extend `tests/bench/test_context_sim.py` — outlet_whitelist_scores returns {} for a non-cohort commander, returns the sac-outlet set flat-1.0 for a cohort-shaped one (in-memory DB), commander self-excluded.

- [ ] **Step 1:** Failing tests → implement → pass. `assemble_whitelist`/`_rank_top30`/B_GRID are reused untouched (they are whitelist-agnostic, `context_sim.py:197-210, 403-463`).
- [ ] **Step 2:** Run the baseline: `uv run python scripts/context_sim.py sweep --fixture tests/fixtures/golden_set_outlet_payoff.json --whitelist-baseline-outlet --output-dir .audit/outlet_cohort/whitelist` (adjust to the actual CLI shape; long timeout). Record the full table (bonus × ΔNDCG × cliffs × gemΔ) into `.audit/outlet_cohort/PINNED_GATES.md` — the O-whitelist bar = best cell with cliffs ≤ 1.
- [ ] **Step 3:** Full suite; commit code + tests (not .audit): `feat(bench): outlet whitelist comparator for G4-style gating (plan 2026-07-07-002 Task 5)`.

---

## Task 6: The `death_outlet_feeder` rule — flag-off, hash-neutral wiring

**Files:**
- Create: `src/mtg_synergy_graph/complement_rules/death_outlet.py`
- Modify: `src/mtg_synergy_graph/complement_rules/core.py` (dispatch), `src/mtg_synergy_graph/complement_rules/registry.py` (RuleGate — port-gated, NOT CARD_LEVEL_RULES: the gate is a single death-shaped trigger port), `src/mtg_synergy_graph/universal_scorer.py` (`_RULE_TO_BUCKET: "death_outlet_feeder": "cost_synergy"`)
- Test: `tests/test_death_outlet.py`

**Design (from the explorer, verbatim intent):** a helper `_find_death_outlet_complements(conn, cmdr_ports, cmdr_set)` that (1) gates on the commander having a ChangesZone-shaped death trigger per the SAME logic as the Task 2 predicate's port-level core (non-self-only, no Sacrificed port — factor a shared `death_payoff.has_changeszone_death_payoff(cmdr_ports) -> bool` if Task 2 didn't already); (2) emits one `PortComplement(rule_id="death_outlet_feeder", direction="synergy", cmdr_event="death_outlet", cand_event=<filter_group>)` per candidate card holding a `cost.sacrifice` port, dedup one per card, with `cand_event` set from `_cost_filter_group`-style enrichment (`free_outlet`/`paid_outlet`/`self_sac` — reuse/import the existing helper from core.py so groups match the sibling arm's semantics) so IDF differentiates outlet classes. (3) `_ENABLE_DEATH_OUTLET_FEEDER = False` module flag, checked first. Do NOT modify `_invert_cost_feeds`, the seed JSON, or `combat.py:441` — the existing `cost_feeds_trigger` arm stays untouched.

- [ ] **Step 1: TDD per RULE_PLANNING §4** — failing tests: flag-off returns []; gate rejection (ETB-shaped ChangesZone → []); self-only filter → []; commander with Sacrificed port → [] (that commander is served by the existing arm); match case (cohort-shaped commander + candidate with cost.sacrifice port → one complement, exact rule_id/direction/cmdr_event, cand_event ∈ {free_outlet, paid_outlet, self_sac}); dedup (two sac-cost ports on one card → one complement); self-exclusion.
- [ ] **Step 2:** Implement + pass.
- [ ] **Step 3:** Wire: core.py dispatch (next to its cost-synergy siblings), registry RuleGate keyed on the death-shaped-trigger port predicate (so the auditor attributes coverage correctly and `rule_quality_gate.py` can find targets — follow an existing port-gated RuleGate's shape), `_RULE_TO_BUCKET` entry. TestWiring-style assertions (registry gate present, bucket mapping, dispatch reachable).
- [ ] **Step 4: Hash-neutrality (blocking):** `uv run pytest tests/ -q` green; `uv run scripts/bench.py audit --expect-identity` PASS (flag False, no weights entry, no ScoringConfigInputs field yet — see choreography in Global Constraints).
- [ ] **Step 5:** `graphify update .`; commit: `feat(rules): death_outlet_feeder rule, flag-off hash-neutral wiring (plan 2026-07-07-002 Task 6)`.

---

## Task 7: Measurement + bounded tuning (working tree only, nothing committed)

All flips uncommitted. Baselines: Task 4's pinned bands (H_outlet etc.), Task 5's whitelist table, golden-500 standing bands, `.audit/context_sim/g500/bands.json` per-commander values for cliffs.

- [ ] **Step 1:** Working tree: `_ENABLE_DEATH_OUTLET_FEEDER = True`; add `_RULE_QUALITY_MULTIPLIER` entry `"death_outlet_feeder": {"value": 1.5, "comment": "Plan 2026-07-07-002 starting point; tuned in Task 7."}`.
- [ ] **Step 2:** Golden-100 audit (`bench.py audit` — verdict, aggregate delta, gem numbers, warning); `rule_quality_gate.py --rule death_outlet_feeder` (stock CLI — port-gated rule, should evaluate; if it REJECTs, diagnose before proceeding); `--collinearity` vs the named siblings; `--rule death_outlet_feeder` ablation summary; `--inspect death_outlet_feeder --limit 20` (sanity: Viscera Seer / Ashnod's Altar-class cards on Meren/Judith-class commanders).
- [ ] **Step 3:** Page-based deltas: `context_sim bands` on the outlet fixture and golden-500 (vs recorded baselines; per-commander cliffs vs Task 4's per-commander values). Forensics ONE run for the O-clean sidecar numbers (raw + rank_bonus-ablated NDCG).
- [ ] **Step 4:** Tuning sweep: multiplier ∈ {0.75, 1.0, 1.5, 2.0, 2.5} on the outlet fixture (record ΔNDCG/gemΔ/cliffs per cell; reuse the Task-5/subtype-supply harness patterns); golden-500 + audit on the top 2 cells satisfying gem/cliff gates. NOTE the supply class is 1,923 cards — expect the leverage regime from the flatness diagnostic (every touched candidate jumps 15-40 ranks); if ALL cells cliff, record the smallest multiplier's numbers anyway and route by the gates.
- [ ] **Step 5:** Write `.audit/outlet_cohort/decision.md`: full gate table (O-noise / O-whitelist / O-500 / O-100 / O-quality / O-clean) at the chosen cell + the whitelist bar table + verdict per the pre-registered rules (SHIP / PARTIAL→human / DECLINE). If PARTIAL: STOP and report — the human decides.

---

## Task 8: Decision execution

### SHIP path (all O-gates pass at the chosen cell)
- [ ] One commit containing: flag True + shipped comment; `ScoringConfigInputs` field `enable_death_outlet_feeder` + `get_scoring_config_inputs` sourcing + hash-flip test + pinned-field-tuple test update (the PR #102 F1 lesson — do NOT ship an unregistered flag); weights entry with final value + evidence comment; re-pin all FOUR fixtures (`bench.py audit --repin --yes` for golden-100, then `--fixture` golden_set_run_500, golden_set_archetype_payoff, golden_set_outlet_payoff); `_PRODUCTION_HASH` refresh; RULE_HISTORY dated entry (gate table incl. O-clean raw+ablated numbers, honest verdict); plan status → shipped; `graphify update .`; full suite green.
- [ ] Post-ship verification: `bench.py audit` Δ≈0 self-consistency; re-run `--collinearity` at the final state and record ACTUAL numbers in RULE_HISTORY (no carryover).

### DECLINE path
- [ ] Revert working tree to Task 6 state (flag False; verify `--expect-identity` PASS); null-result doc `docs/solutions/best-practices/death-outlet-feeder-null-result-2026-07-XX.md` (frontmatter per house style; full sweep table; which gates killed it; what remains untested); plan status → declined + DECISION block; predicate/fixture/comparator/sidecar all REMAIN as standing infra; commit docs.

---

## Self-review notes

- **Spec coverage:** Part A (sidecar + honesty) = Task 1; Part B playbook = Tasks 2-8 mirroring plan 2026-07-07-001 with all four review lessons baked in (flag registration at ship, no collinearity carryover, honest verdict wording, instrument-vs-record reconciliation).
- **Type consistency:** `outlet_direction_death_payoff(conn) -> set[str]` (Tasks 2/3/5); `_find_death_outlet_complements(conn, cmdr_ports, cmdr_set)` + flag `_ENABLE_DEATH_OUTLET_FEEDER` (Tasks 6/7/8); rule_id `death_outlet_feeder` everywhere; H_outlet defined in Task 4, consumed in Task 7's gates.
- **Deliberate exclusions (YAGNI):** no seed-JSON edit (unsafe per `_SCOPED_TRIGGER_EVENTS` analysis); no `_COHORT_PREDICATES` union change (pinned-fixture stability); no rank_bonus removal (separate future decision); no combat.py:441 changes; the 22 Sacrificed-port commanders stay on the existing `cost_feeds_trigger` arm.
- **Known judgment calls left to implementers with guidance:** exact `RankedCandidate` shape (read forensics.py first); the whitelist CLI selector shape (match existing style); whether Task 2 factors a shared port-level helper into `death_payoff.py` for Tasks 5/6 to reuse (preferred if clean).

---

## DECISION (Task 8, 2026-07-07)

**Verdict: DECLINE.** Working tree reverted to the Task 6 hash-neutral state
(`_ENABLE_DEATH_OUTLET_FEEDER = False`, no weights entry); `uv run pytest tests/ -q`
green; `bench.py audit --expect-identity` PASS. Nothing committed for Part B beyond
the already-merged Task 6 flag-off wiring. Full analysis:
`docs/solutions/best-practices/death-outlet-feeder-null-result-2026-07-07.md`.

**Sweep (outlet fixture n=126, vs baseline 0.1152 NDCG / 0.9616 gem):**

| multiplier | ΔNDCG | gem Δ | cliffs (<−0.05) |
|---|---|---|---|
| 0.75 (chosen) | −0.0113 | +0.0058 | 10 |
| 1.00 | −0.0141 | +0.0058 | 17 |
| 1.50 | −0.0220 | +0.0082 | 22 |
| 2.00 | −0.0255 | +0.0101 | 24 |
| 2.50 | −0.0328 | +0.0127 | 32 |

Monotone degradation at every cell; no cell NDCG-positive.

**Gate table at the chosen cell (multiplier 0.75):**

| Gate | Threshold | Measured | Result |
|---|---|---|---|
| O-noise | ΔNDCG ≥ +0.0233 | −0.0113 | **FAIL** |
| O-gem | Δ ≥ −0.0104 | +0.0058 | PASS* |
| O-cliffs | ≤ 1 | 10 | **FAIL** |
| O-500 (NDCG/gem) | ≥ −0.0136 / ≥ −0.0235 | +0.0008 / −0.0005 | PASS |
| O-100 | non-NEGATIVE, no gem warning | positive (Δ +38.01) | PASS |
| O-quality (gate CLI) | PASS | PASS | PASS |
| O-quality (collinearity) | no VIF>5 AND \|r\|>0.8 | max \|r\|=0.723 (edict_feeder) | PASS |
| O-clean (golden-100) | not negative beyond noise | Δ −0.0007 | PASS |
| O-clean (cohort) | ablated Δ ≥ +0.0233 | **−0.0069** | **FAIL** |

\* the gem bump sits inside the whitelist comparator's own range at negative NDCG —
whitelist-signature, not merit. The whitelist comparator (Task 5) was ALSO negative
at every cell (best: bonus 0.10, ΔNDCG −0.0083, 8 cliffs), and the rule at 0.75 sits
between the whitelist's 0.10 and 0.25 cells on every axis — whitelist-equivalent.

**Root cause:** every one of 1,996 sac-outlet candidates receives one flat
per-`filter_group` IDF contribution (0.1729 at multiplier 1.5) — zero
per-candidate discrimination (Viscera Seer == Akki Avalanchers). This is the
flatness-diagnostic leverage regime (`.superpowers/sdd/diagnostic-head-flatness.md`)
measured at rule scale: a large low-dispersion class bump reshuffles the flat head
en masse, producing 10-32 per-commander cliffs. Same failure family as the
deck-context flood DECLINE — a per-class flat credit cannot rank within the class
it targets.

Predicate, fixture, bands, whitelist comparator, rule code (flag-off), and the
Task 1 rank_bonus sidecar all REMAIN as standing infra for a future
per-candidate-discriminating retry.
