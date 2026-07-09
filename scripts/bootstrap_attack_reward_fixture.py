"""One-shot bootstrap: build the attack-reward cohort fixture.

Thin entry point over ``scripts/bootstrap_archetype_payoff_fixture.py``'s
parameterized build/pin protocol: selects ``bench.cohorts.attack_reward``
(legal legendary-creature commanders with a team-benefiting attack-reward
trigger — see that function's docstring), filters to those with at least one
``High Synergy Cards`` row in EDHREC's tags.db, and pins their top-N scores to
``tests/fixtures/golden_set_attack_reward.json``.

Evaluation instrument, zero scoring-path impact. Re-run after cardsfolder
imports / scoring-config changes; the fixture carries its own ``config_hash``
and is enforced by ``tests/bench/test_fixture_freshness.py``.

Exit codes mirror the base module: 0 success, 2 if a required DB is missing.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make src/ importable when run as a bare script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

# scripts/ is on sys.path so this resolves to the sibling module.
import bootstrap_archetype_payoff_fixture as _base

from mtg_synergy_graph.bench.cohorts import attack_reward

REPO_ROOT = _base.REPO_ROOT
OUTPUT_PATH = REPO_ROOT / "tests" / "fixtures" / "golden_set_attack_reward.json"


def main() -> int:
    return _base.main(cohort_fn=attack_reward, output_path=OUTPUT_PATH)


if __name__ == "__main__":
    raise SystemExit(main())
