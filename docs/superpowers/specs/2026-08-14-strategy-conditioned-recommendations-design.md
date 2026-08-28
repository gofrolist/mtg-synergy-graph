# Strategy-conditioned recommendations — design spec

**Date:** 2026-08-14
**Status:** design approved, pre-implementation
**Scope:** greenfield rebuild in a new repo, `mtg-strategy-graph`
**Location note:** written here because this repo holds the null-results that
motivate it; copied into `mtg-strategy-graph` as its founding spec once that
repo exists
**Predecessor:**
`docs/solutions/best-practices/aristocrats-death-bridge-null-result-2026-07-09.md`
(the pool-independence wall this design routes around), plus the three sibling
DECLINEs it summarises: `team-anthem-payoff-null-result-2026-07-08`,
`attack-reward-evasion-null-result-2026-07-09`,
`x-cost-scaler-null-result-2026-07-09`

## 1. Motivation

Four consecutive rule cycles DECLINED at pre-registered gates. The final
null-result diagnoses the cause as **pool-independence**:

> The GATE is commander-dependent (which commanders fire the rule), but the
> POOL is not (every gated commander gets the SAME 218+131 death-engine cards,
> modulo color identity). Reyhan (a +1/+1-counter deck that happens to
> sacrifice) and Yawgmoth (a pure aristocrats engine) both receive the
> identical death-card flood — correct for Yawgmoth, top-30-destroying for
> Reyhan.

That document concludes the only fix is a pool that varies per commander, and
that such variation is unavailable to a mechanical scorer "without importing
EDHREC popularity."

**This design takes the third option: the user supplies the missing bit.**
Reyhan-as-counters and Reyhan-as-aristocrats become two different queries.
The death pool is not down-weighted for the counters query — it is *absent*.
Pool-independence dissolves because there is no longer a single pool per
commander.

The same move makes rule isolation achievable. Today, IDF weights,
`_syn_concentration_factor` and the multi-rule pair bonus are computed across
every rule that fires for a commander, so adding a rule mathematically
perturbs the weights of all others. Partitioning by strategy scopes that
computation, turning "did this rule regress anything?" from a two-hour audit
cycle into a CI assertion.

### 1.1 The three interference channels

Rules affect each other through three distinct mechanisms, and only the first
is obvious:

| Channel | Mechanism | Today | Under the partition |
|---|---|---|---|
| **(a) Additive** | Scores sum, so a firing rule moves ranks | Global | Strategy-local |
| **(b) Normalisation** | `_compute_idf_weights` denominators run over *all* complements; `_syn_concentration_factor` and `_compute_pair_bonus(frozenset[rules])` likewise | Global | Strategy-local |
| **(c) Selection** | Top-N is a fixed window — cards added push others out | Global | Strategy-local |

**Channel (b) is the one that makes small rules expensive.** A rule that fires
for a single commander still changes the IDF denominators and pair bonuses of
every commander sharing any rule with it. The cost is not the points the rule
adds — it is the weights it perturbs. This is why a narrow,
mechanically-correct rule for one commander cannot be added today without an
aggregate regression sweep, and it is the direct cause of the observation that
the rule set has hit a ceiling.

Across strategies the partition drives all three channels to zero, because
non-manifest rules are never loaded. **Within** a strategy all three remain,
by design — rules in one manifest *should* compete. What changes is that the
affected set is exactly `manifest(S)`, a readable list of typically 5–15
rules rather than ~90. Interference is not eliminated; it is **bounded and
enumerable**, and the bound is chosen by the author.

## 2. Decisions

Settled during brainstorming; recorded with rationale because each closes off
alternatives that will otherwise resurface.

| # | Decision | Rationale |
|---|---|---|
| D1 | EDHREC tags supply the strategy **vocabulary** and which strategies a commander offers | Already the user-facing vocabulary in `mtg-edh-builder`; the dependency is small, human-legible, and product-aligned |
| D2 | Card→strategy membership is decided by **Forge ports at inference**; EDHREC theme lists are used **offline only**, to learn which port patterns characterise each strategy | Preserves hidden-gem discovery and generalises to cards/commanders with no theme page. Same design-time-only discipline as `forge_oracle` |
| D3 | **Hard partition, deny-by-default.** A rule fires only for strategies whose manifest lists it, plus a shared `core` set. IDF, concentration and pair-bonus computed within the active set | Makes non-interference structural rather than statistical; enforceable as a bitwise CI test |
| D4 | **Greenfield repo**, `mtg-strategy-graph`. Extraction moves over; `mtg-synergy-graph` is retired | The accumulated gate machinery is anchored to an EDHREC-NDCG framing this design retires |
| D5 | **Single strategy per query.** No multi-select | Removes union/double-counting design; makes results precomputable; near-synonym tags (Sacrifice/Aristocrats) are better handled by vocabulary merging than by user-side combination |
| D6 | All rules are **data rows**, no Python rule path | Partition verifiable statically; the signature miner can emit candidate rules directly; a rule's blast radius is readable without running anything |
| D7 | Aggregate NDCG@30 vs the blended EDHREC commander page is **retired as a target** | Wrong instrument by construction: a correctly focused reanimator list *should* score worse against a mixture of all that commander's decks |
| D8 | Strategy vocabulary is **seeded by EDHREC tags, not limited to them**; **micro-strategies** (1–5 commanders, hand-authored) are a first-class class | Rare commander-specific mechanics have no home in a global rule set, where channel (b) (§1.1) makes them cost a full regression sweep regardless of how narrowly they fire |
| D9 | Success is measured by **discriminative recall against theme-page inclusion rates** (`num_decks/potential_decks`), used as a **floor** with thresholds pre-registered in the plan, and **novelty rate** as the co-metric that guards it | Matches the stated expectation of being close to the theme page, without making the metric a maximand — under maximisation the optimal system is to serve EDHREC's list directly, contradicting D2. The High Synergy Cards section was rejected as the label after measurement: 7/10 shared between Korvold's Sacrifice and Reanimator (§6.1) |
| D10 | Both **tag-level** (`/tags/<theme>/<colors>`) and **commander-level** (`/commanders/<slug>/<theme>`) labels are collected | The pair separates "strategy signature is wrong" from "commander-specific rules are missing"; neither label does that alone (§6.2) |

## 3. Core model

### 3.1 Strategy as a first-class object

One data row per strategy, four parts:

```
strategy: aristocrats
  edhrec_aliases:  ["Aristocrats", "Sacrifice"]      # tag → strategy mapping
  availability:    <predicate over COMMANDER ports>   # can this commander play it?
  signature:       <predicate over CANDIDATE ports>   # what belongs to it?
  manifest:        [rule ids that may fire]           # deny-by-default
```

`availability` is **mechanical, not EDHREC-derived**. EDHREC tags validate the
predicate offline; they never answer it at runtime. This is what lets the
system serve commanders with no theme page and cards printed after the last
scrape.

`generic` is an ordinary strategy whose manifest is the core rule set (ramp,
draw, protection, the commander's own literal circuits). No selection resolves
to `generic` — not a special code path, just another row. This mirrors the
portal's current no-theme behaviour, which falls back to the blended EDHREC
commander page (`algorithm/pipeline.py:228`, guarded by
`if constraints.theme_slugs:` — there is no default theme today).

**The vocabulary is seeded by EDHREC tags but not limited to them.** Nothing
in the model requires a strategy to be popular — it is an availability
predicate plus a manifest, and the partition granularity goes down to a single
commander. Two classes:

| Class | Backing | Signature source | Evaluated by |
|---|---|---|---|
| **Tagged** | An EDHREC tag (or merged tag group) | Mined (§5) | Judgment + theme-list metrics (§6) |
| **Micro** | Mechanical only; 1–5 commanders, no EDHREC label | Hand-authored from the Forge DSL | Judgment + the isolation test; no aggregate gate |

Micro-strategies are the answer to rare, commander-specific mechanics, which
today have nowhere to live except the global rule set — where channel (b)
(§1.1) makes them cost a full regression sweep. A micro-strategy with two
rules has an interference set of two rules plus `core`; every other strategy
is bitwise identical, and the isolation test proves it, so no aggregate
measurement is required at all.

Four constraints keep the class from degenerating into hand-maintained
whitelists:

1. **Port predicate, never a card list** — a newly printed card of the same
   shape joins automatically.
2. **Never auto-applied** — reachable only by explicit user selection, so it
   cannot affect anyone's default results.
3. **Declares its availability predicate** — its commander count is
   reportable; one claiming 400 commanders is misfiled and belongs as a
   tagged strategy.
4. **No aggregate gate** — judged on its own commanders. Aggregate gating is
   what made these rules impossible before.

### 3.2 Scoring

```
score(card | commander, strategy) =      Σ            w_r · idf_r(card | strategy)
                                  r ∈ manifest(strategy) ∪ core
```

Two departures from the old engine:

**Deny-by-default.** A rule outside the active manifest does not fire, is not
summed, and is not loaded. Reyhan-as-counters does not rank the death pool
low; it never sees it.

**Strategy-local IDF.** The IDF denominator is the strategy's candidate pool,
not the global card pool. This is better modelling, not merely isolation
hygiene: a Blood Artist is unremarkable among aristocrats payoffs and
extraordinary in a counters pool, and global IDF cannot express that. It also
structurally removes the coupling — concentration factor and pair bonus become
strategy-local.

### 3.3 Rules are data

Every rule is a row: `(rule_id, strategy_ids, commander_predicate,
candidate_predicate, weight)`. The old repo proved this out for 16 rules via
`port_graph/interpreter.py` but kept a parallel Python path; the new repo has
only the data path.

Worked example. Forge yields these ports for two canonical reanimation cards:

```
Reanimate     effect ChangeZone  valid_filter=Creature   Graveyard → Battlefield
Animate Dead  effect ChangeZone  valid_filter=Enchanted  Graveyard → Battlefield
```

The rule row that matches the first:

```
rule_id:             reanimation_target
strategies:          [reanimator]
candidate_predicate: port_type        = effect
                     event_class      = ChangeZone
                     zone_origin      contains Graveyard
                     zone_destination = Battlefield
                     valid_filter     matches ^Creature  AND NOT contains Opp
weight_tier:         primary
```

Live-DB counts that make the authoring stakes concrete: this predicate matches
**245** cards. Dropping the `valid_filter` clause matches **693** — the extra
448 include `Land.YouOwn` (6), `Artifact.YouCtrl` (10) and `Creature.OppCtrl`
(8, i.e. stealing from opponents' graveyards, a different deck). The flood is
visible in a SQL count before any scoring code is written.

Note also that `Animate Dead` carries `valid_filter=Enchanted` and is *missed*
by the rule above, along with 164 cards using `Enchanted` / `Remembered` /
`Targeted` / empty filters. Those need a second rule at a different weight
tier. This is the class of omission the miner (§5) catches and hand-authoring
reliably forgets.

Accepted cost: the predicate DSL must be expressive enough, and rules that
genuinely needed Python (the depth-2 pathway cascade) either get DSL support
or do not come across. The starting op set is the old
`port_graph/vocabulary.py::GATE_OPS`, extended as mining demands.

### 3.4 The isolation invariant

> The ranked output for strategy S is a function of *only* the rules in
> `manifest(S) ∪ core`.

Enforced as a CI test that mutates every rule outside S — adds synthetic
rules, perturbs weights, deletes rules — and asserts S's output is **bitwise
identical**.

Honest limit: this makes rules **non-interfering**, not **correct**. A bad
aristocrats rule still ruins aristocrats. What it buys is the ability to work
on one strategy at a time, which is exactly what the four DECLINE cycles cost.

### 3.5 `core` is frozen

`core` is the one surface every strategy shares, so a change there propagates
to everything and is the single place the old regression pain survives — by
design, and confined.

Three consequences, binding on implementation:

- **Core stays small**: ramp, draw, protection, and the commander's own
  literal circuits. Nothing archetype-flavoured.
- **Core changes require the full regression sweep** across every strategy's
  golden snapshots (§7). Strategy-manifest changes do not.
- **When in doubt, it goes in a strategy.** A wrong call there costs one
  strategy; a wrong call in core costs all of them. This is the highest-stakes
  authoring question in the system and should be treated as such in review.

## 4. Data pipeline

### 4.1 ETL move

Moves verbatim: `parser.py`, `ports.py`, `attributes.py`, `tokens.py`,
`copy_face_from.py`, `etb_replacement.py`, `importer.py`, `db.py`.

Does not move: `universal_scorer`, `complement_rules/`, `penalties`,
`heuristics`, `graph_engine`, `bench/`, `portfolio`, `quality`.

Gated by a **parity test**: the new importer must produce exactly **110,366
ports from 32,624 cards** (re-verified 2026-08-15 against `data/synergy.db`)
with a per-`(port_type, event_class)` histogram identical to the old DB.
A silent extraction regression would poison every mined signature
downstream.

### 4.2 EDHREC theme corpus

Two endpoints, both already proven in `mtg-edh-builder`
(`edhrec_themes.py`, `edhrec.py::parse_synergy_cards`):

| Source | Yields | Table |
|---|---|---|
| `json.edhrec.com/pages/commanders/<slug>.json` → `panels.taglinks` | strategies offered + deck counts | `commander_themes(commander_slug, theme_slug, label, deck_count, scraped_at)` |
| `json.edhrec.com/pages/commanders/<slug>/<theme>.json` → `container.json_dict.cardlists[].cardviews[]` | the card list for that (commander, strategy) | `theme_cards(commander_slug, theme_slug, card_name, synergy, inclusion, category)` |
| `json.edhrec.com/pages/tags/<theme>/<color-identity>.json` | the commander-independent card list for that (strategy, colours) — the tag-level label of §6.2 — **plus `topcommanders` (24)**, a free per-theme commander list used to pick the slice corpus honestly | `tag_cards(theme_slug, color_identity, card_name, synergy, inclusion, category)`, `tag_commanders(theme_slug, color_identity, commander_name, rank)` |

Every row must preserve `num_decks`, `potential_decks` and `section`.
`num_decks / potential_decks` is the inclusion rate and **is the §6.1 label**;
`section` is needed to reconstruct the High Synergy set as a secondary sanity
signal.

Endpoint facts established by probe (2026-08-15): sections are capped at 50
cards, per-card-type sub-pages (`/commanders/<slug>/creatures`) return 403,
and `/pages/themes/<theme>.json` returns 403. Commander pages yield ~319
cards, theme pages ~313, tag pages ~338.

**Optional deeper tier — real decklists.** `/pages/decks/<slug>.json` returns
one row per deck (20,489 for Korvold) with per-deck `tags`, `urlhash`,
bracket, price; `edhrec.com/api/deckpreview/<urlhash>` then returns the **full
100-card list** plus that deck's tags and source URL. This enables held-out
card prediction — hide N cards from a real deck, measure whether we rank them
back — the strongest eval available and one needing no synergy formula at all.

**Deferred out of the slice.** It costs ~300 requests per (commander, theme)
against ~1 for the inclusion-rate path, and a 40-deck-per-theme sample
reproduced the cheap path's answer exactly (§6.1). Tag coverage is 22.3% of
decks (4,560 of 20,489 for Korvold), so thin themes will have thin labels.
Revisit in sub-project E.

Stored in a **separate `themes.db`** that the recommendation path physically
cannot open — the design-time-only discipline kept structural rather than by
convention.

Under D5 the fetch unit and the eval label unit are the same
`(commander, theme)` pair, e.g.
`edhrec.com/commanders/korvold-fae-cursed-king/sacrifice`.

Scope: a full scrape is ~2,761 commanders × ~30 themes ≈ 80k requests and is
out of scope. The slice needs ~20 commanders × their themes ≈ 400 requests,
cached to disk with provenance rows.

**Name resolution.** EDHREC keys by card name, the ports DB by `oracle_id`.
DFCs, split cards and alternate printings need a normalisation map. Unresolved
names must be logged and counted, not silently dropped — a 5% silent drop rate
would bias every mined signature. Covered by a test (§6).

### 4.3 Vocabulary normalisation

EDHREC's 40+ tags are overlapping and not all of them are strategies. Part of
the slice is classifying them:

- **distinct strategies** — Reanimator, Tokens, +1/+1 Counters, Landfall…
- **aliases to merge** — Sacrifice and Aristocrats nearly nest; they should
  normalise to one strategy carrying both slugs
- **not strategies** — deck attributes rather than spines: Budget, Midrange,
  Good Stuff, Combo

This classifies the *tagged* class only. Micro-strategies (§3.1) originate
from Forge DSL inspection, not from this table, and are added independently of
any scrape.

**Tags are noisy, and one measured example should govern expectations.**
Tags come from user-authored Archidekt tags and Moxfield hubs, are
multi-valued (a sampled Korvold deck carried 8), and do not always mean what
the word says. Measured 2026-08-15: **Korvold's `Reanimator` cohort is
lands-matter / graveyard-value**, not reanimation — its distinctive cards are
Lotus Cobra, Azusa, World Shaper, Crucible of Worlds, Life from the Loam,
Krosan Wayfarer, with almost no `Graveyard → Battlefield` present.

The mitigation is already in §5.1: positives are pooled across *all*
commanders tagged T, so Meren, Karador and Chainer wash out Korvold's
idiosyncratic usage. The consequence is that **a per-commander label is not
evidence about what a strategy means** — only the cross-commander aggregate
is. This governs kill criterion 1 (§9).

## 5. Signature mining

Design-time only. Produces proposals for human review; never mutates committed
artefacts. Applies to **tagged** strategies; micro-strategies are hand-authored
and skip this pipeline entirely (they have no theme list to mine).

### 5.1 The contrast is within-commander

To learn what "Reanimator" means, compare the Reanimator list of a commander
against *that same commander's other theme lists*:

```
positives P_T  = cards on (C, T) lists, for commanders C tagged T
background B_T = cards on (C, T') lists for the SAME commanders C, T' ≠ T
```

Contrasting across different commanders instead would learn colour and
commander bias — "Reanimator means black" — a useless-but-plausible signal
that would pass a naive eval.

### 5.2 Features and ranking

Port patterns at several granularities: coarse `(port_type, event_class)`
through `+ zone_origin/zone_destination`, `+ counter_type`,
`+ granted_keyword`, `+ port_attributes` rows.

Ranked by smoothed log-odds of `P(pattern | P_T)` vs `P(pattern | B_T)`, with
floors on support (≥N distinct cards, ≥K distinct commanders) so
single-commander quirks cannot promote. Exact floors are calibrated during
implementation against the slice corpus and recorded in the plan.

### 5.3 Output is a proposal

`signature_proposal.json` ranks patterns with support, lift, example cards,
and a flag for those already covered by an existing rule. A human promotes
them into rule rows. Direct precedent: `.audit/optimize_proposal.json` never
auto-writes `scoring_weights.json`.

### 5.4 Anti-whitelist guard

The failure this pipeline invites is a "signature" that has memorised EDHREC's
list. A pattern must clear all three checks to be promotable:

1. **Expressible** — a port predicate, not a card-name set.
2. **Generalises** — `|cards matched outside any training list| / |cards
   matched|` above a floor. A pattern matching only training cards is a
   whitelist in costume.
3. **Holds out commanders** — mine on 80% of tagged commanders, confirm
   support on the unseen 20%.

Guard 2 is also what preserves hidden-gem discovery: a card on no EDHREC list
can still surface because its *ports* match the signature.

## 6. Evaluation

### 6.1 The label and the metric

**The label is the theme page's per-card inclusion rate**, not its High
Synergy Cards section. Every `cardview` on
`/pages/commanders/<slug>/<theme>.json` carries `num_decks` and
`potential_decks`; their ratio is the card's inclusion rate *within that
theme*. One request yields ~313 cards with inclusion rates.

#### Why not High Synergy Cards

The High Synergy section holds exactly 10 cards (2,720 of 2,761 commanders in
the current scrape). It was the obvious label and it is **a poor
discriminator** — measured 2026-08-15:

- Korvold's *Sacrifice* and *Reanimator* high-synergy lists share **7 of 10**.
- The shared seven are cEDH staples with no thematic content: Dark Ritual,
  Veil of Summer, Culling the Weak, Tinder Wall, Ragavan, Diabolic Intent,
  Mayhem Devil.
- Over the full 313-card lists, Jaccard(Sacrifice, Reanimator) = **0.735** —
  ~85% is shared Jund goodstuff and lands.

Gating on that label would have capped the cross-strategy divergence metric
(§6.4) at the ground truth's own 30% separation.

Ranking the same payload by **lift** — inclusion in this theme against
inclusion in the commander's other themes — separates cleanly:

| | Top distinctive cards |
|---|---|
| Sacrifice | Mahadi, Dockside Extortionist, Plaguecrafter, Witch's Oven, Grim Hireling, Brass's Bounty, Xorn |
| Reanimator | Lotus Cobra, Aftermath Analyst, Lumra, Dryad Arbor, World Shaper, Azusa, Krosan Wayfarer |

**Jaccard(top-15 distinctive) = 0.000.** Cross-validated against 40 real
decklists per theme sampled through `deckpreview` (§4.2): the two independent
methods agree, so the cheap one-request path suffices.

#### The two-tier metric

```
core recall        = |top-30 ∩ {cards ≥ inclusion floor in theme T}| / |labels|
discriminative recall = |top-30 ∩ {top-N by lift(T vs commander's other themes)}| / N
```

Core recall asks "are these sane cards for this deck". Discriminative recall
asks "did choosing the strategy change anything" — and unlike core recall it
cannot be gamed by returning generic staples, which is why it is the gate that
matters. Floors and `N` are calibrated during implementation against the slice
corpus and recorded in the plan.

### 6.2 Two label levels

Both EDHREC pages are used, and the pair is diagnostic in a way neither is
alone:

| Label | URL shape | Ground truth for |
|---|---|---|
| **Tag-level** | `/tags/<theme>/<color-identity>` | The **strategy manifest alone** — commander-independent |
| **Commander-level** | `/commanders/<slug>/<theme>` | **Manifest + commander ports** |

Matching the tag page while missing the commander page means the strategy
signature is right and commander-specific rules are missing. The reverse means
the strategy is overfitted to the commander. A single metric cannot separate
those.

### 6.3 Measured baseline (2026-08-14, current engine, blended commander page)

**Measured against the High Synergy Cards label, which §6.1 discards.** It is
retained because the *diagnosis* it produced — which commanders fail and how —
is label-independent and is what the design responds to. The numbers
themselves do not transfer: the §6.4 thresholds must be re-derived against the
inclusion-rate label before they mean anything.

Aggregate `recall@30` = **0.230** over ten sacrifice/counters commanders:

| Commander | recall@30 |
|---|---|
| Reyhan, Last of the Abzan | 6/10 |
| Teysa Karlov | 5/10 |
| Chatterfang, Squirrel General | 4/10 |
| Prossh, Skyraider of Kher | 3/10 |
| Judith, the Scourge Diva | 3/10 |
| Ghave, Guru of Spores | 2/10 |
| Korvold / Meren / Yawgmoth / Karador | **0/10** |

The distribution is the diagnosis. The engine performs where a strategy has a
**tight mechanical signature** (Reyhan's +1/+1 counters — Corpsejack Menace,
Hardened Scales, Winding Constrictor) and collapses to zero where the strategy
is defined by archetype rather than port shape. The zero set is precisely the
four DECLINE commanders.

**Two distinct failure modes, which `recall@30` alone conflates and the report
must therefore decompose:**

- **Ranking failure** — Korvold: the cards are in the pool but buried. Mayhem
  Devil rank 35 (near miss), Ruthless Technomancer 267, Tireless Provisioner
  425, Deadly Dispute 696, Diabolic Intent 699, Warren Soultrader 779,
  Chatterfang 783; three score nothing at all.
- **Coverage failure** — Yawgmoth: the entire candidate pool is **49 cards**
  out of ~2,500 legal in his colours. All 10 labels are outside it. No ranking
  change can help.

Design note this raises: deny-by-default makes pools *smaller*, and Yawgmoth's
is already 49. The model still helps because under the partition the pool
derives from the **strategy**, not from the commander's port bridges —
selecting `aristocrats` fires the death-payoff rules whether or not Yawgmoth's
own ports bridge to them, and his 10 labels are exactly that pool. This is the
DECLINED `aristocrats_death_bridge` rule with its scope corrected; the
null-result itself records that "Inspecting Yawgmoth's missed EDHREC cards had
confirmed the pool *is* where his picks live." **Pool size per (commander,
strategy) is therefore a reported diagnostic, not an afterthought.**

### 6.4 Gates

**Primary gate: human judgment.** ~20 commanders × 3 strategies, old top-30
beside new top-30, each list marked good/bad with a reason. Dissatisfaction
with current output is the thing being fixed and no proxy substitutes for it.

**Quantitative gate — a floor, not a maximand.** The gate is
**discriminative recall** (§6.1); core recall is reported alongside it as a
sanity check.

Thresholds are **pre-registered in the implementation plan, not here.** The
earlier draft's ≥ 0.50 aggregate / ≥ 3-of-10 per commander was calibrated
against the High Synergy label that §6.1 discards, so carrying it over would
be a number with no measurement behind it. Task 1 of the plan is: build the
inclusion-rate labels for the slice corpus, measure the current engine against
them, and set the floor from that baseline. The *shape* is fixed and binding —
an aggregate floor plus a per-commander floor with **no zeros permitted**,
since eliminating the 0/10 commanders is the point.

Recall is deliberately a **floor**. Maximising it is self-defeating: the
optimal system under maximisation is to serve EDHREC's theme list directly,
which is the option rejected at D2 and makes the mechanical engine
strictly redundant.

**Co-metrics — reported, and one of them guards the floor:**

- **Novelty rate** — fraction of top-30 absent from every EDHREC list for that
  commander yet matching the strategy signature. Successor to
  `hidden_gem_hit_rate`. This is the guard: discriminative recall → 1.0 with
  `novelty → 0` means an EDHREC mirror has been built, a cheaper product that
  needs none of this architecture. Success is clearing the floor **while**
  retaining defensible off-list cards.
- **Cross-strategy divergence** — Jaccard between the same commander's top-30
  under different strategies. EDHREC-free and measures the target property
  directly; near-identical lists mean the project failed. The old architecture
  could not produce this number at all.
- **Pool size** per (commander, strategy) — separates coverage failure from
  ranking failure (§6.3).

Per D7, aggregate NDCG@30 against the blended commander page is retired.

## 7. Tests

| Test | Catches |
|---|---|
| Extraction parity — 110,366 ports / 32,624 cards (re-verified 2026-08-15), per-`(port_type, event_class)` histogram | Silent ETL regression poisoning every downstream signature |
| Isolation invariant — mutate all rules outside S, assert S bitwise identical, for every S including micro-strategies | The regression class behind the four DECLINEs; also the *only* gate a micro-strategy must clear (§3.1) |
| Core-change sweep — full golden-snapshot diff across every strategy when `core` changes | The one surface where global regression survives (§3.5) |
| Generalisation guard over committed **mined** rules (§5.4; micro-strategy rules are exempt — they have no training list, and constraint 1 of §3.1 covers them instead) | Signatures degenerating into memorised EDHREC lists |
| Golden output snapshots per (commander, strategy) | Ranking changes become visible diffs in PRs |
| Name-resolution coverage | Silent EDHREC-name drop biasing the mining corpus |

## 8. Scope of the first slice

**In:** new repo `mtg-strategy-graph`; ETL move + parity test; theme scrape
for ~20 commanders **plus the tag-level pages for the slice strategies × the
colour identities in play** (§6.2); vocabulary normalisation for the tags in
play; mining for
`aristocrats`, `reanimator`, `plus1_counters`, `generic`; **one hand-authored
micro-strategy** as a worked example of the class (§3.1) — chosen during
implementation from a commander with a rare mechanic and no useful EDHREC tag;
rule DSL + interpreter; deny-by-default partitioned scoring with
strategy-local IDF; CLI `recommend --commander X --strategy Y --explain`;
judgment report + three metrics; isolation and parity tests.

The micro-strategy is in scope deliberately: it is cheap (hand-authored, 1–3
rules, no scrape, no mining) and it validates the property that motivated the
rebuild — that a narrow rule can be added with provably zero effect on
anything else.

**Commander selection:** ~20 commanders, chosen so that most carry **two or
more** of the three slice strategies — the demonstration is the *same*
commander recommending differently per strategy. Korvold, Fae-Cursed King
carries all three (Sacrifice 1597 / Aristocrats 876 / +1+1 Counters 439 /
Reanimator 217). Reyhan is included deliberately: it is the documented flood
casualty; Yawgmoth and Karador for the coverage-failure mode (§6.3).

The rest of the list is drawn from the `topcommanders` block on each slice
strategy's tag page (§4.2) rather than from memory — 24 ranked commanders per
(theme, colour identity), free with the label fetch. Commanders whose label
turns out to be miscoded (the §4.3 tag-noise case) are kept and *reported*,
not quietly dropped, since silently curating the corpus toward agreeable
labels would make every downstream number meaningless.

**Out:** deck context — the API accepts `deck: Sequence[str] = ()` from day
one and ignores it, documented, so adding it later is not a breaking change;
full 2,761-commander scrape; `mtg-edh-builder` integration and packaging;
weight optimisation; embeddings; combo detection; budget / bracket /
playstyle.

## 9. Kill criteria, pre-registered

Stop and rethink rather than scale, if:

1. Mined Reanimator signatures do not rank `ChangeZone Graveyard→Battlefield`
   at the top **when mined across all Reanimator-tagged commanders** — the
   mining method is broken. Measured against a *single* commander this
   criterion is invalid: Korvold's `Reanimator` cohort is lands-matter (§4.3),
   so a correct signature would appear to fail there. Evaluate the aggregate,
   never one commander.
2. Cross-strategy divergence is low — conditioning is not reaching the output,
   and building 40 strategies would multiply a broken mechanism.
3. The generalisation guard rejects nearly every mined pattern — signatures
   are whitelists and the membership model needs rethinking.
4. Discriminative recall for the coverage-failure commanders (Yawgmoth,
   Karador) stays at zero — if a strategy-supplied pool cannot reach cards the
   commander's own ports never bridge to, the central mechanism does not work
   and no amount of ranking work will rescue it.
5. Discriminative recall clears the floor but novelty collapses to ~0 — an
   EDHREC mirror, which does not need this architecture (§6.4).

## 10. Roadmap beyond the slice

| # | Sub-project | Depends on |
|---|---|---|
| A | Foundation — repo, extraction move, ports DB | — |
| B | Strategy data — full scrape, complete vocabulary | A |
| C | Signature learning at scale — all strategies | B |
| D | Partitioned engine — full rule migration | A |
| E | Eval harness — noise bands, regression suite | B, D |
| F | Deck context — selected cards condition the ranking | D |
| G | Builder integration — replace the pinned wheel; `ThemeSelector.tsx` and `theme_slugs: list[str]` become single-select per D5 | D |

The slice is a vertical cut through A, B, C, D and a thin E. Each later
sub-project gets its own spec → plan → implementation cycle.
