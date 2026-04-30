"""Coordinate Ascent weight optimizer for ``_RULE_QUALITY_MULTIPLIER``.

Foothold M1 of the tensor-driven weight optimizer plan
(``docs/plans/2026-04-26-001-feat-tensor-weight-optimizer-plan.md``).

This module is built incrementally across the plan's units; each unit
appends its layer to this file:

* **Unit 1 — Composite objective + random split + EDHREC label loader.**
  Pure helper functions over commander lists and EDHREC label dicts.
  Independent of which rules fire.
* **Unit 2 — Cached-complements scorer.** Production-faithful per-grid-cell
  scoring using cached ``find_all_complements`` + re-IDF + ``UniversalScore``.
* **Unit 3 — Coordinate Ascent driver.** The optimizer's main loop:
  alphabetical sweep over rule keys, multiplicative grid, accept/reject
  gates, cumulative-drift revert, planted-perturbation self-test.
* **Unit 4 — CLI handler.** Wires ``bench.py audit --optimize`` into the
  existing dispatcher; tensor-staleness precondition.
* **Unit 5 — Output artifacts.** ``.audit/optimize_proposal.json`` +
  ``.audit/optimize_history.csv`` writers.

The optimizer never mutates ``data/scoring_weights.json`` — it emits a
candidate diff for human review. Application is human-driven via
``bench.py audit --repin --yes``.
"""

from __future__ import annotations

import random
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType

# ---------------------------------------------------------------------------
# Unit 1: Composite α-blended objective
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CompositeObjective:
    """Composite α-blended objective value + per-axis breakdown.

    ``composite = α · mean_ndcg + (1 - α) · gem_rate`` over commanders that
    contribute to each axis. ``gem_rate`` is ``None`` when no commander in
    the input set has EDHREC data; in that case the gem term contributes
    zero and the composite collapses to ``α · mean_ndcg``.

    Attributes:
        composite: Final blended score in [0, 1].
        mean_ndcg: Arithmetic mean per-commander nDCG@30 over the input
            commanders. Always populated.
        gem_rate: Mean ``hidden_gem_hit_rate`` over commanders with
            EDHREC data; ``None`` when no commander has data.
        n_commanders: Total commanders in the input set.
        n_commanders_with_gem: Subset of ``n_commanders`` for whom the
            gem metric was computable (EDHREC data present).
    """

    composite: float
    mean_ndcg: float
    gem_rate: float | None
    n_commanders: int
    n_commanders_with_gem: int


def composite_objective(
    per_commander_ndcg: Mapping[str, float],
    per_commander_gem: Mapping[str, float | None],
    alpha: float,
) -> CompositeObjective:
    """Blend per-commander nDCG and hidden-gem rate into a composite score.

    The two input mappings need not have identical keys: a commander
    appearing only in ``per_commander_ndcg`` contributes to the nDCG mean
    but not the gem mean. A commander whose gem entry is ``None``
    (EDHREC data absent) is also excluded from the gem mean.

    Raises:
        ValueError: ``alpha`` is outside ``[0, 1]``.
    """
    if not 0.0 <= alpha <= 1.0:
        raise ValueError(f"alpha must be in [0, 1], got {alpha}")

    n_commanders = len(per_commander_ndcg)
    if n_commanders == 0:
        return CompositeObjective(
            composite=0.0,
            mean_ndcg=0.0,
            gem_rate=None,
            n_commanders=0,
            n_commanders_with_gem=0,
        )

    mean_ndcg = sum(per_commander_ndcg.values()) / n_commanders

    gem_values = [v for v in per_commander_gem.values() if v is not None]
    n_with_gem = len(gem_values)
    if n_with_gem == 0:
        gem_rate: float | None = None
        composite = alpha * mean_ndcg
    else:
        gem_rate = sum(gem_values) / n_with_gem
        composite = alpha * mean_ndcg + (1.0 - alpha) * gem_rate

    return CompositeObjective(
        composite=composite,
        mean_ndcg=mean_ndcg,
        gem_rate=gem_rate,
        n_commanders=n_commanders,
        n_commanders_with_gem=n_with_gem,
    )


# ---------------------------------------------------------------------------
# Unit 1: Random fixed-seed train/held split
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SplitResult:
    """Train/held commander split + per-color-identity bucket counts.

    The plan revision dropped archetype stratification (no data source
    in ``data/tags.db``); this random split + post-hoc color-identity
    reporting is the substitute. Bucket counts let the human reviewer
    spot obvious skew in the proposal output. Stratification is NOT
    enforced — the buckets are reported, not gated.
    """

    train: tuple[str, ...]
    held: tuple[str, ...]
    color_buckets: Mapping[str, dict[str, int]]


_COLOR_BUCKETS = ("colorless", "mono", "2c", "3c+")


def _color_bucket(color_identity: str | None) -> str:
    """Map ``cards.color_identity`` to a coarse bucket.

    Recognised forms: ``""``/``None`` → ``colorless``, ``"R"`` →
    ``mono``, ``"R,U"`` → ``2c``, ``"R,U,B"`` and wider → ``3c+``.
    """
    if not color_identity:
        return "colorless"
    colors = [c.strip() for c in color_identity.split(",") if c.strip()]
    n = len(colors)
    if n == 0:
        return "colorless"
    if n == 1:
        return "mono"
    if n == 2:
        return "2c"
    return "3c+"


def random_split(
    commanders: Sequence[str],
    conn: sqlite3.Connection | None,
    *,
    train_ratio: float = 0.8,
    seed: int,
) -> SplitResult:
    """Deterministic random 80/20 split with color-identity bucket reporting.

    Uses ``random.Random(seed).shuffle`` for determinism. The conn argument
    is read-only — it queries ``cards.color_identity`` for the bucket
    report. When ``conn`` is ``None`` all commanders fall into the
    ``colorless`` bucket (the report is informational only, so this is
    safe for unit tests that don't need real DB seeding).

    Raises:
        ValueError: ``train_ratio`` is outside ``(0, 1)``.
    """
    if not 0.0 < train_ratio < 1.0:
        raise ValueError(f"train_ratio must be in (0, 1), got {train_ratio}")

    rng = random.Random(seed)  # noqa: S311 — determinism, not crypto
    shuffled = list(commanders)
    rng.shuffle(shuffled)
    cut = round(len(shuffled) * train_ratio)
    train = tuple(shuffled[:cut])
    held = tuple(shuffled[cut:])

    color_by_cmdr: dict[str, str | None] = {}
    if conn is not None and commanders:
        placeholders = ",".join("?" * len(commanders))
        query = f"SELECT name, color_identity FROM cards WHERE name IN ({placeholders})"  # noqa: S608
        for row in conn.execute(query, list(commanders)).fetchall():
            # Row may be a sqlite3.Row (dict-like) or a plain tuple; index
            # by position so both work.
            color_by_cmdr[row[0]] = row[1]

    buckets: dict[str, dict[str, int]] = {b: {"train": 0, "held": 0} for b in _COLOR_BUCKETS}
    for name in train:
        buckets[_color_bucket(color_by_cmdr.get(name))]["train"] += 1
    for name in held:
        buckets[_color_bucket(color_by_cmdr.get(name))]["held"] += 1

    # Drop empty buckets so the report stays focused.
    non_empty = {k: v for k, v in buckets.items() if v["train"] or v["held"]}

    return SplitResult(
        train=train,
        held=held,
        color_buckets=MappingProxyType(non_empty),
    )


# ---------------------------------------------------------------------------
# Unit 1: EDHREC label loader
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EdhrecLabels:
    """Per-commander EDHREC inputs for the composite objective.

    ``graded_labels`` follows ``weight_grid_search.py``'s shape: 3.0 for
    cards in the ``High Synergy Cards`` section, 1.0 for cards in any
    other on-page section, 0 (absent) elsewhere. Used by ``compute_ndcg``.

    ``top_30_set`` is the EDHREC top-30 high-synergy-cards set used as
    the ``edhrec_top_30`` argument to ``hidden_gem_hit_rate_for_commander``.
    ``None`` when EDHREC has no high-synergy data for this commander —
    the documented "skip gem metric" sentinel.
    """

    graded_labels: Mapping[str, float]
    top_30_set: frozenset[str] | None


def load_edhrec_labels(
    edhrec_conn: sqlite3.Connection,
    commander: str,
) -> EdhrecLabels:
    """Build graded labels + top-30 set for one commander from EDHREC.

    Reads the ``edhrec_card_synergy`` table. The slug-conversion is
    handled internally by the helpers we delegate to. Local imports
    avoid pulling ``validate`` (and its eager ``engine`` import) into
    the module-load path.
    """
    from mtg_synergy_graph.edhrec_helpers import fetch_high_synergy_top_n
    from mtg_synergy_graph.validate import _fetch_edhrec_sections, commander_to_slug

    slug = commander_to_slug(commander)
    sections = _fetch_edhrec_sections(edhrec_conn, slug)
    hi_syn = sections.get("High Synergy Cards", set())
    on_page: set[str] = set()
    for cards in sections.values():
        on_page |= cards

    graded: dict[str, float] = {}
    for card in hi_syn:
        graded[card] = 3.0
    for card in on_page - hi_syn:
        graded[card] = 1.0

    top_30 = fetch_high_synergy_top_n(edhrec_conn, commander, limit=30)
    return EdhrecLabels(
        graded_labels=MappingProxyType(graded),
        top_30_set=frozenset(top_30) if top_30 is not None else None,
    )
