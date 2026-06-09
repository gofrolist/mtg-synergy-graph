"""Auditor: identify what's missing in rule coverage and propose fixes.

End-to-end pipeline:
  1. Enumerate every commander port shape in the universe, sliced by
     fine-grained discriminators (replacement_result, zone tuple, top
     valid_filter qualifier token).
  2. For each port, find which registered rules' gates match it
     (pure static analysis — no rule simulation needed because the
     registry's gate predicates ARE the commander-side conditions).
  3. Rank gaps by commander reach * (1 - covered_rate).
  4. Match each gap to a rule template from a small catalog.
  5. Emit `docs/gap_report.md` with a ranked queue of concrete rule
     proposals — gate signature, exemplar commanders, candidate-tier
     SQL sketches, expected reach.

Replaces the manual "read coverage, pick a cell, propose a rule" loop
with a single command. The next rule to write is whatever sits at the
top of the report, with no human prioritization required.

The auditor walks both coverage substrates:
  * Python helpers registered in ``complement_rules.registry.RULE_GATES``
    (each gate is a ``(rule_id, predicate)`` pair).
  * Declarative rules in ``src/mtg_synergy_graph/data/rules_seed.json`` — the ``RuleInterpreter``
    compiles their ``gate_predicate`` JSON into equivalent port-level
    predicates so this scan treats them the same way as Python helpers.

Either substrate is sufficient for attribution; the contract is just
"somewhere a gate exists" (2026-04-24 update). Without gate registration
on EITHER side the rule still runs but the auditor cannot attribute
its activations — the old blindspot was real and produced spurious
0%-coverage reports for declarative-only rules.
"""

from __future__ import annotations

import argparse
import collections
import dataclasses
import re
import sqlite3
import sys
from collections.abc import Callable
from pathlib import Path

from mtg_synergy_graph.complement_rules.core import (
    PortRow,
    load_ports_for_set,
)
from mtg_synergy_graph.complement_rules.registry import RULE_GATES
from mtg_synergy_graph.forge_oracle import gap_weight as _forge_gap_weight
from mtg_synergy_graph.port_graph.interpreter import RuleInterpreter
from mtg_synergy_graph.preflight import (
    Candidate as _PreflightCandidate,
)
from mtg_synergy_graph.preflight import (
    PipelineVerdict as _PipelineVerdict,
)
from mtg_synergy_graph.preflight import (
    Severity as _PreflightSeverity,
)
from mtg_synergy_graph.preflight import (
    evaluate_one as _preflight_evaluate_one,
)

# Local sibling-script import for the attempt log.
sys.path.insert(0, str(Path(__file__).parent))
from _attempt_log import prior_attempts_for_template

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
    #: Forge-oracle signal in ``[1.0, 1.5]`` (plan 002 Unit 6). Multiplies
    #: ``impact`` in the sort key. Defaults to 1.0 (neutral) when
    #: ``data/forge_oracle.db`` is missing or has no coverage for this
    #: subkind. See ``src/mtg_synergy_graph/forge_oracle/gap_weight.py``.
    forge_signal: float = 1.0

    @property
    def activation_rate(self) -> float:
        return self.activations / self.commanders if self.commanders else 0.0

    @property
    def impact(self) -> float:
        """Reach × inverse coverage. Used to rank proposals."""
        return self.commanders * (1.0 - self.activation_rate)

    @property
    def weighted_impact(self) -> float:
        """``impact * forge_signal`` — the final rank key (Unit 6)."""
        return self.impact * self.forge_signal


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


def _scan_universe(conn: sqlite3.Connection, commanders: list[str]) -> list[GapStat]:
    """Static gate-matching scan — no rule simulation.

    For each commander, load their ports and bucket by sub-cell
    signature. For each (commander, signature) bucket, walk the
    registered gates (Python *and* declarative) and check which match
    any port in the bucket. A signature is "covered" for the commander
    iff at least one registered rule's gate matches.

    Pure static analysis — O(commanders * ports * gates), runs in
    seconds vs minutes for the simulation-based scan. The trade-off:
    rules without a registered gate on either substrate are invisible
    (no Python ``RuleGate`` entry AND no row in ``src/mtg_synergy_graph/data/rules_seed.json``).
    """
    commanders_per_sig: collections.Counter[tuple[str, str, str]] = collections.Counter()
    activations_per_sig: collections.Counter[tuple[str, str, str]] = collections.Counter()
    exemplars_per_sig: dict[tuple[str, str, str], list[str]] = collections.defaultdict(list)
    rules_per_sig: dict[tuple[str, str, str], collections.Counter[str]] = collections.defaultdict(collections.Counter)

    # Declarative rules: compile each ``gate_predicate`` into a
    # port-level callable so this scan matches them the same way it
    # matches Python ``RULE_GATES``. The interpreter normally runs
    # per-commander SQL, but its compiled gate predicates are pure
    # Python callables we can reuse without any DB queries.
    interpreter = RuleInterpreter(conn)
    declarative_gates: list[tuple[str, Callable[[PortRow], bool]]] = [
        (c.row.rule_id, c.gate) for c in interpreter._compiled
    ]

    for name in commanders:
        ports = load_ports_for_set(conn, [name])
        ports_by_sig: dict[tuple[str, str, str], list[PortRow]] = collections.defaultdict(list)
        for port in ports:
            ports_by_sig[_port_signature(port)].append(port)

        for sig, sig_ports in ports_by_sig.items():
            commanders_per_sig[sig] += 1
            matched_rules: set[str] = set()
            for port in sig_ports:
                for gate in RULE_GATES:
                    if gate.predicate(port):
                        matched_rules.add(gate.rule_id)
                for rule_id, gate_pred in declarative_gates:
                    if gate_pred(port):
                        matched_rules.add(rule_id)
            if matched_rules:
                activations_per_sig[sig] += 1
                for rid in matched_rules:
                    rules_per_sig[sig][rid] += 1
            elif len(exemplars_per_sig[sig]) < 8:
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
    """Concrete proposal: which template to apply to which gap.

    A proposal with ``template == "needs_template"`` represents a gap
    that no template in the catalog matches. The auditor still
    surfaces it (don't hide work just because the catalog is small)
    — the rationale field documents what the writer needs to
    investigate.
    """

    gap: GapStat
    template: str
    rationale: str
    gate_sketch: str
    tier_sketches: tuple[str, ...]
    pool_sizes: dict[str, int]


def _no_template_proposal(gap: GapStat) -> RuleProposal:
    """Placeholder proposal for gaps the catalog can't match yet.

    Lists the existing rule activations (if any) so the writer can
    judge whether the gap needs a new rule or a registry tweak to an
    existing one.
    """
    pt, ev, sub = gap.signature
    if gap.top_rules:
        existing = ", ".join(f"{r}({n})" for r, n in gap.top_rules)
        rationale = (
            f"No template in the catalog matches `{pt}.{ev}[{sub or '*'}]`. "
            f"Some registered rules already partially cover it: {existing}. "
            "Investigate whether broadening one of those gates is enough, "
            "or whether a new template is warranted."
        )
    else:
        rationale = (
            f"No template in the catalog matches `{pt}.{ev}[{sub or '*'}]` "
            "and no registered rule covers any commander carrying this "
            "signature. Pure gap — needs both a new rule and (probably) "
            "a new template entry."
        )
    return RuleProposal(
        gap=gap,
        template="needs_template",
        rationale=rationale,
        gate_sketch=(f"port_type='{pt}' AND event_class='{ev}'" + (f" AND sub_discriminator='{sub}'" if sub else "")),
        tier_sketches=(),
        pool_sizes={},
    )


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
        if 0 < pool <= 100 and ev not in {
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

    # Template: creature_count_scaler
    # scales_with.Valid[*] where the valid_filter references Creature
    # (commander scales with the count of creatures you control).
    # Hamza / Shanna / Beastmaster Ascension shape. Payoff = anything
    # that adds creatures quickly (token producers, wide anthems) and
    # cards that count creatures themselves (other scalers stack IDF).
    if pt == "scales_with" and ev == "Valid" and not sub:
        token_pool = conn.execute(
            "SELECT COUNT(DISTINCT cp.card_name) FROM card_ports cp "
            "JOIN cards c ON c.name = cp.card_name "
            "WHERE cp.port_type = 'effect' AND cp.event_class = 'Token' "
            "AND cp.raw_line LIKE '%TokenScript%' "
            "AND c.legal_commander = 1"
        ).fetchone()[0]
        anthem_pool = conn.execute(
            "SELECT COUNT(DISTINCT card_name) FROM card_ports "
            "WHERE port_type = 'static' AND event_class = 'Continuous' "
            "AND raw_line LIKE '%Creature.YouCtrl%' "
            "AND (raw_line LIKE '%AddPower%' OR raw_line LIKE '%AddToughness%' OR raw_line LIKE '%AddKeyword%')"
        ).fetchone()[0]
        return RuleProposal(
            gap=gap,
            template="creature_count_scaler",
            rationale=(
                f"Commander scales_with creature count (valid_filter references "
                f"Creature). Wants token producers, anthems, wide payoffs. "
                f"{gap.commanders - gap.activations} of {gap.commanders} "
                f"currently uncovered."
            ),
            gate_sketch=(
                "p.port_type='scales_with' AND p.event_class='Valid' "
                "AND 'Creature' IN valid_filter AND no type token from "
                "{Aura, Equipment, Enchantment, Artifact, Land, ...}"
            ),
            tier_sketches=(
                "token_producer: effect=Token with TokenScript (creature tokens)",
                "wide_anthem: static.Continuous Affected=Creature.YouCtrl + AddPower/Toughness/Keyword",
                "other_scalers: scales_with.Valid with Creature in filter (stack IDF)",
            ),
            pool_sizes={"token_producer": token_pool, "wide_anthem": anthem_pool},
        )

    # Template: x_cost_scaler
    # scales_with.xPaid[*] — commander has an X-cost ability that
    # scales with mana paid (Mistform Ultimus, Hydras, Walking Ballista,
    # Comet Storm). Payoff = mana acceleration (rituals, doublers),
    # mana-infinite combos, cost reducers.
    if pt == "scales_with" and ev == "xPaid":
        mana_pool = conn.execute(
            "SELECT COUNT(DISTINCT cp.card_name) FROM card_ports cp "
            "JOIN cards c ON c.name = cp.card_name "
            "WHERE cp.port_type = 'effect' AND cp.event_class = 'Mana' "
            "AND (cp.amount IS NOT NULL OR cp.raw_line LIKE '%Amount%') "
            "AND c.legal_commander = 1"
        ).fetchone()[0]
        ritual_pool = conn.execute(
            "SELECT COUNT(DISTINCT cp.card_name) FROM card_ports cp "
            "JOIN cards c ON c.name = cp.card_name "
            "WHERE cp.port_type = 'effect' AND cp.event_class = 'Mana' "
            "AND c.types LIKE '%Instant%' OR c.types LIKE '%Sorcery%'"
        ).fetchone()[0]
        doubler_pool = conn.execute(
            "SELECT COUNT(DISTINCT card_name) FROM card_ports "
            "WHERE port_type = 'replacement' AND replacement_event = 'ProduceMana'"
        ).fetchone()[0]
        return RuleProposal(
            gap=gap,
            template="x_cost_scaler",
            rationale=(
                f"Commander scales_with X paid — X-cost abilities want "
                f"more mana. Matches existing mana_doubler and cost_reducer "
                f"rules but needs its own gate to attribute xPaid-specific "
                f"commanders. {gap.commanders} commanders on this axis."
            ),
            gate_sketch="p.port_type='scales_with' AND p.event_class='xPaid'",
            tier_sketches=(
                "mana_doubler: replacement.ProduceMana (stack with mana_doubler rule)",
                "ritual: Instant/Sorcery with effect=Mana Amount>=2",
                "x_cost_stacker: scales_with.xPaid (other X-cards)",
            ),
            pool_sizes={"mana_producer": mana_pool, "ritual": ritual_pool, "mana_doubler": doubler_pool},
        )

    # Template: counter_removal_payoff
    # cost.remove_counter[*] — commander activates abilities by
    # removing counters. Hamza's +1/+1 payoffs, Roalesk's proliferate
    # variants, Ghave's sac-and-counter combos. Wants counter sources
    # (producers, doublers, ETB-counter creatures).
    if pt == "cost" and ev == "remove_counter":
        producer_pool = conn.execute(
            "SELECT COUNT(DISTINCT card_name) FROM card_ports "
            "WHERE port_type = 'effect' "
            "AND event_class IN ('PutCounter', 'PutCounterAll') "
            "AND counter_type = 'P1P1'"
        ).fetchone()[0]
        doubler_pool = conn.execute(
            "SELECT COUNT(DISTINCT card_name) FROM card_ports "
            "WHERE port_type = 'replacement' AND event_class = 'AddCounter' "
            "AND raw_line LIKE '%P1P1%'"
        ).fetchone()[0]
        proliferate_pool = conn.execute(
            "SELECT COUNT(DISTINCT card_name) FROM card_ports "
            "WHERE port_type = 'effect' AND event_class = 'Proliferate'"
        ).fetchone()[0]
        return RuleProposal(
            gap=gap,
            template="counter_removal_payoff",
            rationale=(
                f"Commander removes counters as a cost — synergizes with "
                f"anything that ADDS counters. Cross-pollinates with "
                f"counter_axis_feeder and modified_axis_feeder axes but "
                f"keyed from the cost side. {gap.commanders} commanders."
            ),
            gate_sketch=(
                "p.port_type='cost' AND p.event_class='remove_counter' "
                "AND cost_target != 'self' (external counter-consumer)"
            ),
            tier_sketches=(
                "counter_producer: effect.PutCounter[All] P1P1 (reuse counter_axis_feeder pool)",
                "counter_doubler: replacement.AddCounter with P1P1 (Hardened Scales)",
                "proliferate: effect.Proliferate",
            ),
            pool_sizes={"producer": producer_pool, "doubler": doubler_pool, "proliferate": proliferate_pool},
        )

    # Template: replacement_stack (generic)
    # Any replacement.<E>[<R>] with non-trivial commander reach but no
    # existing rule covering it. Requires both event_class and
    # replacement_result -- the generator can't produce a peer-stack
    # rule without both discriminators.
    if pt == "replacement" and sub:
        pool = conn.execute(
            "SELECT COUNT(DISTINCT card_name) FROM card_ports "
            "WHERE port_type = 'replacement' AND event_class = ? "
            "AND replacement_result = ?",
            (ev, sub),
        ).fetchone()[0]
        # Peer-stack rules need a small pool — IDF only stays meaningful
        # when the rule's match candidates are a curated subset, not a
        # broad type. Mirrors the keyword-tribal threshold above. Larger
        # pools (e.g. Moved[ETBTapped] = 589) ship as MARGINAL, displace
        # better picks, and net negative — proven empirically.
        if not 0 < pool <= 100:
            return None
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

#: Port shapes that are mechanically incidental — they exist on
#: thousands of cards as side properties but don't drive payoff
#: matching. A vanilla 4/4 with Flying isn't "uncovered" because
#: Flying has no rule; Flying just isn't a payoff axis. These shapes
#: would dominate the gap report with false positives, so we exclude
#: them entirely. Reintroduce a shape only if a real peer-tribal /
#: payoff template appears for it.
_INCIDENTAL_SHAPES: frozenset[tuple[str, str]] = frozenset(
    {
        # Universal / common keywords — cards have them as static
        # ability prints, not as commander-mechanic anchors. Tribal
        # / peer-evasion payoffs route through dedicated rules with
        # their own gates (combat_enhancer, peer_evasion_tribal,
        # voltron, evasion).
        ("keyword", "Flying"),
        ("keyword", "Vigilance"),
        ("keyword", "Trample"),
        ("keyword", "Haste"),
        ("keyword", "Reach"),
        ("keyword", "Lifelink"),
        ("keyword", "Menace"),
        ("keyword", "Deathtouch"),
        ("keyword", "First Strike"),
        ("keyword", "Double Strike"),
        ("keyword", "Defender"),
        ("keyword", "Hexproof"),
        ("keyword", "Indestructible"),
        ("keyword", "Flash"),
        ("keyword", "Partner"),
        ("keyword", "Shroud"),
        ("keyword", "Intimidate"),
        ("keyword", "Fear"),
        ("keyword", "Skulk"),
        # Importer occasionally surfaces multi-word keywords as their
        # first token (e.g. "First Strike" -> "First", "Double
        # Strike" -> "Double"). Treat both forms as incidental.
        ("keyword", "First"),
        ("keyword", "Double"),
        ("keyword", "Banding"),
        ("keyword", "Phasing"),
        ("keyword", "Bushido"),
        ("keyword", "Protection"),
        # Universal costs / structural ports.
        ("cost", "tap"),
        ("cost", "sacrifice"),
        ("cost", "discard"),
        ("cost", "exile"),
        ("cost", "pay_life"),
        # Wrapper / utility effects with no payoff semantics.
        ("effect", "Cleanup"),
        ("effect", "Effect"),
        ("effect", "SetState"),
        ("effect", "DelayedTrigger"),
        ("effect", "ImmediateTrigger"),
        ("effect", "Animate"),
        ("effect", "AnimateAll"),
    }
)


def _eligible(gap: GapStat, *, max_activation: float) -> bool:
    """Eligibility filter — keep any gap with non-100% coverage.

    No minimum commander threshold: a single uncovered commander is a
    real gap (their port shape has no rule). The impact metric
    (commanders * (1 - covered_rate)) sorts low-reach gaps to the
    bottom naturally without dropping them.
    """
    if gap.activation_rate >= max_activation:
        return False
    pt, ev, sub = gap.signature
    # Drop incidental port shapes (Flying, Vigilance, cost.tap,
    # effect.Cleanup, etc.) — they don't drive payoff matching.
    if (pt, ev) in _INCIDENTAL_SHAPES:
        return False
    # Empty sub-discriminator entries duplicate the cell-level
    # coverage_matrix.py output — drop them unless the cell genuinely
    # has no further structure (cost ports, keyword ports,
    # scales_with).
    return not (not sub and pt not in ("cost", "keyword", "scales_with"))


def _evaluate_preflight(
    proposals: list[RuleProposal],
    conn: sqlite3.Connection,
) -> dict[tuple[str, str, str], _PipelineVerdict]:
    """Run Stage A pre-flight on each proposal's signature.

    Returns a dict keyed by signature tuple so the formatter can look up
    each proposal's verdict without re-running the SQL queries.
    """
    out: dict[tuple[str, str, str], _PipelineVerdict] = {}
    for prop in proposals:
        sig = prop.gap.signature
        if sig in out:
            continue
        candidate = _PreflightCandidate(
            signature=sig,
            gap_id=f"{sig[0]}.{sig[1]}[{sig[2] or '*'}]",
        )
        out[sig] = _preflight_evaluate_one(candidate, conn)
    return out


def _band_for_severity(severity: _PreflightSeverity) -> int:
    """Return sort-key band: PASS=0, WARN=1, REJECT=2."""
    return severity.value


def _format_report(
    proposals: list[RuleProposal],
    stats_total: int,
    eligible_total: int,
    *,
    verdicts: dict[tuple[str, str, str], _PipelineVerdict] | None = None,
) -> str:
    lines: list[str] = []
    lines.append("# Rule coverage gap report")
    lines.append("")
    lines.append(
        "Auto-generated by `scripts/gap_report.py`. Each entry is a "
        "sub-cell with non-100% coverage, ranked by "
        "`commanders * (1 - covered_rate)`. Proposals labelled "
        "`needs_template` lack a matching template in the catalog — "
        "they're real gaps, just unfit for any existing template."
    )
    lines.append("")
    lines.append(f"**Total sub-cells scanned**: {stats_total}")
    lines.append(f"**Eligible gaps (covered_rate < threshold)**: {eligible_total}")
    lines.append(f"**Shown in this report**: {len(proposals)}")
    lines.append("")

    if not proposals:
        lines.append(
            "No gaps surviving filter — all under-covered "
            "sub-cells either have low reach (<10 commanders) "
            "or their template is already implemented."
        )
        lines.append("")
        return "\n".join(lines)

    # Pre-flight band sorting: PASS first, then WARN, then REJECT.
    # Within each band, preserve impact-desc order (proposals are already
    # sorted by impact desc when they arrive here per rank_gaps).
    if verdicts is None:
        verdicts = {}

    pass_band: list[RuleProposal] = []
    warn_band: list[RuleProposal] = []
    reject_band: list[RuleProposal] = []
    unknown_band: list[RuleProposal] = []

    for prop in proposals:
        verdict = verdicts.get(prop.gap.signature)
        if verdict is None:
            unknown_band.append(prop)
        elif verdict.severity is _PreflightSeverity.PASS:
            pass_band.append(prop)
        elif verdict.severity is _PreflightSeverity.WARN:
            warn_band.append(prop)
        else:
            reject_band.append(prop)

    lines.append("## Ranked proposals")
    lines.append("")
    lines.append(
        f"**Pre-flight summary**: {len(pass_band)} PASS · "
        f"{len(warn_band)} WARN · {len(reject_band)} REJECT"
        + (f" · {len(unknown_band)} unevaluated" if unknown_band else "")
    )
    lines.append("")

    sequence: list[tuple[str, str, list[RuleProposal]]] = [
        ("PASS", "Stage A predicts the rule is testable on the current fixture.", pass_band),
        (
            "WARN",
            "Stage A flagged the rule as structurally legitimate but unmeasurable on the 500-cmdr "
            "fixture (FIXTURE_BLIND_SPOT). Attempt at your own risk; consider extending the fixture.",
            warn_band,
        ),
        (
            "REJECT",
            "Stage A predicts the rule fires on essentially nothing in the universe (UNTESTABLE). "
            "Skip unless you have evidence the gate is wrong.",
            reject_band,
        ),
    ]
    if unknown_band:
        sequence.append(("UNEVALUATED", "Pre-flight evaluation unavailable for these proposals.", unknown_band))

    counter = 0
    for band_label, band_blurb, band_props in sequence:
        lines.append(f"### {band_label} ({len(band_props)} entries)")
        lines.append("")
        lines.append(f"_{band_blurb}_")
        lines.append("")
        if not band_props:
            lines.append("(none)")
            lines.append("")
            continue

        for prop in band_props:
            counter += 1
            i = counter
            pt, ev, sub = prop.gap.signature
            lines.append(f"#### #{i}: `{pt}.{ev}[{sub or '*'}]`")
            # Pre-flight verdict line: surface Stage A's reason directly
            # so humans reading the report see the decision context per
            # entry, not just per band.
            verdict = verdicts.get(prop.gap.signature)
            if verdict is not None:
                # Always show the actual gate reason rather than the
                # PipelineVerdict.reason property (which collapses to
                # "PASS" when all gates pass — losing the per-gate
                # detail humans want here).
                gate_reasons = " | ".join(g.reason for g in verdict.gates)
                lines.append(f"- **Pre-flight**: {verdict.severity.name} — {gate_reasons}")
            # Decorate with prior-attempt history (if any). Prevents the
            # reader from re-attempting a known-failing template/rule_id
            # combination without first refining it.
            prior = prior_attempts_for_template(prop.template)
            if prior:
                sig_prior = [a for a in prior if tuple(a.signature) == tuple(prop.gap.signature)]
                sig_reverts = [a for a in sig_prior if a.outcome == "reverted"]
                sig_trivials = [a for a in sig_prior if a.outcome == "trivial"]
                sig_passes = [a for a in sig_prior if a.outcome == "passed"]
                tpl_reverts = [a for a in prior if a.outcome == "reverted"]
                if sig_reverts:
                    latest = sig_reverts[-1]
                    lines.append(
                        f"- ⚠️ **Signature reverted**: {len(sig_reverts)} prior "
                        f"revert(s) on this exact signature — most recent "
                        f"{latest.timestamp}: _{latest.reason[:200]}_"
                    )
                elif sig_trivials:
                    latest = sig_trivials[-1]
                    lines.append(
                        f"- 🟡 **Signature trivial**: {len(sig_trivials)} prior "
                        f"trivial attempt(s) on this exact signature — most "
                        f"recent {latest.timestamp}: _{latest.reason[:200]}_"
                    )
                elif tpl_reverts:
                    latest = tpl_reverts[-1]
                    lines.append(
                        f"- ⚠️ **Template caution**: {len(tpl_reverts)} revert(s) "
                        f"elsewhere on this template — most recent "
                        f"{latest.timestamp}: _{latest.reason[:200]}_"
                    )
                if sig_passes:
                    lines.append(
                        f"- ✅ **Signature already shipped**: {len(sig_passes)} "
                        "passing apply(s) on this exact signature — rule should "
                        "be in the registry; investigate runtime mismatch."
                    )
            lines.append(
                f"- **Reach**: {prop.gap.commanders} commanders carrying "
                f"this signature; {prop.gap.activations} get any rule "
                f"activation ({prop.gap.activation_rate:.0%})."
            )
            if prop.gap.forge_signal != 1.0:
                lines.append(
                    f"- **Impact**: {prop.gap.impact:.1f} * "
                    f"forge_signal {prop.gap.forge_signal:.2f} = "
                    f"{prop.gap.weighted_impact:.1f}"
                )
            else:
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
# Public API for in-process callers (e.g., scripts/forge_oracle.py propose-rules).
# ---------------------------------------------------------------------------


def rank_gaps(
    conn: sqlite3.Connection,
    *,
    forge_oracle_db: Path | None = None,
    max_activation: float = 1.0,
    commanders_limit: int = 0,
    warn_fn: Callable[[str], None] | None = None,
) -> tuple[list[RuleProposal], int, int]:
    """Scan universe, apply forge_signal, rank, and propose.

    Returns ``(proposals, stats_total, eligible_total)``. Proposals are
    sorted by descending ``weighted_impact``. Callers should slice to
    their desired depth.

    ``warn_fn`` is called with human-readable status messages. Pass
    ``print(..., file=sys.stderr)`` or ``logger.warning`` to surface
    them; pass ``None`` to silence.
    """
    commanders = _commander_names(conn)
    if commanders_limit > 0:
        commanders = commanders[:commanders_limit]
    if warn_fn is not None:
        warn_fn(f"scanning {len(commanders)} commanders for sub-cell coverage...")
    stats = _scan_universe(conn, commanders)

    forge_signals = _forge_gap_weight.load_forge_signals(forge_oracle_db) if forge_oracle_db is not None else {}
    if not forge_signals and forge_oracle_db is not None and warn_fn is not None:
        warn_fn(
            f"forge_oracle.db at {forge_oracle_db} is missing or empty — "
            "falling back to volume-only ranking (forge_signal = 1.0 for all gaps)."
        )
    stats = [
        dataclasses.replace(
            s,
            forge_signal=_forge_gap_weight.forge_weight_for_signature(s.signature, forge_signals),
        )
        for s in stats
    ]

    eligible = [s for s in stats if _eligible(s, max_activation=max_activation)]
    eligible.sort(key=lambda s: (-s.weighted_impact, -s.commanders, s.signature))

    proposals: list[RuleProposal] = []
    for gap in eligible:
        prop = _propose(gap, conn)
        if prop is None:
            prop = _no_template_proposal(gap)
        proposals.append(prop)

    return proposals, len(stats), len(eligible)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("data/synergy.db"))
    parser.add_argument("--out", type=Path, default=Path("docs/gap_report.md"))
    parser.add_argument(
        "--forge-oracle-db",
        dest="forge_oracle_db",
        type=Path,
        default=Path("data/forge_oracle.db"),
        help="Optional sidecar forge_oracle.db. Absent/empty → silent "
        "fallback to volume-only ranking (plan 002 Unit 6).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="if >0, sample only the first N commanders (for fast iteration)",
    )
    parser.add_argument(
        "--max-activation",
        type=float,
        default=1.0,
        help="upper bound on covered_rate; default 1.0 keeps every gap "
        "with non-100% coverage. Lower (e.g. 0.5) to focus only on "
        "severely uncovered sub-cells.",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=50,
        help="how many ranked proposals to include in the report. "
        "Sub-cells are sorted by impact = commanders * (1 - covered_rate); "
        "single-commander gaps still appear (just lower in the queue).",
    )
    args = parser.parse_args()

    # sqlite3.connect would silently create an empty DB at a missing
    # path and the report would show zero ports / zero gaps.
    if not Path(args.db).is_file():
        print(
            f"error: {args.db} not found — run scripts/import_cardsfolder.py first.",
            file=sys.stderr,
        )
        return 2

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    try:
        proposals, stats_total, eligible_total = rank_gaps(
            conn,
            forge_oracle_db=args.forge_oracle_db,
            max_activation=args.max_activation,
            commanders_limit=args.limit,
            warn_fn=lambda msg: print(msg, file=sys.stderr),
        )
        top_proposals = proposals[: args.top]
        verdicts = _evaluate_preflight(top_proposals, conn)
        report = _format_report(
            top_proposals,
            stats_total=stats_total,
            eligible_total=eligible_total,
            verdicts=verdicts,
        )
    finally:
        conn.close()

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(report)
    print(report)
    print(f"\nwrote {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
