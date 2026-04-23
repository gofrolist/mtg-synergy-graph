"""Guard: seed JSONs must be committed to git.

Pre-commit's pytest hook runs against the working tree where
gitignored files still exist on disk — so a missing-from-git seed
file won't be caught locally. CI clones fresh and fails on import.
This test closes that gap by asserting at test time that both seed
files are tracked, not merely present.

If this test fails, check `.gitignore`: the `data/` directory is
intentionally ignored (it also holds regenerable .db files), so
seed JSONs must be un-ignored via `!data/*_seed.json`.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
REQUIRED_SEED_FILES = (
    "data/event_match_seed.json",
    "data/rules_seed.json",
)


def _git_tracked_files() -> set[str]:
    result = subprocess.run(
        ["git", "ls-files", "data/"],  # noqa: S607 — `git` from PATH is the point
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def test_seed_files_are_git_tracked() -> None:
    tracked = _git_tracked_files()
    missing = [p for p in REQUIRED_SEED_FILES if p not in tracked]
    assert not missing, (
        f"Seed files are NOT committed to git: {missing}. "
        f"They may exist on disk but CI will fail on import. "
        f"Check .gitignore for `data/` directory ignore and the "
        f"`!data/*_seed.json` exception."
    )


def test_seed_files_exist_on_disk() -> None:
    """Corollary: they should also exist on disk — sanity check."""
    for rel in REQUIRED_SEED_FILES:
        path = REPO_ROOT / rel
        assert path.is_file(), f"{rel} missing from working tree"
        assert path.stat().st_size > 0, f"{rel} is empty"
