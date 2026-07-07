#!/usr/bin/env python3
"""CLI wrapper for the deck-context kill-test instrument (plan 2026-07-06-001)."""

from __future__ import annotations

import sys

from mtg_synergy_graph.bench.context_sim import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
