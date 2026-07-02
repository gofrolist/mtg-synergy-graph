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
from typing import Any, NamedTuple

from .attributes import explode_filter
from .copy_face_from import CopyFaceFromSummary, resolve_copy_face_from_references
from .parser import parse_card_file
from .ports import _parse_token_script, extract_all_ports

# Re-exported so call sites that imported these from ``importer`` (the
# historical home) continue to work. Both symbols now live in
# ``copy_face_from.py``; this is a thin compatibility shim — the source
# of truth is the other module.
__all__ = [
    "CopyFaceFromSummary",
    "import_card",
    "import_cards_folder",
    "resolve_copy_face_from_references",
]

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


#: Value type produced by the resolver. Named tuple so fields are
#: self-documenting at call sites (``hit.legal_commander`` reads better
#: than ``hit[2]``) while still being indexable as a plain tuple for
#: back-compat with older callers that do ``hit[0]`` / ``hit[1]``.
#:
#: - ``edhrec_rank`` is ``None`` when the Scryfall source row has no
#:   rank, or the DB schema pre-dates the column.
#: - ``legal_commander`` defaults to ``True`` when the schema pre-dates
#:   the ``legal_commander`` column — "no known reason to exclude".
#: - ``color_identity`` is the engine's sorted comma-joined pip format
#:   (``"B,G,R,U,W"``; ``""`` = colourless). ``None`` when the schema
#:   pre-dates the column or the value is unparseable — the importer
#:   then falls back to the cost-derived placeholder.
class ScryfallMeta(NamedTuple):
    oracle_id: str
    edhrec_rank: int | None
    legal_commander: bool = True
    color_identity: str | None = None


def _normalise_scryfall_identity(raw: object) -> str | None:
    """Convert a Scryfall ``color_identity`` value to sorted ``"B,G,R,U,W"`` form.

    tags.db stores the column as a JSON array string (``'["B", "W"]'``);
    tolerate an already comma-joined string as well. Returns ``""`` for a
    genuinely colourless identity (``[]``) and ``None`` for missing or
    unparseable input so callers can fall back to the cost-derived
    placeholder.
    """
    if raw is None or not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    if text.startswith("["):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return None
        if not isinstance(parsed, list):
            return None
        tokens = [str(t) for t in parsed]
    else:
        tokens = text.split(",")
    pips = {t.strip().upper() for t in tokens if t.strip()}
    if pips - _MANA_PIPS:
        return None
    return ",".join(sorted(pips))


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
    has_legal_commander = "legal_commander" in cols
    has_color_identity = "color_identity" in cols

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
    if has_legal_commander:
        select_cols.append("legal_commander")
    if has_color_identity:
        select_cols.append("color_identity")
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
        if has_edhrec_rank:
            idx += 1
        # Scryfall stores commander legality as 1/0/None. Anything not
        # explicitly 0 is treated as legal — "no known reason to exclude".
        raw_legal = row[idx] if has_legal_commander else None
        legal_commander = raw_legal != 0
        if has_legal_commander:
            idx += 1
        color_identity = _normalise_scryfall_identity(row[idx]) if has_color_identity else None

        if not oracle_id or not canonical:
            continue

        meta = ScryfallMeta(oracle_id, edhrec_rank, legal_commander, color_identity)
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
        # Scryfall-resolved identity when available (set by import_card);
        # cost-derived proxy otherwise. "" is a valid colourless identity,
        # so only None falls back.
        "color_identity": (card["color_identity"] if card.get("color_identity") is not None else colors),
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
        # Default to legal (1) when unknown — the Scryfall resolver
        # overrides this via `card["legal_commander"]` when the DB
        # carries the column. Never None: the schema enforces DEFAULT 1
        # but binding an explicit value avoids relying on that default.
        "legal_commander": 1 if card.get("legal_commander", True) else 0,
        "copy_face_from": card.get("copy_face_from"),
    }


_CARD_INSERT_SQL = """
INSERT OR REPLACE INTO cards (
    name, oracle_id, mana_cost, cmc, types, supertypes, subtypes, card_types,
    colors, color_identity, power, toughness, loyalty, keywords,
    oracle_text, is_commander, deck_hints, deck_needs, deck_has,
    edhrec_rank, rarity, set_code, legal_commander, copy_face_from
) VALUES (
    :name, :oracle_id, :mana_cost, :cmc, :types, :supertypes, :subtypes, :card_types,
    :colors, :color_identity, :power, :toughness, :loyalty, :keywords,
    :oracle_text, :is_commander, :deck_hints, :deck_needs, :deck_has,
    :edhrec_rank, :rarity, :set_code, :legal_commander, :copy_face_from
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
    "effect_conditional",
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


#: Keys the importer consumes from the port dict but does NOT persist
#: on card_ports. Producers attach these; import_card must pop them
#: before calling _normalise_port.
_TRANSIENT_PORT_KEYS: frozenset[str] = frozenset({"_change_type", "_token_script", "_attributes", "_etb_scope"})

#: BuffedBy SVar tokens classified by explode_filter → card_hints.category.
#: attr_kinds not in this map (controller, cmc_cmp, etc.) are skipped —
#: they carry no tribal/synergy signal.
_BUFFED_BY_CATEGORY: dict[str, str] = {
    "type": "Type",
    "subtype": "Type",
    "supertype": "Type",
    "keyword": "Keyword",
    "color": "Color",
}


def _normalise_port(port: dict[str, Any]) -> dict[str, Any]:
    leaked = _TRANSIENT_PORT_KEYS & port.keys()
    if leaked:
        raise ValueError(f"transient port keys must be popped before _normalise_port: {sorted(leaked)}")
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
    conn.execute("DELETE FROM card_hints WHERE card_name = ?", (name,))

    if oracle_id_resolver is not None and not card.get("oracle_id"):
        hit = _resolve_scryfall_meta(
            name,
            card.get("alternate_name"),
            oracle_id_resolver,
        )
        if hit is not None:
            card["oracle_id"] = hit.oracle_id
            # Only overwrite edhrec_rank when not already set on the
            # parsed card (future-proofing: if parsers ever learn to
            # read rank directly from Forge data, that would take
            # priority).
            if card.get("edhrec_rank") is None:
                card["edhrec_rank"] = hit.edhrec_rank
            # Scryfall legality always wins — Forge has no legality
            # signal, so there is nothing to protect against being
            # overwritten here.
            card["legal_commander"] = hit.legal_commander
            # Scryfall colour identity always wins too: the cost-derived
            # placeholder misses off-cost pips (activated abilities like
            # Sisay's {W}{U}{B}{R}{G}, colour indicators, land types).
            if hit.color_identity is not None:
                card["color_identity"] = hit.color_identity

    conn.execute(_CARD_INSERT_SQL, _card_row(card))

    # Project deck_needs / deck_hints / deck_has dicts into the normalised
    # card_hints table so synergy rules can join by (kind, category, value).
    for kind, key in (("needs", "deck_needs"), ("hints", "deck_hints"), ("has", "deck_has")):
        source = card.get(key)
        if not source:
            continue
        for category, values in source.items():
            for value in values:
                conn.execute(
                    "INSERT OR IGNORE INTO card_hints (card_name, kind, category, value) VALUES (?, ?, ?, ?)",
                    (name, kind, category, value),
                )

    for svar_name, svar_value in card.get("svars", {}).items():
        conn.execute(
            "INSERT OR REPLACE INTO card_svars (card_name, svar_name, svar_value) VALUES (?, ?, ?)",
            (name, svar_name, svar_value),
        )

    # BuffedBy is Forge's AI hint declaring which permanents buff this card.
    # Flatten into card_hints so complement rules can match it to candidate
    # types/subtypes/keywords/colors.
    buffed_by = card.get("svars", {}).get("BuffedBy", "")
    if buffed_by:
        for piece in buffed_by.split(","):
            for attr in explode_filter(piece.strip()):
                category = _BUFFED_BY_CATEGORY.get(attr["attr_kind"])
                if category is None:
                    continue
                conn.execute(
                    "INSERT OR IGNORE INTO card_hints (card_name, kind, category, value) VALUES (?, ?, ?, ?)",
                    (name, "buffed_by", category, attr["attr_value"]),
                )

    ports = extract_all_ports(card)
    inserted = 0
    for port in ports:
        change_type = port.pop("_change_type", "")
        token_script = port.pop("_token_script", "")
        attributes_csv = port.pop("_attributes", "")
        etb_scope = port.pop("_etb_scope", "")
        cur = conn.execute(_PORT_INSERT_SQL, _normalise_port(port))
        port_id = cur.lastrowid
        inserted += 1
        for attr in explode_filter(port.get("valid_filter") or ""):
            conn.execute(
                "INSERT OR IGNORE INTO port_attributes "
                "(port_id, attr_kind, attr_value, is_negated) VALUES (?, ?, ?, ?)",
                (port_id, attr["attr_kind"], attr["attr_value"], attr["is_negated"]),
            )
        # ChangeType is a comma-separated list of filter clauses; explode
        # each clause with the shared filter parser and re-tag as change_type.
        if change_type:
            for clause in change_type.split(","):
                for attr in explode_filter(clause):
                    # Only keep semantically-interesting kinds (types and
                    # subtypes). Controller/color/cmc_cmp are orthogonal.
                    if attr["attr_kind"] in ("type", "subtype"):
                        conn.execute(
                            "INSERT OR IGNORE INTO port_attributes "
                            "(port_id, attr_kind, attr_value, is_negated) VALUES (?, ?, ?, ?)",
                            (port_id, "change_type", attr["attr_value"], attr["is_negated"]),
                        )
        if token_script:
            for attr_kind, attr_value in _parse_token_script(token_script):
                conn.execute(
                    "INSERT OR IGNORE INTO port_attributes "
                    "(port_id, attr_kind, attr_value, is_negated) VALUES (?, ?, ?, ?)",
                    (port_id, attr_kind, attr_value, False),
                )
        # AlterAttribute Attributes$ values (Prepared, Suspected, …) — one
        # port_attributes row per comma-separated entry under attr_kind=
        # 'attribute'. Joined by the prepared_mechanic complement rule.
        if attributes_csv:
            for raw in attributes_csv.split(","):
                attr_value = raw.strip()
                if attr_value:
                    conn.execute(
                        "INSERT OR IGNORE INTO port_attributes "
                        "(port_id, attr_kind, attr_value, is_negated) VALUES (?, ?, ?, ?)",
                        (port_id, "attribute", attr_value, False),
                    )
        # K:ETBReplacement provenance: 'other' or 'copy'. Stored under
        # attr_kind='etb_scope' so downstream rules can distinguish
        # SVar-walked ETB-replacement effects from regular ones.
        # See docs/brainstorms/2026-05-20-etb-replacement-svar-walking-requirements.md.
        if etb_scope:
            conn.execute(
                "INSERT OR IGNORE INTO port_attributes "
                "(port_id, attr_kind, attr_value, is_negated) VALUES (?, ?, ?, ?)",
                (port_id, "etb_scope", etb_scope, False),
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

    # Second pass: resolve CopyFaceFrom:<Name> references after every
    # card is in the universe. Order-independence is essential — a
    # carrier whose .txt sorts before its reference's .txt would fail
    # if we attempted resolution per-card during the first pass.
    with conn:
        cff_summary = resolve_copy_face_from_references(conn, _PORT_COLUMNS)
    log.info(
        "CopyFaceFrom resolution: %d carriers, %d/%d resolved (%d ports inherited)",
        cff_summary.carriers,
        cff_summary.resolved,
        cff_summary.carriers,
        cff_summary.inherited_ports,
    )
    if cff_summary.unresolved:
        head = ", ".join(f"{c}→{r}" for c, r in cff_summary.unresolved[:5])
        suffix = "" if len(cff_summary.unresolved) <= 5 else f" (+{len(cff_summary.unresolved) - 5} more)"
        log.warning(
            "%d unresolved CopyFaceFrom references: %s%s",
            len(cff_summary.unresolved),
            head,
            suffix,
        )

    # Seed the event_match_map + cost_feeds_trigger tables from
    # data/event_match_seed.json (plan 003 Unit 3). Idempotent —
    # INSERT OR REPLACE — so re-imports on an existing DB refresh
    # the tables without disturbing other data.
    from .port_graph.event_maps import seed_event_match_map_db

    seed_event_match_map_db(conn)

    # Seed the rules table from data/rules_seed.json (plan 003 Unit 4).
    # Empty on first land; populated in Units 7 & 8 as the POC rule
    # migrations land. Idempotent via INSERT OR REPLACE.
    from .port_graph.rules_schema import seed_rules_db

    seed_rules_db(conn)

    return card_count, port_count
