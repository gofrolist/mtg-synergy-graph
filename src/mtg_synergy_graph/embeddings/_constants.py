"""numpy-free constants shared across the embeddings subpackage.

Lives in its own module so ``config.get_embedding_config_inputs()``
(called by the scoring config-hash accessor) can read the token
grammar version without importing ``vectorizer`` — which imports
numpy at module scope, and numpy is only present with the optional
``[graph]`` extra.
"""

from __future__ import annotations

#: Token-format version. Bump this when the emitted token grammar
#: changes (new field added, old field renamed, attr-kind vocabulary
#: restructured). Flows into ``EmbeddingConfigInputs`` so a change
#: invalidates both the pinned audit tensor and the on-disk
#: ``card_embeddings`` table.
TOKEN_FORMAT_VERSION: str = "v1"  # noqa: S105 — not a secret; version tag
