"""Template-based pattern matching for complex oracle text patterns.

This module handles the ~5% of oracle text patterns that don't fit neatly
into the trigger/effect/cost parser pipeline — scaling expressions,
doubling effects, trigger modifiers, etc.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, List

from mtg_synergy.parse.ast_types import Effect, ScalesWith


@dataclass
class TemplateResult:
    kind: Optional[str] = None
    scaling: Optional[ScalesWith] = None
    effects: Optional[List[Effect]] = None


# Template registry: list of (compiled regex, handler function)
_TEMPLATES: list[tuple[re.Pattern, callable]] = []


def _register(pattern: str, flags: int = re.IGNORECASE):
    """Decorator to register a template regex with its handler."""
    compiled = re.compile(pattern, flags)

    def decorator(fn):
        _TEMPLATES.append((compiled, fn))
        return fn

    return decorator


# ---- Linear scaling templates ----

@_register(r'equal to the number of\s+(.+?)(?:\.|$)')
def _equal_to_number(m: re.Match) -> TemplateResult:
    return TemplateResult(
        scaling=ScalesWith(what=m.group(1).strip(), how="linear"),
    )


@_register(r'for each\s+(.+?)(?:\s+(?:you control|in your graveyard|in your hand))(.*)$')
def _for_each(m: re.Match) -> TemplateResult:
    what = m.group(1).strip()
    suffix = m.group(0)
    # Extract the "you control" / "in your graveyard" part
    loc_match = re.search(r'(you control|in your graveyard|in your hand)', suffix)
    if loc_match:
        what = what + " " + loc_match.group(1)
    return TemplateResult(
        scaling=ScalesWith(what=what, how="linear"),
    )


@_register(r'where X is the number of\s+(.+?)(?:\.|$)')
def _where_x_is(m: re.Match) -> TemplateResult:
    return TemplateResult(
        scaling=ScalesWith(what=m.group(1).strip(), how="linear"),
    )


@_register(r'that many plus one')
def _that_many_plus_one(m: re.Match) -> TemplateResult:
    return TemplateResult(
        scaling=ScalesWith(what="that many plus one", how="linear"),
    )


# ---- Multiplicative scaling templates ----

@_register(r'double the number')
def _double(m: re.Match) -> TemplateResult:
    return TemplateResult(
        scaling=ScalesWith(what="double", how="multiplicative"),
    )


@_register(r'twice that many')
def _twice_that_many(m: re.Match) -> TemplateResult:
    return TemplateResult(
        scaling=ScalesWith(what="twice that many", how="multiplicative"),
    )


# ---- Trigger modifier ----

@_register(r'triggers?\s+an\s+additional\s+time')
def _additional_trigger(m: re.Match) -> TemplateResult:
    return TemplateResult(kind="trigger_modifier")


# ---- Public API ----

def apply_templates(text: str) -> Optional[TemplateResult]:
    """Try each template regex against text, return first match or None."""
    for pattern, handler in _TEMPLATES:
        m = pattern.search(text)
        if m:
            return handler(m)
    return None


