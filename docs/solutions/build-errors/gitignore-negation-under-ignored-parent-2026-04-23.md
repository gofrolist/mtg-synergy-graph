---
title: Gitignore negation fails under already-ignored parent directory
date: 2026-04-23
category: build-errors
module: mtg-synergy-graph
problem_type: build_error
component: development_workflow
severity: high
symptoms:
  - "CI fails on fresh clone with FileNotFoundError: data/event_match_seed.json not found"
  - "Pre-commit hooks and local pytest pass while CI is red"
  - "Runtime code cannot load data/*_seed.json in fresh checkouts"
  - "Adding !data/*_seed.json after data/ in .gitignore has no effect"
  - "git ls-files data/ returns empty while ls data/ shows the files present"
root_cause: config_error
resolution_type: config_change
related_components:
  - tooling
  - testing_framework
tags: [gitignore, ci, fresh-clone, seed-data, regression-guard, pre-commit, port-graph]
---

# Gitignore negation fails under already-ignored parent directory

## Problem

Fresh CI clones failed at import time with `FileNotFoundError: data/event_match_seed.json not found` because two declarative seed JSONs required by the typed port-graph substrate were never tracked by git. The working tree retained the files locally, masking the issue from developers and from pre-commit hooks.

## Symptoms

- `FileNotFoundError: data/event_match_seed.json not found` on GitHub Actions immediately after checkout.
- Same error for `data/rules_seed.json` once the first was resolved.
- Local `uv run pytest tests/` and pre-commit hooks passed cleanly.
- `git clone` of the repo into a scratch directory reproduced the CI failure deterministically.
- `git ls-files data/` returned empty, even though `ls data/` showed both JSONs present on disk.

## What Didn't Work

- **Assuming pre-commit would catch the gap.** Pre-commit ran pytest against the working tree where the JSONs existed on disk; it had no visibility into git's tracked-file set, so import-time loads succeeded.
- **First gitignore fix — adding `!data/*_seed.json` beneath the existing `data/` line:**
  ```gitignore
  data/
  !data/*_seed.json   # no effect
  ```
  `git check-ignore -v data/event_match_seed.json` still reported the file as ignored. Git never descends into a directory already matched by a directory-level ignore, so negation patterns nested under it are unreachable.
- **`git add -f data/event_match_seed.json` as a workaround.** Force-add succeeded for the one-shot commit, but the files would silently fall back out of tracking on any future `git rm --cached` or rename, and new seed files would hit the same trap. Not a durable fix.
- **Diagnosing as a packaging bug.** Briefly considered whether `importlib.resources` or a missing package-data entry in `pyproject.toml` was at fault before `git ls-files` made the tracking gap obvious.

## Solution

**`.gitignore` — ignore the directory's *contents*, not the directory itself:**

```gitignore
# Before (broken — negation unreachable)
data/

# After (working — negation evaluated per-child)
data/*
!data/*_seed.json
```

**Regression guard at `tests/test_seed_files_tracked.py`:**

```python
import subprocess


def test_seed_jsons_are_tracked():
    """Both declarative seed JSONs must be committed to git.

    Regression guard: if someone re-adds `data/` (without the `/*` trailer)
    to .gitignore, git will stop tracking the seed JSONs. CI environments
    cloning fresh will then hit FileNotFoundError at import time.
    """
    result = subprocess.run(
        ["git", "ls-files", "data/"],
        capture_output=True,
        text=True,
        check=True,
    )
    tracked = set(result.stdout.splitlines())
    assert "data/event_match_seed.json" in tracked, (
        "data/event_match_seed.json is not tracked by git — check .gitignore"
    )
    assert "data/rules_seed.json" in tracked, (
        "data/rules_seed.json is not tracked by git — check .gitignore"
    )
```

Landed in commit `3a86c9d fix(ci): commit declarative seed JSONs + guard against future drift`.

## Why This Works

Git's ignore evaluation is **directory-prunes-before-descent**. When git walks the tree to decide which paths are candidates for staging, it checks each directory against `.gitignore` patterns *before* recursing into it:

- `data/` (trailing slash, no `*`) matches the directory itself. Once matched, git prunes the entire subtree and never evaluates children. Any negation pattern targeting `data/foo` below this line is dead code — the walker never reaches `foo` to test it. This is documented behavior: *"It is not possible to re-include a file if a parent directory of that file is excluded."*
- `data/*` matches every direct child of `data/` individually. Git *must* descend into `data/` to expand the glob, so each child is tested against subsequent patterns. The next line, `!data/*_seed.json`, is now reachable and re-includes matching children. Everything else (`tags.db`, `cardsfolder/`, generated artifacts) stays ignored.

The regression test closes the loop by asserting on git's **tracked-file set** (`git ls-files`) rather than the filesystem. Filesystem presence is what deceived pre-commit; tracked-set presence is what CI actually sees after `git clone`. Any future edit that regresses the pattern back to `data/` — a common cleanup instinct — will cause the tracked set to lose both JSONs on the next commit, and the test fails before the change reaches CI.

## Prevention

- **Prefer the contents-wildcard form when committing files under a broadly-ignored directory.** Use `dir/*` + `!dir/keep.ext`, never `dir/` + `!dir/keep.ext`. The shape of the parent pattern determines whether the child negation is even evaluated:
  ```gitignore
  # Pattern to reach for when you need exceptions
  build/*
  !build/version.txt
  ```
- **Add a `git ls-files` regression test for any file loaded at import or startup.** The snippet above is the concrete form that shipped; the template below is the generalizable pattern to copy into a new project. It anchors `cwd` to the repo root (so `git ls-files` resolves correctly regardless of where pytest is invoked from), types `REQUIRED_TRACKED` as a `Final` tuple, and collects all missing paths into a single assertion message:
  ```python
  import subprocess
  from pathlib import Path
  from typing import Final

  # Adjust `parents[N]` to point at the repo root from this test file.
  REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
  REQUIRED_TRACKED: Final[tuple[str, ...]] = (
      "data/event_match_seed.json",
      "data/rules_seed.json",
      # add runtime-loaded artifacts here as they are introduced
  )


  def test_required_files_are_tracked() -> None:
      """Regression guard: runtime-loaded files must be committed to git.

      If a broad gitignore hides them, pre-commit still passes (the files
      exist in the working tree on disk), but CI fresh clones fail at
      import time because git never tracked them.
      """
      result = subprocess.run(
          ["git", "ls-files"],
          cwd=REPO_ROOT,
          capture_output=True,
          text=True,
          check=True,
      )
      tracked = set(result.stdout.splitlines())
      missing = [p for p in REQUIRED_TRACKED if p not in tracked]
      assert not missing, f"Not tracked by git (check .gitignore): {missing}"
  ```
  Precondition: the test assumes a git work tree. If your CI runs against a source tarball or a tree without `.git`, `subprocess.run(..., check=True)` will raise `CalledProcessError`; either skip the test in that environment or drop `check=True` and handle the non-zero exit explicitly.
- **Verify the guard actually fires.** Before trusting it, break it on purpose:
  ```bash
  git rm --cached data/event_match_seed.json
  uv run pytest tests/test_seed_files_tracked.py  # expect red
  git restore --staged data/event_match_seed.json
  ```
  A guard that never fails is indistinguishable from a guard that doesn't work.
- **Treat "works locally, fails in CI" as a structural gap, not a flake.** Pre-commit validates the working tree; CI validates the git index. The two diverge exactly when `.gitignore` hides files the code depends on. Any test that reads from the filesystem is complicit — pair it with one that reads from `git ls-files`.
- **Sanity-check new additions with a throwaway clone.** Before landing a commit that introduces a runtime-loaded resource:
  ```bash
  git clone . /tmp/fresh-clone && cd /tmp/fresh-clone && uv run pytest tests/
  ```
  This mirrors CI's view and catches gitignore traps in seconds.
- **Verify the pattern directly with `git check-ignore -v <path>`** when adding exceptions. It reports which line of which ignore file matched — if the matching line is a directory-level rule above your negation, the negation is unreachable and you need the `dir/*` form.

## Related Issues

- Commit `3a86c9d fix(ci): commit declarative seed JSONs + guard against future drift` (main branch).
- Plan: `docs/plans/2026-04-22-002-feat-typed-port-graph-substrate-plan.md` — introduced the two seed JSONs (`data/event_match_seed.json`, `data/rules_seed.json`) that exposed the gitignore gap.
- `.gitignore` — authoritative record of the fix; carries an inline comment explaining the gotcha.
- `tests/test_seed_files_tracked.py` — the regression guard referenced above.
