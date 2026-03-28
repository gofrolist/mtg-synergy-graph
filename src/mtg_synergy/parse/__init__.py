"""Oracle text parser — deterministic, zero-cost card text analysis."""
from mtg_synergy.parse.ast_types import Ability, Restrictions
from mtg_synergy.parse.splitter import split_abilities
from mtg_synergy.parse.trigger_parser import parse_trigger
from mtg_synergy.parse.effect_parser import parse_effects
from mtg_synergy.parse.cost_parser import parse_cost
from mtg_synergy.parse.resolver import resolve_references
from mtg_synergy.parse.templates import apply_templates


def parse_card(oracle_text: str, type_line: str = "", mana_cost: str = "") -> list[Ability]:
    if not oracle_text or not oracle_text.strip():
        return []

    raw_abilities = split_abilities(oracle_text)
    abilities = []

    for raw in raw_abilities:
        # Pass 3a: Parse trigger
        trigger = None
        if raw.kind == "triggered" and raw.trigger_text:
            trigger = parse_trigger(raw.trigger_text)

        # Pass 3b: Parse effects
        effects = []
        if raw.effect_text:
            effects = parse_effects(raw.effect_text)

        # Parse modal modes if no effects from effect_text
        if not effects and raw.modes:
            import re as _re
            for mode_text in raw.modes:
                mode_text = mode_text.strip()
                # Strip mode label (e.g., "Sell Contraband — ")
                label_match = _re.match(r'^[A-Z][\w\s]+\s*—\s*', mode_text)
                if label_match:
                    mode_text = mode_text[label_match.end():]
                mode_effects = parse_effects(mode_text)
                effects.extend(mode_effects)

        # Pass 3c: Parse cost
        cost = None
        if raw.cost_text:
            is_loyalty = raw.loyalty_cost is not None
            cost = parse_cost(raw.cost_text, is_loyalty=is_loyalty)
        if raw.loyalty_cost is not None and cost:
            cost.loyalty = raw.loyalty_cost

        # Apply templates for scaling/trigger_modifier detection
        template_result = apply_templates(raw.raw_text)

        # Determine kind — template may override
        kind = raw.kind
        if template_result and template_result.kind:
            kind = template_result.kind

        # Parse restrictions
        restrictions = _parse_restrictions(raw.restrictions_text) if raw.restrictions_text else None

        ability = Ability(
            kind=kind,
            trigger=trigger,
            cost=cost,
            effects=effects,
            restrictions=restrictions,
        )

        # Pass 4: Resolve cross-references
        ability = resolve_references(ability)

        abilities.append(ability)

    return abilities


def ensure_parse_schema(conn):
    """Create parsed_abilities table if not exists."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS parsed_abilities (
            oracle_id      TEXT NOT NULL,
            ability_index  INTEGER NOT NULL,
            kind           TEXT NOT NULL,
            ast_json       TEXT NOT NULL,
            cost_json      TEXT NOT NULL DEFAULT '{}',
            production_json TEXT NOT NULL DEFAULT '{}',
            PRIMARY KEY (oracle_id, ability_index)
        )
    """)
    conn.commit()


def save_parsed(conn, oracle_id: str, abilities: list):
    """Save parsed abilities to DB. Replaces existing entries for this oracle_id."""
    import json as _json
    conn.execute("DELETE FROM parsed_abilities WHERE oracle_id = ?", (oracle_id,))
    for i, ability in enumerate(abilities):
        d = ability.to_dict()
        conn.execute(
            "INSERT INTO parsed_abilities (oracle_id, ability_index, kind, ast_json) VALUES (?, ?, ?, ?)",
            (oracle_id, i, ability.kind, _json.dumps(d)),
        )
    conn.commit()


def load_parsed(conn, oracle_id: str) -> list:
    """Load parsed abilities from DB."""
    import json as _json
    rows = conn.execute(
        "SELECT ast_json FROM parsed_abilities WHERE oracle_id = ? ORDER BY ability_index",
        (oracle_id,),
    ).fetchall()
    return [Ability.from_dict(_json.loads(row[0])) for row in rows]


def _parse_restrictions(text: str) -> Restrictions | None:
    if not text:
        return None
    text_lower = text.lower()
    return Restrictions(
        once_per_turn="once each turn" in text_lower or "once per turn" in text_lower,
        sorcery_speed="as a sorcery" in text_lower,
        once_per_game="only once" in text_lower and "each turn" not in text_lower,
        your_turn_only="only during your turn" in text_lower,
    )
