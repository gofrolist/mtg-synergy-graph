"""EmbeddingConfigInputs + compute_embedding_hash + refuse-to-run contract.

Mirrors ``src/mtg_synergy_graph/forge_oracle/config.py`` for the
embedding pipeline. The meta-principle is the same — mechanically
enforce that consumers never compare a rebuilt ``card_embeddings``
table against inputs computed under a different vectorizer/config.

Consumers:

- ``scripts/build_embeddings.py`` (Unit 3) — writes the hash into
  ``card_embeddings_config`` at the end of every build.
- ``embeddings.store.load_card_embeddings`` (Unit 5) — soft check;
  stale hash degrades to ``{}`` + ``logging.warning`` (inference path
  never raises per D6).
- ``bench.py audit --embedding-dedup`` (Unit 6) — strict consumer;
  exits 2 on stale hash per D7.

See ``docs/solutions/best-practices/offline-oracle-hash-pattern-2026-04-23.md``
for the hybrid-hash discipline: this KV table detects on-disk drift;
``ScoringConfigInputs`` (Unit 7) detects scorer-side drift. Both fire
in different failure modes — keep both.

Plan: docs/plans/2026-04-23-003-feat-content-embeddings-fallback-plan.md Unit 2.
"""

from __future__ import annotations

import hashlib
import sqlite3
from typing import NamedTuple

#: Default target dimensionality after truncated SVD. Single source of
#: truth for both ``get_embedding_config_inputs()`` and the
#: ``scripts/build_embeddings.py`` argparse ``default=``. Leading
#: underscore indicates module-internal, but the build script imports
#: it directly so the two defaults cannot drift.
_DEFAULT_SVD_DIMS: int = 128

#: Default minimum document frequency for TF-IDF pruning. Same
#: single-source-of-truth rationale as ``_DEFAULT_SVD_DIMS``.
_DEFAULT_MIN_DF: int = 2


class EmbeddingConfigInputs(NamedTuple):
    """All inputs whose change must invalidate a rebuilt ``card_embeddings``.

    Adding a field requires a ``vectorizer_version`` bump in callers
    (they will see mismatches against previously-built tables until
    rebuild). Order of fields is irrelevant — ``compute_embedding_hash``
    sorts the ``_asdict()`` items before hashing.
    """

    #: Token grammar version (``embeddings.vectorizer.TOKEN_FORMAT_VERSION``).
    #: Bumped when emitted token shapes change (new column, renamed
    #: column, attr-kind vocabulary restructured).
    token_format_version: str

    #: Target dimensionality of the per-card vector after truncated SVD.
    #: Fixed at 128 by plan D1.
    svd_dims: int

    #: Minimum document frequency for TF-IDF pruning. Tokens on fewer
    #: than ``min_df`` cards are dropped from the vocabulary entirely.
    min_df: int

    #: Integer version stamped into ``card_embeddings.vectorizer_version``
    #: for each row. Bumped alongside ``token_format_version`` or any
    #: algorithmic change to the SVD / normalization pipeline.
    vectorizer_version: int

    #: Version of the port-signature vocabulary
    #: (``port_graph.vocabulary.VOCAB_VERSION``). Bumped when the
    #: canonical ``node_kind`` / ``subkind`` mapping changes — since
    #: downstream rules read ``port_nodes``, a drift there can alter
    #: the structural features the vectorizer observes.
    port_signature_version: str


class EmbeddingConfigError(RuntimeError):
    """Base class for ``card_embeddings_config`` contract failures.

    Catching this subsumes the missing-rows, corruption, and stale
    cases. Inference-path callers (per plan D6) catch this and degrade
    to ``{}``; strict offline consumers re-raise.
    """


class EmbeddingConfigMissingError(EmbeddingConfigError):
    """Raised when required ``card_embeddings_config`` KV rows are absent."""


class EmbeddingConfigCorruptError(EmbeddingConfigError):
    """Raised when stored KV rows do not hash back to the stored ``config_hash``.

    Indicates a partial write (rows updated without hash, or vice versa).
    Distinct from ``Stale`` — a corrupt artifact is internally
    inconsistent, while a stale artifact is internally consistent but
    built under a different version of the code than the reader.
    """


class EmbeddingConfigStaleError(EmbeddingConfigError):
    """Raised when the stored config diverges from what the current code expects.

    The stored artifact is internally consistent (corruption check
    passes), but was built under different ``EmbeddingConfigInputs`` —
    e.g., the builder used ``--min-df 3`` while the current code
    defaults to ``min_df=2``. Rebuild with matching flags, or update
    the defaults to match the build.
    """


#: KV keys that must all be present for ``read_stored_config`` to
#: reconstruct a complete ``EmbeddingConfigInputs``. Excludes
#: ``built_at`` (diagnostic only) but includes ``config_hash`` (the
#: corruption check's expected value).
_REQUIRED_CONFIG_KEYS: tuple[str, ...] = (
    "config_hash",
    "token_format_version",
    "svd_dims",
    "min_df",
    "vectorizer_version",
    "port_signature_version",
)


def compute_embedding_hash(inputs: EmbeddingConfigInputs) -> str:
    """SHA-256 over the sorted repr of ``inputs``. Hex digest (64 chars).

    Deterministic across Python releases: ``repr`` of a NamedTuple with
    primitive fields is stable, and we sort the asdict items so field
    order in the class definition cannot leak into the hash.
    """
    serialized = repr(sorted(inputs._asdict().items()))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def get_embedding_config_inputs() -> EmbeddingConfigInputs:
    """Build an ``EmbeddingConfigInputs`` from the current ambient config.

    ``token_format_version`` comes from
    ``embeddings.vectorizer.TOKEN_FORMAT_VERSION`` (single source of
    truth for the emitted grammar). ``port_signature_version`` comes
    from ``port_graph.vocabulary.VOCAB_VERSION`` (so a vocabulary bump
    invalidates stored embeddings). ``svd_dims`` / ``min_df`` /
    ``vectorizer_version`` are fixed at the current defaults chosen in
    plan D1 and Unit 1.

    Callers that want to compute a hypothetical hash (for dry-run
    "would this require rebuild?" tests) can construct an
    ``EmbeddingConfigInputs`` directly.
    """
    # Local imports to avoid circular-import risk: vectorizer imports
    # numpy + sqlite3 and may grow more consumers over time.
    from mtg_synergy_graph.embeddings import vectorizer as emb_vectorizer
    from mtg_synergy_graph.port_graph import vocabulary as port_vocab

    return EmbeddingConfigInputs(
        token_format_version=emb_vectorizer.TOKEN_FORMAT_VERSION,
        svd_dims=_DEFAULT_SVD_DIMS,
        min_df=_DEFAULT_MIN_DF,
        vectorizer_version=1,
        port_signature_version=port_vocab.VOCAB_VERSION,
    )


def read_stored_config(
    conn: sqlite3.Connection,
) -> tuple[EmbeddingConfigInputs, str]:
    """Reconstruct ``EmbeddingConfigInputs`` + stored hash from KV rows.

    Returns ``(stored_inputs, stored_hash)`` where ``stored_inputs``
    reflects the parameters actually used at build time — not the
    current module-level defaults. This is the provenance-correct input
    to both the corruption check and the staleness check in
    :func:`verify_current_or_raise`.

    Raises ``EmbeddingConfigMissingError`` if any required KV key is
    absent (pre-Unit-2 DB, failed build, or foreign write path). Wraps
    ``sqlite3.OperationalError`` (missing table) and malformed int
    values in the same exception so callers see one failure class.

    See ``docs/solutions/best-practices/verify-from-stored-config-not-code-defaults-2026-04-23.md``
    for the generalized discipline this enforces.
    """
    try:
        rows = dict(conn.execute("SELECT key, value FROM card_embeddings_config").fetchall())
    except sqlite3.OperationalError as exc:
        raise EmbeddingConfigMissingError(
            "card_embeddings_config table is missing. Rebuild with `uv run scripts/build_embeddings.py` to populate it."
        ) from exc

    missing = [k for k in _REQUIRED_CONFIG_KEYS if k not in rows]
    if missing:
        raise EmbeddingConfigMissingError(
            f"card_embeddings_config is missing required keys: {missing}. "
            f"Rebuild with `uv run scripts/build_embeddings.py` to populate."
        )

    try:
        stored_inputs = EmbeddingConfigInputs(
            token_format_version=rows["token_format_version"],
            svd_dims=int(rows["svd_dims"]),
            min_df=int(rows["min_df"]),
            vectorizer_version=int(rows["vectorizer_version"]),
            port_signature_version=rows["port_signature_version"],
        )
    except (TypeError, ValueError) as exc:
        raise EmbeddingConfigCorruptError(
            f"card_embeddings_config has malformed integer value: {exc}. "
            f"Rebuild with `uv run scripts/build_embeddings.py` to refresh."
        ) from exc

    return stored_inputs, rows["config_hash"]


def _format_input_differences(
    stored: EmbeddingConfigInputs,
    current: EmbeddingConfigInputs,
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
    current_code_inputs: EmbeddingConfigInputs,
) -> None:
    """Two-step verification against stored KV rows (not code defaults).

    Step 1 — **corruption check**: reconstruct ``EmbeddingConfigInputs``
    from the stored KV rows and confirm its hash matches the stored
    ``config_hash``. A mismatch means the rows were partially written
    (hash out of sync with values) — raises
    ``EmbeddingConfigCorruptError``.

    Step 2 — **staleness check**: the reconstructed stored inputs must
    equal ``current_code_inputs`` (what the consuming code expects). A
    divergence means the artifact was built with a different set of
    parameters (e.g., ``--min-df 3`` vs current default ``min_df=2``)
    — raises ``EmbeddingConfigStaleError`` naming the diverging fields.

    The caller supplies ``current_code_inputs`` so tests can compare
    against synthetic expectations. Production callers pass
    :func:`get_embedding_config_inputs`.

    See ``docs/solutions/best-practices/verify-from-stored-config-not-code-defaults-2026-04-23.md``
    for why this must read from stored KV rather than recomputing from
    current-code defaults.
    """
    stored_inputs, stored_hash = read_stored_config(conn)

    recomputed = compute_embedding_hash(stored_inputs)
    if recomputed != stored_hash:
        raise EmbeddingConfigCorruptError(
            f"card_embeddings_config stored hash {stored_hash[:12]}... does not "
            f"match hash recomputed from stored KV rows ({recomputed[:12]}...). "
            f"DB is partially written. "
            f"Rebuild with `uv run scripts/build_embeddings.py` to refresh."
        )

    if stored_inputs != current_code_inputs:
        diffs = _format_input_differences(stored_inputs, current_code_inputs)
        raise EmbeddingConfigStaleError(
            f"card_embeddings was built with different parameters than the "
            f"current code expects. Diverging fields: {diffs}. "
            f"Either rebuild with `uv run scripts/build_embeddings.py` using "
            f"matching flags, or update the defaults in "
            f"mtg_synergy_graph.embeddings.config to match the build."
        )
