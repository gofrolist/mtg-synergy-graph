"""Auditor: identify what's missing in rule coverage and propose fixes.

End-to-end pipeline:
  1. Enumerate every commander port shape in the universe, sliced by
     fine-grained discriminators (replacement_result, zone tuple, top
     valid_filter qualifier token).
  2. Compute empirical coverage per sub-cell via find_all_complements.
  3. Rank surviving gaps by commander reach × inverse activation rate.
  4. Match each gap to a rule template from a small catalog.
  5. Emit `docs/gap_report.md` with a ranked queue of concrete rule
     proposals — gate signature, exemplar commanders, candidate-tier
     SQL sketches, expected reach.

Replaces the manual "read coverage, pick a cell, propose a rule" loop
with a single command. The next rule to write is whatever sits at the
top of the report, with no human prioritization required.
"""

from __future__ import annotations

import argparse
import collections
import dataclasses
import re
import sqlite3
import sys
import time
from pathlib import Path

from mtg_synergy_graph.complement_rules.core import (
    PortRow,
    find_all_complements,
    load_ports_for_set,
)
from mtg_synergy_graph.complement_rules.registry import (
    CARD_LEVEL_RULES,
    RULE_GATES,
    registered_rule_ids,
)
from mtg_synergy_graph.penalties import build_candidate_cache

# ---------------------------------------------------------------------------
# Sub-cell signature extraction
# ---------------------------------------------------------------------------

#: ``valid_filter`` qualifier tokens that meaningfully discriminate a
#: cell. ``modified`` / ``attacking`` / ``tapped`` etc each imply
#: distinct mechanical archetypes the engine should recognize.
#: Counter-axis tokens (``counters_GE*``) collapse to a single
#: ``counters_GE`` signature so we don't fragment Hamza / Marchesa /
#: etc into per-counter cells.
#:
#: ``Self`` is intentionally excluded — it's a self-reference (the
#: trigger / effect / cost mentions CARDNAME), not a payoff axis.
#: Including it would surface false gaps like ``trigger.Attacks
#: [Self]`` (every voltron commander) where the right discriminator
#: is whether the trigger has an engine effect (covered by
#: combat_enhancer), not whether it self-references.
_NOTABLE_QUALIFIERS: frozenset[str] = frozenset(
    {
        "modified",
        "attacking",
        "blocking",
        "tapped",
        "untapped",
        "kicked",
        "Other",
        "token",
    }
)

_COUNTER_GATE_RE = re.compile(r"counters_GE\d*_[A-Z0-9]+")


def _notable_qualifier(valid_filter: str) -> str:
    """Pick the most discriminating qualifier token from a valid_filter.

    Priority: counters_GE > modified > attacking/blocking/tapped >
    Self/Other > kicked/token > "" (none). Never returns more than one
    token — keeps sub-cell cardinality bounded.
    """
    if not valid_filter:
        return ""
    if _COUNTER_GATE_RE.search(valid_filter):
        return "counters_GE"
    tokens = [t.strip().lstrip("!") for t in re.split(r"[.+,]", valid_filter)]
    for priority in ("modified", "attacking", "blocking", "tapped", "untapped", "Other", "kicked", "token"):
        if priority in tokens:
            return priority
    return ""


def _port_signature(port: dict) -> tuple[str, str, str]:
    """Return ``(port_type, event_class, sub_discriminator)`` triple.

    The sub_discriminator is empty for cells that don't benefit from
    further slicing. For replacement ports it's the
    ``replacement_result``. For ChangesZone / ChangeZone events it's
    ``"<origin>->><destination>"``. Otherwise it's the most notable
    valid_filter qualifier token (from ``_NOTABLE_QUALIFIERS``).
    """
    pt = (port.get("port_type") or "").strip()
    ev = (port.get("event_class") or "").strip()
    if pt == "replacement":
        result = (port.get("replacement_result") or "").strip()
        return (pt, ev, result)
    if ev in ("ChangesZone", "ChangeZone", "ChangesZoneAll", "ChangeZoneAll"):
        zo = (port.get("zone_origin") or "").strip()
        zd = (port.get("zone_destination") or "").strip()
        if zo or zd:
            return (pt, ev, f"{zo}->{zd}")
    qual = _notable_qualifier((port.get("valid_filter") or "").strip())
    return (pt, ev, qual)


# ---------------------------------------------------------------------------
# Gap detection
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class GapStat:
    """Empirical coverage stats for a single sub-cell signature."""

    signature: tuple[str, str, str]
    commanders: int
    activations: int
    exemplars: tuple[str, ...]
    top_rules: tuple[tuple[str, int], ...]

    @property
    def activation_rate(self) -> float:
        return self.activations / self.commanders if self.commanders else 0.0

    @property
    def impact(self) -> float:
        """Reach × inverse coverage. Used to rank proposals."""
        return self.commanders * (1.0 - self.activation_rate)


def _commander_names(conn: sqlite3.Connection) -> list[str]:
    return [
        r[0]
        for r in conn.execute(
            "SELECT name FROM cards "
            "WHERE legal_commander = 1 "
            "AND types LIKE '%Creature%' "
            "AND supertypes LIKE '%Legendary%' "
            "ORDER BY name"
        )
    ]


def _scan_universe(
    conn: sqlite3.Connection,
    commanders: list[str],
    *,
    progress_every: int = 200,
) -> list[GapStat]:
    """Per-port attribution scan.

    For each commander, load all their ports and compute their
    sub-cell signatures. Run find_all_complements once. For every
    fired rule that has a registered gate, determine which of the
    commander's ports actually satisfy that gate — those are the
    ports the rule was attributed to. A sub-cell is "covered" for a
    commander iff at least one of the commander's ports carrying that
    signature is attributable to a fired rule via the registry.

    Rules without a registered gate fall back to the legacy commander-
    level signal: any unregistered rule that fired marks every one of
    the commander's signatures as covered. This keeps the auditor
    complete (no rule's activations are dropped) while letting
    registered rules give exact attribution.
    """
    cache = build_candidate_cache(conn)
    registered = registered_rule_ids()

    ports_by_cmdr: dict[str, list[PortRow]] = {}
    sigs_by_cmdr: dict[str, set[tuple[str, str, str]]] = {}
    for name in commanders:
        ports = load_ports_for_set(conn, [name])
        ports_by_cmdr[name] = ports
        sigs_by_cmdr[name] = {_port_signature(p) for p in ports}

    commanders_per_sig: collections.Counter[tuple[str, str, str]] = collections.Counter()
    activations_per_sig: collections.Counter[tuple[str, str, str]] = collections.Counter()
    exemplars_per_sig: dict[tuple[str, str, str], list[str]] = collections.defaultdict(list)
    rules_per_sig: dict[tuple[str, str, str], collections.Counter[str]] = collections.defaultdict(collections.Counter)

    for sigs in sigs_by_cmdr.values():
        for sig in sigs:
            commanders_per_sig[sig] += 1

    t0 = time.time()
    for i, name in enumerate(commanders):
        if i and i % progress_every == 0:
            elapsed = time.time() - t0
            print(
                f"  ...{i}/{len(commanders)}  ({elapsed:.0f}s, {i / elapsed:.0f}/s)",
                file=sys.stderr,
                flush=True,
            )
        try:
            comps = find_all_complements(conn, [name], candidate_cache=cache)
        except Exception as exc:
            print(f"  [skip] {name}: {exc}", file=sys.stderr)
            continue
        fired_rule_ids = {c.rule_id for c in comps}
        # Card-level rules (tribal_density, lord, scaling, etc.) fire
        # based on commander identity (subtypes / oracle text), not on
        # any specific port's mechanical shape. Excluding them from
        # the unregistered-fallback set keeps per-signature activation
        # honest — a Goblin commander's tribal_density firing
        # shouldn't mark their `trigger.DamageDone[Self]` port covered.
        unregistered_fired = fired_rule_ids - registered - CARD_LEVEL_RULES

        # For each port, compute the registered rules attributable to it
        # AND whether any of those rules actually fired in this run.
        sig_to_attributed_rules: dict[tuple[str, str, str], set[str]] = collections.defaultdict(set)
        for port in ports_by_cmdr.get(name, ()):
            sig = _port_signature(port)
            for gate in RULE_GATES:
                if gate.rule_id in fired_rule_ids and gate.predicate(port):
                    sig_to_attributed_rules[sig].add(gate.rule_id)

        for sig in sigs_by_cmdr.get(name, ()):
            attributed = sig_to_attributed_rules.get(sig, set())
            # Fallback: unregistered rules fired but we can't attribute
            # them to a specific port. Mark every signature as covered
            # by them so we don't undercount unregistered-rule reach.
            covered_by = attributed | unregistered_fired
            if covered_by:
                activations_per_sig[sig] += 1
                for rid in covered_by:
                    rules_per_sig[sig][rid] += 1
            else:
                if len(exemplars_per_sig[sig]) < 8:
                    exemplars_per_sig[sig].append(name)

    out: list[GapStat] = []
    for sig, n_cmdrs in commanders_per_sig.items():
        out.append(
            GapStat(
                signature=sig,
                commanders=n_cmdrs,
                activations=activations_per_sig.get(sig, 0),
                exemplars=tuple(exemplars_per_sig.get(sig, [])),
                top_rules=tuple(rules_per_sig[sig].most_common(5)),
            )
        )
    return out


# ---------------------------------------------------------------------------
# Template catalog
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class RuleProposal:
    """Concrete proposal: which template to apply to which gap."""

    gap: GapStat
    template: str
    rationale: str
    gate_sketch: str
    tier_sketches: tuple[str, ...]
    pool_sizes: dict[str, int]


#: Damage-amp ``replacement_result`` tokens (mirror of the set in
#: utility.py — the auditor uses it to recognize the doubler family).
_DAMAGE_AMP_RESULTS: frozenset[str] = frozenset(
    {"DmgTwice", "DmgTriple", "DmgPlus", "DmgPlus1", "DmgPlus2", "Dmg2", "Dmg3", "HarshDmg"}
)


def _propose(gap: GapStat, conn: sqlite3.Connection) -> RuleProposal | None:
    """Match a sub-cell gap to the best-fitting rule template.

    Returns None if no template applies (manual investigation needed).
    Each template hard-codes its eligibility predicate AND uses live
    SQL probes to estimate candidate-tier pool sizes from the data.
    """
    pt, ev, sub = gap.signature

    # Template: damage_prevention_voltron
    # replacement.DamageDone with replacement_result='Prevent' targeting
    # YouCtrl-side permanents → commander wants aggressive creatures
    # whose attacks become risk-free.
    if pt == "replacement" and ev == "DamageDone" and sub == "Prevent":
        creature_pool = conn.execute(
            "SELECT COUNT(DISTINCT name) FROM cards "
            "WHERE types LIKE '%Creature%' "
            "AND CAST(power AS INTEGER) >= 3 "
            "AND cmc <= 4 AND legal_commander = 1"
        ).fetchone()[0]
        evasion_pool = conn.execute(
            "SELECT COUNT(DISTINCT card_name) FROM card_ports "
            "WHERE port_type = 'keyword' "
            "AND event_class IN ('Menace', 'Trample', 'Flying', 'First Strike', 'Double Strike')"
        ).fetchone()[0]
        return RuleProposal(
            gap=gap,
            template="damage_prevention_voltron",
            rationale=(
                "Prevention statics let attackers ignore retaliation — "
                "the payoff is high-power creatures with combat keywords."
            ),
            gate_sketch=(
                "p.port_type='replacement' AND p.event_class='DamageDone' "
                "AND p.replacement_result='Prevent' "
                "AND raw_line LIKE '%YouCtrl%' (preventer protects YOUR things)"
            ),
            tier_sketches=(
                "aggressive_creatures: types LIKE '%Creature%' AND power>=3 AND cmc<=4",
                "evasion_keyword: keyword in (Menace, Trample, Flying, First Strike, Double Strike)",
            ),
            pool_sizes={"aggressive_creatures": creature_pool, "evasion_keyword": evasion_pool},
        )

    # Template: damage_amp_doubler (already implemented as
    # damage_doubler_synergy — emit reminder so the auditor doesn't
    # re-propose).
    if pt == "replacement" and ev == "DamageDone" and sub in _DAMAGE_AMP_RESULTS:
        return RuleProposal(
            gap=gap,
            template="damage_amp_doubler [IMPLEMENTED]",
            rationale="Already covered by damage_doubler_synergy.",
            gate_sketch="(see complement_rules/utility.py:_find_damage_doubler_synergy)",
            tier_sketches=(),
            pool_sizes={},
        )

    # Template: peer_tribal
    # keyword.<K> where K is a small-pool keyword AND not a vanilla
    # blocker like Flying. Surface other cards with the same keyword.
    if pt == "keyword":
        pool = conn.execute(
            "SELECT COUNT(DISTINCT card_name) FROM card_ports WHERE port_type = 'keyword' AND event_class = ?",
            (ev,),
        ).fetchone()[0]
        if 0 < pool <= 80 and ev not in {
            "Flying",
            "Trample",
            "Vigilance",
            "Haste",
            "Reach",
            "First Strike",
            "Double Strike",
            "Lifelink",
            "Menace",
            "Deathtouch",
            "Defender",
            "Hexproof",
            "Indestructible",
            "Flash",
            "Partner",
        }:
            return RuleProposal(
                gap=gap,
                template="peer_tribal_keyword",
                rationale=(
                    f"Keyword '{ev}' has a small card pool ({pool}). Commanders "
                    "carrying it want the rest of the pool as natural partners."
                ),
                gate_sketch=f"p.port_type='keyword' AND p.event_class='{ev}'",
                tier_sketches=(f"same-keyword pool: card_ports.event_class='{ev}'",),
                pool_sizes={"same_keyword": pool},
            )

    # Template: replacement_stack (generic)
    # Any replacement.<E> with non-trivial commander reach but no
    # existing rule covering it.
    if pt == "replacement":
        pool = conn.execute(
            "SELECT COUNT(DISTINCT card_name) FROM card_ports "
            "WHERE port_type = 'replacement' AND event_class = ? "
            "AND replacement_result = ?",
            (ev, sub),
        ).fetchone()[0]
        return RuleProposal(
            gap=gap,
            template="replacement_stack",
            rationale=(
                f"Other cards with the same replacement event '{ev}' and "
                f"result '{sub}' stack with the commander's effect."
            ),
            gate_sketch=(f"p.port_type='replacement' AND p.event_class='{ev}' AND p.replacement_result='{sub}'"),
            tier_sketches=(
                f"same-shape replacements: port_type='replacement' AND "
                f"event_class='{ev}' AND replacement_result='{sub}'",
            ),
            pool_sizes={"same_shape": pool},
        )

    # Template: axis_feeder (qualifier-specific)
    # Any port carrying a notable qualifier (modified / counters_GE /
    # attacking / etc) on a sub-cell with low coverage.
    if sub in _NOTABLE_QUALIFIERS or sub == "counters_GE":
        return RuleProposal(
            gap=gap,
            template="axis_feeder",
            rationale=(f"Qualifier '{sub}' is a payoff axis — surface cards that produce or scale on that axis."),
            gate_sketch=(f"any cmdr port with valid_filter containing '{sub}' on a non-Self scope"),
            tier_sketches=(
                f"axis_payoff: port valid_filter contains '{sub}'",
                f"axis_producer: effect that produces the {sub} state",
            ),
            pool_sizes={},
        )

    return None


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

#: Cells too broad to slice usefully — the bare cell-level signature
#: is fine for them and sub-cell entries with empty discriminator are
#: dropped from the report (they duplicate what coverage_matrix.py
#: already shows).
_DROP_EMPTY_SUB: frozenset[tuple[str, str]] = frozenset()


def _eligible(gap: GapStat, *, min_commanders: int, max_activation: float) -> bool:
    if gap.commanders < min_commanders:
        return False
    if gap.activation_rate >= max_activation:
        return False
    pt, _ev, sub = gap.signature
    # Empty sub-discriminator entries duplicate the cell-level
    # coverage_matrix.py output — drop them unless the cell genuinely
    # has no further structure (cost ports, keyword ports).
    return not (not sub and pt not in ("cost", "keyword", "scales_with"))


def _format_report(proposals: list[RuleProposal], stats_total: int) -> str:
    lines: list[str] = []
    lines.append("# Rule coverage gap report")
    lines.append("")
    lines.append(
        "Auto-generated by `scripts/gap_report.py`. Each entry is a "
        "sub-cell with non-trivial commander reach and low empirical "
        "coverage, ranked by `commanders * (1 - activation_rate)`. "
        "The proposed template is the auditor's best fit — implement "
        "the top entry, then re-run."
    )
    lines.append("")
    lines.append(f"**Total sub-cells scanned**: {stats_total}")
    lines.append(f"**Surviving gaps**: {len(proposals)}")
    lines.append("")

    if not proposals:
        lines.append(
            "No gaps surviving filter — all under-covered "
            "sub-cells either have low reach (<10 commanders) "
            "or their template is already implemented."
        )
        lines.append("")
        return "\n".join(lines)

    lines.append("## Ranked proposals")
    lines.append("")
    for i, prop in enumerate(proposals, 1):
        pt, ev, sub = prop.gap.signature
        lines.append(f"### #{i}: `{pt}.{ev}[{sub or '*'}]`")
        lines.append(
            f"- **Reach**: {prop.gap.commanders} commanders carrying "
            f"this signature; {prop.gap.activations} get any rule "
            f"activation ({prop.gap.activation_rate:.0%})."
        )
        lines.append(f"- **Impact**: {prop.gap.impact:.1f}")
        lines.append(f"- **Template**: `{prop.template}`")
        lines.append(f"- **Rationale**: {prop.rationale}")
        if prop.gap.exemplars:
            lines.append("- **Exemplar commanders** (no rule activation): " + ", ".join(prop.gap.exemplars))
        if prop.gap.top_rules:
            top = ", ".join(f"{r}({n})" for r, n in prop.gap.top_rules)
            lines.append(f"- **Existing rule activations**: {top}")
        lines.append(f"- **Gate sketch**: `{prop.gate_sketch}`")
        if prop.tier_sketches:
            lines.append("- **Tier sketches**:")
            for t in prop.tier_sketches:
                lines.append(f"  - {t}")
        if prop.pool_sizes:
            ps = ", ".join(f"{k}={v}" for k, v in prop.pool_sizes.items())
            lines.append(f"- **Estimated pool sizes**: {ps}")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("data/synergy.db"))
    parser.add_argument("--out", type=Path, default=Path("docs/gap_report.md"))
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="if >0, sample only the first N commanders (for fast iteration)",
    )
    parser.add_argument(
        "--min-commanders",
        type=int,
        default=10,
        help="minimum commander reach for a sub-cell to qualify as a gap",
    )
    parser.add_argument(
        "--max-activation",
        type=float,
        default=0.5,
        help="maximum activation_rate (fraction) for a sub-cell to qualify as a gap",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=20,
        help="how many ranked proposals to include in the report",
    )
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    try:
        commanders = _commander_names(conn)
        if args.limit > 0:
            commanders = commanders[: args.limit]
        print(
            f"scanning {len(commanders)} commanders for sub-cell coverage...",
            file=sys.stderr,
        )
        stats = _scan_universe(conn, commanders)

        eligible = [
            s for s in stats if _eligible(s, min_commanders=args.min_commanders, max_activation=args.max_activation)
        ]
        eligible.sort(key=lambda s: -s.impact)

        proposals: list[RuleProposal] = []
        for gap in eligible[: args.top]:
            prop = _propose(gap, conn)
            if prop is not None:
                proposals.append(prop)

        report = _format_report(proposals, stats_total=len(stats))
    finally:
        conn.close()

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(report)
    print(report)
    print(f"\nwrote {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
