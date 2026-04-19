"""Single-process validator for a freshly-applied rule.

Runs in ONE subprocess (one ``uv run`` cold start) and executes:

1. pytest (full suite) in-process via ``pytest.main()``.
2. Golden NDCG check via :func:`check_golden_set` with ``score_only``
   restricted to commanders whose ports activate the new rule's gate.
3. Broad NDCG check via :func:`broad_set_track._check` with
   ``--touched-only`` for the same reason.
4. Trivial-impact check: did the rule's gate activate on ANY commander
   in the validation universe? If not, the rule's contribution is
   invisible to our harness — we have no evidence it helps anything.

Emits ONE JSON line on stdout for the parent to parse::

    {
      "passed": bool,            # all stages green
      "trivial": bool,           # rule has no measurable impact in our universe
      "trivial_reason": str|null,
      "summary": str             # one-paragraph human narrative
    }

Replaces the four ``uv run`` subprocess calls in ``scaffold_rule._validate``
with a single invocation, eliminating ~4.5s of cold-start overhead per
attempt.
"""

from __future__ import annotations

import argparse
import io
import json
import sqlite3
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import broad_set_track  # noqa: E402
import pytest  # noqa: E402
from _touched_commanders import find_touched_card_names  # noqa: E402

from mtg_synergy_graph import (  # noqa: E402
    SynergyEngine,
    check_golden_set,
    regression_failed,
)
from mtg_synergy_graph.complement_rules.registry import RULE_GATES  # noqa: E402


def _emit(payload: dict) -> None:
    """Write the result as a single JSON line to stdout."""
    print(json.dumps(payload))


def _golden_score_only(
    syn_db: Path,
    baseline_path: Path,
    rule_id: str,
) -> tuple[set[str], int]:
    """Return ``(score_only_set, total_baseline_count)`` for the
    golden baseline. The set holds canonical commander names whose
    any partner-name activates the new rule's gate.
    """
    payload = json.loads(baseline_path.read_text())
    names = [e["commander"] for e in payload.get("entries", [])]
    partner_names = {p for n in names for p in n.split(" + ")}
    touched_partners = find_touched_card_names(syn_db, partner_names, [rule_id])
    score_only = {n for n in names if any(p in touched_partners for p in n.split(" + "))}
    return score_only, len(names)


def _broad_touched_count(
    syn_db: Path,
    baseline_path: Path,
    rule_id: str,
) -> tuple[int, int]:
    """Return ``(touched_count, total_baseline_count)`` for the broad baseline."""
    payload = json.loads(baseline_path.read_text())
    names_by_oid: dict[str, str] = payload.get("names_by_oracle_id", {})
    touched = find_touched_card_names(syn_db, names_by_oid.values(), [rule_id])
    return len(touched), len(names_by_oid)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rule-id", required=True, help="rule_id of the freshly-applied rule")
    parser.add_argument("--db", type=Path, default=Path("data/synergy.db"))
    parser.add_argument("--edhrec-db", type=Path, default=Path("data/tags.db"))
    parser.add_argument(
        "--golden-baseline",
        type=Path,
        default=Path("tests/fixtures/golden_set_run.json"),
    )
    parser.add_argument(
        "--broad-baseline",
        type=Path,
        default=Path("tests/fixtures/full_set_baseline.json"),
    )
    parser.add_argument("--allow-ndcg-drop", type=float, default=0.0)
    parser.add_argument(
        "--gen-test-path",
        type=Path,
        help="optional generated test file to run first as a fast smoke check",
    )
    args = parser.parse_args()

    rule_id = args.rule_id

    # Safety guard: gate must be registered. Otherwise `score_only`
    # would be empty, golden+broad would inherit baseline for everyone,
    # AND the trivial check would silently fire — we'd ship a rule the
    # harness can't see at all.
    registered = {g.rule_id for g in RULE_GATES}
    if rule_id not in registered:
        _emit(
            {
                "passed": False,
                "trivial": False,
                "trivial_reason": None,
                "summary": (
                    f"rule_id '{rule_id}' has no registered gate in RULE_GATES. "
                    "Add a RuleGate alongside the helper before validating."
                ),
            }
        )
        return 1

    # ------------------------------------------------------------------
    # 1. pytest (full suite) — in-process. Optionally pre-flight the
    # generated test alone for a faster failure signal.
    # ------------------------------------------------------------------
    if args.gen_test_path is not None and args.gen_test_path.exists():
        rc = pytest.main([str(args.gen_test_path), "-q", "--no-cov", "--tb=short"])
        if rc != 0:
            _emit(
                {
                    "passed": False,
                    "trivial": False,
                    "trivial_reason": None,
                    "summary": f"Generated test failed (pytest exit {int(rc)}).",
                }
            )
            return 1

    rc = pytest.main(["tests/", "-q", "--no-cov", "--tb=short"])
    if rc != 0:
        _emit(
            {
                "passed": False,
                "trivial": False,
                "trivial_reason": None,
                "summary": f"Full pytest suite failed (exit {int(rc)}).",
            }
        )
        return 1

    # ------------------------------------------------------------------
    # 2. Golden NDCG (touched-only).
    # ------------------------------------------------------------------
    score_only_golden, golden_total = _golden_score_only(args.db, args.golden_baseline, rule_id)

    edhrec_conn = sqlite3.connect(args.edhrec_db)
    edhrec_conn.row_factory = sqlite3.Row
    try:
        with SynergyEngine(args.db) as engine:
            golden_report = check_golden_set(
                engine,
                args.golden_baseline,
                edhrec_conn=edhrec_conn,
                edhrec_db_path=args.edhrec_db,
                score_only=score_only_golden,
                ndcg_tolerance=args.allow_ndcg_drop,
            )
    finally:
        edhrec_conn.close()

    golden_drop = golden_report.baseline_ndcg - golden_report.aggregate_ndcg
    if regression_failed(golden_report, ndcg_tolerance=args.allow_ndcg_drop):
        _emit(
            {
                "passed": False,
                "trivial": False,
                "trivial_reason": None,
                "summary": (
                    f"Golden NDCG regression: drop {golden_drop:+.4f} "
                    f"(baseline {golden_report.baseline_ndcg:.4f} -> "
                    f"fresh {golden_report.aggregate_ndcg:.4f}, "
                    f"tolerance {args.allow_ndcg_drop}). "
                    f"rank_shifts={len(golden_report.rank_shifts)} "
                    f"ndcg_drops={len(golden_report.ndcg_drops)}"
                ),
            }
        )
        return 1

    # ------------------------------------------------------------------
    # 3. Broad NDCG (touched-only).
    # ------------------------------------------------------------------
    if args.broad_baseline.exists():
        # _check prints to stderr; capture and replay only on FAIL to
        # keep the parent's output clean on the happy path.
        captured = io.StringIO()
        with redirect_stderr(captured), redirect_stdout(captured):
            broad_rc = broad_set_track._check(
                args.db,
                args.edhrec_db,
                args.broad_baseline,
                aggregate_tolerance=0.001,
                per_commander_tolerance=0.05,
                touched_only=[rule_id],
            )
        if broad_rc != 0:
            sys.stderr.write(captured.getvalue())
            _emit(
                {
                    "passed": False,
                    "trivial": False,
                    "trivial_reason": None,
                    "summary": (
                        f"Broad NDCG regression (exit {broad_rc}). "
                        f"Last lines: {captured.getvalue().strip().splitlines()[-3:]}"
                    ),
                }
            )
            return 1
        broad_touched, broad_total = _broad_touched_count(args.db, args.broad_baseline, rule_id)
    else:
        broad_touched, broad_total = 0, 0

    # ------------------------------------------------------------------
    # 4. Trivial-impact check.
    # ------------------------------------------------------------------
    total_touched = len(score_only_golden) + broad_touched
    universe_size = golden_total + broad_total
    trivial = total_touched == 0
    trivial_reason = (
        f"no commander in golden ({golden_total}) or broad ({broad_total}) "
        f"sample activates the new gate; rule's contribution is invisible "
        f"to the validation harness. Either the gate is too narrow or the "
        f"validation universe doesn't cover the affected archetype."
        if trivial
        else None
    )

    _emit(
        {
            "passed": True,
            "trivial": trivial,
            "trivial_reason": trivial_reason,
            "summary": (
                f"All checks passed. Touched {len(score_only_golden)}/{golden_total} golden + "
                f"{broad_touched}/{broad_total} broad = {total_touched}/{universe_size} commanders. "
                f"Golden NDCG {golden_report.baseline_ndcg:.4f} -> {golden_report.aggregate_ndcg:.4f} "
                f"(delta {-golden_drop:+.4f})."
            ),
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
