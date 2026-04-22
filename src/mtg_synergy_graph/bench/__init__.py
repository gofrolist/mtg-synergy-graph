"""Unified eval harness for the MTG Synergy Graph scoring path.

Replaces the separate scripts (_audit_rule_impact.py, golden_set_track.py,
compare_edhrec.py, weight_grid_search.py, broad_set_track.py) with one
bench.py CLI backed by a persisted rule-contribution tensor. See
docs/plans/2026-04-22-001-feat-unified-eval-harness-plan.md for the
plan and docs/brainstorms/2026-04-21-unified-eval-harness-requirements.md
for the origin requirements.
"""

from __future__ import annotations

from mtg_synergy_graph.bench import cli as _cli
from mtg_synergy_graph.bench.cli import main
from mtg_synergy_graph.bench.handlers import handle_expect_identity, handle_repin

# Unit 3: wire the fixture-repin and expect-identity handlers into the
# dispatch table. Units 4-6 will add their own register() calls the same
# way so cli.py stays unaware of implementations.
_cli.register("repin", handle_repin)
_cli.register("expect_identity", handle_expect_identity)

__all__ = ["main"]
