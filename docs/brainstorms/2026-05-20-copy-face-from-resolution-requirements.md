# `CopyFaceFrom:<Name>` resolution — requirements

**Date**: 2026-05-20
**Trigger**: PR #47 (`prepared_mechanic` landing) deferred this explicitly
as "biggest payoff, needs its own brainstorm". The 2026-05-19 Prepared
brainstorm flagged it as an open question at the bottom of the doc
(`docs/brainstorms/2026-05-19-prepared-mechanic-requirements.md` §"Open
question deferred to follow-up").

## What `CopyFaceFrom:<Name>` does (Forge corpus reading)

A Prepared card has a front face (creature) and a back face (stored
spell). Forge's `.txt` files encode the back face in one of two flavors:

1. **Inline bespoke spell** — `A:SP$ <Effect>` lines on the back-face
   block (e.g., Abigale, Poet Laureate's back face is "Heroic Stanza"
   defined inline as `A:SP$ PutCounter | CounterType$ P1P1 | ...`).
2. **`CopyFaceFrom:<ReferencedCardName>`** — a single line on the
   back-face block that means "this back face IS that other card's
   spell content". Used for Prepared cards whose back face reuses an
   existing, well-known spell (Reanimate, Brainstorm, Demonic Tutor,
   Swords to Plowshares, Wheel of Fortune, Lightning Bolt, …).

The 47 Prepared cards split **25 inline / 22 CopyFaceFrom**. The 25
inline ones already work today — `parser.py::_parse_card_block` merges
back-face abilities into the front-face `card["abilities"]` list, so
their back-face ports flow into the importer normally. The 22
CopyFaceFrom cards do not — the directive is silently dropped because
it doesn't match any prefix in `_LINE_DISPATCH` or the ability prefixes.

### Empirical gap

```
$ sqlite3 data/synergy.db "SELECT ... FROM card_ports WHERE card_name='Grave Researcher'"
trigger|Phase|||                # T: upkeep
effect|Surveil|||               # Surveil 1
effect|AlterAttribute|||        # → Prepared (conditional)
static|AlternateMode|||         # AlternateMode:Prepare marker

$ sqlite3 data/synergy.db "SELECT ... FROM card_ports WHERE card_name='Reanimate'"
effect|ChangeZone|Creature|Graveyard|Battlefield   # ← missing on Grave Researcher
effect|LoseLife|You||
effect|Cleanup|||
```

Grave Researcher carries no `ChangeZone Graveyard → Battlefield` port,
so the reanimator/graveyard-retrieval rules cannot fire on it. A
Karador or Meren commander looking at the WB Prepared candidates sees
Grave Researcher as just a 3/3 with surveil — not "a 3/3 that
conditionally reanimates a creature from your graveyard."

Multiply this by 22 cards: the synergy graph is missing 22 Prepared
cards' worth of high-signal spell ports (Reanimate, Brainstorm,
Demonic Tutor, Swords to Plowshares, Wheel of Fortune, Lightning Bolt,
Regrowth, Replenish, Channel, …). Each is one of the highest-IDF
spells in EDH.

### The 22 referenced cards (full list)

```
Ancestral Recall, Bind, Braingeyser, Brainstorm, Careful Study, Channel,
Demonic Tutor, Exsanguinate, Fire, Jump, Liberate, Lightning Bolt,
Raise Dead, Rampant Growth, Reanimate, Regrowth, Replenish,
Secret Rendezvous, Seething Song, Sign in Blood, Start, Stream of Life,
Swords to Plowshares, Wheel of Fortune
```

(Some appear twice — e.g., `Start // Fire` is a split card whose halves
are individually referenced; counts above merge them. There are 22
distinct `CopyFaceFrom` source files, ~24 distinct targets.)

Every one of these is a tier-1 EDH card with a strong, well-known
mechanical shape. The closed-set, high-quality nature of the target
list is what makes this follow-up high-payoff — we already extract
these spells' ports correctly when they appear in `cards`, we just
need to inherit them onto the 22 Prepared carriers.

## Design questions

### Q1. Where to resolve — import time or scoring time?

| Option | Pros | Cons |
|---|---|---|
| **Import-time** (materialize inherited ports into `card_ports`) | Zero query-time cost; all existing rules see the inherited ports for free; `--explain` output naturally describes the synergy via the existing rule's vocabulary | Importer needs a two-pass design (resolve names after all cards are imported); 22 extra `card_ports` rows per Prepared card (~250-500 new rows total — trivial); inherited ports must be marked with provenance for auditability |
| **Scoring-time** (look up the reference on demand) | No DB schema change; the back-face spell stays separable from the carrier card | Every rule has to learn the indirection, OR we add a join in `find_all_complements`; query cost grows with carrier count; `--explain` output becomes harder to format |

**Recommendation: import-time.** Lower coupling, no rule changes,
matches the existing convention (Abigale's inline back face already
materializes — CopyFaceFrom should be functionally equivalent).

### Q2. How to tag inherited ports (provenance)

Inherited ports must be distinguishable from native ports so we can:
- Audit which firings came from inherited vs native (debugging false
  positives later).
- Toggle the feature off (kill switch) without re-importing.
- Apply a different weight later if the inherited-port firings prove
  too generous (the carrier card requires casting + self-prepare +
  cast-the-copy, not just casting, so it's mechanically lossier than
  the raw spell).

**Recommendation**: extend `port_attributes` with
`attr_kind='via_copyfacefrom'`, `attr_value='<ReferencedCardName>'`,
one row per inherited `card_ports` row. Uses the existing pattern
(same shape as the `attr_kind='attribute'` rows added for Prepared).
No new schema migration.

### Q3. Conditionality — should inherited ports gate on Prepared state?

A creature with `CopyFaceFrom:Reanimate` only actually casts Reanimate
**after** it self-prepares — which requires a separate setup step
(surveil to 3+ creatures in graveyard for Grave Researcher; cast a
creature spell for Abigale; etc.). The pure mechanical truth is "this
card eventually casts Reanimate, conditional on its self-prepare
trigger firing."

| Option | Behaviour |
|---|---|
| **Ungated (always-on)** | Inherited ports score like native ports. Korvold sees Grave Researcher as Reanimate-like even without Prepared infrastructure. |
| **Gated by Prepared ecosystem** | Inherited ports only score when the commander also triggers `prepared_mechanic`. Collapses to the existing Prepared-tribal signal — no incremental information. |
| **Always-on but at reduced weight** | Treat as "discounted" Reanimate (e.g., 0.5× IDF). Compromises between truth and noise. |

**Recommendation: ungated (always-on), full IDF weight in v1.** Mechanical
truth is: the card carries the spell, and any commander wanting that
spell's effect should mechanically prefer this card over a vanilla
3/3 (an EDH brewer adding Grave Researcher to Karador absolutely
*does* benefit from the Reanimate, even without other Prepared cards).
The audit will tell us whether the always-on weight overshoots; if it
does, the provenance tag from Q2 lets us add a `via_copyfacefrom`
discount in a follow-up.

### Q4. Missing-target handling

The referenced card may not be in the importer's universe (e.g.,
power-9 not in the legal set; CopyFaceFrom name typo). Three options:

| Option | Behaviour |
|---|---|
| **Hard error** | Fail import. Catches typos. Risk: any Forge data refresh introducing an unknown reference blocks the import. |
| **Warn + skip** | Log `unresolved CopyFaceFrom:<Name> on <CarrierName>`; inherit zero ports. Forward-compatible with future Forge cards. |
| **Silent skip** | Hide the warning. Risk: regressions go unnoticed. |

**Recommendation: warn + skip with an audit counter.** Add a row to
`.audit/copyfacefrom_unresolved.csv` (gitignored) per unresolved
reference, plus a stderr warning. Forge data refreshes can introduce
new referenced cards before the carrier cards' targets are imported
in the same pass — the warn-and-skip path keeps imports green while
surfacing the gap.

### Q5. Self-reference cycles

A card `A` with `CopyFaceFrom:B` where `B` also has `CopyFaceFrom:A`
would loop. None exist today (22 cards reference 22 distinct vanilla
spells, no Prepared-card self-references), and the use case is
unlikely — but the resolver should guard against it defensively (a
seen-set in the recursion) rather than hang.

## v1 design

### Data layer

1. **Parser change** (`parser.py`): add `CopyFaceFrom:` to the
   back-face dispatch. On the back-face block, capture `CopyFaceFrom:`
   into `card["copy_face_from"]=<ReferencedCardName>`. Front-face block
   ignores it (defensive — it should never appear on the front face,
   but the parser shouldn't crash if it does).

2. **Importer change** (`importer.py`): two-pass.
   - **Pass 1** (existing): import every card's front-face + inline
     back-face ports into `card_ports` / `port_attributes` exactly as
     today.
   - **Pass 2** (new): for each card with a non-NULL `copy_face_from`
     field, look up the referenced card by name. Copy every
     `card_ports` row whose carrier matches the reference into the
     carrier card, **except** ports with `port_type='static'` and
     `event_class='AlternateMode'` (defensive — never inherit a
     Prepared marker via CopyFaceFrom). For each copied row, also
     insert a `port_attributes` row with
     `attr_kind='via_copyfacefrom'`, `attr_value=<ReferencedCardName>`.
   - Log unresolved references; emit a summary line:
     `CopyFaceFrom resolution: 22 carriers, 22/22 resolved (66 ports
     inherited)`.

3. **No new `card_ports` columns.** The provenance lives in
   `port_attributes` — same pattern as `attr_kind='attribute'` from
   PR #47.

### Scoring layer

**Nothing changes in `complement_rules/` for v1.** Inherited ports look
identical to native ports at scoring time, so every existing rule
(gy_loader, gy_retrieval, brainstorm-style draw, tutor-shape, etc.)
automatically picks them up. This is the design goal.

### Auditability

- `bench.py audit --inspect <RULE_ID>` already exists; inherited-port
  firings will surface in the per-rule contribution rows.
- New flag (optional, v1+): `bench.py audit --via-copyfacefrom` —
  filter contribution rows to those whose port has an associated
  `via_copyfacefrom` attribute. Punts to a follow-up if not trivial.

## Acceptance criteria

- `uv run pytest tests/` green (new tests for parser + importer paths).
- `bench.py audit` POSITIVE or NEUTRAL on the 100-cmdr golden set.
  - **Expected**: small POSITIVE — Korvold, Meren, Sidisi, Karador,
    The Mimeoplasm, Phenax (graveyard archetypes) should plausibly
    gain Grave Researcher / Cheerful Osteomancer / similar as
    inherited-Reanimate candidates. Other archetypes (Brainstorm
    carriers for Rashmi / Zur; Demonic Tutor for Yawgmoth-shape;
    Wheel of Fortune for Nekusar) likewise.
  - **Risk**: NEGATIVE if the inherited ports cause Prepared cards
    to dominate generic-good-spell archetypes (a Reanimate-bearing
    3/3 outranking actual Reanimate). The provenance tag (Q2) gives
    us a knob to discount via `_RULE_QUALITY_MULTIPLIER` or a
    pathway prefilter without reverting the importer.
- `recommend.py --commander "Karador, Ghost Chieftain" --explain` shows
  the inherited Reanimate-shape ports firing on Grave Researcher,
  Cheerful Osteomancer, and Bloodline Recollector (the three
  `CopyFaceFrom:Reanimate`/`Raise Dead`-ish WB Prepared cards).
- `recommend.py --commander "Abigale, Poet Laureate" --explain` shows
  the same Prepared-tribal ordering as today (no regression on the
  follow-up #1 wins from PR #48). The inherited ports should be
  additive on Karador/Meren-shape commanders without disturbing the
  Prepared-tribal ordering.
- Unresolved references logged but non-fatal; importer exits clean.
- `card_ports` row growth ~50-100 rows (22 carriers × ~3 ports each).

## Out of scope for v1

- **Scoring-layer changes** — no new rule, no new gate, no weight
  override. Lean on existing rules picking up the inherited ports.
- **Provenance-aware weighting** — the `via_copyfacefrom` tag is
  recorded but no rule consumes it in v1. Reserved as a follow-up
  knob.
- **Cycle detection beyond a depth-1 seen-set** — depth-2 chains
  (A→B→C) and beyond not supported. None exist in Forge today.
- **CopyFaceFrom on non-Prepared cards** — the directive may
  hypothetically appear on a non-Prepared card in a future Forge
  refresh. v1 resolves it everywhere it appears, but the audit
  surface focuses on the 22 known Prepared carriers.
- **Re-firing audits for previously-pinned commanders** — the audit
  will tell us if the golden set shifts; re-pin via `--repin --yes`
  as usual.

## Open questions deferred to follow-up

- Should we eventually surface inherited ports in `--explain` output
  as "Grave Researcher (via Reanimate)" so users know why a card
  scored? Nice-to-have, not a v1 blocker.
- If the always-on weight overshoots, what's the right discount —
  flat 0.5× via a `via_copyfacefrom_discount` knob, or rule-by-rule
  via gate predicates? Defer until audit data tells us whether a
  discount is needed at all.
- Should `K:ETBReplacement` SVar walking (the third PR #47 follow-up)
  land before or after this one? They're independent. ETB-replacement
  walking covers 20 Prepared cards that don't surface an AlterAttribute
  port on the **commander side** (matters only for enabler detection);
  CopyFaceFrom resolution covers 22 Prepared cards' **candidate-side**
  back-face spells. No interaction.
