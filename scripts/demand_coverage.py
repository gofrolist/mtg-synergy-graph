#!/usr/bin/env python3
"""Stage 0 demand-coverage instrument CLI (plan 2026-07-02-005, Unit 2).

Thin wrapper around :mod:`mtg_synergy_graph.bench.demand_coverage`. See
that module for the classification predicates, exit-code taxonomy, and
the plan at ``docs/plans/2026-07-02-005-feat-resource-flow-demand-plan.md``.
"""

from __future__ import annotations

import sys

from mtg_synergy_graph.bench.demand_coverage import main

if __name__ == "__main__":
    sys.exit(main())
