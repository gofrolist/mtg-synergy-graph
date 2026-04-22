"""Unit 8 — deprecation-notice shims on legacy eval scripts.

The legacy scripts still run, but each now prints a single stderr line
pointing to its ``bench.py`` replacement. This module locks the contract
so a future refactor doesn't silently drop the deprecation pointer.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mtg_synergy_graph.bench._deprecation import emit_deprecation

_REPO_ROOT = Path(__file__).resolve().parents[2]


def test_emit_deprecation_writes_to_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Helper writes a single line containing the legacy + replacement paths."""
    emit_deprecation("scripts/legacy.py", "bench.py audit")
    err = capsys.readouterr().err
    assert "DEPRECATED" in err
    assert "scripts/legacy.py" in err
    assert "bench.py audit" in err


@pytest.mark.parametrize(
    "legacy_script",
    [
        "scripts/_audit_rule_impact.py",
        "scripts/golden_set_track.py",
        "scripts/compare_edhrec.py",
        "scripts/weight_grid_search.py",
        "scripts/broad_set_track.py",
    ],
)
def test_legacy_script_imports_emit_deprecation(legacy_script: str) -> None:
    """Each legacy script's ``main()`` body contains an ``emit_deprecation``
    call so the pointer fires on the very first line of execution.
    """
    content = (_REPO_ROOT / legacy_script).read_text(encoding="utf-8")
    assert "emit_deprecation" in content, f"{legacy_script} missing emit_deprecation() call at top of main()"


def test_claude_md_mentions_bench_py() -> None:
    """CLAUDE.md Common Commands section documents the new CLI."""
    claude_md = (_REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    assert "bench.py audit" in claude_md
    # Legacy commands are no longer the recommended entry; the unified
    # harness section exists.
    assert "Unified eval harness" in claude_md


def test_rule_planning_md_references_bench_py() -> None:
    """docs/RULE_PLANNING.md workflow table points to bench.py."""
    rp = (_REPO_ROOT / "docs" / "RULE_PLANNING.md").read_text(encoding="utf-8")
    assert "bench.py audit" in rp


def test_deprecation_call_is_first_line_of_main() -> None:
    """Sanity: ``emit_deprecation`` lives inside ``main()`` (not at module
    scope), so it fires when the script is run rather than when it is
    merely imported. Regression guard against a careless refactor that
    hoists the call out of main.
    """
    for script in (
        "_audit_rule_impact.py",
        "golden_set_track.py",
        "compare_edhrec.py",
        "weight_grid_search.py",
        "broad_set_track.py",
    ):
        text = (_REPO_ROOT / "scripts" / script).read_text()
        # Find the `def main(` line, then the first `emit_deprecation(` after it.
        main_idx = text.index("def main(")
        emit_idx = text.index("emit_deprecation(", main_idx)
        between = text[main_idx:emit_idx]
        # The call must be in the opening lines of main, not buried deep.
        assert between.count("\n") < 10, f"emit_deprecation() is not near the top of main() in {script}"
