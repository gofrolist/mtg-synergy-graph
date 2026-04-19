"""Append-only log of rule scaffold attempts.

Tracks every ``scaffold_rule.py --apply`` invocation outcome
(passed / reverted / skipped) so the system has a learning loop:
repeated failures don't get re-attempted, and the auditor can
annotate proposals with prior-failure context.

Storage: ``docs/rule_attempts.jsonl`` (append-only JSONL — one
record per line, easy to grep, easy for humans + tools to read).

Schema (one record):

  ``timestamp``   ISO-8601 UTC timestamp.
  ``rule_id``     The rule's identifier (e.g. ``creature_count_scaler``).
  ``template``    The template name from the gap_report catalog.
  ``signature``   ``(port_type, event_class, sub_discriminator)`` triple
                  identifying the gap the rule targeted.
  ``outcome``     One of ``passed`` (validation succeeded, kept),
                  ``reverted`` (validation failed, restored), or
                  ``skipped`` (refused to retry a known-bad).
  ``reason``      Free-text summary — for ``reverted`` includes test
                  failure or NDCG drop details; for ``skipped`` references
                  the prior attempt's reason.
  ``files_touched``       Files the apply step would have written.
  ``validation_summary``  Optional dict with structured validation data
                          (NDCG numbers, test counts).

Reads: any caller can ``load_attempts()`` to get the full history.
The auditor uses ``prior_attempts_for_template()`` to decorate
proposals; the scaffolder uses ``is_known_bad()`` to refuse to
retry the same (template, rule_id) without ``--force``.
"""

from __future__ import annotations

import datetime
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

LOG_PATH = Path(__file__).parent.parent / "docs" / "rule_attempts.jsonl"


@dataclass
class AttemptRecord:
    """One scaffold attempt's outcome — append-only by convention."""

    timestamp: str
    rule_id: str
    template: str
    signature: tuple[str, str, str]
    outcome: str
    reason: str
    files_touched: tuple[str, ...] = ()
    validation_summary: dict = field(default_factory=dict)


def now_iso() -> str:
    """Current UTC time as ISO-8601, second precision."""
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")


def record_attempt(record: AttemptRecord) -> None:
    """Append ``record`` as a JSON line. Creates the log file + parent
    directory if needed.
    """
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = asdict(record)
    # JSON doesn't have tuples — list serialization is canonical.
    payload["signature"] = list(record.signature)
    payload["files_touched"] = list(record.files_touched)
    with LOG_PATH.open("a") as f:
        f.write(json.dumps(payload) + "\n")


def load_attempts() -> list[AttemptRecord]:
    """Read every attempt record from the log. Returns ``[]`` if the
    file doesn't exist yet.
    """
    if not LOG_PATH.exists():
        return []
    out: list[AttemptRecord] = []
    with LOG_PATH.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            data = json.loads(line)
            data["signature"] = tuple(data.get("signature", ("", "", "")))
            data["files_touched"] = tuple(data.get("files_touched", ()))
            data.setdefault("validation_summary", {})
            out.append(AttemptRecord(**data))
    return out


def prior_attempts_for_template(template: str) -> list[AttemptRecord]:
    """All past attempts whose template name matches ``template``."""
    return [a for a in load_attempts() if a.template == template]


def is_known_bad(template: str, rule_id: str) -> tuple[bool, str | None]:
    """True iff a prior attempt with the same (template, rule_id) was
    reverted. Returns ``(blocked, prior_reason)``.

    Used by the scaffolder to refuse to re-try known-failing
    combinations without ``--force``. Only ``reverted`` outcomes
    block; ``skipped`` ones don't (those are themselves a refusal,
    not a fresh attempt).
    """
    for a in load_attempts():
        if a.template == template and a.rule_id == rule_id and a.outcome == "reverted":
            return (True, a.reason)
    return (False, None)
