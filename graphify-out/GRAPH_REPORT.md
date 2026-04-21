# Graph Report - .  (2026-04-21)

## Corpus Check
- 134 files · ~186,251 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 3203 nodes · 6955 edges · 40 communities detected
- Extraction: 72% EXTRACTED · 28% INFERRED · 0% AMBIGUOUS · INFERRED: 1979 edges (avg confidence: 0.72)
- Token cost: 0 input · 0 output

## Community Hubs (Navigation)
- [[_COMMUNITY_Utility Rules Tests (axis + cost_payoff)|Utility Rules Tests (axis + cost_payoff)]]
- [[_COMMUNITY_Universal Scorer & Core|Universal Scorer & Core]]
- [[_COMMUNITY_Audit & Validation|Audit & Validation]]
- [[_COMMUNITY_Importer & Data Pipeline|Importer & Data Pipeline]]
- [[_COMMUNITY_Density Rules|Density Rules]]
- [[_COMMUNITY_Graveyard & Artifact Recursion|Graveyard & Artifact Recursion]]
- [[_COMMUNITY_Combat Payoffs & Evasion|Combat Payoffs & Evasion]]
- [[_COMMUNITY_Port Parser & Fixtures|Port Parser & Fixtures]]
- [[_COMMUNITY_Documentation & Concepts|Documentation & Concepts]]
- [[_COMMUNITY_Strategic Heuristics|Strategic Heuristics]]
- [[_COMMUNITY_Rule Registry + Generated Choose Tribal|Rule Registry + Generated: Choose Tribal]]
- [[_COMMUNITY_Graph Engine & Attributes|Graph Engine & Attributes]]
- [[_COMMUNITY_Panharmonicon Rules|Panharmonicon Rules]]
- [[_COMMUNITY_Rule Scaffolder & Gap Report|Rule Scaffolder & Gap Report]]
- [[_COMMUNITY_Penalty & Coverage Analysis|Penalty & Coverage Analysis]]
- [[_COMMUNITY_Scope Compatibility Tests|Scope Compatibility Tests]]
- [[_COMMUNITY_Resonance Pair Builders|Resonance Pair Builders]]
- [[_COMMUNITY_Tap-Type Axis Tests|Tap-Type Axis Tests]]
- [[_COMMUNITY_Static Ability Rules|Static Ability Rules]]
- [[_COMMUNITY_Hand-Size Rules|Hand-Size Rules]]
- [[_COMMUNITY_Untap Combo Tests|Untap Combo Tests]]
- [[_COMMUNITY_Generated Exile Replacement|Generated: Exile Replacement]]
- [[_COMMUNITY_Generated Damage Counter Replacement|Generated: Damage Counter Replacement]]
- [[_COMMUNITY_Generated ETB Choosect Tribal|Generated: ETB Choosect Tribal]]
- [[_COMMUNITY_Generated Landwalk Island Tribal|Generated: Landwalk Island Tribal]]
- [[_COMMUNITY_Generated Mentor Tribal|Generated: Mentor Tribal]]
- [[_COMMUNITY_Generated Melee Tribal|Generated: Melee Tribal]]
- [[_COMMUNITY_Generated ETB Copy Optional Tribal|Generated: ETB Copy Optional Tribal]]
- [[_COMMUNITY_Generated More Tribal|Generated: More Tribal]]
- [[_COMMUNITY_Generated Changeling Tribal|Generated: Changeling Tribal]]
- [[_COMMUNITY_Generated Start Tribal|Generated: Start Tribal]]
- [[_COMMUNITY_Generated Firebending 2 Tribal|Generated: Firebending 2 Tribal]]
- [[_COMMUNITY_Generated Doctor's Tribal|Generated: Doctor's Tribal]]
- [[_COMMUNITY_Generated Training Tribal|Generated: Training Tribal]]
- [[_COMMUNITY_Generated Prowess Tribal|Generated: Prowess Tribal]]
- [[_COMMUNITY_Port Universe Inventory|Port Universe Inventory]]
- [[_COMMUNITY_ApiType Inventory Snapshot|ApiType Inventory Snapshot]]
- [[_COMMUNITY_Wrath of God Fixture|Wrath of God Fixture]]
- [[_COMMUNITY_EDHREC Validation Oracle|EDHREC Validation Oracle]]
- [[_COMMUNITY_Gap Reach Formula|Gap Reach Formula]]

## God Nodes (most connected - your core abstractions)
1. `PortComplement` - 331 edges
2. `_add_port()` - 176 edges
3. `_port_row()` - 136 edges
4. `CandidateCache` - 118 edges
5. `_candidates()` - 90 edges
6. `UniversalScore` - 87 edges
7. `_insert_card()` - 76 edges
8. `_insert_port()` - 74 edges
9. `_port()` - 64 edges
10. `_port()` - 62 edges

## Surprising Connections (you probably didn't know these)
- `Unit tests using synthetic parsed trigger dicts.` --uses--> `PortComplement`  [INFERRED]
  tests/test_effect_conditional.py → src/mtg_synergy_graph/complement_rules/core.py
- `Korvold-style: Sacrificed trigger with straightforward Execute.` --uses--> `PortComplement`  [INFERRED]
  tests/test_effect_conditional.py → src/mtg_synergy_graph/complement_rules/core.py
- `Selvala-style: ETB trigger whose execute has ConditionCheckSVar.` --uses--> `PortComplement`  [INFERRED]
  tests/test_effect_conditional.py → src/mtg_synergy_graph/complement_rules/core.py
- `ConditionPresent$ also counts as a runtime gate.` --uses--> `PortComplement`  [INFERRED]
  tests/test_effect_conditional.py → src/mtg_synergy_graph/complement_rules/core.py
- `A standalone CheckSVar in the execute also counts.` --uses--> `PortComplement`  [INFERRED]
  tests/test_effect_conditional.py → src/mtg_synergy_graph/complement_rules/core.py

## Hyperedges (group relationships)
- **Rule-planning pipeline: gap_report → scaffold → audit → golden-set regression** — concept_gap_report_script, concept_scaffold_rule, concept_audit_rule_impact, concept_golden_set, concept_ndcg_30, concept_port_universe, concept_coverage_matrix [EXTRACTED 0.95]
- **Primitive complement rules (trigger/effect/cost/resonance family)** — rule_trigger_effect, rule_cost_feeds_trigger, rule_trigger_resonance, rule_effect_resonance, rule_replacement_resonance, rule_replacement_producer [EXTRACTED 0.90]
- **Gated axis-feeder rules (single-axis scaler families)** — rule_counter_axis_feeder, rule_modified_axis_feeder, rule_cardpower_axis_feeder, rule_tap_type_feeder, rule_hand_size_feeder, rule_gy_fuel_feeder, rule_lifegain_feeder, rule_life_total_feeder, rule_land_bounce_feeder, rule_party_feeder, rule_creature_died_feeder [EXTRACTED 0.95]

## Communities

### Community 0 - "Utility Rules Tests (axis + cost_payoff)"
Cohesion: 0.01
Nodes (260): _find_cardpower_axis_feeders(), _find_cost_payoff_complements(), _find_counter_axis_feeders(), _find_modified_axis_feeders(), _is_self_qualified(), _only_self_sac_cost(), Axis feeders (counter, modified, cardpower, tap-type, hand-size) and cost payoff, Return True if the first OR-alt of ``filter_or_clause`` carries a     ``Self`` t (+252 more)

### Community 1 - "Universal Scorer & Core"
Cohesion: 0.02
Nodes (212): _build_stax_exclusion(), _extract_filter_group(), find_all_complements(), PortComplement, Extract the most specific type/subtype from a port's valid_filter.      Used to, Find all port-pair complements between commander and candidate cards.      Algor, A single complementary (commander_port, candidate_port) pair., Build set of cards to globally exclude -- stax pieces that actively     hurt thi (+204 more)

### Community 2 - "Audit & Validation"
Cohesion: 0.02
Nodes (222): _all_auditable_rules(), _all_generated_rules(), _audit_many(), _audit_one(), _audit_rule_single(), classify_impact(), _golden_set_oids(), hi_syn_and_ndcg() (+214 more)

### Community 3 - "Importer & Data Pipeline"
Cohesion: 0.02
Nodes (187): main(), Precompute the causal graph cache (SPEC §6.8 / Phase 4.4).  Run this once after, open_db(), SQLite helpers: open + initialize the synergy.db schema., Open a SQLite connection with the v1.2.2 schema applied.      Safe to call repea, _schema_sql(), Return the ``cards.name`` row that matches ``oracle_id``.          Raises ``Look, build_graph_cache() (+179 more)

### Community 4 - "Density Rules"
Cohesion: 0.03
Nodes (109): _escape_like(), _find_counter_doubler_synergy(), _find_counter_keyword_synergy(), _find_etb_self_complements(), _find_lord_complements(), _find_scales_with_density(), _find_scaling_complements(), _find_spellcast_density_complements() (+101 more)

### Community 5 - "Graveyard & Artifact Recursion"
Cohesion: 0.03
Nodes (115): _is_static_continuous(), True iff ``p`` is a ``static`` port with ``event_class`` ``Continuous``.      Sh, _extract_recast_types(), _find_artifact_recursion(), _find_copy_synergy(), _find_dies_drain(), _find_graveyard_fillers(), _find_gy_loader() (+107 more)

### Community 6 - "Combat Payoffs & Evasion"
Cohesion: 0.02
Nodes (113): _attacks_trigger_has_value_effect(), _cmdr_trigger_subject_types(), _find_attack_payoffs(), _find_changeszone_resonance(), _find_combat_enhancers(), _find_evasion_complements(), _find_sacrifice_outlets(), _find_subject_zone_feeders() (+105 more)

### Community 7 - "Port Parser & Fixtures"
Cohesion: 0.02
Nodes (155): cathars_crusade(), korvold(), _load(), panharmonicon(), Shared fixtures for the mtg_synergy_graph test suite., rhystic_study(), scute_swarm(), ChainNode (+147 more)

### Community 8 - "Documentation & Concepts"
Cohesion: 0.02
Nodes (113): Cathars' Crusade, CHANGELOG, CLAUDE.md project instructions, Complement Rules reference, _audit_rule_impact.py, BuffedBy SVar, card_hints table, card_ports table (+105 more)

### Community 9 - "Strategic Heuristics"
Cohesion: 0.03
Nodes (94): active_rules_for_commander(), _card_keywords(), _cmdr_wants_combat_damage(), evaluate_strategic_rules(), _evasion_boost(), _has_effect(), _has_effect_in(), _lki_scaling_boost() (+86 more)

### Community 10 - "Rule Registry + Generated: Choose Tribal"
Cohesion: 0.02
Nodes (55): _choose_tribal_gate(), _find_choose_tribal(), AUTO-GENERATED rule: choose_tribal.  Generated by scripts/scaffold_rule.py from, True iff ``port`` is a keyword port for the target keyword., Return every other card carrying the same keyword.      Pool is small by gate co, attributable_rules_for_port(), _cardpower_axis_gate(), _counter_target_payoff_gate() (+47 more)

### Community 11 - "Graph Engine & Attributes"
Cohesion: 0.03
Nodes (85): classify_attr_token(), explode_filter(), Filter attribute explosion (SPEC §4.6).  Converts a Forge ``ValidCard$``-style f, Convert a Forge ValidCard$ filter string into attribute rows.      >>> explode_f, Classify a single filter token (post '!' stripping).      Returns the ``attr_kin, _effect_matches_filter_group(), Check if a candidate effect produces something matching the filter_group.      O, _branch_priority() (+77 more)

### Community 12 - "Panharmonicon Rules"
Cohesion: 0.04
Nodes (54): _find_panharmonicon_complements(), _find_panharmonicon_stacking(), _find_reverse_panharmonicon(), _iter_panharmonicon_statics(), Panharmonicon-style complement matchers., Find candidates with Panharmonicon static that double the commander's triggers., Find Panharmonicon-like cards when the commander IS a Panharmonicon.      Yarok, Yield ``(card_name, raw_line)`` for every Panharmonicon static port.      Reads (+46 more)

### Community 13 - "Rule Scaffolder & Gap Report"
Cohesion: 0.05
Nodes (83): AttemptRecord, is_known_bad(), is_template_blocked(), load_attempts(), now_iso(), prior_attempts_for_template(), Append-only log of rule scaffold attempts.  Tracks every ``scaffold_rule.py --ap, Per-template outcome counts. Outer key = template name, inner     keys = ``passe (+75 more)

### Community 14 - "Penalty & Coverage Analysis"
Cohesion: 0.04
Nodes (70): _build_matrix(), _commander_names(), _commander_port_shapes(), _formal_coverage(), main(), _print_summary(), Coverage matrix: which port shapes have rules vs. which are gaps.  Two-axis repo, Return mapping (port_type, event_class) → [rule_id, ...] from     the static ``e (+62 more)

### Community 15 - "Scope Compatibility Tests"
Cohesion: 0.04
Nodes (42): True iff a commander trigger can fire on the candidate's effect.      ``any`` on, _scope_compatible(), _cost_feeds_trigger_rule(), Tests for scope-aware trigger/effect compatibility.  Covers _parse_trigger_scope, Cross-product of cmdr_scope × cand_scope., The trigger_effect rule must respect player scope on player-centric events., Tergrid (opp-scoped Sacrificed) × Lich's Tomb (you-scoped Sacrifice)         mus, Tergrid × Smallpox ("Each player sacrifices") must match:         forcing all pl (+34 more)

### Community 16 - "Resonance Pair Builders"
Cohesion: 0.05
Nodes (39): _build_resonance_pairs(), _build_sacrifice_cluster_pairs(), _build_trigger_resonance_pairs(), _commander_subtypes_from_ports(), _cost_filter_group(), _has_any_noncreature_trigger(), _invert_cost_feeds(), _invert_event_match_map() (+31 more)

### Community 17 - "Tap-Type Axis Tests"
Cohesion: 0.07
Nodes (25): _classify_tap_type_axis(), _find_tap_type_feeders(), Resolve the set of axis classes implied by a commander's     ``cost.tap_type`` p, Rule for commanders with a ``cost.tap_type`` port     (``tapXType<N/SUBJECT>``)., Axis resolver extracts the SUBJECT from a ``tapXType<N/SUBJECT>``     cost and c, ``Food`` is an Artifact subtype and goes on the artifact axis., ``Halfling.Other`` / ``Artifact.!token`` qualifiers don't         change the axi, Caparocti: ``Artifact;Creature`` — both classes. (+17 more)

### Community 18 - "Static Ability Rules"
Cohesion: 0.09
Nodes (29): _find_cost_reduction_synergy(), _find_edict_feeders(), _find_graveyard_play_synergy(), Static ability complement matchers (cost reduction, graveyard play, edicts)., Find MayPlay-from-Graveyard enablers for landfall/GY commanders.      Omnath tri, Find edict effects for death-trigger commanders.      Meren triggers on creature, Find cost reducers for commanders with SpellCast triggers.      Talrand triggers, conn() (+21 more)

### Community 19 - "Hand-Size Rules"
Cohesion: 0.08
Nodes (25): _find_hand_size_feeders(), _is_big_hand_commander(), Classify a commander with ``scales_with ValidHand Card.YouOwn``     as big-hand, Rule for big-hand commanders — those with ``SVar:<X>:Count$     ValidHand Card.Y, Classify hand-size commanders as big-hand (reward more cards in     hand) or sma, Non-hand-size commanders are neither big nor small — the         gate returns Fa, Alandra has a hand-SVar but no small-hand compare — classified         as big-ha, Hazoret: ``CheckSVar: X`` + ``SVarCompare: GE2`` on         CantAttack = blocked (+17 more)

### Community 20 - "Untap Combo Tests"
Cohesion: 0.17
Nodes (20): _candidates(), conn(), _insert_card(), _insert_port(), _make_db(), _mass_untap(), _port(), Tests for the untap_combo rule.  Covers ``_find_untap_combo`` which matches broa (+12 more)

### Community 21 - "Generated: Exile Replacement"
Cohesion: 0.18
Nodes (11): _find_repl_moved_exile_stack(), AUTO-GENERATED rule: repl_moved_exile_stack.  Generated by scripts/scaffold_rule, True iff ``port`` is a replacement port for the target shape., Return every other card carrying the same replacement shape.      Pool is small, _repl_moved_exile_stack_gate(), _add_repl(), conn(), _port() (+3 more)

### Community 22 - "Generated: Damage Counter Replacement"
Cohesion: 0.18
Nodes (11): _find_repl_damagedone_counters_stack(), AUTO-GENERATED rule: repl_damagedone_counters_stack.  Generated by scripts/scaff, True iff ``port`` is a replacement port for the target shape., Return every other card carrying the same replacement shape.      Pool is small, _repl_damagedone_counters_stack_gate(), _add_repl(), conn(), _port() (+3 more)

### Community 23 - "Generated: ETB Choosect Tribal"
Cohesion: 0.18
Nodes (11): _etbreplacement_other_choosect_tribal_gate(), _find_etbreplacement_other_choosect_tribal(), AUTO-GENERATED rule: etbreplacement_other_choosect_tribal.  Generated by scripts, True iff ``port`` is a keyword port for the target keyword., Return every other card carrying the same keyword.      Pool is small by gate co, _add_keyword(), conn(), _port() (+3 more)

### Community 24 - "Generated: Landwalk Island Tribal"
Cohesion: 0.18
Nodes (11): _find_landwalk_island_tribal(), _landwalk_island_tribal_gate(), AUTO-GENERATED rule: landwalk_island_tribal.  Generated by scripts/scaffold_rule, True iff ``port`` is a keyword port for the target keyword., Return every other card carrying the same keyword.      Pool is small by gate co, _add_keyword(), conn(), _port() (+3 more)

### Community 25 - "Generated: Mentor Tribal"
Cohesion: 0.18
Nodes (11): _find_mentor_tribal(), _mentor_tribal_gate(), AUTO-GENERATED rule: mentor_tribal.  Generated by scripts/scaffold_rule.py from, True iff ``port`` is a keyword port for the target keyword., Return every other card carrying the same keyword.      Pool is small by gate co, _add_keyword(), conn(), _port() (+3 more)

### Community 26 - "Generated: Melee Tribal"
Cohesion: 0.18
Nodes (11): _find_melee_tribal(), _melee_tribal_gate(), AUTO-GENERATED rule: melee_tribal.  Generated by scripts/scaffold_rule.py from t, True iff ``port`` is a keyword port for the target keyword., Return every other card carrying the same keyword.      Pool is small by gate co, _add_keyword(), conn(), _port() (+3 more)

### Community 27 - "Generated: ETB Copy Optional Tribal"
Cohesion: 0.18
Nodes (11): _etbreplacement_copy_dbcopy_optional_tribal_gate(), _find_etbreplacement_copy_dbcopy_optional_tribal(), AUTO-GENERATED rule: etbreplacement_copy_dbcopy_optional_tribal.  Generated by s, True iff ``port`` is a keyword port for the target keyword., Return every other card carrying the same keyword.      Pool is small by gate co, _add_keyword(), conn(), _port() (+3 more)

### Community 28 - "Generated: More Tribal"
Cohesion: 0.18
Nodes (11): _find_more_tribal(), _more_tribal_gate(), AUTO-GENERATED rule: more_tribal.  Generated by scripts/scaffold_rule.py from te, True iff ``port`` is a keyword port for the target keyword., Return every other card carrying the same keyword.      Pool is small by gate co, _add_keyword(), conn(), _port() (+3 more)

### Community 29 - "Generated: Changeling Tribal"
Cohesion: 0.18
Nodes (11): _changeling_tribal_gate(), _find_changeling_tribal(), AUTO-GENERATED rule: changeling_tribal.  Generated by scripts/scaffold_rule.py f, True iff ``port`` is a keyword port for the target keyword., Return every other card carrying the same keyword.      Pool is small by gate co, _add_keyword(), conn(), _port() (+3 more)

### Community 30 - "Generated: Start Tribal"
Cohesion: 0.18
Nodes (11): _find_start_tribal(), AUTO-GENERATED rule: start_tribal.  Generated by scripts/scaffold_rule.py from t, True iff ``port`` is a keyword port for the target keyword., Return every other card carrying the same keyword.      Pool is small by gate co, _start_tribal_gate(), _add_keyword(), conn(), _port() (+3 more)

### Community 31 - "Generated: Firebending 2 Tribal"
Cohesion: 0.18
Nodes (11): _find_firebending_2_tribal(), _firebending_2_tribal_gate(), AUTO-GENERATED rule: firebending_2_tribal.  Generated by scripts/scaffold_rule.p, True iff ``port`` is a keyword port for the target keyword., Return every other card carrying the same keyword.      Pool is small by gate co, _add_keyword(), conn(), _port() (+3 more)

### Community 32 - "Generated: Doctor's Tribal"
Cohesion: 0.18
Nodes (11): _doctor_s_tribal_gate(), _find_doctor_s_tribal(), AUTO-GENERATED rule: doctor_s_tribal.  Generated by scripts/scaffold_rule.py fro, True iff ``port`` is a keyword port for the target keyword., Return every other card carrying the same keyword.      Pool is small by gate co, _add_keyword(), conn(), _port() (+3 more)

### Community 33 - "Generated: Training Tribal"
Cohesion: 0.18
Nodes (11): _add_keyword(), conn(), _port(), AUTO-GENERATED tests for rule: training_tribal., TestFindTraining, TestTrainingGate, _find_training_tribal(), AUTO-GENERATED rule: training_tribal.  Generated by scripts/scaffold_rule.py fro (+3 more)

### Community 34 - "Generated: Prowess Tribal"
Cohesion: 0.18
Nodes (11): _find_prowess_tribal(), _prowess_tribal_gate(), AUTO-GENERATED rule: prowess_tribal.  Generated by scripts/scaffold_rule.py from, True iff ``port`` is a keyword port for the target keyword., Return every other card carrying the same keyword.      Pool is small by gate co, _add_keyword(), conn(), _port() (+3 more)

### Community 35 - "Port Universe Inventory"
Cohesion: 0.27
Nodes (10): _build_catalog(), _extract_clause_keys(), _extract_qualifiers(), _load_commander_names(), main(), _print_summary(), Enumerate the Forge DSL port universe present in synergy.db.  Produces a structu, Return qualifier tokens from a valid_filter expression.      Strips the main typ (+2 more)

### Community 36 - "ApiType Inventory Snapshot"
Cohesion: 0.33
Nodes (5): Phase B2 — ApiType effect-verb inventory regression test.  Snapshot of every dis, Sanity: every verb we deliberately ignore must ALSO be in the     known-verb sna, Cheap sanity check that runs without the DB — just asserts the     known-set siz, test_deliberately_ignored_verbs_are_in_known_set(), test_effect_verb_inventory_count_is_stable()

### Community 37 - "Wrath of God Fixture"
Cohesion: 1.0
Nodes (2): DestroyAll / Wrath, Wrath of God

### Community 38 - "EDHREC Validation Oracle"
Cohesion: 1.0
Nodes (2): EDHREC (validation oracle), Rationale: EDHREC is validation oracle, not design oracle

### Community 39 - "Gap Reach Formula"
Cohesion: 1.0
Nodes (1): Reach × inverse coverage. Used to rank proposals.

## Knowledge Gaps
- **903 isolated node(s):** `Unit tests for the Forge DSL parser (SPEC §5.2-§5.3).`, `A Forge DFC file separates faces with ``ALTERNATE``. Front face     contributes`, `Tests for panharmonicon complement matchers.  Builds a small in-memory SQLite da`, `Create an in-memory SQLite DB with the minimal schema needed.`, `Build a PortRow dict with defaults.` (+898 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **Thin community `Wrath of God Fixture`** (2 nodes): `DestroyAll / Wrath`, `Wrath of God`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `EDHREC Validation Oracle`** (2 nodes): `EDHREC (validation oracle)`, `Rationale: EDHREC is validation oracle, not design oracle`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Gap Reach Formula`** (1 nodes): `Reach × inverse coverage. Used to rank proposals.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `PortComplement` connect `Universal Scorer & Core` to `Utility Rules Tests (axis + cost_payoff)`, `Audit & Validation`, `Density Rules`, `Graveyard & Artifact Recursion`, `Combat Payoffs & Evasion`, `Port Parser & Fixtures`, `Rule Registry + Generated: Choose Tribal`, `Panharmonicon Rules`, `Resonance Pair Builders`, `Tap-Type Axis Tests`, `Static Ability Rules`, `Hand-Size Rules`, `Untap Combo Tests`, `Generated: Exile Replacement`, `Generated: Damage Counter Replacement`, `Generated: ETB Choosect Tribal`, `Generated: Landwalk Island Tribal`, `Generated: Mentor Tribal`, `Generated: Melee Tribal`, `Generated: ETB Copy Optional Tribal`, `Generated: More Tribal`, `Generated: Changeling Tribal`, `Generated: Start Tribal`, `Generated: Firebending 2 Tribal`, `Generated: Doctor's Tribal`, `Generated: Training Tribal`, `Generated: Prowess Tribal`?**
  _High betweenness centrality (0.477) - this node is a cross-community bridge._
- **Why does `Utility complement matchers.  Thin re-export shim so external callers can keep u` connect `Audit & Validation` to `Utility Rules Tests (axis + cost_payoff)`, `Universal Scorer & Core`, `Importer & Data Pipeline`, `Port Parser & Fixtures`, `Resonance Pair Builders`?**
  _High betweenness centrality (0.093) - this node is a cross-community bridge._
- **Why does `CandidateCache` connect `Audit & Validation` to `Universal Scorer & Core`, `Importer & Data Pipeline`, `Density Rules`, `Graveyard & Artifact Recursion`, `Combat Payoffs & Evasion`, `Graph Engine & Attributes`, `Panharmonicon Rules`, `Penalty & Coverage Analysis`, `Scope Compatibility Tests`, `Resonance Pair Builders`, `Untap Combo Tests`?**
  _High betweenness centrality (0.082) - this node is a cross-community bridge._
- **Are the 328 inferred relationships involving `PortComplement` (e.g. with `Tests for circuit bonus and CMC micro-score in universal_scorer.` and `Circuit bonus is included in the final score.`) actually correct?**
  _`PortComplement` has 328 INFERRED edges - model-reasoned connections that need verification._
- **Are the 115 inferred relationships involving `CandidateCache` (e.g. with `UniversalScore` and `Universal port-complement scorer.  Scores every candidate by counting distinct m`) actually correct?**
  _`CandidateCache` has 115 INFERRED edges - model-reasoned connections that need verification._
- **What connects `Unit tests for the Forge DSL parser (SPEC §5.2-§5.3).`, `A Forge DFC file separates faces with ``ALTERNATE``. Front face     contributes`, `Tests for panharmonicon complement matchers.  Builds a small in-memory SQLite da` to the rest of the system?**
  _903 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `Utility Rules Tests (axis + cost_payoff)` be split into smaller, more focused modules?**
  _Cohesion score 0.01 - nodes in this community are weakly interconnected._