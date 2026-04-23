---
title: 26 forge_oracle tests fail in CI because data/forge/ is a gitignored vendored clone
date: 2026-04-23
category: test-failures
module: forge_oracle
problem_type: test_failure
component: testing_framework
symptoms:
  - "26 CI tests fail with OracleForgeCheckoutError: data/forge is not a git checkout"
  - "Local pytest runs fully green (1568 passed) because data/forge/ is present locally"
  - "All 26 failures are identical — not flaky, not specific to one test"
  - "Failures span 5 test files (version_pin, config_hash, ingest, bench_vs_forge_oracle, propose_rules), all transitively calling get_oracle_config_inputs()"
root_cause: incomplete_setup
resolution_type: test_fix
severity: high
related_components:
  - tooling
  - development_workflow
tags:
  - ci
  - autouse-fixture
  - conftest
  - monkeypatch
  - gitignored-dependency
  - forge-oracle
  - subprocess-stub
---

# 26 forge_oracle tests fail in CI because data/forge/ is a gitignored vendored clone

## Problem

The `forge_oracle` package (added in PR #12, plan 2026-04-23-002) captures a pinned Forge commit SHA as one of the inputs to its config-hash. `config.get_oracle_config_inputs()` calls `version.read_current_forge_sha()`, which shells out to `git -C data/forge rev-parse HEAD`. `data/forge/` is a vendored partial clone — gitignored via `data/*`, set up manually in dev per [`docs/FORGE_ORACLE.md`](../../FORGE_ORACLE.md). CI runners don't have it and the `ci.yml` workflow does not clone Forge.

Result: 26 tests that transitively call `get_oracle_config_inputs()` pass locally on every dev machine but fail immediately the moment the PR is pushed. No individual test is about the subprocess call — they all exercise oracle build / verify / handler / propose-rules logic that happens to capture the SHA as part of normal operation.

## Symptoms

Every failing test emits an identical exception:

```
mtg_synergy_graph.forge_oracle.version.OracleForgeCheckoutError:
/home/runner/work/mtg-synergy-graph/mtg-synergy-graph/data/forge is not a git checkout.
See docs/FORGE_ORACLE.md for setup instructions.
```

Failures spanned five test files:

- `tests/test_forge_oracle_version_pin.py` (3 tests)
- `tests/test_forge_oracle_config_hash.py` (2 tests)
- `tests/test_forge_oracle_ingest.py` (6 tests)
- `tests/test_bench_vs_forge_oracle.py` (10 tests)
- `tests/test_forge_oracle_propose_rules.py` (5 tests)

Local pytest with `data/forge/` present: `1568 passed, 0 failed`. GitHub Actions `test (3.13)` on the same commit: `26 failed, 1542 passed`.

## What Didn't Work

Four approaches were considered and rejected before the autouse-stub landed. (session history)

1. **Clone Forge in CI.** Add a `git clone --filter=blob:none --sparse ...` step to `.github/workflows/ci.yml`. Rejected: adds ~90s to every CI run for an offline-only feature the inference path doesn't depend on. Keeping CI yaml simple outweighs the marginal benefit. Plan 002 explicitly decided Forge stays dev-only; CI was never expected to receive a clone.

2. **`@pytest.mark.skipif(not Path("data/forge").exists())` on every affected test.** Rejected: CI would silently skip 26 tests and miss regressions in oracle build, verify, and propose-rules paths. Silent skips are worse than visible failures for coverage confidence — the tests exist to catch logic bugs, not to check whether Forge is installed.

3. **Per-test `monkeypatch` in each of the five test files.** Rejected: boilerplate for a dependency orthogonal to what any test is asserting. The 26 tests care about config correctness, verify semantics, handler dispatch, and propose-rules logic — not about access to `data/forge/`.

4. **Factor `forge_sha` out as a kwarg through every call chain.** Would thread a stub value through `build_forge_oracle_db`, `handle_vs_forge_oracle`, `_cmd_propose_rules`, etc. Rejected: the refactor cost is high and changes the public API for a detail that belongs in test infrastructure, not production signatures.

**What did NOT happen (session history):** no prior conftest stubs for `forge_oracle` existed — the package was entirely new in PR #12. Every forge_oracle test passed locally during 10 commits of development because `data/forge/` was set up. The CI failure surfaced the first time the tests ran outside the dev machine. Total time from CI-red to CI-green after diagnosis: ~6 minutes.

## Solution

A single `autouse=True` conftest fixture stubs `read_current_forge_sha` for the whole session when `data/forge/.git` is absent. When the real checkout is present, the fixture is a no-op.

Commit: `ec00d94 fix(tests): stub read_current_forge_sha when data/forge/ is absent` on branch `feat/forge-oracle-phase-1`, merged as `1c67369` on `main` via [PR #12](https://github.com/gofrolist/mtg-synergy-graph/pull/12).

**`tests/conftest.py` (added fixture):**

```python
@pytest.fixture(autouse=True)
def _stub_forge_sha_when_checkout_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    from mtg_synergy_graph.forge_oracle import version as fo_version

    if (fo_version._FORGE_DIR / ".git").exists():
        # Real checkout present — exercise the real function.
        return

    pinned_sha = fo_version.read_pinned_sha()
    real_read = fo_version.read_current_forge_sha

    def _stub(forge_dir: Path = fo_version._FORGE_DIR) -> str:
        # Preserve negative-path semantics: explicit non-default paths go
        # through the real function so tests targeting error modes still fire.
        if forge_dir != fo_version._FORGE_DIR:
            return real_read(forge_dir)
        return pinned_sha

    monkeypatch.setattr(fo_version, "read_current_forge_sha", _stub)
```

**Verification — simulate CI locally before pushing:**

```bash
mv data/forge/.git data/forge/.git.bak && uv run pytest && mv data/forge/.git.bak data/forge/.git
```

54 affected tests (26 originally failing + 28 indirectly touching the oracle path) all passed via the stub. Then CI re-ran: `lint` PASS 17s, `test (3.13)` PASS 27s.

## Why This Works

**Environment-aware detection.** The fixture inspects `data/forge/.git` presence at session start. When the checkout exists (local dev), the fixture returns immediately and the full subprocess path runs unmodified. No production behavior change.

**Stub value equals committed truth.** `read_pinned_sha()` reads `data/forge_oracle/version.txt`, which is a committed file. The stub returns the exact SHA that would come from `git -C data/forge rev-parse HEAD` if the checkout were present and in sync — the same invariant that `test_verify_pin_matches_checkout_passes_when_in_sync` asserts for local runs. When the pin bumps via `scripts/forge_oracle.py upgrade`, the stub automatically follows.

**Negative-path tests continue to fire.** The stub's conditional — `if forge_dir != fo_version._FORGE_DIR: return real_read(forge_dir)` — means any test that passes an explicit `tmp_path` (e.g. `test_read_current_forge_sha_raises_on_missing_checkout`) still hits the real `subprocess` call and still raises the expected `OracleForgeCheckoutError`. The error mode is not silenced.

**Autouse + monkeypatch = zero boilerplate, zero cross-test interference.** The fixture applies to every test in the session without touching any individual test file. `monkeypatch` auto-reverts between tests, so no test can observe a patched state left by another.

## Prevention

1. **Treat all gitignored dev-only dependencies as CI fault vectors.** Any module that shells out to a gitignored directory (external checkout, local model weights, credential file) should have a matching conftest stub established at the time the module is first tested — not retroactively after CI fails. For `forge_oracle`, the stub should have been introduced alongside the first unit test that called `get_oracle_config_inputs()`.

2. **Simulate missing dependencies locally before opening a PR.** The one-liner below is cheap and catches this entire class of bug pre-push:

   ```bash
   mv data/forge/.git data/forge/.git.bak && uv run pytest && mv data/forge/.git.bak data/forge/.git
   ```

   Consider adding a `just test-ci-sim` task or similar that wraps this pattern. Per-dependency variants (for each gitignored dev dir) compose cleanly.

3. **Prefer a single autouse conftest stub over per-test monkeypatches.** When many tests transitively depend on an external resource but none of them are testing the resource access itself, one conditional stub in `conftest.py` is the correct abstraction level. Per-test patches spread the same concern across many files and diverge over time.

4. **Stub values must track committed truth, not hardcoded literals.** Reading the pinned SHA from `version.txt` (rather than hardcoding `"ed97d9bb77f03d9681aba59186416bcf7923d5dd"`) means the stub auto-follows if the pin is bumped in a future commit — no manual stub update required and no future CI failure from a stale literal.

5. **When the fix is a test-infrastructure pattern, document it.** Autouse conftest stubs for absent external dependencies are reusable; the next time someone adds a subsystem that shells out to a gitignored resource, this doc is the reference.

## Related Issues

- [`docs/solutions/best-practices/offline-oracle-hash-pattern-2026-04-23.md`](../best-practices/offline-oracle-hash-pattern-2026-04-23.md) — the pattern doc for the subsystem this test infrastructure supports. Covers runtime hash-enforcement and the three consumer strictness tiers for `forge_oracle`. Should be updated with a "Test isolation" pointer to this doc.
- [`docs/solutions/build-errors/gitignore-negation-under-ignored-parent-2026-04-23.md`](../build-errors/gitignore-negation-under-ignored-parent-2026-04-23.md) — sibling "works locally, fails in CI" pattern. That doc resolves missing-tracked-file gaps via `.gitignore` fix; this doc resolves missing-external-checkout gaps via test stubbing.
- [`docs/FORGE_ORACLE.md`](../../FORGE_ORACLE.md) — setup instructions for `data/forge/` partial clone; the prerequisite this fixture stub avoids in CI.
- [`docs/plans/2026-04-23-002-feat-forge-second-oracle-plan.md`](../../plans/2026-04-23-002-feat-forge-second-oracle-plan.md) — plan that introduced the feature and codified the dev-only Forge stance.
- PR #12 on `gofrolist/mtg-synergy-graph` — Forge-Second-Oracle feature merge; this fix landed as commit `ec00d94` on the same branch.
