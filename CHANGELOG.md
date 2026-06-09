## [unreleased]

### 🐛 Bug Fixes

- *(test)* Isolate GIT_* env in test_commit_sha_empty_on_non_git_dir (#69)
- Audit hardening — packaged seeds, integrity guards, importable wheel

### 📚 Documentation

- *(claude)* Release-workflow gotchas + test-isolation reminder

### ⚙️ Miscellaneous Tasks

- Auto-merge Dependabot PRs when checks pass (#70)
- Add GPL-3.0 license (same as Forge)
- Harden release pipeline and dependabot auto-merge
## [0.4.0] - 2026-05-21

### 🚀 Features

- Filter-aware IDF, spellcast resonance, and scoring improvements (+3.1% NDCG)
- Granular scoring and complement rule expansion (+13.4% NDCG)
- New complement rules, scoring refinements, and legacy pipeline removal (+1.7% NDCG)
- New complement rules, IDF tuning, and perf optimization (+4.7% NDCG)
- 7 new complement rules, density rebalance, and dead code cleanup (+5.6% NDCG)
- Extract ChangeType from ChangeZone effects into port_attributes
- Extract TokenScript into token_color/token_subtype attrs
- Add card_hints table populated from DeckNeeds/Hints/Has
- Populate card_hints.buffed_by from BuffedBy SVar
- Scope-aware trigger_effect for player-centric events
- Scope-aware cost_feeds_trigger for player-centric events
- Scope-aware trigger_resonance and sacrifice_cluster
- Effect_conditional trigger flag + IDF dampening
- Counter_producer rule for Marchesa-style counter-filter triggers
- Extend cheat_cmc to Sharuum-style reanimate triggers
- Yard_caster rule for Chainer-style cast-from-graveyard
- Affinity_archetype rule for Affinity-keyword commanders
- Multicolor_untap rule for EachColorAmong mana dorks
- Token_etb_damage rule for passive-token commanders
- Scryfall legal_commander filter
- Include DamageAll in LifeOppsLost scaling
- 6 new rules + Gate 5 for incidental token subtypes
- 4 new rules + archetype tunings (+0.022 NDCG, 5.2× batch speedup)
- Filter-axis generalization replaces commander-specific rules
- Landfall-via-creature-types static + broaden combat_enhancer to Attacks-Self engines
- Tribal_density vanilla-anchor fallback for keyword-only commanders
- Flicker gate handles Lagrella-style temporary exile, scales_with handles pure creature-count
- Gy_loader for replay-keyword grants, flicker Detain, scales_with Domain
- Modified_axis_feeder rule for Kodama/Red XIII/SP//dr archetype
- Schema-driven rule planning pipeline (port universe + coverage matrix)
- Close 2 hard coverage gaps (damage_doubler + peer_evasion_tribal)
- Gap_report auditor — autonomous "what's next" entry point
- Per-port attribution via rule-gate registry
- *(registry)* Complete sweep — 100% rule coverage
- *(gap_report)* Expand template catalog + raise peer-tribal cutoff
- Scaffold_rule.py — auto-generate complement rules from auditor proposals
- *(scaffold)* Auto-revert on validation failure
- Rule_attempts log — learning loop for the scaffolder
- Peer_tribal_keyword generator + first PASSING auto-ship
- *(scaffold)* --walk N for autonomous queue draining
- *(scaffold)* X_cost_scaler generator + 4 more autonomous ships
- Broad-set NDCG check — close the 97% blind spot in safety harness
- *(scaffold)* Mine attempt log -- template-level block + --show-template-stats
- Per-rule impact audit + delete ward_1_tribal (trivial) & ward_2_tribal (HARMFUL)
- *(validate)* Pre-ship per-rule impact check -- catches HARMFUL rules at apply
- *(scaffold)* Counter_removal_payoff generator + tighten trivial classifier
- *(validate)* MARGINAL verdict band -- catch barely-positive rules
- 2 new peer_tribal_keyword rules (mentor + etbreplacement_other_choosect)
- Replacement_stack template + repl_moved_exile_stack rule
- Changeling_tribal rule + drop 4 peer_tribal trivials
- 2 new keyword-tribal rules (landwalk_island, melee)
- 2 new rules (training_tribal, repl_damagedone_counters_stack)
- *(scaffold)* Axis_feeder generator + ship attacking_axis_feeder
- *(audit)* NDCG@30 metric + golden-set deletion-safety check
- Cardpower_axis_feeder rule — +0.707 NDCG across 59 commanders
- Tap_type_feeder rule — axis-aware untap engines for 27 cmdrs
- Hand_size_feeder rule — +2.686 NDCG across 24 big-hand cmdrs
- Gy_fuel_feeder rule — self-mill for exile-from-grave cmdrs
- Lifegain_feeder rule — +4.353 NDCG, +45 hits, zero regressions
- Life_total_feeder rule — +0.169 NDCG for Bilbo + Elenda
- Land_bounce_feeder rule — +0.235 NDCG across 5 cmdrs
- 3 new axis-feeder rules (+2.633 NDCG aggregate)
- 3 new complement rules — cascade_tribal, changezone_resonance, spellcast_universal
- Land_to_gy_synergy rule for Gitrog / Titania Voice of Gaea
- *(bench)* Scaffold CLI + rule_contributions schema (Unit 1)
- *(bench)* Tensor write-path + config_hash (Unit 2)
- *(bench)* PinnedFixture + --repin + --expect-identity (Unit 3)
- *(bench)* Audit main subcommand + report (Unit 4)
- *(bench)* Rank-shuffle histogram + 5-verdict rollup (Unit 5)
- *(bench)* --rule, --inspect, --collinearity (Unit 6)
- *(bench)* Pre-commit hook + .audit/ output (Unit 7)
- *(bench)* Deprecate legacy eval scripts + update docs (Unit 8)
- *(port-graph)* Canonical vocabulary module (Unit 1 of plan 003)
- *(port-graph)* Port_nodes view + schema (Unit 2 of plan 003)
- *(port-graph)* Event_match_map + cost_feeds_trigger tables (Unit 3)
- *(port-graph)* Rules table + JSON validator (Unit 4 of plan 003)
- *(bench)* Audit --unknowns subcommand (Unit 6 of plan 003)
- *(port-graph)* RuleInterpreter class (Unit 5 of plan 003)
- *(port-graph)* Cascade_tribal as declarative row (Unit 7 of plan 003)
- *(port-graph)* Migrate 15 tribal/stack rules to data rows (Unit 8)
- *(bench)* Hidden_gem_hit_rate metric core (plan 003 Unit 1)
- *(bench)* Build_fixture populates hidden_gem_hit_rate (plan 003 Unit 2)
- *(bench)* Audit report surfaces hidden_gem_hit_rate + FR4 warning (Unit 3)
- *(bench)* Audit --inspect-gems CLI handler (plan 003 Unit 4)
- *(bench)* .audit/history.csv + audit --trend hidden_gems (Unit 5)
- *(bench)* --format json for handle_rule/inspect/collinearity (CLR-001/002)
- *(pathway)* Depth-2 self-bridging walker (Unit 1 of plan 2026-04-23-001)
- *(pathway)* Self_bridging_cascade (Units 2-4 + 6, plan 2026-04-23-001)
- *(forge-oracle)* Unit 1 — sparse-checkout ext + SHA pin + pkg skeleton
- *(forge-oracle)* Unit 3 — Python port of CardRanker pair scorer
- *(forge-oracle)* Unit 4 — .dck parser + PPMI ingest pipeline
- *(forge-oracle)* Unit 5 — OracleConfigInputs + compute_oracle_hash
- *(forge-oracle)* Unit 6 — gap_report.py forge_signal re-ranking
- *(forge-oracle)* Unit 7 — bench.py audit --vs-forge-oracle sidecar
- *(forge-oracle)* Unit 8 — scripts/forge_oracle.py propose-rules
- Content embeddings as zero-shot fallback (plan 003) (#13)
- *(port_graph)* VOCAB v3 — classify top UNKNOWN subkinds + tie embeddings to VOCAB_VERSION
- *(rules)* Add ward_2_tribal — 28 commanders gain declarative coverage
- *(quality-gate)* Add Gate C (EDHREC NDCG delta on target commanders)
- *(edict)* Narrow gate + boost edict_feeder (CONTENTIOUS → positive)
- *(token_producer)* Narrow gate + boost (HARMFUL → positive)
- *(bench)* Tensor-driven weight optimizer (M1 foothold) (#27)
- *(bench)* Batch E — agent-native CLI surface for --optimize (#34)
- *(preflight)* Stage A gate stack v1.0 (golden-coverage prefilter) (#39)
- *(bench)* Per-commander NDCG@30 audit reporting (Unit 1)
- *(data)* Refresh Forge to f42b9abc1 (487 commits, +297 cards) (#45)
- *(scoring)* Capture Prepared / AlternateMode:Prepare mechanic (#47)
- *(scoring)* CopyFaceFrom:<Name> resolution v1 (replaces #50) (#54)
- *(scoring)* K:ETBReplacement SVar walking v1 (replaces #52) (#55)

### 🐛 Bug Fixes

- Prevent exponential SubAbility re-walk in extract_effect_ports
- Improve A1 test assertion messages and add lower bounds
- Enforce transient port key contract + simplify pop
- Handle artifact-creature TokenScript format in _parse_token_script
- Expand multi-color TokenScript prefixes + skip named scripts
- Close sqlite connections in effect_conditional tests; harden affinity_archetype
- Combat_enhancer DamageDone branch requires is_combat flag
- *(validate)* Align MARGINAL denominator + drop false-trivial on missing baseline
- *(validate)* MARGINAL message uses scored denominator (matches verdict logic)
- *(gap_report)* Skip replacement_stack proposals with empty result
- Code-review HIGH/MEDIUM from session review
- *(scaffold)* Backfill 4 reverted entries; fix per-signature attribution
- *(ports)* Fallback to ValidCards for ChangesZoneAll triggers
- *(land_to_gy_synergy)* Reject nonLand filters, tighten gate parser
- *(bench)* Split tensor storage — SQLite for tensor, JSON for top-N scores
- *(bench)* Ce-code-review fixups — 11 safe_auto + 1 P1 correctness
- *(port-graph)* Ce-code-review safe_auto batch — 19 fixes + 10 tests
- *(ci)* Commit declarative seed JSONs + guard against future drift
- *(bench)* Ce-code-review P1 batch — wire edhrec_conn, 7 more (round 1)
- *(pathway)* Ce-code-review pass — score_one, stax, perf cache (3 P1 + 5 P2/P3)
- *(forge-oracle)* Apply safe_auto code-review findings (LFG pass)
- *(tests)* Stub read_current_forge_sha when data/forge/ is absent
- *(embeddings)* Verify from stored config not code defaults (FU-1, FU-4)
- *(forge_oracle)* Default smoothing_k 0.5 -> 0.0; restores forge_signal
- *(gap_report)* Wire declarative rules into the static coverage scan
- *(registry)* Remove registration-ghost RuleGate for extra_land_plays
- *(scaffold)* Patch data/scoring_weights.json instead of removed dict literal
- *(bench)* Optimizer follow-up batch D — correctness + edge cases (#29)
- *(bench)* Write_proposal_json crashes on production color_buckets type (#31)
- *(test)* Harden parity harness — multiset comparison + multi-commander demo
- *(test)* Stop test_format_json_emits_summary creating data/synergy.db

### 🚜 Refactor

- Add player-scope compatibility helpers
- Address code-review findings on non-golden-coverage commits
- Address python-review findings (3 MED, 2 LOW)
- Hoist clause regex to core + O(ports²) fix in scales_with
- Drop partner_friends_tribal -- 0/2737 commanders activate gate
- Drop living_tribal -- trivial (sum Δ = 0 across 13 touched cmdrs)
- Dedupe impact-check helpers + drop redundant validation work
- *(scaffold)* Qualifier-portable axis_feeder test scaffold + 2 documented revert attempts
- *(audit)* Apply code-review M1-M5 + L1+L3 polish
- *(audit)* Apply python-review M+L findings (idiomatic Python)
- Apply python-review findings to session's 5 new rules
- Split utility.py into focused submodules + re-export shim
- *(land_to_gy_synergy)* Address python-review nits
- Address ce-code-review P2 punch-list
- *(bench)* Land the 3 deferred P1s from ce-code-review
- *(port-graph)* P1 structural hardening from ce-code-review
- *(bench)* Ce-code-review P2 safe_auto batch (round 2)
- *(pathway)* Remove dead _CHANNEL_VALID_FILTER constant
- *(rules)* Migrate monarch_synergy to declarative (17th in rules_seed.json)
- *(rules)* Migrate toughness_synergy to declarative (18th in rules_seed.json)
- *(rules)* Migrate party_feeder to declarative (19th in rules_seed.json)
- *(rules)* Migrate etb_tapped_stax_feeder to declarative (20th in rules_seed.json)
- Apply ce-code-review fixes across 14 files
- *(review)* Consolidate fragment tuples + harden lazy baseline + add gate tests
- *(scoring)* Externalize _RULE_QUALITY_MULTIPLIER + _FLAT_WEIGHT_OVERRIDES to data/scoring_weights.json
- *(scoring)* Apply ce-code-review LFG fixes (19 findings)
- *(bench)* Consolidate optimizer patch+restore + hoist imports + rename (#36)
- *(bench)* Split run_optimizer's monolithic body into helpers (#17) (#37)
- Declarative-rule cleanup, forge_oracle config split, parity harness
- Rename port_signature_version -> vocab_version, add VOCAB v2 rejection test

### 📚 Documentation

- Add scoring model improvement design (3 phases)
- Add scoring model improvement implementation plan
- Update CLAUDE.md for Phase A data-layer cleanup
- Slim CLAUDE.md, move rule history to docs/RULE_HISTORY.md
- *(audit)* Log life_total_feeder HARMFUL revert
- *(audit)* Log raid_peer_feeder HARMFUL revert
- *(audit)* Log card_counter_all_feeder HARMFUL revert
- Move rule catalogue to docs/COMPLEMENT_RULES.md
- Refresh gap_report + log scaffold walk TRIVIALs
- Document token qualifier in axis_feeder block list
- Recommendation-model roadmap — ideation, 7 brainstorms, plan #1
- *(plan)* Typed port-graph substrate + rule-interpreter POC
- CLAUDE.md + RULE_HISTORY for plan 003 landing
- *(plan)* Useful-disagreement hidden_gem_hit_rate metric (plan 003)
- CLAUDE.md + RULE_HISTORY for plan 003-gem landing (Unit 6)
- *(solutions)* First entry — gitignore negation under ignored parent
- *(solutions)* Flag-gated multi-port complement-rule authoring pattern
- *(forge-oracle)* Unit 2 recon spike — GO verdict
- *(forge-oracle)* Unit 10 — writeups + plan closed
- *(solutions)* Capture forge_oracle CI-checkout-stub pattern
- *(solutions)* Verify derived data from stored config, not code defaults
- *(embeddings)* Decline flag flip, document null result (plan 003 Phase C)
- *(rules)* Document consolidation-check baseline (zero redundancy at 62 rules)
- *(rules)* Document scaffold walker exhaustion — bottleneck is the generator catalog
- *(spec)* Externalize scoring weights to data/scoring_weights.json (M3+M4)
- *(planning)* Document gap-report queue exhaustion on the current golden set
- *(learnings)* Document writer-side audit gap as systemic blind spot
- *(scoring)* Brainstorm + plan for BM25 IDF probe
- *(scoring)* BM25 IDF probe declined — null result
- *(workflow)* META lesson — read sibling solutions docs before improvement levers
- *(scoring)* Brainstorm + design for CopyFaceFrom:<Name> resolution (#49)
- *(scoring)* Brainstorm + design for K:ETBReplacement SVar walking (#51)
- *(claude-md)* Document attr_kind='attribute' + synthetic AlternateMode port (#53)
- *(rules)* Document weight_hint as reserved-for-M2 prior (#23)

### ⚡ Performance

- Cross-commander caching for batch eval (-30%)
- *(tests)* Fold 5 redundant test_rule_id methods into primary tests
- Cache token_etb_damage self-join in CandidateCache
- *(gap_report)* Drop simulation, surface all gaps regardless of reach
- *(validate)* Touched-only NDCG + single-process orchestrator + full universe
- *(audit)* Single-engine + shared baseline + remove etb_sac_target (HARMFUL)
- Delete 3 HARMFUL + 5 TRIVIAL rules — golden NDCG +0.0021
- *(audit)* Parallelize + skip-positive-golden — full audit 12min → 5min
- *(audit)* Delete attacking_axis_feeder — +0.450 NDCG recovery
- *(audit)* Dampen combat_enhancer multiplier 1.0 → 0.7 (+0.486 NDCG)
- *(audit)* Dampen counter_keyword 1.0 → 0.5 (+0.912 NDCG recovery)
- *(audit)* Dampen token_etb_damage 0.5 → 0.3 (+0.184 NDCG, CONTENTIOUS → positive)
- *(audit)* Boost zone_resonance 1.0 → 1.3 (+0.477 NDCG, MARGINAL → positive)
- *(audit)* Boost counter_doubler 1.0 → 1.5 (Animar +5 on-page, 0 regressions)
- *(cheat_cmc)* Unify subtypes into one IDF pool + drop cmc_mid
- *(pathway)* Cache _type_token_set + short-circuit empty valid_filter
- *(audit)* Dampen token_producer 0.25 → 0.15 (+0.266 NDCG recovery)
- *(audit)* Boost cardpower_axis_feeder 2.5 → 3.5 (+3 hits, MARGINAL → positive)
- *(audit)* Boost counter_keyword 0.5 → 0.7 (+2 hits, TRIVIAL → positive)
- *(audit)* Defer golden baseline for single-rule serial audits (−44%)
- *(audit)* Boost cost_reducer 1.0 → 1.2 (MARGINAL → positive, Melek anchor)
- *(audit)* Dampen gy_fuel_feeder 2.5 → 1.2 (+3 hits, -0.246 → +0.289 NDCG)
- *(audit)* Boost panharmonicon 1.0 → 2.0 (recover session IDF rebalance)
- *(audit)* Dampen tap_type_feeder 2.0 → 1.0 (HARMFUL → positive)
- *(audit)* Dampen evasion 0.15 → 0.10 (+0.094 NDCG)
- *(bench)* Cache IDF basis per commander across grid cells (#7) (#30)
- *(scoring)* Cache _compute_pair_bonus + document optimizer perf profile (#32)
- *(bench)* Fuse _fast_total + _build_contributions into one walk (#9) (#33)
- *(tests)* Skip prod-DB fallback in companion-flag warning test
- *(tests)* Parallelize with pytest-xdist -n=auto

### 🧪 Testing

- Remove 17 integration tests that are always skipped in CI
- Skip integration tests when data/synergy.db or cardsfolder absent
- Close leaked sqlite connections in importer + effect_conditional
- Introduce `integration` marker, split suite into unit + integration tiers
- Drop draft integration workflow, keep local-dev-only
- *(land_to_gy_synergy)* Add unit tests + address review nits
- *(pathway)* Bloodghast fixture (Unit 5 of plan 2026-04-23-001)
- *(forge-oracle)* Unit 9 — inference-path isolation fence
- *(bench)* Batch C — close optimizer test gaps + fix cascade flake (#35)
- *(rules)* Pin NULL-valid_filter invariant for etb_tapped_stax_feeder (#15)
- *(bench)* Convert seeded_db / scoring_fixture to yield + close
- Close leaked sqlite connections in helper-based fixtures (#56)
- *(bench)* Broaden scoring_fixture to fire trigger_resonance
- Promote ResourceWarning to error to lock in leak fix (#56)

### ⚙️ Miscellaneous Tasks

- Update golden set baseline to NDCG 0.1748
- Refresh golden set baseline after scope fix
- Refresh golden set baseline after cost_feeds scope fix
- Refresh golden set baseline after trigger_resonance/sac_cluster scope
- Refresh golden set baseline after effect_conditional dampening
- Refresh golden set baseline after counter_producer rule
- Refresh golden set baseline after yard_caster rule
- Refresh golden set baseline after multicolor_untap rule
- Refresh golden set baseline + docs hygiene
- Drop 3 trivial peer_tribal_keyword rules (sum Δ = 0)
- Drop 3 more peer_tribal_keyword trivials (sum Δ = 0)
- Add graphify knowledge graph + project integration
- Remove unused _flicker_synergy_gate
- *(bench)* Re-pin golden_set_run.json to post-tuning baseline
- Remove one-shot migration artifacts
- *(bench)* Re-pin golden_set_run.json to current scoring config
- *(bench)* Optimizer follow-up batch A — docs + cleanup (#28)
- *(scoring)* Tune prepared_mechanic multiplier 1.0 → 3.0 (#48)
- *(test)* Mutmut setup (known-incomplete; in-process pytest blocker)
- Release v0.4.0 (#57)

### ◀️ Revert

- *(rules)* Remove ward_2_tribal + add quality gate that would have caught it
## [0.3.0] - 2026-04-14

### 🚀 Features

- Model versioning, eliminate strategy dependency, 5 new Forge features
- *(forge)* R-event verbs, 6 new GBM features, hard-negative sampling
- *(forge)* Extract ValidAttacker$/ValidBlocker$ into raw_trigger_filters
- *(forge)* Expand cost_types with 4 new categories
- *(forge)* Add static_mode column + S: branch parser fix
- *(forge)* Wire static_mode through ForgeFeatureContext
- Auto-derive Static$<mode> deck tags from profile.static_modes
- *(forge)* Mechanics_vectors integration for static_mode
- *(mtg-synergy-graph)* Deterministic Forge-DSL engine + anchor-quality + perf
- *(mtg-synergy-graph)* A1-A4 parser/schema gaps for Forge DSL coverage
- *(mtg-synergy-graph)* B1 trigger ↔ effect map additions
- *(mtg-synergy-graph)* B3 combo primitive synthetic port
- *(mtg-synergy-graph)* C1 zone-aware Moved/Prevent replacements
- *(mtg-synergy-graph)* D1 sacrifice outlet ↔ payoff matcher
- *(mtg-synergy-graph)* D2 mana restriction matcher
- *(mtg-synergy-graph)* D4 flicker loop detector primitive
- *(mtg-synergy-graph)* C2 substitution-blocking replacements
- *(mtg-synergy-graph)* A5+D3 combat modifier strategic rule
- *(mtg-synergy-graph)* Match cards by oracle_id
- *(mtg-synergy-graph)* F1 counter_synergy bucket (+0.002 NDCG)
- *(mtg-synergy-graph)* Edhrec_rank as intra-tie tiebreaker
- *(mtg-synergy-graph)* F2 deck_hints combo bonus (+0.0068 NDCG)
- *(mtg-synergy-graph)* F5 graveyard_synergy bucket (+0.002 NDCG)
- Add oracle_id_for_name() public API to SynergyEngine
- F6–F10 scoring layers, 100-commander golden set (+9.5% NDCG)
- F11–F13 scoring layers, opponent-trigger fix, 10x golden set speedup
- Replace 27 hand-tuned detectors with universal port matcher
- F14–F21 scoring layers, token parser fix, +8.4% NDCG
- 20 new complement rules, port extraction expansion, +8.5% NDCG

### 🐛 Bug Fixes

- *(train)* Revert negative sampling to 2:1 + 3 tiers
- *(forge)* Iterate all comma-separated parts in combat filter coarse-type derivation
- *(forge)* _parse_cost_types signature str|None + negative tap test
- *(forge)* _check_schema distinguishes missing-table from missing-column
- *(mtg-synergy-graph)* F1.1 accept Self valid_filter in counter_synergy
- Address python review — SQL parameterization, dead code, constants
- Wheel_synergy empty-filter bug, variable shadowing in panharmonicon
- Address code review — docstring, toughness proxy, creature_names comment
- Correct REPO_ROOT path in test_scale.py (parents[3] → parents[1])
- Lower coverage threshold to 77% for CI compatibility

### 🚜 Refactor

- Simplify strategy_detector
- Remove legacy LightGBM packages, flatten to single package
- Remove 156 lines of dead code
- Split complement_rules.py (2803 lines) into subpackage

### 📚 Documentation

- Add Forge DSL Phase 1 implementation plan
- *(plan)* Abandon Task 1 (ReplaceWith$ DSL format was mis-specified)
- Note Phase 1 Forge DSL extractions in CLAUDE.md and FEATURE_REFERENCE.md
- *(spec)* Phase 1.5 sub-project B — Static ability Mode$ semantics
- *(plan)* Phase 1.5 sub-project B implementation plan
- *(CLAUDE)* Phase 1.5b — static_mode column + Static$ tag prefix
- Session summary — Phase 1 + Phase 1.5b results
- Forge-DSL audit follow-up session summary (14 commits, +0.0013 NDCG)
- Minify CLAUDE.md (679 → 216 lines)

### ⚡ Performance

- Reduce peak memory by narrowing SELECT * queries
- Parallelize golden set tracker and cache scoring results

### 🧪 Testing

- *(forge)* Strengthen P1.5b T2 isolation tests per code review
- *(forge)* Strengthen P1.5b T7 mechanics vectors tests per code review
- *(mtg-synergy-graph)* B2 ApiType effect-verb inventory regression
- *(mtg-synergy-graph)* F4 golden set 25 → 50, fix Gitrog data gap
- Add 26 unit tests for complement rule functions
- Increase coverage to 83% for CI 80% threshold

### ⚙️ Miscellaneous Tasks

- Gitignore entire data/ directory, remove tracked edhrec_theme_cards.json
- Remove obsolete entries from gitignore
- Address branch review medium issues (M1/M2/M3)
- Address python-review medium and low issues
- *(mtg-synergy-graph)* Post-audit python-review cleanup
- *(mtg-synergy-graph)* Dead code sweep — 47 lines removed
- *(mtg-synergy-graph)* --graph-metrics opt-in flag (not default)
- Add graph optional extra to dev dependencies
- Migrate to Python 3.13, add pre-commit/ruff/CI/publish workflow
- Add automated release pipeline, security fixes, changelog
- Add pyright, 80% test coverage, fix type errors and resource leaks
- Release v0.3.0

### ◀️ Revert

- *(mtg-synergy-graph)* D3 combat modifier rule — net-negative Hi-Syn
## [0.2.0] - 2026-04-05

### 💼 Other

- Proper versioning with hatch-vcs, trigger on v* tags
## [mtg-synergy-v0.1.0+84b16c6] - 2026-04-05

### ⚡ Performance

- Reduce inference RSS from ~840 MB to ~490 MB (42% reduction)
## [mtg-synergy-v0.1.0+18e62ee] - 2026-04-05

### ⚙️ Miscellaneous Tasks

- Add workflow_dispatch trigger to publish-wheel
## [mtg-synergy-v0.1.0+2178c77] - 2026-04-05

### 🚀 Features

- Add rebuild-registry command to rebuild tag vocabulary from full DB
- Add DB schema for abilities, card_strategies, spellbook tables
- Add ability parser Phase 1 — keyword extraction and card parsing skeleton
- Add ability parser Phase 2 — pattern matching for triggered/activated/static/replacement
- Add ability parser Phase 3 — effect and trigger tagging
- Add bulk ability parsing with DB integration and CLI inspection
- Add strategy detector with rule-based + EDHREC mapping
- Add Commander Spellbook fetcher with DB import
- Add 3-tier combo detection (confirmed/likely/synergy)
- Add strategy-weighted recommendations with CLI override
- Add partial Spellbook combo detection (1-card-away)
- Add anti-synergy detection for strategy-mismatched deck cards
- Add enhanced recommendation output, 3-tier combo display, deck analysis summary
- Add mana cost penalty for high-CMC recommendations
- Add wants-based strategy detection for better coverage
- Add trigger-effect bridges for combo-likely detection
- Integrate 3-tier combos into HTML visualization
- Improve anti-synergy detection with edge count awareness
- Add cross-validation of tags vs EDHREC themes
- Add enrichment pipeline to update workflow + --enrich-only flag
- Add payoff strategy rules for better EDHREC coverage
- Derive strategies from parsed abilities (ABILITY_STRATEGY_MAP)
- Normalize recommendation scores to 0-100% with visual bar
- Wire strategy scoring into --swaps with normalized output
- Detect strategies from deck composition (20%+ threshold)
- Add validation suite measuring system accuracy
- Expand KEYWORD_EFFECT_TAGS to 108 keywords (3% → 14.8% coverage)
- Add untap/mana/spell-copy semantic bridges
- Add combat/turn/damage/graveyard/copy/creature-board bridges
- Tag refinement + massive semantic bridge expansion → 89.7% recall
- Mill/planeswalker/blink/wipe bridges → 90.7% Spellbook recall
- Final bridge batch → 92.5% Spellbook combo recall
- Expand strategy rules for stax/voltron/equipment/wheels/storm
- Push all metrics toward perfection
- 95.2% Spellbook recall + 100% curated pairs
- 96.4% Spellbook recall — 16 more bridges
- 97.0% Spellbook recall — approaching theoretical ceiling
- Commander affinity scoring (tag-level, bypass graph caps)
- Deck-preserving fan-out caps + commander affinity
- Commander affinity multiplier + bridge-expanded deck candidates
- Oracle text concept matching + affinity-primary scoring
- ML-based recommendation model trained on 94 commanders
- Combined classification + pairwise ranking loss → 24% recs
- Add creature type + rare concept features → 30% recommendations
- Spellbook combo + ability match features → 32% recommendations
- Theme/strategy/rarity features → 44% recommendations!
- GBT ensemble + text similarity → 44% recs (Sram 80%, Krenko 80%)
- Equipment/infect/mana bridges → 98.2% curated, 97.3% Spellbook
- Synergy recommendation system overhaul — 2.8/30 → 13.4/30 EDHREC alignment
- Add mtg_synergy package with centralized config and DB factory
- Implement is_equipped, counter_type, power, tapped filter keys
- Add 12 missing semantic bridges for tribal-combat and sacrifice patterns
- Switch LLM scoring to float 1.0-10.0 scale
- Focal loss + provider normalization + 2 new structural features
- Compute commander tag overlap as diagnostic signal
- Show LLM reasoning in --recommend output
- Add mtg-synergy console script entry point
- Add response_format=json_object to OpenAI scoring API calls
- Add curated synergy pair validation benchmark
- Deeper tower model MLP + dropout + LR scheduling
- Combined batch extraction pipeline for mechanics
- Add --limit flag to batch_extract.py + extract 976 more card mechanics
- Replace 6 generic tags with 20 sub-tags in tag registry
- Add reclassify_tags.py for tag sub-categorization via Batch API
- Update SEMANTIC_BRIDGES and TRIGGER_EFFECT_BRIDGES for sub-tags
- Update normalize_tags inference rules for sub-tags
- Update ability_parser tag mappings to use sub-tags
- Update strategy_detector rules for sub-tags
- Update swaps SYNERGY_PROVIDES and edges SKIP_WANTS for sub-tags
- Update remaining files and tests for sub-tag references
- Enable tag overlap tiebreaker with board-generic exclusion
- *(parse)* Add AST type definitions for oracle text parser
- *(parse)* Add ability splitter (pass 1-2)
- *(parse)* Add cost parser for mana, tap, sacrifice, life costs
- *(parse)* Add trigger parser with ~25 event patterns
- *(parse)* Add effect parser with ~20 verb patterns
- *(parse)* Add cross-reference resolver (pass 4)
- *(parse)* Add verb resolvers (rules engine: Effect -> StateChange)
- *(parse)* Add template library for complex patterns
- *(parse)* Wire up full parse_card() pipeline
- *(parse)* Add DB storage and CLI entry point for oracle parser
- *(causal)* Add type definitions for interaction graph
- *(causal)* Add event indexer for parsed abilities
- *(causal)* Add resource flow tracking for loop validation
- *(causal)* Add graph builder with trigger + feeds edges
- *(causal)* Add chain finder with linear + loop detection
- *(causal)* Add graph DB storage, CLI, and causal_score()
- Integrate causal_score into recommendation pipeline
- *(causal)* Add amplifies + enables edges to interaction graph
- Refine causal scoring + restore LLM signal in dynamic pipeline
- *(causal)* Add strategy awareness + effect impact refinements
- *(causal)* Add subtype-aware tribal edges
- *(causal)* Add anti-synergy detection + strategy-aware edge pruning
- Add weight optimizer + NDCG@30 evaluation against 502 EDHREC commanders
- *(causal)* Add event IDF computation to CardIndex
- *(causal)* Apply IDF weighting to trigger and amplifies edges
- *(causal)* Add chain bonus scoring to CausalContext
- Add commander archetype inference from oracle text
- Add EDHREC average deck fetcher + Recall@K evaluation
- *(parse)* Add optional field to Effect dataclass
- *(parse)* Add effect text pre-processor with normalization rules
- Add Forge DSL import script + verb mapping fallback
- *(parse)* Add modal ability mode parsing
- *(parse)* Add Forge-native AST types (ForgeFilter, ForgeTrigger, ForgeEffect)
- *(parse)* Add Forge filter grammar parser
- *(parse)* Full Forge DSL import with shallow SVar resolution
- *(scoring)* Add DeckHas/DeckHints overlap scoring from Forge tags
- *(causal)* Add Forge verb→event mapping table
- *(causal)* Add Forge-native causal indexer
- *(causal)* Add Forge-native graph builder with filter matching
- *(causal)* Wire Forge graph builder into build_graph.py
- *(causal)* Use oracle_ids in Forge graph edges + rebuild
- *(parse)* Add 10 new verb parsers derived from Forge data
- *(parse)* Improve PutCounter (64→86%), add animate + put-onto-battlefield
- Full 12-feature evaluation in optimizer + ablation analysis
- Retrain tower model on Forge causal scores (corr=0.70)
- Unified EDHREC fetcher + dual Recall metric (avg deck + synergy)
- Conditional blending — LLM when available, causal as fallback
- Add fusion model config entries
- Stage 1 -- retrain tower on EDHREC binary membership
- Stage 2 -- build 10-feature matrix from EDHREC data
- Stage 2 -- LightGBM training with leave-commander-out CV
- Wire fusion model into scoring pipeline with graceful fallback
- Update swaps pipeline to use fusion model
- Add --fusion evaluation mode to optimize_weights.py
- Add --holdout-eval for true generalization measurement
- Add --drop-feature flag for feature ablation in holdout eval
- Core Forge verb→provides mapping (Task 1)
- Add trigger→wants mapping (Task 2)
- Add derive_role() with tag-based priority logic (Task 3)
- Full derive_all pipeline + CLI + integration tests (Task 4)
- Replace SEMANTIC_BRIDGES with Forge action→event bridges (Task 5)
- Update swaps.py tag sets to Forge-derived vocabulary (Task 6)
- Tower pre-filter with CI for candidate discovery
- Show causal graph partners in recommendation output
- Forge-native recommendation pipeline — zero EDHREC dependency
- --forge CLI flag + token decontamination across pipeline
- Expand synthetic edges — ETB for permanents + SpellCast for all spells
- Skip baseline retrain + cache forge feature matrix for fast iteration
- Hard negative sampling (50% strategy/subtype overlap) for forge GBM
- Widen forge tower prefilter from 3000 to 8000 candidates
- Filter generic staples from training positives (>30% deck frequency)
- Add edge precision features (deck_exact_edge_ratio + cmdr_exact_edge)
- Speed + quality overhaul — edge index, LambdaRank, 31 features, scryfall links
- Add entity-presence edges to causal graph (1.8M new edges)
- Forge mechanics vectors — general game understanding from structured ability data
- Add Forge ability profile loader to ForgeFeatureContext
- Add 7 new Forge-derived features F33-F39, update FORGE_FEATURE_NAMES to 40
- Replace oracle text fallback with Forge raw_line parsing in mechanics vectors
- Upgrade commander profile inference to use Forge verb/trigger profiles
- Replace F9 oracle_similarity and F28 cmdr_keyword_match with Forge-native features
- Drop tower/embeddings from forge pipeline — 38 pure Forge features, color-identity filter
- Self-supervised training — causal graph graded labels replace EDHREC
- Anti-tribal detects cost/conditional subtype requirements from Forge
- Extract effect-target subtypes from ValidCards$/Affected$ in raw_line
- Extract ALL Forge raw_line fields — granted_keywords, conditions, duration, combat_damage, scales_with, etc.
- 51 features — wire ALL extracted Forge fields into GBM
- Add 12 new forge features (51 → 63), NDCG@30 0.51 → 0.53
- Add 5 zone-aware concepts to mechanics vectors (27 → 32 game concepts)
- Add 8 new forge features (ability counts, token complexity, zone interaction)
- Add 3 deck tag expansion features (cmdr_needs, needs_satisfied, needs_rarity)
- Extract excluded_subtypes (nonHuman, nonGoblin etc.) from Forge data
- --commander "Name" --recommend mode (no deck config needed)
- Batch_recommend for multi-commander scoring (0.8s/cmdr vs 7s subprocess)
- Change default --top from 30 to 50
- Post-scoring penalties + interaction features for anti-synergy
- Combine edhrec_card_synergy + average_deck for training labels
- 78 features, post-scoring penalties, sample weighting for forge model
- 3:1 negative ratio with tag-aware hard negatives
- F78 cmdr_p1p1_card_no_counters — penalize non-counter creatures for counter commanders
- Functional fingerprints — semantic understanding of card abilities
- Continuous pump edges — lord/anthem effects create causal graph edges
- Add position numbers to recommendation output
- Theme-based features, graph edges, and mechanics vectors
- Noise suppression — edhrec_deck_pct is now #1 feature at 11.3%
- Forge-native noise suppression + EDHREC_FREE toggle
- Hyperparameter tuning — num_leaves=511, lr=0.03
- 2-hop graph features + wider HP search (leaves=767, lr=0.025)
- Hidden gem engine — mechanical reasoning for rare synergy discovery
- Mechanical synergy bonus in --recommend pipeline
- Continuous synergy grading — NDCG 0.514 → 0.531
- Add validate_recommendations.py + fix scoring bugs
- End-to-end pipeline validation (model + scoring + penalties)
- Add 6 spellslinger features (105→111) for spell-based commanders
- Add spellslinger CMC interaction feature (112 features total)
- Add 4 graveyard/self-sacrifice features (112→116 features)
- Parse R: replacement effects, penalize opponent-only amplifiers for self-targeting commanders
- Extend ValidPlayer$/defined opponent detection to all ability types
- Extract 5 new Forge DSL fields, 89→93 features

### 🐛 Bug Fixes

- Remove false positive wants tribal tags via oracle text cross-reference
- Correct LLM optimization issues found during validation
- Populate has_keyword in card_produces_events output
- Reduce BATCH_SIZE 150→100 to prevent output truncation
- Increase max_completion_tokens 4096→16384 for gpt-5.x models
- Batch API had hardcoded max_completion_tokens:4096 (missed by replace_all)
- Handle DFC card names in EDHREC comparison
- *(parse)* Fix ManaAmount/Amount deserialization in from_dict
- Parse effects for static abilities, multi-keyword grants, and can't patterns
- Update test_forge_fallback for new forge_abilities schema
- *(causal)* Remove edge limit, skip Card.Self triggers, dedup in-place
- Use EDHREC avg deck as context + recompute strat keywords after profile
- Match edhrec_card_synergy 6-column schema in unified fetcher
- Suppress LightGBM feature names warning in predict_proba calls
- Unicode normalization + DFC slug fix in EDHREC fetchers
- Counter-type-specific tags prevent false synergy matches
- Add lightgbm to uv dependencies
- Use raw GBM scores for ranking + filter token cards
- Revert to EDHREC training labels — self-supervised causal labels overfit
- Guard against best_iteration=0 when early stopping doesn't trigger
- Only build aggregated edge dicts for training, clean up dead code
- Guard zone-PRODUCES on zone-transition verbs, consistent Hand matching
- Handle comma-separated token scripts, explicit zone dims in mech_zone_fwd
- Filter commander-illegal cards from training negative pool
- Filter commander-illegal cards from recommendations (A-Alchemy etc.)
- Expand post-scoring penalties for required_subtypes + wrong token types
- Restore counters_on_lands penalty in score_forge_candidates path
- Harden forge features — named columns, specific exceptions, safe SQL
- Include filter_precision in build_and_store_graph INSERT
- Address review findings from deduplication refactor
- Penalize wrong counter types for counter commanders
- Required_subtypes penalty for all commanders + niche counter penalty
- DFC-aware subtype extraction + Doctor's companion + energy penalty
- Apply excluded_subtypes penalty for all commanders
- Restore Forge DSL data extraction for Secondary$, Duration$, effect_zones
- Restore card metadata display — build card_meta after scoring
- Unify _GENERIC_TYPES, address code review findings
- Security hardening + 6.6x faster cold start (35s → 5s)

### 💼 Other

- Run enrichment pipeline on 10k cards
- Registry rebuild after payoff strategy rules
- Filter partial Spellbook combos by commander color identity
- Remove creature-etb → blink strategy mapping (too generic)
- Remove generic creature wants from tokens/go-wide strategy mapping
- Hide 0-card strategies and skip empty tribal strategies
- Full enrichment on 34k cards
- Toxic keyword now maps to poison-counter-placement effect tag
- Record baseline EDHREC scores before tag subcategorization
- Record EDHREC scores after tag reclassification (tiebreaker off)
- Completed tag reclassification via Batch API (~41k tags)
- Record EDHREC scores with overlap tiebreaker enabled
- Deeper GBM with regularization for forge model

### 🚜 Refactor

- Extract SEMANTIC_BRIDGES and constants to mtg_synergy.constants
- Extract graph building to mtg_synergy.graph
- Extract combo detection to mtg_synergy.combos
- Extract recommendation engine to mtg_synergy.recommend
- Extract deck analysis and visualization to mtg_synergy.analysis
- Extract CLI to mtg_synergy.cli, synergy_graph.py is now thin re-export wrapper
- Update external consumers to import from mtg_synergy package
- Switch optimizer from top-30 overlap to Recall@100
- Replace parsed_abilities with forge_abilities in causal scoring
- Replace abilities table reads with provides/wants in detector and strategy
- Remove LLM synergy_scores from recommendation pipeline
- Remove dead apply_llm_scoring + helper functions from engine.py
- Remove F2 (card_mechanics) and F9 (STRATEGY_KEYWORDS) from scoring pipeline
- Remove provides/wants tag overlap from scoring (F3/F4)
- Migrate swaps from graph to tower pre-filter + causal
- Delete 14 legacy scripts + skip graph for recommend/swaps
- Remove provides/wants from fusion model training
- Migrate 4 files from provides/wants tables to forge_abilities
- Eliminate provides/wants tables, bridges, and tag abstraction layer
- Slim fusion model to 8 features + drop 6 unused tables
- Remove dead graph/ package, clean tag_db CLI, update CLAUDE.md
- Revert to strategy/subtype-only hard negatives (50%)
- Extract shared forge feature computation into forge_features.py
- Remove _card_oracle dict — F28 now uses _card_tokens
- Move mtg_synergy package to src/ layout with uv build backend
- Delete train_tower_model.py, card_embeddings.py, optimize_weights.py (dead code)
- Gut scoring.py — remove all baseline/tower code, keep forge-only
- Simplify engine.py — forge-only recommendation, no branching
- Simplify swaps.py — use forge scoring, remove baseline deps
- Clean config.py — remove tower/baseline/scoring weight entries
- --recommend defaults to forge, --forge is deprecated no-op
- Strip baseline training from train_fusion_model.py (forge-only)
- Remove decks/ folder and --deck CLI, commander-only interface
- Compare_edhrec.py uses --commander, compares against DB synergy data
- Remove unused load_edhrec_membership (replaced by edhrec_card_synergy)
- Clean one-line output for --recommend
- Remove dead popularity weighting code
- Code review fixes + dead code cleanup (-10k LOC)
- Make Forge graph building the default in build_graph.py
- Deduplicate shared logic across scoring and causal modules
- Remove normalize_tags.py and tag_registry.json
- Move scripts to scripts/, library modules to src/mtg_synergy/
- Remove dead code, legacy DB tables, and oracle text parser (-7.5k lines)
- Generalize features — remove archetype-specific, add per-category mech sub-products (101→89 features)
- Simplify compare_edhrec.py — add --top N, remove --limit
- Remove excluded_subtypes penalty — absorbed by features
- Remove dead code — _arr_grants_abilities, has_static_anthem
- Remove unused profile fields — grants_types, mana_colors, counter_num_variable
- Zero 6 redundant workaround features, remove dead profile fields
- Remove dead code — unused imports, functions, constants
- Remove combo detection — moved to deck builder project
- Monorepo split + unmet ability needs penalty + code quality fixes

### 📚 Documentation

- Update CLAUDE.md with complete system documentation
- Update CLAUDE.md with new mtg_synergy package architecture
- Update test count to 89
- Add tag sub-categorization design spec
- Fix scope estimates in tag subcategorization spec
- Add tag sub-categorization implementation plan
- Update CLAUDE.md for tag sub-categorization
- Fix sub-tag names in CLAUDE.md and update test count
- Add rules engine design spec
- Address spec review findings in rules engine design
- Add implementation plan for rules engine (part 1: parser + rules)
- Add implementation plan for rules engine part 2
- Update CLAUDE.md with rules engine, causal graph, and NDCG findings
- Add deterministic stack optimization design spec
- Fix 7 review issues in deterministic stack spec
- Switch success criteria to Recall@K as primary metric
- Add implementation plan for deterministic stack optimization
- Update CLAUDE.md with IDF graph, commander profiles, Recall@K
- Replace NDCG references with Recall@100, clarify compare_edhrec scope
- Add effect extraction improvement design spec
- Fix 5 review issues in effect extraction spec
- Fix review round 2 — add ast_types.py to files list, clarify rule ordering
- Add effect extraction implementation plan
- Add Forge-native architecture design spec
- Fix 9 review issues in Forge-native architecture spec
- Fix trigger count 200→134 and card_types example in Forge spec
- Add Forge Data Foundation implementation plan (Plan A)
- Add Forge Causal Graph Rewrite plan (Plan B)
- Add local synergy model spec for next session
- Finalize hybrid synergy model spec (tower + LightGBM)
- Add hybrid fusion model implementation plan
- Update CLAUDE.md and spec with fusion model results
- Add Forge-derived tags design spec
- Update Forge-derived tags spec with review fixes
- Map all Forge verbs regardless of count
- Add Forge-derived tags implementation plan
- Update CLAUDE.md for Forge-derived tag system
- Add tower pre-filter design spec
- Update CLAUDE.md for forge-native pipeline, performance, and expanded causal graph
- Design spec for creature-enters synthetic edges + has_cmdr_edge feature
- Update CLAUDE.md for expanded synthetic edges (17.1M edges)
- Update CLAUDE.md for forge model training improvements
- Update CLAUDE.md for 22 features, forge_features module, current state
- Update CLAUDE.md for forge v2 — 33 features, LambdaRank, 18.4M edges, mechanics vectors
- Update CLAUDE.md for 40 Forge-native features, oracle text elimination
- Update CLAUDE.md — 100% Forge-native features, zero oracle text parsing
- Update CLAUDE.md — 38 features, no tower/embeddings, color-identity filter
- Update CLAUDE.md — self-supervised training, zero EDHREC dependency
- Update CLAUDE.md — 51 features, 20 profile fields, comprehensive raw_line extraction
- Plan for 8 new features + zone-aware mechanics vectors
- Update CLAUDE.md — 71 features, 32 game concepts, NDCG 0.54
- Add completed training speedup plan from previous session
- Plan to remove baseline/tower, forge-only default
- Update CLAUDE.md — remove baseline/tower documentation, forge-only
- Update CLAUDE.md — 74 features, NDCG 0.52 (legality-corrected), fix test assertions
- Spec for synergy-based training labels + CLI simplification
- Plan for synergy labels + CLI simplification
- Update CLAUDE.md for synergy labels + commander CLI, fix test assertions
- Update CLAUDE.md — 76 features, post-scoring penalties, session wrap-up
- Update CLAUDE.md — 78 features, new penalties, sample weighting, NDCG 0.50
- Update CLAUDE.md — 83 features, functional fingerprints, 19.7M edges, continuous pump
- Update CLAUDE.md — 105 features, NDCG 0.52, EDHREC_FREE toggle, --tune flag
- Update CLAUDE.md — hidden gems, mechanical bonus, training perf
- Update CLAUDE.md — 2x data, continuous grading, perf improvements
- Rewrite README for current Forge-native architecture
- Use uv run mtg-synergy for CLI commands
- Update CLAUDE.md — new project structure, scoring penalties, validation
- Update CLAUDE.md — reflect cleanup, fix stale references, update test count
- Update CLAUDE.md — 116 features, NDCG 0.595, tuned HP, new archetypes
- Update CLAUDE.md — opponent-only detection for all ability types, 160 tests
- Add feature reference — maps all 93 GBM features to Forge DSL fields with examples
- Update CLAUDE.md — 93 features, new Forge fields, feature reference
- Update CLAUDE.md — test count, perf numbers, security conventions
- Remove stale 'thresholds' from config.py description (SWAP removed)
- Update README — remove combo references, fix stale numbers

### ⚡ Performance

- 5 performance optimizations for recommendation pipeline
- Optimize LLM API usage — 50-75% cost reduction
- *(causal)* Lazy-load edges per card instead of all 7.6M at init
- Reduce training set — 1.76M→488k pairs, 35min→7.5min training time
- Training 35min → 1min — edge cache, aggregated SQL, fast negative sampling
- Optimize forge-only pipeline (10min → 2min, 500MB less memory)
- Speed up GBM training -- 3 folds, lr=0.05, CV-capped final model
- Load edge index at inference — 81s → 7s recommendation time
- Skip HP search by default, add --tune flag
- Tune HP + parallel feature build & CV folds
- Vectorized batch feature computation for training
- Shared ForgeFeatureContext via fork pool — feature build 32s → 8s
- 2x faster training — vectorized NDCG/CV/weights, thread pinning, reduced rounds
- Adjacency cache, EDHREC fetcher rewrite, 2x training data
- Tune training hyperparams — NDCG@30 0.529→0.594 (+12.2%)
- Reuse ForgeFeatureContext across calls — 430x faster recommendations
- Pre-compile regex, batch features, promote constants — 500x total speedup
- Optimize training pipeline — dead code, batch queries, json precompute
- Merge card array loops + covering index — context init 4.0s → 3.0s
- Smarter thread allocation for LightGBM training folds

### 🧪 Testing

- Add test infrastructure with shared fixtures for enrichment pipeline
- Add end-to-end integration test for full enrichment pipeline
- Verify two-step chain filter matching in mechanics engine
- Add 11 tests for score_synergies.parse_response
- *(parse)* Add subject normalization + deconjugation tests
- *(parse)* Add integration + regression tests for effect extraction
- Update tests for forge-only (remove baseline/tower tests)
- Delete test_recall_evaluation.py (optimize_weights.py was removed)
- Cache ForgeFeatureContext across tests — 124s → 3.5s

### ⚙️ Miscellaneous Tasks

- Add utility scripts, plans, and update .gitignore
- Pass edhrec_slug to recommend/swaps CLI + gitignore model files
- Remove dead data files, add npz cache to gitignore
- Remove last legacy references (embeddings, LLM tags)
- Remove legacy data files from git tracking (~180MB)
- Default --validate-top to 100 commanders
- Add .DS_Store and .claude/ to .gitignore
- Remove unused deps (torch, transformers, sentence-transformers, etc.)
- Gitignore training logs, sweep artifacts, model variants, .ecc/
- Drop spellbook tables from DB and schema
- Upgrade pygments 2.19.2 → 2.20.0 (fix ReDoS CVE-2026-4539)
- Gitignore data/inference/ export artifacts
- Publish mtg-synergy wheel to GitHub Releases on push
