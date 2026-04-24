"""Inference-path reader + ``embedding_contribution`` term (plan 003 Unit 5).

This module is the "reader contract" half of plan 003. It is *not*
yet called by the scorer — Unit 7 wires
:func:`embedding_contribution` into ``score_all_universal`` and
:func:`load_card_embeddings_verified` into the scoring init path.
Until then, the module is shippable in isolation and every scoring
invariant holds because of the flag-off default.

Flipping procedure
------------------

The three module-level constants below are intentionally edit-in-place
Python (no env var / config file). Mirrors
``src/mtg_synergy_graph/complement_rules/pathway.py:_ENABLE_PATHWAY_RULES``:

1. Edit ``_ENABLE_EMBEDDING_CONTRIBUTION`` to ``True`` (and pick a
   non-zero ``_EMBEDDING_W``) in the working tree.
2. Run ``uv run scripts/bench.py audit --repin --yes`` to pin a new
   fixture under the new config hash.
3. Run ``uv run scripts/bench.py audit`` and record the aggregate NDCG
   delta + ``hidden_gem_hit_rate`` delta (see
   ``memory/feedback_hidden_gem_metric.md`` for the non-negotiable gate).
4. Commit flip + pinned fixture together (mirrors the ``pathway``
   plan 001 Unit 6 flip commit pattern).

Test-time override
------------------

Tests toggle via ``monkeypatch.setattr(contribution,
"_ENABLE_EMBEDDING_CONTRIBUTION", True)`` or
``unittest.mock.patch.object(contribution, "_EMBEDDING_W", 0.5)``.
The module default stays safe; tests never mutate the global process
state.

Graceful-fallback contract
--------------------------

:func:`embedding_contribution` returns ``0.0`` on every missing-input
short-circuit (flag off, ``v_cmdr`` None, candidate absent from
``vectors``). :func:`load_card_embeddings_verified` returns ``{}`` on
every config-hash failure mode. Neither function raises from the
inference path — see plan D6 and
``src/mtg_synergy_graph/forge_oracle/gap_weight.py:load_forge_signals``
for the shared taxonomy.

Plan: docs/plans/2026-04-23-003-feat-content-embeddings-fallback-plan.md Unit 5.
"""

from __future__ import annotations

import logging
import math
import sqlite3
from collections.abc import Mapping

import numpy as np

from mtg_synergy_graph.embeddings import config as emb_config
from mtg_synergy_graph.embeddings import store as emb_store

logger = logging.getLogger(__name__)

#: Master flag for the embedding contribution term. Default ``False``
#: — the reader is shippable in isolation; the scorer wire-up in
#: Unit 7 and the audit-gated flip to ``True`` are separate commits.
#: Tests toggle via ``monkeypatch.setattr``; production edit-in-place.
_ENABLE_EMBEDDING_CONTRIBUTION: bool = False

#: Scaling constant ``w_emb`` for the ``w * decay * cosine`` term.
#: Initial ``0.0`` means even a ``True`` flag produces bitwise-identity
#: output — the flip and the weight tune are independently audited.
_EMBEDDING_W: float = 0.0

#: Exponential decay rate ``k`` applied to ``N_rules`` (the count of
#: rules already firing on the ``(commander, candidate)`` pair).
#: Larger ``k`` = embeddings fade faster as rule coverage grows.
#: ``0.8`` is the plan-prescribed starting point.
_EMBEDDING_K: float = 0.8


def embedding_contribution(
    *,
    candidate_name: str,
    n_rules: int,
    v_cmdr: np.ndarray | None,
    vectors: Mapping[str, np.ndarray],
) -> float:
    """Return the additive embedding term for one ``(commander, candidate)`` pair.

    Formula:
        ``_EMBEDDING_W * exp(-_EMBEDDING_K * n_rules) * cosine(v_cand, v_cmdr)``

    Cosine is computed as a raw dot product — both inputs are expected
    to be L2-normalized by construction (the vectorizer in Unit 1 and
    the commander-target builder in Unit 4 both emit unit vectors).

    Short-circuits (all return ``0.0``):

    * ``not _ENABLE_EMBEDDING_CONTRIBUTION`` — default gate.
    * ``v_cmdr is None`` — commander has no port vector and no
      golden-set hi-syn matches.
    * ``candidate_name`` not in ``vectors`` — card was pruned by
      min_df, or its DB row was absent at build time.

    Keyword-only args prevent positional misuse when Unit 7 wires this
    into ``score_all_universal``'s results loop. No side effects; no
    mutation of ``v_cmdr`` or ``vectors``. No logging — this is the
    hot-path function called once per candidate; Unit 7 logs once per
    scoring run.
    """
    if not _ENABLE_EMBEDDING_CONTRIBUTION:
        return 0.0

    if v_cmdr is None:
        return 0.0

    v_cand = vectors.get(candidate_name)
    if v_cand is None:
        return 0.0

    # Both inputs are L2-normalized; dot product == cosine. Cast to
    # Python float at the boundary — the scorer sums the result with
    # other floats, so keeping numpy scalars would leak dtype
    # sensitivity into downstream arithmetic.
    cosine = float(v_cand @ v_cmdr)
    decay = math.exp(-_EMBEDDING_K * n_rules)
    return _EMBEDDING_W * decay * cosine


def load_card_embeddings_verified(conn: sqlite3.Connection) -> dict[str, np.ndarray]:
    """Load ``card_embeddings`` only when the config hash matches.

    This is the inference-path-specific consumer described in plan D6.
    The offline handlers
    (``bench.py audit --embedding-dedup`` in Unit 6 and
    ``scripts/build_embeddings.py`` in Unit 3) perform their own
    stricter checks that re-raise on stale hash; this wrapper degrades
    to an empty dict so the scorer never crashes on a pre-rebuild
    cardsfolder refresh.

    Flow:

    1. Delegate to ``store.load_card_embeddings`` — returns ``{}``
       (with its own warning) on missing table, DB error, or empty
       table.
    2. If empty, pass it through — no point verifying a hash against
       no vectors, and the store has already warned.
    3. Call ``verify_current_or_raise``: on
       ``EmbeddingConfigMissingError`` or
       ``EmbeddingConfigStaleError``, log a warning with the
       exception's message (which includes the rebuild hint) and
       return ``{}``. Any other unexpected exception is caught and
       logged at warning level — the scorer's graceful fallback is
       the whole contract.
    4. On success, return the loaded vectors dict.
    """
    # ``store.load_card_embeddings`` owns the "missing table / DB
    # error / empty table / malformed row" taxonomy and returns ``{}``
    # on every failure, never raises. We trust that contract here and
    # wrap only the config-hash verify, which *does* raise by design.
    vectors = emb_store.load_card_embeddings(conn)
    if not vectors:
        # Store has already logged about missing/empty/corrupt table;
        # skip the hash check to avoid a misleading "stale" message
        # on top of the real "table missing" one.
        return {}

    try:
        emb_config.verify_current_or_raise(conn, emb_config.get_embedding_config_inputs())
    except emb_config.EmbeddingConfigError as exc:
        logger.warning("card_embeddings config verification failed: %s", exc)
        return {}
    except sqlite3.DatabaseError as exc:
        logger.warning("card_embeddings config read failed: %s", exc)
        return {}
    except Exception as exc:
        # Catch-all safety net: ``get_embedding_config_inputs`` has
        # transitive imports that could raise ImportError/AttributeError
        # on partial environments. The module docstring guarantees
        # "never raises from the inference path" — honor that.
        logger.warning("unexpected error verifying embedding config: %s", exc)
        return {}

    return vectors


__all__ = [
    "embedding_contribution",
    "load_card_embeddings_verified",
]
