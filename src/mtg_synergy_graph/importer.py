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
from typing import Any

from .attributes import explode_filter
from .parser import parse_card_file
from .ports import extract_all_ports

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Scryfall metadata resolver (4-tier, mirrors the legacy forge_name_map)
# ---------------------------------------------------------------------------
#
# Resolver value type: ``(oracle_id, edhrec_rank)``.
#
# ``edhrec_rank`` is Scryfall's global popularity index — lower rank =
# more commonly played across *all* commanders. We persist it alongside
# oracle_id so the engine can use it as a pure intra-tie tiebreaker:
# inside a cluster of cards with identical mechanical scores (e.g. the
# 62 cards at total=18 for Kyler), the more popular ones rise to the
# top. This *never* influences the displayed mechanical score — only
# the intra-tie ordering. Aligned with the "EDHREC as tiebreaker only"
# rule banked in ~/.claude/memory/feedback_edhrec_hivemind.md.

#: Tier descriptions in the order the resolver tries them.
_RESOLVER_TIERS = (
    "exact_non_token",
    "exact_any",
    "dfc_front_face",  # Forge stores "A", Scryfall stores "A // B"
    "dfc_back_face",  # Forge has alternate_name "B", Scryfall stores "A // B"
)

#: Value type produced by the resolver: ``(oracle_id, edhrec_rank)``.
#: ``edhrec_rank`` is ``None`` when the Scryfall source row has no rank,
#: or when the scryfall DB schema pre-dates the column (tiny test fixtures).
ScryfallMeta = tuple[str, int | None]


def _build_oracle_id_resolver(
    scryfall_conn: sqlite3.Connection,
) -> dict[str, ScryfallMeta]:
    """Index the Scryfall ``cards`` table for fast name-based lookup.

    Returns a dict keyed by the canonical Scryfall name *and* — for
    DFC/MDFC cards — the front-face and back-face substrings split out
    of ``A // B`` forms. Values are :data:`ScryfallMeta` tuples
    ``(oracle_id, edhrec_rank)``.

    Resolution priority is encoded by bucket iteration order: exact
    non-token matches win, then exact any, then DFC front-face, then
    DFC back-face. ``dict.setdefault`` guarantees earlier tiers are
    never overwritten by later ones.

    The scryfall DB is permitted to lack ``type_line`` or
    ``edhrec_rank`` columns — the former disables the token-priority
    tier, the latter makes every entry's ``edhrec_rank`` ``None``.
    Both concessions exist for the tiny synthetic fixtures used in
    unit tests.
    """
    cols = {r[1] for r in scryfall_conn.execute("PRAGMA table_info(cards)").fetchall()}
    has_type_line = "type_line" in cols
    has_edhrec_rank = "edhrec_rank" in cols

    # Bucket each Scryfall row by priority so later tiers never clobber
    # earlier ones.
    by_name_non_token: dict[str, ScryfallMeta] = {}
    by_name_any: dict[str, ScryfallMeta] = {}
    by_front: dict[str, ScryfallMeta] = {}
    by_back: dict[str, ScryfallMeta] = {}

    select_cols = ["oracle_id", "name"]
    if has_type_line:
        select_cols.append("type_line")
    if has_edhrec_rank:
        select_cols.append("edhrec_rank")
    sql = "SELECT " + ", ".join(select_cols) + " FROM cards"

    for row in scryfall_conn.execute(sql):
        oracle_id = row[0]
        canonical = row[1]
        idx = 2
        type_line = row[idx] if has_type_line else ""
        if has_type_line:
            idx += 1
        raw_rank = row[idx] if has_edhrec_rank else None
        edhrec_rank = int(raw_rank) if raw_rank is not None else None

        if not oracle_id or not canonical:
            continue

        meta: ScryfallMeta = (oracle_id, edhrec_rank)
        is_token = bool(type_line) and "Token" in type_line
        if is_token:
            by_name_any.setdefault(canonical, meta)
        else:
            by_name_non_token.setdefault(canonical, meta)
            by_name_any.setdefault(canonical, meta)

        if " // " in canonical:
            front, _, back = canonical.partition(" // ")
            by_front.setdefault(front, meta)
            by_back.setdefault(back, meta)

    resolver: dict[str, ScryfallMeta] = {}
    for bucket in (by_name_non_token, by_name_any, by_front, by_back):
        for k, v in bucket.items():
            resolver.setdefault(k, v)
    return resolver


def _resolve_scryfall_meta(
    forge_name: str,
    alternate_name: str | None,
    resolver: dict[str, ScryfallMeta],
) -> ScryfallMeta | None:
    """Look up a Forge card's Scryfall metadata tuple.

    Tries the front face first, then the DFC back face. Returns
    ``None`` if nothing matched.
    """
    hit = resolver.get(forge_name)
    if hit is not None:
        return hit
    if alternate_name:
        hit = resolver.get(alternate_name)
        if hit is not None:
            return hit
    return None


def _resolve_oracle_id(
    forge_name: str,
    alternate_name: str | None,
    resolver: dict[str, ScryfallMeta],
) -> str | None:
    """Back-compat shim: return just the oracle_id from the full meta
    tuple. Prefer :func:`_resolve_scryfall_meta` in new code."""
    hit = _resolve_scryfall_meta(forge_name, alternate_name, resolver)
    return hit[0] if hit is not None else None


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
    "blue": "U",
    "black": "B",
    "red": "R",
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
_LEGAL_CARD_TYPES = frozenset(
    {
        "Artifact",
        "Creature",
        "Enchantment",
        "Instant",
        "Land",
        "Planeswalker",
        "Sorcery",
        "Tribal",
        "Battle",
    }
)
_NONLEGAL_CARD_TYPES = frozenset(
    {
        "Plane",
        "Phenomenon",
        "Scheme",
        "Conspiracy",
        "Vanguard",
        "Dungeon",
    }
)
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
        "name": card.get("name", ""),
        "oracle_id": card.get("oracle_id"),
        "mana_cost": card.get("mana_cost"),
        "cmc": _derive_cmc(card.get("mana_cost")),
        "types": raw_types,
        "supertypes": supertypes,
        "subtypes": subtypes,
        "card_types": card_types,
        "colors": colors,
        "color_identity": colors,  # placeholder until full identity logic lands
        "power": power or None,
        "toughness": toughness or None,
        "loyalty": card.get("loyalty"),
        "keywords": keywords_json,
        "oracle_text": card.get("oracle"),
        "is_commander": False,
        "deck_hints": deck_hints_json,
        "deck_needs": deck_needs_json,
        "deck_has": deck_has_json,
        "edhrec_rank": card.get("edhrec_rank"),
        "rarity": None,
        "set_code": None,
    }


_CARD_INSERT_SQL = """
INSERT OR REPLACE INTO cards (
    name, oracle_id, mana_cost, cmc, types, supertypes, subtypes, card_types,
    colors, color_identity, power, toughness, loyalty, keywords,
    oracle_text, is_commander, deck_hints, deck_needs, deck_has,
    edhrec_rank, rarity, set_code
) VALUES (
    :name, :oracle_id, :mana_cost, :cmc, :types, :supertypes, :subtypes, :card_types,
    :colors, :color_identity, :power, :toughness, :loyalty, :keywords,
    :oracle_text, :is_commander, :deck_hints, :deck_needs, :deck_has,
    :edhrec_rank, :rarity, :set_code
)
"""

# Every column on card_ports that the extractors may emit. Missing keys are
# bound as NULL via dict.get().
_PORT_COLUMNS = (
    "card_name",
    "port_type",
    "event_class",
    "valid_filter",
    "zone_origin",
    "zone_destination",
    "phase",
    "affected_scope",
    "effect_zone",
    "cost_subtype",
    "cost_target",
    "trigger_source",
    "mana_restriction",
    "amount",
    "counter_type",
    "granted_keyword",
    "granted_ability",
    "execute_ref",
    "sub_ability_ref",
    "is_conditional",
    "branch_kind",
    "branch_parent",
    "source_svar",
    "chain_depth",
    "scaling_expression",
    "is_optional",
    "is_combat",
    "is_curse",
    "replacement_event",
    "replacement_result",
    "replacement_player",
    "duration",
    "raw_line",
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


def import_card(
    conn: sqlite3.Connection,
    card: dict[str, Any],
    *,
    oracle_id_resolver: dict[str, ScryfallMeta] | None = None,
) -> int:
    """Import a single parsed card. Returns number of port rows inserted.

    Idempotent — drops any existing rows for this card before reinserting.
    When ``oracle_id_resolver`` is supplied, the card's ``oracle_id``
    *and* ``edhrec_rank`` are resolved via the 4-tier lookup and
    persisted into the corresponding ``cards`` columns.
    """
    name = card.get("name")
    if not name:
        return 0

    # Order matters: port_attributes references card_ports(id), so we must
    # delete attributes before clearing the parent ports.
    conn.execute(
        "DELETE FROM port_attributes WHERE port_id IN (SELECT id FROM card_ports WHERE card_name = ?)",
        (name,),
    )
    conn.execute("DELETE FROM card_ports WHERE card_name = ?", (name,))
    conn.execute("DELETE FROM card_svars WHERE card_name = ?", (name,))

    if oracle_id_resolver is not None and not card.get("oracle_id"):
        hit = _resolve_scryfall_meta(
            name,
            card.get("alternate_name"),
            oracle_id_resolver,
        )
        if hit is not None:
            card["oracle_id"] = hit[0]
            # Only overwrite edhrec_rank when not already set on the
            # parsed card (future-proofing: if parsers ever learn to
            # read rank directly from Forge data, that would take
            # priority).
            if card.get("edhrec_rank") is None:
                card["edhrec_rank"] = hit[1]

    conn.execute(_CARD_INSERT_SQL, _card_row(card))

    for svar_name, svar_value in card.get("svars", {}).items():
        conn.execute(
            "INSERT OR REPLACE INTO card_svars (card_name, svar_name, svar_value) VALUES (?, ?, ?)",
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


def import_cards_folder(
    conn: sqlite3.Connection,
    folder: str | Path,
    *,
    scryfall_db: str | Path | None,
    limit: int | None = None,
) -> tuple[int, int]:
    """Walk a Forge ``cardsfolder/`` tree and import every ``.txt``.

    ``scryfall_db`` must point at a Scryfall-shaped sqlite database with
    a ``cards(oracle_id TEXT, name TEXT[, type_line TEXT, edhrec_rank
    INTEGER])`` table — in practice the ECC-era ``data/tags.db``. The
    importer builds a 4-tier name→metadata resolver and writes both
    ``cards.oracle_id`` and ``cards.edhrec_rank`` for every Forge card
    it can match. Pass ``None`` *only* from tests where Scryfall
    metadata population is intentionally skipped; production callers
    must supply a real path.

    Returns ``(card_count, port_count)``.
    """
    folder = Path(folder)
    txt_files = sorted(folder.rglob("*.txt"))
    if limit is not None:
        txt_files = txt_files[:limit]

    resolver: dict[str, ScryfallMeta] | None = None
    if scryfall_db is not None:
        scryfall_path = Path(scryfall_db)
        if not scryfall_path.exists():
            raise FileNotFoundError(f"scryfall_db does not exist: {scryfall_path}")
        scryfall_conn = sqlite3.connect(scryfall_path)
        try:
            resolver = _build_oracle_id_resolver(scryfall_conn)
        finally:
            scryfall_conn.close()
        log.info(
            "oracle_id resolver built from %s: %d name keys",
            scryfall_path,
            len(resolver),
        )

    card_count = 0
    port_count = 0
    resolved = 0
    unresolved: list[str] = []
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
            name = card.get("name")
            if not name:
                continue
            port_count += import_card(
                conn,
                card,
                oracle_id_resolver=resolver,
            )
            card_count += 1
            if resolver is not None:
                if card.get("oracle_id"):
                    resolved += 1
                else:
                    unresolved.append(name)

    if resolver is not None:
        pct = (100.0 * resolved / card_count) if card_count else 0.0
        log.info(
            "oracle_id coverage: %d/%d cards resolved (%.1f%%)",
            resolved,
            card_count,
            pct,
        )
        ranked = conn.execute("SELECT COUNT(*) FROM cards WHERE edhrec_rank IS NOT NULL").fetchone()[0]
        rank_pct = (100.0 * ranked / card_count) if card_count else 0.0
        log.info(
            "edhrec_rank coverage: %d/%d cards ranked (%.1f%%)",
            ranked,
            card_count,
            rank_pct,
        )
        if unresolved:
            head = ", ".join(unresolved[:5])
            suffix = "" if len(unresolved) <= 5 else f" (+{len(unresolved) - 5} more)"
            log.warning(
                "%d cards have no oracle_id: %s%s",
                len(unresolved),
                head,
                suffix,
            )

    return card_count, port_count
