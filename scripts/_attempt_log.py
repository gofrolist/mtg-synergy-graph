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


_BLOCKING_OUTCOMES: frozenset[str] = frozenset({"reverted", "trivial"})

#: Templates whose pass rate falls at or below this threshold AND have
#: at least :data:`_TEMPLATE_BLOCK_MIN_FRESH` fresh attempts get blocked
#: at the template level — every (template, rule_id) it generates will
#: skip without ``--force``. Catches templates whose generator has a
#: systemic flaw (not just one bad rule_id).
_TEMPLATE_BLOCK_RATE: float = 0.34
_TEMPLATE_BLOCK_MIN_FRESH: int = 2


def template_stats() -> dict[str, dict[str, int]]:
    """Per-template outcome counts. Outer key = template name, inner
    keys = ``passed`` / ``trivial`` / ``reverted`` / ``skipped`` (only
    keys with non-zero counts are present).
    """
    out: dict[str, dict[str, int]] = {}
    for a in load_attempts():
        bucket = out.setdefault(a.template, {})
        bucket[a.outcome] = bucket.get(a.outcome, 0) + 1
    return out


def template_pass_rate(template: str) -> tuple[float, int]:
    """Laplace-smoothed pass rate + ``fresh`` attempt count for one
    template. Returns ``((passed + 1) / (fresh + 2), fresh)`` where
    ``fresh = passed + trivial + reverted``.

    Smoothing rationale:
    - templates with no history get a neutral 0.5 (give them a chance);
    - one trivial doesn't immediately blacklist a template;
    - at scale, converges to the true rate.

    ``skipped`` outcomes don't count — they're refusals, not new tries.
    """
    stats = template_stats().get(template, {})
    passed = stats.get("passed", 0)
    trivial = stats.get("trivial", 0)
    reverted = stats.get("reverted", 0)
    fresh = passed + trivial + reverted
    return ((passed + 1) / (fresh + 2), fresh)


def is_template_blocked(template: str) -> tuple[bool, str | None]:
    """True iff the template has a low enough pass rate to skip outright.

    Threshold: ``rate <= _TEMPLATE_BLOCK_RATE`` AND
    ``fresh >= _TEMPLATE_BLOCK_MIN_FRESH``. Returns
    ``(blocked, reason)`` so the caller can surface the block in logs.
    """
    rate, fresh = template_pass_rate(template)
    if fresh < _TEMPLATE_BLOCK_MIN_FRESH:
        return (False, None)
    if rate > _TEMPLATE_BLOCK_RATE:
        return (False, None)
    stats = template_stats().get(template, {})
    return (
        True,
        (
            f"template pass rate {rate:.2f} (passed={stats.get('passed', 0)}, "
            f"trivial={stats.get('trivial', 0)}, reverted={stats.get('reverted', 0)}) "
            f"<= {_TEMPLATE_BLOCK_RATE} threshold"
        ),
    )


def is_known_bad(template: str, rule_id: str) -> tuple[bool, str | None]:
    """True iff a prior attempt with the same (template, rule_id) had
    a blocking outcome. Returns ``(blocked, prior_reason)``.

    Blocking outcomes:
    - ``reverted``: the apply step failed validation (regression, test
      failure, etc.). Same code → same failure.
    - ``trivial``: the apply step passed validation but the rule didn't
      activate on any commander in the validation universe. Same code
      → same no-op, no point re-shipping.

    ``skipped`` outcomes don't block (they're already a refusal, not a
    fresh attempt). ``passed`` doesn't block (caller wouldn't be
    re-attempting a working rule anyway).

    Used by the scaffolder to refuse to re-try known-failing
    combinations without ``--force``.
    """
    for a in load_attempts():
        if a.template == template and a.rule_id == rule_id and a.outcome in _BLOCKING_OUTCOMES:
            return (True, f"prior outcome={a.outcome}: {a.reason}")
    return (False, None)
