"""Hand-rolled TF-IDF feature-bag builder for content embeddings (Unit 1).

Pure-function pipeline from a SQLite connection to a ``{card_name:
feature_tokens}`` corpus, and from that corpus to a TF-IDF matrix.
No sklearn, no scipy — numpy + stdlib only.

Feature-token grammar (version ``TOKEN_FORMAT_VERSION``):

* ``port_type:<v>`` — every row in ``card_ports`` contributes one.
* ``event_class:<v>`` — every row (column is ``NOT NULL``).
* ``zone_origin:<v>``, ``zone_destination:<v>``, ``counter_type:<v>``,
  ``branch_kind:<v>`` — emitted only when the column is non-null.
* ``attr:<attr_kind>:<attr_value>`` — one per ``port_attributes`` row
  (pre-exploded by ``attributes.explode_filter`` at importer time, so
  the vectorizer does NOT re-parse ``valid_filter``).
* ``keyword:<kw>`` — one per element of the JSON-decoded
  ``cards.keywords`` array.

Duplicate tokens on the same card are retained (raw term counts feed
TF-IDF). The corpus returns a ``tuple`` for each card because the
downstream caller may iterate multiple times; tuples hash quickly if
callers memoize.

Plan: ``docs/plans/2026-04-23-003-feat-content-embeddings-fallback-plan.md``
Unit 1.
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
from typing import NamedTuple

import numpy as np

logger = logging.getLogger(__name__)

#: Token-format version. Bump this when the emitted token grammar
#: changes (new field added, old field renamed, attr-kind vocabulary
#: restructured). Flows into ``EmbeddingConfigInputs`` so a change
#: invalidates both the pinned audit tensor and the on-disk
#: ``card_embeddings`` table.
TOKEN_FORMAT_VERSION: str = "v1"  # noqa: S105 — not a secret; version tag


#: Columns from ``card_ports`` that are emitted as ``<column>:<value>``
#: tokens when non-null. ``port_type`` is always non-null (``NOT NULL``)
#: so is handled separately; the rest are optional.
_OPTIONAL_PORT_TOKEN_COLUMNS: tuple[str, ...] = (
    "event_class",
    "zone_origin",
    "zone_destination",
    "counter_type",
    "branch_kind",
)


class TfidfResult(NamedTuple):
    """TF-IDF output — matrix, ordered card names, and vocabulary.

    Fields:

    * ``tfidf_matrix`` — ``(n_cards, n_tokens)`` ``float64``. Row ``i``
      is the TF-IDF vector for ``card_names[i]``; column ``j`` is the
      term ``vocabulary[j]``.
    * ``card_names`` — cards sorted by name (deterministic).
    * ``vocabulary`` — surviving tokens (after ``min_df`` prune)
      sorted ascending (deterministic).
    """

    tfidf_matrix: np.ndarray
    card_names: list[str]
    vocabulary: list[str]


def extract_card_tokens(conn: sqlite3.Connection) -> dict[str, tuple[str, ...]]:
    """Build the ``{card_name: feature_tokens}`` corpus from SQLite.

    Reads:

    * ``card_ports`` — port-row categorical fields (see module docstring
      for the grammar).
    * ``port_attributes`` — pre-exploded attribute rows keyed by port_id.
    * ``cards.keywords`` — JSON array; each keyword becomes one token.

    Cards with no ports and no keywords still appear in the output with
    an empty tuple, so every row in ``cards`` gets a feature vector
    (possibly zero) downstream.
    """
    tokens: dict[str, list[str]] = {}

    # Seed every card with an empty bag so cards with zero ports/keywords
    # still show up (zero-token cards are an explicit edge case the
    # downstream SVD / L2-normalization handles).
    for row in conn.execute("SELECT name FROM cards"):
        tokens[row[0]] = []

    # Port-row tokens. We read id + card_name so port_attributes can be
    # joined downstream without another N+1.
    port_id_to_card: dict[int, str] = {}
    for row in conn.execute(
        "SELECT id, card_name, port_type, event_class, "
        "zone_origin, zone_destination, counter_type, branch_kind "
        "FROM card_ports"
    ):
        port_id = int(row[0])
        card_name = row[1]
        port_id_to_card[port_id] = card_name
        if card_name not in tokens:
            # Orphan port (card_name not in cards) — skip silently; the
            # importer guarantees a FK, but defensive handling keeps the
            # vectorizer robust against partial test fixtures.
            continue
        bag = tokens[card_name]
        port_type = row[2]
        bag.append(f"port_type:{port_type}")
        # ``event_class`` is NOT NULL per schema, but guard anyway.
        values = (row[3], row[4], row[5], row[6], row[7])
        for col, val in zip(_OPTIONAL_PORT_TOKEN_COLUMNS, values, strict=True):
            if val is not None and val != "":
                bag.append(f"{col}:{val}")

    # port_attributes rows (pre-exploded). Empty fixtures may omit the
    # table; be defensive.
    try:
        for row in conn.execute("SELECT port_id, attr_kind, attr_value FROM port_attributes"):
            port_id = int(row[0])
            card_name = port_id_to_card.get(port_id)
            if card_name is None or card_name not in tokens:
                continue
            tokens[card_name].append(f"attr:{row[1]}:{row[2]}")
    except sqlite3.OperationalError:
        # No port_attributes table in this DB (e.g., minimal test
        # fixture). Skip silently — the corpus is simply sparser.
        logger.debug("port_attributes table missing; skipping attr tokens")

    # Keyword tokens from cards.keywords JSON array.
    for row in conn.execute("SELECT name, keywords FROM cards"):
        card_name = row[0]
        raw = row[1]
        if raw is None or raw == "":
            continue
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            logger.debug("cards.keywords not JSON for %s; skipping", card_name)
            continue
        if not isinstance(parsed, list):
            continue
        bag = tokens[card_name]
        for kw in parsed:
            if isinstance(kw, str) and kw:
                bag.append(f"keyword:{kw}")

    return {name: tuple(bag) for name, bag in tokens.items()}


def compute_tfidf(
    corpus: dict[str, tuple[str, ...]],
    min_df: int = 2,
) -> TfidfResult:
    """TF-IDF over the corpus with smoothed IDF and min-df pruning.

    Term-frequency is raw count (no sublinear scaling); IDF is
    ``ln((1 + N) / (1 + df)) + 1`` — the smoothed form commonly used by
    IR libraries to avoid division-by-zero when ``df == N``. Tokens
    present on fewer than ``min_df`` cards are pruned from the
    vocabulary entirely. Rows are **not** L2-normalized here; SVD +
    normalization happens in ``embeddings.svd.truncated_svd``.

    Determinism:

    * ``card_names`` are sorted ascending, so row ``i`` is reproducible.
    * ``vocabulary`` is sorted ascending.
    * No random sampling; no seed needed.
    """
    if min_df < 1:
        raise ValueError(f"min_df must be >= 1; got {min_df}")

    card_names = sorted(corpus.keys())
    n_cards = len(card_names)

    # Document frequency (how many cards contain each token at least once).
    document_frequency: dict[str, int] = {}
    for name in card_names:
        for token in set(corpus[name]):
            document_frequency[token] = document_frequency.get(token, 0) + 1

    # Prune tokens below min_df, then sort deterministically.
    vocabulary = sorted(token for token, df in document_frequency.items() if df >= min_df)
    vocab_index: dict[str, int] = {token: i for i, token in enumerate(vocabulary)}

    if not vocabulary:
        # All tokens pruned (or corpus empty). Return an empty matrix
        # with the correct row count so downstream SVD still sees a
        # 2-D array.
        return TfidfResult(
            tfidf_matrix=np.zeros((n_cards, 0), dtype=np.float64),
            card_names=card_names,
            vocabulary=vocabulary,
        )

    # IDF vector (smoothed) — computed once per run.
    idf = np.zeros(len(vocabulary), dtype=np.float64)
    for token, df in document_frequency.items():
        idx = vocab_index.get(token)
        if idx is None:
            continue
        idf[idx] = math.log((1.0 + n_cards) / (1.0 + df)) + 1.0

    # TF matrix — raw counts per (card, token).
    tf = np.zeros((n_cards, len(vocabulary)), dtype=np.float64)
    for i, name in enumerate(card_names):
        for token in corpus[name]:
            idx = vocab_index.get(token)
            if idx is None:
                continue
            tf[i, idx] += 1.0

    tfidf = tf * idf  # row-wise broadcast: each column scaled by its IDF

    return TfidfResult(
        tfidf_matrix=tfidf,
        card_names=card_names,
        vocabulary=vocabulary,
    )
