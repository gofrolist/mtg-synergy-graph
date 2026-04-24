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

    Catching this subsumes both the "missing hash row" and "stale hash"
    cases. Inference-path callers (per plan D6) catch this and degrade
    to ``{}``; strict offline consumers re-raise.
    """


class EmbeddingConfigMissingError(EmbeddingConfigError):
    """Raised when ``card_embeddings_config`` has no ``config_hash`` row."""


class EmbeddingConfigStaleError(EmbeddingConfigError):
    """Raised when the DB's stored hash does not match the current config."""


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
    truth for the emitted grammar). ``svd_dims`` / ``min_df`` /
    ``vectorizer_version`` / ``port_signature_version`` are fixed at
    the current defaults chosen in plan D1 and Unit 1.

    Callers that want to compute a hypothetical hash (for dry-run
    "would this require rebuild?" tests) can construct an
    ``EmbeddingConfigInputs`` directly.
    """
    # Local import to avoid circular-import risk: vectorizer imports
    # numpy + sqlite3 and may grow more consumers over time.
    from mtg_synergy_graph.embeddings import vectorizer as emb_vectorizer

    return EmbeddingConfigInputs(
        token_format_version=emb_vectorizer.TOKEN_FORMAT_VERSION,
        svd_dims=_DEFAULT_SVD_DIMS,
        min_df=_DEFAULT_MIN_DF,
        vectorizer_version=1,
        port_signature_version="v1",
    )


def verify_current_or_raise(
    conn: sqlite3.Connection,
    inputs: EmbeddingConfigInputs,
) -> None:
    """Assert the DB's stored hash matches the current config.

    Raises ``EmbeddingConfigMissingError`` if no ``config_hash`` row is
    present (DB was built before Unit 2 landed, or the
    ``card_embeddings_config`` table is empty). Raises
    ``EmbeddingConfigStaleError`` if the hash differs — message
    includes both hashes (truncated) plus the rebuild command.
    """
    row = conn.execute("SELECT value FROM card_embeddings_config WHERE key = 'config_hash'").fetchone()
    if row is None:
        raise EmbeddingConfigMissingError(
            "card_embeddings_config has no config_hash row. "
            "Rebuild with `uv run scripts/build_embeddings.py` to populate it."
        )
    stored = row[0]
    current = compute_embedding_hash(inputs)
    if stored != current:
        raise EmbeddingConfigStaleError(
            f"card_embeddings was built under a different config "
            f"(stored hash {stored[:12]}..., current {current[:12]}...). "
            f"Rebuild with `uv run scripts/build_embeddings.py` to refresh."
        )
