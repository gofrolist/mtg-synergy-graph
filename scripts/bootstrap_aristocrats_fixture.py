"""One-shot bootstrap: build the aristocrats cohort fixture.

Thin entry point over ``scripts/bootstrap_archetype_payoff_fixture.py``'s
parameterized build/pin protocol: selects ``bench.cohorts.aristocrats``
(sacrifice-outlet / death-trigger legal legendary-creature commanders), filters
to those with at least one ``High Synergy Cards`` row in EDHREC's tags.db, and
pins their top-N scores to ``tests/fixtures/golden_set_aristocrats.json``.

Evaluation instrument, zero scoring-path impact. Use THIS (not
``bench.py audit --repin``) after a cardsfolder import — ``--repin`` preserves
the old cohort_members snapshot. The fixture carries its own ``config_hash`` and
is enforced by ``tests/bench/test_fixture_freshness.py``.

Exit codes mirror the base module: 0 success, 2 if a required DB is missing.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import bootstrap_archetype_payoff_fixture as _base

from mtg_synergy_graph.bench.cohorts import aristocrats

REPO_ROOT = _base.REPO_ROOT
OUTPUT_PATH = REPO_ROOT / "tests" / "fixtures" / "golden_set_aristocrats.json"


def main() -> int:
    return _base.main(cohort_fn=aristocrats, output_path=OUTPUT_PATH)


if __name__ == "__main__":
    raise SystemExit(main())
