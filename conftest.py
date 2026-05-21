"""Repo-root conftest — only purpose is to make mutmut work with the src-layout.

Mutmut copies the repo into ``mutants/`` and runs pytest from there. With a
src-layout + non-editable uv install, pytest finds ``mtg_synergy_graph`` in the
``.venv`` site-packages (unmutated) instead of ``mutants/src/`` (mutated). This
defeats the entire point of mutation testing.

When this file is loaded from a path whose parent name is ``mutants``, it
prepends that directory's ``src/`` to ``sys.path`` so the mutated package
takes precedence over the installed copy. In every other context (normal
``pytest tests/`` runs) the check is False and this file is a no-op.
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if _HERE.name == "mutants":
    _MUTANT_SRC = _HERE / "src"
    if _MUTANT_SRC.is_dir() and str(_MUTANT_SRC) not in sys.path:
        sys.path.insert(0, str(_MUTANT_SRC))
