"""Guard the utility/__init__.py re-export convention.

The utility subpackage is a thin re-export shim: every ``_find_*`` rule in a
submodule must be surfaced through the package's public surface so that legacy
imports like ``from mtg_synergy_graph.complement_rules.utility import _find_foo``
(used by ``core.py`` and the tests) keep working.

If someone adds a new ``_find_*`` to a submodule and forgets to re-export it,
this test fails with a pointer to the exact fix.
"""

from __future__ import annotations

import ast
from pathlib import Path

from mtg_synergy_graph.complement_rules import utility as utility_pkg

UTILITY_DIR = Path(utility_pkg.__file__).parent
PRIVATE_MODULE_STEMS = frozenset({"__init__", "_shared"})


def _find_defs_in_submodule(path: Path) -> set[str]:
    """Return every top-level ``def _find_*`` name in ``path``."""

    tree = ast.parse(path.read_text(), filename=str(path))
    return {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("_find_")
    }


def _all_find_defs() -> dict[Path, set[str]]:
    """Map each non-private submodule to its ``_find_*`` defs."""

    return {
        path: _find_defs_in_submodule(path)
        for path in sorted(UTILITY_DIR.glob("*.py"))
        if path.stem not in PRIVATE_MODULE_STEMS
    }


class TestReExportConvention:
    def test_every_find_def_is_reexported_in_all(self) -> None:
        missing: list[tuple[str, str]] = []
        for path, names in _all_find_defs().items():
            for name in names:
                if name not in utility_pkg.__all__:
                    missing.append((path.name, name))

        assert not missing, (
            "Found _find_* functions in utility/ submodules that are not "
            "listed in utility/__init__.py __all__. Add them to both the "
            "matching `from .<submodule> import (...)` block and to __all__ "
            "so that `from mtg_synergy_graph.complement_rules.utility "
            "import <name>` keeps working.\n"
            f"Missing: {missing}"
        )

    def test_every_find_def_is_importable_from_package(self) -> None:
        missing: list[tuple[str, str]] = []
        for path, names in _all_find_defs().items():
            for name in names:
                if not hasattr(utility_pkg, name):
                    missing.append((path.name, name))

        assert not missing, (
            "Found _find_* functions that AST-parse in a submodule but do "
            "not exist as attributes on the utility package. Add an explicit "
            "`from .<submodule> import <name>` line to utility/__init__.py.\n"
            f"Missing: {missing}"
        )

    def test_all_listed_names_actually_exist(self) -> None:
        """Guard against stale entries in ``__all__`` that point at nothing."""

        ghosts = [name for name in utility_pkg.__all__ if not hasattr(utility_pkg, name)]
        assert not ghosts, f"utility/__init__.py __all__ lists names that do not resolve. Remove or re-import: {ghosts}"

    def test_all_is_sorted(self) -> None:
        """Keep ``__all__`` sorted so diffs stay small when rules are added."""

        assert utility_pkg.__all__ == sorted(utility_pkg.__all__), (
            "utility/__init__.py __all__ must stay sorted alphabetically."
        )


class TestSubmoduleHealth:
    """Catch regressions in the split itself, not just the re-exports."""

    def test_every_submodule_defines_at_least_one_find(self) -> None:
        """A submodule with no ``_find_*`` is dead weight - the split got it wrong."""

        empty = [path.name for path, names in _all_find_defs().items() if not names]
        assert not empty, (
            f"These utility submodules contain no _find_* functions - merge them into a sibling or delete them: {empty}"
        )

    def test_no_duplicate_find_across_submodules(self) -> None:
        """Each ``_find_*`` should live in exactly one submodule."""

        seen: dict[str, str] = {}
        duplicates: list[tuple[str, str, str]] = []
        for path, names in _all_find_defs().items():
            for name in names:
                if name in seen:
                    duplicates.append((name, seen[name], path.name))
                else:
                    seen[name] = path.name
        assert not duplicates, f"Same _find_* appears in multiple submodules - pick one home: {duplicates}"


def test_core_legacy_import_still_works() -> None:
    """The concrete legacy import path used by core.py:976 must keep resolving."""

    from mtg_synergy_graph.complement_rules.utility import (  # noqa: F401
        _find_cardpower_axis_feeders,
        _find_cascade_value,
        _find_cost_payoff_complements,
        _find_counter_axis_feeders,
        _find_counter_target_payoff,
        _find_creature_died_feeders,
        _find_creature_untap_engine,
        _find_creatures_as_lands_landfall,
        _find_damage_doubler_synergy,
        _find_etb_tapped_stax_feeders,
        _find_extra_land_plays,
        _find_flicker_payoffs,
        _find_flicker_synergy,
        _find_gy_fuel_feeders,
        _find_hand_size_feeders,
        _find_land_bounce_feeders,
        _find_landfall_enablers,
        _find_life_total_feeders,
        _find_lifegain_feeders,
        _find_mana_doubler_synergy,
        _find_modified_axis_feeders,
        _find_monarch_synergy,
        _find_multicolor_untap,
        _find_opponent_forcing,
        _find_party_feeders,
        _find_tap_type_feeders,
        _find_untap_combo,
        _find_untap_synergy,
        _find_wheel_synergy,
    )
