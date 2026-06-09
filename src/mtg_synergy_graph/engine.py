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
import math
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .db import open_db
from .penalties import (
    CandidateCache,
    build_candidate_cache,
    build_penalty_context,
)
from .universal_scorer import UniversalScore, score_all_universal

SPEC_VERSION = "1.2.2"
ENGINE_VERSION = "0.1.0"

#: Top-level card types that are NOT legal in EDH/Commander decks. Cards
#: whose ``card_types`` row contains any of these are hard-filtered before
#: scoring. The classic culprits are Planechase planes (e.g. ``Jund``,
#: ``Murasa``) which have triggers that match commander triggers but
#: aren't legal deck cards. Empty ``card_types`` is also rejected because
#: every legitimate EDH card has at least one top-level type.
NON_EDH_CARD_TYPES: frozenset[str] = frozenset(
    {
        "Plane",
        "Phenomenon",
        "Scheme",
        "Conspiracy",
        "Vanguard",
        "Dungeon",
    }
)

#: Sentinel rank for cards/commanders without an EDHREC rank. Sorted last
#: in the production sort key so ranked cards appear before unranked ones.
#: Shared between :func:`SynergyEngine.page` and the bench optimizer's
#: production-faithful sort path (``bench.optimize.score_commander_from_complements``).
UNRANKED_EDHREC_SENTINEL: int = 10**9

# ---------------------------------------------------------------------------
# Data classes (§8 + §9)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContributingPort:
    """A single port-match that fed into a candidate's score (§8)."""

    bucket: str
    port_type: str
    event_class: str
    valid_filter: str | None
    branch_kind: str
    raw_line: str | None


@dataclass(frozen=True)
class Recommendation:
    """A single ranked card in a recommendation page (§9)."""

    rank: int
    card: str
    total_score: float
    scores: dict[str, float]
    contributing_ports: list[ContributingPort]
    explanation: list[str] | None = None


@dataclass(frozen=True)
class RecommendationPage:
    """A paginated slice of the synergy ranking (§9 + §14)."""

    commander: list[str]
    color_identity: list[str]
    total: int
    offset: int
    limit: int
    generated_at: str
    forge_version: str
    spec_version: str
    engine_version: str
    items: list[Recommendation] = field(default_factory=list)


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


class SynergyEngine:
    """Public façade around the deterministic graph engine (§14)."""

    def __init__(
        self,
        db_path: str | Path,
    ):
        """Open the engine against a synergy.db.

        Scoring uses the universal port-complement matcher: score = count
        of distinct mechanical interactions between commander ports and
        candidate ports, weighted by specificity (IDF). No hand-tuned
        weights, no global penalties.
        """
        self._db_path = Path(db_path)
        # create=False: a mistyped/missing DB path must fail with a
        # rebuild hint, not silently score against an empty database.
        self._conn = open_db(self._db_path, create=False)
        self._forge_version: str = self._detect_forge_version()
        self._candidate_cache: CandidateCache | None = None
        self._score_cache: dict[tuple[str, ...], Any] = {}

    # ----- lifecycle -------------------------------------------------------

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> SynergyEngine:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ----- public API ------------------------------------------------------

    def metadata(self) -> dict[str, str]:
        return {
            "spec_version": SPEC_VERSION,
            "engine_version": ENGINE_VERSION,
            "forge_version": self._forge_version,
            "db_path": str(self._db_path),
        }

    def legal_cards(self, commander: str | Sequence[str]) -> list[str]:
        """Return every card legal for ``commander``: colour identity ⊆
        commander union (§6.9.1) AND a non-empty top-level type that is
        not in :data:`NON_EDH_CARD_TYPES` (planes, schemes, etc.) AND
        not flagged ``legal_commander = 0`` by Scryfall (acorn/un-set
        leakage from the Forge cardsfolder)."""
        cmdr_set = _normalise_commander(commander)
        cmdr_rows = self._fetch_cards(cmdr_set)
        identity = set(_color_identity_union(cmdr_rows))
        # Same back-compat gate as in penalties._bulk_load_candidates:
        # synergy.db files created before the legal_commander migration
        # don't have the column, so fall back to a literal 1.
        has_legal = any(r[1] == "legal_commander" for r in self._conn.execute("PRAGMA table_info(cards)").fetchall())
        legal_expr = "legal_commander" if has_legal else "1 AS legal_commander"
        cur = self._conn.execute(
            f"SELECT name, color_identity, card_types, {legal_expr} FROM cards WHERE name NOT IN ({{}})".format(
                ",".join("?" * len(cmdr_set))
            ),
            tuple(cmdr_set),
        )
        out = []
        for r in cur.fetchall():
            cand_types = (r["card_types"] or "").split()
            if not cand_types:
                continue
            if any(t in NON_EDH_CARD_TYPES for t in cand_types):
                continue
            if r["legal_commander"] == 0:
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
        cache_key = tuple(cmdr_set)
        universal = self._score_cache.get(cache_key)
        if universal is None:
            if self._candidate_cache is None:
                self._candidate_cache = build_candidate_cache(self._conn)
            universal = score_all_universal(
                self._conn,
                cmdr_set,
                candidate_cache=self._candidate_cache,
            )
            self._score_cache[cache_key] = universal
        us = universal.get(card)
        if us is None:
            # Deferred: scoring.py → signals.py → scoring.py circular import
            from .scoring import empty_buckets

            scores: dict[str, float] = empty_buckets()
        else:
            scores = us.to_legacy_buckets()

        # Rule-covered names derived from the cached universal scores;
        # cheap because the score dict is already in-memory.
        rule_covered_names: set[str] = {
            name for name, u in universal.items() if u is not None and len(u.distinct_rules) >= 1
        }
        explanation = (
            self._render_explanation(
                card,
                scores,
                us,
                commander_set=cmdr_set,
                rule_covered_names=rule_covered_names,
            )
            if include_explanation
            else None
        )

        return Recommendation(
            rank=0,
            card=card,
            total_score=scores["total"],
            scores=scores,
            contributing_ports=[],
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
                f"oracle_id {oracle_id!r} is not in synergy.db (card absent from the Forge cardsfolder source)"
            )
        return row["name"]

    def oracle_id_for_name(self, name: str) -> str | None:
        """Return the ``oracle_id`` for a card name, or ``None`` if not found."""
        row = self._conn.execute(
            "SELECT oracle_id FROM cards WHERE name = ? LIMIT 1",
            (name,),
        ).fetchone()
        return row["oracle_id"] if row else None

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
        if self._candidate_cache is None:
            self._candidate_cache = build_candidate_cache(self._conn)
        penalty_ctx = build_penalty_context(
            self._conn,
            cmdr_set,
            candidate_cache=self._candidate_cache,
        )
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
            # Scryfall `legal_commander = 0` (acorn / silver-border /
            # banned) — hard drop before scoring.
            if row.get("legal_commander") == 0:
                continue
            cand_pips = {t.strip() for t in (row["color_identity"] or "").split(",") if t.strip()}
            if not (cand_pips - identity_set):
                legal.add(name)

        # -- Universal port-complement scoring --------------------------------
        _UNRANKED = UNRANKED_EDHREC_SENTINEL
        cmc_lookup: dict[str, float] = {}
        rank_lookup: dict[str, int] = {}
        for name, row in penalty_ctx.candidate_rows.items():
            cmc_lookup[name] = row["cmc"] if row["cmc"] is not None else 99.0
            raw_rank = row.get("edhrec_rank")
            rank_lookup[name] = int(raw_rank) if raw_rank is not None else _UNRANKED

        cache_key = tuple(cmdr_set)
        universal = self._score_cache.get(cache_key)
        if universal is None:
            # _candidate_cache is guaranteed populated above (line ~319).
            universal = score_all_universal(
                self._conn,
                cmdr_set,
                candidate_cache=self._candidate_cache,
            )
            self._score_cache[cache_key] = universal
        # Carry UniversalScore alongside buckets so _render_explanation
        # can surface rule-specific metadata (e.g., self_bridging_cascade
        # path_info) beyond what the legacy bucket dict exposes.
        ranked: list[tuple[str, dict[str, float], UniversalScore]] = []
        for cand in legal:
            us = universal.get(cand)
            if us is None:
                continue
            ranked.append((cand, us.to_legacy_buckets(), us))
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

        # Plan 2026-04-23-003 Unit 7 — rule-covered candidate set fed
        # to _render_explanation so the embedding-nearest-neighbor
        # lookup filters to structurally-meaningful peers only. Built
        # once per page(), not per candidate.
        rule_covered_names: set[str] = {cand for cand, _buckets, us in ranked if len(us.distinct_rules) >= 1}

        items: list[Recommendation] = []
        for i, (cand, buckets, us) in enumerate(window):
            explanation = (
                self._render_explanation(
                    cand,
                    buckets,
                    us,
                    commander_set=cmdr_set,
                    rule_covered_names=rule_covered_names,
                )
                if include_explanations
                else None
            )
            items.append(
                Recommendation(
                    rank=offset + i + 1,
                    card=cand,
                    total_score=buckets["total"],
                    scores=buckets,
                    contributing_ports=[],
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
        universal_score: UniversalScore | None = None,
        *,
        commander_set: Sequence[str] | None = None,
        rule_covered_names: set[str] | None = None,
    ) -> list[str]:
        """Plain-English narrator (§8) — optional, for debug surfaces.

        When ``universal_score`` is provided, surfaces rule-specific
        metadata from ``PortComplement.path_info`` alongside the
        bucket-summary prose (currently: one ``self_bridging_cascade:``
        line per firing).

        When ``universal_score.embedding_contribution > 0`` and both
        ``commander_set`` + ``rule_covered_names`` are supplied, renders
        an extra ``embedding_contribution:`` line decomposed into
        ``decay × cosine``, plus a ``nearest covered neighbors:`` line
        with the top-3 rule-covered cards by cosine similarity. Plan
        2026-04-23-003 Unit 7 / FR6.
        """
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
        if universal_score is not None:
            seen_paths: set[str] = set()
            for c in universal_score.complements:
                if c.rule_id != "self_bridging_cascade" or not c.path_info:
                    continue
                if c.path_info in seen_paths:
                    continue
                seen_paths.add(c.path_info)
                lines.append(f"self_bridging_cascade: {c.path_info}")
            # Plan 2026-04-23-003 Unit 7 — render the embedding
            # contribution decomposition + nearest covered neighbors
            # when the term fired. Silently skip if vectors /
            # commander target cannot be rebuilt at render time
            # (graceful-fallback per plan D6).
            if universal_score.embedding_contribution > 0 and commander_set is not None:
                lines.extend(
                    self._render_embedding_block(
                        card=card,
                        universal_score=universal_score,
                        commander_set=commander_set,
                        rule_covered_names=rule_covered_names or set(),
                    )
                )
        return lines or [f"No mechanical synergy detected for {card}."]

    def _render_embedding_block(
        self,
        *,
        card: str,
        universal_score: UniversalScore,
        commander_set: Sequence[str],
        rule_covered_names: set[str],
    ) -> list[str]:
        """Render the ``embedding_contribution:`` + neighbors lines.

        Re-loads vectors + commander-target lazily here instead of
        threading them through ``page()`` (plan design decision:
        Option 2 re-load, not Option 1 pass-through — keeps the
        scoring path data-free of render concerns). Silently returns
        ``[]`` if vectors or target are unavailable — the scorer has
        already computed the contribution value, so the score line
        never surprises the user even when the decomposition can't
        be rendered.
        """
        # Local imports — embeddings subpackage is cold on the import
        # path unless actually rendering an embedding explanation.
        from .embeddings.commander_target import build_commander_target_vector
        from .embeddings.contribution import (
            _EMBEDDING_K,
            load_card_embeddings_verified,
        )

        try:
            vectors = load_card_embeddings_verified(self._conn)
        except Exception:
            # Never raise from --explain: the scorer has already computed
            # the contribution value, so the score line never surprises
            # the user even when the decomposition cannot be rendered.
            return []
        cand_vec = vectors.get(card)
        if cand_vec is None:
            return []
        cmdr_target = build_commander_target_vector(
            tuple(commander_set),
            vectors,
            edhrec_conn=None,
        )
        if cmdr_target is None:
            return []

        cosine = float(cand_vec @ cmdr_target)
        decay = math.exp(-_EMBEDDING_K * len(universal_score.distinct_rules))
        out: list[str] = [
            f"  embedding_contribution: +{universal_score.embedding_contribution:.3f} "
            f"(decay × cosine = {decay:.2f} × {cosine:.3f})"  # noqa: RUF001 — multiplication-sign glyph is the user-facing explain format.
        ]
        neighbors = _nearest_covered_neighbors(
            cand_vec,
            vectors,
            rule_covered_names=rule_covered_names - {card},
            k=3,
        )
        if neighbors:
            rendered = ", ".join(f"{n} (cosine {s:.2f})" for n, s in neighbors)
            out.append(f"    nearest covered neighbors: {rendered}")
        return out


def _nearest_covered_neighbors(
    vec: Any,
    vectors: dict[str, Any],
    *,
    rule_covered_names: set[str],
    k: int = 3,
) -> list[tuple[str, float]]:
    """Return the top-``k`` ``(name, cosine)`` pairs from ``vectors``
    filtered to ``rule_covered_names``.

    All vectors are L2-normalized by construction (plan R1 + Unit 4),
    so cosine == dot product. When ``rule_covered_names`` is empty or
    no name in it has a vector, returns ``[]``.
    """
    scored: list[tuple[str, float]] = []
    for name in rule_covered_names:
        v = vectors.get(name)
        if v is None:
            continue
        cosine = float(v @ vec)
        scored.append((name, cosine))
    scored.sort(key=lambda t: -t[1])
    return scored[:k]
