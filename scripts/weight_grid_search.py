"""Grid search over flat weight overrides to maximize NDCG@30.

Optimized: computes complements ONCE per commander, then re-scores
with different flat weights. ~100x faster than naive approach.
"""
from __future__ import annotations

import itertools
import json
import math
import sqlite3
import types
from collections import defaultdict
from pathlib import Path

from mtg_synergy_graph.complement_rules import find_all_complements, PortComplement
from mtg_synergy_graph.graph_engine import load_ports_for_set, clear_ports_cache
from mtg_synergy_graph.validate import (
    _fetch_edhrec_sections,
    commander_to_slug,
    compute_ndcg,
)
from mtg_synergy_graph import universal_scorer as us


def _score_with_weights(
    complements: list[PortComplement],
    flat_rules: frozenset[str],
    weight_overrides: dict[str, float],
) -> dict[str, float]:
    """Re-compute IDF weights with custom flat overrides, return per-candidate scores."""
    # Compute IDF
    freq: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    for c in complements:
        key = (c.rule_id, c.cmdr_event, c.cand_event)
        freq[key].add(c.candidate)

    idf: dict[tuple[str, str, str], float] = {}
    for key, candidates in freq.items():
        rule_id = key[0]
        if rule_id in flat_rules:
            idf[key] = weight_overrides.get(rule_id, 1.0)
        else:
            idf[key] = 1.0 / math.log2(1.0 + len(candidates))

    # Score per candidate
    by_candidate: dict[str, list[PortComplement]] = defaultdict(list)
    for c in complements:
        by_candidate[c.candidate].append(c)

    scores: dict[str, float] = {}
    for name, comps in by_candidate.items():
        syn = 0.0
        anti = 0.0
        seen_syn: set[tuple[str, str, str]] = set()
        seen_anti: set[tuple[str, str, str]] = set()
        distinct_rules: set[str] = set()
        for c in comps:
            key = (c.rule_id, c.cmdr_event, c.cand_event)
            if c.direction == "synergy":
                if key not in seen_syn:
                    seen_syn.add(key)
                    syn += idf.get(key, 1.0)
                    distinct_rules.add(c.rule_id)
            else:
                if key not in seen_anti:
                    seen_anti.add(key)
                    anti += idf.get(key, 1.0)
        base = syn - anti
        n_rules = len(distinct_rules)
        if n_rules >= 3:
            base += 0.05 * min(n_rules - 2, 3)
        scores[name] = base

    return scores


def main() -> None:
    gs_path = Path("tests/fixtures/golden_set.json")
    commanders = json.loads(gs_path.read_text())["commanders"]

    synergy_conn = sqlite3.connect("data/synergy.db")
    synergy_conn.row_factory = sqlite3.Row
    edhrec_conn = sqlite3.connect("data/tags.db")
    edhrec_conn.row_factory = sqlite3.Row

    # Pre-compute complements and EDHREC labels for all commanders
    print(f"Pre-computing complements for {len(commanders)} commanders...")
    cmdr_data: list[tuple[str, list[PortComplement], dict[str, float]]] = []
    for cmdr in commanders:
        try:
            clear_ports_cache()
            comps = find_all_complements(synergy_conn, [cmdr])
            slug = commander_to_slug(cmdr)
            sections = _fetch_edhrec_sections(edhrec_conn, slug)
            hi_cards = sections.get("High Synergy Cards", set())
            on_page = set()
            for cards in sections.values():
                on_page |= cards
            labels: dict[str, float] = {}
            for card in hi_cards:
                labels[card] = 3.0
            for card in on_page - hi_cards:
                labels[card] = 1.0
            cmdr_data.append((cmdr, comps, labels))
        except Exception:
            pass
    print(f"  Done. {len(cmdr_data)} commanders loaded.")

    # Grid: weight combinations
    grid = {
        "tribal_density": [0.3, 0.4, 0.5, 0.6],
        "token_producer": [0.15, 0.20, 0.25, 0.30],
        "spell_density": [0.5, 0.75, 1.0],
        "scaling": [0.5, 0.75, 1.0],
    }

    keys = list(grid.keys())
    combos = list(itertools.product(*[grid[k] for k in keys]))
    print(f"Grid search: {len(combos)} combinations")

    flat_rules = us._FLAT_COUNT_RULES
    results: list[tuple[float, int, dict[str, float]]] = []

    for i, combo in enumerate(combos):
        overrides = dict(zip(keys, combo))
        # Remove default values to keep overrides clean
        clean = {k: v for k, v in overrides.items() if not (k in ("spell_density", "scaling") and v == 1.0)}

        total_ndcg = 0.0
        total_hi = 0
        count = 0
        for cmdr, comps, labels in cmdr_data:
            scores = _score_with_weights(comps, flat_rules, clean)
            # Sort by score descending
            ranked = sorted(scores.items(), key=lambda x: -x[1])[:30]
            window = [name for name, _ in ranked]
            hi_cards = {k for k, v in labels.items() if v >= 3.0}
            ndcg = compute_ndcg(window, labels, k=30)
            total_ndcg += ndcg
            total_hi += sum(1 for c in window if c in hi_cards)
            count += 1

        avg_ndcg = total_ndcg / count if count else 0
        results.append((avg_ndcg, total_hi, clean))

        if (i + 1) % 20 == 0:
            best = max(r[0] for r in results)
            print(f"  {i+1}/{len(combos)}: current={avg_ndcg:.4f}, best={best:.4f}")

    results.sort(key=lambda x: -x[0])
    print(f"\n=== TOP 10 ===")
    for ndcg, hi, ov in results[:10]:
        print(f"  NDCG={ndcg:.4f}  Hi-Syn={hi}  weights={ov}")

    print(f"\n=== CURRENT (baseline) ===")
    current = {"tribal_density": 0.5, "token_producer": 0.25}
    for ndcg, hi, ov in results:
        if ov == current:
            print(f"  NDCG={ndcg:.4f}  Hi-Syn={hi}  weights={ov}")
            break

    print(f"\n=== BOTTOM 3 ===")
    for ndcg, hi, ov in results[-3:]:
        print(f"  NDCG={ndcg:.4f}  Hi-Syn={hi}  weights={ov}")


if __name__ == "__main__":
    main()
