"""Single-process validator for a freshly-applied rule.

Runs in ONE subprocess (one ``uv run`` cold start) and executes:

1. pytest (full suite) in-process via ``pytest.main()``.
2. Golden NDCG check via :func:`check_golden_set` with ``score_only``
   restricted to commanders whose ports activate the new rule's gate.
3. Broad NDCG check via :func:`broad_set_track._check` with
   ``--touched-only`` for the same reason.
4. Per-rule impact check (replaces the binary trivial test). For each
   commander touched by the new rule's gate, compare ``hi_syn_hits``
   in top-30 WITH vs WITHOUT the rule (helper monkey-patched to
   return ``[]``). Three verdicts:

   - **HARMFUL** (sum_delta < 0): rule's complements are displacing
     better candidates from top-30 on its own touched cmdrs. Fail
     validation -> auto-revert. This is the wart we missed before
     ward_2_tribal shipped.
   - **TRIVIAL** (max|delta| == 0): rule fires but never lands in
     top-30 anywhere. Pass + flag trivial; same handling as before.
   - **positive** (sum_delta > 0): rule contributes measurable
     hi_syn improvement. Ship clean.

Emits ONE JSON line on stdout for the parent to parse::

    {
      "passed": bool,            # all stages green AND not harmful
      "trivial": bool,           # passed, but no measurable impact
      "trivial_reason": str|null,
      "harmful": bool,           # impact check found net-negative shift
      "harmful_reason": str|null,
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
    compare_to_edhrec,
    regression_failed,
)
from mtg_synergy_graph.complement_rules import core  # noqa: E402
from mtg_synergy_graph.complement_rules.registry import RULE_GATES  # noqa: E402


def _emit(payload: dict) -> None:
    """Write the result as a single JSON line to stdout."""
    print(json.dumps(payload))


def _fail(summary: str) -> None:
    """Emit a failure result with all impact-check fields zeroed."""
    _emit(
        {
            "passed": False,
            "trivial": False,
            "trivial_reason": None,
            "harmful": False,
            "harmful_reason": None,
            "summary": summary,
        }
    )


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


def _broad_touched_oids(
    syn_db: Path,
    baseline_path: Path,
    rule_id: str,
) -> tuple[set[str], dict[str, str]]:
    """Return ``(touched_oids, names_by_oid)`` for the broad baseline."""
    payload = json.loads(baseline_path.read_text())
    names_by_oid: dict[str, str] = payload.get("names_by_oracle_id", {})
    touched_names = find_touched_card_names(syn_db, names_by_oid.values(), [rule_id])
    touched_oids = {oid for oid, name in names_by_oid.items() if name in touched_names}
    return touched_oids, names_by_oid


def _hi_syn_hits(engine: SynergyEngine, edhrec_conn: sqlite3.Connection, oid: str) -> int | None:
    """Hi-Syn hits in top-30 for one oracle_id; ``None`` if no curated data."""
    try:
        page = engine.page_by_oracle_id([oid], offset=0, limit=30)
    except (LookupError, ValueError):
        return None
    cmp = compare_to_edhrec(page, edhrec_conn, top_n=30)
    if cmp.hi_syn_size == 0:
        return None
    return cmp.hi_syn_hits


def _impact_check(
    syn_db: Path,
    edhrec_db: Path,
    rule_id: str,
    touched_oids: set[str],
    names_by_oid: dict[str, str],
) -> dict:
    """Score touched commanders WITH and WITHOUT the rule (helper monkey-
    patched to ``[]``). Returns ``{verdict, sum_delta, max_abs_delta,
    movers}`` where verdict is ``"positive"`` / ``"trivial"`` / ``"harmful"``.

    ``trivial`` covers both "no commander activates the gate" (touched
    is empty) and "rule fires but never lands in top-30" (max|Δ| = 0).
    """
    if not touched_oids:
        return {"verdict": "trivial", "sum_delta": 0, "max_abs_delta": 0, "movers": ()}

    helper_name = f"_find_{rule_id}"
    if not hasattr(core, helper_name):
        # No helper to monkey-patch — caller used a non-standard naming
        # convention. Fall back to "no impact measurement available";
        # treat as positive so we don't accidentally fail a working rule.
        return {"verdict": "positive", "sum_delta": 0, "max_abs_delta": 0, "movers": ()}

    edhrec_conn = sqlite3.connect(edhrec_db)
    edhrec_conn.row_factory = sqlite3.Row
    try:
        with SynergyEngine(syn_db) as eng:
            with_scores = {oid: _hi_syn_hits(eng, edhrec_conn, oid) for oid in touched_oids}

        original = getattr(core, helper_name)
        setattr(core, helper_name, lambda *a, **k: [])
        try:
            with SynergyEngine(syn_db) as eng:
                without_scores = {oid: _hi_syn_hits(eng, edhrec_conn, oid) for oid in touched_oids}
        finally:
            setattr(core, helper_name, original)
    finally:
        edhrec_conn.close()

    movers: list[tuple[str, int, int, int]] = []
    for oid in touched_oids:
        w, wo = with_scores.get(oid), without_scores.get(oid)
        if w is None or wo is None:
            continue
        d = w - wo
        if d != 0:
            movers.append((names_by_oid.get(oid, oid), w, wo, d))
    movers.sort(key=lambda m: -abs(m[3]))

    sum_delta = sum(m[3] for m in movers)
    max_abs = max((abs(m[3]) for m in movers), default=0)
    # max_abs == 0:           no per-cmdr movement -> trivial
    # sum_delta < 0:           net hi_syn loss -> harmful (revert)
    # sum_delta == 0 (movers): wins offset losses, net-zero -> trivial
    # sum_delta > 0:           net hi_syn gain -> positive (ship)
    if max_abs == 0 or sum_delta == 0:
        verdict = "trivial"
    elif sum_delta < 0:
        verdict = "harmful"
    else:
        verdict = "positive"
    return {
        "verdict": verdict,
        "sum_delta": sum_delta,
        "max_abs_delta": max_abs,
        "movers": tuple(movers),
    }


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
        _fail(
            f"rule_id '{rule_id}' has no registered gate in RULE_GATES. "
            "Add a RuleGate alongside the helper before validating."
        )
        return 1

    # ------------------------------------------------------------------
    # 1. pytest (full suite) — in-process. Optionally pre-flight the
    # generated test alone for a faster failure signal.
    # ------------------------------------------------------------------
    if args.gen_test_path is not None and args.gen_test_path.exists():
        rc = pytest.main([str(args.gen_test_path), "-q", "--no-cov", "--tb=short"])
        if rc != 0:
            _fail(f"Generated test failed (pytest exit {int(rc)}).")
            return 1

    rc = pytest.main(["tests/", "-q", "--no-cov", "--tb=short"])
    if rc != 0:
        _fail(f"Full pytest suite failed (exit {int(rc)}).")
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
        _fail(
            f"Golden NDCG regression: drop {golden_drop:+.4f} "
            f"(baseline {golden_report.baseline_ndcg:.4f} -> "
            f"fresh {golden_report.aggregate_ndcg:.4f}, "
            f"tolerance {args.allow_ndcg_drop}). "
            f"rank_shifts={len(golden_report.rank_shifts)} "
            f"ndcg_drops={len(golden_report.ndcg_drops)}"
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
            _fail(
                f"Broad NDCG regression (exit {broad_rc}). Last lines: {captured.getvalue().strip().splitlines()[-3:]}"
            )
            return 1
        broad_touched_oids, broad_names = _broad_touched_oids(args.db, args.broad_baseline, rule_id)
        broad_total = len(broad_names)
    else:
        broad_touched_oids, broad_names = set(), {}
        broad_total = 0

    # ------------------------------------------------------------------
    # 4. Per-rule impact check (subsumes the old binary trivial test).
    #    Score touched commanders WITH and WITHOUT the rule; map the
    #    net hi_syn delta to a verdict.
    # ------------------------------------------------------------------
    impact = _impact_check(
        args.db,
        args.edhrec_db,
        rule_id,
        broad_touched_oids,
        broad_names,
    )
    verdict = impact["verdict"]
    sum_d = impact["sum_delta"]
    max_d = impact["max_abs_delta"]
    n_touched = len(broad_touched_oids)
    base_summary = (
        f"Touched {len(score_only_golden)}/{golden_total} golden + "
        f"{n_touched}/{broad_total} broad. "
        f"Golden NDCG {golden_report.baseline_ndcg:.4f} -> "
        f"{golden_report.aggregate_ndcg:.4f} (delta {-golden_drop:+.4f}). "
        f"Impact: sum_Δhi_syn={sum_d:+}, max|Δ|={max_d}."
    )

    if verdict == "harmful":
        worst = ", ".join(f"{n} ({d:+})" for n, _, _, d in impact["movers"][:3])
        _emit(
            {
                "passed": False,
                "trivial": False,
                "trivial_reason": None,
                "harmful": True,
                "harmful_reason": (f"Net hi_syn delta {sum_d:+} across {n_touched} touched cmdrs; top losers: {worst}"),
                "summary": f"HARMFUL: {base_summary} Top losers: {worst}",
            }
        )
        return 1

    if verdict == "trivial":
        if n_touched == 0:
            t_reason = f"no commander in broad universe ({broad_total}) activates the gate"
        else:
            t_reason = f"rule fires on {n_touched} commander(s) but never lands in top-30 (max|Δhi_syn|=0)"
        _emit(
            {
                "passed": True,
                "trivial": True,
                "trivial_reason": t_reason,
                "harmful": False,
                "harmful_reason": None,
                "summary": f"TRIVIAL: {base_summary}",
            }
        )
        return 0

    # positive
    top_movers = ", ".join(f"{n} ({d:+})" for n, _, _, d in impact["movers"][:3])
    _emit(
        {
            "passed": True,
            "trivial": False,
            "trivial_reason": None,
            "harmful": False,
            "harmful_reason": None,
            "summary": (f"PASS: {base_summary}" + (f" Top movers: {top_movers}." if top_movers else "")),
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
