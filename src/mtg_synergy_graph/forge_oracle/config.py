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

    #: Version of the port-graph vocabulary (``vocabulary.VOCAB_VERSION``
    #: from ``port_graph``). Bumped when the canonical node_kind /
    #: subkind mapping changes — forces oracle rebuild so subkinds in
    #: the table match current projection.
    vocab_version: str

    #: Identifier of the Forge Java method the pair scorer is ported
    #: from, with a Forge SHA suffix. Example:
    #: ``"CardRanker.getScoreForDeckHints@forge-ed97d9bb"``.
    java_method_id: str


class OracleConfigError(RuntimeError):
    """Base class for ``oracle_config`` contract failures.

    Catching this subsumes the missing-rows, corruption, and stale
    cases. Mirrors ``EmbeddingConfigError`` so callers that need a
    one-catch degrade path can do so.
    """


class OracleConfigMissingError(OracleConfigError):
    """Raised when required ``oracle_config`` KV rows are absent."""


class OracleConfigCorruptError(OracleConfigError):
    """Raised when stored KV rows do not hash back to the stored ``config_hash``.

    Indicates a partial write (rows updated without hash, or vice versa).
    Distinct from ``Stale`` — a corrupt artifact is internally
    inconsistent, while a stale artifact is internally consistent but
    built under a different version of the code than the reader.
    """


class OracleConfigStaleError(OracleConfigError):
    """Raised when stored config diverges from what current code expects.

    The stored artifact is internally consistent (corruption check
    passes), but was built under different ``OracleConfigInputs`` —
    rebuild with matching flags, or update the defaults to match the
    build.
    """


#: KV keys that must all be present for ``read_stored_oracle_config``
#: to reconstruct a complete ``OracleConfigInputs``. Includes
#: ``config_hash`` (the corruption check's expected value).
_REQUIRED_ORACLE_CONFIG_KEYS: tuple[str, ...] = (
    "config_hash",
    "forge_sha",
    "ppmi_smoothing_k",
    "min_decks_count",
    "vocab_version",
    "java_method_id",
)


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
    ``vocab_version`` comes from ``port_graph.vocabulary``.
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
        vocab_version=port_vocab.VOCAB_VERSION,
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
        ("vocab_version", inputs.vocab_version),
        ("java_method_id", inputs.java_method_id),
    ]
    conn.executemany(
        "INSERT OR REPLACE INTO oracle_config(key, value) VALUES (?, ?)",
        rows,
    )
    return h


def read_stored_oracle_config(
    conn: sqlite3.Connection,
) -> tuple[OracleConfigInputs, str]:
    """Reconstruct ``OracleConfigInputs`` + stored hash from KV rows.

    Returns ``(stored_inputs, stored_hash)`` where ``stored_inputs``
    reflects the parameters actually used at build time — not the
    current module-level defaults. This is the provenance-correct input
    to both the corruption check and the staleness check in
    :func:`verify_current_or_raise`.

    Raises ``OracleConfigMissingError`` if any required KV key is
    absent. Wraps ``sqlite3.OperationalError`` (missing table) and
    malformed numeric values in the same Missing/Corrupt classes so
    callers see one failure class per failure mode.

    See ``docs/solutions/best-practices/verify-from-stored-config-not-code-defaults-2026-04-23.md``
    for the generalized discipline this enforces.
    """
    try:
        rows = dict(conn.execute("SELECT key, value FROM oracle_config").fetchall())
    except sqlite3.OperationalError as exc:
        raise OracleConfigMissingError(
            "oracle_config table is missing. Rebuild with `scripts/forge_oracle.py build` to populate it."
        ) from exc

    missing = [k for k in _REQUIRED_ORACLE_CONFIG_KEYS if k not in rows]
    if missing:
        raise OracleConfigMissingError(
            f"oracle_config is missing required keys: {missing}. "
            f"Rebuild with `scripts/forge_oracle.py build` to populate."
        )

    try:
        stored_inputs = OracleConfigInputs(
            forge_sha=rows["forge_sha"],
            ppmi_smoothing_k=float(rows["ppmi_smoothing_k"]),
            min_decks_count=int(rows["min_decks_count"]),
            vocab_version=rows["vocab_version"],
            java_method_id=rows["java_method_id"],
        )
    except (TypeError, ValueError) as exc:
        raise OracleConfigCorruptError(
            f"oracle_config has malformed numeric value: {exc}. "
            f"Rebuild with `scripts/forge_oracle.py build` to refresh."
        ) from exc

    return stored_inputs, rows["config_hash"]


def _format_input_differences(
    stored: OracleConfigInputs,
    current: OracleConfigInputs,
) -> str:
    """Return a human-readable list of diverging fields between two inputs."""
    diffs = [
        f"{key}: stored={getattr(stored, key)!r} != current={getattr(current, key)!r}"
        for key in stored._asdict()
        if getattr(stored, key) != getattr(current, key)
    ]
    return "; ".join(diffs) if diffs else "(no field-level differences — unreachable)"


def verify_current_or_raise(
    conn: sqlite3.Connection,
    inputs: OracleConfigInputs,
) -> None:
    """Two-step verification against stored KV rows (not code defaults).

    Step 1 — **corruption check**: reconstruct ``OracleConfigInputs``
    from the stored KV rows and confirm its hash matches the stored
    ``config_hash``. A mismatch means the rows were partially written
    (hash out of sync with values) — raises
    ``OracleConfigCorruptError``.

    Step 2 — **staleness check**: the reconstructed stored inputs must
    equal ``inputs`` (what the consuming code expects). A divergence
    means the artifact was built with a different set of parameters
    (e.g. different ``forge_sha`` or ``ppmi_smoothing_k``) — raises
    ``OracleConfigStaleError`` naming the diverging fields.

    See ``docs/solutions/best-practices/verify-from-stored-config-not-code-defaults-2026-04-23.md``
    for why this must read from stored KV rather than recomputing from
    current-code defaults.
    """
    stored_inputs, stored_hash = read_stored_oracle_config(conn)

    recomputed = compute_oracle_hash(stored_inputs)
    if recomputed != stored_hash:
        raise OracleConfigCorruptError(
            f"forge_oracle.db stored hash {stored_hash[:12]}... does not "
            f"match hash recomputed from stored KV rows ({recomputed[:12]}...). "
            f"DB is partially written. "
            f"Rebuild with `scripts/forge_oracle.py build` to refresh."
        )

    if stored_inputs != inputs:
        diffs = _format_input_differences(stored_inputs, inputs)
        raise OracleConfigStaleError(
            f"forge_oracle.db was built with different parameters than the "
            f"current code expects. Diverging fields: {diffs}. "
            f"Rebuild with `scripts/forge_oracle.py build` to refresh."
        )
