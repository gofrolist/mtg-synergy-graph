"""Card recommendation engine — forge-only scoring pipeline."""
import logging
import sqlite3
from collections import defaultdict
from contextlib import closing

_log = logging.getLogger(__name__)


def recommend_cards(graph: dict, deck_cards: set[str], cards: list[dict],
                    deck_types: set[str] | None = None, top_n: int = 50,
                    mech_labels: list[str] | None = None,
                    db_path: str | None = None,
                    color_identity: set[str] | None = None,
                    commander: str | None = None,
                    *, card_provider=None,
):
    """Rank non-deck cards by total synergy with the current decklist.

    Uses color-identity filter for candidate discovery, then Forge GBM
    (93 features, 87 active + 6 zeroed) for final ranking.
    """
    _shared_conn = None
    _extra_meta = {}
    card_meta = {}
    card_oid_lookup = {}

    # Build oid lookup early.
    # card_meta is rebuilt after scoring since score_forge_candidates
    # appends newly-found cards to the cards list.
    for c in cards:
        card_oid_lookup[c["name"]] = c.get("oracle_id", "")

    if commander and db_path:
        try:
            _shared_conn = sqlite3.connect(db_path)
        except (sqlite3.OperationalError, ValueError):
            _shared_conn = None

    if _shared_conn:
        from mtg_synergy.recommend.scoring import (
            color_identity_filter, score_forge_candidates)

        with closing(_shared_conn):
            # Get commander oracle_id
            cmdr_oid = ""
            for c in cards:
                if c["name"] == commander:
                    cmdr_oid = c.get("oracle_id", "")
                    break

            ci_results = color_identity_filter(
                _shared_conn, cmdr_oid, color_identity or set(),
                deck_cards=deck_cards, card_provider=card_provider)

            candidate_scores = {}
            for oid, name in ci_results:
                if name not in deck_cards:
                    candidate_scores[name] = {
                        "total": 0.0, "partners": [], "multi_sig": 0,
                        "commander_synergy": 0.0, "key_synergy": 0.0,
                    }

            _log.info("Color-identity filter: %d color-legal cards (%d after excluding deck)",
                      len(ci_results), len(candidate_scores))

            # Forge-only scoring (93 features, 87 active + 6 zeroed)
            score_forge_candidates(candidate_scores, cards, _shared_conn,
                                   commander, deck_cards, deck_types,
                                   None,  # active_strategies (removed)
                                   color_identity=color_identity,
                                   card_provider=card_provider)

            # Fill card metadata for scored candidates (before conn closes).
            # Build into _extra_meta (local) — never mutate caller's cards list.
            _extra_meta = {}
            known_names = {c["name"] for c in cards}
            missing_meta = [n for n in candidate_scores if n not in known_names]
            if missing_meta:
                for i in range(0, len(missing_meta), 500):
                    chunk = missing_meta[i:i + 500]
                    ph = ",".join("?" * len(chunk))
                    for row in _shared_conn.execute(
                        f"SELECT name, type_line, cmc FROM cards "
                        f"WHERE name IN ({ph})", chunk
                    ).fetchall():
                        _extra_meta[row[0]] = {
                            "type_line": row[1] or "", "cmc": row[2] or 0,
                        }
    else:
        # Fallback: graph scores only (no DB)
        deck_scores = _deck_card_scores(graph, deck_cards)
        key_cards = {name for name, _ in sorted(
            [(n, i["total"]) for n, i in deck_scores.items() if n != commander],
            key=lambda x: -x[1])[:10]}
        candidate_scores = _candidate_scores(
            graph, deck_cards, commander=commander, key_cards=key_cards)

    # Build card metadata for display (after scoring, which may append cards)
    for c in cards:
        name = c["name"]
        tl = c.get("type_line", "")
        existing = card_meta.get(name)
        if existing and "Token" not in existing.get("type_line", "") and "Token" in tl:
            continue
        card_meta[name] = {
            "type_line": tl, "cmc": c.get("cmc", 0),
            "mana_cost": c.get("mana_cost", ""), "oracle_id": c.get("oracle_id", ""),
            "edhrec_rank": c.get("edhrec_rank"),
        }
        card_oid_lookup[name] = c.get("oracle_id", "")

    # Merge extra metadata from DB for scored candidates not in original cards
    if _extra_meta:
        card_meta.update(_extra_meta)

    # Sort by total synergy
    ranked = sorted(candidate_scores.items(), key=lambda x: x[1]["total"], reverse=True)

    # Normalize scores to 0-100% scale (top card = 100%)
    max_score = ranked[0][1]["total"] if ranked else 1.0
    if max_score <= 0:
        max_score = 1.0
    for card, info in ranked:
        info["pct"] = round(info["total"] / max_score * 100, 1)

    # --- Output ---
    from mtg_synergy.recommend.display import print_card_table

    header = f"TOP {top_n} RECOMMENDED CARDS"
    if mech_labels:
        header += f" | {', '.join(mech_labels)}"

    rows = []
    for card, info in ranked[:top_n]:
        meta = card_meta.get(card, {})
        rows.append({
            "name": card,
            "type_line": meta.get("type_line", ""),
            "cmc": meta.get("cmc", 0),
            "score": info["total"],
        })
    print_card_table(header, rows, top_n=top_n)


def _deck_card_scores(graph: dict, deck_cards: set[str]) -> dict:
    """Compute per-card synergy totals within the deck. Returns {card: {total, partners}}."""
    adj = graph["adjacency"]
    card_synergy = defaultdict(float)
    card_partners = defaultdict(int)

    seen = set()
    for card in deck_cards:
        for edge in adj.get(card, []):
            if edge["target"] in deck_cards:
                pair = tuple(sorted([edge["source"], edge["target"]]))
                if pair not in seen:
                    seen.add(pair)
                    card_synergy[edge["source"]] += edge["score"]
                    card_synergy[edge["target"]] += edge["score"]
                    card_partners[edge["source"]] += 1
                    card_partners[edge["target"]] += 1

    return {
        card: {"total": card_synergy.get(card, 0.0), "partners": card_partners.get(card, 0)}
        for card in deck_cards
    }


def _candidate_scores(graph: dict, deck_cards: set[str],
                      commander: str = None, key_cards: set = None) -> dict:
    """Compute synergy totals for non-deck cards against the decklist."""
    adj = graph["adjacency"]
    scores = defaultdict(lambda: {"total": 0.0, "partners": [], "multi_sig": 0,
                                  "commander_synergy": 0.0, "key_synergy": 0.0})

    if key_cards is None:
        key_cards = set()

    for card in deck_cards:
        for edge in adj.get(card, []):
            target = edge["target"]
            if target not in deck_cards:
                info = scores[target]
                base_score = edge["score"]

                if card == commander:
                    info["commander_synergy"] += base_score
                    info["total"] += base_score * 5.0  # Commander synergy weighted 5x
                elif card in key_cards:
                    info["key_synergy"] += base_score
                    info["total"] += base_score * 2.0  # Key card synergy weighted 2x
                else:
                    info["total"] += base_score

                info["partners"].append((card, edge["score"], edge["signals"]))
                if edge["signals"] >= 2:
                    info["multi_sig"] += 1

    return dict(scores)
