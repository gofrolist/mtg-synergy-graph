"""Pre-flight gate stack for the gap_report -> scaffold -> audit loop.

v1.0 ships Stage A only: a deterministic golden-coverage prefilter that
answers "could this candidate rule even be tested?" before any generator
code is written.

See docs/plans/2026-05-02-001-feat-preflight-gate-stack-plan.md.

Stages B and C (paper-rule simulator + embedding-shape prior) are
deferred to v1.5; the library shape here is forward-compatible for them
to slot in as additional gates without consumer-side changes.
"""

from .pipeline import evaluate_all, evaluate_one
from .types import Candidate, GateVerdict, PipelineVerdict, Severity

__all__ = [
    "Candidate",
    "GateVerdict",
    "PipelineVerdict",
    "Severity",
    "evaluate_all",
    "evaluate_one",
]
