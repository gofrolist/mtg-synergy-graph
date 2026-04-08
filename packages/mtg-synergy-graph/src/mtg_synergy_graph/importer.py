"""Forge cardsfolder importer (SPEC §5).

Walks a Forge ``cardsfolder/`` tree, parses each ``.txt``, and writes rows
into ``cards``, ``card_ports``, ``card_svars`` and ``port_attributes``.
Designed to be re-runnable: ``import_cards_folder`` clears the four target
tables for any card it touches before re-inserting.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from .attributes import explode_filter
from .parser import parse_card_file
from .ports import extract_all_ports

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Card row construction
# ---------------------------------------------------------------------------

# Pip-letter sets used to derive a colour-identity proxy from ManaCost when
# Forge does not give us an explicit Colors: line. This is a starter — full
# colour identity (including activated abilities, hybrid pips, etc.) lives
# in Phase 2.
_MANA_PIPS = {"W", "U", "B", "R", "G"}

# Forge's ``Colors:`` line uses full colour words (``black,green``), not pip
# letters. Required for cards with ``ManaCost:no cost`` (suspend cards,
# back-faces, planeswalkers without a cast cost) where the Colors line is the
# only colour-identity source.
_COLOR_WORD_TO_PIP = {
    "white": "W",
    "blue":  "U",
    "black": "B",
    "red":   "R",
    "green": "G",
}


def _derive_cmc(mana_cost: str | None) -> float | None:
    """Best-effort CMC from a Forge ManaCost string ('3 W W' → 5.0).

    ``no cost`` (suspend cards, back-faces, costless planeswalkers) returns
    ``None`` rather than the literal token count of 2.
    """
    if not mana_cost or mana_cost == "no cost":
        return None
    total = 0.0
    for tok in mana_cost.split():
        if tok.isdigit():
            total += float(tok)
        elif tok == "X":
            continue
        else:
            # Each non-numeric token = 1 mana (single pip / hybrid / phyrexian)
            total += 1.0
    return total


def _derive_colors(mana_cost: str | None, colors_line: str | None) -> str:
    """Comma-separated W,U,B,R,G subset present in mana cost (or Colors: line).

    Forge's ``Colors:`` line uses full colour words (``black,green``); the
    ``ManaCost:`` line uses pip letters (``2 G G``). Both are folded into the
    same pip set here.
    """
    pips: set[str] = set()
    if colors_line:
        for raw_word in colors_line.replace(",", " ").split():
            word = raw_word.strip().lower()
            pip = _COLOR_WORD_TO_PIP.get(word)
            if pip is not None:
                pips.add(pip)
            elif raw_word.strip() in _MANA_PIPS:  # tolerate already-pip input
                pips.add(raw_word.strip())
    if mana_cost and mana_cost != "no cost":
        for tok in mana_cost.split():
            for ch in tok:
                if ch in _MANA_PIPS:
                    pips.add(ch)
    return ",".join(sorted(pips))


# Top-level card types recognised by the importer. The first set is the
# normal EDH-legal types; the second is the non-EDH set (Plane / Phenomenon /
# Scheme / Conspiracy / Vanguard / Dungeon) — they MUST still land in
# ``card_types`` so the engine's legality filter can hard-exclude them
# instead of treating them as ``card_types=''`` colourless permanents that
# slip through every check.
_LEGAL_CARD_TYPES = frozenset({
    "Artifact", "Creature", "Enchantment", "Instant",
    "Land", "Planeswalker", "Sorcery", "Tribal", "Battle",
})
_NONLEGAL_CARD_TYPES = frozenset({
    "Plane", "Phenomenon", "Scheme", "Conspiracy", "Vanguard", "Dungeon",
})
_ALL_CARD_TYPES = _LEGAL_CARD_TYPES | _NONLEGAL_CARD_TYPES


def _split_types(types_line: str | None) -> tuple[str, str, str, str]:
    """Decompose a Forge ``Types:`` line into supertypes / card types / subtypes / raw.

    Forge convention: tokens before the recognised card-type words are
    supertypes, tokens after are subtypes.
    """
    if not types_line:
        return "", "", "", ""
    tokens = types_line.split()
    card_types = [t for t in tokens if t in _ALL_CARD_TYPES]
    supertypes = [t for t in tokens if t in {"Legendary", "Basic", "Snow", "World"}]
    subtypes = [t for t in tokens if t not in card_types and t not in supertypes]
    return (
        " ".join(supertypes),
        " ".join(card_types),
        " ".join(subtypes),
        types_line,
    )


def _card_row(card: dict[str, Any]) -> dict[str, Any]:
    types_line = card.get("types")
    supertypes, card_types, subtypes, raw_types = _split_types(types_line)

    pt = card.get("pt") or ""
    power, _, toughness = pt.partition("/")

    keywords_json = json.dumps(card.get("keywords", []))
    deck_hints_json = json.dumps(card.get("deck_hints")) if card.get("deck_hints") else None
    deck_needs_json = json.dumps(card.get("deck_needs")) if card.get("deck_needs") else None
    deck_has_json = json.dumps(card.get("deck_has")) if card.get("deck_has") else None

    colors = _derive_colors(card.get("mana_cost"), card.get("colors"))

    return {
        "name":           card.get("name", ""),
        "mana_cost":      card.get("mana_cost"),
        "cmc":            _derive_cmc(card.get("mana_cost")),
        "types":          raw_types,
        "supertypes":     supertypes,
        "subtypes":       subtypes,
        "card_types":     card_types,
        "colors":         colors,
        "color_identity": colors,  # placeholder until full identity logic lands
        "power":          power or None,
        "toughness":      toughness or None,
        "loyalty":        card.get("loyalty"),
        "keywords":       keywords_json,
        "oracle_text":    card.get("oracle"),
        "is_commander":   False,
        "deck_hints":     deck_hints_json,
        "deck_needs":     deck_needs_json,
        "deck_has":       deck_has_json,
        "edhrec_rank":    None,
        "rarity":         None,
        "set_code":       None,
    }


_CARD_INSERT_SQL = """
INSERT OR REPLACE INTO cards (
    name, mana_cost, cmc, types, supertypes, subtypes, card_types,
    colors, color_identity, power, toughness, loyalty, keywords,
    oracle_text, is_commander, deck_hints, deck_needs, deck_has,
    edhrec_rank, rarity, set_code
) VALUES (
    :name, :mana_cost, :cmc, :types, :supertypes, :subtypes, :card_types,
    :colors, :color_identity, :power, :toughness, :loyalty, :keywords,
    :oracle_text, :is_commander, :deck_hints, :deck_needs, :deck_has,
    :edhrec_rank, :rarity, :set_code
)
"""

# Every column on card_ports that the extractors may emit. Missing keys are
# bound as NULL via dict.get().
_PORT_COLUMNS = (
    "card_name", "port_type", "event_class", "valid_filter",
    "zone_origin", "zone_destination", "phase", "affected_scope",
    "effect_zone", "cost_subtype", "amount", "counter_type",
    "granted_keyword", "granted_ability", "execute_ref", "sub_ability_ref",
    "is_conditional", "branch_kind", "branch_parent", "source_svar",
    "chain_depth", "scaling_expression", "is_optional", "is_combat",
    "is_curse", "replacement_event", "replacement_result",
    "replacement_player", "duration", "raw_line",
)

_PORT_INSERT_SQL = (
    "INSERT INTO card_ports ("
    + ", ".join(_PORT_COLUMNS)
    + ") VALUES ("
    + ", ".join(f":{c}" for c in _PORT_COLUMNS)
    + ")"
)


def _normalise_port(port: dict[str, Any]) -> dict[str, Any]:
    return {col: port.get(col) for col in _PORT_COLUMNS}


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def import_card(conn: sqlite3.Connection, card: dict[str, Any]) -> int:
    """Import a single parsed card. Returns number of port rows inserted.

    Idempotent — drops any existing rows for this card before reinserting.
    """
    name = card.get("name")
    if not name:
        return 0

    # Order matters: port_attributes references card_ports(id), so we must
    # delete attributes before clearing the parent ports.
    conn.execute(
        "DELETE FROM port_attributes WHERE port_id IN "
        "(SELECT id FROM card_ports WHERE card_name = ?)",
        (name,),
    )
    conn.execute("DELETE FROM card_ports WHERE card_name = ?", (name,))
    conn.execute("DELETE FROM card_svars WHERE card_name = ?", (name,))

    conn.execute(_CARD_INSERT_SQL, _card_row(card))

    for svar_name, svar_value in card.get("svars", {}).items():
        conn.execute(
            "INSERT OR REPLACE INTO card_svars (card_name, svar_name, svar_value) "
            "VALUES (?, ?, ?)",
            (name, svar_name, svar_value),
        )

    ports = extract_all_ports(card)
    inserted = 0
    for port in ports:
        cur = conn.execute(_PORT_INSERT_SQL, _normalise_port(port))
        port_id = cur.lastrowid
        inserted += 1
        for attr in explode_filter(port.get("valid_filter") or ""):
            conn.execute(
                "INSERT OR IGNORE INTO port_attributes "
                "(port_id, attr_kind, attr_value, is_negated) VALUES (?, ?, ?, ?)",
                (port_id, attr["attr_kind"], attr["attr_value"], attr["is_negated"]),
            )

    return inserted


def import_cards(
    conn: sqlite3.Connection,
    cards: Iterable[dict[str, Any]],
) -> int:
    """Import a sequence of already-parsed cards inside a single transaction."""
    total_ports = 0
    with conn:
        for card in cards:
            total_ports += import_card(conn, card)
    return total_ports


def import_cards_folder(
    conn: sqlite3.Connection,
    folder: str | Path,
    *,
    limit: int | None = None,
) -> tuple[int, int]:
    """Walk a Forge ``cardsfolder/`` tree and import every ``.txt``.

    Returns ``(card_count, port_count)``.
    """
    folder = Path(folder)
    txt_files = sorted(folder.rglob("*.txt"))
    if limit is not None:
        txt_files = txt_files[:limit]

    card_count = 0
    port_count = 0
    with conn:
        for path in txt_files:
            try:
                card = parse_card_file(path)
            except (OSError, UnicodeDecodeError, ValueError) as exc:
                # OSError       — read failures, permission denied
                # UnicodeDecode — non-UTF8 cardsfolder files
                # ValueError    — malformed structured fields parsed by
                #                 parse_forge_line / parse_deck_hints
                # Programming errors (AttributeError, KeyError) MUST
                # propagate so regressions in the parser fail loudly.
                log.warning("skipping unparseable card %s: %s", path, exc)
                continue
            if not card.get("name"):
                continue
            port_count += import_card(conn, card)
            card_count += 1
    return card_count, port_count
