"""Static ability complement matchers (cost reduction, graveyard play, edicts)."""

from __future__ import annotations

import re
import sqlite3
from typing import TYPE_CHECKING

from ..graph_engine import _trigger_only_matches_self
from .core import PortComplement, PortRow

if TYPE_CHECKING:
    from ..penalties import CandidateCache

#: Coverage-oriented rule (spec 2026-07-08-team-anthem-payoff-rule). Default
#: OFF and hash-neutral until it clears the pre-registered coverage +
#: no-regression gates.
_ENABLE_TEAM_ANTHEM_PAYOFF: bool = False

#: valid_filter values indicating edict-style sacrifice targeting.
_EDICT_FILTERS: tuple[str, ...] = (
    "Player",
    "Opponent",
    "Player.Opponent",
    "TriggeredPlayer",
    "TriggeredDefendingPlayer",
    "Player.Other",
)

#: Top-level card types that map cleanly to the ``card_types`` column.
#: Compound Affinity forms like ``Creature.Artifact:artifact``
#: (Urza, Chief Artificer) or ``Land.Snow:snow`` would not match
#: card_types as a substring; callers take the first type token only,
#: skip the rest.
_AFFINITY_RECOGNIZED_TYPES: frozenset[str] = frozenset(
    {"Artifact", "Creature", "Enchantment", "Land", "Planeswalker", "Instant", "Sorcery"}
)


def _find_cost_reduction_synergy(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
) -> list[PortComplement]:
    """Find cost reducers for commanders with SpellCast triggers.

    Talrand triggers on Instant/Sorcery cast -> Goblin Electromancer
    reduces Instant/Sorcery costs, letting you cast more spells per turn.
    Jhoira triggers on Artifact cast -> Etherium Sculptor reduces
    Artifact costs.

    Only matches when the cost reducer's ValidCard type overlaps the
    commander's spell trigger filter type.
    """
    # Collect spell types the commander cares about
    wanted_types: set[str] = set()
    for p in cmdr_ports:
        pt = (p.get("port_type") or "").strip()
        ev = (p.get("event_class") or "").strip()
        if pt == "trigger" and ev == "SpellCast":
            vf = p.get("valid_filter") or ""
            for alt in vf.split(","):
                base = alt.strip().split(".")[0].split("+")[0].strip()
                if base in ("Instant", "Sorcery", "Creature", "Artifact", "Enchantment", "Aura", "Equipment"):
                    wanted_types.add(base)
            # "nonCreature" -> Instant/Sorcery/Artifact/Enchantment
            if "nonCreature" in vf:
                wanted_types.update(("Instant", "Sorcery", "Artifact", "Enchantment"))

    if not wanted_types:
        return []

    # Find ReduceCost statics that affect the wanted types
    cur = conn.execute(
        "SELECT DISTINCT card_name, raw_line FROM card_ports "
        "WHERE port_type = 'static' AND event_class = 'ReduceCost' "
        "AND raw_line NOT LIKE '%ValidCard%Card.Self%' "
        "AND raw_line NOT LIKE '%ValidTarget%Card.Self%'"
    )
    results: list[PortComplement] = []
    seen: set[str] = set()
    for r in cur.fetchall():
        name = r["card_name"]
        if name in cmdr_set or name in seen:
            continue
        raw = r["raw_line"] or ""
        # Extract ValidCard types
        m = re.search(r"'ValidCard':\s*'([^']+)'", raw)
        if not m:
            m = re.search(r"'ValidTarget':\s*'([^']+)'", raw)
        if not m:
            continue
        valid_card = m.group(1)
        # Check overlap: prefer specific type match, fall back to generic "Card"
        matched_type = next((wt for wt in sorted(wanted_types) if wt in valid_card), "")
        if not matched_type and valid_card.split(".")[0].split(",")[0].strip() == "Card":
            matched_type = next(iter(sorted(wanted_types)))
        if not matched_type:
            continue
        seen.add(name)
        results.append(
            PortComplement(
                rule_id="cost_reducer",
                direction="synergy",
                candidate=name,
                cmdr_event=f"SpellCast_{matched_type}",
                cand_event=f"ReduceCost_{matched_type}",
                filter_group=matched_type,
            )
        )

    return results


def _find_graveyard_play_synergy(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
) -> list[PortComplement]:
    """Find MayPlay-from-Graveyard enablers for landfall/GY commanders.

    Omnath triggers on Land entering -> Crucible of Worlds lets you play
    lands from graveyard (more landfall triggers from fetch lands).
    Muldrotha wants to cast permanents from GY -> Crucible covers the
    land slot.

    Narrow: only ~42 cards have MayPlay + Graveyard + Land.
    """
    # Detect landfall commanders
    has_landfall = False
    has_gy_scale = False
    for p in cmdr_ports:
        pt = (p.get("port_type") or "").strip()
        ev = (p.get("event_class") or "").strip()
        vf = p.get("valid_filter") or ""
        if pt == "trigger" and ev == "ChangesZone" and "Land" in vf:
            zd = (p.get("zone_destination") or "").strip() or "Battlefield"
            if zd == "Battlefield":
                has_landfall = True
        if pt == "scales_with" and ("Land" in ev or "land" in ev):
            has_gy_scale = True

    if not has_landfall and not has_gy_scale:
        return []

    # Find MayPlay + Graveyard + Land statics
    cur = conn.execute(
        "SELECT DISTINCT card_name FROM card_ports "
        "WHERE port_type = 'static' AND event_class = 'Continuous' "
        "AND raw_line LIKE '%MayPlay%' AND raw_line LIKE '%Graveyard%' "
        "AND raw_line LIKE '%Land%'"
    )
    results: list[PortComplement] = []
    for r in cur.fetchall():
        name = r["card_name"]
        if name not in cmdr_set:
            results.append(
                PortComplement(
                    rule_id="graveyard_play",
                    direction="synergy",
                    candidate=name,
                    cmdr_event="landfall",
                    cand_event="MayPlay_Land_GY",
                )
            )

    return results


def _find_affinity_archetype(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
) -> list[PortComplement]:
    """Affinity-keyword commanders want many permanents of the affinity
    type. Emry, Lurker of the Loch has Affinity:Artifact -> she benefits
    from cheap artifacts, artifact lands, and artifact-cost reducers.

    Narrow gate: commander has a ``Affinity:<Type>`` keyword port.

    Three cand_event buckets with distinct IDF:
    - ``typed_land``: Land cards of the affinity type (Seat of the Synod,
      Darksteel Citadel for Artifact). ~22 cards, very high IDF.
    - ``cost_reducer``: ReduceCost statics targeting the type (Etherium
      Sculptor, Foundry Inspector).
    - ``cheap_typed``: CMC 0-1 non-land permanents of the type (Mishra's
      Bauble, Lotus Petal, Chromatic Star). ~300 cards, medium IDF.

    The affinity-type subtype isn't used as a filter directly — the
    common case is Affinity:Artifact, so we read the keyword suffix
    and match `card_types LIKE '%<Type>%'`. For non-Artifact Affinity
    (e.g. Equipment) the rule works the same way.
    """
    affinity_type = ""
    for p in cmdr_ports:
        if (p.get("port_type") or "").strip() != "keyword":
            continue
        ev = (p.get("event_class") or "").strip()
        if ev.startswith("Affinity:"):
            raw_type = ev.split(":", 1)[1].strip()
            # Split on "." to drop compound subtype qualifiers and keep the
            # base type ("Creature.Artifact:artifact" → "Creature").
            base = raw_type.split(".", 1)[0].strip()
            if base in _AFFINITY_RECOGNIZED_TYPES:
                affinity_type = base
                break

    # Extended gate for Artifact: commanders without Affinity:Artifact but
    # whose core mechanic involves artifacts (Sharuum reanimates artifacts,
    # Breya sacrifices artifacts, Osgir sacs artifacts for +X/+0, Urza taps
    # artifacts for mana) share the same canonical support pool — cheap
    # artifacts, artifact lands, cost reducers.
    #
    # Require the cost/effect to be PURELY artifact — Mondrak's
    # ``Sac<2/Artifact.Other;Creature.Other>`` mixes artifacts with
    # creatures and isn't an artifact-matters commander. The ``;``
    # separator in Forge notation indicates alternative types.
    if not affinity_type:
        for p in cmdr_ports:
            pt = (p.get("port_type") or "").strip()
            ev = (p.get("event_class") or "").strip()
            vf = p.get("valid_filter") or ""
            raw = p.get("raw_line") or ""
            # Reanimate/cast-from-GY artifact (Sharuum, Emry)
            if pt == "effect" and ev in ("ChangeZone", "Effect") and "Artifact" in vf and ";" not in vf:
                affinity_type = "Artifact"
                break
            # Sacrifice/tap-type with only-Artifact cost (Breya, Osgir, Urza)
            if pt == "cost" and ev in ("sacrifice", "tap_type"):
                m = re.search(r"<\d+/([^/>]+)(?:/[^>]*)?>", raw)
                if m and "Artifact" in m.group(1) and ";" not in m.group(1):
                    affinity_type = "Artifact"
                    break
    if not affinity_type:
        return []

    results: list[PortComplement] = []
    seen: set[str] = set()
    type_pattern = f"%{affinity_type}%"

    def _add(name: str, cand_event: str) -> None:
        if name in cmdr_set or name in seen:
            return
        seen.add(name)
        results.append(
            PortComplement(
                rule_id="affinity_archetype",
                direction="synergy",
                candidate=name,
                cmdr_event=f"Affinity_{affinity_type}",
                cand_event=cand_event,
            )
        )

    # typed_land: Land cards of the affinity type
    cur = conn.execute(
        "SELECT name FROM cards WHERE card_types LIKE '%Land%' AND card_types LIKE ?",
        (type_pattern,),
    )
    for r in cur.fetchall():
        _add(r["name"], "typed_land")

    # cost_reducer: ReduceCost statics whose ValidCard hits the type
    cur = conn.execute(
        "SELECT DISTINCT card_name, raw_line FROM card_ports WHERE port_type = 'static' AND event_class = 'ReduceCost'"
    )
    for r in cur.fetchall():
        raw = r["raw_line"] or ""
        if f"'ValidCard': '{affinity_type}" in raw or f"'Type': '{affinity_type}" in raw:
            _add(r["card_name"], "cost_reducer")

    # cheap_typed: non-land permanents of the type at CMC <= 1
    cur = conn.execute(
        "SELECT name FROM cards WHERE card_types LIKE ? AND card_types NOT LIKE '%Land%' AND cmc <= 1",
        (type_pattern,),
    )
    for r in cur.fetchall():
        _add(r["name"], "cheap_typed")

    return results


#: Trigger-filter fragments that narrow the firing population enough
#: that edict effects (forcing opponents to sacrifice creatures) no
#: longer reliably feed the trigger. Audit 2026-04-24 showed rules
#: with these filters hitting edict_feeder's 153-touched set but ALL
#: in the displacement tail — Toxrill (``counters_GE1_SLIME``),
#: Koll (``+enchanted``/``+equipped``), Donatello (``HasCounters``),
#: Jaws (``Artifact.nonCreature``), Astrid (``Food``/``Clue``).
#: Generic edicts produce deaths of plain creatures without these
#: modifiers, so the triggers don't actually fire.
#:
#: Imported by ``complement_rules.registry._edict_feeder_gate`` so
#: the audit touched-set and runtime helper share one source of
#: truth.
EDICT_GATE_REJECTING_FRAGMENTS: tuple[str, ...] = (
    "counters_GE",
    "counters_EQ",
    "counters_LE",
    "+enchanted",
    "+equipped",
    "HasCounters",
    "Food",
    "Clue",
    "Treasure",
    "Blood",
)


def _edict_benefits_from_trigger(vf: str) -> bool:
    """Return True if a trigger with valid_filter ``vf`` would plausibly
    fire when an opponent is forced to sacrifice a creature by an
    edict effect.

    Rejects narrow mechanical filters that depend on conditions edicts
    don't produce (specific counters, attached auras/equipment,
    non-creature artifact subtypes).
    """
    if any(frag in vf for frag in EDICT_GATE_REJECTING_FRAGMENTS):
        return False
    # Edicts target creatures — reject filters whose main type is
    # Artifact / Enchantment *without* Creature as a separate type.
    # Substring "Creature" inside "nonCreature" doesn't count as the
    # type being present (Jaws, Relentless Predator has
    # ``Artifact.nonCreature`` — edicts don't sac non-creature
    # artifacts).
    has_creature_type = "Creature" in vf and "nonCreature" not in vf
    if vf.startswith("Artifact") and not has_creature_type:
        return False
    if vf.startswith("Enchantment") and not has_creature_type:  # noqa: SIM103
        return False
    return True


def _find_edict_feeders(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
) -> list[PortComplement]:
    """Find edict effects for death-trigger commanders.

    Meren triggers on creatures dying -> Plaguecrafter's ETB forces
    each player to sacrifice a creature, feeding Meren's trigger.
    Tergrid triggers on opponent Sacrificed -> edicts force opponents
    to sacrifice, handing Tergrid permanents from the graveyard.

    Edicts are Sacrifice/SacrificeAll effects targeting Player/Opponent/Each.
    N ≈ 100-200 (edict effects in Magic).

    Gate narrowing (2026-04-24): the trigger filter must plausibly
    fire from an opponent-creature sacrifice. Excludes narrow
    mechanical filters that edicts don't satisfy — counter-gated
    triggers (Toxrill/Donatello), enchanted/equipped-only filters
    (Koll), and non-creature artifact subtypes (Jaws/Astrid).
    See ``_edict_benefits_from_trigger`` for the full predicate.
    """
    # Detect death-trigger commanders with edict-compatible filters
    has_death_trigger = False
    has_sacrificed_trigger = False
    for p in cmdr_ports:
        pt = (p.get("port_type") or "").strip()
        ev = (p.get("event_class") or "").strip()
        if pt != "trigger":
            continue
        vf = p.get("valid_filter") or ""
        if ev == "ChangesZone":
            zd = (p.get("zone_destination") or "").strip()
            if zd == "Graveyard" and not _trigger_only_matches_self(vf) and _edict_benefits_from_trigger(vf):
                has_death_trigger = True
        if ev == "Sacrificed" and _edict_benefits_from_trigger(vf):
            has_sacrificed_trigger = True

    if not has_death_trigger and not has_sacrificed_trigger:
        return []

    cmdr_event = "Sacrificed" if has_sacrificed_trigger else "ChangesZone_death"

    # Find edict effects: Sacrifice/SacrificeAll targeting opponents/each player.
    # Safety: placeholders are "?,?,..." from len() of a module-level constant;
    # all values bound via params — no user input enters the SQL string.
    placeholders = ",".join("?" * len(_EDICT_FILTERS))
    cur = conn.execute(
        f"SELECT DISTINCT card_name FROM card_ports "
        f"WHERE port_type = 'effect' "
        f"AND event_class IN ('Sacrifice', 'SacrificeAll') "
        f"AND valid_filter IN ({placeholders})",
        _EDICT_FILTERS,
    )
    results: list[PortComplement] = []
    for r in cur.fetchall():
        name = r["card_name"]
        if name not in cmdr_set:
            results.append(
                PortComplement(
                    rule_id="edict_feeder",
                    direction="synergy",
                    candidate=name,
                    cmdr_event=cmdr_event,
                    cand_event="Sacrifice_edict",
                )
            )

    return results


#: Creature-token scripts carry a P/T segment (``w_1_1_human``,
#: ``c_0_1_a_thopter``); Treasure/Food/Clue scripts do not. Gate for
#: anthem_payoff's commander-side token-production check.
_CREATURE_TOKEN_PT_RE = re.compile(r"_\d+_\d+")

_TOKENSCRIPT_RE = re.compile(r"'TokenScript':\s*'([^']+)'")

#: Buff keys an anthem must add for the payoff to be real; a negative
#: value (drawback statics) disqualifies the port.
_ANTHEM_BUFF_KEYS: tuple[str, ...] = ("'AddPower'", "'AddToughness'", "'AddKeyword'")


def _commander_makes_creature_tokens(cmdr_ports: list[PortRow]) -> bool:
    """True iff some effect.Token port produces a creature token."""
    for p in cmdr_ports:
        if (p.get("port_type") or "").strip() != "effect":
            continue
        if (p.get("event_class") or "").strip() != "Token":
            continue
        m = _TOKENSCRIPT_RE.search(str(p.get("raw_line") or ""))
        if m and _CREATURE_TOKEN_PT_RE.search(m.group(1)):
            return True
    return False


_NEGATIVE_BUFF_RE = re.compile(r"'Add(?:Power|Toughness)':\s*'-")

#: Affected-scope base types that make a static a *team* anthem (vs a
#: creature-subtype lord, which stays lord's territory, or Card.Self voltron).
_TEAM_ANTHEM_BASES: frozenset[str] = frozenset({"Creature", "Permanent"})

#: Affected-scope qualifier tokens that do NOT restrict a static from being an
#: unconditional whole-team anthem. Anything else after the base type — a
#: creature subtype (``Pirate``), a condition (``counters_GE1_P1P1``,
#: ``tapped``, ``enchanted``), or a color (``Black``) — makes it a restricted
#: lord / conditional anthem, which is NOT a team anthem and stays out. Without
#: this check a subtype/condition folded in as a ``+``-qualifier after a
#: ``Creature``/``Permanent`` base (e.g. Admiral Beckett Brass's
#: ``Creature.Pirate+Other+YouCtrl``, Abzan Falconer's
#: ``Creature.YouCtrl+counters_GE1_P1P1``) would slip through.
_BENIGN_TEAM_SCOPE_QUALIFIERS: frozenset[str] = frozenset({"YouCtrl", "Other"})


def _parse_scope_alt(alt: str) -> tuple[str, list[str]]:
    """Split one ``affected_scope`` alternative into (base type, qualifier tokens).

    ``'Creature.Pirate+Other+YouCtrl'`` -> ``('Creature', ['Pirate', 'Other',
    'YouCtrl'])``; ``'Creature.YouCtrl'`` -> ``('Creature', ['YouCtrl'])``;
    ``'Card.Self'`` -> ``('Card', ['Self'])``. Splits on both ``.`` and ``+``;
    the base is the first token. Shared by ``_find_anthem_payoffs`` and
    ``_commander_has_team_anthem_static`` so the two scope parsers cannot drift.
    """
    tokens = [t for t in re.split(r"[.+]", alt.strip()) if t]
    if not tokens:
        return "", []
    return tokens[0], tokens[1:]


def _commander_has_team_anthem_static(cmdr_ports: list[PortRow]) -> bool:
    """True iff the commander has a your-team anthem static (Unit 1 gate).

    Qualifies iff some ``static.Continuous`` port grants a positive
    ``AddPower``/``AddToughness`` or an ``AddKeyword`` to an UNCONDITIONAL
    ``Creature``/``Permanent`` team scope: base in ``_TEAM_ANTHEM_BASES``,
    ``YouCtrl`` present, and every other affected-scope qualifier benign
    (``_BENIGN_TEAM_SCOPE_QUALIFIERS``). Rejects symmetric anthems (no
    ``YouCtrl``), ``Card.Self`` voltron, creature-*subtype* lords AND any
    subtype/condition qualifier folded after the base, and negative drawback
    statics. This is the mirror boundary of ``_find_anthem_payoffs``.
    """
    for p in cmdr_ports:
        if (p.get("port_type") or "").strip() != "static":
            continue
        if (p.get("event_class") or "").strip() != "Continuous":
            continue
        raw = str(p.get("raw_line") or "")
        if not any(k in raw for k in _ANTHEM_BUFF_KEYS):
            continue
        if _NEGATIVE_BUFF_RE.search(raw):
            continue
        for alt in (p.get("affected_scope") or "").split(","):
            base, quals = _parse_scope_alt(alt)
            if base not in _TEAM_ANTHEM_BASES:
                continue
            if "YouCtrl" not in quals:
                continue
            if any(q not in _BENIGN_TEAM_SCOPE_QUALIFIERS for q in quals):
                continue
            return True
    return False


def _find_anthem_payoffs(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
) -> list[PortComplement]:
    """Type-scoped anthems for creature-token producers.

    Plan 2026-07-02-002 Unit 10: the lord rule only matches statics
    whose affected_scope names a SUBTYPE the commander references —
    global anthems (``Creature.YouCtrl`` Cathars'-Crusade shapes) were
    part of the 159-card static.Continuous NO_RULES block. Gated the
    way axis-feeders are: the commander must provably supply the axis
    (creature-token production), so this does not become a new
    flat-noise family (the ward_2_tribal lesson). IDF-weighted, keyed
    per affected-scope shape so specificity has real granularity.

    Tribal-scoped anthems (``Goblin.YouCtrl``) stay lord's territory —
    the base must be the Creature TYPE here, which also prevents
    double counting between the two rules.
    """
    if not _commander_makes_creature_tokens(cmdr_ports):
        return []

    results: list[PortComplement] = []
    seen: set[str] = set()
    cur = conn.execute(
        "SELECT card_name, affected_scope, raw_line, branch_kind FROM card_ports "
        "WHERE port_type = 'static' AND event_class = 'Continuous' "
        "AND affected_scope IS NOT NULL AND affected_scope != ''"
    )
    for row in cur.fetchall():
        name = row["card_name"]
        if name in cmdr_set or name in seen:
            continue
        raw = str(row["raw_line"] or "")
        if not any(k in raw for k in _ANTHEM_BUFF_KEYS):
            continue
        # Drawback statics pump negatively — not a payoff.
        if _NEGATIVE_BUFF_RE.search(raw):
            continue
        scope = row["affected_scope"] or ""
        matched_alt: str | None = None
        for alt in scope.split(","):
            alt = alt.strip()
            base, _quals = _parse_scope_alt(alt)
            if base != "Creature":
                continue
            # Own-board anthems only: an anthem for opponents'
            # creatures (or one with no controller scope at all,
            # which pumps every board equally) is not a payoff.
            if "YouCtrl" not in alt:
                continue
            matched_alt = alt
            break
        if matched_alt is None:
            continue
        seen.add(name)
        # Coarse two-value key: full scope strings fragment into
        # tiny-N IDF keys that saturate toward weight 1.0 (the
        # high-cardinality granularity trap) — first measurement
        # flooded 35 cliffs (Krenko -0.357) exactly that way.
        cand_event = "token_anthem" if ".token" in matched_alt else "creature_anthem"
        results.append(
            PortComplement(
                rule_id="anthem_payoff",
                direction="synergy",
                candidate=name,
                cmdr_event="token_go_wide",
                cand_event=cand_event,
                branch_kind=row["branch_kind"] or "root",
            )
        )
    return results


#: ``ReplaceWith`` results that MULTIPLY the controller's own token creation
#: (so they multiply creature bodies a team anthem can buff). Excludes
#: ``HalveToken`` (Halving Season — reduces, inverted polarity) and the
#: pass-through replacements ``TokenReplace``/``DBReplace`` used by
#: non-creature-only makers (Academy Manufactor: Clue/Food/Treasure; Xorn:
#: Treasure) which are not creature-body multipliers.
_TOKEN_MULTIPLYING_REPLACEMENTS: frozenset[str] = frozenset({"DoubleToken", "TripleToken"})
_REPLACEWITH_RE = re.compile(r"'ReplaceWith':\s*'([^']+)'")
_TOKENOWNER_RE = re.compile(r"'TokenOwner':\s*'([^']+)'")

#: ``TokenOwner`` values that hand the token to a non-controlling player, so the
#: body lands off the anthem commander's board (Akroan Horse, Forbidden Orchard,
#: the Hunted cycle). ``Opponent`` is matched as a substring (covers
#: ``Player.Opponent`` and compound owners); these are matched exactly.
_AWAY_TOKEN_OWNERS: frozenset[str] = frozenset({"Targeted", "TargetedPlayer", "ThisTargetedPlayer"})


def _is_own_board_token_multiplier(raw: str) -> bool:
    """True iff a ``replacement.CreateToken`` port multiplies the controller's
    OWN tokens (Doubling Season, Ojer Taq) rather than halving, replacing a
    non-creature token, or affecting an opponent's tokens."""
    m = _REPLACEWITH_RE.search(raw)
    if m is None or m.group(1) not in _TOKEN_MULTIPLYING_REPLACEMENTS:
        return False
    return "Card.OppCtrl" not in raw and "'ValidPlayer': 'Opponent'" not in raw


def _token_lands_on_own_board(raw: str) -> bool:
    """True iff a ``effect.Token`` port's created token is owned by the
    controller (the anthem can buff it). ``TokenOwner`` absent defaults to the
    controller; an ``Opponent``/``Targeted`` owner lands the body elsewhere."""
    m = _TOKENOWNER_RE.search(raw)
    if m is None:
        return True
    owner = m.group(1)
    return "Opponent" not in owner and owner not in _AWAY_TOKEN_OWNERS


def _find_team_anthem_payoffs(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
    candidate_cache: CandidateCache | None = None,
) -> list[PortComplement]:
    """Token-producer payoffs for passive team-anthem commanders (Unit 2).

    Fires when the commander has a qualifying team-anthem static (Unit 1).
    Candidates are OWN-BOARD creature-token makers, tiered ``token_doubler``
    (strong, ``replacement.CreateToken`` multipliers) > ``token_producer``
    (``effect.Token`` with a creature P/T TokenScript). Both tiers exclude
    opponent-directed / inverted-polarity / non-creature makers so the anthem
    only pairs with bodies it can actually buff. Dedup per candidate, strong
    tier wins.

    When ``candidate_cache`` is provided, the producer tier reads the shared
    ``token_effect_rows`` instead of re-issuing the (commander-independent)
    ``effect.Token`` scan for every commander in a batch run (see
    ``_find_subtype_supply_complements`` for the established pattern).
    """
    if not _ENABLE_TEAM_ANTHEM_PAYOFF:
        return []
    if not _commander_has_team_anthem_static(cmdr_ports):
        return []

    results: list[PortComplement] = []
    seen: set[str] = set()

    def _emit(name: str, cand_event: str) -> None:
        if name in cmdr_set or name in seen:
            return
        seen.add(name)
        results.append(
            PortComplement(
                rule_id="team_anthem_payoff",
                direction="synergy",
                candidate=name,
                cmdr_event="team_anthem",
                cand_event=cand_event,
            )
        )

    # Strong tier first so dedup keeps the doubler credit.
    for name, raw in conn.execute(
        "SELECT DISTINCT card_name, raw_line FROM card_ports "
        "WHERE port_type = 'replacement' AND event_class = 'CreateToken'"
    ).fetchall():
        if _is_own_board_token_multiplier(str(raw or "")):
            _emit(name, "token_doubler")

    if candidate_cache is not None:
        token_rows: list[tuple[str, str]] = list(candidate_cache.token_effect_rows)
    else:
        token_rows = [
            (row["card_name"], row["raw_line"] or "")
            for row in conn.execute(
                "SELECT DISTINCT card_name, raw_line FROM card_ports "
                "WHERE port_type = 'effect' AND event_class = 'Token'"
            ).fetchall()
        ]
    for name, raw_line in token_rows:
        raw = str(raw_line or "")
        m = _TOKENSCRIPT_RE.search(raw)
        if not (m and _CREATURE_TOKEN_PT_RE.search(m.group(1))):
            continue
        if not _token_lands_on_own_board(raw):
            continue
        _emit(name, "token_producer")

    return results
