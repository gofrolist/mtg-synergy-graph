"""Measure the CURRENT engine against the new inclusion-rate labels.

Run from the mtg-synergy-graph repo; writes baseline.json for
mtg-strategy-graph to pin. This is the only task that touches the old
repo, and it only adds a script.

    uv run python scripts/measure_new_labels.py \
        --themes-db ~/gofrolist/mtg-strategy-graph/data/themes.db \
        --out ~/gofrolist/mtg-strategy-graph/docs/baseline.json
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path.home() / "gofrolist/mtg-strategy-graph/src"))
from mtg_strategy_graph.labels import metrics
from mtg_strategy_graph.labels.resolve import build_name_map, resolve_names

from mtg_synergy_graph import SynergyEngine
from mtg_synergy_graph.validate import commander_to_slug

CORE_FLOOR = 0.25
DISCRIMINATIVE_N = 20
MIN_INCLUSION = 0.10


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--themes-db", type=Path, required=True)
    ap.add_argument("--synergy-db", type=Path, default=Path("data/synergy.db"))
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--top", type=int, default=30)
    args = ap.parse_args()

    tconn = sqlite3.connect(args.themes_db)
    engine = SynergyEngine(db_path=str(args.synergy_db))
    pairs = tconn.execute("SELECT DISTINCT commander_slug, theme_slug FROM theme_cards ORDER BY 1, 2").fetchall()

    # NOTE: deliberate deviation from the task-10-brief template. Building
    # slug_to_name only from tag_commanders (EDHREC "top commanders" per
    # theme+colour-identity page) silently drops any corpus commander that
    # isn't popular enough to rank on those pages -- which is exactly
    # reyhan-last-of-the-abzan and yawgmoth-thran-physician, two of the
    # four MANDATORY commanders (chosen precisely because they are
    # low-coverage / flood-casualty cases). Building the map from every
    # card name in the engine's own DB instead is more principled (any
    # name the engine can actually score should resolve) and doesn't lose
    # the two commanders the whole measurement is meant to characterize.
    # Ambiguous slugs (two distinct names collapsing to the same slug) are
    # dropped rather than guessed at; empirically there are none in the
    # 32,624-card corpus (see docs/DECISIONS.md).
    slug_to_name: dict[str, str] = {}
    ambiguous_slugs: set[str] = set()
    for (name,) in engine._conn.execute("SELECT name FROM cards"):
        slug = commander_to_slug(name)
        if slug in slug_to_name and slug_to_name[slug] != name:
            ambiguous_slugs.add(slug)
        else:
            slug_to_name[slug] = name
    for slug in ambiguous_slugs:
        del slug_to_name[slug]

    # theme_cards.card_name is EDHREC's spelling (e.g. "Dusk // Dawn"),
    # while engine.page() ranks candidates under the Forge canonical name
    # (e.g. "Dusk"). Resolve every label name through the same
    # exact/front-face/normalised pipeline used for commander names before
    # intersecting with the engine's output, or the two sides silently
    # speak different name dialects.
    #
    # Names the pipeline cannot resolve (cards genuinely absent from the
    # Forge cardsfolder snapshot, e.g. recent Universes Beyond product; see
    # docs/DECISIONS.md "Name-resolution coverage") are kept in the label
    # set under their ORIGINAL raw name rather than dropped. They could
    # never intersect `top` either way (top only contains real Forge
    # names), so this preserves the label's size/denominator and confines
    # the fix to its intended scope: the small set of names that already
    # exist under a different spelling on the Forge side.
    name_map = build_name_map(engine._conn)

    def _resolve_label_names(names) -> set[str]:
        resolved, unresolved = resolve_names(names, name_map)
        out = set(resolved.values())
        out.update(unresolved)
        return out

    per_pair = {}
    for slug, theme in pairs:
        name = slug_to_name.get(slug)
        if name is None:
            continue
        try:
            page = engine.page(commander=name, offset=0, limit=args.top)
        except Exception as exc:  # commander missing from the ports DB
            per_pair[f"{slug}|{theme}"] = {"error": str(exc)}
            continue
        top = [i.card for i in page.items]
        core = _resolve_label_names(metrics.core_label(metrics.inclusion_rates(tconn, slug, theme), floor=CORE_FLOOR))
        disc = _resolve_label_names(
            metrics.discriminative_label(tconn, slug, theme, n=DISCRIMINATIVE_N, min_inclusion=MIN_INCLUSION)
        )
        per_pair[f"{slug}|{theme}"] = {
            "core_recall": metrics.recall(top, core),
            "discriminative_recall": metrics.recall(top, disc),
            "core_label_size": len(core),
            "discriminative_label_size": len(disc),
            "pool_size": page.total,
        }

    scored = [v for v in per_pair.values() if "error" not in v]
    agg_disc = sum(v["discriminative_recall"] for v in scored) / max(len(scored), 1)
    agg_core = sum(v["core_recall"] for v in scored) / max(len(scored), 1)

    out_data = {
        "measured_at": dt.datetime.now(dt.UTC).isoformat(),
        "engine": "mtg-synergy-graph (pre-rebuild baseline)",
        "core_floor": CORE_FLOOR,
        "discriminative_n": DISCRIMINATIVE_N,
        "min_inclusion": MIN_INCLUSION,
        "top_n": args.top,
        "per_pair": per_pair,
        "aggregate": {"core_recall": agg_core, "discriminative_recall": agg_disc},
    }
    # Preserve hand-written fields (e.g. "corpus", "gates") from an existing
    # baseline file instead of clobbering them: only fields this script
    # itself computes are refreshed.
    if args.out.exists():
        existing = json.loads(args.out.read_text(encoding="utf-8"))
        for key, value in existing.items():
            out_data.setdefault(key, value)

    args.out.write_text(json.dumps(out_data, indent=2) + "\n", encoding="utf-8")
    print(f"aggregate discriminative_recall={agg_disc:.3f} core_recall={agg_core:.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
