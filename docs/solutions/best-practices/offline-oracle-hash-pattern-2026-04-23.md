---
module: forge_oracle
title: Offline oracle sidecar with refuse-to-run hash enforcement
tags: [offline-invariant, sidecar-db, config-hash, rule-authoring, forge, ppmi, plan-002]
problem_type: best_practice
symptoms:
  - sidecar DB silently compared against drifted inputs
  - offline module imports creep into the inference path
  - sidecars built under one config consumed under another
resolution_type: pattern
applies_when:
  - A new offline-only subsystem feeds a design-time workflow (rule authoring, coverage analysis, audit sidecars).
  - Consumers span multiple CLIs that each need a different tolerance for staleness.
  - The subsystem is derived from external data (e.g., a vendored clone pinned to a commit SHA) and must not contaminate the main scoring / inference path.
created: 2026-04-23
plan_ref: docs/plans/2026-04-23-002-feat-forge-second-oracle-plan.md
---

# Offline oracle sidecar with refuse-to-run hash enforcement

When adding an offline subsystem that feeds design-time workflows (rule
authoring, audits, coverage analysis), the sidecar data it produces
sits in a separate SQLite DB that's derived from inputs that can drift
silently (source SHA, smoothing constants, minimum-evidence
thresholds, vocabulary version). Without mechanical enforcement a
consumer will eventually read stale signal and produce a report that
*looks* trustworthy.

This pattern was extracted from the Forge-Second-Oracle landing
(plan 002), itself a transfer of the inference-path pattern in
[flag-gated-multi-port-rule-pattern](flag-gated-multi-port-rule-pattern-2026-04-23.md).
The meta-principle — mechanically enforce the subsystem invariant via
a hash that refuses stale comparisons — applies on both sides, but the
offline side has a different consumer-strictness spectrum that this
doc captures.

## Context

You are adding an offline subsystem that:

- Reads derived data from a vendored / external source pinned to a
  commit SHA (in our case, the `data/forge/` partial clone).
- Applies a few configurable numeric knobs (smoothing `k`,
  minimum-evidence threshold, normalization percentile, etc.).
- Projects through a versioned vocabulary (in our case,
  `port_graph.VOCAB_VERSION`).
- Produces a SQLite sidecar (e.g., `data/forge_oracle.db`) consumed
  by one or more design-time CLIs and never by the inference path.

The subsystem already has a short-circuit for "never imported by
inference" — a structural grep fence + behavioral `--expect-identity`
audit. This pattern is about the *complementary* problem: a consumer
opening a sidecar that was built under different inputs and silently
comparing against drifted numbers.

## Guidance

### 1. One sidecar DB per subsystem, separate from the main DB

Do not add new oracle tables to `mtg_synergy.db`. Use a separate file
like `data/forge_oracle.db`. Two concrete benefits:

- The offline-only invariant becomes trivial to verify: the inference
  path never calls `sqlite3.connect` on the sidecar's filename.
- Sidecars can be regenerable without touching the pinned scoring DB.
  The build can `.tmp` + atomic rename; a crash mid-build never
  corrupts the pinned state.

### 2. Model the `config_inputs` as an immutable `NamedTuple`

```python
# src/mtg_synergy_graph/forge_oracle/config.py
class OracleConfigInputs(NamedTuple):
    forge_sha: str
    ppmi_smoothing_k: float
    min_decks_count: int
    port_signature_version: str
    java_method_id: str
```

Every field that, if changed, invalidates the sidecar must be in the
tuple. Mirror the inference-path `ScoringConfigInputs` shape so
reviewers recognize the pattern at sight.

### 3. Hash with `hashlib.sha256` over a stable serialization

```python
def compute_oracle_hash(inputs: OracleConfigInputs) -> str:
    serialized = repr(sorted(inputs._asdict().items()))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()
```

`sorted(...items())` protects against NamedTuple field-order changes
leaking through. `repr` is stable for primitive fields across Python
releases.

### 4. Write a `oracle_config` KV table in the same transaction as the data

```sql
CREATE TABLE IF NOT EXISTS oracle_config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
```

The build writes `("config_hash", <hex>)` alongside one row per
individual input value (useful for diagnostics when a hash mismatch
fires — the user immediately sees which field drifted).

### 5. Three refuse-to-run modes for consumers

Not every consumer should respond to staleness the same way. The
Forge-Second-Oracle consumers split into three tolerance tiers:

| Consumer | Missing sidecar | Stale hash | Reason |
|---|---|---|---|
| `scripts/gap_report.py` | Silent fallback + stderr warn | Silent fallback + stderr warn | Rule-authoring tool: must always produce output. A rule writer running without the sidecar still gets the volume-ranked report. |
| `scripts/forge_oracle.py propose-rules` | Exit 2 with rebuild hint | Exit 2 with rebuild hint | Consumes sidecar as load-bearing input. Empty output would be misleading. |
| `bench.py audit --vs-forge-oracle` | Exit 2 with rebuild hint | Exit 2 with rebuild hint | Sidecar IS the comparison target. Without it there's nothing to compare. |

The distinction matters: silently degrading is correct for tools that
produce a report from multiple signals; exit 2 is correct for tools
whose entire purpose is the sidecar's content.

### 6. `verify_current_or_raise` is the single enforcement seam

```python
def verify_current_or_raise(
    conn: sqlite3.Connection, inputs: OracleConfigInputs
) -> None:
    row = conn.execute(
        "SELECT value FROM oracle_config WHERE key = 'config_hash'"
    ).fetchone()
    if row is None:
        raise OracleConfigMissingError(...)
    if row[0] != compute_oracle_hash(inputs):
        raise OracleConfigStaleError(...)
```

Every strict consumer calls this exactly once at the top of its
handler. Callers that want silent fallback never call it — they just
read the sidecar and handle a missing / empty table by returning a
sentinel (empty dict, 1.0 weight, etc.).

### 7. Actionable error messages

`OracleConfigStaleError` message must include the rebuild command:

```
forge_oracle.db was built under a different config
(stored hash abc123..., current def456...).
Rebuild with `scripts/forge_oracle.py build` to refresh.
```

Don't make the user figure out which script rebuilds. They already
have a broken run; minimize the loop to recovery.

### 8. Graceful-fallback contract for soft consumers

A soft consumer that reads the sidecar (e.g., `load_forge_signals`)
should treat every failure mode as "return empty":

- Missing file → `{}`.
- `sqlite3.DatabaseError` opening → `{}`.
- Missing table → `{}`.
- Empty table → `{}`.
- All-zero values → `{}`.

All with a `logging.warning` so operators can trace why the tool
looks neutral. Never raise, never crash, never emit garbage — the
consumer's invariant is that it always produces output.

## Why this matters

Without mechanical enforcement you can build a sidecar under
`smoothing_k=0.5`, later decide to try `k=1.0`, rebuild, and then
accidentally run a legacy consumer that was hardcoded against `k=0.5`
— the consumer reads the new table silently and reports numbers that
look reasonable. You don't find out until a rule proposal downstream
is flat-out wrong.

The hash solves this with O(1) runtime cost:

- Every input that matters flows into the hash.
- Consumers that can't tolerate drift fail loudly.
- Consumers that can tolerate it degrade silently with a visible warning.
- Diagnostics (the per-field KV rows) are right next to the hash,
  so "what drifted" is one SQL query away.

## When to apply

Use this pattern whenever you're adding an **offline** subsystem that:

- Produces derived data sensitive to configuration knobs.
- Has multiple consumers with different tolerances for staleness.
- Must stay structurally isolated from a primary data path (scoring,
  inference, serving).

Do NOT apply to inference-path subsystems — those go through
`ScoringConfigInputs` + `compute_config_hash` + the `--expect-identity`
audit. See the sibling doc
[flag-gated-multi-port-rule-pattern](flag-gated-multi-port-rule-pattern-2026-04-23.md)
for that pattern.

## Examples from plan 002

- `src/mtg_synergy_graph/forge_oracle/config.py` — the NamedTuple +
  hash + `verify_current_or_raise`.
- `src/mtg_synergy_graph/forge_oracle/ingest.py` — writes the hash in
  the same transaction as the PPMI rows.
- `src/mtg_synergy_graph/bench/forge_oracle_handler.py` — strict
  consumer calling `verify_current_or_raise`.
- `src/mtg_synergy_graph/forge_oracle/gap_weight.py` — soft consumer
  returning `{}` on any failure.

## References

- [flag-gated-multi-port-rule-pattern](flag-gated-multi-port-rule-pattern-2026-04-23.md) — the inference-path analog this pattern transfers from.
- `docs/plans/2026-04-23-002-feat-forge-second-oracle-plan.md` — the plan that codified this.
- `docs/brainstorms/2026-04-21-forge-second-oracle-requirements.md` — origin requirements (FR6 SHA pinning).
