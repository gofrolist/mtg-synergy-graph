# Forge-Second-Oracle — Setup & Maintenance

Design-time oracle feeding `gap_report.py` re-ranking and
`bench.py audit --vs-forge-oracle`. **Never consulted at inference.**

Plan: [docs/plans/2026-04-23-002-feat-forge-second-oracle-plan.md](plans/2026-04-23-002-feat-forge-second-oracle-plan.md).
Origin: [docs/brainstorms/2026-04-21-forge-second-oracle-requirements.md](brainstorms/2026-04-21-forge-second-oracle-requirements.md).

## Vendored Forge checkout

The oracle reads two things from upstream Forge:

1. Java AI source (BoosterDraftAI + DeckgenUtil) — feeds FR1 Python port.
2. Precon `.dck` decks — feeds FR2 PPMI table.

Forge is vendored at `data/forge/` as a **partial clone** (`blob:none`)
with **sparse-checkout enabled**. The filter + sparse list keep disk
usage ~order-of-magnitude below a full clone.

### First-time setup (fresh repo clone)

```bash
# Clone Forge as a partial, non-cone sparse checkout.
git clone --filter=blob:none --sparse \
  https://github.com/Card-Forge/forge.git data/forge

# Switch off cone mode so our non-cone pattern file works.
git -C data/forge sparse-checkout disable
git -C data/forge config core.sparseCheckout true

# Install the pinned sparse pattern list.
cp docs/forge_sparse_checkout.txt data/forge/.git/info/sparse-checkout
git -C data/forge read-tree -mu HEAD

# Pin to the committed Forge SHA.
git -C data/forge checkout "$(grep -Ev '^(#|$)' data/forge_oracle/version.txt | head -1)"
```

`docs/forge_sparse_checkout.txt` is the committed reference copy of
`data/forge/.git/info/sparse-checkout`; keep them in sync when you
extend the sparse list.

### SHA pinning

`data/forge_oracle/version.txt` pins the Forge commit SHA used to
build the oracle sidecar. Shape:

```
# comments OK
<40-char-hex-SHA>
```

Before reading any sidecar-derived artifact, oracle consumers call
`forge_oracle.version.verify_pin_matches_checkout()` — drift raises
`OracleVersionMismatchError` with an actionable rebuild hint.

Current pin: `ed97d9bb77f03d9681aba59186416bcf7923d5dd`.

### Bumping the pin

`scripts/forge_oracle.py upgrade` (delivered in plan unit 5) will:

1. Fetch upstream Forge, fast-forward the checkout.
2. Re-run `scripts/forge_oracle.py build` to regenerate
   `data/forge_oracle.db` against the new SHA.
3. Write the new SHA into `data/forge_oracle/version.txt`.
4. Emit a diff report: Forge-agreement τ delta, new / removed PPMI
   pairs, new / removed BoosterDraftAI score fields.

Do **not** `git pull` in `data/forge/` without running `upgrade` —
the oracle sidecar will start raising `OracleVersionMismatchError`
on every strict-consumer invocation.

## Invariant: offline only

These files MUST NOT import from `src/mtg_synergy_graph/forge_oracle/`:

- `src/mtg_synergy_graph/engine.py`
- `src/mtg_synergy_graph/universal_scorer.py`
- `src/mtg_synergy_graph/graph_engine.py`
- everything under `src/mtg_synergy_graph/complement_rules/`
- `scripts/recommend.py`

Enforced by `tests/test_forge_oracle_isolation.py` (grep fence,
delivered in plan unit 9) and `bench.py audit --expect-identity`
(behavioral).
