"""Pre-commit hook entry point for ``bench-audit``.

Runs ``bench.py audit`` in advisory mode when the staged diff touches
scoring-path files. The hook always exits 0 so a HARMFUL verdict warns
but does not block the commit — the guardrail in
``memory/feedback_audit_every_change.md`` asks the human to review
HARMFUL, not to refuse to land anything.

Failure modes that DO exit non-zero (genuine bugs rather than scoring
drift): missing DB, malformed fixture, unexpected exceptions. These
bubble up so the author sees the real issue instead of a silent pass.

Environment:
* ``BENCH_DB``     — override the DB path (default: ``synergy.db``).
* ``BENCH_FIXTURE``— override the fixture path (default:
                    ``tests/fixtures/golden_set_run.json``).
* ``BENCH_FORMAT`` — ``md`` (default), ``json``, or ``csv``. ``csv`` is
                    only meaningful for ``bench.py audit --trend``; the
                    hook skips the ``.audit/last.*`` write when csv is
                    selected and emits a stderr warning.
"""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

from mtg_synergy_graph.bench.audit import run_audit
from mtg_synergy_graph.bench.histogram import Verdict


def main(argv: list[str] | None = None) -> int:
    db_path = Path(os.environ.get("BENCH_DB", "data/synergy.db"))
    fixture_path = Path(os.environ.get("BENCH_FIXTURE", "tests/fixtures/golden_set_run.json"))
    fmt = os.environ.get("BENCH_FORMAT", "md")

    if not db_path.exists():
        print(
            f"bench-audit: SKIP (DB {db_path} not found; run `uv run python scripts/import_cardsfolder.py`)",
            file=sys.stderr,
        )
        return 0
    if not fixture_path.exists():
        print(
            f"bench-audit: SKIP (pinned fixture {fixture_path} not found; "
            "run `uv run scripts/bench.py audit --repin --yes` to create one)",
            file=sys.stderr,
        )
        return 0

    try:
        report = run_audit(db_path, fixture_path)
    except Exception:
        print(
            "bench-audit: INTERNAL ERROR running audit. Traceback:",
            file=sys.stderr,
        )
        traceback.print_exc(file=sys.stderr)
        # Non-zero exit: this is a genuine bug, not a scoring drift.
        return 1

    # Write the report to .audit/last.{md,json} for consumption by the
    # developer after the commit. ``csv`` is meaningful only for
    # ``bench.py audit --trend`` — for the hook there is no sensible CSV
    # rendering of a one-shot audit report, so we skip the write and
    # warn instead of silently falling back to markdown.
    if fmt == "csv":
        print(
            "bench-audit: warning: BENCH_FORMAT=csv is meaningful only for "
            "`bench.py audit --trend`; skipping .audit/last.* write for this hook run.",
            file=sys.stderr,
        )
    else:
        audit_dir = Path(".audit")
        audit_dir.mkdir(exist_ok=True)
        suffix = "json" if fmt == "json" else "md"
        rendered = report.to_json() if fmt == "json" else report.to_markdown()
        (audit_dir / f"last.{suffix}").write_text(rendered, encoding="utf-8")

    # Emit a single summary line per policy — HARMFUL prints a banner.
    tag = "PASS" if report.is_identical else report.verdict.value
    summary = (
        f"bench-audit: {tag} "
        f"(Δ={report.aggregate_score_delta:+.4f}, "
        f"{report.commanders_compared} cmdrs, "
        f"histogram no_change={report.histogram.no_change})"
    )

    if report.verdict == Verdict.HARMFUL:
        print(f"⚠ {summary}", file=sys.stderr)
        print(
            "  Review .audit/last.md — a HARMFUL verdict means the change "
            "regressed scoring. Commit will proceed; consider reverting or "
            "running `uv run scripts/bench.py audit --repin --yes` if the "
            "drift is intentional.",
            file=sys.stderr,
        )
    else:
        print(summary, file=sys.stderr)

    # Always exit 0: the hook is advisory per Unit 7 FR5.
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
