"""OracleConfigInputs + compute_oracle_hash + refuse-to-run contract.

Offline analog of ``src/mtg_synergy_graph/universal_scorer.py``'s
``ScoringConfigInputs`` and ``src/mtg_synergy_graph/bench/tensor.py``'s
``compute_config_hash``. The meta-principle is the same — mechanically
enforce that consumers never compare a sidecar built under one config
against inputs computed under a different config.

Consumers:

- ``scripts/forge_oracle.py build`` — writes the hash into
  ``oracle_config`` at the end of every build.
- ``scripts/forge_oracle.py propose-rules`` (Unit 8) — calls
  ``verify_current_or_raise`` at the top; stale hash → exit 2.
- ``scripts/bench.py audit --vs-forge-oracle`` (Unit 7) — same.
- ``scripts/gap_report.py`` (Unit 6) — checks stored hash but falls
  back to ``forge_signal = 1.0`` instead of raising, because
  gap_report is the rule-authoring tool and must always produce a
  report. A warning is logged so drift is visible.

See ``docs/solutions/best-practices/flag-gated-multi-port-rule-pattern-2026-04-23.md``
for the inference-path precedent this mirrors.

Plan: docs/plans/2026-04-23-002-feat-forge-second-oracle-plan.md Unit 5.
"""

from __future__ import annotations

import hashlib
import sqlite3
from typing import NamedTuple


class OracleConfigInputs(NamedTuple):
    """All inputs whose change must invalidate a prebuilt oracle sidecar.

    Adding a field requires a version bump in callers (they will see
    mismatches against previously-built sidecars until rebuild).
    """

    #: ``git -C data/forge rev-parse HEAD`` at build time. Changing the
    #: Forge SHA changes the precon corpus + the Java reference the
    #: port is derived against.
    forge_sha: str

    #: Laplace add-k smoothing constant used in ``ppmi.compute_ppmi_table``.
    ppmi_smoothing_k: float

    #: Minimum-evidence filter for PPMI rows (decks_count >= this).
    min_decks_count: int

    #: Version of the port-signature vocabulary (``vocabulary.VOCAB_VERSION``
    #: from ``port_graph``). Bumped when the canonical node_kind /
    #: subkind mapping changes — forces oracle rebuild so subkinds in
    #: the table match current projection.
    port_signature_version: str

    #: Identifier of the Forge Java method the pair scorer is ported
    #: from, with a Forge SHA suffix. Example:
    #: ``"CardRanker.getScoreForDeckHints@forge-ed97d9bb"``.
    java_method_id: str


class OracleConfigStaleError(RuntimeError):
    """Raised when the DB's stored hash does not match the current config."""


class OracleConfigMissingError(RuntimeError):
    """Raised when ``oracle_config`` has no ``config_hash`` row."""


def compute_oracle_hash(inputs: OracleConfigInputs) -> str:
    """SHA-256 over the sorted repr of ``inputs``. Hex digest (64 chars).

    Deterministic across Python releases: ``repr`` of a NamedTuple with
    primitive fields is stable, and we sort the asdict items so field
    order doesn't leak through ``NamedTuple._fields`` ordering changes
    (unlikely but cheap to guard against).
    """
    serialized = repr(sorted(inputs._asdict().items()))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def get_oracle_config_inputs(
    *,
    ppmi_smoothing_k: float,
    min_decks_count: int,
) -> OracleConfigInputs:
    """Build an ``OracleConfigInputs`` by reading the current ambient config.

    ``forge_sha`` comes from the live ``data/forge/`` HEAD (the build
    is refusing-to-run elsewhere if the pin has drifted).
    ``port_signature_version`` comes from ``port_graph.vocabulary``.
    ``java_method_id`` is fixed by the port's source (Unit 3 chose
    ``CardRanker.getScoreForDeckHints``).

    Callers that want to compute a hypothetical hash (for e.g. dry-run
    "would this require rebuild?" tests) can construct an
    ``OracleConfigInputs`` directly.
    """
    # Local imports to avoid circular-ish dependencies if version.py grows
    # consumers later.
    from mtg_synergy_graph.forge_oracle import version as fo_version
    from mtg_synergy_graph.port_graph import vocabulary as port_vocab

    return OracleConfigInputs(
        forge_sha=fo_version.read_current_forge_sha(),
        ppmi_smoothing_k=float(ppmi_smoothing_k),
        min_decks_count=int(min_decks_count),
        port_signature_version=port_vocab.VOCAB_VERSION,
        java_method_id=(
            # Keep the method id stable: CardRanker.getScoreForDeckHints is
            # the Unit 3 port target. Forge SHA suffix distinguishes method
            # body drift across Forge releases, even if the method name
            # stays the same.
            f"CardRanker.getScoreForDeckHints@forge-{fo_version.read_current_forge_sha()[:8]}"
        ),
    )


def write_oracle_config(
    conn: sqlite3.Connection,
    inputs: OracleConfigInputs,
) -> str:
    """Write ``inputs`` + computed hash into ``oracle_config``. Returns hash."""
    h = compute_oracle_hash(inputs)
    rows: list[tuple[str, str]] = [
        ("config_hash", h),
        ("forge_sha", inputs.forge_sha),
        ("ppmi_smoothing_k", str(inputs.ppmi_smoothing_k)),
        ("min_decks_count", str(inputs.min_decks_count)),
        ("port_signature_version", inputs.port_signature_version),
        ("java_method_id", inputs.java_method_id),
    ]
    conn.executemany(
        "INSERT OR REPLACE INTO oracle_config(key, value) VALUES (?, ?)",
        rows,
    )
    return h


def verify_current_or_raise(
    conn: sqlite3.Connection,
    inputs: OracleConfigInputs,
) -> None:
    """Assert the DB's stored hash matches the current config.

    Raises ``OracleConfigMissingError`` if no hash row is present (DB
    was built before Unit 5 landed, or the oracle_config table is
    corrupt). Raises ``OracleConfigStaleError`` if the hash differs.
    """
    row = conn.execute("SELECT value FROM oracle_config WHERE key = 'config_hash'").fetchone()
    if row is None:
        raise OracleConfigMissingError(
            "forge_oracle.db has no config_hash row. Rebuild with `scripts/forge_oracle.py build` to populate it."
        )
    stored = row[0]
    current = compute_oracle_hash(inputs)
    if stored != current:
        raise OracleConfigStaleError(
            f"forge_oracle.db was built under a different config "
            f"(stored hash {stored[:12]}..., current {current[:12]}...). "
            f"Rebuild with `scripts/forge_oracle.py build` to refresh."
        )
