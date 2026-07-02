#!/usr/bin/env python3
"""R0 portfolio-selection simulation CLI (plan 2026-07-02-004, Unit 2).

Thin wrapper around :mod:`mtg_synergy_graph.bench.portfolio_sim`. See
that module for the subcommands (``bands`` / ``share`` / ``sweep``) and
the plan at ``docs/plans/2026-07-02-004-feat-portfolio-selection-plan.md``.
"""

from __future__ import annotations

import sys

from mtg_synergy_graph.bench.portfolio_sim import main

if __name__ == "__main__":
    sys.exit(main())
