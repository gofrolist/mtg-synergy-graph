"""Unified eval harness CLI entry point.

Thin wrapper around :mod:`mtg_synergy_graph.bench.cli`. See that module
for the full dispatcher and the plan at
``docs/plans/2026-04-22-001-feat-unified-eval-harness-plan.md``.
"""

from __future__ import annotations

import sys

from mtg_synergy_graph.bench import main

if __name__ == "__main__":
    sys.exit(main())
