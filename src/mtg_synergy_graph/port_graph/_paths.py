"""Shared path-resolution helpers for port_graph seed files."""

from __future__ import annotations

from pathlib import Path


def default_seed_path(filename: str) -> Path:
    """Resolve a ``data/<filename>`` path by walking up from the
    package location, with ``Path.cwd()`` as the first candidate.

    Kept in one place so event_maps.py and rules_schema.py can't
    drift on discovery strategy.
    """
    for candidate in (Path.cwd(), *Path(__file__).resolve().parents):
        p = candidate / "data" / filename
        if p.exists():
            return p
    raise FileNotFoundError(f"data/{filename} not found")
