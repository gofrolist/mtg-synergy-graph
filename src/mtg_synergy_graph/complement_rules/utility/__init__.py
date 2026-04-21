"""Utility complement matchers.

Thin re-export shim so external callers can keep using
``from mtg_synergy_graph.complement_rules.utility import _find_foo``
after the utility module was split into focused submodules.
Add new rules to one of the submodules and re-export here.
"""

from __future__ import annotations

from .axis_feeders import (
    _classify_tap_type_axis,
    _find_cardpower_axis_feeders,
    _find_cost_payoff_complements,
    _find_counter_axis_feeders,
    _find_hand_size_feeders,
    _find_modified_axis_feeders,
    _find_tap_type_feeders,
    _is_big_hand_commander,
    _only_self_sac_cost,
)
from .counters import (
    _find_counter_target_payoff,
    _find_creature_untap_engine,
)
from .doublers import (
    _find_cascade_value,
    _find_damage_doubler_synergy,
    _find_mana_doubler_synergy,
    _replacement_targets_opponent,
)
from .flicker import (
    _find_flicker_payoffs,
    _find_flicker_synergy,
)
from .landfall import (
    _find_creatures_as_lands_landfall,
    _find_extra_land_plays,
    _find_landfall_enablers,
    _has_creatures_are_lands_static,
    _port_cares_about_lands,
)
from .political import (
    _find_monarch_synergy,
    _find_opponent_forcing,
    _find_wheel_synergy,
)
from .resource_feeders import (
    _find_creature_died_feeders,
    _find_etb_tapped_stax_feeders,
    _find_gy_fuel_feeders,
    _find_land_bounce_feeders,
    _find_life_total_feeders,
    _find_lifegain_feeders,
    _find_party_feeders,
)
from .untap import (
    _find_multicolor_untap,
    _find_untap_combo,
    _find_untap_synergy,
)

__all__ = [
    "_classify_tap_type_axis",
    "_find_cardpower_axis_feeders",
    "_find_cascade_value",
    "_find_cost_payoff_complements",
    "_find_counter_axis_feeders",
    "_find_counter_target_payoff",
    "_find_creature_died_feeders",
    "_find_creature_untap_engine",
    "_find_creatures_as_lands_landfall",
    "_find_damage_doubler_synergy",
    "_find_etb_tapped_stax_feeders",
    "_find_extra_land_plays",
    "_find_flicker_payoffs",
    "_find_flicker_synergy",
    "_find_gy_fuel_feeders",
    "_find_hand_size_feeders",
    "_find_land_bounce_feeders",
    "_find_landfall_enablers",
    "_find_life_total_feeders",
    "_find_lifegain_feeders",
    "_find_mana_doubler_synergy",
    "_find_modified_axis_feeders",
    "_find_monarch_synergy",
    "_find_multicolor_untap",
    "_find_opponent_forcing",
    "_find_party_feeders",
    "_find_tap_type_feeders",
    "_find_untap_combo",
    "_find_untap_synergy",
    "_find_wheel_synergy",
    "_has_creatures_are_lands_static",
    "_is_big_hand_commander",
    "_only_self_sac_cost",
    "_port_cares_about_lands",
    "_replacement_targets_opponent",
]
