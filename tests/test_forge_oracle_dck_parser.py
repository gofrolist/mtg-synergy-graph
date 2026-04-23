"""Tests for ``forge_oracle.dck_parser`` — Forge ``.dck`` INI parser.

Plan: docs/plans/2026-04-23-002-feat-forge-second-oracle-plan.md Unit 4.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mtg_synergy_graph.forge_oracle import dck_parser


def _write(tmp_path: Path, name: str, content: str) -> Path:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


def test_parse_happy_path(tmp_path: Path) -> None:
    """Basic parse: metadata + commander + main."""
    dck = _write(
        tmp_path,
        "deck.dck",
        "[metadata]\nName=Test Deck\n"
        "[Commander]\n1 Korvold, Fae-Cursed King|ELD|329\n"
        "[Main]\n1 Sol Ring|C21|263\n4 Lightning Bolt|LEA|161\n",
    )
    parsed = dck_parser.parse_dck(dck)
    assert parsed.name == "Test Deck"
    assert parsed.commander_cards == [(1, "Korvold, Fae-Cursed King")]
    assert parsed.main_cards == [(1, "Sol Ring"), (4, "Lightning Bolt")]


def test_parse_skips_sideboard_and_shop(tmp_path: Path) -> None:
    """Sideboard, shop, and any non-Commander/non-Main sections are ignored."""
    dck = _write(
        tmp_path,
        "deck.dck",
        "[shop]\nWinsToUnlock=0\n[metadata]\nName=Sideboarded\n[Main]\n1 Sol Ring|C21\n[Sideboard]\n1 Pyroblast|LEA\n",
    )
    parsed = dck_parser.parse_dck(dck)
    assert parsed.main_cards == [(1, "Sol Ring")]
    assert parsed.commander_cards == []


def test_parse_handles_blank_lines_and_comments(tmp_path: Path) -> None:
    dck = _write(
        tmp_path,
        "deck.dck",
        "[metadata]\nName=X\n\n[Main]\n\n1 Sol Ring|C21\n\n",
    )
    parsed = dck_parser.parse_dck(dck)
    assert parsed.main_cards == [(1, "Sol Ring")]


def test_parse_handles_crlf_line_endings(tmp_path: Path) -> None:
    dck = tmp_path / "deck.dck"
    dck.write_bytes(b"[metadata]\r\nName=CRLF\r\n[Main]\r\n1 Sol Ring|C21\r\n")
    parsed = dck_parser.parse_dck(dck)
    assert parsed.name == "CRLF"
    assert parsed.main_cards == [(1, "Sol Ring")]


def test_parse_handles_split_card_names(tmp_path: Path) -> None:
    """Split/DFC cards use ``//`` in the name — kept as a single string."""
    dck = _write(
        tmp_path,
        "deck.dck",
        "[metadata]\nName=X\n[Main]\n1 Fire // Ice|APC|128\n",
    )
    parsed = dck_parser.parse_dck(dck)
    assert parsed.main_cards == [(1, "Fire // Ice")]


def test_parse_handles_multi_digit_count(tmp_path: Path) -> None:
    """Basic-land lines like '24 Forest|...' — multi-digit counts."""
    dck = _write(
        tmp_path,
        "deck.dck",
        "[metadata]\nName=X\n[Main]\n24 Forest|UNF|240\n",
    )
    parsed = dck_parser.parse_dck(dck)
    assert parsed.main_cards == [(24, "Forest")]


def test_parse_unicode_and_apostrophe_names(tmp_path: Path) -> None:
    dck = _write(
        tmp_path,
        "deck.dck",
        "[metadata]\nName=X\n[Main]\n"
        "1 Oriq Loremage|STX\n"
        "1 Raphael, Fiendish Savior|CLB\n"
        "1 Lim-Dûl the Necromancer|TSP\n",
    )
    parsed = dck_parser.parse_dck(dck)
    names = [n for _, n in parsed.main_cards]
    assert "Oriq Loremage" in names
    assert "Raphael, Fiendish Savior" in names
    assert "Lim-Dûl the Necromancer" in names


def test_parse_without_set_info(tmp_path: Path) -> None:
    """Some .dck files omit the ``|SET|NUMBER`` suffix — count + name only."""
    dck = _write(
        tmp_path,
        "deck.dck",
        "[metadata]\nName=X\n[Main]\n1 Sol Ring\n",
    )
    parsed = dck_parser.parse_dck(dck)
    assert parsed.main_cards == [(1, "Sol Ring")]


def test_parse_name_trailing_plus_is_preserved(tmp_path: Path) -> None:
    """Forge sometimes tags alternate printings with trailing ``+``;
    preserve the raw name so the resolver can fall back gracefully."""
    dck = _write(
        tmp_path,
        "deck.dck",
        "[metadata]\nName=X\n[Main]\n1 Ivorytusk Fortress+|KTK\n",
    )
    parsed = dck_parser.parse_dck(dck)
    assert parsed.main_cards == [(1, "Ivorytusk Fortress+")]


def test_parse_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        dck_parser.parse_dck(tmp_path / "nonexistent.dck")


def test_parse_malformed_line_is_skipped(tmp_path: Path) -> None:
    """A line that doesn't start with ``<count> `` is ignored (not a card line)."""
    dck = _write(
        tmp_path,
        "deck.dck",
        "[metadata]\nName=X\n[Main]\n1 Sol Ring|C21\ngarbage line with no leading count\n2 Lightning Bolt|LEA\n",
    )
    parsed = dck_parser.parse_dck(dck)
    assert parsed.main_cards == [(1, "Sol Ring"), (2, "Lightning Bolt")]


def test_parse_section_header_case_insensitive(tmp_path: Path) -> None:
    """Forge uses both ``[Main]`` and (rarely) ``[main]`` — match case-insensitively."""
    dck = _write(
        tmp_path,
        "deck.dck",
        "[Metadata]\nName=X\n[main]\n1 Sol Ring|C21\n[commander]\n1 Korvold|ELD\n",
    )
    parsed = dck_parser.parse_dck(dck)
    assert parsed.main_cards == [(1, "Sol Ring")]
    assert parsed.commander_cards == [(1, "Korvold")]


def test_iter_deck_names_returns_counts_combined(tmp_path: Path) -> None:
    """``iter_deck_names`` is the helper for PPMI ingest — yields main + commander."""
    dck = _write(
        tmp_path,
        "deck.dck",
        "[metadata]\nName=X\n[Commander]\n1 Korvold|ELD\n[Main]\n1 Sol Ring|C21\n4 Lightning Bolt|LEA\n",
    )
    parsed = dck_parser.parse_dck(dck)
    names = list(dck_parser.iter_deck_names(parsed))
    # Commander + main, each name once regardless of count (PPMI uses Boolean "deck contains X").
    assert sorted(names) == sorted(["Korvold", "Sol Ring", "Lightning Bolt"])


def test_iter_deck_files_walks_directory_recursively(tmp_path: Path) -> None:
    """``iter_deck_files`` is the walker used by the ingest orchestrator."""
    (tmp_path / "sub").mkdir()
    (tmp_path / "a.dck").write_text("[Main]\n1 X\n", encoding="utf-8")
    (tmp_path / "sub" / "b.dck").write_text("[Main]\n1 Y\n", encoding="utf-8")
    (tmp_path / "ignore.txt").write_text("x", encoding="utf-8")

    found = sorted(p.name for p in dck_parser.iter_deck_files(tmp_path))
    assert found == ["a.dck", "b.dck"]


def test_parse_handles_name_without_explicit_count(tmp_path: Path) -> None:
    """Lines that don't parse as card entries are skipped without raising."""
    dck = _write(
        tmp_path,
        "deck.dck",
        "[Main]\nSol Ring\n1 Lightning Bolt|LEA\n",
    )
    parsed = dck_parser.parse_dck(dck)
    assert parsed.main_cards == [(1, "Lightning Bolt")]
