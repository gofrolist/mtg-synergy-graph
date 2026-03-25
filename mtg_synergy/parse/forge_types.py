"""Forge-native AST types for structured card data.

Aligned with Forge's 20-year battle-tested DSL vocabulary.
These replace the original ast_types.py Effect/Trigger/ObjectFilter
with Forge-compatible equivalents.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ForgeFilter:
    """Filter for card/permanent matching. Parses Forge filter strings.

    Example: 'Creature.YouCtrl+powerGE4+attacking'
    -> ForgeFilter(card_types=["Creature"], controller="YouCtrl", power_ge=4, is_attacking=True)
    """
    card_types: list[str] = field(default_factory=list)
    subtypes: list[str] = field(default_factory=list)
    controller: str | None = None
    zone: str | None = None
    power_ge: int | None = None
    power_le: int | None = None
    toughness_ge: int | None = None
    toughness_le: int | None = None
    cmc_ge: int | None = None
    cmc_le: int | None = None
    is_token: bool | None = None
    is_attacking: bool | None = None
    is_blocking: bool | None = None
    is_tapped: bool | None = None
    has_keyword: str | None = None
    is_legendary: bool | None = None
    is_other: bool | None = None
    is_self: bool | None = None
    is_remembered: bool | None = None
    attached_by: str | None = None
    raw: str | None = None


@dataclass
class ForgeTrigger:
    """Trigger condition aligned with Forge's 134 trigger modes."""
    mode: str = ""
    valid_card: ForgeFilter | None = None
    origin: str | None = None
    destination: str | None = None
    phase: str | None = None
    trigger_zones: list[str] = field(default_factory=list)


@dataclass
class ForgeEffect:
    """Effect action aligned with Forge's 50+ effect verbs."""
    verb: str = ""
    target: ForgeFilter | None = None
    defined: str | None = None
    amount: str | None = None
    sub_ability: str | None = None
    optional: bool = False
    num_damage: int | None = None
    num_cards: int | None = None
    keyword: str | None = None
    token_script: str | None = None
    counter_type: str | None = None
    zone_origin: str | None = None
    zone_destination: str | None = None


def forge_filter_to_dict(f: ForgeFilter) -> dict:
    """Serialize ForgeFilter to JSON-compatible dict (skip None/empty)."""
    d = {}
    if f.card_types:
        d["card_types"] = f.card_types
    if f.subtypes:
        d["subtypes"] = f.subtypes
    for field_name in ("controller", "zone", "power_ge", "power_le",
                       "toughness_ge", "toughness_le", "cmc_ge", "cmc_le",
                       "is_token", "is_attacking", "is_blocking", "is_tapped",
                       "has_keyword", "is_legendary", "is_other", "is_self",
                       "is_remembered", "attached_by", "raw"):
        val = getattr(f, field_name)
        if val is not None:
            d[field_name] = val
    return d


def forge_filter_from_dict(d: dict) -> ForgeFilter:
    """Deserialize ForgeFilter from dict."""
    return ForgeFilter(
        card_types=d.get("card_types", []),
        subtypes=d.get("subtypes", []),
        controller=d.get("controller"),
        zone=d.get("zone"),
        power_ge=d.get("power_ge"),
        power_le=d.get("power_le"),
        toughness_ge=d.get("toughness_ge"),
        toughness_le=d.get("toughness_le"),
        cmc_ge=d.get("cmc_ge"),
        cmc_le=d.get("cmc_le"),
        is_token=d.get("is_token"),
        is_attacking=d.get("is_attacking"),
        is_blocking=d.get("is_blocking"),
        is_tapped=d.get("is_tapped"),
        has_keyword=d.get("has_keyword"),
        is_legendary=d.get("is_legendary"),
        is_other=d.get("is_other"),
        is_self=d.get("is_self"),
        is_remembered=d.get("is_remembered"),
        attached_by=d.get("attached_by"),
        raw=d.get("raw"),
    )
