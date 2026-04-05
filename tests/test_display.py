"""Tests for CLI display helpers."""
import io
from unittest.mock import patch

from mtg_synergy.recommend.display import print_card_table


class TestPrintCardTable:
    def test_basic_output(self):
        rows = [
            {"name": "Sol Ring", "type_line": "Artifact", "cmc": 1, "score": 100.0},
            {"name": "Lightning Bolt", "type_line": "Instant", "cmc": 1, "score": 95.0},
        ]
        with patch("sys.stdout", new_callable=io.StringIO) as out:
            print_card_table("TOP 2", rows, top_n=2)
            output = out.getvalue()
        assert "TOP 2" in output
        assert "Sol Ring" in output
        assert "Lightning Bolt" in output
        assert "100.0" in output

    def test_truncates_long_type_line(self):
        rows = [
            {"name": "Card", "type_line": "Legendary Creature — Human Soldier Warrior Knight",
             "cmc": 3, "score": 50.0},
        ]
        with patch("sys.stdout", new_callable=io.StringIO) as out:
            print_card_table("TEST", rows)
            output = out.getvalue()
        # "Legendary" shortened to "L" and then truncated with ellipsis
        assert "\u2026" in output or "L Creature" in output

    def test_respects_top_n_limit(self):
        rows = [
            {"name": f"Card {i}", "type_line": "Instant", "cmc": 1, "score": float(100 - i)}
            for i in range(10)
        ]
        with patch("sys.stdout", new_callable=io.StringIO) as out:
            print_card_table("TEST", rows, top_n=3)
            output = out.getvalue()
        assert "Card 0" in output
        assert "Card 2" in output
        assert "Card 3" not in output

    def test_empty_rows(self):
        with patch("sys.stdout", new_callable=io.StringIO) as out:
            print_card_table("EMPTY", [])
            output = out.getvalue()
        assert "EMPTY" in output

    def test_scryfall_link_in_output(self):
        rows = [{"name": "Sol Ring", "type_line": "Artifact", "cmc": 1, "score": 100.0}]
        with patch("sys.stdout", new_callable=io.StringIO) as out:
            print_card_table("TEST", rows)
            output = out.getvalue()
        assert "scryfall.com" in output
