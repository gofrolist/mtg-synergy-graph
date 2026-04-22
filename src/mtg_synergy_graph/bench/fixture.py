"""Pinned reference fixture for bench.py.

The fixture is the baseline against which ``bench.py audit``,
``--expect-identity``, and ``--rule`` ablation compare. Extends the
legacy ``tests/fixtures/golden_set_run.json`` shape with two new
per-commander fields (``scores`` and ``tensor_rows``) so the
persisted rule-contribution tensor has a reviewable git-diff form.

Design choices
--------------
* **JSON, not SQLite blob.** Pinned state lives in the repo; we want it
  diffable and merge-conflict-friendly. A binary tensor dump would be
  opaque.
* **Legacy fields preserved.** ``edhrec_top10``, ``hi_syn_hits``,
  ``hi_syn_total``, ``ndcg30``, ``on_page_hits``, ``top10`` stay as
  top-level keys on each entry so existing ``golden_set_track.py``
  workflows keep working during the transition window.
* **Tensor is authoritative.** Per-(cmdr, cand, rule) rows are the
  atoms of ``--expect-identity`` — if tensor rows match exactly, all
  derived aggregate scores are identical too (``score()`` is
  deterministic).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mtg_synergy_graph.bench.tensor import compute_config_hash
from mtg_synergy_graph.universal_scorer import (
    UniversalScore,
    score_all_universal,
)

#: Current fixture schema version. Bumped when the JSON layout changes
#: in a non-backward-compatible way. Load refuses to read a newer
#: fixture than it knows about.
SCHEMA_VERSION = 1


@dataclass(frozen=True)
class TensorRow:
    """One persisted rule-contribution cell."""

    candidate: str
    rule_id: str
    contribution: float
    idf_weight: float
    raw_count: int

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> TensorRow:
        return cls(
            candidate=d["candidate"],
            rule_id=d["rule_id"],
            contribution=d["contribution"],
            idf_weight=d["idf_weight"],
            raw_count=d["raw_count"],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate": self.candidate,
            "rule_id": self.rule_id,
            "contribution": self.contribution,
            "idf_weight": self.idf_weight,
            "raw_count": self.raw_count,
        }


@dataclass
class FixtureEntry:
    """One commander's pinned baseline."""

    commander: str
    scores: dict[str, float] = field(default_factory=dict)
    tensor_rows: list[TensorRow] = field(default_factory=list)
    # Legacy fields from golden_set_track — preserved for compat but not
    # interpreted by this module.
    legacy: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = dict(self.legacy)
        out["commander"] = self.commander
        out["scores"] = self.scores
        out["tensor_rows"] = [row.to_dict() for row in self.tensor_rows]
        return out

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> FixtureEntry:
        legacy = {k: v for k, v in d.items() if k not in ("commander", "scores", "tensor_rows")}
        return cls(
            commander=d["commander"],
            scores=dict(d.get("scores", {})),
            tensor_rows=[TensorRow.from_dict(r) for r in d.get("tensor_rows", [])],
            legacy=legacy,
        )


@dataclass(frozen=True)
class ScoreDelta:
    """One (commander, candidate) mismatch detected during identity check."""

    commander: str
    candidate: str
    live: float
    pinned: float

    @property
    def delta(self) -> float:
        return self.live - self.pinned


@dataclass(frozen=True)
class TensorDelta:
    """One (commander, candidate, rule_id) mismatch."""

    commander: str
    candidate: str
    rule_id: str
    live: float | None
    pinned: float | None


@dataclass
class IdentityReport:
    """Result of :meth:`PinnedFixture.assert_identity`."""

    score_mismatches: list[ScoreDelta] = field(default_factory=list)
    tensor_mismatches: list[TensorDelta] = field(default_factory=list)
    missing_commanders: list[str] = field(default_factory=list)
    config_hash_mismatch: str | None = None

    @property
    def is_identical(self) -> bool:
        return not (
            self.score_mismatches or self.tensor_mismatches or self.missing_commanders or self.config_hash_mismatch
        )


@dataclass
class PinnedFixture:
    """The full pinned baseline plus metadata."""

    config_hash: str
    created_at: str
    schema_version: int = SCHEMA_VERSION
    entries: list[FixtureEntry] = field(default_factory=list)

    # ---- I/O --------------------------------------------------------------

    @classmethod
    def load(cls, path: Path | str) -> PinnedFixture:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        schema = int(data.get("schema_version", 1))
        if schema > SCHEMA_VERSION:
            raise ValueError(
                f"fixture {path} declares schema_version={schema} but we only understand <= {SCHEMA_VERSION}"
            )
        return cls(
            config_hash=data.get("config_hash", ""),
            created_at=data.get("created_at", ""),
            schema_version=schema,
            entries=[FixtureEntry.from_dict(e) for e in data.get("entries", [])],
        )

    def write(self, path: Path | str) -> None:
        out = {
            "schema_version": self.schema_version,
            "config_hash": self.config_hash,
            "created_at": self.created_at,
            "entries": [e.to_dict() for e in self.entries],
        }
        Path(path).write_text(
            json.dumps(out, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    # ---- comparison -------------------------------------------------------

    def assert_identity(self, live: PinnedFixture) -> IdentityReport:
        """Compare this pinned fixture to a freshly-computed one.

        ``live`` is typically the output of :func:`score_commanders`
        run in-process with the current DB + scoring config. Returns a
        structured report so callers can render it however suits the
        CLI / test / hook context.
        """
        report = IdentityReport()
        if self.config_hash != live.config_hash:
            report.config_hash_mismatch = f"pinned={self.config_hash} live={live.config_hash}"

        pinned_by_cmdr = {e.commander: e for e in self.entries}
        live_by_cmdr = {e.commander: e for e in live.entries}

        for cmdr, pinned_entry in pinned_by_cmdr.items():
            live_entry = live_by_cmdr.get(cmdr)
            if live_entry is None:
                report.missing_commanders.append(cmdr)
                continue

            # Score-level diff (per candidate). Bitwise float equality is
            # appropriate: `score()` is deterministic.
            pinned_scores = pinned_entry.scores
            live_scores = live_entry.scores
            all_cands = set(pinned_scores) | set(live_scores)
            for cand in all_cands:
                p = pinned_scores.get(cand, 0.0)
                q = live_scores.get(cand, 0.0)
                if p != q:
                    report.score_mismatches.append(ScoreDelta(commander=cmdr, candidate=cand, live=q, pinned=p))

            # Tensor-level diff (per rule).
            pinned_tensor = {(r.candidate, r.rule_id): r.contribution for r in pinned_entry.tensor_rows}
            live_tensor = {(r.candidate, r.rule_id): r.contribution for r in live_entry.tensor_rows}
            all_keys = set(pinned_tensor) | set(live_tensor)
            for key in all_keys:
                p = pinned_tensor.get(key)
                q = live_tensor.get(key)
                if p != q:
                    report.tensor_mismatches.append(
                        TensorDelta(
                            commander=cmdr,
                            candidate=key[0],
                            rule_id=key[1],
                            live=q,
                            pinned=p,
                        )
                    )

        return report


def score_commander(
    conn: sqlite3.Connection,
    commander: str,
) -> tuple[dict[str, UniversalScore], list[TensorRow]]:
    """Score one commander and capture its tensor rows.

    Returns ``(per_candidate_scores, tensor_rows)``. This is the minimum
    scorer orchestration needed by ``--repin`` and ``--expect-identity``;
    Unit 4's ``bench.py audit`` layers NDCG + parallel dispatch on top.
    """
    rows: list[TensorRow] = []

    def sink(
        cmdr: str,
        cand: str,
        rule_id: str,
        contribution: float,
        idf_weight: float,
        raw_count: int,
    ) -> None:
        rows.append(
            TensorRow(
                candidate=cand,
                rule_id=rule_id,
                contribution=contribution,
                idf_weight=idf_weight,
                raw_count=raw_count,
            )
        )

    scores = score_all_universal(conn, [commander], tensor_sink=sink)
    return scores, rows


def build_fixture(
    conn: sqlite3.Connection,
    commanders: list[str],
    existing: PinnedFixture | None = None,
) -> PinnedFixture:
    """Score all commanders and produce a fresh fixture.

    If ``existing`` is provided, legacy non-score fields (edhrec_top10,
    hi_syn_hits, etc.) are carried forward per-commander so a ``--repin``
    doesn't wipe them. Only ``scores`` and ``tensor_rows`` are recomputed.
    """
    existing_by_cmdr = {e.commander: e for e in existing.entries} if existing is not None else {}

    entries: list[FixtureEntry] = []
    for cmdr in commanders:
        scores, tensor_rows = score_commander(conn, cmdr)
        legacy = existing_by_cmdr.get(cmdr)
        entries.append(
            FixtureEntry(
                commander=cmdr,
                scores={name: score.score for name, score in scores.items()},
                tensor_rows=tensor_rows,
                legacy=dict(legacy.legacy) if legacy is not None else {},
            )
        )

    return PinnedFixture(
        config_hash=compute_config_hash(),
        created_at=datetime.now(UTC).isoformat(timespec="seconds"),
        schema_version=SCHEMA_VERSION,
        entries=entries,
    )
