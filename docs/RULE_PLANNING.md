# Rule planning workflow

Schema-driven workflow for adding complement rules. Replaces the
ad-hoc loop ("find a broken commander, write a rule, repeat") with a
coverage-first pipeline that treats Forge ports as a finite,
enumerable vocabulary.

## Why schema-driven

EDHREC tells us *which cards* a commander wants. Forge tells us *what
the commander mechanically does*. The engine should be able to write
a useful pile for any commander whose ports we understand — including
brand-new printings EDHREC has never seen. EDHREC remains the
validation oracle, not the design oracle.

## Tooling

| Script | Output | Purpose |
|---|---|---|
| `scripts/gap_report.py` | `docs/gap_report.md` | **Primary entry point.** Sub-cell coverage + template matching. Outputs a ranked markdown queue of concrete rule proposals. The next rule to write is the top entry — no human prioritization required. |
| `scripts/port_universe.py` | `docs/port_universe.json` | Enumerate every distinct (port_type, event_class) cell in `card_ports` with commander reach, top valid_filter qualifiers, and top raw_line clause keys. Re-run after every importer change. |
| `scripts/coverage_matrix.py` | `docs/coverage_matrix.json` | Cell-level coverage (coarser than `gap_report.py`). Useful for sanity checks and historical diffs. |
| `scripts/golden_set_track.py` | stdout | Regression check on the 100-commander golden set NDCG@30 against `tests/fixtures/golden_set_run.json`. Run before/after every rule change. |
| `scripts/compare_edhrec.py` | stdout | Hi-Syn / Top / OnPage breakdown for any commander or commander list — used for *validation*, not planning. |

## The pipeline

### 1. Run the auditor

```bash
uv run python scripts/gap_report.py
```

Generates `docs/gap_report.md` with a ranked queue of sub-cell gaps,
each annotated with:
- Reach (commanders carrying the signature)
- Activation rate (fraction with any rule activation today)
- Impact (`commanders × (1 - activation_rate)`)
- Best-fit template from the catalog (`peer_tribal`,
  `damage_amp_doubler`, `damage_prevention_voltron`,
  `replacement_stack`, `axis_feeder`)
- Gate sketch + tier sketches + estimated pool sizes
- Exemplar commanders with no rule activation

### 2. Pick the top entry — no judgment required

The next rule to write is the entry at position #1. Skip it only if
the template label says `[IMPLEMENTED]` (auditor's safety net so we
don't redo a finished rule).

To refresh the underlying universe / coverage artifacts:

```bash
uv run python scripts/port_universe.py
uv run python scripts/coverage_matrix.py
```

Both produce JSON artifacts under `docs/`.

### 3. Plan the rule before coding

The auditor's proposal is the seed. Expand it into a short note covering:

- **Cell signature**: `(port_type, event_class)` plus any qualifier
  axis being extracted (e.g. `valid_filter ~ '%modified%'`).
- **Mechanical hypothesis**: why these commanders need this rule —
  what player-facing pattern is it modelling? A one-paragraph
  description that doesn't reference any specific commander.
- **Tier structure**: which candidate-side ports we'll match, ranked
  by specificity. Each tier should map cleanly to a SQL query.
- **Self / scope / description rejection list**: which qualifier
  positions or clause keys must be skipped to avoid false positives
  (the `TargetsValid` / `Card.Self+modified` / `TriggerDescription`
  family).
- **Expected reach**: count of commanders the gate should fire for,
  estimated from the catalog. Cross-check after implementation.
- **Validation plan**: which existing commanders should improve
  (Kodama-style), which must not regress (Pearl-Ear-style), and
  what the multiplier baseline should be.

### 4. TDD the rule

Test file: `tests/test_<area>_rules.py`, one new `TestFind…` class
per rule. Required tests:

- Gate rejection: empty / unrelated commander → `[]`.
- Qualifier rejection: Self-anchored / TargetsValid / description
  clause → `[]`.
- Per-tier match: minimal candidate row per tier with the expected
  `cand_event` value.
- Per-tier exclusion: candidate that almost matches but should fall
  through (e.g. self-sac-only producers).
- Dedup: card matching multiple tiers gets exactly one complement,
  highest priority wins.
- Commander self-exclusion.
- `rule_id` value is exactly the new ID.

Each test uses the in-memory schema fixture in
`tests/test_utility_rules.py` (`_make_db()`).

### 5. Wire and weight

- Add helper to `complement_rules/<file>.py`.
- Import + dispatch in `complement_rules/core.py`
  (`_card_attr_complements` or the formal rules tuple).
- Add `_RULE_TO_BUCKET` entry in `universal_scorer.py`.
- Add `_RULE_QUALITY_MULTIPLIER` entry with a comment justifying the
  multiplier in terms of pool size and tier specificity.

### 6. Validate

```bash
uv run pytest tests/                                           # 850+ tests
uv run python scripts/golden_set_track.py \
    --baseline tests/fixtures/golden_set_run.json              # NDCG must not regress
uv run python scripts/coverage_matrix.py                       # cell should now show formal coverage / activation lift
```

For non-golden impact, sample 200–500 commanders carrying the cell's
shape and report aggregate Hi-Syn / OnPage delta. Don't refactor the
gate to chase a single commander's curated picks — EDHREC is a
sanity check, not the target.

### 7. Commit

One commit per rule:

```
feat: <rule_id> for <mechanic>-axis commanders

<one-paragraph what + why, no commander names in the header>

Impact:
- <commander A>: hi_syn X→Y, on_page A→B
- ...
- Golden set NDCG unchanged at <value>

<test count> new tests; <total> total tests, <coverage>% coverage.
```

## Anti-patterns

- **Per-commander hardcoding.** If the gate mentions a card name or
  oracle text fragment that isn't a Forge port shape, it's wrong.
  Extract the underlying mechanical signature instead.
- **Chasing EDHREC overlap as the goal.** Hi-Syn for niche
  commanders is curated by tens of decks and isn't a reliable
  ranking signal. Use it to validate that a *mechanical* hypothesis
  surfaces correct cards, not as the optimisation target.
- **Rule sprawl from the same axis.** If two new rules differ only
  in which tier they emphasize, merge them. Each rule should have a
  distinct mechanical hypothesis.
- **Skipping the regression baseline.** The golden set takes ~5 s to
  re-score. Never commit a rule without confirming NDCG.

## Known limitations of the tooling

- `coverage_matrix.py` and `gap_report.py` both attribute activation
  at the commander level — if any rule fires for a commander, every
  port shape they carry is marked covered. A commander with 8 ports
  gets every shape marked even if only one port drove the
  activation. Treat low activation rates as definitive ("uncovered")
  and high rates as indicative ("probably covered, verify per-
  commander"). Fixing this requires tagging `PortComplement` with
  the originating commander port_id — a small but cross-cutting
  refactor.
- `port_universe.json` enumerates qualifier *tokens* but not their
  combinatorial overlap (e.g. `attacking+modified`). For nuanced
  axes look at the raw `valid_filter` distribution in
  `card_ports`.
- `gap_report.py` template catalog is small (5 templates today). A
  proposal labelled `replacement_stack` or `axis_feeder` is the
  auditor's best fit, not a guaranteed match — investigate the
  actual mechanic before implementing. Templates without a
  reasonable fit fall through (`_propose` returns None) and the
  gap is omitted from the report rather than surfacing a misleading
  proposal.
