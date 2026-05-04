"""Shared types for the pre-flight pipeline.

Frozen dataclasses; pure values. The pipeline orchestrator and individual
gates exchange these without sharing mutable state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Severity(Enum):
    """Verdict severity tiers — match rule_quality_gate.py exit-code convention.

    Maps to exit codes 0/1/2 when the pipeline is invoked from a CLI.
    """

    PASS = 0
    WARN = 1
    REJECT = 2

    def __ge__(self, other: object) -> bool:
        if not isinstance(other, Severity):
            return NotImplemented
        return self.value >= other.value


@dataclass(frozen=True)
class Candidate:
    """A proposed-rule candidate to evaluate.

    For v1.0 Stage A only the ``signature`` is consumed. Additional fields
    are optional v1.5-forward-compatible knobs (left as Optional/None for
    now so v1.5 can extend without breaking existing call sites).

    Attributes:
        signature: ``(port_type, event_class, sub_discriminator)`` triple
            as emitted by ``scripts.gap_report._port_signature``. The
            sub_discriminator may be empty, a replacement_result, a
            ``"zone_origin->zone_destination"`` string, or a notable
            valid_filter qualifier token.
        gap_id: Optional human-readable identifier (typically
            ``"<port_type>.<event_class>[<sub>]"``) used for logging,
            override CSV rows, and gap_report.md emission. Defaults to
            an empty string; populated by ``Candidate.from_signature``.
    """

    signature: tuple[str, str, str]
    gap_id: str = ""


@dataclass(frozen=True)
class GateVerdict:
    """One gate's verdict for one candidate.

    ``name`` identifies which gate produced this verdict (e.g.
    ``"stage_a"``); ``reason`` is a short human-readable string suitable
    for display in gap_report.md and for the override CSV's verdict
    column.
    """

    name: str
    severity: Severity
    reason: str


@dataclass(frozen=True)
class PipelineVerdict:
    """Combined verdict for a candidate across all active gates.

    ``severity`` is the maximum across ``gates``. ``gates`` is the
    full per-gate verdict list in invocation order (in v1.0 this is
    just ``[stage_a_verdict]``).
    """

    candidate: Candidate
    severity: Severity
    gates: tuple[GateVerdict, ...] = field(default_factory=tuple)

    @property
    def reason(self) -> str:
        """One-line reason combining all non-PASS gate reasons.

        Returns ``"PASS"`` if every gate passed; otherwise the
        concatenation of non-PASS gate reasons separated by ``" | "``.
        """
        parts = [g.reason for g in self.gates if g.severity is not Severity.PASS]
        return " | ".join(parts) if parts else "PASS"
