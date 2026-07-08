"""One-shot bootstrap: build the outlet-direction death-payoff cohort fixture.

Thin entry point (plan 2026-07-07-002 Task 3) over
``scripts/bootstrap_archetype_payoff_fixture.py``'s parameterized
build/pin protocol: selects ``bench.cohorts.outlet_direction_death_payoff``
(legal legendary-creature commanders with an outlet-direction, non-self,
non-Sacrificed death trigger — see that function's docstring), filters to
those with at least one ``High Synergy Cards`` row in EDHREC's tags.db, and
pins their top-N scores to ``tests/fixtures/golden_set_outlet_payoff.json``.

This is a SEPARATE fixture from ``golden_set_archetype_payoff.json`` —
``outlet_direction_death_payoff`` is deliberately NOT a member of
``bench.cohorts.archetype_payoff_cohort``'s predicate union (see that
function's docstring), so building this fixture never touches the existing
one. Same evaluation-instrument caveats apply: zero scoring-path impact,
re-run after cardsfolder imports / scoring-config changes, enforced by
``tests/bench/test_fixture_freshness.py``.

Exit codes mirror the base module: 0 success, 2 if a required DB is missing.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make src/ importable when run as a bare script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

# scripts/ is on sys.path (either auto-added as this script's own directory
# when run as __main__, or by tests/conftest.py's sys.path mutation) so this
# resolves to the sibling module, not a package-relative import.
import bootstrap_archetype_payoff_fixture as _base

from mtg_synergy_graph.bench.cohorts import outlet_direction_death_payoff

REPO_ROOT = _base.REPO_ROOT
OUTPUT_PATH = REPO_ROOT / "tests" / "fixtures" / "golden_set_outlet_payoff.json"


def main() -> int:
    return _base.main(cohort_fn=outlet_direction_death_payoff, output_path=OUTPUT_PATH)


if __name__ == "__main__":
    raise SystemExit(main())
