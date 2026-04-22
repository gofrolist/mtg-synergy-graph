"""Pinned reference fixture for bench.py.

The fixture is the git-committed baseline against which ``bench.py
audit`` and ``--expect-identity`` compare. The persisted rule-
contribution tensor lives in SQLite (``rule_contributions`` table) —
this JSON only carries the per-commander top-N scores + config_hash
so git diffs stay readable and the file stays small.

Design choices
--------------
* **Two-layer storage.** JSON here carries scoring fingerprints (top-N
  scores per commander + config_hash + legacy fields). SQLite carries
  the full per-(cmdr, cand, rule) contribution tensor under the same
  config_hash. ``--repin`` writes both; ``--expect-identity`` checks
  JSON; ``--rule`` / ``--inspect`` / ``--collinearity`` query SQLite.
* **Top-N scores are sufficient for identity.** ``score()`` is a
  deterministic sum over ``complements × idf_weights``. If the top-N
  scores match bitwise and the config_hash matches, every underlying
  contribution matches too; there is nothing a deeper comparison could
  catch that a full top-N bitwise diff does not.
* **Legacy fields preserved.** ``edhrec_top10``, ``hi_syn_hits``,
  ``hi_syn_total``, ``ndcg30``, ``on_page_hits``, ``top10`` stay as
  top-level keys on each entry so existing ``golden_set_track.py``
  workflows keep working during the transition window.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mtg_synergy_graph.bench.tensor import TensorWriter, compute_config_hash
from mtg_synergy_graph.universal_scorer import (
    TensorRow,
    TensorSink,
    UniversalScore,
    score_all_universal,
)

#: Current fixture schema version. Bumped when the JSON layout changes
#: in a non-backward-compatible way. Load refuses to read a newer
#: fixture than it knows about.
SCHEMA_VERSION = 2

#: Number of top-scored candidates to pin per commander. Tuned so the
#: fixture stays < ~5 MB on the 100-commander golden set while still
#: comfortably exceeding the NDCG@30 horizon (we pin the top 100 so a
#: rule that shuffles ranks around 30 still lands inside the pinned
#: window and gets caught by --expect-identity).
TOP_N_PINNED = 100


# ``TensorRow`` is defined in ``universal_scorer`` so the sink contract
# and the dataclass ship together. Re-export from this module for
# backwards compatibility with callers that already import it from
# ``bench.fixture``.


@dataclass
class FixtureEntry:
    """One commander's pinned baseline (scoring fingerprint)."""

    commander: str
    #: Top-N candidates by score desc, as ``{candidate_name: score}``.
    #: Full-map storage is deliberately avoided — see module docstring.
    scores: dict[str, float] = field(default_factory=dict)
    #: Pass-through of legacy ``golden_set_track.py`` fields so
    #: existing workflows keep reading them.
    legacy: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = dict(self.legacy)
        out["commander"] = self.commander
        out["scores"] = self.scores
        return out

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> FixtureEntry:
        legacy = {k: v for k, v in d.items() if k not in ("commander", "scores", "tensor_rows")}
        return cls(
            commander=d["commander"],
            scores=dict(d.get("scores", {})),
            legacy=legacy,
        )


@dataclass(frozen=True)
class ScoreDelta:
    """One (commander, candidate) score mismatch."""

    commander: str
    candidate: str
    live: float
    pinned: float

    @property
    def delta(self) -> float:
        return self.live - self.pinned


@dataclass
class IdentityReport:
    """Result of :meth:`PinnedFixture.assert_identity`."""

    score_mismatches: list[ScoreDelta] = field(default_factory=list)
    missing_commanders: list[str] = field(default_factory=list)
    config_hash_mismatch: str | None = None

    @property
    def is_identical(self) -> bool:
        return not (self.score_mismatches or self.missing_commanders or self.config_hash_mismatch)


@dataclass
class PinnedFixture:
    """Git-committed scoring fingerprint + metadata."""

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

        Scoring is deterministic, so bitwise float equality is the
        right relation. Any score mismatch on any pinned candidate is
        a real difference; the identity check does not tolerate it.
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

            pinned_scores = pinned_entry.scores
            live_scores = live_entry.scores
            all_cands = set(pinned_scores) | set(live_scores)
            for cand in all_cands:
                p = pinned_scores.get(cand, 0.0)
                q = live_scores.get(cand, 0.0)
                if p != q:
                    report.score_mismatches.append(ScoreDelta(commander=cmdr, candidate=cand, live=q, pinned=p))

        return report


# ---------------------------------------------------------------------------
# Scoring orchestration
# ---------------------------------------------------------------------------


def _top_n_scores(scores: dict[str, UniversalScore], n: int) -> dict[str, float]:
    """Return the top-``n`` (candidate → score) entries from a scoring run.

    Ties broken by candidate name for determinism (so re-pinning is
    reproducible). Float scores preserved bitwise.
    """
    ranked = sorted(
        ((name, s.score) for name, s in scores.items()),
        key=lambda kv: (-kv[1], kv[0]),
    )
    return dict(ranked[:n])


def score_commander(
    conn: sqlite3.Connection,
    commander: str,
    tensor_sink: TensorSink | None = None,
    top_n: int = TOP_N_PINNED,
) -> tuple[dict[str, float], list[TensorRow]]:
    """Score one commander, returning (top-N scores, tensor_rows).

    When a downstream ``tensor_sink`` is wired (typically a
    ``TensorWriter`` during ``--repin``) the returned row list is
    empty — the sink is the single persistence path and the local
    list would be redundant memory pressure on a ~5k-row per-commander
    result. Tests that exercise the return list call without a sink.
    """
    rows: list[TensorRow] = []
    capture_locally = tensor_sink is None

    def sink(row: TensorRow) -> None:
        if capture_locally:
            rows.append(row)
        else:
            # tensor_sink is not None — pyright narrows on the outer
            # closure capture.
            assert tensor_sink is not None
            tensor_sink(row)

    scores = score_all_universal(conn, [commander], tensor_sink=sink)
    return _top_n_scores(scores, top_n), rows


def build_fixture(
    conn: sqlite3.Connection,
    commanders: list[str],
    existing: PinnedFixture | None = None,
    tensor_writer: TensorWriter | None = None,
) -> PinnedFixture:
    """Score all commanders and produce a fresh fixture.

    When ``tensor_writer`` is provided, the persisted tensor is also
    written to its SQLite-backed sink. ``--repin`` passes one here so
    Unit 6's ``--rule`` / ``--inspect`` / ``--collinearity`` queries
    have data to read afterward. ``--expect-identity`` omits the writer
    so it does not mutate DB state while auditing.
    """
    existing_by_cmdr = {e.commander: e for e in existing.entries} if existing is not None else {}

    entries: list[FixtureEntry] = []
    sink = tensor_writer.sink if tensor_writer is not None else None
    for cmdr in commanders:
        top_scores, _rows = score_commander(conn, cmdr, tensor_sink=sink)
        legacy = existing_by_cmdr.get(cmdr)
        entries.append(
            FixtureEntry(
                commander=cmdr,
                scores=top_scores,
                legacy=dict(legacy.legacy) if legacy is not None else {},
            )
        )

    return PinnedFixture(
        config_hash=compute_config_hash(),
        created_at=datetime.now(UTC).isoformat(timespec="seconds"),
        schema_version=SCHEMA_VERSION,
        entries=entries,
    )
