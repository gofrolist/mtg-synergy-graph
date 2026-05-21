"""TokenScript parsing — Forge's compact token-spec format.

Tokens produced by ``DB$ Token | TokenScript$ <spec>`` lines are encoded
as compact underscore-separated strings whose shape varies by token kind
(creature, artifact, artifact-creature, X/X creature). This module
parses one or more comma-separated specs into ``(attr_kind, attr_value)``
pairs that the importer projects into ``port_attributes`` under
``token_color`` / ``token_subtype`` keys.

Extracted from ``ports.py`` (2026-05-20) to keep that file under the
800-line guideline. ``ports.py`` re-exports both public symbols for
back-compat.
"""

from __future__ import annotations

#: Forge token-script colour letters → canonical uppercase.
_TOKEN_COLOR_MAP: dict[str, str] = {
    "w": "W",
    "u": "U",
    "b": "B",
    "r": "R",
    "g": "G",
    "c": "C",
}


def _parse_token_script(script: str) -> list[tuple[str, str]]:
    """Parse a TokenScript like ``w_1_1_soldier`` (or multi-choice
    ``w_1_1_human,u_1_1_merfolk``) into a list of ``(attr_kind, attr_value)``
    pairs.

    Supported prefix formats (parts[0]):
    - Single letter in ``_TOKEN_COLOR_MAP`` (``w``, ``u``, ``b``, ``r``, ``g``,
      ``c``) — emits one ``token_color``.
    - Multi-letter combo (``gw``, ``bg``, ``rgw``, etc.) — emits one
      ``token_color`` per letter. ``all`` expands to W/U/B/R/G.
    - Non-color leading word (named scripts like ``kobolds_of_kher_keep``) —
      emits nothing; the entry is skipped entirely to avoid junk subtypes.

    Body formats:
    - Creature         ``<p>_<t>_<subtype>[_<kw>]*``  (``w_1_1_soldier``)
    - Artifact         ``a_<subtype>[_<flag>]*``       (``c_a_food_sac``)
    - Artifact-creature ``<p>_<t>_a_<subtype>``       (``c_0_1_a_egg``)
    - X/X creature      ``x_x_<subtype>``              (``b_x_x_demon``)

    >>> _parse_token_script("w_1_1_soldier")
    [('token_color', 'W'), ('token_subtype', 'Soldier')]
    >>> _parse_token_script("w_1_1_human,u_1_1_merfolk")
    [('token_color', 'W'), ('token_subtype', 'Human'), ('token_color', 'U'), ('token_subtype', 'Merfolk')]
    >>> _parse_token_script("c_0_1_a_egg")
    [('token_color', 'C'), ('token_subtype', 'Egg')]
    >>> _parse_token_script("gw_1_1_citizen")
    [('token_color', 'G'), ('token_color', 'W'), ('token_subtype', 'Citizen')]
    """
    attrs: list[tuple[str, str]] = []
    if not script:
        return attrs
    for piece in script.split(","):
        parts = piece.strip().split("_")
        if len(parts) < 4:
            continue
        colors = _colors_for_prefix(parts[0])
        if not colors:
            # Non-color leading word (named scripts). Without a recognised
            # color prefix we cannot identify which part holds the subtype,
            # so skip entirely rather than emit junk.
            continue
        for color in colors:
            attrs.append(("token_color", color))
        # Forge TokenScript body positions vary by token kind:
        #   creature         : p _ t _ subtype              (w_1_1_soldier)
        #   artifact         : a _ subtype [_ extra]        (c_a_food_sac)
        #   artifact-creature: p _ t _ a _ subtype          (c_0_1_a_egg)
        #   X/X creature     : x _ x _ subtype              (b_x_x_demon)
        # The 'a' marker can sit at [1] (no P/T) or [3] (with P/T).
        if parts[1].isdigit() or parts[1].lower() == "x":
            # Has P/T in positions [1] and [2]. Subtype is at [3] unless
            # [3] is the 'a' artifact marker, in which case subtype is [4].
            subtype_raw = parts[4] if len(parts) >= 5 and parts[3].lower() == "a" else parts[3]
        elif parts[1].lower() == "a":
            # No P/T: artifact token. Subtype at [2].
            subtype_raw = parts[2]
        else:
            # Unknown body format; skip rather than emit a guessed subtype.
            continue
        if subtype_raw:
            attrs.append(("token_subtype", subtype_raw.capitalize()))
    return attrs


def _colors_for_prefix(prefix: str) -> list[str]:
    """Expand a TokenScript color-prefix to canonical uppercase letters.

    - ``w`` → ``['W']``
    - ``gw`` → ``['G', 'W']``
    - ``all`` → ``['W', 'U', 'B', 'R', 'G']``
    - ``kobolds`` → ``[]`` (non-color, caller should skip entire entry)
    """
    lowered = prefix.lower()
    if lowered == "all":
        return ["W", "U", "B", "R", "G"]
    expanded = [_TOKEN_COLOR_MAP[ch] for ch in lowered if ch in _TOKEN_COLOR_MAP]
    # Require the full prefix to consist of recognised colour letters; otherwise
    # the prefix is a non-color word (e.g. "kobolds") and we refuse to emit.
    if len(expanded) != len(lowered):
        return []
    return expanded
