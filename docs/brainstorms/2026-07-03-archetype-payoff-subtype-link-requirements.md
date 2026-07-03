# Requirements — Archetype-payoff subtype link (aristocrats-shaped): token-producer → subtype-death payoff

- **Date:** 2026-07-03
- **Status:** parked — core premise falsified in 5-persona review (user-accepted 2026-07-03). Not advanced to `/ce-plan`. Next lever is the eval-fixture rebuild (see Reframe option 3). Null recorded in `docs/solutions/best-practices/archetype-payoff-idf-flood-null-result-2026-07-03.md`.
- **Scope:** Deep (cross-cutting, cliff-prone — new synergy *direction*, not a new axis-feeder)

## Review verdict (5-persona ce-doc-review, 2026-07-03) — IDF-damping premise is arithmetically false

The doc's load-bearing assumption — "common subtype → low IDF → flood
self-damps" — does not hold, verified against `universal_scorer.py:869`
(`base_idf_non_flat[key] = (1.0 / math.log2(1.0 + n)) * cond_mult`, N =
distinct candidates per `(rule_id, cmdr_event, cand_event, filter_group)`
tuple). Even with `filter_group` = subtype (best case), IDF across the
meaningful-supply band is nearly flat:

| Subtype | N producers | IDF `1/log2(1+N)` |
|---|---|---|
| Egg / Cleric | 2 | 0.63 (but ~0 coverage) |
| Sliver | 8 | 0.32 (but ~0 coverage) |
| Saproling | 81 | **0.157** |
| Zombie | 145 | **0.139** |
| Treasure | 289 | **0.122** |

`log2` flattens N=46–289 into a 0.12–0.16 window; IDF damps *per-item
weight* but the flood is driven by *count* (145 Zombie producers × 0.139
each, against a commander scoring archetype cards at zero, cracks the
top-30). **There is no N-range that is both flood-safe AND high-coverage**
(the only genuinely-high-IDF subtypes have ~2 producers). Any "principled
specificity threshold" separating Saproling (0.157) from Zombie (0.139) is
a ~0.018-wide slice — i.e. a raw supply cutoff, which is the rarity
whitelist `feedback_no_individual_rules` forbids. So the IDF-only matcher's
honest outcome space is **DECLINE or a disguised whitelist**.

Three further review findings compound this:
- **Eval-set dilution (product-lens P1):** only ~2–3 of the 75 demanding
  commanders sit in the golden-100 fixture with a subtype-keyed death
  trigger; a genuine local win dilutes below the fixture noise band, so
  R3's aggregate SHIP gate is structurally unable to register it.
- **R0/R3 measure different populations (P1):** R0's intra-cohort share
  can green-light a build R3's whole-fixture aggregate never sees.
- **Direction may be wrong half (adversarial P2):** EDHREC aristocrats
  lists favor sac *outlets* and death-payoff *pieces* (Blood Artist), not
  token *producers*; the producer direction rests on one anecdote
  (Mycoloth/Slimefoot) and may be a minority of the addressable misses.
- **Overlap unmeasured (adversarial P2):** `self_bridging_cascade` already
  targets "aristocrats-chain token-makers"; Mycoloth-class cards may
  already be scored for some of the 75, further shrinking marginal share.
- **Not even a direct link (feasibility P2):** producing subtype-S tokens
  does NOT fire an S-death trigger — an intermediate *sacrifice outlet* is
  required for the tokens to die. The "two halves" framing is really a
  depth-2 path (produce → sac outlet → death payoff) with the middle node
  unmodeled; a direct producer↔payoff match would fire with no sac bridge,
  re-creating the cut `valid_filter` channel. (Also: `event_class='Dies'`
  has zero rows — death is `Sacrificed` or `ChangesZone` BF→GY; the 335/75
  demand counts are commander-scoped, not the card-scoped ~2,460/409.)

**Verdict:** do NOT advance to `/ce-plan` as framed. The requirements
below are retained as the falsified record. The *lever* (archetype-payoff
link) is not disproven — but the IDF-weighted broad-subtype-production
mechanism is, and a viable version needs an intrinsically-narrow gate
(not IDF) or a different edge (outlet/payoff-piece). See "Reframe options."
- **Origin:** `/ce-brainstorm` — the primary open lever surfaced three times
  this session (resource-flow DECLINE; Slimefoot scoping; gy_fuel
  vocabulary DECLINE). See `memory/project_no_rules_archetype_gap.md` and
  `docs/solutions/best-practices/resource-flow-demand-null-result-2026-07-02.md`.

## Problem

The NO_RULES forensics bucket (~43% of EDHREC-label misses) is dominated
by on-theme archetype synergies the engine scores **zero** — not
goodstuff. The canonical case: **Slimefoot, the Stowaway** has a
`trigger ChangesZone / Saproling.YouCtrl → DamageAll` death payoff, and
**Mycoloth** (produces Saprolings) is absent from his entire top-200. The
engine extracts *both halves* — Slimefoot's payoff port fires; Mycoloth's
Saproling-token production is recorded — but **no rule connects a
token *producer* to a subtype-keyed *payoff***. The missing edge is:

> "Candidate manufactures the fodder (tokens of subtype **S**) that fires
> the commander's **S**-keyed death payoff."

This recurs across the bucket: Yawgmoth (aristocrat death-drain),
Titania/Ashling (Elemental death), the Clue/Food/Blood sacrifice
sub-families, etc.

## Why this is a genuinely new direction (not a re-tread)

Verified against `data/synergy.db`:

- **Distinct from `peer_tribal_keyword`** (16 declarative rules): those
  match tribal *membership* (commander cares about tribe X ↔ card *is*
  tribe X). This matches *production* (card *makes* subtype-S tokens) →
  *payoff* (commander triggers on subtype-S death). Different edge.
- **Distinct from the pathway `valid_filter` channel** that the
  2026-04-23 audit *cut for flooding*: that channel matched broad
  **card types** (Creature/Artifact). Subtype (Saproling ≠ Creature) is
  1–2 orders of magnitude narrower.
- **Data substrate already exists** — no port-extraction work needed:
  - Supply: `port_attributes.attr_kind='token_subtype'` — **2,721**
    token-producing cards, subtype-labeled.
  - Demand: `card_ports.valid_filter` on `trigger` ports names the
    subtype — **335** cards total; **75** in the aristocrats-shaped
    (death/sacrifice-keyed) subset chosen for the first cut.

## Scope — aristocrats-shaped only (first cut)

Fire the link **only** when:
1. Commander has a `trigger` whose `event_class` is a **death** event
   (`Dies` / `Sacrificed` / `ChangesZone` Battlefield→Graveyard) AND
   whose `valid_filter` names a creature-or-token **subtype S**; AND
2. The commander's matched effect is a payoff (damage/drain/draw/counter
   — not a bounce/tuck); AND
3. Candidate produces tokens of subtype **S** (`token_subtype=S`).

Chosen because death-payoff commanders are a naturally bounded set (75),
the edge is the sharpest and most EDHREC-aligned, and it excludes the
flood-prone ETB/attack subtype payoffs on common types.

## The flood map (per-subtype supply/demand — this IS the risk model)

| Band | Subtypes (demand cards / producer supply) | Read |
|---|---|---|
| **Tight (high IDF, safe)** | Egg 2/2, Cleric 1/2, Tentacle 1/2, Sliver 1/8, Samurai 1/7, Mutant 1/7, Insect 2/46, **Saproling 1/81** | Producer set small or rare → IDF-clean; the Slimefoot win lives here |
| **Mid** | Clue 15/26, Elf 5/43, Blood 4/40, Vampire 3/29, Angel 2/36 | Clue is the largest single demand (a whole missed sub-family), supply modest |
| **Flood-prone (low IDF)** | Food 9/131, Zombie 6/145, Goblin 4/72, Treasure 1/289, Spirit 1/137, Soldier 1/103, Human 2/88 | Ubiquitous subtypes; a naive link surfaces 100+ producers → the exact flood that killed uniform tribal rules |

The scorer's IDF weighting *should* down-weight the flood-prone band
(common subtype → low specificity → low weight). **Whether IDF damping
alone prevents regression on the low-IDF band is the empirical question
this cycle must answer before shipping.**

> ⚠️ **Superseded by the Review verdict (top of doc).** The "high IDF /
> low IDF" band labels above are arithmetically false: `1/log2(1+N)` puts
> Saproling (0.157), Zombie (0.139), and Treasure (0.122) within 0.035 of
> each other. Read the flood map only as a *supply/demand* map; the IDF
> column does not separate safe from flood-prone.

## Requirements

**R0 — Stage-0 addressable-share + flood measurement is a BLOCKING gate**
(bars pinned BLIND before measurement, per the resource-flow precedent).
Before any scoring code, a read-only instrument reports, for the
aristocrats-shaped scope:
- **Addressable share:** of the NO_RULES misses for the ~75 demanding
  commanders, what fraction is a token-producer of the demanded subtype?
  (Numerator = misses reachable by the link; denominator = their NO_RULES
  misses.) Compare against a null model (random producers).
- **Per-subtype flood projection:** for each demanding commander, how
  many producers would enter its top-30 under live IDF, and does any
  low-IDF subtype (Food/Zombie/Treasure) displace archetype picks?
- **Funding bars (pin numeric values before running):** addressable
  share ≥ [TBD]; ≥ [TBD] commanders gain a top-30 EDHREC-corroborated
  producer; AND no low-IDF-band commander regresses beyond noise.

**R1 — The link matcher (only if R0 clears).** A rule matching commander
death-subtype-S trigger ↔ candidate `token_subtype=S`. Emits a
`PortComplement` weighted by the standard IDF of subtype S (no hand-tuned
per-subtype weights — `memory/feedback_no_individual_rules.md`).

**R2 — IDF is the only flood defense; no per-subtype allow/deny list.**
If IDF damping is insufficient (R0 shows low-IDF floods), the answer is
DECLINE or a *principled* specificity threshold (e.g. exclude subtypes
whose producer supply exceeds an IDF cutoff), NOT a hand-curated subtype
whitelist. A whitelist is the per-archetype rule the feedback forbids.

**R3 — Kill-test gate + re-pin.** Scoring-path change → `bench.py audit`
must pass; SHIP only on strictly-positive aggregate NDCG@30 with no
regression beyond noise; re-pin on SHIP.

**R4 — Explainability.** `--explain` emits the link
(`produces <S> tokens → <commander> <S>-death payoff`).

## Success criteria

- Recovers EDHREC-corroborated NO_RULES misses on aristocrats-shaped
  commanders (Slimefoot→Saproling producers, Clue/Food sacrifice
  commanders) with strictly-positive aggregate NDCG@30 and no
  regression beyond noise on any demanding commander.
- Zero hand-tuned per-subtype weights; the effect is IDF-weighted and
  self-limiting by subtype specificity.
- OR a clean DECLINE at Stage 0 with the flood map recorded — a
  successful cycle outcome.

## Non-Goals

- **ETB / attack subtype payoffs** (the broader 335-card demand) — the
  flood-prone zone; deferred to a possible second cut only if the
  aristocrats cut SHIPs cleanly.
- **Tribal membership / lord effects** — covered by
  `peer_tribal_keyword`; not this edge.
- **New port extraction** — both halves already exist in columns.
- **Uniform go-wide / token-count rules** — the flood-irreducible class
  (`memory/project_flood_as_archetype_irreducible`). This link is
  subtype-*specific*, which is the whole point of the bet.

## Reframe options (the lever, minus the falsified mechanism)

The review kills the *IDF-weighted broad-subtype-production* mechanism, not
the archetype-payoff lever. Viable-looking successors, each of which needs
its own cheap pre-flight before any build:

1. **Intrinsically-narrow gate, not IDF.** Require the candidate to share
   *more* with the commander than the subtype — e.g. depth-2: candidate
   produces subtype-S tokens AND has an internal sac/death loop
   reinforcing the payoff. This is close to `self_bridging_cascade`; the
   pre-flight is "how many of the 75 misses does the existing cascade
   already reach, and what does adding the token_subtype channel add?"
2. **Flip the direction to outlet / payoff-piece.** If EDHREC misses are
   dominated by sac outlets (Ashnod's Altar-class) and death-payoff pieces
   (Blood Artist / Zulaport), the addressable edge may be commander-death-
   payoff ↔ *other death-payoff pieces of the same subtype-agnostic kind*,
   which is not supply-count-flooded. Pre-flight: role-split the misses.
3. **Fix the ruler before the rule.** The eval-set dilution finding says
   the golden-100 fixture can't see a 75-commander effect. Rebuilding /
   over-sampling the audit fixture on the demanding cohort is a
   prerequisite for *any* of these — and is reusable infrastructure
   regardless of which mechanism wins.

The honest read: option 3 (fixture) is the highest-leverage next step
because it unblocks measurement for every archetype-payoff variant, and it
is a clean infra task with no flood risk.

## Open questions for planning

- **Where does the IDF land for the flood-prone band?** Pull the actual
  IDF weight of Zombie/Food/Treasure vs Saproling/Clue and check whether
  the common-subtype contribution is already near-zero. This is the
  single highest-information pre-flight — it may pre-answer R0's flood leg.
- **Effect-is-a-payoff filter:** how to distinguish a death-subtype
  trigger that *rewards* death (Slimefoot damage, Blood Artist drain)
  from one that merely *references* the subtype (a tuck/exile) — the same
  payoff-vs-mention distinction the density rules already handle.
- **Non-creature token subtypes (Clue/Food/Treasure/Blood):** confirm
  these belong in the aristocrats cut — "whenever a Clue is sacrificed"
  is aristocrats-shaped, but Treasure (supply 289) is the flood extreme.
