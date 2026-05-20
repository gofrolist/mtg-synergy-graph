# Prepared / AlternateMode:Prepare mechanic — requirements

**Date**: 2026-05-19
**Trigger**: Forge refresh `f42b9abc1` introduced ~48 cards with a new
`Attributes$ Prepared` state + `AlternateMode:Prepare` alt-face mechanic
that the importer currently captures only as raw text in `card_ports.raw_line`
(via `event_class='AlterAttribute'`) without surfacing the attribute value
or the alt-face existence as queryable signal.

## What "Prepared" does (Forge corpus reading)

A creature has two faces. The front face is the creature. The back face is
a stored spell. When the creature becomes **prepared**, you may cast a copy
of the back-face spell at no extra cost. Casting the copy unprepares the
creature.

Two roles in the ecosystem:

1. **Prepared-payoff cards** — creatures with `AlternateMode:Prepare`.
   The back face is the payoff (a P+1/+1 spell, removal, Reanimate,
   Brainstorm, Demonic Tutor, …). Each card has a built-in
   "self-prepare" mechanism — a trigger / ETB replacement / activated
   ability / conditional upkeep — that prepares **Self** on some condition.
2. **Prepare-enablers** — cards (currently only **Skycoach Waypoint**, a
   land) that prepare **other creatures** via
   `A: AB$ AlterAttribute | ValidTgts$ Creature | Attributes$ Prepared`.

The mechanic is gated by reciprocity: "Only creatures with prepare spells
can become prepared." So an enabler is useful **only** when the deck
contains payoff cards. This is a tribal-style synergy: Prepared cards
co-deck.

### Self-prepare trigger shapes observed in 6-card sample

| Card | Mechanism |
|---|---|
| Abigale, Poet Laureate | `T: SpellCast (Creature)` → `DB AlterAttribute Prepared on Self` |
| Adventurous Eater | `K: ETBReplacement:Other:DBPrepare` → ETB prepared |
| Harmonized Trio | `A: AB AlterAttribute Cost$ T tapXType<2/Creature>` (activated; tap 2 other creatures) |
| Grave Researcher | `T: Upkeep` → Surveil → conditional `DBPrepare if Creature.YouOwn GE 3 in Graveyard` |
| Emeritus of Woe | `R: Event$ Moved Destination$ Battlefield ReplaceWith$ TrigPrepare` (replacement ETB-prepared) + `T: End of Turn` conditional (2+ creatures died) |
| Skycoach Waypoint | `A: AB AlterAttribute ValidTgts$ Creature` (land that prepares **other** creatures) |

### Alt-face structure

The back face is either:

- A **bespoke spell** with `A:SP$ <Effect>` (e.g., Heroic Stanza =
  `SP$ PutCounter | CounterType$ P1P1`).
- A **reference** via `CopyFaceFrom:<ExistingCardName>` (Brainstorm,
  Reanimate, Demonic Tutor are reused this way).

The Forge parser already merges back-face abilities into the front-face
card on import (`_ALTERNATE_MARKER` in `parser.py`), so functionally
Abigale's port set today *includes* a `SP$ PutCounter` effect port from
the merged Heroic Stanza face. This is convenient — the alt-face payoff
is already a port — but the importer has no way to know which ports
came from the alt face vs the front face.

## What's missing from the data model

1. **`Attributes$ Prepared` is not exploded into `port_attributes`.**
   The importer captures the value only inside `card_ports.raw_line`
   as JSON text. Cannot be queried efficiently. Same gap applies to
   `Attributes$ Suspected` (existing mechanic, ~48 cards) and any future
   `AlterAttribute Attributes$` extension.
2. **`AlternateMode:Prepare` is not captured at all.** The top-level
   `AlternateMode:` header is silently dropped by the parser (it's not in
   `_LINE_DISPATCH` or the ability prefixes). We have no way to flag
   "this card has a Prepare alt-face" without re-reading the source file.

## v1 design

### Data-layer

1. **`port_attributes` extension**: when an effect port has
   `event_class='AlterAttribute'` and the raw_line carries `Attributes$ <V>`,
   explode each comma-separated value into a `port_attributes` row with
   `attr_kind='attribute'`, `attr_value=<V>`. Covers `Prepared`, `Suspected`,
   and any future attributes uniformly. No new schema, no new attr_kind
   string lookup at scoring time — it joins through the same shape as
   `change_type`, `token_color`, etc.

2. **Synthetic `AlternateMode` port**: parser recognizes the `AlternateMode:`
   top-level header and stashes `card["alternate_mode"]=<Value>`. Ports
   extractor adds one synthetic `port_type='static'`,
   `event_class='AlternateMode'`, `granted_keyword=<Value>` port per card
   that has the header. This matches the existing keyword-port shape (one
   row per top-level marker) and keeps the queryable surface uniform.

### Complement rule (Python helper)

One rule, `prepared_mechanic`, in `complement_rules/prepared.py`:

> A `(commander, candidate)` pair is a Prepared-synergy match when:
> - The commander has an AlterAttribute port with `attr_kind='attribute',
>   attr_value='Prepared'` (covers self-preparers AND other-preparers), AND
> - The candidate has a static port `event_class='AlternateMode'`
>   with `granted_keyword='Prepare'`.

IDF-weighted like every other rule. No special weight knob. The Prepared
universe is small (~48 cards) so IDF will be high — each match is
high-value when it fires.

### Out of scope for v1

- Self-vs-other distinction at the gate level. A creature commander that
  only self-prepares still benefits from sharing the deck with other
  prepared-payoff creatures (the enabler-payoff reciprocity is at the deck
  level, not the per-pair level). The rule should match symmetrically;
  per-pair refinement can come in a future iteration with more data.
- Reanimator / Brainstorm / Demonic Tutor sub-synergies inherited from the
  `CopyFaceFrom` references. Those already feed existing rules through the
  merged-face port extraction; no new logic needed.
- Promoting the rule from Python-helper to declarative
  (`port_graph/rules_schema.py`). The interpreter gate grammar doesn't yet
  cover "candidate has port with attribute X" queries — adding a
  `has_port_attribute` predicate is a separate brainstorm/plan cycle.

## Acceptance

- `bench.py audit` verdict POSITIVE or NEUTRAL on 100-cmdr golden set
  (most golden commanders are pre-Prepared-era, so signal will be small
  but should not regress).
- `rule_quality_gate.py --rule prepared_mechanic` passes (no vacuum-fill).
- `scripts/recommend.py --commander "Abigale, Poet Laureate" --explain`
  shows `prepared_mechanic` firing on candidates that share the Prepare
  mechanic, and the top-30 surfaces other Prepared creatures with
  plausible mechanical fit (high cmc alt-mode > low cmc alt-mode).
- Brand-new commanders that EDHREC has no data for (Abigale, Augusta,
  Emeritus of Woe, …) get a non-trivial scoring profile rather than
  collapsing to the generic creature baseline.

## Open question deferred to follow-up

Whether `CopyFaceFrom:<Name>` resolution should be done at import time
(materialize the back-face spell into the merged port set) or at scoring
time (look up the referenced card's ports on demand). Today it's
implicit — Forge's runtime resolves the reference but the importer does
not, so a `CopyFaceFrom:Reanimate` card carries no Reanimate ports. Most
golden-set rules will fail to fire on these. Out of scope for v1; needs
its own brainstorm.
