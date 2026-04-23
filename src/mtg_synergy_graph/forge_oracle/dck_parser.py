"""Forge ``.dck`` file parser.

Forge ships deck lists as INI-style files with bracketed section
headers and one line per card entry. Format:

    [metadata]
    Name=Deck Name

    [Commander]
    1 Korvold, Fae-Cursed King|ELD|329

    [Main]
    1 Sol Ring|C21|263
    24 Forest|UNF|240

    [Sideboard]
    ...

Card lines begin with a decimal count, followed by a space, then the
card name. An optional ``|SET|NUMBER`` suffix records the print
(set code + collector number); our parser strips it.

Scope: extract the deck's main + commander cards for PPMI ingest.
Everything else (sideboard, shop metadata, deck description) is
ignored. See plan 2026-04-23-002 Unit 4.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

_SECTION_RE = re.compile(r"^\s*\[(?P<name>[^\]]+)\]\s*$")
_CARD_LINE_RE = re.compile(r"^\s*(?P<count>\d+)\s+(?P<body>.+?)\s*$")


@dataclass(frozen=True, slots=True)
class PreconDeck:
    """Parsed representation of one ``.dck`` file."""

    path: Path
    name: str
    commander_cards: list[tuple[int, str]] = field(default_factory=list)
    main_cards: list[tuple[int, str]] = field(default_factory=list)


def parse_dck(path: Path) -> PreconDeck:
    """Parse one ``.dck`` file.

    Raises ``FileNotFoundError`` if the file is missing. Malformed
    lines inside a card section are silently skipped — Forge's
    precon corpus contains occasional stray text and failing the
    entire ingest over one bad line is not worth the brittleness.
    """
    if not path.is_file():
        raise FileNotFoundError(f"{path} is not a file")

    text = path.read_text(encoding="utf-8", errors="replace")
    section: str | None = None
    deck_name = path.stem  # fallback
    commander_cards: list[tuple[int, str]] = []
    main_cards: list[tuple[int, str]] = []

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        m_section = _SECTION_RE.match(line)
        if m_section is not None:
            section = m_section.group("name").strip().lower()
            continue

        # Metadata key=value lines (only ``Name`` is interesting for our use)
        if section == "metadata":
            if "=" in line:
                k, _, v = line.partition("=")
                if k.strip().lower() == "name":
                    deck_name = v.strip()
            continue

        if section not in ("main", "commander"):
            continue

        m_card = _CARD_LINE_RE.match(line)
        if m_card is None:
            continue  # skip malformed lines silently
        count = int(m_card.group("count"))
        body = m_card.group("body")
        # Strip optional ``|SET|NUMBER`` suffix. Card name itself may contain
        # ``//`` (split/DFC cards), but never ``|``.
        card_name = body.split("|", 1)[0].strip()
        if not card_name:
            continue

        if section == "main":
            main_cards.append((count, card_name))
        else:  # "commander"
            commander_cards.append((count, card_name))

    return PreconDeck(
        path=path,
        name=deck_name,
        commander_cards=commander_cards,
        main_cards=main_cards,
    )


def iter_deck_names(deck: PreconDeck) -> Iterator[str]:
    """Yield each distinct card name in the deck (Boolean presence).

    PPMI uses Boolean "deck contains card X" — card count does not
    factor into the signal. Four copies of Lightning Bolt contribute
    the same as one copy of Sol Ring to a pair's co-occurrence.
    """
    seen: set[str] = set()
    for _, name in deck.commander_cards:
        if name not in seen:
            seen.add(name)
            yield name
    for _, name in deck.main_cards:
        if name not in seen:
            seen.add(name)
            yield name


def iter_deck_files(root: Path) -> Iterator[Path]:
    """Walk ``root`` recursively for ``.dck`` files, sorted for determinism."""
    yield from sorted(p for p in root.rglob("*.dck") if p.is_file())
