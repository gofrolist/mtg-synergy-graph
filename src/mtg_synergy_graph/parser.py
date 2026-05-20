"""Forge DSL parser + branch-aware SVar chain walker.

Implements SPEC §5.2-§5.4. The parser is intentionally lossless: every line
in a Forge .txt file becomes a structured record without dropping branch
provenance, so scoring (§7.2) can discount conditional branches without the
parser having to bake heuristics in.

Key invariants (regression set against v1.1):

* Forge DSL uses ``$`` as the key/value separator inside pipe-delimited
  segments. v1.1 used ``=`` and silently broke every line.
* Line prefixes use ``Name:`` (5 chars), ``T:`` (2 chars), etc. v1.1 was
  off-by-one and stripped a leading colon onto the value.
* ``parse_deck_hints`` splits on ``&`` between groups and ``$`` between key
  and value, matching ``Type$Zombie & Keyword$Flying``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Branch metadata (SPEC §5.4)
# ---------------------------------------------------------------------------

#: Forge SVar reference keys that point to sub-abilities, paired with the
#: ``branch_kind`` label written to ``card_ports`` rows.
CHAIN_KEYS: dict[str, str] = {
    "Execute": "execute",
    "SubAbility": "subability",
    "TrueSubAbility": "true",
    "FalseSubAbility": "false",
    "WinSubAbility": "win",
    "OtherwiseSubAbility": "otherwise",
    "RepeatSubAbility": "repeat",
    "ChangeZoneTable": "change_zone_table",
}

#: Branches whose ports are contingent on a runtime decision.
CONDITIONAL_BRANCH_KINDS: frozenset[str] = frozenset({"true", "false", "win", "otherwise", "choice"})


@dataclass
class ChainNode:
    """Single node in a walked SVar chain (§5.4)."""

    svar_name: str
    parsed: dict[str, Any]
    branch_kind: str
    branch_parent: str | None
    source_svar: str
    chain_depth: int
    is_conditional: bool


# ---------------------------------------------------------------------------
# Core line parser (§5.3)
# ---------------------------------------------------------------------------

# DB$/SP$/AB$/ST$ verb prefixes that may appear before a $-separated verb in
# the first segment of an A: line or SVar value.
_VERB_PREFIXES = ("DB", "SP", "AB", "ST")


def parse_forge_line(line: str) -> dict[str, Any]:
    """Parse a pipe-delimited Forge DSL line into a key-value dict.

    >>> parse_forge_line('Mode$ ChangesZone | ValidCard$ Creature.YouCtrl | Origin$ Any')
    {'Mode': 'ChangesZone', 'ValidCard': 'Creature.YouCtrl', 'Origin': 'Any'}

    Verb prefixes (``DB$``, ``SP$``, etc.) are recorded under ``_prefix`` and
    ``_verb`` so the port extractor can recover the effect class even when
    the segment doesn't carry a regular ``Key$Value`` pair.
    """
    result: dict[str, Any] = {}
    for raw_part in line.split("|"):
        part = raw_part.strip()
        if not part:
            continue
        # Verb-prefix segments such as "DB$ PutCounter" become both a regular
        # entry under "DB" and a synthetic _prefix/_verb pair so downstream
        # code can ask for either.
        if "$" not in part:
            continue
        key, _, val = part.partition("$")
        key = key.strip()
        val = val.strip()
        result[key] = val
        if key in _VERB_PREFIXES and "_verb" not in result:
            result["_prefix"] = key
            result["_verb"] = val
    return result


def parse_deck_hints(hint_str: str) -> dict[str, list[str]]:
    """Parse a DeckHints/DeckNeeds/DeckHas string (§5.6).

    >>> parse_deck_hints('Type$Zombie & Keyword$Flying')
    {'Type': ['Zombie'], 'Keyword': ['Flying']}
    >>> parse_deck_hints('Ability$Token|Counters')
    {'Ability': ['Token', 'Counters']}
    """
    result: dict[str, list[str]] = {}
    for part in hint_str.split("&"):
        part = part.strip()
        if "$" not in part:
            continue
        key, _, val = part.partition("$")
        key = key.strip()
        values = [v.strip() for v in val.split("|") if v.strip()]
        if values:
            result.setdefault(key, []).extend(values)
    return result


# ---------------------------------------------------------------------------
# Card file parser (§5.2)
# ---------------------------------------------------------------------------

# Mapping from line prefix → (key in returned card dict, prefix length, kind)
# kind is one of: "scalar", "deck_hints", "ability", "keyword", "svar"
_LINE_DISPATCH: tuple[tuple[str, str, int, str], ...] = (
    ("Name:", "name", 5, "scalar"),
    ("ManaCost:", "mana_cost", 9, "scalar"),
    ("Types:", "types", 6, "scalar"),
    ("PT:", "pt", 3, "scalar"),
    ("Loyalty:", "loyalty", 8, "scalar"),
    ("Colors:", "colors", 7, "scalar"),
    ("Oracle:", "oracle", 7, "scalar"),
    ("DeckHints:", "deck_hints", 10, "deck_hints"),
    ("DeckNeeds:", "deck_needs", 10, "deck_hints"),
    ("DeckHas:", "deck_has", 8, "deck_hints"),
    # `AlternateMode:Prepare` marks a card whose back face is a stored
    # spell castable when the front-face creature is prepared. Captured
    # here so the importer can emit a synthetic AlternateMode port.
    ("AlternateMode:", "alternate_mode", 14, "scalar"),
)

#: Map of ability prefixes (``T:``/``A:``/``S:``/``R:``) to the kind label
#: stored alongside the parsed dict in ``card["abilities"]``.
_ABILITY_PREFIXES: dict[str, str] = {
    "T:": "trigger",
    "A:": "ability",
    "S:": "static",
    "R:": "replacement",
}


#: Forge separates the front and back face of a DFC / MDFC / transform card
#: with a single ``ALTERNATE`` line inside the same .txt file. We import the
#: front face's identity (Name / ManaCost / Types / Colors) but merge the back
#: face's abilities, keywords and SVars into the same card so DFC mechanics
#: still feed the port extractor. Without this fix, every DFC was imported as
#: just its back face — losing colour identity entirely (back face has no
#: ``ManaCost`` line, so colour_identity ended up empty and the card leaked
#: into every commander pool).
_ALTERNATE_MARKER = "ALTERNATE"


def _parse_card_block(block_lines: list[str], card: dict[str, Any], *, is_alternate: bool) -> None:
    """Parse one face of a Forge .txt file into ``card``.

    For the front face we set every key. For the back face we only merge
    abilities / keywords / svars — identity fields stay on the front face,
    except for the back-face ``Name:`` which is recorded in
    ``card["alternate_name"]`` so the importer can do DFC composite
    matching against Scryfall's ``A // B`` canonical form.
    """
    for raw in block_lines:
        line = raw.strip()
        if not line:
            continue

        matched = False
        for prefix, key, plen, kind in _LINE_DISPATCH:
            if not line.startswith(prefix):
                continue
            if is_alternate:
                # Identity fields belong to the front face only — except
                # the back-face Name, which we preserve for DFC resolution.
                if key == "name":
                    card["alternate_name"] = line[plen:]
                matched = True
                break
            value = line[plen:]
            if kind == "deck_hints":
                card[key] = parse_deck_hints(value)
            else:
                card[key] = value
            matched = True
            break
        if matched:
            continue

        ability_kind = _ABILITY_PREFIXES.get(line[:2])
        if ability_kind is not None:
            card["abilities"].append((ability_kind, parse_forge_line(line[2:])))
            continue

        if line.startswith("K:"):
            card["keywords"].append(line[2:])
            continue

        if line.startswith("SVar:"):
            # SVar:Name:rest-of-line
            body = line[5:]
            name, sep, value = body.partition(":")
            if sep:
                card["svars"][name] = value
            continue


def parse_card_text(text: str) -> dict[str, Any]:
    """Parse the contents of a Forge card .txt file into a structured dict.

    The returned dict has shape::

        {
            "name": str,
            "mana_cost": str | None,
            "types": str | None,
            ...
            "keywords": [str, ...],          # K: lines (raw)
            "svars": {svar_name: svar_value},
            "abilities": [(kind, parsed_dict), ...],
            "deck_hints": dict | None,
            "deck_needs": dict | None,
            "deck_has":   dict | None,
        }

    For DFC / MDFC files (separated by an ``ALTERNATE`` line), the front face
    contributes identity (name, mana cost, types, colors) and both faces
    contribute abilities, keywords and SVars.
    """
    card: dict[str, Any] = {
        "name": "",
        "alternate_name": None,  # populated for DFC/MDFC back faces
        "abilities": [],
        "svars": {},
        "keywords": [],
    }

    front: list[str] = []
    back: list[str] = []
    seen_alternate = False
    for raw in text.splitlines():
        if raw.strip() == _ALTERNATE_MARKER:
            seen_alternate = True
            continue
        (back if seen_alternate else front).append(raw)

    _parse_card_block(front, card, is_alternate=False)
    if back:
        _parse_card_block(back, card, is_alternate=True)
    return card


def parse_card_file(filepath: str | Path) -> dict[str, Any]:
    """Read and parse a Forge card .txt file from disk."""
    path = Path(filepath)
    return parse_card_text(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# SVar chain walker (§5.4)
# ---------------------------------------------------------------------------


def walk_svar_chain(
    entry_name: str,
    all_svars: dict[str, str],
    *,
    visited: set[tuple[str, str, str]] | None = None,
    branch_kind: str = "root",
    branch_parent: str | None = None,
    chain_depth: int = 0,
) -> list[ChainNode]:
    """Recursively follow SVar chains, preserving branch metadata.

    Returns one ``ChainNode`` per traversed SVar.

    The ``visited`` set is keyed by ``(svar_name, parent, branch_kind)`` so
    the same SVar may legitimately be reached via multiple distinct branches
    while still preventing infinite cycles.
    """
    if visited is None:
        visited = set()

    path_key = (entry_name, branch_parent or "", branch_kind)
    if entry_name not in all_svars or path_key in visited:
        return []

    next_visited = set(visited)
    next_visited.add(path_key)

    parsed = parse_forge_line(all_svars[entry_name])
    node = ChainNode(
        svar_name=entry_name,
        parsed=parsed,
        branch_kind=branch_kind,
        branch_parent=branch_parent,
        source_svar=entry_name,
        chain_depth=chain_depth,
        is_conditional=branch_kind in CONDITIONAL_BRANCH_KINDS,
    )
    results: list[ChainNode] = [node]

    for key, child_branch_kind in CHAIN_KEYS.items():
        ref_name = parsed.get(key)
        if not ref_name:
            continue
        results.extend(
            walk_svar_chain(
                ref_name,
                all_svars,
                visited=next_visited,
                branch_kind=child_branch_kind,
                branch_parent=entry_name,
                chain_depth=chain_depth + 1,
            )
        )

    return results


# ---------------------------------------------------------------------------
# Parser-side invariants (used by tests + scoring)
# ---------------------------------------------------------------------------


def parser_branch_kinds() -> set[str]:
    """Every distinct ``branch_kind`` value the parser writes onto a port row.

    Used by tests to assert that :data:`scoring.BRANCH_MULTIPLIER` covers
    every branch the parser can produce. Lives here (not in scoring) so the
    invariant has a single source of truth — :data:`CHAIN_KEYS`.
    """
    return set(CHAIN_KEYS.values()) | {
        "root",
        "static_condition",
        "replacement_condition",
    }
