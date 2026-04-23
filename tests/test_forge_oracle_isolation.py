"""Inference-path isolation regression test — plan Unit 9.

Mechanically enforces the architectural invariant that the
forge-oracle is **offline-only**. The following files MUST NOT
import from ``src/mtg_synergy_graph/forge_oracle/``:

- ``src/mtg_synergy_graph/engine.py``  (``SynergyEngine.page``)
- ``src/mtg_synergy_graph/universal_scorer.py``  (scoring math)
- ``src/mtg_synergy_graph/graph_engine.py``  (port matching)
- everything under ``src/mtg_synergy_graph/complement_rules/``
- ``scripts/recommend.py``  (inference CLI)

Enforcement has two layers:

1. **Grep fence** (this test) — a static substring search. Catches
   unused-but-present imports that behavioral tests would miss
   ("I imported it `just in case`, it's not called yet").
2. **bench.py audit --expect-identity** — a behavioral assertion
   that scores are bitwise-identical before and after. Subprocess
   test below invokes it directly.

Both together provide belt-and-suspenders. The plan's Unit 9
checklist calls out that either alone has a known gap:

* --expect-identity misses imports that don't yet affect output.
* Grep fence misses changes that route through a refactor (e.g.,
  module-level state mutation that leaves the import declaration
  untouched).

Plan: docs/plans/2026-04-23-002-feat-forge-second-oracle-plan.md Unit 9.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent

#: Inference-path files that MUST stay isolated from forge_oracle.
_SENTINEL_FILES: tuple[Path, ...] = (
    _REPO_ROOT / "src" / "mtg_synergy_graph" / "engine.py",
    _REPO_ROOT / "src" / "mtg_synergy_graph" / "universal_scorer.py",
    _REPO_ROOT / "src" / "mtg_synergy_graph" / "graph_engine.py",
    _REPO_ROOT / "scripts" / "recommend.py",
)

#: Dir whose every .py file must stay isolated.
_SENTINEL_DIRS: tuple[Path, ...] = (_REPO_ROOT / "src" / "mtg_synergy_graph" / "complement_rules",)

#: Allowed context-only mentions. Docstrings and explicit invariant
#: comments that REFERENCE the isolation rule itself are fine — the
#: regex below ignores any line that is purely a comment or docstring
#: line mentioning the word, but flags actual imports.
_IMPORT_PATTERN = re.compile(
    r"^\s*(?:from\s+mtg_synergy_graph\.forge_oracle|import\s+mtg_synergy_graph\.forge_oracle|from\s+forge_oracle|import\s+forge_oracle)",
    flags=re.MULTILINE,
)


def _iter_sentinel_files() -> list[Path]:
    files = list(_SENTINEL_FILES)
    for d in _SENTINEL_DIRS:
        if not d.is_dir():
            continue
        files.extend(sorted(p for p in d.rglob("*.py") if p.is_file()))
    return files


def test_no_inference_path_imports_forge_oracle() -> None:
    """Grep fence: no inference-path file may import from forge_oracle.

    If this test fails, something in ``SynergyEngine.page`` / scoring /
    complement_rules / recommend.py has started reading the offline
    oracle — that violates the plan 002 architectural invariant. Move
    the consumer into ``scripts/forge_oracle.py`` or ``bench.py``, or
    revisit the plan before landing the change.
    """
    violations: list[tuple[Path, str]] = []
    for path in _iter_sentinel_files():
        text = path.read_text(encoding="utf-8")
        for match in _IMPORT_PATTERN.finditer(text):
            line_no = text[: match.start()].count("\n") + 1
            # Pull the matching line for the error message.
            line = text.splitlines()[line_no - 1]
            violations.append((path, f"{line_no}: {line.strip()}"))

    assert not violations, (
        "Inference-path file(s) import from forge_oracle — this violates "
        "the plan 002 isolation invariant:\n"
        + "\n".join(f"  {p.relative_to(_REPO_ROOT)} {detail}" for p, detail in violations)
    )


@pytest.mark.integration
@pytest.mark.skipif(
    not (_REPO_ROOT / "data" / "synergy.db").exists() and not (_REPO_ROOT / "mtg_synergy.db").exists(),
    reason="requires synergy.db (run scripts/import_cardsfolder.py)",
)
def test_expect_identity_still_passes_in_subprocess() -> None:
    """Run ``bench.py audit --expect-identity`` end-to-end.

    This complements the grep fence with a behavioural check: even
    if nothing in the inference path imports forge_oracle, any change
    that accidentally moves scoring math would surface here as a
    per-(cmdr, cand) score mismatch against the pinned fixture.

    Tagged ``integration`` because it scores the 100-commander golden
    set live against the pinned fixture — takes ~60s. Skipped when
    synergy.db is absent.
    """
    result = subprocess.run(  # noqa: S603 — sys.executable + fixed script path, no user input
        [
            sys.executable,
            str(_REPO_ROOT / "scripts" / "bench.py"),
            "audit",
            "--expect-identity",
        ],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, (
        "bench.py audit --expect-identity failed after this change.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "PASS" in result.stdout or "PASS" in result.stderr, (
        "bench.py audit --expect-identity did not emit a PASS marker.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def test_grep_fence_detects_injected_violation(tmp_path: Path) -> None:
    """Meta-test: the grep pattern actually fires on a simulated import.

    Protects against regressions where someone 'fixes' the regex in
    a way that quietly allows imports through. Uses a synthetic file
    in tmp_path so we do not pollute the sentinel set.
    """
    synthetic = tmp_path / "fake_inference_module.py"
    synthetic.write_text(
        "from mtg_synergy_graph.forge_oracle import pair_scorer\n\ndef compute():\n    return pair_scorer.rate_pair\n",
        encoding="utf-8",
    )
    text = synthetic.read_text(encoding="utf-8")
    matches = list(_IMPORT_PATTERN.finditer(text))
    assert matches, "Grep pattern must match a real forge_oracle import"


def test_grep_fence_ignores_docstring_mentions(tmp_path: Path) -> None:
    """Docstring / comment mentions of 'forge_oracle' are OK — only
    actual import statements trigger the fence."""
    synthetic = tmp_path / "clean_inference_module.py"
    synthetic.write_text(
        '"""Module docstring that mentions forge_oracle for context.\n\n'
        "This module does NOT import from forge_oracle per plan 002.\n"
        '"""\n\n'
        "# See forge_oracle/__init__.py for the offline side.\n"
        "def compute():\n    return 42\n",
        encoding="utf-8",
    )
    text = synthetic.read_text(encoding="utf-8")
    assert not list(_IMPORT_PATTERN.finditer(text)), (
        "Grep pattern must ignore docstring / comment mentions — only imports should trigger."
    )
