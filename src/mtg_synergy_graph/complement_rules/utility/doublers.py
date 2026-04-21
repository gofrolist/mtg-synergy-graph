"""Damage / mana doublers and cascade-value payoff."""

from __future__ import annotations

import re
import sqlite3

from ..core import (
    PortComplement,
    PortRow,
)
from ._shared import (
    _DAMAGE_AMP_RESULTS,
    _PING_TRIGGER_EVENTS,
)


def _find_damage_doubler_synergy(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
) -> list[PortComplement]:
    """Find synergies for damage-amplifier replacement-effect commanders.

    Torbran's ``+2`` static, Gisela / Solphim's ``double`` statics, and
    Tor Wauki / Raphael / Wolverine's similar replacements all share a
    mechanical shape: ``replacement.DamageDone`` with a damage-amp
    ``replacement_result`` (DmgTwice / DmgTriple / DmgPlus*) targeting
    opponent / opponent-permanent. The static is the commander's
    finisher — every damage source becomes more lethal.

    Two tiers:

    - ``damage_amp_stack``: candidates carrying their own opponent-
      facing damage-amp replacement (Furnace of Rath, Fiery
      Emancipation, Dictate of the Twin Gods, Curse of Bloodletting,
      Angrath's Marauders, City on Fire). Multiplicative stacking
      makes each pair of doublers worth disproportionately more than
      either alone — the highest-priority match for these commanders.
    - ``damage_pinger``: candidates with a non-combat repeating
      trigger (SpellCast / Phase / LandPlayed / TapsForMana / Drawn /
      Discarded / Sacrificed / ChangesZone) AND an effect=DealDamage
      port targeting Player / Opponent. Guttersnipe, Firebrand
      Archer, Thermo-Alchemist, Manabarbs, Sulfuric Vortex,
      Kessig Flamebreather, Storm-Kiln Artist. The amplifier turns
      each ping into a serious damage clock.

    Rejected commander shapes:

    - Pure prevention statics (Iroas, Tajic, Emmara, Frodo) — their
      replacement_result is ``Prevent`` or has ``PreventionEffect:
      True`` / ``Prevent: True``. Different mechanical axis.
    - Self-target replacements (Dralnu's "damage to me → sacrifice
      permanents", Polukranos's "damage to me → counters") — the
      ``ValidTarget`` is ``Card.Self`` / ``You`` / ``Permanent.YouCtrl``
      with no opponent-facing target. These are damage-routing
      commanders, not amplifiers.
    - Damage-decreasing replacements (DmgMinus1, DmgHalfDown) — same
      rejection as preventers.
    """
    has_amp = False
    for p in cmdr_ports:
        if (p.get("port_type") or "").strip() != "replacement":
            continue
        if (p.get("event_class") or "").strip() != "DamageDone":
            continue
        result = (p.get("replacement_result") or "").strip()
        if result not in _DAMAGE_AMP_RESULTS:
            continue
        raw = str(p.get("raw_line") or "")
        # Reject any prevention flag — amp + prevent never coexist on
        # the same port, but defensive check keeps the gate clean.
        if "'Prevent': 'True'" in raw or "'PreventionEffect': 'True'" in raw:
            continue
        # Require an opponent-facing ValidTarget — self-only targets
        # describe damage routing (Dralnu / Polukranos), not amps.
        if not _replacement_targets_opponent(raw):
            continue
        has_amp = True
        break
    if not has_amp:
        return []

    amp_placeholders = ",".join("?" * len(_DAMAGE_AMP_RESULTS))
    amp_list = sorted(_DAMAGE_AMP_RESULTS)
    amp_set: set[str] = {
        row["card_name"]
        for row in conn.execute(
            "SELECT DISTINCT card_name FROM card_ports "
            "WHERE port_type = 'replacement' "
            "AND event_class = 'DamageDone' "
            f"AND replacement_result IN ({amp_placeholders}) "
            "AND raw_line NOT LIKE ? "
            "AND raw_line NOT LIKE ?",
            (*amp_list, "%'Prevent': 'True'%", "%'PreventionEffect': 'True'%"),
        )
    }

    ping_placeholders = ",".join("?" * len(_PING_TRIGGER_EVENTS))
    ping_list = sorted(_PING_TRIGGER_EVENTS)
    ping_set: set[str] = {
        row["card_name"]
        for row in conn.execute(
            "SELECT DISTINCT cp_eff.card_name "
            "FROM card_ports cp_eff "
            "JOIN card_ports cp_trig ON cp_trig.card_name = cp_eff.card_name "
            "WHERE cp_eff.port_type = 'effect' AND cp_eff.event_class = 'DealDamage' "
            "AND (cp_eff.valid_filter LIKE '%Opponent%' "
            "     OR cp_eff.valid_filter LIKE '%Player%' "
            "     OR cp_eff.valid_filter LIKE '%Each%') "
            "AND cp_trig.port_type = 'trigger' "
            f"AND cp_trig.event_class IN ({ping_placeholders})",
            ping_list,
        )
    }

    tier_priority = (
        ("damage_amp_stack", amp_set),
        ("damage_pinger", ping_set),
    )
    seen: set[str] = set()
    results: list[PortComplement] = []
    for cand_event, candidates in tier_priority:
        for name in candidates:
            if name in cmdr_set or name in seen:
                continue
            seen.add(name)
            results.append(
                PortComplement(
                    rule_id="damage_doubler_synergy",
                    direction="synergy",
                    candidate=name,
                    cmdr_event="damage_amp",
                    cand_event=cand_event,
                )
            )
    return results


_OPPONENT_TARGET_SUBSTRINGS: tuple[str, ...] = (
    "Opponent",
    "OppCtrl",
    "Player.Opp",
)


def _replacement_targets_opponent(raw_line: str) -> bool:
    """True iff the replacement port's ``ValidTarget`` clause names
    an opponent or opponent-controlled permanent.
    """
    m = re.search(r"'ValidTarget':\s*'([^']+)'", raw_line)
    if not m:
        # No ValidTarget clause = unrestricted (Furnace of Rath shape:
        # all damage anywhere). Treat as opponent-targeting too — the
        # candidate-side query below will still narrow by replacement
        # result.
        return True
    val = m.group(1)
    return any(sub in val for sub in _OPPONENT_TARGET_SUBSTRINGS)


def _find_mana_doubler_synergy(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
) -> list[PortComplement]:
    """Find mana doublers for TapsForMana trigger commanders.

    Selvala triggers on TapsForMana -> Mana Reflection doubles output.
    Kinnan triggers on TapsForMana -> Nyxbloom Ancient triples mana.

    Matches replacement effects with ProduceMana (mana doublers/triplers).
    Very narrow: N ~ 15-30 (excellent IDF).
    """
    has_mana_trigger = any(
        (p.get("port_type") or "").strip() == "trigger" and (p.get("event_class") or "").strip() == "TapsForMana"
        for p in cmdr_ports
    )
    if not has_mana_trigger:
        return []

    cur = conn.execute(
        "SELECT DISTINCT card_name FROM card_ports "
        "WHERE port_type = 'replacement' AND replacement_event = 'ProduceMana'"
    )
    results: list[PortComplement] = []
    for r in cur.fetchall():
        name = r["card_name"]
        if name not in cmdr_set:
            results.append(
                PortComplement(
                    rule_id="mana_doubler",
                    direction="synergy",
                    candidate=name,
                    cmdr_event="TapsForMana",
                    cand_event="ProduceMana_doubler",
                )
            )

    return results


_CASCADE_VALUE_EFFECTS: tuple[str, ...] = (
    "Draw",
    "Destroy",
    "DestroyAll",
    "Token",
    "ChangeZone",
    "ChangeZoneAll",
    "DealDamage",
    "GainControl",
    "Mill",
    "Mana",
)


def _find_cascade_value(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
) -> list[PortComplement]:
    """High-CMC value spells for Cascade commanders.

    Maelstrom Wanderer cascades twice → wants expensive spells with
    powerful effects to hit. CMC 7+: ``cascade_high``; CMC 5-6:
    ``cascade_mid``. Low-CMC spells don't benefit from cascade.
    """
    has_cascade = any(
        (p.get("port_type") or "").strip() == "keyword" and "Cascade" in ((p.get("event_class") or "").strip())
        for p in cmdr_ports
    )
    if not has_cascade:
        return []

    ph = ",".join("?" * len(_CASCADE_VALUE_EFFECTS))
    # Cards with CMC 5+ and at least one valuable effect
    cur = conn.execute(
        "SELECT DISTINCT c.name, c.cmc FROM cards c "
        "WHERE c.cmc >= 5 "
        "AND c.name IN ("
        f"  SELECT card_name FROM card_ports "
        f"  WHERE port_type = 'effect' AND event_class IN ({ph})"
        ")",
        _CASCADE_VALUE_EFFECTS,
    )
    results: list[PortComplement] = []
    for r in cur.fetchall():
        name = r["name"]
        if name in cmdr_set:
            continue
        label = "cascade_high" if r["cmc"] >= 7 else "cascade_mid"
        results.append(
            PortComplement(
                rule_id="cascade_value",
                direction="synergy",
                candidate=name,
                cmdr_event="Cascade",
                cand_event="high_cmc_value",
                filter_group=label,
            )
        )
    return results
