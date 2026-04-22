"""Shared deprecation-notice helper for the legacy eval scripts.

Each of ``scripts/_audit_rule_impact.py``, ``golden_set_track.py``,
``compare_edhrec.py``, ``weight_grid_search.py``, and
``broad_set_track.py`` prints one line at entry pointing to the
``bench.py`` replacement. Script logic is left untouched so existing
muscle memory keeps working during the transition window; a follow-up
cleanup PR will fully remove the legacy scripts after a stabilization
period.
"""

from __future__ import annotations

import sys


def emit_deprecation(legacy_script: str, bench_equivalent: str) -> None:
    """Print a single-line stderr deprecation pointer.

    Parameters
    ----------
    legacy_script:
        The filename of the script being deprecated, e.g.
        ``"scripts/_audit_rule_impact.py"``.
    bench_equivalent:
        The suggested replacement, e.g. ``"bench.py audit"``.
    """
    print(
        f"DEPRECATED: {legacy_script} is superseded by `uv run scripts/{bench_equivalent}`. "
        "The legacy script still runs; it will be removed in a follow-up cleanup.",
        file=sys.stderr,
    )
