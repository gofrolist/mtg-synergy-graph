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
from mtg_synergy_graph.bench.audit import handle_audit
from mtg_synergy_graph.bench.cli import main
from mtg_synergy_graph.bench.handlers import (
    handle_collinearity,
    handle_expect_identity,
    handle_inspect,
    handle_repin,
    handle_rule,
)

# Unit 3: --repin, --expect-identity.
# Unit 4: main audit.
# Unit 6: --rule, --inspect, --collinearity.
_cli.register("audit", handle_audit)
_cli.register("repin", handle_repin)
_cli.register("expect_identity", handle_expect_identity)
_cli.register("rule", handle_rule)
_cli.register("inspect", handle_inspect)
_cli.register("collinearity", handle_collinearity)

__all__ = ["main"]
