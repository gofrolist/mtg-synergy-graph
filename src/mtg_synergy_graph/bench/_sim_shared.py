"""Shared CLI/report helpers for context_sim and quality_sim.

PR #101 review — shared CLI/report helpers for the plan 2026-07-06-001
kill-test instruments. ``context_sim.py`` and ``quality_sim.py`` are
independent sibling instruments (different assembly semantics, different
main()-driver gate/whitelist branching) that had accumulated byte-identical
(or near-identical) small helpers. This module is the single home for those
helpers so future edits to e.g. ``bands.md`` rendering can't silently drift
between the two instruments.

Deliberately NOT unified here: the two ``main()`` drivers and their
gate/whitelist branching — reviewed as desirable to keep separate since the
composite-gate logic differs per instrument.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..engine import UNRANKED_EDHREC_SENTINEL

#: Shared sentinel for an EDHREC-unranked candidate in the (-total, cmc,
#: edhrec_rank, name) sort key — same value as ``engine.UNRANKED_EDHREC_SENTINEL``.
UNRANKED = UNRANKED_EDHREC_SENTINEL


def write_text(outdir: Path, name: str, text: str) -> Path:
    """Write ``text`` to ``outdir/name``, creating ``outdir`` if needed."""
    outdir.mkdir(parents=True, exist_ok=True)
    path = outdir / name
    path.write_text(text, encoding="utf-8")
    return path


def render_bands_markdown(report: dict[str, Any], *, title: str) -> str:
    """Render the common ``bands`` report shape (ndcg_band + gem_band) as markdown."""
    nb = report["ndcg_band"]
    gb = report["gem_band"]
    lines = [
        f"# {title}",
        "",
        f"fixture: {report.get('fixture', '?')}  |  commanders: {report['n_commanders']}",
        "",
        f"NDCG@30   mean={nb['mean']:.4f}  95% CI [{nb['ci95_low']:.4f}, {nb['ci95_high']:.4f}]  "
        f"half-width={nb['half_width']:.4f}",
        f"gem rate  mean={gb['mean']:.4f}  95% CI [{gb['ci95_low']:.4f}, {gb['ci95_high']:.4f}]  "
        f"half-width={gb['half_width']:.4f}",
    ]
    return "\n".join(lines) + "\n"


def parse_grid_cells[T](
    raw: str,
    *,
    make_cell: Callable[[str], T],
    expected: str,
    error_cls: type[Exception],
) -> list[T]:
    """Parse a ``";"``-separated, ``","``-delimited grid-cell spec into cell objects.

    ``make_cell`` receives one already-stripped ``"a,b"`` segment and
    returns the parsed cell object — typically via tuple-unpacking
    ``part.split(",")`` and applying the caller's own field converters
    (``int``/``float``), exactly as the pre-refactor per-module
    ``_parse_cells`` did. Any ``ValueError`` raised by ``make_cell``
    (wrong field count via unpacking, bad numeric literal, ...) is
    re-wrapped in ``error_cls`` with the historical message shape,
    parameterized only by ``expected`` (e.g. ``"K,W"`` / ``"q,r0"``) so
    the two callers' error text stays byte-identical to before.
    """
    cells: list[T] = []
    for part in raw.split(";"):
        part = part.strip()
        if not part:
            continue
        try:
            cells.append(make_cell(part))
        except ValueError as exc:
            raise error_cls(f"--cells: malformed cell {part!r} in {raw!r} (expected {expected!r}): {exc}") from exc
    if not cells:
        raise error_cls(f"--cells produced no cells from {raw!r}")
    return cells


def add_common_sim_args(
    p: argparse.ArgumentParser,
    *,
    default_fixture: str,
    default_output_dir: str,
    include_k_max: bool = False,
) -> None:
    """Shared ``--db``/``--edhrec-db``/``--fixture``/``--limit-commanders``/``--output-dir``/``--seed``.

    ``default_fixture`` and ``default_output_dir`` are per-caller
    (context_sim vs quality_sim have different fixture and output-dir
    defaults); ``include_k_max`` adds context_sim's extra ``--k-max`` flag.
    """
    p.add_argument("--db", default="data/synergy.db", help="synergy DB path")
    p.add_argument("--edhrec-db", default="data/tags.db", help="EDHREC tags DB path")
    p.add_argument("--fixture", default=default_fixture, help="golden-set fixture (commander names only)")
    p.add_argument(
        "--limit-commanders",
        type=int,
        default=None,
        help="limit to the first N fixture commanders (smoke runs)",
    )
    p.add_argument("--output-dir", default=default_output_dir, help="report output directory")
    p.add_argument("--seed", type=int, default=17, help="bootstrap RNG seed")
    if include_k_max:
        p.add_argument("--k-max", type=int, default=30, help="context-pool candidate cap (select_context k)")
