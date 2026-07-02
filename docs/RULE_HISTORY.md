# Rule history

Dated log of complement-rule additions, audit verdicts, and per-commander
impact notes. See `scripts/_audit_rule_impact.py` for the per-rule impact
methodology (NDCG@30 metric + golden-set safety net + CONTENTIOUS verdict).
See `docs/RULE_PLANNING.md` for the forward-looking planning workflow.

## 2026-07-02

### anthem_payoff probe — DECLINED, helper retained for the C1 cycle (plan 2026-07-02-002 Unit 10)

Type-scoped anthems (Creature.YouCtrl AddPower/AddToughness/AddKeyword
statics) for creature-token producers — the global-anthem slice of the
159-card static.Continuous NO_RULES block. Quality Gates A/B PASS (288
target commanders, coverage 6.0, CV 0.117). Three variants measured on
the 500-cmdr fixture: full-scope IDF keys flooded (35 cliffs, Krenko
−0.357 — the high-cardinality granularity trap); coarse two-key
granularity reached mean −0.0001 / gem +0.0192 / Krenko +0.053 with 2
cliffs; multiplier 0.6 fixed Jan Jansen but worsened Myrel to −0.1266
(Soldier archetype × Unit-2 skiplist interplay). Fourth appearance of
the flood-vs-archetype displacement pattern (see
calibration-track-null-result-2026-07-02.md) — the weight layer cannot
place even a well-formed support family safely. Unwired; the
`_find_anthem_payoffs` helper + tests stay for the C1 cycle to re-wire
behind the lift baseline.

### Vocabulary v4 — PHASE + INTERNAL classification; unwrapping deferred with evidence (plan 2026-07-02-002 Unit 8)

`trigger.Phase` (2,305 rows) and `effect.Cleanup` (2,759 rows) leave
UNKNOWN; distinct UNKNOWN-shaped cards 15,338 → 13,777. Embeddings
rebuilt under vocab v4; zero scoring impact (identity PASS).
Investigation findings: Phase triggers' Execute payloads were already
extracted as separate effect ports, so no event-map expansion is
needed at the trigger level; the REAL coverage gap is the
granted-ability / wrapper family — `effect.Effect` SVar wrappers and
`AddAbility` statics (Phenax's mill lives inside a Continuous static
granting "T: target player mills X" and never surfaces as an
effect.Mill port). That unwrapping is importer-level work, deferred to
the PPMI/importer batch. Remaining top UNKNOWN shapes (Destroy 1,200 /
Dig 828 / Charm 738 cards) are generic-glue classes owned by the C1
design cycle.

### Calibration track closed — Units 4–6 DECLINE, escalation to C1 (plan 2026-07-02-002)

Ten weight-layer configurations measured (concave haircuts, payoff/body
tiers, pool scaling, joint arms) — none clears the −0.05 per-commander
cliff gate; every cliff is a flood-as-archetype commander (tribal /
tribe-as-fuel / spell-as-archetype). Gem rate improved in every
configuration (up to +0.0297); NDCG-vs-EDHREC cliffs are the sole
blocker. Escalation rule fired: the OUTRANKED lever moves to the C1
lift-normalization design cycle. Full table + structural finding:
`docs/solutions/best-practices/calibration-track-null-result-2026-07-02.md`.
All probe infrastructure survives flag-OFF.

### Tribal payoff/body two-tier probe — INVESTIGATE, resolution via Unit 6 joint probe (plan 2026-07-02-002 Unit 5)

`_ENABLE_TRIBAL_PAYOFF_TIER` splits tribal emission into payoff pieces
(structured tribal reference → `tribal_density` 0.5) and vanilla bodies
(`tribal_body`, flat 0.3). Best-variant evidence on the 500-cmdr
fixture: gem +0.0241 (the largest goal-axis gain of the remediation
plan), mean NDCG +0.0051, Marrow-Gnawer +0.2544 / Chatterfang +0.0775 /
Lathril +0.0238 / Edgar +0.0188 — but 5 fuel-tribe cliffs (Nissa
Resurgent Animist −0.107, Arasta −0.096, Rograkh −0.067, Camellia
−0.060, Elenda −0.053): commanders whose engine consumes vanilla tribe
bodies (mana elves, sac fodder, token fuel). Body-weight sweep
0.15/0.30/0.40 → 11/5/"gains lost"; a fuel-tribe exemption (commander
filter references tribe) was measured and REJECTED — it zeroed the
Marrow-class wins. The gem-dominant INVESTIGATE trigger fired (gem ≥
+0.02, NDCG flat). Flag stays OFF; infra (tribal_body rule_id, weight
entry, hash field, vanilla-anchor exemption, tests) lands inert.
Resolution: Unit 6 pool-scaled flat weights naturally protect small
fuel tribes (Kobold 8, Spider 60, Squirrel 40) — the tier re-flips
jointly in Unit 6's evidence package.

### Tribal skiplist bypass fix — structured evidence for overbroad tribes (SHIPPED, plan 2026-07-02-002 Unit 2)

The `_VANILLA_TRIBAL_SKIPLIST` (Human/Warrior/Soldier) only guarded the
vanilla-anchor fallback; the primary `_commander_subtypes_from_ports`
path admitted skiplisted tribes via (a) TriggerDescription prose in
`raw_line` ("...create a 1/1 white Human creature token...") and (b)
token Gate 1 (own-type token production). Adeline activated the full
~1,500-card Human pool at flat 0.5 — her forensics displacer profile was
93.5% tribal_density.

- **Fix**: skiplisted tribes now require a structured
  `valid_filter`/`affected_scope` reference on the body direction
  (`tribal_density`). The payoff direction (`lord` anthem matching)
  passes `include_overbroad_tribes=True` — Adeline making Human tokens
  genuinely wants Human anthems (first pass without this restored
  direction put Adeline at −0.0697 NDCG by dropping Coppercoat
  Vanguard / General's Enforcer; the flag recovered them).
- **Skiplist constant** moved density.py → core.py (density imports
  core; reverse would be circular).
- **Audit (non-probe gate, plan 2026-07-02-002)**: 100-cmdr aggregate
  NDCG −0.0003, gem 0.8153→0.8160; 500-cmdr aggregate −0.0001, gem
  −0.0005, per-commander cliff violations 0 (worst: Adeline −0.0392).
  Score mass −60.8 on the golden set = the Human/Warrior flood removed
  (Adeline −41, Yawgmoth −21.5, Rhys −2.5); hi_syn losses 0. Histogram
  verdict HARMFUL is the expected score-mass reaction; gate asymmetry
  documented in the plan.
- Both fixtures re-pinned in the landing commit.

## 2026-05-20

### `CopyFaceFrom:<Name>` back-face resolution (LANDED, follow-up #2 to PR #47)

Closes the data-model gap flagged in the 2026-05-19 Prepared brainstorm:
22 of the 47 Prepared-payoff cards encode their back face as
`CopyFaceFrom:<X>` (Reanimate, Brainstorm, Demonic Tutor, Swords to
Plowshares, Wheel of Fortune, …), and the importer previously dropped
the directive — Grave Researcher carried no Reanimate ports, Naktamun
Lorespinner no Wheel-of-Fortune ports, etc. See
`docs/brainstorms/2026-05-20-copy-face-from-resolution-requirements.md`
for the full design (Q1-Q5 + v1 recommendations).

- **Parser extension**: `CopyFaceFrom:` on the back face captures the
  referenced card name into `card["copy_face_from"]`. Front-face
  occurrences are ignored defensively.
- **Schema extension**: new `cards.copy_face_from TEXT` column so the
  second pass can find every carrier without re-parsing.
  **Re-import required for legacy DBs**: `schema.sql` uses
  `CREATE TABLE IF NOT EXISTS`, so existing `data/synergy.db` files
  miss the column. Rebuild via `uv run python scripts/import_cardsfolder.py`
  before running anything that calls `import_card`. Test DBs are
  unaffected — they use a freshly-opened schema via `open_db()`.
- **Importer extension (two-pass)**: after every `.txt` is imported,
  `resolve_copy_face_from_references(conn)` iterates carriers, copies
  every referenced-card `card_ports` row onto the carrier, and tags
  each inherited row with `port_attributes.attr_kind='via_copyfacefrom'`,
  `attr_value=<ReferencedCardName>`. Inherited rows skip
  `static AlternateMode` (defensive — never inherit a Prepared marker).
  Unresolved references warn + skip; self-references caught by a
  depth-1 cycle guard.
- **Production importer run**: 20 carriers, 20/20 resolved, 32 ports
  inherited (mean ~1.6 ports/carrier, max 3 for Reanimate-shape).
- **`bench.py audit`**: **POSITIVE**, aggregate Δ = **+0.0933** on the
  100-cmdr golden set. Histogram: 78 no_change / 20 rank_shuffle_within
  top30 / 2 rank_shuffle_across_top30_boundary / **0 hi_syn_loss**.
  hidden_gem_hit_rate stable at 0.8053 (Δ —).
- **Qualitative wins**:
  - **Nekusar, the Mindrazer** (+0.2446): gained Naktamun Lorespinner
    (`CopyFaceFrom:Wheel of Fortune`) — now ranked #14 alongside the
    canonical Wheels cluster (Wheel of Fortune, Windfall, Memory Jar,
    Whispering Madness, …).
  - **Niv-Mizzet, Parun** (+0.1151): instant/sorcery commander absorbs
    several inherited spell-shapes (Brainstorm carriers, etc.) into
    its trigger pool.
- **Notable non-win**: my brainstorm predicted Karador / Meren wins on
  the Reanimate-shape carriers. Audit shows ~0 movement — Karador's
  port set doesn't have the right trigger shapes for ChangeZone
  Graveyard→Battlefield to match. Cheap follow-up if needed: add a
  gy-recursion port to Karador-style commanders, or build a generic
  rule that fires on any "graveyard-to-battlefield ChangeZone" port.
- **Notable shift**: Tergrid -0.1917 (boundary shuffle, gained Nath of
  the Gilt-Leaf, lost Ferrafor, Young Yew — neither a CopyFaceFrom
  carrier). Pure IDF-recomputation collateral from the universe-wide
  port-count change. Within the POSITIVE-verdict envelope.

Fixture re-pinned via `bench.py audit --repin --yes`.

### `prepared_mechanic` rule weight tuned 1.0 → 3.0

Follow-up to the 2026-05-19 landing. The `recommend.py` qualitative check
on `Abigale, Poet Laureate` flagged that Prepared candidates scored
~0.187 (port_match) vs ~0.516 for generic WB staples (Bontu's /
Oketra's / Hazoret's / Rhonas's / Kefnet's Monuments + the medallion
cycle), so they sank to rank #50-60 below mana-rock noise. Set
`data/scoring_weights.json::rule_quality_multiplier.prepared_mechanic
= 3.0` to push them above the staple band.

- **`bench.py audit`**: Δ = **+0.0000** on the 100-cmdr golden set
  (100/100 no_change, hidden_gem_hit_rate stable at 0.8053). Golden-
  set-neutral because no commander in the 100-cmdr canonical triggers
  the rule (cheap path: no `AlternateMode|Prepare` static port; slow
  path: no `AlterAttribute → Prepared` effect port). Re-pinned the
  fixture for the new config_hash; underlying scores unchanged.
- **Qualitative validation**: `recommend.py "Abigale, Poet Laureate"
  --top 60`. All 17 WB-castable Prepared-payoff cards now occupy
  ranks **#1-17** (score 0.5); Bontu's Monument dropped from #1 to
  **#18**. Prepared cards now tie the staple band rather than under-
  shoot it, which is the intended ordering for a Prepared-payoff
  commander.

### `prepared_mechanic` rule_id literal inlined

Minor: `prepared.py` originally used `rule_id=_RULE_ID` (constant)
where the rest of `complement_rules/` uses inline `rule_id="..."`
literals. The dead-key-detector test
(`tests/test_scoring_weights.py::test_no_dead_rule_ids_in_quality_multiplier`)
scrapes literal strings via regex, so the constant form was invisible
to it. Inlined to match codebase convention and unblock the test.

### `K:ETBReplacement` SVar walking (LANDED, follow-up #3 to PR #47)

Closes the data-model gap on 400 cards that encode an ETB replacement
effect via the `K:ETBReplacement:Scope:SVarRef` keyword form. Today
`extract_keyword_ports` emits one thin keyword port per K: line with
the full replacement payload invisible — counter-doubler / tribal /
clone rules can't see Hardened Scales, Cavern of Souls, Reflections of
Kiki-Jiki, etc. Brainstorm:
`docs/brainstorms/2026-05-20-etb-replacement-svar-walking-requirements.md`.

- **Parser invariant**: new `etb_replacement` branch_kind registered in
  `parser_branch_kinds()` + `BRANCH_MULTIPLIER` (1.0× — ETB
  replacements are unconditional once the carrier is in play).
- **Ports extension**: new `extract_etb_replacement_ports(card_name,
  keyword_lines, svars)` parses each `K:ETBReplacement:Scope:SVarRef[
  :Mandatory|Optional[:Zone[:ValidFilter]]]` directive and walks the
  referenced SVar via the existing `walk_svar_chain`. Emits one effect
  port per ChainNode tagged with `branch_kind='etb_replacement'`,
  `source_svar=<ref>`, `is_optional` per the K: line, and a transient
  `_etb_scope='other'|'copy'` key.
- **Surface keyword port preserved**: today's thin
  `event_class='ETBReplacement:Scope:SVarRef:...'` keyword port stays
  for back-compat (no rule actually matches on it because the
  event_class is per-card-unique colon-joined string — but removing
  it would require auditing every grep for ETBReplacement).
- **`port_attributes` extension**: each inherited port gets a row with
  `attr_kind='etb_scope'`, `attr_value='other'|'copy'` so downstream
  rules can filter ETB-replacement-derived ports if needed. No v1
  consumer; data infrastructure for future tuning.
- **Production importer run**: 400 K:ETBReplacement directives parsed,
  400 root nodes emitted with `branch_kind='etb_replacement'`, ~1290
  additional sub-ability expansions through existing CHAIN_KEYS,
  524 `etb_scope` provenance tags. Total card_ports row count grew
  108,644 → 110,334 (+1,690).
- **`bench.py audit`**: **POSITIVE**, aggregate Δ = **+6.5477** on the
  100-cmdr golden set. Histogram: 75 no_change / 21
  rank_shuffle_within_top30 / 4 rank_shuffle_across_top30_boundary /
  **0 hi_syn_loss / 0 hi_syn_gain**.
  - **hidden_gem_hit_rate**: 0.8053 → **0.8153** (+0.0100 = +1
    hidden gem per commander on the second-axis metric).
- **Qualitative wins** (recommend.py verified):
  - **The Mimeoplasm** (+6.5971): top-30 reshapes around graveyard /
    counter / animator shapes. Top 10 now: Midnight Clock, Bloodcrazed
    Hoplite, Diabolic Servitude, Takklemaggot, Traveling Plague,
    Wormfang Newt, Flourishing Defenses, Wormfang Turtle, Hardened
    Scales, Ozolith. Diabolic Servitude (ETB-replaces with reanimate)
    and Hardened Scales (counter doubler) are textbook fits.
  - **Hamza, Guardian of Arashin** (+0.1549): gained Bramblewood
    Paragon, lost Lonis. Bramblewood Paragon is `K:ETBReplacement:
    Other:AddExtraCounter` for Warriors — now properly modelled.
- **Notable shifts within envelope**: Araumi -0.0635 (Urborg Lhurgoyf
  in, Dogmeat out); Locust God / Emry / Nekusar / others all
  <0.05 magnitude — IDF-recomputation noise. No hi_syn_loss
  anywhere.

Fixture re-pinned via `bench.py audit --repin --yes`.

## 2026-05-19

### `prepared_mechanic` rule (LANDED)

Captures the new `AlternateMode:Prepare` / `Attributes$ Prepared` mechanic
introduced by ~48 cards in the 2026-05-19 Forge refresh (SHA `f42b9abc1`).
See `docs/brainstorms/2026-05-19-prepared-mechanic-requirements.md`.

- **Importer extension**: `extract_alternate_mode_ports` emits a synthetic
  `static AlternateMode Prepare` port for every card with the top-level
  `AlternateMode:Prepare` header (47 cards). Restricted to the `Prepare`
  value via `_ALTERNATE_MODE_PORT_VALUES` frozenset so other AlternateMode
  values (DoubleFaced, Adventure, Split, Modal, Flip, Specialize, Omen,
  Meld) are intentionally **not** surfaced as ports — emitting for those
  perturbed the depth-2 cascade walker's Stage-1 relevant-event prefilter
  (caught Tergrid in the audit before the narrowing fix).
- **`port_attributes` extension**: every `AlterAttribute` effect port's
  `Attributes$ <V>` value is exploded into `port_attributes` with
  `attr_kind='attribute'`. Surfaces Prepared (29 ports), Suspected (25),
  Solved (15), Plotted (4), Commander (3), Saddled (3), Harnessed (2).
- **Rule**: `complement_rules/prepared.py::_find_prepared_mechanic_complements`.
  Dual-path commander detection: cheap path on the synthetic AlternateMode
  static port (covers all 47 Prepared payoff creatures including those
  that self-prepare via `K:ETBReplacement:Other:DBPrepare` which doesn't
  walk SVars), slow path via SQL join through `port_attributes` for
  AlterAttribute Prepared (covers enabler-only commanders like a future
  legendary version of Skycoach Waypoint).
- **`rule_quality_gate.py --rule prepared_mechanic`**: WARN
  (47 targets, cov=2.0, cv=0.613). Same shape as the documented-
  acceptable `ward_2_tribal` WARN — new-mechanic targets are
  intentionally thinly covered by existing rules.
- **`bench.py audit`**: aggregate Δ = **+0.0000** on the 100-cmdr golden
  set (100/100 no_change). Rule does not fire on historical commanders
  by design — none of the 100 are Prepared payoff cards.
- **Qualitative validation**: `recommend.py "Abigale, Poet Laureate"
  --top 60` shows the rule contributing `port_match = 0.180` per
  Prepared candidate (Bloodline Recollector, Adventurous Eater,
  Cheerful Osteomancer, Defacing Duskmage, Emeritus of Truce/Woe,
  Spiritcall Enthusiast, …). Total scores sit at ~0.187 vs ~0.516 for
  generic WB staples (Bontu's Monument); rule magnitude can be tuned
  via `_RULE_QUALITY_MULTIPLIER` in a follow-up if Prepared commanders
  should weight Prepared-tribal cards above generic mana-rock staples.

## 2026-04-23

### Forge-Second-Oracle design-time pipeline (LANDED, plan 2026-04-23-002)

**No complement rules added.** This is pure rule-authoring tooling —
the scoring path is bitwise-unchanged (`bench.py audit
--expect-identity` PASS before and after every unit).

- New `src/mtg_synergy_graph/forge_oracle/` package: offline-only,
  grep-fenced from the inference path (see
  `tests/test_forge_oracle_isolation.py`).
- **`pair_scorer.rate_pair(conn, a, b)`** — Python port of Forge
  `CardRanker.getScoreForDeckHints` at SHA
  `ed97d9bb77f03d9681aba59186416bcf7923d5dd`, 273 LOC (hard cap
  500). Reads the normalized `card_hints(kind, category, value)`
  rows the importer already populates from Forge's
  `DeckHints`/`DeckNeeds` SVars, so the port never re-parses the
  raw `TYPE$param` format.
- **`scripts/forge_oracle.py build`** — walks
  `data/forge/forge-gui/res/quest/{commanderprecons,precons}/` (667
  `.dck` files), aggregates pair co-occurrence over
  `port_nodes.subkind` pairs, computes RAPM-lineup-adjusted +
  Laplace-smoothed PPMI, writes to `data/forge_oracle.db`
  (`forge_precon_ppmi` + `oracle_config`).
- **`OracleConfigInputs` + `compute_oracle_hash`** — mirrors the
  inference-path `ScoringConfigInputs` pattern but applied offline.
  Strict consumers (`bench.py audit --vs-forge-oracle`,
  `forge_oracle.py propose-rules`) exit 2 on stale hash; soft
  consumer (`gap_report.py`) silently falls back to
  `forge_signal = 1.0` so the rule-authoring tool always produces a
  report.
- **`gap_report.py` re-ranks by `impact * forge_signal`** — gaps
  whose subkind has a strong Forge PPMI signal rise in the queue.
  Sort-key change is monotonic under `forge_signal = 1.0`, so
  missing sidecar preserves pre-change order bitwise.
- **`bench.py audit --vs-forge-oracle`** — Kendall-τ sidecar. For
  each commander, scores the same top-N candidates both ways and
  reports aggregate mean τ + per-commander breakdown + top-10
  divergences (seeds for future rule proposals). Tracking-only at
  MVP — gate stays on NDCG histogram.
- **`forge_oracle.py propose-rules --top N`** — iterates top-N
  forge-signal-ranked gaps, delegates to `scaffold_rule._GENERATORS`
  per-template, emits scaffold previews ready for the existing
  scaffold → audit → human-review loop.
- **Sparse-checkout extended** to pull `forge-gui/src/...` +
  `forge-ai/` + `quest/{commanderprecons,precons}/`. SHA pinned in
  committed `data/forge_oracle/version.txt`.
- **Spike verdict** (`docs/spikes/2026-04-23-boosterdraft-port-feasibility.md`):
  the brainstorm hypothesized `BoosterDraftAI.rateCard` or
  `CardSynergy.getSynergy` as the port target. Spike found
  `CardRanker.getScoreForDeckHints` is the actual pair-scoring
  surface (~30 LOC of math over `DeckHints`/`DeckNeeds`
  annotations) — `CardSynergy` does not exist;
  `BoosterDraftAI.java` is a 110-LOC thin wrapper delegating to
  `CardRanker`. Port delivered at ~100-150 LOC core, well under the
  500-LOC FR5 cap.
- **Testing**: 100+ new test cases across 10 test files. Full
  suite: 1573 passed (pre-plan-002: 1435), coverage 87%+.
- **Institutional pattern** extracted as
  `docs/solutions/best-practices/offline-oracle-hash-pattern-2026-04-23.md`
  for future offline subsystems.

---

### `self_bridging_cascade` depth-2 pathway rule (LANDED, plan 2026-04-23-001)

- New complement-rule family `self_bridging_cascade` ships
  `_ENABLE_PATHWAY_RULES = True`. Fires when a candidate has >=2 ports
  each matching a commander port AND two of those ports form an
  internal length-<=2 edge via a canonical cascade substrate.
- **Two channels after audit tuning**: the walker accepts
  `event_match` (named trigger->effect entries in `EVENT_MATCH_MAP`,
  wildcard `*` rejected) and `cost_feeds` (`COST_FEEDS_TRIGGER`).
  The brainstorm proposed a third `valid_filter` channel but it was
  dropped mid-audit (see below).
- **Audit verdict**: POSITIVE at three successively narrower walker
  configurations; hidden_gem_hit_rate identical at 0.8423 across all
  three (up from 0.7287 baseline, +0.1136 on the stated goal metric).

  | variant | agg Δ | no_change | hi_syn_loss | hidden_gem |
  |---|---|---|---|---|
  | v1: 3 channels (event_match + cost_feeds + valid_filter) | +463.7 | 14 | 0 | 0.8423 |
  | v2: 2 channels (drop valid_filter) | +377.4 | 16 | 0 | 0.8423 |
  | v3: 2 channels, wildcard `*` rejected (LANDED) | +209.3 | 36 | 0 | 0.8423 |

- **Why valid_filter was dropped**: fired on voltron / proliferate /
  monarch commanders whose typed triggers matched equipment-aura
  cascade shapes that weren't genuine internal edges. Qualitative
  collateral (Rafiq + Strength-Testing Hammer type of drift) without
  improving the hidden-gem metric.
- **Why wildcard (`*`) was dropped**: `Attacks->*` /
  `SpellCast->*` / `LandPlayed->*` created spurious internal edges
  on cards where the port extractor splits a single ability into
  `trigger` + `effect` ports (e.g., equipment with
  "equipped creature attacks -> pump"). The landed walker
  (`_canonical_trigger_effect`) rejects wildcard entries; the public
  `graph_engine.match_event` still honors them for commander-vs-
  candidate matching where wildcard semantics are appropriate.
- **Target commander impact** (plan FR1): Korvold +8.98, Gitrog +3.51,
  Meren +1.98, Muldrotha/Teysa low-magnitude. All positive deltas.
- **No regressions**: zero commanders with hi_syn_loss, zero
  commanders with score_delta < -0.05 (all 100 commanders landed
  positive or no_change).
- **Config hash invalidation**: `enable_pathway_rules` added to
  `ScoringConfigInputs`; flipping the flag shifts
  `compute_config_hash()` so stale tensor rows are refused. Pinned
  fixture re-pinned at flag=True.
- Plan: `docs/plans/2026-04-23-001-feat-self-bridging-cascade-pathway-plan.md`.
- Seed commits: 594873f (walker, Unit 1), Unit 2, Unit 3, Unit 4, Unit 6.

**Post-review fix pass (2026-04-23, same day).** `ce-code-review` surfaced
three P1 findings:
1. `SynergyEngine.score_one` called `_render_explanation` without the
   `UniversalScore` argument, so `self_bridging_cascade:` lines were
   silently dropped via that public entry point. Fixed.
2. `stax_excluded` was built in `find_all_complements` but never
   threaded to `_find_self_bridging_cascade`; stax cards fired the rule
   for affected commanders. Fixed by extending the helper signature.
3. Plan's Unit 2 profiling step was never run. Measurement at flag=True
   against `data/synergy.db` showed pathway overhead: Korvold 15 ms ->
   345 ms (+2084%), Gitrog +203%, Yawgmoth +262%. The fix adds
   `CandidateCache.ports_by_card` as the plan pre-authorised, plus a
   shape memo on commander-port matching in Stage-3. Post-fix overhead:
   Korvold +725%, Gitrog +112%, Yawgmoth +165%, Rafiq +97%. Absolute
   per-page costs: Korvold 113 ms, Gitrog 30 ms, Yawgmoth 22 ms,
   Rafiq 12 ms.

Also applied: Stage-1 SQL now uses `COUNT(DISTINCT port_type || '|' ||
event_class) >= 2` to match the plan spec (previously the single-column
form silently dropped cards with same event_class across different
port_types). Dead `seen` set removed. Redundant
`_cand_port_matches_any_cmdr` calls after walker return eliminated.
Docstring on `_port_pair_matches` corrected to describe the intentional
M-set / internal-edge asymmetry (M accepts 3 channels, walker accepts
2). `_render_explanation` universal_score plumbed through `score_one()`.
Plan file Implementation Unit checkboxes updated to `- [x]`.

Post-fix audit verdict stayed POSITIVE with aggregate Δ +0.74 over the
pre-fix flag=True baseline (small positive drift from the concat-distinct
admission fix). Zero hi_syn_loss. hidden_gem_hit_rate 0.7710 (vs 0.7287
baseline, +0.0423 kept). Fixture re-pinned at post-fix baseline.

**Performance optimization pass (2026-04-23, same day).** `cProfile` run
on Korvold (1336 firings, ~20 cmdr_ports) showed `_valid_filter_edge ->
_type_token_set -> _changezone_type_set` consumed 344 ms of the 1036 ms
pathway total — 33% of cost on a pure function of ~1-2k unique filter
strings called 158k times per page. Added `functools.cache` on
`_type_token_set` (98% hit rate in production) plus a short-circuit in
`_valid_filter_edge` for ports lacking a `valid_filter`. A commander
event-class index was prototyped but rejected because it changed the
first-match iteration order; the cmdr_event label depends on source-order
traversal of `cmdr_ports`, so any prune that reorders them shifts the
IDF dedup key and breaks `--expect-identity`.

Final per-page overhead (vs flag=False baseline):
  commander   raw     +cache  +cache+memo   final
  Korvold     345 ms  275 ms   128 ms      114 ms   (67% reduction)
  Gitrog       91 ms   76 ms    56 ms       55 ms
  Yawgmoth     76 ms   57 ms    36 ms       36 ms
  Rafiq         —      —        24 ms       24 ms
  Animar        —      —        —           20 ms
  Tergrid       —      —        —          165 ms
  avg         ~120 ms              ~60 ms   ~69 ms

Still above the plan's 10% target on broad commanders (Korvold: +600%)
but the absolute cost is tolerable for interactive use and the
optimization diminishing-returns ceiling has been reached without a
semantic change. Fixture re-pinned and `--expect-identity` PASS.

## 2026-04-22

### IDF reforms plan 002 (BM25F + conditional denominator — not landed)

- Prior infrastructure-only branch `feat/idf-reforms-bm25f-conditional`
  explored BM25F length-normalization and conditional color-pool
  IDF denominators. Both reforms audited as HARMFUL on the
  100-commander golden set (best BM25F +0.0016 agg with 15 losers
  ≥0.05; conditional −0.0142 agg with 18 losers). Defaults stayed
  legacy/global; code preserved on a local branch for reference.
  See plan 002 (docs/plans/2026-04-22-002-feat-idf-reforms-bm25f-
  conditional-plan.md on the abandoned branch) for the full sweep
  data.

### Typed port graph plan 003 (substrate + 16-rule POC migration, LANDED)

- Data-layer refactor: canonical `NODE_KINDS` vocabulary,
  `port_nodes` SQL view, `event_match_map` / `cost_feeds_trigger`
  tables seeded from `data/event_match_seed.json`, `rules` table
  seeded from `data/rules_seed.json`, and a `RuleInterpreter` that
  compiles JSON predicate trees to SQL + Python gate callables.
- 16 auto-generated tribal / replacement-stack rules migrated
  from Python helpers to declarative JSON rows — `cascade_tribal`
  plus the 13 other keyword-tribals plus 2 replacement-stack rules.
  `src/mtg_synergy_graph/complement_rules/generated/` shrinks from
  16 files (~1,100 LOC) to just `__init__.py`; the same rule set
  fits in a 375-line seed JSON.
- **Identity-preserving**: aggregate NDCG@30 bitwise-identical to
  10 decimals (0.262219416007 before and after); zero commanders
  with any per-commander delta > 1e-12; `bench.py audit
  --expect-identity` PASS on every unit of the plan.
- New `bench.py audit --unknowns` reports port-graph subkinds
  that fell through to `UNKNOWN` classification — 375 distinct
  subkinds across 29,711 prod-DB cards, ranked by
  distinct_cards × EDHREC rank weight. Candidates for future
  vocabulary expansion: `effect.Pump`, `effect.Cleanup`,
  `trigger.Phase`, `effect.Effect`, `effect.LoseLife` (top 5 by
  rank weight).
- Scope boundary: FR8 at scale (migrate ~28 non-tribal rules) and
  FR9 (rewrite `scripts/scaffold_rule.py` to emit JSON rows) are
  explicitly deferred to a follow-up plan — gated on this
  infrastructure proving out on the POC migrations (which it did).
- Vocabulary bump to v2: added `zone_dest_battlefield`
  match_quality to preserve the exact semantics of the legacy
  inline lambdas under `ChangesZone` (Token / CopyPermanent /
  Animate). The lambda checks only trigger side; mapping to
  `zone_compatible` would drift on Graveyard-triggered rows.
- Deferred follow-ups recorded in the plan's Open Questions:
  per-port auditor attribution for declarative rules,
  `card_hints` revisit, `scales_with.value` vs `scales_with.valid`
  field split for future BM25F revisits.

### Useful-disagreement plan 003-gem (`hidden_gem_hit_rate`, tracking-only)

- Second evaluation axis for `bench.py audit`:
  `hidden_gem_hit_rate = |plausible_hidden| / 30` where
  `plausible_hidden = our_top_30 \ edhrec_top_30` filtered by the
  mechanical-plausibility gate (N_rules_firing ≥ 2 OR
  total_contribution > per-commander-median, strict inequality).
  Operationalizes `memory/feedback_edhrec_not_goal.md`.
- Six implementation units landed on `feat/hidden-gem-metric`:
  (1) pure metric core in `bench/hidden_gems.py`,
  (2) `build_fixture` writes per-commander rate + hidden_cards into
  `FixtureEntry.legacy` when an EDHREC DB conn is provided,
  (3) `AuditReport` surfaces aggregate + delta + FR4 stderr warning
  when delta drops below `-_HIDDEN_GEM_WARN_THRESHOLD` (= −0.02),
  (4) `bench.py audit --inspect-gems` per-commander lost/gained
  diff (Δ as integer count out of 30),
  (5) `.audit/history.csv` append-on-every-audit + `bench.py audit
  --trend hidden_gems` CSV reader with md/json format support,
  (6) docs + FR6 escalation block in `hidden_gems.py` docstring.
- **Tracking-only at MVP.** Existing histogram-based commit gate is
  unchanged; the new warning is advisory. Promotion to a commit-
  gate requires a separate brainstorm + plan per FR6: ≥20 commits
  of tracking + human correlation + <10% false-positive rate.
- **Identity-preserving.** `bench.py audit --expect-identity` PASS
  on every unit — gem fields live in `legacy`, the `scores` dict
  is untouched.
- Post-landing validation plan: manually replay `hidden_gem_hit_rate`
  against three historical reverted experiments (broad
  `gy_retrieval`, deck-hint-match, Survivor 3 BM25F branch) to
  confirm at least one produces a ≤0 retrospective delta. Finding
  to be recorded as a memory note alongside
  `memory/project_reanimator_hisyn_gap.md`.

## 2026-04-21

### creature_died_feeder

- New rule for aristocrats-archetype commanders whose payoff scales
  with Forge's `Count$ThisTurnEntered_Graveyard_from_Battlefield_Creature`
  SVar (count of creatures that died this turn). Covers all filter
  variants via LIKE prefix-match — plain / `.YouCtrl` / `.YouOwn` /
  `.!token` / `.!namedSelf` / `.!token+YouCtrl`.
- 15 legendary cmdrs, 0% prior coverage on this axis: Asmira Holy
  Avenger, Bontu the Glorified, Denethor Ruling Steward, Ebondeath
  Dracolich, Faramir Field Commander, Gadrak Crown-Scourge, Gimli
  Mournful Avenger, Inga Rune-Eyes, Kuon Ogre Ascendant, Lagomos Hand
  of Hatred, Mahadi Emporium Master, Nevinyrral Urborg Tyrant,
  Shessra Death's Whisper, Sméagol Helpful Guide, Tobias Doomed
  Conqueror.
- Single tier `creature_died_peer` pulls the ~49 non-legendary cards
  on the same axis — Feast of the Victorious Dead, Fresh Meat,
  Caller of the Claw, Deathreap Ritual, Grizzly Ghoul, Khabál Ghoul,
  Tallyman of Nurgle, Liliana's Devotee / Scrounger / Standard
  Bearer, Warlock Class, Ichor Shade, Rise of the Dread Marn, Osai
  Vultures, Vile Redeemer, Spoils of Blood, Body Count, Spymaster's
  Vault, Séance Board, Season of Loss. Pool is mechanically uniform
  with the cmdrs — same peer-shape pattern as party_feeder.
- Audit verdict **positive**: 12 scored, +5 hi_syn hits, **+0.301
  NDCG aggregate**. Top movers: Shessra +0.141 (+2 hits), Denethor
  +0.069 (+1), Bontu +0.061, Mahadi +0.059 (+2). Only regression
  Nevinyrral -0.038 (under 0.05 tolerance).
- Multiplier 2.5× (matches party_feeder and other single-axis
  feeders). No archetype exclusions needed — the aristocrats archetype
  is uniform across all 15 cmdrs.
- Sibling-revert note: tried `gy_creature_count_feeder` (gap #22,
  scales_with.ValidGraveyard Creature.YouOwn) earlier today —
  reverted TRIVIAL because that 77-card peer pool included fringe
  scalers (Boneyard Wurm, Soulshriek, Nightstalker Engine) which
  aren't EDHREC hi_syn, and Bladewing regressed -0.128 (Zombie Knight
  deck displaced by generic GY peers). creature_died_feeder avoids
  that trap because the 49-peer pool IS the aristocrats staple set —
  Feast / Fresh Meat / Deathreap Ritual ARE EDHREC hi_syn for every
  sac-death-trigger deck.

### party_feeder

- New rule for Party-count commanders (Zendikar Rising / Baldur's
  Gate / Final Fantasy Party mechanic). Forge's `Count$Party` SVar
  returns 1-4 = distinct count of Cleric / Rogue / Warrior / Wizard
  creatures you control, capped at one of each. 9 legendary cmdrs,
  0% prior coverage: Burakos Party Leader, Linvala Shield of Sea
  Gate, Nalia de'Arnise, Tazri Beacon of Unity, The Destined Black
  Mage / Thief / Warrior / White Mage, Zagras Thief of Heartbeats.
- Single tier `party_peer` pulls all other ~34 cards with
  `scales_with.Party` — Acquisitions Expert, Allied Assault,
  Archpriest of Iona, Ardent Electromancer, Cascade Seer, Coveted
  Prize, Deadly Alliance, Drana's Silencer, Emeria Captain, Grotag
  Bug-Catcher, Journey to Oblivion, Kabira Outrider, Malakir Blood-
  Priest, Multiclass Baldric, Nimble Trapfinder, Ravager's Mace,
  Sea Gate Colossus, Seafloor Stalker, Spoils of Adventure, Squad
  Commander, Synchronized Spellcraft, Thundering Sparkmage, Thwart
  the Grave, Veteran Adventurer. Pool is mechanically identical to
  the cmdrs — strongest possible archetype signal.
- Audit verdict **positive**: 9 touched, 9 scored, **+14 hi_syn
  hits**, **+1.607 NDCG aggregate** (biggest single-rule lift in
  session). Zero regressions (ndcgmin +0.000). Top movers: Linvala
  Shield of Sea Gate +0.332 (+3 hi_syn), Burakos +0.321 (+3), The
  Destined Black Mage +0.241 (+2), Zagras +0.164 (+2), Nalia
  +0.142 (+2).
- Multiplier 2.5× (matches other narrow single-axis feeders). No
  need for archetype exclusion — the Party mechanic is mechanically
  uniform across all 9 cmdrs.

### etb_tapped_stax_feeder

- New rule for stax / pillowfort commanders whose `replacement.Moved`
  port forces EXTERNAL permanents to ETB tapped. 7 legendary cmdrs,
  0% prior coverage: Reidane (opp snow lands), Spider-Woman (opp
  artifacts + creatures), Thalia and The Gitrog Monster and Thalia
  Heretic Cathar (opp creatures + non-basic lands), Urabrask the
  Hidden (opp creatures), Zhao the Moon Slayer (non-basic lands),
  Archelos Lagoon Mystic (all permanents while Self tapped).
- Single-tier `etb_tapped_stax_peer` pulls the ~24 other cards with
  the same mechanical shape: Authority of the Consuls, Kismet, Blind
  Obedience, Loxodon Gatekeeper, Kinjalli's Sunwing, Imposing
  Sovereign, Manglehorn, Dauntless Dismantler, Archon of Emeria,
  Phyrexian Censor, Frozen Aether, Orb of Dreams, Root Maze, False
  Floor, Radiant Grace, Ashling's Prerogative — all EDHREC stax
  staples.
- Gate explicitly rejects Card.Self (~542 cards: every tapped land
  plus creatures like Grimgrin / Ebondeath / Alirios / Taeko whose
  "ETBs tapped" is a drawback, not a stax tool — covered by
  sacrifice_outlets / graveyard_filler / etb_self).
- Gap report context: this sub-cell was `replacement.Moved[ETBTapped]`
  at impact 14 (7 external cmdrs + 7 self-ETBTapped creature cmdrs
  that are out of scope).
- Audit verdict **positive**: 7 touched, 7 scored, +6 hi_syn hits,
  **+0.725 NDCG aggregate**. Spider-Woman +0.267 (0→4 hi_syn), Thalia
  Heretic Cathar +0.297 (0→2 hi_syn), Reidane +0.181, Zhao +0.016.
  Only wobble Thalia+Gitrog -0.031 (well under 0.05 tolerance).
  Golden unchanged at 0.2566.
- Multiplier 2.5× (matches other narrow single-axis feeders — tight
  pool of ~24, IDF ~0.22 per match, effective ~0.55).

## 2026-04-20

### land_bounce_feeder

- New rule for commanders whose activated ability costs a land-return
  (Meloku, Mina and Denn, Multani, Sutina, Uyo — 5 scored, 7 touched
  before archetype exclusion, 0% prior coverage). Two deduped tiers:
  - `land_bounce_extra_drops` (~38): `static.Continuous` with
    `AdjustLandPlays` — Azusa, Exploration, Oracle of Mul Daya,
    Dryad of the Ilysian Grove, Fastbond, Ghirapur Orrery,
    Rites of Flourishing, Burgeoning, Flubs (the Fool), Hugs,
    Aesi Tyrant of Gyre Strait. Turns the land-bounce into a neutral
    tempo play (replay the bounced land, still land drop).
  - `land_bounce_gy_recur` (~56): `effect.ChangeZone` with
    `zone_origin='Graveyard'` and `valid_filter` containing `Land`,
    rejecting opponent-targeting — Crucible of Worlds, Ramunap
    Excavator, Splendid Reclamation, World Shaper, Life from the
    Loam, Emeria Shepherd, Lord Windgrace, Molderhulk. Compounds
    with the bounce loop.
- Archetype exclusion: cmdrs with `scales_with.xPaid` (Tameshi —
  X-cost flicker engine) or `scales_with.ValidHand Card.YouOwn`
  (Soramaro — big-hand payoff) are dropped from the rule. Initial
  draft without these exclusions regressed Soramaro -0.139 NDCG and
  Tameshi -0.056 NDCG — their land-return cost is incidental to a
  different primary engine, and generic AdjustLandPlays / GY-land-
  recur feeds displaced their real archetype picks.
- Audit verdict TRIVIAL by hi_syn hit count (Sutina +1 / Mina -1
  cancel at the net level) but NDCG@30 aggregate **+0.235** (Sutina
  +0.095, Mina +0.060, Uyo +0.051, Multani +0.029, Meloku small).
  Zero per-commander regressions (ndcgmin +0.000), golden unchanged.
  Multiplier 2.5×.

### life_total_feeder (after a reverted first attempt)

- **First attempt, reverted (commit ec67250)**: Gate was
  `scales_with.YourLifeTotal + any of {Lifelink, GainLife on 'You',
  replacement.GainLife}`. Fed 27 peer cards (Serra Ascendant, Angel
  of Vitality, Divinity of Pride, etc.) to all 8 YourLifeTotal
  commanders. Audit verdict HARMFUL: -2 hi_syn hits / -0.017 NDCG
  across 7 active cmdrs. Elenda +0.102 and Bilbo +0.067 were offset
  by regressions on Ayli (-0.101), Jerren (-0.060), Linvala (-0.026),
  Cecil (-0.013). Root cause: YourLifeTotal axis is heterogeneous —
  Ayli reads life as an exile-power cap (query variable), Bane's LEX
  threshold makes him indestructible at LOW life, Cecil/Jerren flip
  on life-total thresholds, Linvala's angel token count scales with
  life. Generic lifegain peers displaced their mechanical picks.
- **Narrowed gate, shipped**: Commander must additionally carry an
  up-biased lifegain signal — `replacement.GainLife` amp on self
  (ValidPlayer 'You', not Prevent) OR `static.Continuous` whose
  raw_line contains `SVarCompare: GT*` / `GE*` (up-biased life
  threshold). Rejects Bane (`LEX`), Ayli / Beza / Cecil / Jerren /
  Linvala (no such static). Only Bilbo (GainLife doubler) and Elenda
  (+1/+1 when life > starting, +5/+5 when life ≥ +10) remain.
- Peer pool: other cards with `scales_with.YourLifeTotal` that ALSO
  satisfy the symmetric positive-life filter (Lifelink / GainLife on
  'You' / replacement.GainLife), 27 cards.
- Audit verdict TRIVIAL by hi_syn hit count (neither target hits
  EDHREC's top-30 hi-syn list), but **NDCG@30 +0.169 aggregate**
  (Elenda +0.102 Bilbo +0.067). Golden-set aggregate NDCG unchanged
  (0.255904 ⇄ 0.255904 stash test) — no indirect IDF regression.
  Multiplier 2.5×.

## 2026-04-19

### Audit-driven cleanup

Deleted 9 net-negative or dead rules (`etb_sac_target`, `power_matters`,
`token_sac_chain`, `pan_density`, `token_etb_damage` → kept as
CONTENTIOUS, `damage_synergy`, `counter_producer`, `pinger`,
`peer_evasion_tribal`, `yard_caster`) for an aggregate NDCG lift of
+0.0049.

### cardpower_axis_feeder

- New general rule for the 67 legendary-creature commanders whose
  `SVar:X:Count$CardPower` scales an ability with their own power
  (Combustion Man's damage = power, Krenko TSK's Goblin token count,
  Carmen / Alesha / Ayesha Tanaka's cmcLEX reanimate/Dig cap,
  Inferno of the Star Mounts's charge-up to 20). `Count$CardPower`
  resolves to the commander's own power — different axis from
  `TotalPower` / `greatestPower` which scan the board. The deleted
  `power_matters` rule conflated the two and fed high-power creatures
  to every scales_with Power commander; this rule targets only
  CardPower and feeds **commander-pumping** cards.
- Two deduped tiers (highest priority wins per card):
  - `cardpower_big_attachment`: Equipment/Aura with static Continuous
    `AddPower ≥ 3` OR `AddPower = X/Y/Z` (scaling SVar). ~220 cards —
    Colossus Hammer, Eldrazi Conscription, Grafted Wargear, Kaldra
    Compleat. +1 / +2 trinkets dropped (not meaningful pumps).
  - `cardpower_p1p1_producer`: `effect=PutCounter[All] P1P1` on
    Creature target (not Self); drops self-sac-only distributors via
    `_only_self_sac_cost`. ~400 cards (Rishkar, Drana). Grower
    archetypes (Alesha / Carmen / Krenko TSK / Agatha all put P1P1
    counters on themselves as part of their triggered chain)
    compound with external producers; non-grower CardPower
    commanders still benefit because a P1P1 counter on the commander
    raises the count.
- Disjoint from `voltron` (4 of 67 overlap, gated on
  Hexproof/Exalted/Shroud/Trample) and from `modified_axis_feeder` /
  `counter_axis_feeder` (2 overlap each, require explicit qualifiers).
- Per-rule audit: `positive` verdict — 59 commanders touched,
  ndcgΣ +0.707 with max +0.244 (Raubahn +3 hits, +0.244 NDCG),
  Ayesha Tanaka +0.208, Ian the Reckless +0.139, Combustion Man
  +0.120. One regression: Velomachus Lorehold -0.222 (her
  EDHREC Hi-Syn values high-CMC Instants/Sorceries she cheats via
  Play.cmcLEX, which now yield top-30 slots to Equipment); net
  remains strongly positive. Multiplier 2.5× (one notch below
  counter/modified's 3.0× because the attachment tier partially
  overlaps with the general voltron pool). Golden NDCG unchanged
  at 0.25105. 13 new tests; 972 total.

### tap_type_feeder

- New rule for the 27 legendary-creature commanders with a
  `cost.tap_type` port (`tapXType<N/SUBJECT>`) — Azami (tap Wizard),
  Urza (Artifact), Aryel (Knight), Kumena (Merfolk), Lathril (Elf),
  Apothecary White (Food), Baylen (Permanent.token), Caparocti
  (Artifact;Creature). Every tap-cost commander wants to fire the
  cost TWICE per rotation, so the universal reward is a sustained
  untap engine.
- Axis-aware via `_classify_tap_type_axis`: extract SUBJECT from
  raw_line, classify as creature / artifact / permanent. Creature-
  taps (Azami) get Seedborn Muse / Prophet of Kruphix / Murkfiend
  Liege but NOT Unwinding Clock. Artifact-taps (Urza) get Unwinding
  Clock + Seedborn Muse (Permanent-subsuming) but NOT Drumbellower.
  Permanent-taps (Baylen) match everything.
- Two tiers (deduped, tier 1 wins):
  - `tap_type_sustained_untap`: static.UntapOtherPlayer whose
    ValidCard matches the axis, non-Self. ~10 per axis.
    Archetype-defining — Seedborn Muse et al.
  - `tap_type_phase_untap`: trigger.Phase + effect.UntapAll on
    axis-matching valid_filter. ~10 per axis. Awakening, White
    Plume Adventurer, Virtue of Loyalty.
- First draft at 3.0x multiplier flooded Aryel/Kumena top-30 with
  untaps and displaced tribal Hi-Syn picks (Aryel -0.167 NDCG,
  Kumena -0.107). Subject-aware filter cut that to -0.039 / +0.004.
  Multiplier lowered to 2.0x (vs counter/modified's 3.0x) because
  the pool is already tight — 20 cards total per axis — so IDF
  ~0.29 per match is premium on its own.
- Per-rule audit verdict: MARGINAL (+0.101 NDCG sum, +1 hit net,
  ratio 0.004 < 0.1 positive threshold). Top lifts: Kirol +0.138,
  Belisarius Cawl +0.080, Shao Jun +0.018. Golden NDCG unchanged
  at 0.25113. 20 new tests (9 axis classifier + 11 rule). 992 total.

### hand_size_feeder

- New rule for the 24 big-hand commanders with a `scales_with
  ValidHand Card.YouOwn` port whose mechanic rewards LARGE hands
  (Alandra Drakes pump, Damia refill-to-7, Kefnet attack-if-7+,
  Tishana P/T=hand, Soramaro / Kagemaro / Syr Elenora / Alrund /
  Jin-Gitaxias / Kozilek / Doctor Octopus / Duggan / Mr. Foxglove
  / Krang / Leonardo da Vinci).
- The axis is BIDIRECTIONAL — 4 commanders (Hazoret, Neheb,
  Djeru and Hazoret, Flubs the Fool) want EMPTY hands. Feeding
  them SetMaxHandSize: Unlimited staples would be anti-synergy.
  `_is_big_hand_commander` rejects them via small-hand SVarCompare
  signals (`LE0`/`LE1`/`EQ0` fires-on-empty, `GE2`/`GE3` pairs
  with CantAttack/CantBlock) on the hand-binding SVar (extracted
  from `SVar:<X>:Count$ValidHand Card.YouOwn` bindings).
- Single tier: `hand_size_no_max` — static.Continuous with
  `SetMaxHandSize: 'Unlimited'` (~46 cards: Reliquary Tower,
  Thought Vessel, Library of Leng, Spellbook, Venser's Journal,
  Decanter of Endless Water, Folio of Fancies, The Magic Mirror).
  Narrow, archetype-defining — these remove the end-of-turn
  discard cap that would otherwise pin the hand-size axis at 7.
- Per-rule audit verdict: positive. 24 commanders touched, ndcgΣ
  +2.686 (largest per-rule lift ever). Top wins: Soramaro +4 hits
  +0.397 NDCG, Kagemaro +3 +0.349, Syr Elenora +3 +0.335, Kefnet
  +2 +0.272, Alrund +2 +0.270. One regression: Damia -0.194
  (her 79-candidate pool is tiny so 46 new hand-size cards
  displace 7 generic on-page staples — net Hi-Syn unchanged at
  0/10 but on-page drops from 9 → 2). All 4 small-hand
  commanders correctly rejected — zero false positives in the
  gate. Golden set NDCG unchanged at 0.25113.
- 15 new tests — 8 for `_is_big_hand_commander` (no SVar,
  default big-hand, Hazoret GE2, Neheb LE1, Flubs EQ0 via branch,
  Damia LT7 big-hand, Jin-Gitaxias GE7 big-hand, non-hand-SVar
  compare ignored) and 7 for the rule (gate, Reliquary Tower
  surfaces, Hazoret skipped, fixed max rejected, non-static
  Effect rejected, commander self-exclusion, rule_id). 1007
  total tests, 82% coverage.

### gy_fuel_feeder

- New rule for the 18 commanders with `cost.exile_from_grave` +
  `cost_target='any'` — Araumi (golden anchor), Aphemia, Ashnod,
  Drivnod, Egon, Gorex, Ishkanah, Kethis, Osgir, Ultimecia,
  Varina, Winter, Kroxa and Kunoros, Ludevic, Taigam, Tawnos,
  Baron Zemo, Capitoline Triad.
- Archetype: pay by exiling graveyard cards → reward is self-mill
  (more cards in GY = more fuel for the cost).
- Single tier `gy_fuel_self_mill`: `effect.Mill` with
  `Defined: 'You'`, `NumCards >= 3` OR scaling `X/Y/Z`. ~100
  cards (Aftermath Analyst, Altar of Dementia, Hedron Crab,
  Ashiok Nightmare Weaver, Mesmeric Orb, Sphinx's Tutelage).
  Rejects Opponent / EachPlayer targets.
- Initial draft at NumCards >= 2 flooded Osgir's top-30 with
  cantrip-mills and pushed her archetype artifact picks out
  (Osgir golden NDCG 0.3458 → 0.2528 = -0.093, Ultimecia
  -0.436). Tightening to NumCards >= 3 cut Osgir drop to -0.022
  and flipped the audit verdict from CONTENTIOUS to positive.
- Self-target escape-style commanders (`cost_target='self'`:
  Wilson, Symbiote Spider-Man, Tocasia, Venom, Morbius,
  Spider-Slayer, Beetle) excluded at the gate — they want
  die-triggers and sac outlets, a different archetype.
- Per-rule audit verdict: positive. 18 commanders touched,
  +1 hit net, ndcgΣ -0.250 (the Ultimecia / Varina -0.441 /
  -0.183 rank-shuffle regressions exceed the other lifts), BUT
  the safety net catches Araumi on the golden set: without the
  rule, Araumi would drop -0.236 NDCG. Net golden aggregate:
  0.251127 → 0.253267 (+0.0021). Top non-golden wins: Egon +2
  hits +0.257, Gorex +2 hits +0.156.
- 10 new tests: gate (no exile cost, self-target excluded),
  tier matching (integer N, scaling SVar, N=2 rejected,
  Opponent rejected, EachPlayer rejected, non-Defined-You
  rejected), commander self-exclusion, rule_id. 1017 total
  tests, 82% coverage.

### lifegain_feeder

- New rule for the 21 commanders with `scales_with
  LifeYouGainedThisTurn` — Celestine, Aerith, Astarion, Bre,
  Haliya, Hope Estheim, Frodo, Gollum, Gwaihir, Lathiel (golden
  anchor), Licia, Saint Elenda, Sorin House Markov, Willowdusk,
  Will Scion of Peace, plus 6 more. Monotonic-positive axis —
  no bidirectional filter.
- Two tiers (deduped, tier 1 wins):
  - `lifegain_amp`: `replacement.GainLife` with
    `ValidPlayer: 'You'`, non-Prevent, amp ReplaceWith
    (`GainDouble` / `GainLife` / `ReplaceGain`). ~12 cards:
    Alhammarret's Archive, Rhox Faithmender, Boon Reflection,
    Wind Crystal, Cleric Class, Honor Troll, Heron of Hope,
    Angel of Vitality, Leyline of Hope, Bilbo Birthday
    Celebrant, Knight of Dawn's Light, Phial of Galadriel.
    Rejects opponent-target (Tainted Remedy / Plague Drone —
    converts gain → lose) and prevention (Sulfuric Vortex).
  - `lifegain_etb_trigger`: `trigger.ChangesZone` with Creature
    filter + `Destination: Battlefield` + coupled
    `effect.GainLife`. ~45 cards: Soul Warden, Auriok Champion,
    Soul's Attendant, Ajani's Welcome, Anointer Priest, Angelic
    Chorus, Daxos Blessed by the Sun, Authority of the Consuls.
- Per-rule audit verdict: positive. **+4.353 NDCG sum (new
  session record)** across 21 commanders, **+45 hits net,
  ZERO regressions** (ndcgmin +0.000 — every touched commander
  improved). Top wins: Bre +6 hits +0.469, Aerith +6 +0.325,
  Hope Estheim +5 +0.373, Haliya +5 +0.358, Celestine +5
  +0.344. Lathiel golden anchor would drop -0.264 without rule.
- Golden aggregate: 0.253267 → 0.255904 (+0.0026). Cumulative
  session lift: 0.246137 → 0.255904 = **+0.0098** (best single-
  session improvement on record).
- 11 new tests: gate, both tiers (GainDouble amp, ReplaceGain
  amp, ChangesZone Creature ETB), rejections (Opponent-target
  amp, Prevent static, non-Creature ETB, ETB-without-GainLife),
  tier priority dedup, commander self-exclusion, rule_id.
  1028 total tests, 82% coverage.

## 2026-04-18

### Filter-axis generalization

- Extract the subject type (Land, Creature, Artifact, creature subtype) from a
  commander's ChangesZone BF→GY trigger filter, then narrow cost_feeds_trigger
  candidates to sacrifice costs whose Sac<N/X> target aligns. Replaces
  commander-specific hand-coded rules — the same logic now lifts any future
  card/commander following the pattern.
- Generic subject_zone_feeder rule (non-Creature subjects only): matches
  effect=Sacrifice SacValid=<subject> and mass ChangeZoneAll returns from
  Graveyard. Scope filter rejects opponent-forcing effects on YouCtrl triggers.
- Generic counter_axis_feeder rule: extracts `counters_GE_<TYPE>` qualifier
  from any commander port (trigger / scales_with / static) on non-Self scope.
  Matches candidates on 4 tiers (payoff / producer / etb_counter / self_recur).
- Result: Titania 0.1703 → 0.3210, Marchesa 0.0211 → 0.0476, Hamza 0.0750 →
  0.1094, all via filter-axis extraction with zero commander-specific code.
  Aggregate golden-set NDCG 0.243525 → 0.245677, no regressions.

### Non-golden commander coverage

- `creatures_as_lands_landfall`: detects Ashaya's type-bending static
  (Affected=Creature, AddType=Land) and emits landfall-payoff matches
  (ChangesZone Land ETB + LandPlayed triggers). Field of the Dead /
  Lotus Cobra / Rampaging Baloths / Scute Swarm now surface for Ashaya.
- `combat_enhancer` broadened: now also fires for Attacks-Self triggers
  when the commander's effect chain contains an engine effect
  (AddPhase / Dig / Play / Mana / Token / DealDamage / Discard / Mill)
  or ≥2 value effects. Etali / Scourge of the Throne / Narset now surface
  extra-combat spells (Relentless Assault, Aggravated Assault, Seize
  the Day). Zur / Wyleth excluded — single Draw or single ChangeZone
  tutor is voltron, not an extra-combat engine.
- Aggregate golden-set NDCG 0.245677 → 0.246137, Hi-Syn 222 → 224.

### Vanilla tribal-anchor fallback

- `tribal_density` rule now falls back to the commander's literal
  creature subtypes when the commander is a *vanilla anchor* (only
  keyword ports, no triggers / effects / statics). Akroma (Angel),
  Ghalta (Dinosaur), Rorix (Dragon), Grumgully (Goblin Shaman), Konda
  (Samurai) — their EDHREC Hi-Syn is dominated by the tribe but no
  other rule emits a match because they have no mechanical structure.
- Skiplist `{Human, Warrior, Soldier}` — pools too large (Human ~4300)
  or not the recognized EDHREC tribal axis.
- Across 2,559 non-golden commanders: 16 commanders improved (Konda
  +0.67, Moritte +0.49, Leonardo +0.27, Akroma +0.18, Rorix +0.14),
  6 regressed (Gorm -0.23, Zetalpa -0.01 — voltron commanders whose
  Hi-Syn isn't tribal). Net +0.001 aggregate across the broad set;
  golden-set NDCG unchanged.

### Flicker gate + creature-count scaling

- `flicker_synergy` now fires when the commander's ETB has a
  temporary-exile ChangeZone effect (`Battlefield → Exile` with a
  `ReturnAbility` clause in raw_line). Lagrella, the Magpie's
  "exile-until-she-leaves" engine qualifies. Plain bounce
  (`→ Hand`), saga-timed exile (Vorinclex, Joshua), and reanimation
  (`Graveyard →`, Sharuum) are rejected.
- `scales_with Valid Creature.YouCtrl` (pure creature-count scaling,
  no counter qualifier) now emits token-producer and populate
  complements. Narrow gate: commander must have no trigger / effect
  / cost / replacement port AND all statics must be self-scoped
  (`Affected: Card.Self` or `ValidTarget: Card.Self`). Shanna, Sisay's
  Legacy qualifies; Adeline (attack trigger) and Ghalta (`ReduceCost
  ValidCard`) stay out.
- Non-golden set: 2 commanders crack 0 → non-zero (Lagrella 0 → 0.174,
  Shanna 0 → 0.169). Zero golden-set regressions, 5 new tests.

### GY-replay keyword grant + Detain + Domain

- `_wants_gy_fill` now also fires when the commander has a Continuous
  static that grants a GY-replay keyword (Unearth / Embalm /
  Eternalize / Encore / Escape / Flashback / Jump-start) to creature
  cards in the graveyard. Sedris, the Traitor King (Unearth) and
  Sliver Gravemother (Encore) — both previously had zero complements
  because their mechanic is "fill the GY, play creatures from it" but
  neither has an explicit ChangeZone GY→BF effect port.
- `flicker_synergy` now accepts `Detain` as a high-value ETB effect.
  Lavinia of the Tenth's "detain opponents' permanents on ETB" is
  mechanically the same shape as Lagrella's temporary exile —
  flickering re-detains new targets.
- `scales_with Domain` now matches basic-land-type adders
  (Prismatic Omen, Dryad of the Ilysian Grove, Nylea's Presence).
  Radha, Coalition Warlord / Nael, Avizoa Aeronaut both scale with
  Domain count.
- Non-golden set: +5 commanders improved (Radha +0.37, Lavinia +0.32,
  Nael +0.29, Sedris +0.20, Zar Ojanen +0.19), zero regressions,
  zero golden-set impact. 835 tests (5 new).

### combat_enhancer tightened to is_combat-only

- `_find_combat_enhancers` DamageDone branch now requires the
  trigger port's `is_combat` flag (Forge sets this iff the raw trigger
  has `CombatDamage: True`). Spell-damage commanders (Imodane, the
  Pyrohammer / Ghyrson Starn, Kelermorph — their DamageDone triggers
  on Instant.YouCtrl, Sorcery.YouCtrl or Card.Other+YouCtrl with
  `DamageAmount EQ1` rather than CombatDamage: True) were previously
  picked up by combat_enhancer and flooded with extra-combat spells
  that their burn-doubler archetype doesn't want.
- Edward Kenway (Vehicle.YouCtrl + CombatDamage: True) and Saskia
  (Creature.YouCtrl + CombatDamage: True) still qualify — the
  is_combat flag directly captures the distinction rather than
  needing a filter-type allowlist.
- Non-golden set: 3 commanders improved (Ghyrson Starn +0.017,
  Taii Wakeen +0.015, Auntie Blyte +0.027), zero regressions. 3
  new tests.

### modified_axis_feeder

- New general rule mirroring counter_axis_feeder for the `modified`
  qualifier (a creature with a +1/+1 counter, an Aura attached, or
  Equipment attached). Detects `modified` in any commander port's
  valid_filter or raw_line clauses. Self-anchored conditions
  (Ian the Reckless's `IsPresent: Card.Self+modified`) and clause
  keys carrying the qualifier as a side condition or flavor text
  (`TargetsValid` for Pearl-Ear, `TriggerDescription`,
  `Description`, `SpellDescription`, `StackDescription`,
  `PrecostDesc`) are skipped — they don't make the commander a
  modified-axis archetype.
- Five tiers (deduped, highest-priority wins per card):
  - `modified_p1p1_doubler` — replacement AddCounter with
    ValidCounterType P1P1 and ReplaceWith AddOneMoreCounters
    (Hardened Scales, Doubling Season, Kami of Whispered Hopes).
  - `modified_p1p1_producer` — effect=PutCounter[All] P1P1 on
    Creature scope, excluding self-sac-only producers.
  - `modified_self_grower` — Creature card with PutCounter Self
    P1P1 (Champion of Lambholt, Forgotten Ancient, Managorger
    Hydra). Restricted to creature cards via cards.types JOIN.
  - `modified_proliferate` — any Proliferate effect.
  - `modified_etb_keyword` — etbCounter:P1P1:N keyword + Modular.
- 11 legendary creature commanders use the modified filter; 9 have
  EDHREC data. Kodama of the West Tree 0/10 → 4/10 hi_syn (Hardened
  Scales / Ozolith / Kami of Whispered Hopes / Evolution Sage),
  on_page 0 → 8/30. Chishiro on_page 3 → 8, Red XIII 0 → 6, SP//dr
  hi_syn 0 → 1. Pearl-Ear (Aura tribal) preserved at 5/10 baseline
  via TargetsValid skip; Silver Sable shows minor on_page churn
  (13 → 7) but new top 30 is mechanically more correct (Hardened
  Scales / Doubling Season / etbCounter:P1P1 cards). Multiplier
  3.0× to match counter_axis_feeder. Golden set NDCG unchanged.
  14 new tests; 852 total tests passing, 86% coverage.

### Schema-driven gap closures

- `damage_doubler_synergy`: replacement.DamageDone with damage-amp
  replacement_result (DmgTwice, DmgTriple, DmgPlus*, Dmg2/3,
  HarshDmg) targeting opponent. Two tiers — damage_amp_stack
  (other replacement-doublers, ~50 cards) > damage_pinger (cards
  with non-combat repeating trigger + DealDamage opponent, ~170).
  Rejects prevention statics (Iroas/Tajic/Emmara/Frodo —
  Prevent: True / PreventionEffect: True), self-target replacements
  (Dralnu/Polukranos/Sekki — ValidTarget: Card.Self / You /
  Permanent.YouCtrl), and damage-decreasing results
  (DmgMinus*, DmgHalf*). Lifts Gisela 0→2 hi_syn, Solphim 0→1,
  Wolverine 0→1, Raphael 0→2, Tor Wauki 0→1; Torbran top-30
  becomes pure doublers (Furnace of Rath, Curse of Bloodletting,
  Mechanized Warfare, Fiery Emancipation, City on Fire).
  Multiplier 2.5×. Closes the cell from 48% → 58% activation.
- `peer_evasion_tribal`: commander has a peer-blocking keyword
  (Horsemanship 29 cards / Shadow 36 cards) → match other cards
  with the SAME keyword. Pools are siloed (horsemanship cmdr
  doesn't pull Shadow, vice versa). General gate: any future
  peer-blocking keyword goes in the `_PEER_BLOCKING_KEYWORDS`
  frozenset. Closes the keyword.Horsemanship cell from 36% →
  100% activation — all 14 P3K legendary horsemanship commanders
  (Cao Ren / Liu Bei / Lu Bu / Lu Meng / Lu Xun / Ma Chao /
  Sun Ce / Xiahou Dun / Yuan Shao / Zhang Fei / Zhang He /
  Zhao Zilong / Lady Zhurong / Guan Yu) now surface their pool.
  Multiplier 2.0×.
- 20 new tests (14 doubler + 6 horsemanship); 872 total tests
  passing, 86% coverage. Golden set NDCG unchanged at 0.246137.
