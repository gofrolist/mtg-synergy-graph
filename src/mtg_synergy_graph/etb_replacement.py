"""K:ETBReplacement keyword directive parsing and SVar-chain port extraction.

400 cards in the Forge corpus encode an ETB replacement effect via a
single ``K:ETBReplacement:Scope:SVarRef[:Mandatory|Optional[:Zone[:ValidFilter]]]``
keyword line. The standard `extract_keyword_ports` emits one thin
keyword port per K: line with the SVar payload invisible. This module
adds the second-pass resolution that walks the referenced SVar chain
and emits standard effect ports for every node, mirroring the
trigger-chain pattern in :func:`extract_trigger_ports`.

Design and audit notes (POSITIVE Δ=+6.5477, 0 hi_syn_loss,
hidden_gem_hit_rate +0.01):
``docs/brainstorms/2026-05-20-etb-replacement-svar-walking-requirements.md``.
"""

from __future__ import annotations

from typing import Any

from .parser import walk_svar_chain

PortRow = dict[str, Any]


def _parse_etb_replacement_keyword(line: str) -> tuple[str, str, bool, str, str] | None:
    """Parse one ``K:ETBReplacement:Scope:SVarRef[:Mandatory|Optional[:Zone[:ValidFilter]]]``
    keyword line (the K: prefix already stripped by the parser).

    Returns ``(scope, svar_ref, optional, zone, valid_filter)`` or ``None``
    if the line isn't an ETBReplacement directive or is malformed (fewer
    than 3 colon-separated parts).

    The Forge ValidFilter syntax may contain ``+``, ``,``, ``.``, ``!`` but
    never ``:``, so a 6-way split with ``maxsplit=5`` cleanly separates
    every segment.
    """
    line = line.strip()
    if not line.startswith("ETBReplacement:"):
        return None
    parts = line.split(":", 5)
    if len(parts) < 3:
        return None
    _, scope, svar_ref = parts[0], parts[1], parts[2]
    if not svar_ref:
        return None
    optional_token = parts[3] if len(parts) > 3 else "Mandatory"
    zone = parts[4] if len(parts) > 4 else ""
    valid_filter = parts[5] if len(parts) > 5 else ""
    # ruff S105 false positive: "Optional" is a Forge keyword flag, not a credential.
    optional = optional_token == "Optional"  # noqa: S105
    return (scope, svar_ref, optional, zone, valid_filter)


def extract_etb_replacement_ports(
    card_name: str,
    keyword_lines: list[str],
    svars: dict[str, str],
) -> list[PortRow]:
    """Resolve `K:ETBReplacement` keyword directives by walking the
    referenced SVar chain and emitting standard effect ports.

    Mirrors the existing trigger-chain pattern (``extract_trigger_ports``)
    but rooted on a keyword line instead of a ``T:`` line. Each resolved
    port carries ``branch_kind='etb_replacement'`` on the chain root
    (sub-abilities use the existing ``CHAIN_KEYS`` mapping for their
    own branch kinds), ``is_optional=True`` when the keyword carried
    ``:Optional``, and a transient ``_etb_scope='other'|'copy'`` key that
    the importer projects into ``port_attributes`` under
    ``attr_kind='etb_scope'``.

    Today (pre-this-change) the surface-level keyword port for these
    lines has a per-card-unique ``event_class`` like
    ``ETBReplacement:Other:DBPrepare`` (the whole colon-separated
    string), which no complement rule matches on. This extractor adds
    the resolved-effect ports without altering the keyword port — that
    stays for back-compat.
    """
    # Local import: avoids a top-level cycle with ``ports`` (which
    # re-exports this module's public symbols and consumes them in
    # ``extract_all_ports``). Same pattern as ``copy_face_from.py``.
    from .ports import extract_effect_ports

    ports: list[PortRow] = []
    for line in keyword_lines:
        directive = _parse_etb_replacement_keyword(line)
        if directive is None:
            continue
        scope, svar_ref, optional, _zone, _valid_filter = directive
        chain = walk_svar_chain(
            svar_ref,
            svars,
            branch_kind="etb_replacement",
            branch_parent=None,
            chain_depth=1,
        )
        scope_label = scope.lower()
        for node in chain:
            for child_port in extract_effect_ports(card_name, node, svars):
                if optional:
                    child_port["is_optional"] = True
                # Transient — importer pops + projects into port_attributes.
                child_port["_etb_scope"] = scope_label
                ports.append(child_port)
    return ports
