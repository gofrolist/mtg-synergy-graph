"""Per-rule impact audit: measure each rule's actual NDCG contribution.

The trivial-detection check in ``_validate_rule.py`` only catches
rules that activate on **zero** commanders. It misses rules that
activate but produce no measurable score change in any touched
commander's top-30 — and rules that activate but *displace* better
candidates from the top-30 (net-negative impact).

This script closes that gap. For each rule:

1. Find commanders whose ports activate the rule's gate (touched).
2. Score those commanders WITH the rule (= current state).
3. Monkey-patch the helper to ``return []`` and re-score them.
4. Compare ``hi_syn_hits`` per commander; report the delta.

Verdicts:

  * ``TRIVIAL``  — no per-commander movement OR wins offset losses
    to net-zero. Helper fires but produces no useful net change.
  * ``HARMFUL``  — net Δ < 0 across touched commanders. Rule is
    actively displacing better candidates. Aggregate-NDCG
    validation can't catch this when the per-cmdr signal averages
    out across thousands of commanders.
  * ``MARGINAL`` — net Δ > 0 but ratio sum/touched < 0.1, i.e.
    fewer than one hi_syn improvement per ten touched commanders.
    Rule contributes something but barely above noise; consider
    whether the template is worth keeping.
  * ``positive`` — net Δ > 0 with ratio >= 0.1. Real contribution.

Usage::

    # Audit every generated rule.
    uv run python scripts/_audit_rule_impact.py

    # Audit specific rules.
    uv run python scripts/_audit_rule_impact.py --rule-id ward_1_tribal ward_2_tribal

Exits non-zero if any rule is HARMFUL (net Δ < 0), so this can run
periodically as a smoke check.

Caveats:

  * Caller must pass rules whose helper is named ``_find_<rule_id>``
    in ``mtg_synergy_graph.complement_rules.core``. All current
    generated rules follow this convention.
  * The "without rule" pass changes IDF bucket frequencies and the
    multi-rule bonus for candidates the rule formerly helped. So
    Δ measures the rule's contribution **including downstream
    effects**, which is exactly what we want for an impact audit.
  * Limited to commanders with EDHREC Hi-Syn data (the metric).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from _touched_commanders import find_touched_card_names  # noqa: E402

from mtg_synergy_graph import SynergyEngine, compare_to_edhrec  # noqa: E402
from mtg_synergy_graph.complement_rules import core  # noqa: E402

GENERATED_DIR = REPO_ROOT / "src" / "mtg_synergy_graph" / "complement_rules" / "generated"
GOLDEN_BASELINE = REPO_ROOT / "tests" / "fixtures" / "golden_set_run.json"
FULL_BASELINE = REPO_ROOT / "tests" / "fixtures" / "full_set_baseline.json"

#: Below this ratio of (sum_delta / touched_count), a positive rule is
#: classified MARGINAL: it contributes hi_syn improvement but barely
#: above noise (under one hi_syn lift per ten touched commanders).
_MARGINAL_RATIO: float = 0.1


def _all_generated_rules() -> list[str]:
    """Discover generated rule modules. Stem == rule_id by convention."""
    return sorted(p.stem for p in GENERATED_DIR.glob("*.py") if p.stem != "__init__")


def _name_to_oid(syn_db: Path) -> dict[str, str]:
    """Build the union name → oracle_id map across golden + full baselines."""
    out: dict[str, str] = {}

    # Full set is keyed by oracle_id.
    full = json.loads(FULL_BASELINE.read_text())
    for oid, name in full.get("names_by_oracle_id", {}).items():
        out[name] = oid

    # Golden is keyed by canonical (possibly partner-pair) name; resolve
    # each partner via the cards table.
    golden = json.loads(GOLDEN_BASELINE.read_text())
    partners = {p for e in golden.get("entries", []) for p in e["commander"].split(" + ")}
    missing = partners - out.keys()
    if missing:
        conn = sqlite3.connect(syn_db)
        conn.row_factory = sqlite3.Row
        try:
            for n in missing:
                row = conn.execute("SELECT oracle_id FROM cards WHERE name=? LIMIT 1", (n,)).fetchone()
                if row and row["oracle_id"]:
                    out[n] = row["oracle_id"]
        finally:
            conn.close()
    return out


def hi_syn_hits(engine: SynergyEngine, edhrec_conn: sqlite3.Connection, oid: str) -> int | None:
    """Hi-Syn hits in top-30 for one oracle_id. ``None`` if no curated data.

    Public so the orchestrator (``_validate_rule.py``) can share the same
    metric without re-implementing it.
    """
    try:
        page = engine.page_by_oracle_id([oid], offset=0, limit=30)
    except (LookupError, ValueError):
        return None
    cmp = compare_to_edhrec(page, edhrec_conn, top_n=30)
    if cmp.hi_syn_size == 0:
        return None
    return cmp.hi_syn_hits


@dataclass(frozen=True)
class ImpactClassification:
    """Verdict + supporting metrics for a single rule's impact check.

    ``verdict`` is always lowercase (``trivial`` / ``harmful`` / ``marginal``
    / ``positive``). Display layers can uppercase as needed.
    """

    verdict: str
    sum_delta: int
    max_abs_delta: int
    ratio: float


def classify_impact(
    movers: tuple[tuple[str, int, int, int], ...],
    scored: int,
) -> ImpactClassification:
    """Map (movers, scored) to a verdict using ``_MARGINAL_RATIO``.

    Verdicts:
      - ``trivial``  : no movement OR wins offset losses (sum == 0)
      - ``harmful``  : net negative
      - ``marginal`` : net positive but ratio < _MARGINAL_RATIO
      - ``positive`` : net positive at or above the ratio threshold
    """
    sum_delta = sum(m[3] for m in movers)
    max_abs = max((abs(m[3]) for m in movers), default=0)
    ratio = sum_delta / (scored or 1)
    if max_abs == 0 or sum_delta == 0:
        verdict = "trivial"
    elif sum_delta < 0:
        verdict = "harmful"
    elif ratio < _MARGINAL_RATIO:
        verdict = "marginal"
    else:
        verdict = "positive"
    return ImpactClassification(
        verdict=verdict,
        sum_delta=sum_delta,
        max_abs_delta=max_abs,
        ratio=ratio,
    )


def _audit_one(
    rule_id: str,
    syn_db: Path,
    edhrec_db: Path,
    name_to_oid: dict[str, str],
) -> dict | None:
    """Return a verdict dict for one rule, or ``None`` if helper missing."""
    helper_name = f"_find_{rule_id}"
    if not hasattr(core, helper_name):
        return None

    touched = find_touched_card_names(syn_db, name_to_oid.keys(), [rule_id])
    targets = [(n, name_to_oid[n]) for n in touched if n in name_to_oid]

    if not targets:
        return {
            "rule_id": rule_id,
            "touched": 0,
            "scored": 0,
            "sum_delta": 0,
            "max_abs_delta": 0,
            "movers": (),
            "verdict": "TRIVIAL",
        }

    edhrec_conn = sqlite3.connect(edhrec_db)
    edhrec_conn.row_factory = sqlite3.Row
    try:
        with SynergyEngine(syn_db) as eng:
            with_scores = {oid: hi_syn_hits(eng, edhrec_conn, oid) for _, oid in targets}

        original = getattr(core, helper_name)
        setattr(core, helper_name, lambda *a, **k: [])
        try:
            with SynergyEngine(syn_db) as eng:
                without_scores = {oid: hi_syn_hits(eng, edhrec_conn, oid) for _, oid in targets}
        finally:
            setattr(core, helper_name, original)
    finally:
        edhrec_conn.close()

    movers: list[tuple[str, int, int, int]] = []
    scored = 0
    for name, oid in targets:
        w, wo = with_scores.get(oid), without_scores.get(oid)
        if w is None or wo is None:
            continue
        scored += 1
        d = w - wo
        if d != 0:
            movers.append((name, w, wo, d))
    movers.sort(key=lambda m: -abs(m[3]))

    cls = classify_impact(tuple(movers), scored)
    # Audit table uppercases the failure verdicts so they grep cleanly;
    # the happy-path "positive" stays lowercase.
    verdict = cls.verdict.upper() if cls.verdict != "positive" else "positive"
    return {
        "rule_id": rule_id,
        "touched": len(targets),
        "scored": scored,
        "sum_delta": cls.sum_delta,
        "max_abs_delta": cls.max_abs_delta,
        "movers": tuple(movers),
        "verdict": verdict,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("data/synergy.db"))
    parser.add_argument("--edhrec-db", type=Path, default=Path("data/tags.db"))
    parser.add_argument(
        "--rule-id",
        nargs="+",
        metavar="RULE_ID",
        help="Specific rules to audit. Default: all generated rules.",
    )
    parser.add_argument(
        "--top-movers",
        type=int,
        default=5,
        help="How many top-magnitude per-commander movers to print per rule.",
    )
    args = parser.parse_args()

    rules = args.rule_id or _all_generated_rules()
    name_to_oid = _name_to_oid(args.db)
    print(
        f"Auditing {len(rules)} rule(s) against {len(name_to_oid)} commanders...",
        file=sys.stderr,
    )

    results: list[dict] = []
    for r in rules:
        verdict = _audit_one(r, args.db, args.edhrec_db, name_to_oid)
        if verdict is None:
            print(f"  [skip] {r}: no _find_{r} in core", file=sys.stderr)
            continue
        results.append(verdict)

    # Summary table.
    print(f"\n{'rule_id':<55} {'touched':>7} {'scored':>6} {'sum Δ':>6} {'max|Δ|':>7}  verdict")
    print("-" * 95)
    for v in results:
        print(
            f"{v['rule_id']:<55} {v['touched']:>7} {v['scored']:>6} "
            f"{v['sum_delta']:>+6} {v['max_abs_delta']:>7}  {v['verdict']}"
        )

    # Per-rule mover detail (for non-trivial ones).
    detail_rules = [v for v in results if v["movers"]]
    if detail_rules:
        print(f"\nPer-commander movers (top {args.top_movers} by |Δ|):")
        for v in detail_rules:
            print(f"\n  {v['rule_id']} [{v['verdict']}]:")
            for name, w, wo, d in v["movers"][: args.top_movers]:
                print(f"    {name[:50]:50}  with={w}  without={wo}  Δ={d:+d}")

    harmful = [v for v in results if v["verdict"] == "HARMFUL"]
    return 1 if harmful else 0


if __name__ == "__main__":
    raise SystemExit(main())
