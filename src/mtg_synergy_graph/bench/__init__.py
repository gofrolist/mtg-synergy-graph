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
from mtg_synergy_graph.bench.audit import handle_audit, run_audit
from mtg_synergy_graph.bench.cli import main
from mtg_synergy_graph.bench.embedding_dedup_handler import handle_embedding_dedup
from mtg_synergy_graph.bench.fixture import (
    FixtureEntry,
    PinnedFixture,
    ScoreDelta,
    TensorRow,
    build_fixture,
    score_commander,
)
from mtg_synergy_graph.bench.forensics_history import handle_trend_forensics
from mtg_synergy_graph.bench.forensics_report import handle_forensics
from mtg_synergy_graph.bench.forge_oracle_handler import handle_vs_forge_oracle
from mtg_synergy_graph.bench.handlers import (
    handle_collinearity,
    handle_expect_identity,
    handle_inspect,
    handle_inspect_gems,
    handle_repin,
    handle_rule,
    handle_trend_hidden_gems,
    handle_unknowns,
)
from mtg_synergy_graph.bench.histogram import Bucket, Histogram, Verdict
from mtg_synergy_graph.bench.optimize import handle_optimize
from mtg_synergy_graph.bench.per_commander_ndcg import handle_per_commander_ndcg
from mtg_synergy_graph.bench.report import AuditReport, build_report
from mtg_synergy_graph.bench.tensor import (
    TensorWriter,
    compute_config_hash,
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
# Plan 003 Unit 6 — UNKNOWN port_nodes reporter.
_cli.register("unknowns", handle_unknowns)
# Hidden-gem plan Unit 4 — per-commander diff of hidden-gem sets
# between pinned and live.
_cli.register("inspect_gems", handle_inspect_gems)
# Hidden-gem plan Unit 5 — print last N rows of .audit/history.csv.
_cli.register("trend", handle_trend_hidden_gems)
# Plan 002 Unit 7 — Kendall τ sidecar: compare our top-N to Forge
# CardRanker's ranking over the same candidate set.
_cli.register("vs_forge_oracle", handle_vs_forge_oracle)
# Plan 003 (content-embeddings) Unit 6 — rule-pair embedding dedup
# diagnostic. Flags rule pairs whose candidate-activation sets are
# near-parallel in embedding space.
_cli.register("embedding_dedup", handle_embedding_dedup)
# Plan 2026-04-26-001 Unit 4 — Coordinate Ascent weight optimizer.
_cli.register("optimize", handle_optimize)
# BM25 IDF probe (plan 2026-05-04-001) Unit 1 — per-commander NDCG@30
# diff handler. Required by the BM25 probe's per-commander prerequisite
# gate; general-purpose audit infra that survives the probe outcome.
_cli.register("per_commander_ndcg", handle_per_commander_ndcg)
# Divergence forensics (plan 2026-06-10-001 Unit 4) — per-miss failure
# taxonomy + metric sidecars + justified-divergence view. Read-only.
_cli.register("forensics", handle_forensics)
# Plan 2026-06-10-001 Unit 5 — ``--trend forensics`` reader over the
# sibling forensics history CSV with (config_hash, snapshot) boundary
# markers.
_cli.register("trend_forensics", handle_trend_forensics)

__all__ = [
    "AuditReport",
    "Bucket",
    "FixtureEntry",
    "Histogram",
    "PinnedFixture",
    "ScoreDelta",
    "TensorRow",
    "TensorWriter",
    "Verdict",
    "build_fixture",
    "build_report",
    "compute_config_hash",
    "main",
    "run_audit",
    "score_commander",
]
