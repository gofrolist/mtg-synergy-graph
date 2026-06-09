"""Shared path-resolution helpers for the packaged seed files."""

from __future__ import annotations

from pathlib import Path

#: Packaged seed directory (``src/mtg_synergy_graph/data/`` in the repo,
#: ``site-packages/mtg_synergy_graph/data/`` in an installed wheel).
#: 2026-06-09 audit follow-up: the seeds previously lived at the repo
#: root ``data/`` dir, so an installed wheel could not even be imported
#: (the loader raised before any consumer code ran).
_PACKAGE_DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def default_seed_path(filename: str) -> Path:
    """Resolve a packaged seed file by name.

    Kept in one place so event_maps.py, rules_schema.py, registry.py,
    and the config-hash seed digests can't drift on discovery strategy.
    """
    p = _PACKAGE_DATA_DIR / filename
    if p.exists():
        return p
    raise FileNotFoundError(
        f"packaged seed {filename} not found at {p} — the mtg_synergy_graph "
        "install is broken or the file was removed from src/mtg_synergy_graph/data/."
    )
