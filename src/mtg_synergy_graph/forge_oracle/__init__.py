"""Forge-Second-Oracle — design-time signals from Forge's AI + precons.

This package is offline infrastructure. It is NEVER imported by
``engine.py``, ``universal_scorer.py``, ``graph_engine.py``,
``complement_rules/*``, or ``scripts/recommend.py``. Enforced by
``tests/test_forge_oracle_isolation.py`` (added in plan unit 9).

Feeds:
- ``scripts/gap_report.py`` (soft read, graceful fallback when sidecar
  DB is missing)
- ``scripts/bench.py audit --vs-forge-oracle`` (strict read)
- ``scripts/forge_oracle.py {build, inspect, propose-rules, upgrade}``

Plan: ``docs/plans/2026-04-23-002-feat-forge-second-oracle-plan.md``.
"""

from __future__ import annotations
