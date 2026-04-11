"""SynergyEngine — public package API (SPEC §14).

This is the surface that ``mtg-edh-builder`` (and any other consumer) uses
when it imports ``mtg_synergy_graph``::

    from mtg_synergy_graph import SynergyEngine

    engine = SynergyEngine(db_path="synergy.db")
    page = engine.page(commander="Korvold, Fae-Cursed King", offset=0, limit=100)
    for rec in page.items:
        print(rec.rank, rec.card, rec.total_score)

Pagination is mandatory: there is no top-N truncation. ``limit=1_000_000``
returns every legal card; ``offset=500, limit=100`` returns rank 501-600.
"""

from __future__ import annotations

import datetime as _dt
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .db import open_db
from .penalties import (
    HARD_FILTER_SCORE,
    apply_penalties,
    apply_penalties_ctx,
    build_penalty_context,
)
from .scoring import empty_buckets, score_all_candidates

SPEC_VERSION = "1.2.2"
ENGINE_VERSION = "0.1.0"

#: Top-level card types that are NOT legal in EDH/Commander decks. Cards
#: whose ``card_types`` row contains any of these are hard-filtered before
#: scoring. The classic culprits are Planechase planes (e.g. ``Jund``,
#: ``Murasa``) which have triggers that match commander triggers but
#: aren't legal deck cards. Empty ``card_types`` is also rejected because
#: every legitimate EDH card has at least one top-level type.
NON_EDH_CARD_TYPES: frozenset[str] = frozenset({
    "Plane", "Phenomenon", "Scheme", "Conspiracy", "Vanguard", "Dungeon",
})

# ---------------------------------------------------------------------------
# Data classes (§8 + §9)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContributingPort:
    """A single port-match that fed into a candidate's score (§8)."""

    bucket:       str
    port_type:    str
    event_class:  str
    valid_filter: str | None
    branch_kind:  str
    raw_line:     str | None


@dataclass(frozen=True)
class Recommendation:
    """A single ranked card in a recommendation page (§9)."""

    rank:               int
    card:               str
    total_score:        float
    scores:             dict[str, float]
    contributing_ports: list[ContributingPort]
    explanation:        list[str] | None = None


@dataclass(frozen=True)
class RecommendationPage:
    """A paginated slice of the synergy ranking (§9 + §14)."""

    commander:      list[str]
    color_identity: list[str]
    total:          int
    offset:         int
    limit:          int
    generated_at:   str
    forge_version:  str
    spec_version:   str
    engine_version: str
    items:          list[Recommendation] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


def _normalise_commander(commander: str | Sequence[str]) -> list[str]:
    if isinstance(commander, str):
        return [commander]
    return list(commander)


def _color_identity_union(rows: list[sqlite3.Row]) -> list[str]:
    pips: set[str] = set()
    for r in rows:
        for tok in (r["color_identity"] or "").split(","):
            tok = tok.strip()
            if tok:
                pips.add(tok)
    return sorted(pips)


_PORT_BUCKETS = frozenset(
    {"port_match", "cost_synergy", "scaling", "amplifier", "lord", "replacement"}
)
_SYNTH_BUCKETS = frozenset(
    {"catchall", "deck_hints", "chain", "internal_synergy",
     "resource_density", "effect_resonance", "replacement_resonance"}
)


def _build_contributing_ports(
    conn: sqlite3.Connection,
    candidate: str,
    raw_matches: list[dict[str, Any]],
) -> list[ContributingPort]:
    """Hydrate the per-candidate match list into ContributingPort rows.

    For port-derived buckets (``port_match``, ``cost_synergy``, etc.) we
    look up at most one representative ``card_ports`` row per bucket from
    a single SQL query, cached locally to avoid the H-7 double-fetch.
    Synthetic buckets (``catchall``, ``deck_hints``, ``chain``,
    ``internal_synergy``) don't need a port row.
    """
    out: list[ContributingPort] = []
    seen: set[tuple[str, str, str]] = set()

    needs_port_fetch = any(m.get("bucket") in _PORT_BUCKETS for m in raw_matches)
    port_rows: list[dict[str, Any]] = []
    if needs_port_fetch:
        cur = conn.execute(
            "SELECT port_type, event_class, valid_filter, branch_kind, raw_line "
            "FROM card_ports WHERE card_name = ? LIMIT 200",
            (candidate,),
        )
        port_rows = [dict(r) for r in cur.fetchall()]

    for m in raw_matches:
        bucket = m.get("bucket", "")
        if bucket in _PORT_BUCKETS:
            for r in port_rows:
                key = (bucket, r["port_type"], r["event_class"] or "")
                if key in seen:
                    continue
                seen.add(key)
                out.append(
                    ContributingPort(
                        bucket=bucket,
                        port_type=r["port_type"],
                        event_class=r["event_class"] or "",
                        valid_filter=r["valid_filter"],
                        branch_kind=r["branch_kind"] or "root",
                        raw_line=r["raw_line"],
                    )
                )
                # One representative row per bucket is enough — full
                # detail is in synergy_edges.
                break
        elif bucket in _SYNTH_BUCKETS:
            key = (bucket, "*", "")
            if key not in seen:
                seen.add(key)
                out.append(
                    ContributingPort(
                        bucket=bucket,
                        port_type="*",
                        event_class=str(m.get("trigger_event") or m.get("event_class") or ""),
                        valid_filter=None,
                        branch_kind=str(m.get("branch_kind") or "root"),
                        raw_line=None,
                    )
                )
    return out


class SynergyEngine:
    """Public façade around the deterministic graph engine (§14)."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        graph_metrics: bool = False,
        chain_matches: bool = False,
    ):
        """Open the engine against a synergy.db.

        ``graph_metrics`` defaults to ``False`` because §6.8 causal graph
        metrics are expensive even with the Phase 4.6 numpy fast path:

        * cold (first call):  ~30 s on a 32 k-card import (graph build +
                              numpy personalised PageRank + per-pair Jaccard)
        * warm (cached adj):  ~10 s per page (PR + Jaccard only)

        Enabling it boosts EDHREC Hi-Syn coverage from 9.6 % → 10.8 %
        (the LightGBM ``graph_pagerank`` feature) but slightly softens
        OnPage. Use it for power-user / offline analysis runs; the default
        Round 8 baseline is preserved when this flag is False.

        ``chain_matches`` is similarly opt-in: 2-hop chain detection
        re-walks the trigger feeders for every intermediate, which is the
        dominant cost on a full Forge import. Disabled by default; enable
        explicitly when you need ``chain_score`` populated.
        """
        self._db_path = Path(db_path)
        self._conn = open_db(self._db_path)
        self._forge_version: str = self._detect_forge_version()
        self._use_graph_metrics: bool = graph_metrics
        self._use_chain_matches: bool = chain_matches
        # Phase 4.6: when graph_metrics is enabled, the causal graph is
        # built on first use and reused across page() calls. The build
        # costs ~17 s on a 32 k-card import — far too expensive to repeat
        # per request. The cache is per-process; restarting clears it.
        # Phase 4.7: same idea for the scipy CSR matrix — built once,
        # reused across commanders. This is what makes graph_metrics
        # warm-call cost <1 s.
        self._adjacency_cache: dict[str, set[str]] | None = None
        self._scipy_pr_context: Any = None

    # ----- lifecycle -------------------------------------------------------

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "SynergyEngine":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ----- public API ------------------------------------------------------

    def metadata(self) -> dict[str, str]:
        return {
            "spec_version":   SPEC_VERSION,
            "engine_version": ENGINE_VERSION,
            "forge_version":  self._forge_version,
            "db_path":        str(self._db_path),
        }

    def legal_cards(self, commander: str | Sequence[str]) -> list[str]:
        """Return every card legal for ``commander``: colour identity ⊆
        commander union (§6.9.1) AND a non-empty top-level type that is
        not in :data:`NON_EDH_CARD_TYPES` (planes, schemes, etc.)."""
        cmdr_set = _normalise_commander(commander)
        cmdr_rows = self._fetch_cards(cmdr_set)
        identity = set(_color_identity_union(cmdr_rows))
        cur = self._conn.execute(
            "SELECT name, color_identity, card_types FROM cards "
            "WHERE name NOT IN ({})".format(",".join("?" * len(cmdr_set))),
            tuple(cmdr_set),
        )
        out = []
        for r in cur.fetchall():
            cand_types = (r["card_types"] or "").split()
            if not cand_types:
                continue
            if any(t in NON_EDH_CARD_TYPES for t in cand_types):
                continue
            cand_pips = {t.strip() for t in (r["color_identity"] or "").split(",") if t.strip()}
            if not (cand_pips - identity):
                out.append(r["name"])
        return sorted(out)

    def score_one(
        self,
        commander: str | Sequence[str],
        card: str,
        *,
        include_explanation: bool = False,
    ) -> Recommendation:
        cmdr_set = _normalise_commander(commander)
        self._ensure_graph_caches()
        result = score_all_candidates(
            self._conn,
            cmdr_set,
            use_graph_metrics=self._use_graph_metrics,
            use_chain_matches=self._use_chain_matches,
            adjacency_cache=self._adjacency_cache,
            scipy_pr_context=self._scipy_pr_context,
        )
        buckets = result.buckets.get(card)
        raw_matches = result.matches.get(card, [])
        if buckets is None:
            scores: dict[str, float] = empty_buckets()
        else:
            scores = dict(buckets)

        adjusted = apply_penalties(self._conn, cmdr_set, card, scores["total"])
        scores["total"] = adjusted

        contributing = _build_contributing_ports(self._conn, card, raw_matches)
        explanation = (
            self._render_explanation(card, scores, contributing)
            if include_explanation
            else None
        )

        return Recommendation(
            rank=0,
            card=card,
            total_score=adjusted,
            scores=scores,
            contributing_ports=contributing,
            explanation=explanation,
        )

    def resolve_oracle_id(self, oracle_id: str) -> str:
        """Return the ``cards.name`` row that matches ``oracle_id``.

        Raises ``LookupError`` when the oracle_id is not present in
        ``synergy.db``. Callers can catch this distinctly from
        :class:`ValueError` (which ``page()`` raises for unknown
        commander *names*) to distinguish "card not in Forge source"
        from "name lookup failed".
        """
        row = self._conn.execute(
            "SELECT name FROM cards WHERE oracle_id = ? LIMIT 1",
            (oracle_id,),
        ).fetchone()
        if row is None:
            raise LookupError(
                f"oracle_id {oracle_id!r} is not in synergy.db "
                "(card absent from the Forge cardsfolder source)"
            )
        return row["name"]

    def page_by_oracle_id(
        self,
        oracle_id: str | Sequence[str],
        *,
        offset: int = 0,
        limit: int = 100,
        include_explanations: bool = False,
    ) -> RecommendationPage:
        """Paginate by Scryfall oracle_id instead of Forge card name.

        Resolves each oracle_id → synergy.db card name via
        :meth:`resolve_oracle_id`, then delegates to :meth:`page`. This is
        the preferred entry point for callers that already hold Scryfall
        identifiers (e.g. EDHREC comparison harnesses, API clients that
        key on oracle_id). For DFC commanders it sidesteps the
        front-face-vs-combined-name mismatch entirely.

        ``oracle_id`` may be a single string or a sequence for partner
        pairs.

        Raises:
            LookupError: when any supplied oracle_id is not in synergy.db.
        """
        if isinstance(oracle_id, str):
            oids: tuple[str, ...] = (oracle_id,)
        else:
            oids = tuple(oracle_id)
        if not oids:
            raise ValueError("at least one oracle_id is required")
        names = [self.resolve_oracle_id(oid) for oid in oids]
        return self.page(
            names,
            offset=offset,
            limit=limit,
            include_explanations=include_explanations,
        )

    def page(
        self,
        commander: str | Sequence[str],
        *,
        offset: int = 0,
        limit: int = 100,
        include_explanations: bool = False,
    ) -> RecommendationPage:
        """Return a paginated ranking window for ``commander``.

        Pagination semantics:

        * ``offset=0, limit=100`` → top 100
        * ``offset=500, limit=100`` → rank 501-600
        * ``limit=1_000_000`` → entire ranking (with ``total == len(items)``)

        ``offset`` < 0 or ``limit`` <= 0 raise ``ValueError``.
        """
        if offset < 0:
            raise ValueError("offset must be >= 0")
        if limit <= 0:
            raise ValueError("limit must be > 0")

        cmdr_set = _normalise_commander(commander)
        cmdr_rows = self._fetch_cards(cmdr_set)
        if not cmdr_rows:
            raise ValueError(f"unknown commander: {commander!r}")

        identity = _color_identity_union(cmdr_rows)
        identity_set = set(identity)
        cmdr_name_set = set(cmdr_set)

        # Bulk-load the data the penalty layer needs. The same dict is
        # reused below for legality (avoids a second SELECT name FROM cards
        # in legal_cards()).
        penalty_ctx = build_penalty_context(self._conn, cmdr_set)
        legal: set[str] = set()
        for name, row in penalty_ctx.candidate_rows.items():
            if name in cmdr_name_set:
                continue
            # Drop non-EDH-legal types (planes, schemes, etc.) and any
            # row whose card_types is empty — every real card has at
            # least one top-level type, so empty means a malformed
            # entry that shouldn't be ranked.
            cand_types = (row["card_types"] or "").split()
            if not cand_types:
                continue
            if any(t in NON_EDH_CARD_TYPES for t in cand_types):
                continue
            cand_pips = {
                t.strip()
                for t in (row["color_identity"] or "").split(",")
                if t.strip()
            }
            if not (cand_pips - identity_set):
                legal.add(name)

        self._ensure_graph_caches()
        result = score_all_candidates(
            self._conn,
            cmdr_set,
            use_graph_metrics=self._use_graph_metrics,
            use_chain_matches=self._use_chain_matches,
            adjacency_cache=self._adjacency_cache,
            scipy_pr_context=self._scipy_pr_context,
        )

        ranked: list[tuple[str, dict[str, float], list[dict[str, Any]]]] = []
        for cand in legal:
            buckets_src = result.buckets.get(cand)
            buckets = dict(buckets_src) if buckets_src is not None else empty_buckets()
            raw_matches = list(result.matches.get(cand, ()))
            adjusted = apply_penalties_ctx(penalty_ctx, cand, buckets["total"])
            if adjusted <= HARD_FILTER_SCORE / 2:
                continue
            buckets["total"] = adjusted
            ranked.append((cand, buckets, raw_matches))

        # Tiebreak in order:
        #   1. lower mechanical total (reversed to highest-first)
        #   2. lower CMC — cheaper cards are more flexible
        #   3. lower edhrec_rank — within mechanically-equal clusters,
        #      more popular cards rise (pure tiebreaker; never changes
        #      the displayed mechanical score). Aligned with the
        #      "EDHREC as tiebreaker only" rule — cards without an
        #      edhrec_rank slot below every ranked card in the same
        #      tie rather than being penalised outright.
        #   4. alphabetical — stable final fallback
        #
        # Without (3), the ~62-card clusters that share a total like
        # 18.0 degenerate into CMC + alphabetical order, which made
        # the top of the list look arbitrary for counter-payoff
        # commanders like Kyler.
        _UNRANKED = 10**9
        cmc_lookup: dict[str, float] = {}
        rank_lookup: dict[str, int] = {}
        for name, row in penalty_ctx.candidate_rows.items():
            cmc_lookup[name] = row["cmc"] if row["cmc"] is not None else 99.0
            raw_rank = row.get("edhrec_rank")
            rank_lookup[name] = int(raw_rank) if raw_rank is not None else _UNRANKED
        ranked.sort(
            key=lambda r: (
                -r[1]["total"],
                cmc_lookup.get(r[0], 99.0),
                rank_lookup.get(r[0], _UNRANKED),
                r[0],
            )
        )
        total = len(ranked)
        window = ranked[offset : offset + limit]

        items: list[Recommendation] = []
        for i, (cand, buckets, raw_matches) in enumerate(window):
            contributing = _build_contributing_ports(self._conn, cand, raw_matches)
            explanation = (
                self._render_explanation(cand, buckets, contributing)
                if include_explanations
                else None
            )
            items.append(
                Recommendation(
                    rank=offset + i + 1,
                    card=cand,
                    total_score=buckets["total"],
                    scores=buckets,
                    contributing_ports=contributing,
                    explanation=explanation,
                )
            )

        return RecommendationPage(
            commander=cmdr_set,
            color_identity=identity,
            total=total,
            offset=offset,
            limit=limit,
            generated_at=_dt.datetime.now(_dt.UTC).isoformat(),
            forge_version=self._forge_version,
            spec_version=SPEC_VERSION,
            engine_version=ENGINE_VERSION,
            items=items,
        )

    # ----- internals -------------------------------------------------------

    def _ensure_graph_caches(self) -> None:
        """Lazily build the adjacency + scipy CSR caches on first use.

        Both caches are commander-independent so they're built exactly
        once per ``SynergyEngine`` lifetime. Skipped entirely when
        ``graph_metrics=False`` (the default).
        """
        if not self._use_graph_metrics or self._adjacency_cache is not None:
            return
        from .graph_metrics import (
            HAS_SCIPY,
            build_causal_graph,
            build_scipy_pr_context,
        )
        self._adjacency_cache = build_causal_graph(self._conn)
        if HAS_SCIPY:
            self._scipy_pr_context = build_scipy_pr_context(self._adjacency_cache)

    def _fetch_cards(self, names: Sequence[str]) -> list[sqlite3.Row]:
        if not names:
            return []
        return list(
            self._conn.execute(
                "SELECT * FROM cards WHERE name IN ({})".format(",".join("?" * len(names))),
                tuple(names),
            ).fetchall()
        )

    def _detect_forge_version(self) -> str:
        # Phase 3 placeholder. Real implementation reads
        # data/forge/.git/HEAD or a version stamp baked into the import.
        return "unknown"

    def _render_explanation(
        self,
        card: str,
        scores: dict[str, float],
        contributing: list[ContributingPort],
    ) -> list[str]:
        """Plain-English narrator (§8) — optional, for debug surfaces."""
        lines: list[str] = []
        if scores.get("port_match", 0):
            lines.append(f"{card} feeds the commander's triggers (+{scores['port_match']:.0f}).")
        if scores.get("cost_synergy", 0):
            lines.append(f"{card}'s costs match the commander's payoff structure (+{scores['cost_synergy']:.0f}).")
        if scores.get("amplifier", 0):
            lines.append(f"{card} amplifies the commander's ETB triggers (+{scores['amplifier']:.0f}).")
        if scores.get("lord", 0):
            lines.append(f"{card} is a lord-style buff for the commander's tribe (+{scores['lord']:.0f}).")
        if scores.get("scaling", 0):
            lines.append(f"{card} scales with the commander's board (+{scores['scaling']:.0f}).")
        if scores.get("deck_hints", 0):
            lines.append(f"Forge deck-hint metadata flags {card} as a fit (+{scores['deck_hints']:.0f}).")
        if scores.get("internal_synergy", 0):
            lines.append(f"{card} feeds the partner-pair engine (+{scores['internal_synergy']:.0f}).")
        if scores.get("replacement", 0):
            lines.append(f"{card} blocks a commander trigger ({scores['replacement']:.0f}).")
        return lines or [f"No mechanical synergy detected for {card}."]
