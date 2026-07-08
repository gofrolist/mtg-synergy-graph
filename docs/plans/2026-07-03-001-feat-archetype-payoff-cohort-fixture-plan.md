---
title: "feat: Archetype-payoff cohort fixture + cohort-restricted NDCG readout"
type: feat
status: active
date: 2026-07-03
deepened: 2026-07-03
origin: docs/brainstorms/2026-07-03-archetype-payoff-subtype-link-requirements.md
---

# feat: Archetype-payoff cohort fixture + cohort-restricted NDCG readout

## Overview

Build an **evaluation instrument**, not a scoring change: a committed
cohort fixture over the archetype-payoff commander cohort, plus a
cohort-restricted NDCG slice in the existing per-commander NDCG reporter.
Today the golden-100/500 audit fixtures contain only **2 / 4** of the 27
archetype-payoff commanders, so any future archetype-payoff mechanism
DECLINEs on eval-set *dilution* rather than merit. This is the "fix the
ruler before the rule" step from
`docs/solutions/best-practices/archetype-payoff-idf-flood-null-result-2026-07-03.md`.

## Problem Frame

The 5-persona review of the archetype-payoff brainstorm (see origin, now
`parked`) established that the eval fixture is the real blocker for the
whole archetype-payoff direction: a win concentrated in a few-dozen-
commander cohort is invisible to a 100/500-commander aggregate. Measured
this session against `data/synergy.db` + `data/tags.db`:

- Cohort = **27** legal legendary-creature commanders with a subtype-keyed
  **death** trigger (`valid_filter` names a token-producible subtype; event
  is `Sacrificed` or `ChangesZone` Battlefield→Graveyard).
- In golden-100: **2** (Slimefoot, Wilhelt); golden-500: **4**; **22** in
  neither.
- **24 / 27** have ≥10 EDHREC synergy labels — but that count used *all*
  `edhrec_card_synergy` rows; the fixture builder filters on the
  `'High Synergy Cards'` section (`MIN_HIGH_SYNERGY_ROWS=1`), so the real
  buildable count must be re-verified at implementation time (see Open
  Questions).

## Requirements Trace

- **R1.** A reusable, extensible cohort-selection module. First predicate
  (`subtype_death_payoff`) yields the measured 27; adding future archetype
  predicates (graveyard/lands/token payoffs, incl. the broader
  outlet-direction death-payoff population) is appending a callable to a
  module-level tuple, not a rewrite. (User decision: "Extensible module,
  seed with the 27" — kept light per review: tuple-of-callables, no
  name-dispatch registry.)
- **R2.** A committed cohort fixture
  `tests/fixtures/golden_set_archetype_payoff.json`, built via the existing
  `build_fixture(conn, commanders, edhrec_conn=...)` over the
  EDHREC-covered cohort, carrying its own `config_hash` + pinned baseline.
- **R3.** A cohort-restricted NDCG readout: a slice dimension in
  `per_commander_ndcg` that groups per-commander NDCG by cohort membership,
  so a cohort-local win is visible undiluted (mirrors the existing
  color-identity slice).
- **R4.** Zero scoring-path changes. Canonical golden-100/500 fixtures and
  the commit-gate verdict logic are untouched; the new fixture joins the
  `config_hash` re-pin discipline **mechanically** — added to
  `_COMMITTED_GOLDEN_FIXTURES` in `tests/bench/test_fixture_freshness.py`
  so its staleness is a no-DB CI gate, not prose (the freshness test
  exists precisely because prose-only discipline let golden-500 go stale
  for ~3 weeks).
- **R5.** Docs: a CLAUDE.md command block for building/pinning/reading the
  cohort fixture; re-verify + record the buildable count under the
  High-Synergy filter after the first pin.
- **R6.** A published cohort NDCG **noise band / minimum-detectable-effect**
  (via `portfolio_sim.py bands` machinery) so a cohort readout can be
  judged signal-vs-noise; readouts below the band are not wins.

## Scope Boundaries

- **No scoring-path changes** — no new rule, weight, `universal_scorer`,
  `graph_engine`, or `complement_rules` edit. This is eval-harness infra.
- **Not the archetype-payoff rule** — building any producer→payoff matcher
  is the parked/future work this instrument exists to *enable*, not do.
- **No change to canonical fixtures** — golden-100/500 and their pins stay
  as-is; the cohort fixture is additive and isolated.
- **One predicate only** — the module is extensible, but only
  `subtype_death_payoff` is seeded now. Defining graveyard/lands/token
  predicates is deferred.

### Deferred to Separate Tasks

- Additional archetype-payoff predicates (graveyard/lands/token payoffs):
  future registry additions once each is measured.
- Any producer→payoff scoring rule: a separate brainstorm/plan gated on
  this instrument (see origin doc's Reframe options).

## Context & Research

### Relevant Code and Patterns

- `src/mtg_synergy_graph/bench/fixture.py` — `build_fixture(conn,
  commanders, edhrec_conn=...)`, `PinnedFixture`, `FixtureEntry`. The
  fixture builder is commander-list-driven; a cohort fixture is just this
  over a different list. `TOP_N_PINNED=100`, `SCHEMA_VERSION=2`.
- `scripts/bootstrap_golden_set_500.py` — the canonical pattern to mirror:
  select commanders (SQL over `cards` + EDHREC High-Synergy filter via
  `commander_to_slug`), `build_fixture`, `fixture.write(OUTPUT_PATH)`,
  set `config_hash`. `EDHREC_DB = data/tags.db`, section
  `'High Synergy Cards'`, `MIN_HIGH_SYNERGY_ROWS=1`.
- `src/mtg_synergy_graph/bench/cli.py:362` — `--fixture` flag already
  exists (default `tests/fixtures/golden_set_run.json`, env override
  `BENCH_FIXTURE`). **Running/pinning the cohort fixture needs no CLI
  change** — `bench.py audit --fixture <cohort> --repin --yes`.
- `src/mtg_synergy_graph/bench/per_commander_ndcg.py` — `compute_identity_slices`
  + `_render_identity_slices` are the exact pattern for R3's cohort slice
  (a second slice dimension keyed on cohort membership instead of color
  identity). `compute_per_commander_ndcg_rows`, `render_per_commander_ndcg_markdown`.
- `src/mtg_synergy_graph/validate.py:94` — `commander_to_slug(name)`, the
  canonical slug (use this, not an ad-hoc heuristic — a truncating slug
  produced false EDHREC-coverage zeros during scoping).
- `src/mtg_synergy_graph/bench/tensor.py` — `compute_config_hash()`.

### Institutional Learnings

- `docs/solutions/best-practices/archetype-payoff-idf-flood-null-result-2026-07-03.md`
  — why this instrument exists (eval-set dilution; fix the ruler before
  the rule).
- `docs/solutions/best-practices/optimizer-fixture-size-2026-04-30.md` —
  small fixtures give noisy gradient signal; the cohort fixture (~24) is a
  *diagnostic slice*, not a gradient/optimizer fixture — keep that framing.
- CLAUDE.md test convention: **tmp_path-only test DBs** (session-scoped
  autouse guard fails the run if a `*.db` appears at repo root / `data/`);
  `open_db(..., create=False)`. ValueError-never-assert.
- CLAUDE.md config-hash discipline: a functional edit to seed/weights flips
  `compute_config_hash` → re-pin. A new fixture with its own `config_hash`
  must be re-pinned whenever scoring config changes.

## Key Technical Decisions

1. **Separate cohort fixture file, not an extension of golden-500.**
   Extending golden-500 re-pins the canonical baseline (churns every
   optimizer/audit consumer keyed on its `config_hash`); a dedicated
   `golden_set_archetype_payoff.json` is additive and isolated.
2. **Reuse `--fixture` + `--repin`; no CLI surface change to run.** The
   cohort fixture is selected by path.
3. **Extensible via a tuple of predicate callables, seeded with one.**
   A cohort is the union of predicate functions in a module-level tuple;
   `subtype_death_payoff` is the only member now. Deliberately *light* —
   a tuple + union, NOT a name→function registry with a `predicates`
   selector (that dispatch machinery is unearned at one member; add it
   when a second predicate needs distinguishing). Extensibility is kept
   (user decision) because the outlet-direction survivor will need a
   broader death-payoff predicate — see "Instrument usage caveats."
4. **Cohort membership is SNAPSHOT into the fixture at build time**, and
   the slice reads the snapshot when present (falling back to live
   `bench.cohorts` computation only for untagged fixtures like
   golden-500). Live-only computation is not reproducible from the pin:
   the predicate is a multi-join over port data that shifts across
   cardsfolder refreshes, so the same pin could partition differently at
   read time. The snapshot makes the primary instrument's readout
   reproducible; the live-slice-any-fixture path stays available as a
   secondary, explicitly non-reproducible diagnostic.
5. **The cohort readout is a diagnostic, not a new commit gate.** It does
   not change SHIP/DECLINE logic; it makes a future mechanism's cohort
   effect *visible*. The commit gate stays on the canonical fixture.

## Instrument usage caveats (how a future mechanism must be judged)

The 5-persona plan review surfaced two ways this ruler can mislead if used
naively. These are binding constraints on any *future* mechanism judged
against the cohort — recorded here so the instrument is not misread:

- **A cohort-NDCG gain is NECESSARY-BUT-NOT-SUFFICIENT.** The cohort is
  selected by the same `subtype_death_payoff` predicate a future rule
  would key on, so a disguised supply/rarity whitelist (the null-result
  doc's honest outcome — "DECLINE-or-disguised-whitelist") scores
  *maximally* here by construction. A cohort gain proves "helps where it
  fires," not "generalizes" and not "isn't a whitelist." Any future SHIP
  must ALSO clear (a) a whole-fixture no-regression check on golden-500,
  AND (b) a whitelist-equivalence check (compare the mechanism's cohort
  lift against a hardcoded supply-cutoff baseline — if it doesn't beat the
  whitelist, it IS the whitelist).
- **A cohort delta is meaningless without the noise band.** ~24
  commanders is small; `+0.03` is not "signal" until it clears the
  cohort's minimum-detectable-effect. Unit 4 publishes the bootstrap noise
  band; readouts below it are noise, not wins.
- **The seeded predicate targets the producer/depth-2 survivor
  population** (commander-side: has a subtype-keyed death payoff). The
  outlet-direction survivor may serve a broader "any-creature-death
  payoff" population; that is a *future predicate* in the tuple, not a
  reason to redefine the seed. This is why the module stays extensible.

## Open Questions

### Resolved During Planning

- Fixture location / format → separate `golden_set_archetype_payoff.json`,
  same `PinnedFixture` schema (Decision 1).
- config_hash / pin → own `config_hash`, pinned via existing
  `--fixture`+`--repin`; joins re-pin discipline (R4).
- Cohort breadth → extensible module seeded with the 27 (user decision).
- Aggregation → cohort slice mirroring `compute_identity_slices` (R3).

### Deferred to Implementation

- **Exact buildable count under the High-Synergy filter.** The 24/27 used
  all synergy rows; the builder's `'High Synergy Cards'` ≥1 filter may drop
  a few more (e.g. the 3 low-data commanders Daryl / Sierra / Titania).
  Log dropped commanders; the fixture ships with whatever clears the
  filter. Not a blocker — a smaller cohort still beats 2/100.
  **RESOLVED (implementation):** the seed predicate yields **36** on current
  `data/synergy.db` (the narrative "27" was a prior snapshot; user confirmed
  the seed intentionally includes artifact-token subtypes — Clue/Food/Treasure/
  Junk — matching the Unit 1 Approach spec, not a creature-only restriction).
  **33** clear the High-Synergy filter; **3 dropped** for no High-Synergy EDHREC
  data: Daryl, Jenny Flint, Miara (Sierra and Titania *did* clear). Cohort NDCG
  slice: in-cohort n=33, mean pinned NDCG@30 **0.1436** (per_commander reporter);
  bootstrap noise band (portfolio_sim, seed 17) half-width **0.0567**.
- Whether the cohort slice should also surface the hidden-gem rate per
  cohort (the `hidden_gem_hit_rate` legacy field already exists per entry).
  Add only if trivial once the slice scaffold is in.

## Implementation Units

- [x] **Unit 1: Cohort-selection predicate module**

**Goal:** An extensible module that returns the archetype-payoff cohort
commander names, with one seeded predicate (`subtype_death_payoff`).

**Requirements:** R1

**Dependencies:** None

**Files:**
- Create: `src/mtg_synergy_graph/bench/cohorts.py`
- Test: `tests/bench/test_cohorts.py`

**Approach:**
- A predicate is a pure function `(conn) -> set[str]` returning commander
  names. A module-level tuple `_COHORT_PREDICATES = (subtype_death_payoff,)`
  holds them; `archetype_payoff_cohort(conn) -> set[str]` unions them. No
  name→function dispatch or `predicates` selector (unearned at one member;
  add when a second predicate lands — see Key Decision 3).
- `subtype_death_payoff`: SQL over `card_ports` joined to `cards`
  (legal_commander=1, `supertypes LIKE '%Legendary%'`, `types`/`card_types`
  `LIKE '%Creature%'`), `port_type='trigger'`, `valid_filter` head-token ∈
  token-producible subtypes (from `port_attributes.attr_kind='token_subtype'`),
  event is `Sacrificed` OR (`ChangesZone` reaching the graveyard). This is
  the exact predicate validated during scoping (yields 27).
- **`zone_origin` tolerance:** `ChangesZone` death rows carry `zone_origin`
  values including `Battlefield`, `Any`, empty, and comma-lists — a strict
  `zone_origin='Battlefield'` clause would drop real death shapes. Mirror
  the scoping query's tolerant detection (destination reaches Graveyard;
  origin is Battlefield/Any/empty/contains-Battlefield), which is what
  produced the 27.
- SQL fragment interpolation (if any) guarded by a `_VALID_*` frozenset +
  `ValueError`, never `assert` (CLAUDE.md convention).

**Patterns to follow:**
- The scoping query used this session (subtype set from `token_subtype`,
  death-event detection via `Sacrificed`/`ChangesZone`+GY).
- Registry-of-callables shape used by `complement_rules.registry`.

**Test scenarios:**
- Happy path: `subtype_death_payoff(conn)` on a fixture DB containing
  Slimefoot (Saproling death trigger) returns Slimefoot; a plain goodstuff
  commander with no subtype-death trigger is excluded.
- Edge case: a commander whose trigger names a subtype but the event is
  ETB/Attacks (not death) is excluded (death-event gate).
- Edge case: a commander with a death trigger on a non-token-producible
  subtype token is excluded (subtype must be in the `token_subtype` vocab).
- Happy path: `archetype_payoff_cohort(conn)` unions predicates and returns
  a `set[str]`; with the single seeded predicate it equals
  `subtype_death_payoff(conn)`.
- Integration: run against the real `data/synergy.db` is NOT done in unit
  tests (tmp_path only); use a small synthetic `card_ports`/`cards`/
  `port_attributes` fixture built under `tmp_path`.

**Verification:**
- Module returns a deterministic commander set from a synthetic DB; adding a
  second dummy predicate to `_COHORT_PREDICATES` changes the union as expected.

- [x] **Unit 2: Cohort-fixture bootstrap script + committed fixture**

**Goal:** A script that builds and writes
`tests/fixtures/golden_set_archetype_payoff.json`, and the committed
fixture itself.

**Requirements:** R2, R5

**Dependencies:** Unit 1

**Files:**
- Create: `scripts/bootstrap_archetype_payoff_fixture.py`
- Create: `tests/fixtures/golden_set_archetype_payoff.json` (build output)
- Modify: `tests/bench/test_fixture_freshness.py` (add the new fixture to
  `_COMMITTED_GOLDEN_FIXTURES`)
- Test: `tests/bench/test_archetype_payoff_fixture.py`

**Approach:**
- Mirror `scripts/bootstrap_golden_set_500.py`: get cohort names from
  `bench.cohorts.archetype_payoff_cohort(conn)`, filter to those with
  `≥ MIN_HIGH_SYNERGY_ROWS` in the `'High Synergy Cards'` section (via
  `commander_to_slug` + the same GROUP BY the 500 script uses), log dropped
  commanders to stderr, `build_fixture(conn, kept, edhrec_conn=...)`, set
  `config_hash = compute_config_hash()`, `fixture.write(OUTPUT_PATH)`.
- **Snapshot cohort membership into the fixture** so the slice readout is
  reproducible from the pin (Key Decision 4): persist the built cohort
  commander list as a top-level fixture field (a small `cohort_members`
  key alongside `config_hash`/`created_at`; forward-compatible with
  `PinnedFixture` — the loader ignores unknown top-level keys today, so
  confirm/extend the loader to round-trip it).
- **Add `golden_set_archetype_payoff.json` to `_COMMITTED_GOLDEN_FIXTURES`**
  in `test_fixture_freshness.py` so the config_hash freshness of the new
  fixture is enforced by the same no-DB CI gate as golden-100/500 (R4).
- Exit codes mirror the 500 script (2 if DBs missing).
- Re-verify and print the buildable count (Deferred Question); record it in
  CLAUDE.md after the first pin.

**Patterns to follow:**
- `scripts/bootstrap_golden_set_500.py` end-to-end (selection → filter →
  build → write → config_hash), swapping the selection query for the cohort
  module.

**Test scenarios:**
- Happy path: running the builder against a synthetic `tmp_path` synergy DB
  + tags DB produces a `PinnedFixture` whose entries equal the
  EDHREC-covered cohort; `config_hash` is populated.
- Edge case: a cohort commander with zero `'High Synergy Cards'` rows is
  dropped and logged (not written to the fixture).
- Error path: missing synergy/tags DB → exit code 2 (mirror the 500
  script), no partial file written.
- Integration: the written JSON round-trips through `PinnedFixture.load`
  and has `schema_version == 2`; the `cohort_members` snapshot persists
  and reloads intact.
- Integration: `test_fixture_freshness` now enumerates
  `golden_set_archetype_payoff.json` and its config_hash matches
  `compute_config_hash()` (the no-DB freshness gate covers the new fixture).

**Verification:**
- `tests/fixtures/golden_set_archetype_payoff.json` exists, loads, and
  contains the buildable cohort (Slimefoot + Wilhelt present); builder is
  idempotent (re-run reproduces the same entries modulo `created_at`).

- [x] **Unit 3: Cohort-restricted NDCG slice in per_commander_ndcg**

**Goal:** A cohort slice in the per-commander NDCG reporter so cohort NDCG
is reported separately from the whole-fixture aggregate.

**Requirements:** R3

**Dependencies:** Unit 1

**Files:**
- Modify: `src/mtg_synergy_graph/bench/per_commander_ndcg.py`
- Test: `tests/bench/test_per_commander_ndcg_cohort_slice.py`

**Approach:**
- Mirror `compute_identity_slices` / `_render_identity_slices`: add
  `compute_cohort_slices(rows, cohort_names)` that partitions the
  per-commander NDCG rows into `in-cohort` / `rest` and computes mean
  pinned/live/delta + n per group, and `_render_cohort_slices` for the
  markdown block.
- **Membership source (Key Decision 4):** prefer the fixture's
  `cohort_members` snapshot when present (reproducible from the pin); fall
  back to live `archetype_payoff_cohort(conn)` for untagged fixtures
  (golden-500), rendering that block with an explicit "live, not
  pin-reproducible" note.
- **Handler wiring (verified against the code):** add a
  `cohort_names: set[str] | None` kwarg to
  `render_per_commander_ndcg_markdown`; in `handle_per_commander_ndcg`
  compute `cohort_names` **inside the existing `try` block, alongside
  `fetch_identity_classes`, BEFORE `conn.close()`** — the handler renders
  after closing the connection, so a cohort query issued post-close would
  fail. Thread the result through to the renderer.
- Render the cohort slice after the identity-slice block; print group `n`
  next to the delta. On **n=0** (no cohort members in the fixture) render a
  zero-count row (do NOT omit — a missing block reads as "no cohort" rather
  than "cohort empty here"); guard the mean against division by zero.

**Patterns to follow:**
- `compute_identity_slices`, `_render_identity_slices`,
  `_render_aggregate_summary` in the same file.

**Test scenarios:**
- Happy path: given per-commander rows where 3 of 10 commanders are in the
  cohort, the cohort slice reports n=3 with the correct mean NDCG, and the
  `rest` slice reports n=7.
- Edge case: zero cohort members in the fixture → cohort slice renders a
  zero-count row (NOT omitted) without dividing by zero.
- Edge case: all commanders in the cohort → `rest` slice n=0 handled.
- Integration: a fixture carrying a `cohort_members` snapshot uses it (the
  slice does not re-query the DB); an untagged fixture falls back to live
  `archetype_payoff_cohort(conn)` and is labeled non-reproducible.
- Integration: `render_per_commander_ndcg_markdown` output contains the
  cohort slice block below the identity-slice block, with stable ordering.

**Verification:**
- Running the per-commander NDCG reporter against the cohort fixture (or
  golden-500) shows an `in-cohort` vs `rest` NDCG breakdown with counts.

- [x] **Unit 4: Cohort NDCG noise band + pin + docs**

**Goal:** Publish the cohort's NDCG noise band / minimum-detectable-effect
so readouts are judged signal-vs-noise, then pin the fixture and document
the workflow.

**Requirements:** R6, R4, R5

**Dependencies:** Unit 2, Unit 3

**Files:**
- Create: `tests/bench/test_cohort_noise_band.py`
- Modify: `CLAUDE.md` (Common Commands — cohort fixture build/pin/read +
  noise-band block; note the re-pin-set membership)
- (Data produced by running: the bootstrap script + `--repin`, and the
  noise-band computation.)

**Approach:**
- **Noise band:** reuse `portfolio_sim.py bands` bootstrap machinery (it
  already produces bootstrap NDCG/gem noise bands over a fixture) to
  compute the cohort's NDCG@30 noise band / minimum-detectable-effect.
  Expose it either as a `portfolio_sim bands --fixture <cohort>` invocation
  documented in CLAUDE.md, or a thin wrapper that reports the band for the
  cohort fixture. Publish the resulting band (the delta threshold below
  which a cohort readout is noise) in CLAUDE.md next to the read command.
- **Pin:** `bench.py audit --fixture tests/fixtures/golden_set_archetype_payoff.json --repin --yes`.
- **Docs:** CLAUDE.md command block (build → pin → read cohort slice →
  read noise band); note the cohort fixture joins the re-pin set (freshness
  gate now enforces it — Unit 2); confirm the pre-commit `bench-audit` hook
  still runs only against the canonical fixture (cohort fixture is opt-in
  via `--fixture`) — no change to the default gate.

**Patterns to follow:**
- `src/mtg_synergy_graph/bench/portfolio_sim.py` `bands` mode (bootstrap
  noise bands over a fixture).
- Existing `bench.py audit --repin --yes` + `bootstrap_golden_set_500.py`
  re-run guidance in CLAUDE.md.

**Test scenarios:**
- Happy path: the noise-band computation over the cohort fixture returns a
  finite band (lower/upper or an MDE value) and is deterministic under a
  fixed bootstrap seed (mirror `portfolio_sim` seed discipline).
- Edge case: a degenerate tiny cohort (n<5 synthetic) still returns a band
  without error (wide band is acceptable; a crash is not).

**Verification:**
- `bench.py audit --fixture tests/fixtures/golden_set_archetype_payoff.json`
  runs clean (config_hash matches after pin) and is enforced by the
  freshness gate; CLAUDE.md documents build→pin→read→band with the
  published threshold; canonical commit gate unchanged.

## System-Wide Impact

- **Interaction graph:** New fixture is consumed only via explicit
  `--fixture`. The default pre-commit `bench-audit` hook and the canonical
  golden-100/500 audits are untouched. `per_commander_ndcg` gains an
  additive slice block; existing output ordering preserved above it.
- **Error propagation:** DB-missing and EDHREC-query failures follow the
  existing `bootstrap_golden_set_500.py` / `_edhrec_top_30` degradation
  (exit 2 / stderr warning), not new failure modes.
- **State lifecycle risks:** The cohort fixture carries its own
  `config_hash`; if it drifts from live config it reports a config-hash
  mismatch exactly like the canonical fixture — self-announcing, not silent.
- **API surface parity:** `--fixture` already exists on `bench.py audit`;
  no new flag. The cohort module is importable by future reporters
  (demand_coverage, forensics) for consistent cohort labeling.
- **Unchanged invariants:** Scoring output is bitwise-unchanged (no
  scoring-path edit); `compute_config_hash` inputs are unchanged (a new
  fixture file is not a config-hash input); canonical fixtures and the
  commit-gate verdict are unchanged.

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| Cohort fixture is small (~24) → a readout can't be told from noise | Unit 4 publishes the bootstrap NDCG noise band / minimum-detectable-effect (`portfolio_sim bands`); readouts below the band are declared noise, not wins. Group `n` printed alongside. |
| **Instrument launders a whitelist** (cohort selected by the same predicate a rule keys on) | "Instrument usage caveats": a cohort gain is necessary-but-not-sufficient; any future SHIP must also pass whole-fixture no-regression AND a whitelist-equivalence check vs a hardcoded supply cutoff. |
| **Live cohort membership not reproducible from the pin** (predicate is a volatile multi-join over port data) | Snapshot `cohort_members` into the fixture at build time (Key Decision 4); the slice reads the snapshot; live-slice of untagged fixtures is labeled non-reproducible. |
| High-Synergy filter drops more than the 3 known low-data commanders | Log dropped commanders; the instrument still beats 2/100. Buildable-count re-verify is an explicit Deferred Question, not a blocker. |
| A test accidentally materializes a repo-root `*.db` (poisons skip-guards) | tmp_path-only DBs; synthetic fixtures; the session-scoped autouse guard in `tests/conftest.py` fails the run if violated. |
| Cohort fixture forgotten at re-pin time → stale config_hash | Added to `_COMMITTED_GOLDEN_FIXTURES` (Unit 2) so `test_fixture_freshness` fails on staleness as a no-DB CI gate — mechanical, not prose/self-report. |
| Ad-hoc slug reintroduces false EDHREC misses | Use `commander_to_slug` (validate.py) everywhere; covered by Unit 2 tests. |

## Documentation / Operational Notes

- CLAUDE.md gains the cohort fixture build/pin/read command block (Unit 4).
- No release/rollout impact — internal eval tooling; gitignored `.audit`
  outputs unaffected.

## Sources & References

- **Origin document:** [docs/brainstorms/2026-07-03-archetype-payoff-subtype-link-requirements.md](../brainstorms/2026-07-03-archetype-payoff-subtype-link-requirements.md) (status `parked`)
- Null-result / rationale: `docs/solutions/best-practices/archetype-payoff-idf-flood-null-result-2026-07-03.md`
- Fixture builder: `src/mtg_synergy_graph/bench/fixture.py`; bootstrap
  pattern: `scripts/bootstrap_golden_set_500.py`
- Slice pattern: `src/mtg_synergy_graph/bench/per_commander_ndcg.py`
  (`compute_identity_slices`)
- Related PRs this session: #91 (gy-fuel DECLINE), #92 (archetype-payoff PARK)
