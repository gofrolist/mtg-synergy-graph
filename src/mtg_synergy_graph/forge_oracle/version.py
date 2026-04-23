"""Forge commit SHA pinning.

The pinned SHA lives in ``data/forge_oracle/version.txt`` (committed
to this repo). Oracle sidecar consumers call ``read_pinned_sha()`` at
load time and compare against ``data/forge/`` HEAD — a drift means
the developer ran ``git -C data/forge pull`` without re-running
``scripts/forge_oracle.py build`` + ``--upgrade``.

This module has no behavior yet beyond reading the pin file and the
current Forge HEAD. The ``OracleVersionMismatchError`` surface is
consumed in Unit 5 (``OracleConfigInputs`` + ``compute_oracle_hash``)
and Unit 7 (``--vs-forge-oracle`` handler).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_VERSION_FILE = _REPO_ROOT / "data" / "forge_oracle" / "version.txt"
_FORGE_DIR = _REPO_ROOT / "data" / "forge"


class OracleVersionFileError(RuntimeError):
    """Raised when ``data/forge_oracle/version.txt`` is missing or malformed."""


class OracleForgeCheckoutError(RuntimeError):
    """Raised when ``data/forge/`` is missing or not a git checkout."""


class OracleVersionMismatchError(RuntimeError):
    """Raised when the pinned SHA does not match ``data/forge/`` HEAD.

    Actionable message includes the rebuild hint so the developer can
    unblock immediately.
    """


def read_pinned_sha(path: Path = _VERSION_FILE) -> str:
    """Return the SHA recorded in ``data/forge_oracle/version.txt``.

    File format: any number of comment lines (``#``-prefix) or blank
    lines, then exactly one 40-character hex SHA on its own line.
    Additional non-comment lines after the SHA are ignored — makes it
    safe to append a trailing newline or notes without breaking parses.
    """
    if not path.is_file():
        raise OracleVersionFileError(
            f"Forge oracle version pin not found at {path}. Expected a committed file with the pinned Forge commit SHA."
        )
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if len(line) == 40 and all(c in "0123456789abcdef" for c in line.lower()):
            return line.lower()
        raise OracleVersionFileError(f"First non-comment line of {path} is not a 40-char hex SHA: {line!r}")
    raise OracleVersionFileError(f"{path} contains no SHA line (only comments / blanks).")


def read_current_forge_sha(forge_dir: Path = _FORGE_DIR) -> str:
    """Return ``git -C data/forge/ rev-parse HEAD`` for the vendored Forge checkout."""
    if not (forge_dir / ".git").exists():
        raise OracleForgeCheckoutError(
            f"{forge_dir} is not a git checkout. See docs/FORGE_ORACLE.md for setup instructions."
        )
    try:
        result = subprocess.run(  # noqa: S603 — ``git`` with fixed args, no shell
            ["git", "-C", str(forge_dir), "rev-parse", "HEAD"],  # noqa: S607
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired as exc:
        raise OracleForgeCheckoutError(
            f"git rev-parse HEAD timed out after 30s in {forge_dir}; "
            "checkout may be locked (index.lock present?). "
            "Release the lock or rerun the oracle build."
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise OracleForgeCheckoutError(f"Failed to read HEAD of {forge_dir}: {exc.stderr.strip()}") from exc
    return result.stdout.strip().lower()


def verify_pin_matches_checkout(
    *,
    version_file: Path = _VERSION_FILE,
    forge_dir: Path = _FORGE_DIR,
) -> None:
    """Raise ``OracleVersionMismatchError`` if pin != checkout HEAD.

    No-op on match. Consumers call this before reading any Forge-derived
    sidecar artifact.
    """
    pinned = read_pinned_sha(version_file)
    current = read_current_forge_sha(forge_dir)
    if pinned != current:
        raise OracleVersionMismatchError(
            f"Forge checkout has drifted from pinned SHA.\n"
            f"  pinned  (data/forge_oracle/version.txt): {pinned}\n"
            f"  current (git -C data/forge rev-parse HEAD): {current}\n"
            f"Either reset the checkout with "
            f"`git -C data/forge checkout {pinned}`, or run "
            f"`scripts/forge_oracle.py upgrade` to bump the pin + rebuild "
            f"the oracle sidecar DB."
        )
