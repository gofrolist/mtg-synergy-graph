"""Tests for ``forge_oracle.version`` — the SHA pin reader + match check.

Plan: docs/plans/2026-04-23-002-feat-forge-second-oracle-plan.md Unit 1.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mtg_synergy_graph.forge_oracle import version as fov

REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# read_pinned_sha
# ---------------------------------------------------------------------------


def test_read_pinned_sha_real_pin_is_40_char_hex() -> None:
    """The committed pin must parse to a 40-char lowercase-hex SHA."""
    sha = fov.read_pinned_sha()
    assert len(sha) == 40
    assert all(c in "0123456789abcdef" for c in sha)


def test_read_pinned_sha_skips_comments_and_blanks(tmp_path: Path) -> None:
    pin = tmp_path / "version.txt"
    pin.write_text(
        "# comment\n\n# another comment\nabcdef0123456789abcdef0123456789abcdef01\n# trailing comment ignored\n",
        encoding="utf-8",
    )
    assert fov.read_pinned_sha(pin) == "abcdef0123456789abcdef0123456789abcdef01"


def test_read_pinned_sha_rejects_non_hex_first_line(tmp_path: Path) -> None:
    pin = tmp_path / "version.txt"
    pin.write_text("not-a-sha\n", encoding="utf-8")
    with pytest.raises(fov.OracleVersionFileError, match="not a 40-char hex"):
        fov.read_pinned_sha(pin)


def test_read_pinned_sha_rejects_too_short(tmp_path: Path) -> None:
    pin = tmp_path / "version.txt"
    pin.write_text("abc123\n", encoding="utf-8")
    with pytest.raises(fov.OracleVersionFileError, match="not a 40-char hex"):
        fov.read_pinned_sha(pin)


def test_read_pinned_sha_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(fov.OracleVersionFileError, match="not found"):
        fov.read_pinned_sha(tmp_path / "nonexistent.txt")


def test_read_pinned_sha_rejects_comments_only_file(tmp_path: Path) -> None:
    pin = tmp_path / "version.txt"
    pin.write_text("# only a comment\n\n# still only comments\n", encoding="utf-8")
    with pytest.raises(fov.OracleVersionFileError, match="no SHA line"):
        fov.read_pinned_sha(pin)


def test_read_pinned_sha_normalizes_uppercase_hex_to_lower(tmp_path: Path) -> None:
    pin = tmp_path / "version.txt"
    pin.write_text("ABCDEF0123456789ABCDEF0123456789ABCDEF01\n", encoding="utf-8")
    assert fov.read_pinned_sha(pin) == "abcdef0123456789abcdef0123456789abcdef01"


# ---------------------------------------------------------------------------
# read_current_forge_sha
# ---------------------------------------------------------------------------


def test_read_current_forge_sha_returns_hex() -> None:
    """Reading the live Forge checkout returns a 40-char lowercase-hex SHA."""
    sha = fov.read_current_forge_sha()
    assert len(sha) == 40
    assert all(c in "0123456789abcdef" for c in sha)


def test_read_current_forge_sha_raises_on_missing_checkout(tmp_path: Path) -> None:
    with pytest.raises(fov.OracleForgeCheckoutError, match="not a git checkout"):
        fov.read_current_forge_sha(tmp_path / "nonexistent_forge")


# ---------------------------------------------------------------------------
# verify_pin_matches_checkout
# ---------------------------------------------------------------------------


def test_verify_pin_matches_checkout_passes_when_in_sync() -> None:
    """Committed pin should match the current data/forge/ HEAD.

    If this fails, either:
      - Someone bumped the pin but didn't `git checkout` the new SHA
        in data/forge/, or
      - Someone ran `git -C data/forge pull/checkout` but didn't bump
        data/forge_oracle/version.txt.
    Fix: run `scripts/forge_oracle.py upgrade` (once delivered) or
    manually reset data/forge/ to the pinned SHA.
    """
    fov.verify_pin_matches_checkout()


def test_verify_pin_matches_checkout_raises_on_drift(tmp_path: Path) -> None:
    pin = tmp_path / "version.txt"
    # A SHA that's plausibly real but guaranteed not to equal the live HEAD.
    pin.write_text("0000000000000000000000000000000000000000\n", encoding="utf-8")
    with pytest.raises(fov.OracleVersionMismatchError, match="drifted"):
        fov.verify_pin_matches_checkout(version_file=pin)
