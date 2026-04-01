"""Forge-only scoring — scores card candidates using the forge GBM model.

Public functions:
  color_identity_filter() — returns all color-legal non-token cards
  score_forge_candidates() — scores candidates for a single commander
  batch_recommend() — scores multiple commanders, loading model once
"""
import json
import os
import warnings

import numpy as np
import lightgbm as lgb

from mtg_synergy.config import DATA_DIR
from mtg_synergy.recommend.cmdr_patterns import detect_cmdr_patterns
from mtg_synergy.recommend.forge_features import (
    ForgeFeatureContext, CmdrFeatureContext, compute_batch_features,
    compute_card_features,
)


_gbm_cache = None


def _load_gbm():
    """Load the forge GBM model (cached after first load)."""
    global _gbm_cache
    if _gbm_cache is not None:
        return _gbm_cache
    forge_gbm_path = os.path.join(DATA_DIR, "fusion_model_forge.lgb")
    if not os.path.exists(forge_gbm_path):
        return None
    _gbm_cache = lgb.Booster(model_file=forge_gbm_path)
    return _gbm_cache


def _load_all_cards(conn):
    """Load all card metadata from DB into a dict[name -> card_dict]."""
    card_data = {}
    for row in conn.execute(
        "SELECT oracle_id, name, type_line, mana_cost, cmc FROM cards "
        "WHERE type_line NOT LIKE '%Token%' AND legal_commander = 1"
    ):
        card_data[row[1]] = {
            "oracle_id": row[0], "name": row[1],
            "type_line": row[2] or "", "mana_cost": row[3] or "",
            "cmc": row[4] or 0,
        }
    return card_data


def color_identity_filter(conn, cmdr_oid: str, color_identity: set,
                          deck_cards: set = None) -> list:
    """Return all color-legal non-token cards as (oid, name) pairs."""
    results = []
    deck_cards = deck_cards or set()
    for row in conn.execute(
        "SELECT oracle_id, name, color_identity FROM cards "
        "WHERE type_line NOT LIKE '%Token%' AND legal_commander = 1"
    ):
        oid, name, ci_json = row
        if name in deck_cards or oid == cmdr_oid:
            continue
        ci = set(json.loads(ci_json)) if ci_json else set()
        if ci <= color_identity:
            results.append((oid, name))
    return results


_GENERIC_REQ = {"card", "creature", "permanent", "self", "other",
                "nontoken", "token", "artifact", "enchantment", "land",
                "spell", "any"}

_COLOR_NAME_TO_SYMBOL = {
    "white": "W", "blue": "U", "black": "B", "red": "R", "green": "G",
    "colorless": "C",
}


def _needs_wrong_colors(card_needs: set, color_identity: set) -> bool:
    """Check if card needs colors outside the commander's color identity.

    Forge tags like needs=Color$White, needs=Color$Red|Green are split and
    mapped to MTG color symbols (W, U, B, R, G) for comparison.
    """
    for tag in card_needs:
        if not tag.startswith("Color$"):
            continue
        colors_str = tag[6:]  # strip "Color$"
        needed_symbols = set()
        for color_name in colors_str.split("|"):
            sym = _COLOR_NAME_TO_SYMBOL.get(color_name.lower().strip())
            if sym and sym != "C":
                needed_symbols.add(sym)
        # If the card needs specific colors and NONE are in commander's identity
        if needed_symbols and not (needed_symbols & color_identity):
            return True
    return False


def _has_unmet_type_needs(card_needs: set, card_hints: set,
                          cmdr_provides: set) -> bool:
    """Check if card needs/hints at Type$ tags that the commander can't provide.

    Only fires on Type$ tags (creature subtypes and card types like Enchantment,
    Aura, Equipment). Ignores Ability$ and other tag prefixes.
    Cards needing Type$Dinosaur in a Human deck, or Type$Enchantment in a
    counters deck, are clear mismatches.
    """
    type_reqs = set()
    for tag in (card_needs | card_hints):
        if tag.startswith("Type$"):
            type_reqs.add(tag)
    if not type_reqs:
        return False
    # Check if ANY type requirement is met by commander's has+hints
    return not bool(type_reqs & cmdr_provides)


def _apply_penalties(scores, cand_list, ctx, cmdr_ctx, cmdr_oid,
                     color_identity, oid_fn=None):
    """Apply post-scoring penalties for clear anti-synergy patterns.

    Modifies ``scores`` in-place. ``oid_fn`` resolves a (name, card_dict) pair
    to an oracle_id; defaults to ``cd["oracle_id"]``.
    """
    if oid_fn is None:
        oid_fn = lambda _name, cd: cd["oracle_id"]

    cmdr_subtypes = cmdr_ctx.cmdr_subtypes or set()
    cmdr_profile = ctx._forge_profiles.get(cmdr_oid, {})
    cmdr_has_counters = 'P1P1' in cmdr_profile.get('counter_types', set())
    cmdr_is_tribal = bool(cmdr_profile.get('trigger_filters', set()) & cmdr_subtypes)

    for i, (name, cd) in enumerate(cand_list):
        oid = oid_fn(name, cd)
        profile = ctx._forge_profiles.get(oid, {})
        # Card requires a creature subtype the commander doesn't have
        # (e.g. Dragonspeaker Shaman requires Dragon, Lullmage Mentor requires Merfolk)
        req = profile.get('required_subtypes', set())
        non_generic_req = req - _GENERIC_REQ
        if non_generic_req and cmdr_subtypes and not (non_generic_req & cmdr_subtypes):
            scores[i] *= 0.4
        if cmdr_is_tribal:
            # Card creates tokens of wrong creature type
            token_subs = ctx._token_subtypes.get(oid, set())
            if token_subs and not (token_subs & cmdr_subtypes):
                scores[i] *= 0.5
        # Cards whose core mechanic is a niche counter type (TIME, EXPERIENCE)
        # that the commander doesn't use — penalize unless commander shares it
        card_counters = profile.get('counter_types', set())
        cmdr_counter_types = cmdr_profile.get('counter_types', set())
        niche_only = {'TIME', 'EXPERIENCE', 'ENERGY'}
        card_niche = card_counters & niche_only
        if card_niche and not (card_niche & cmdr_counter_types):
            # Only penalize if card has NO other counter types (niche is the core)
            other_counters = card_counters - niche_only - {'All', 'Any', 'EachFromSource', 'EachType'}
            if not other_counters:
                scores[i] *= 0.4
        # Penalty: counter commanders + cards with wrong counter types
        if cmdr_has_counters:
            card_counters = profile.get('counter_types', set())
            generic_counters = {'All', 'Any', 'EachFromSource', 'EachType'}
            specific_counters = card_counters - generic_counters
            # Card puts wrong counter type (e.g. M1M1, TIME instead of P1P1)
            if specific_counters and 'P1P1' not in specific_counters:
                scores[i] *= 0.4
            # Creature without any P1P1 interaction
            elif "Creature" in cd.get("type_line", ""):
                card_has_p1p1 = profile.get('has_p1p1', False)
                card_counter_verbs = profile.get('verbs', set()) & {
                    'PutCounter', 'PutCounterAll', 'Proliferate', 'MoveCounter'}
                card_puts_p1p1 = 'P1P1' in card_counters or not card_counters
                if not card_has_p1p1 and not (card_counter_verbs and card_puts_p1p1):
                    scores[i] *= 0.6
            # Card places counters on lands, not creatures (earthbend etc.)
            if profile.get('counters_on_lands', False):
                scores[i] *= 0.4
        # Partner-type cards useless without matching partner commander
        card_kws = profile.get('keywords', set())
        if 'Choose a Background' in card_kws:
            scores[i] = -1e9
            continue
        if "Doctor's companion" in card_kws:
            scores[i] = -1e9
            continue
        # Card needs colors outside commander's color identity
        # Hard filter: these cards are guaranteed useless
        card_needs = ctx._deck_needs.get(oid, set())
        if color_identity is not None and card_needs and _needs_wrong_colors(card_needs, color_identity):
            scores[i] = -1e9
            continue  # hard-filtered; skip remaining penalties for this card
        # Card needs/hints at Type$ tags the commander can't provide
        card_hints = ctx._deck_hints.get(oid, set())
        cmdr_prov = cmdr_ctx.cmdr_has | cmdr_ctx.cmdr_hints
        if _has_unmet_type_needs(card_needs, card_hints, cmdr_prov):
            scores[i] *= 0.3
        # Opponent-only replacement effects that conflict with commander's
        # self-targeting strategy (e.g., Bruvac doubles opponent mill but
        # Sidisi cares about self-mill)
        opp_events = profile.get('opponent_only_events', set())
        if opp_events:
            cmdr_strats = cmdr_ctx.cmdr_strats
            if 'Mill' in opp_events and 'self-mill' in cmdr_strats:
                scores[i] *= 0.3


def _apply_mechanical_bonus(scores, cand_list, ctx, cmdr_ctx, cmdr_oid):
    """Boost cards with strong mechanical interaction the GBM may underweight.

    Applies a multiplicative bonus (1.0-1.15) based on:
    - Mechanics vector produces↔consumes alignment
    - Verb→trigger alignment (card produces events commander triggers on)
    - Creature ETB / sacrifice / spellcast pattern matches
    """
    cmdr_profile = ctx._forge_profiles.get(cmdr_oid, {})
    cmdr_produces = ctx._mech_produces.get(cmdr_oid)
    cmdr_consumes = ctx._mech_consumes.get(cmdr_oid)
    cmdr_verbs = cmdr_profile.get('verbs', set())
    cmdr_triggers = cmdr_profile.get('triggers', set())
    cmdr_trigger_filters = cmdr_profile.get('trigger_filters', set())

    # Pre-compute commander flags
    cmdr_flags = detect_cmdr_patterns(cmdr_verbs, cmdr_triggers, cmdr_trigger_filters)

    for i, (name, cd) in enumerate(cand_list):
        if scores[i] <= 0:
            continue

        oid = cd.get("oracle_id", "")
        profile = ctx._forge_profiles.get(oid, {})
        if not profile:
            continue

        bonus = 0.0
        card_verbs = profile.get('verbs', set())
        card_triggers = profile.get('triggers', set())
        card_trigger_filters = profile.get('trigger_filters', set())
        tl = cd.get("type_line", "")

        # Mechanics vector alignment
        card_prod = ctx._mech_produces.get(oid)
        card_cons = ctx._mech_consumes.get(oid)
        if cmdr_consumes is not None and card_prod is not None:
            fwd = float(np.dot(cmdr_consumes, card_prod))
            if fwd > 0.2:
                bonus += fwd * 0.05

        if cmdr_produces is not None and card_cons is not None:
            rev = float(np.dot(cmdr_produces, card_cons))
            if rev > 0.2:
                bonus += rev * 0.05

        # Verb→trigger alignment
        for v in card_verbs:
            matching = ctx._verb_triggers.get(v, set())
            if matching & cmdr_triggers:
                bonus += 0.02
                break
        for v in cmdr_verbs:
            matching = ctx._verb_triggers.get(v, set())
            if matching & card_triggers:
                bonus += 0.02
                break

        # Creature ETB synergy (bidirectional)
        card_triggers_etb = ('ChangesZone' in card_triggers and
                             bool(card_trigger_filters & {'creature', 'permanent', 'nontoken'}))
        card_makes_creatures = bool(card_verbs & {'Token', 'Animate', 'Manifest'})
        if (cmdr_flags.makes_creatures and card_triggers_etb) or \
           (cmdr_flags.triggers_etb and card_makes_creatures):
            bonus += 0.05

        # Sacrifice outlet for death-trigger commanders
        if cmdr_flags.death_trigger and profile.get('_has_sac_cost', False):
            bonus += 0.05

        # Spellcast trigger match
        if cmdr_flags.spellcast and ("Instant" in tl or "Sorcery" in tl):
            if not cmdr_trigger_filters or bool(
                cmdr_trigger_filters & {t.lower() for t in tl.replace("\u2014", " ").split()}
            ):
                bonus += 0.03

        # Apply as multiplicative bonus (capped at 15%)
        if bonus > 0:
            scores[i] *= (1.0 + min(bonus, 0.15))


def _score_commander(cmdr_oid, cmdr_name, color_identity, deck_cards,
                     ctx, gbm, card_data, top_n=50):
    """Score all color-legal candidates for one commander. Returns ranked list of (name, score).

    Uses pre-loaded ctx and gbm to avoid re-initialization.
    """
    # Color identity filter
    deck_cards = deck_cards or {cmdr_name}
    candidates = {}
    for name, cd in card_data.items():
        if name in deck_cards or cd["oracle_id"] == cmdr_oid:
            continue
        ci = set(json.loads(
            cd.get("_ci_json", "[]")
        )) if "_ci_json" in cd else set()
        # We already filtered tokens in _load_all_cards
        if ci <= color_identity:
            candidates[name] = cd

    # Commander context
    deck_oids = set()
    cmdr_ctx = CmdrFeatureContext(ctx, cmdr_oid, deck_oids)
    from mtg_synergy.config import extract_subtypes
    cmdr_type = card_data.get(cmdr_name, {}).get("type_line", "")
    cmdr_ctx.cmdr_subtypes = extract_subtypes(cmdr_type)

    # Compute features
    cand_list = list(candidates.items())
    if not cand_list:
        return []

    card_oids_list = [cd["oracle_id"] for _, cd in cand_list]
    card_cmcs = np.array([float(cd["cmc"]) for _, cd in cand_list], dtype=np.float32)
    X = compute_batch_features(card_oids_list, card_cmcs, ctx, cmdr_ctx).astype(np.float64)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        scores = gbm.predict(X, raw_score=True)

    # Post-scoring penalties for clear anti-synergy patterns
    _apply_penalties(scores, cand_list, ctx, cmdr_ctx, cmdr_oid, color_identity)

    # Mechanical synergy bonus: boost cards with strong forge-mechanical
    # interaction that the GBM may underweight. Uses the same signals as
    # the hidden gem engine but as a mild re-ranking bonus (~5-15% of score).
    _apply_mechanical_bonus(scores, cand_list, ctx, cmdr_ctx, cmdr_oid)

    # Rank and return top N
    ranked = sorted(zip([n for n, _ in cand_list], scores),
                    key=lambda x: -x[1])
    return ranked[:top_n]


def batch_recommend(conn, commander_names: list[str], top_n: int = 30,
                    verbose: bool = True) -> dict[str, list[tuple[str, float]]]:
    """Score multiple commanders in batch, loading model and context once.

    Returns dict[commander_name -> list[(card_name, score)]].
    ~0.3s per commander after initial 3s setup (vs 7s per commander in subprocess).
    """
    gbm = _load_gbm()
    if gbm is None:
        print("ERROR: forge model not found. Run: python3 train_fusion_model.py")
        return {}

    if verbose:
        print("Loading shared context...")
    ctx = ForgeFeatureContext(conn, preload_edges=True)

    # Load all card data + color identities
    card_data = _load_all_cards(conn)
    # Add color identity to card_data for fast filtering
    for row in conn.execute("SELECT name, color_identity FROM cards"):
        if row[0] in card_data:
            card_data[row[0]]["_ci_json"] = row[1] or "[]"

    # Resolve commander names to oids + color identities
    results = {}
    for i, cmdr_name in enumerate(commander_names):
        row = conn.execute(
            "SELECT oracle_id, color_identity FROM cards WHERE LOWER(name) = LOWER(?)",
            (cmdr_name,)
        ).fetchone()
        if not row:
            if verbose:
                print(f"  [{i+1}/{len(commander_names)}] {cmdr_name}: NOT FOUND")
            continue

        cmdr_oid, ci_json = row
        color_identity = set(json.loads(ci_json or "[]"))

        ranked = _score_commander(
            cmdr_oid, cmdr_name, color_identity, {cmdr_name},
            ctx, gbm, card_data, top_n=top_n)
        results[cmdr_name] = ranked

        if verbose and ((i + 1) % 50 == 0 or i == 0):
            print(f"  [{i+1}/{len(commander_names)}] {cmdr_name}: {len(ranked)} recs")

    return results


def score_forge_candidates(candidate_scores: dict, cards: list, conn,
                           commander: str, deck_cards: set,
                           deck_types: set = None, active_strategies: set = None,
                           color_identity: set = None,
                           forge_ctx: "ForgeFeatureContext | None" = None,
                           gbm_model=None) -> None:
    """Score candidates for a single commander. Modifies candidate_scores in-place.

    Pass forge_ctx and gbm_model to reuse pre-loaded context across calls
    (avoids ~7s rebuild per call). For batch scoring, use batch_recommend().
    """
    gbm = gbm_model or _load_gbm()
    if gbm is None:
        print("  ERROR: forge model not found. Run: python3 train_fusion_model.py")
        return

    # Build card data lookup
    card_data = {c["name"]: c for c in cards}
    missing = [n for n in candidate_scores if n not in card_data]
    if missing:
        for i in range(0, len(missing), 500):
            chunk = missing[i:i + 500]
            ph = ",".join("?" * len(chunk))
            for row in conn.execute(
                f"SELECT oracle_id, name, type_line, mana_cost, cmc, edhrec_rank "
                f"FROM cards WHERE name IN ({ph}) AND type_line NOT LIKE '%Token%'", chunk
            ).fetchall():
                cd = {"oracle_id": row[0], "name": row[1], "type_line": row[2] or "",
                      "mana_cost": row[3] or "", "cmc": row[4] or 0}
                cards.append(cd)
                card_data[row[1]] = cd

    cmdr_oid = ""
    for c in cards:
        if c["name"] == commander:
            cmdr_oid = c.get("oracle_id", "")
            break

    ctx = forge_ctx or ForgeFeatureContext(conn, preload_edges=True)
    card_oid = {c["name"]: c.get("oracle_id", "") for c in cards}
    deck_oids = {card_oid.get(n) for n in deck_cards if n in card_oid} - {None, ""}
    cmdr_ctx = CmdrFeatureContext(ctx, cmdr_oid, deck_oids)

    from mtg_synergy.config import extract_subtypes
    cmdr_type = next((c.get("type_line", "") for c in cards if c.get("oracle_id") == cmdr_oid), "")
    cmdr_ctx.cmdr_subtypes = extract_subtypes(cmdr_type)

    cand_list = [(n, card_data[n]) for n in candidate_scores if n in card_data]
    card_oids_list = [cd.get("oracle_id") or card_oid.get(name, "") for name, cd in cand_list]
    card_cmcs = np.array([float(cd.get("cmc", 0)) for _, cd in cand_list], dtype=np.float32)
    X_batch = compute_batch_features(card_oids_list, card_cmcs, ctx, cmdr_ctx) if cand_list else None

    if X_batch is not None:
        X = X_batch.astype(np.float64)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            scores = gbm.predict(X, raw_score=True)

        # Post-scoring penalties for clear anti-synergy patterns
        _apply_penalties(scores, cand_list, ctx, cmdr_ctx, cmdr_oid,
                         color_identity,
                         oid_fn=lambda name, cd, _oid=card_oid: cd.get("oracle_id") or _oid.get(name, ""))

        # Mechanical synergy bonus (same as _score_commander path)
        _apply_mechanical_bonus(scores, cand_list, ctx, cmdr_ctx, cmdr_oid)

        for i, (name, _) in enumerate(cand_list):
            info = candidate_scores[name]
            info["total"] = float(scores[i]) * 10.0
            if info["total"] > 0:
                info["fusion_score"] = round(float(scores[i]), 4)
