"""Forge data types for structured card filter parsing.

Aligned with Forge's DSL vocabulary (ValidCard$ filter expressions).
"""
from __future__ import annotations
from dataclasses import dataclass, field


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


