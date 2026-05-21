# `K:ETBReplacement` SVar walking — requirements

**Date**: 2026-05-20
**Trigger**: PR #47 follow-up #3, originally framed as "covers 20 cards
as enablers" for the Prepared mechanic. Investigation showed the actual
corpus has **400** cards using `K:ETBReplacement:Scope:SVarRef`, of
which `DBPrepare` is only 22. The keyword form is structurally similar
to a regular `R:` replacement but lives entirely on a single `K:` line
whose SVar reference is never walked. Today every one of these 400
cards surfaces only a thin keyword port (`event_class='ETBReplacement'`)
with the full replacement effect invisible to the synergy graph.

## What `K:ETBReplacement` does in Forge

Forge encodes a class of "this permanent enters with a baked-in
replacement effect" via a single K: keyword line of the form:

```
K:ETBReplacement:<Scope>:<SVarRef>[:Optional[:Zone[:ValidFilter]]]
```

Examples from the corpus:

| Card | K: line |
|---|---|
| Grave Researcher | `K:ETBReplacement:Other:DBPrepare` |
| Hardened Scales | `K:ETBReplacement:Other:AddExtraCounter:Mandatory::Card.Self+escaped` |
| Vorel of the Hull Clade | `K:ETBReplacement:Other:DBPutCounter:Mandatory::Card.Self+kicked` |
| Cavern of Souls | `K:ETBReplacement:Other:DBChooseCreatureType` |
| Cathars' Crusade-shape | `K:ETBReplacement:Other:AddExtraCounter:Mandatory:Battlefield:Creature.Other+YouCtrl` |
| Reflections of Kiki-Jiki | `K:ETBReplacement:Copy:DBCopy:Optional` |

The `<SVarRef>` (`DBPrepare`, `AddExtraCounter`, `DBCopy`, etc.) names
an SVar on the same card whose value is a normal Forge effect line
(`DB$ AlterAttribute | Attributes$ Prepared`, `DB$ PutCounter`, etc.).
The runtime semantics are identical to a regular `R:Event$ Moved …
ReplaceWith$ <SVarRef>` replacement, but the K: form is more compact
for "this card has one ETB replacement" cards.

## Universe and distribution

400 cards total. Scope: `Other` 333, `Copy` 67. Top SVar refs:

| SVar ref | Cards | Effect class |
|---|---:|---|
| ChooseColor | 73 | Mana-fixing on ETB (Vivid lands, etc.) |
| ChooseCT | 63 | Tribal name selection (Cavern of Souls) |
| DBCopy | 62 | Copy effects (Reflections, clones) |
| AddExtraCounter | 28 | Counter doublers (Hardened Scales family) |
| DBPrepare | 22 | Prepared mechanic (PR #47) |
| ChooseP | 20 | Player selection (curse effects) |
| DBNameCard | 16 | Card-name selection (Iona, Shield of Emeria) |
| SiegeChoice | 14 | Siege side selection |
| (other) | ~100 | Long tail |

The counter doublers alone are some of the highest-signal cards in EDH
(Hardened Scales, Doubling Season, Branching Evolution, …). The
ChooseCT cards include Cavern of Souls — universally relevant to
tribal commanders. The DBCopy family includes clone-shape cards that
should match copy-archetype commanders.

## What's missing today

`extract_keyword_ports` (`src/mtg_synergy_graph/ports.py:664`) emits
one port per K: line:

```python
{"port_type": "keyword", "event_class": "ETBReplacement", "granted_keyword": "ETBReplacement", "raw_line": "K:ETBReplacement:Other:AddExtraCounter:…"}
```

The full line is preserved in `raw_line` but the SVar chain is never
walked. Two consequences:

1. **No effect port for the replacement payload.** Hardened Scales has
   no `effect|AddExtraCounter` port — counter-doubler rules can't see
   it. Cavern of Souls has no `effect|ChooseCreatureType` port —
   tribal rules can't see it. Grave Researcher has no `effect|
   AlterAttribute` port with `attr_value='Prepared'` — the Prepared
   slow-path is invisible to it (covered by the cheap path today).

2. **No `port_attributes` projection.** The `attr_kind='attribute'`
   row that PR #47 added for AlterAttribute Prepared never fires on
   these 22 cards because the AlterAttribute port itself doesn't
   exist.

For comparison: `R:Event$ ChangesZone | … | ReplaceWith$ FooSVar`
(regular replacement) ALSO doesn't walk the SVar today — see
`_replacement_ext` at `ports.py:799`: `del svars  # replacement
ports don't follow SVar chains`. So the gap is broader than just K:
keywords. But the K:ETBReplacement form is contained, well-shaped,
and worth fixing as the first step.

## Design questions

### Q1. One port per K: line, or port-per-resolved-effect?

| Option | Behaviour |
|---|---|
| **Keep the keyword port + add resolved effect ports** | `port_type='keyword', event_class='ETBReplacement'` stays (back-compat: anything querying `granted_keyword='ETBReplacement'` keeps working) AND the resolved chain emits one effect port per ChainNode. |
| **Replace the keyword port with the resolved effects** | Drop the surface-level keyword port. Anything relying on `event_class='ETBReplacement'` would break. |

**Recommendation: keep both.** The keyword port is essentially free
(one row per card), and removing it risks breaking rules / queries we
haven't audited. The new resolved-effect ports are additive.

### Q2. How to tag inherited (SVar-walked) ports?

Existing branch kinds (`CHAIN_KEYS` in parser.py): execute, subability,
true, false, win, otherwise, repeat, change_zone_table. Plus
`replacement_condition` for R: condition gates.

The K:ETBReplacement-walked ports semantically resemble:
- A replacement effect (they fire on the carrier's own ETB).
- A keyword-derived chain (similar to scaling / suspend keywords).

**Recommendation**: introduce a new branch_kind `etb_replacement`.
Distinct from `replacement` (used by R: lines) so we can target either
in queries. Update `parser_branch_kinds()` and the `BRANCH_MULTIPLIER`
test invariant accordingly. Walk depth handled by the existing SVar
walker (which already uses `chain_depth`).

### Q3. Optional vs Mandatory K: lines

Some K:ETBReplacement carry `:Optional` (the player may choose to
apply the replacement) vs `:Mandatory` (forced). Today's regular R:
replacement layer uses `is_optional` on ability rows but not on
replacement ports.

**Recommendation**: mirror the existing R: handling — record the
optional flag on the *first* resolved port via the existing
`is_optional` column. Downstream rules can gate on it if they want
to discount optional replacements. v1 ships without a consumer; the
column is data infrastructure.

### Q4. `Other` vs `Copy` scope semantics

- `Other` (333 cards): replaces the carrier's ETB event with a sub-ability chain. The chain runs INSTEAD of the normal ETB.
- `Copy` (67 cards): the replacement is a copy effect on creatures entering — the carrier creates copies of those creatures.

The semantics differ enough that consumers may want to distinguish them.

**Recommendation**: record the scope on a new column-free shim —
either via `port_attributes` (`attr_kind='etb_scope'`,
`attr_value='other'|'copy'`) or via the existing `affected_scope`
column (which carries "Creature.YouCtrl"-style values today). The
latter is overloaded; the former is cleaner. Use
`port_attributes.attr_kind='etb_scope'` matching the
`'via_copyfacefrom'` provenance pattern from PR #50.

### Q5. SVar conditional gates (ConditionPresent$, CheckSVar$)

The walked SVar may carry a `ConditionPresent$` gate (e.g.,
`DB$ AlterAttribute | Attributes$ Prepared | ConditionPresent$
Creature.YouOwn | ConditionCompare$ GE3 | ConditionZone$ Graveyard`
for Grave Researcher). Today's existing trigger walker propagates
these via `is_conditional` + `branch_kind='execute'`.

**Recommendation**: same propagation — the existing
`_chain_has_condition` helper at `ports.py:501` covers the
detection; reuse it.

## v1 design

### Parser

1. **`_parse_card_block`**: keep K: lines flowing through the existing
   keyword channel (no change). The keyword stays in `card["keywords"]`
   so back-compat consumers (and `extract_keyword_ports`) keep working.
2. **`extract_etb_replacement_directives(keywords)`**: new helper that
   parses each K: line starting with `ETBReplacement:`, returning a
   list of `(scope, svar_ref, optional, zone, valid_filter)` tuples.
   Defensive on malformed lines — skip + log warning, never crash.

### Ports

1. **`extract_etb_replacement_ports(card_name, directives, svars)`**:
   new extractor. For each directive:
   - Walk `svar_ref` via existing `walk_svar_chain` with
     `branch_kind='etb_replacement'`, `chain_depth=0`.
   - For each ChainNode, build a port row with:
     - `port_type='effect'`
     - `event_class` from the chain's `_verb` (AlterAttribute,
       AddExtraCounter, ChooseCreatureType, etc.)
     - `valid_filter`, `zone_origin`/`zone_destination`, `counter_type`
       from the parsed payload
     - `branch_kind='etb_replacement'` on the root; sub-ability
       children use existing CHAIN_KEYS mapping
     - `source_svar=<svar_ref>`
     - `is_conditional` if `ConditionPresent$`/`CheckSVar$` present
     - `is_optional` if K: line carries `:Optional`
     - Same `_attributes` transient key for `Attributes$ X` →
       `attr_kind='attribute'` projection PR #47 introduced.
   - Tag each port with `port_attributes.attr_kind='etb_scope'`,
     `attr_value='other'|'copy'`.
2. Wire into `extract_all_ports` after `extract_keyword_ports` so the
   keyword port and resolved effect ports coexist.

### Parser-invariant tests

- Update `parser_branch_kinds()` to include `'etb_replacement'`.
- Update `BRANCH_MULTIPLIER` test (in scoring tests) to cover the new
  branch_kind. Without this the BRANCH_MULTIPLIER coverage assertion
  fails immediately.

### No complement-rule changes

Lean on the same principle as PR #50: existing rules pick up the new
effect ports via the normal `port_match` path. Counter-doubler rules
will see Hardened Scales' `effect|AddExtraCounter` port naturally;
tribal-archetype rules will see Cavern of Souls'
`effect|ChooseCreatureType` naturally; prepared_mechanic's slow path
will see Grave Researcher's `effect|AlterAttribute` +
`port_attributes.attr_value='Prepared'` naturally (no behavioral
change on prepared_mechanic, since the cheap path already covered
these 22 cards).

## Audit risk

This is the biggest unknown. Adding ~400 cards' worth of new effect
ports — including high-IDF cards like Hardened Scales, Doubling
Season, Cavern of Souls, Iona — will materially shift the per-rule
candidate frequency (IDF) for several event classes. Expected
movement:

- **Counter commanders** (Marchesa, Vorel, Atraxa-shape, Hardened
  Scales-likes): big positive on counter-doubler rules.
- **Tribal commanders** (any tribal): Cavern of Souls finally matches
  tribal port shapes; may displace other staples.
- **Clone commanders** (Riku, Sakashima, Volo): DBCopy ports surface
  many clone-shape cards.
- **Prepared commanders** (Abigale-shape): no change (cheap path
  already covers; slow path is just data infrastructure).
- **Risk**: IDF dilution for very common event classes. If 28 cards
  now emit `effect|AddExtraCounter`, the IDF weight per match drops
  (1/log2(1+28) ≈ 0.21 vs current ≈ 0.32 if only ~9 cards have it).
  Existing counter-doubler firings get weaker, possibly offsetting
  the new firings.

Net direction is hard to predict; audit will tell us.

**Mitigation if audit goes negative**: add `etb_scope` filtering to
the candidate-selection SQL in counter-doubler / clone / ChooseCT
rules so the new ports are only matched when the rule's gate
predicate accepts them. Alternative: weight discount via
`_RULE_QUALITY_MULTIPLIER` if specific rules overshoot.

## Acceptance criteria

- `uv run pytest tests/` green (new tests for parser + port extractor
  + SVar-chain conditional propagation + back-compat: keyword port
  still emitted).
- `bench.py audit` verdict **POSITIVE or NEUTRAL** on the 100-cmdr
  golden set with **0 `hi_syn_loss`**.
  - Most likely outcome: small POSITIVE driven by counter/tribal
    commanders gaining canonical staples.
  - If NEGATIVE with hi_syn_loss > 0, narrow scope before merging:
    e.g., land only the `DBPrepare` subset first (matches the
    original PR #47 framing) and ship the rest in a separate audit
    cycle.
- `recommend.py --commander "Hardened-Scales-likes (Marchesa, Vorel,
  Atraxa-shape)"` shows Hardened Scales, Doubling Season, Branching
  Evolution surfacing higher than today.
- `recommend.py --commander "Krenko, Mob Boss"` shows Cavern of Souls
  surfacing (Goblin tribal).
- All 22 DBPrepare cards now have an `effect|AlterAttribute` +
  `port_attributes.attr_value='Prepared'` port (verifies the
  AlternateMode-cheap-path fallback now has a slow-path equivalent).

## Out of scope for v1

- **Regular `R:` lines walking SVars**. The parallel gap at
  `ports.py:799` (`del svars # replacement ports don't follow SVar
  chains`) is similar but not addressed here. Defer to a follow-up
  if/when ETBReplacement walking shows positive results.
- **Per-rule etb_scope gating**. v1 ships data; consumers (counter /
  tribal / clone rules) keep their existing gates and pick up the
  new ports as-is. If the audit shows overshoot, gate refinement is
  the second-pass remediation.
- **`--explain` annotation**: "Hardened Scales (via ETBReplacement
  → AddExtraCounter)". Nice-to-have; defer.
- **Forge Oracle rebuild gate change**: the oracle PPMI tables were
  built against the current port set; adding 400 cards' ports will
  shift PPMI. Re-build `data/forge_oracle.db` after this lands
  (existing tooling: `scripts/forge_oracle.py build`).

## Open questions deferred to follow-up

- Should the new `'etb_replacement'` branch_kind have its own
  multiplier in `BRANCH_MULTIPLIER`? Today's branch multipliers
  discount conditional branches; ETB-replacement effects are
  unconditional (they always fire on ETB) so default 1.0× seems
  right. v1 ships at 1.0×; later tune if audit calls for it.
- Should the keyword-level port (`event_class='ETBReplacement'`)
  carry the SVar ref in `granted_keyword` so old rules can opt into
  filtering by the resolved-effect class? Marginal — no current rule
  reads it.
