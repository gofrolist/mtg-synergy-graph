"""Guard: seed JSONs + committed oracle artifacts must be tracked.

Pre-commit's pytest hook runs against the working tree where
gitignored files still exist on disk — so a missing-from-git
tracked file won't be caught locally. CI clones fresh and fails on
import. This test closes that gap by asserting at test time that
the required files are tracked, not merely present.

If this test fails for a `data/...` path, check `.gitignore`: the
`data/` directory is intentionally ignored (it also holds regenerable
.db files), so tracked artifacts under `data/` must be un-ignored via
per-path ``!data/...`` exceptions. Seed JSONs moved into the package
(``src/mtg_synergy_graph/data/``) on 2026-06-09 so installed wheels
ship them; ``src/`` is never gitignored, but the tracked-ness guard
still applies (a deleted-from-git seed breaks every import).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
REQUIRED_TRACKED_FILES = (
    "src/mtg_synergy_graph/data/event_match_seed.json",
    "src/mtg_synergy_graph/data/rules_seed.json",
    "src/mtg_synergy_graph/data/scoring_weights.json",
    # Portfolio-selection family map (plan 2026-07-02-004 Unit 1):
    # rule_id -> family authority loaded at import by portfolio.py.
    "src/mtg_synergy_graph/data/family_map.json",
    # Forge-Second-Oracle plan 002 unit 1: SHA pin for the vendored
    # Forge checkout. Regenerable table (forge_oracle.db) is NOT
    # tracked — only the pin that identifies which Forge commit the
    # table was built against.
    "data/forge_oracle/version.txt",
)


def _git_tracked_files() -> set[str]:
    result = subprocess.run(
        ["git", "ls-files", "data/", "src/mtg_synergy_graph/data/"],  # noqa: S607 — `git` from PATH is the point
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def test_required_files_are_git_tracked() -> None:
    tracked = _git_tracked_files()
    missing = [p for p in REQUIRED_TRACKED_FILES if p not in tracked]
    assert not missing, (
        f"Required files are NOT committed to git: {missing}. "
        f"They may exist on disk but CI will fail on import. "
        f"Check .gitignore for `data/` directory ignore and the "
        f"corresponding `!data/...` exception."
    )


def test_required_files_exist_on_disk() -> None:
    """Corollary: they should also exist on disk — sanity check."""
    for rel in REQUIRED_TRACKED_FILES:
        path = REPO_ROOT / rel
        assert path.is_file(), f"{rel} missing from working tree"
        assert path.stat().st_size > 0, f"{rel} is empty"
