#!/usr/bin/env python3
"""Activation-poverty instrument CLI (coverage census / queue / gate).

Thin wrapper around :mod:`mtg_synergy_graph.bench.coverage_report`. See that
module and
``docs/superpowers/specs/2026-07-08-activation-poverty-instrument-design.md``.
"""

from __future__ import annotations

import sys

from mtg_synergy_graph.bench.coverage_report import main

if __name__ == "__main__":
    sys.exit(main())
