"""Forge-only scoring — scores card candidates using the forge GBM model.

Public functions:
  color_identity_filter() — returns all color-legal non-token cards
  score_forge_candidates() — scores candidates for a single commander
  batch_recommend() — scores multiple commanders, loading model once
"""
import json
import logging
import os
import warnings

import numpy as np
import lightgbm as lgb

from mtg_synergy.config import ARTIFACT_DIR
from mtg_synergy.recommend.cmdr_patterns import detect_cmdr_patterns
from mtg_synergy.recommend.forge_features import (
    ForgeFeatureContext, CmdrFeatureContext, compute_batch_features,
)

_log = logging.getLogger(__name__)


_gbm_cache = None


def _load_gbm(artifact_dir=None):
    """Load the forge GBM model (cached after first load)."""
    global _gbm_cache
    if _gbm_cache is not None:
        return _gbm_cache
    base = artifact_dir or str(ARTIFACT_DIR)
    forge_gbm_path = os.path.join(base, "fusion_model_forge.lgb")
    if not os.path.exists(forge_gbm_path):
        return None
    _gbm_cache = lgb.Booster(model_file=forge_gbm_path)

    from mtg_synergy.model_meta import load_model_meta
    meta = load_model_meta(forge_gbm_path)
    if meta:
        _log.info("Model v%s (git:%s, NDCG@30=%.4f, md5:%s)",
                  meta["version"], meta["git_commit"],
                  meta["mean_ndcg30"], meta.get("model_md5", "?")[:8])

    return _gbm_cache


def color_identity_filter(conn, cmdr_oid: str, color_identity: set[str],
                          deck_cards: set[str] | None = None, *,
                          card_provider=None) -> list[tuple[str, str]]:
    """Return all color-legal non-token cards as (oid, name) pairs.

    If card_provider is given, uses it instead of conn for card data.
    """
    if card_provider is not None:
        exclude = set()
        if cmdr_oid:
            exclude.add(cmdr_oid)
        if deck_cards:
            # deck_cards is a set of names; we need oids for exclusion
            # For now, exclude by oid only (commander). Name filtering done by caller.
            pass
        candidates = card_provider.get_color_legal(color_identity, exclude_oids=exclude)
        results = []
        deck_names = deck_cards or set()
        for cd in candidates:
            if cd["name"] not in deck_names:
                results.append((cd["oracle_id"], cd["name"]))
        return results

    # Legacy path: direct SQL
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


def _has_unmet_ability_needs(card_needs: set[str],
                             cmdr_has: set[str]) -> bool:
    """Check if card needs Ability$ tags the commander can't provide.

    Cards needing Ability$LifeGain in a counters-only commander deck, or
    Ability$Sacrifice in a tokens-only commander, are clear mismatches.
    Only checks 'needs' (hard requirements), not 'hints' (soft preferences).
    Uses only cmdr_has (what the commander supplies), not cmdr_hints
    (what the commander wants) — a commander wanting lifegain doesn't
    mean it provides lifegain.
    """
    ability_reqs = {tag for tag in card_needs if tag.startswith("Ability$")}
    if not ability_reqs:
        return False
    return not bool(ability_reqs & cmdr_has)


def _default_oid_fn(_name: str, cd: dict) -> str:
    """Default oracle_id resolver: reads directly from card dict."""
    return cd["oracle_id"]


def _apply_penalties(scores, cand_list, ctx, cmdr_ctx, color_identity,
                     oid_fn=None):
    """Apply post-scoring penalties for clear anti-synergy patterns.

    Modifies ``scores`` in-place. ``oid_fn`` resolves a (name, card_dict) pair
    to an oracle_id; defaults to ``cd["oracle_id"]``.
    """
    if oid_fn is None:
        oid_fn = _default_oid_fn

    cmdr_subtypes = cmdr_ctx.cmdr_subtypes or set()
    cmdr_profile = cmdr_ctx.cmdr_profile
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
        # Card needs Ability$ tags the commander doesn't supply
        # (e.g., Nykthos Paragon needs LifeGain but Kyler only has Counters)
        # Uses cmdr_has only — hints mean "wants", not "provides"
        if _has_unmet_ability_needs(card_needs, cmdr_ctx.cmdr_has):
            scores[i] *= 0.85
        # Opponent-only replacement effects that conflict with commander's
        # self-targeting strategy (e.g., Bruvac doubles opponent mill but
        # Sidisi cares about self-mill)
        opp_events = profile.get('opponent_only_events', set())
        if opp_events:
            cmdr_profile = cmdr_ctx.cmdr_profile
            cmdr_trigs = cmdr_profile.get('triggers', set())
            cmdr_verbs = cmdr_profile.get('verbs', set())
            # Opponent-only mill vs commander that self-mills
            if 'Mill' in opp_events and ('Milled' in cmdr_trigs or 'Mill' in cmdr_verbs):
                scores[i] *= 0.3


def _apply_mechanical_bonus(scores, cand_list, ctx, cmdr_ctx):
    """Boost cards with strong mechanical interaction the GBM may underweight.

    Applies a multiplicative bonus (1.0-1.15) based on:
    - Mechanics vector produces↔consumes alignment
    - Verb→trigger alignment (card produces events commander triggers on)
    - Creature ETB / sacrifice / spellcast pattern matches
    """
    cmdr_profile = cmdr_ctx.cmdr_profile
    cmdr_produces = cmdr_ctx.cmdr_produces
    cmdr_consumes = cmdr_ctx.cmdr_consumes
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


def _score_commander(cmdr_oids, cmdr_names, color_identity, deck_cards,
                     ctx, gbm, card_data, top_n=50):
    """Score all color-legal candidates for commander(s). Returns ranked list of (name, score).

    Args:
        cmdr_oids: list of 1-2 oracle_ids (for partners/backgrounds).
        cmdr_names: list of 1-2 commander names.
        color_identity: union color identity set.
        card_data: dict[name -> card_dict] with "_ci_json" for color filtering.
        ctx: ForgeFeatureContext.
        gbm: LightGBM booster.
    """
    deck_cards = deck_cards or set(cmdr_names)
    cmdr_oid_set = set(cmdr_oids)
    candidates = {}
    for name, cd in card_data.items():
        if name in deck_cards or cd["oracle_id"] in cmdr_oid_set:
            continue
        ci = set(json.loads(
            cd.get("_ci_json", "[]")
        )) if "_ci_json" in cd else set()
        if ci <= color_identity:
            candidates[name] = cd

    # Commander context (supports 1-2 commanders)
    deck_oids = set()
    cmdr_ctx = CmdrFeatureContext(ctx, cmdr_oids=cmdr_oids, deck_oids=deck_oids)

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

    # Post-scoring penalties
    _apply_penalties(scores, cand_list, ctx, cmdr_ctx, color_identity)

    # Mechanical synergy bonus
    _apply_mechanical_bonus(scores, cand_list, ctx, cmdr_ctx)

    # Rank and return top N
    ranked = sorted(zip([n for n, _ in cand_list], scores),
                    key=lambda x: -x[1])
    return ranked[:top_n]


def batch_recommend(conn, commander_names: list[str], top_n: int = 30,
                    verbose: bool = True, *,
                    card_provider=None) -> dict[str, list[tuple[str, float]]]:
    """Score multiple commanders in batch, loading model and context once.

    Returns dict[commander_name -> list[(card_name, score)]].
    ~0.3s per commander after initial 3s setup (vs 7s per commander in subprocess).

    If card_provider is given, uses it for card data instead of conn.
    """
    gbm = _load_gbm()
    if gbm is None:
        _log.error("Forge model not found. Run: python3 train_fusion_model.py")
        return {}

    if verbose:
        _log.info("Loading shared context...")
    ctx = ForgeFeatureContext(conn, preload_edges=True, card_provider=card_provider)

    if card_provider is not None:
        # Build card_data from card_provider (all legal cards incl. colorless)
        all_colors = {"W", "U", "B", "R", "G", "C"}
        all_legal = card_provider.get_color_legal(all_colors)
        card_data = {}
        for cd in all_legal:
            ci = cd["color_identity"]
            card_data[cd["name"]] = {
                "oracle_id": cd["oracle_id"],
                "name": cd["name"],
                "type_line": cd.get("type_line", ""),
                "mana_cost": cd.get("mana_cost", ""),
                "cmc": cd.get("cmc", 0),
                "_ci_json": json.dumps(sorted(ci)) if isinstance(ci, set) else "[]",
            }
    else:
        # Legacy path: load from conn
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
        for row in conn.execute("SELECT name, color_identity FROM cards"):
            if row[0] in card_data:
                card_data[row[0]]["_ci_json"] = row[1] or "[]"

    # Resolve commander names to oids + color identities
    results = {}
    for i, cmdr_name in enumerate(commander_names):
        if card_provider is not None:
            cmdrs = card_provider.get_commanders([cmdr_name])
            if not cmdrs:
                if verbose:
                    _log.warning("[%d/%d] %s: NOT FOUND", i+1, len(commander_names), cmdr_name)
                continue
            cmdr_oids = [c["oracle_id"] for c in cmdrs]
            color_identity = set()
            for c in cmdrs:
                color_identity |= c["color_identity"]
        else:
            row = conn.execute(
                "SELECT oracle_id, color_identity FROM cards WHERE LOWER(name) = LOWER(?)",
                (cmdr_name,)
            ).fetchone()
            if not row:
                if verbose:
                    _log.warning("[%d/%d] %s: NOT FOUND", i+1, len(commander_names), cmdr_name)
                continue
            cmdr_oids = [row[0]]
            color_identity = set(json.loads(row[1] or "[]"))

        cmdr_names_list = [cmdr_name]
        ranked = _score_commander(
            cmdr_oids, cmdr_names_list, color_identity, set(cmdr_names_list),
            ctx, gbm, card_data, top_n=top_n)
        results[cmdr_name] = ranked

        if verbose and ((i + 1) % 50 == 0 or i == 0):
            _log.info("[%d/%d] %s: %d recs", i+1, len(commander_names), cmdr_name, len(ranked))

    return results


def score_forge_candidates(candidate_scores: dict, cards: list, conn,
                           commander: str, deck_cards: set,
                           deck_types: set = None, active_strategies: set = None,
                           color_identity: set = None,
                           forge_ctx: "ForgeFeatureContext | None" = None,
                           gbm_model=None, *,
                           card_provider=None) -> None:
    """Score candidates for a single commander. Modifies candidate_scores in-place.

    Pass forge_ctx and gbm_model to reuse pre-loaded context across calls
    (avoids ~7s rebuild per call). For batch scoring, use batch_recommend().

    If card_provider is given, uses it for missing card resolution instead of conn.
    """
    gbm = gbm_model or _load_gbm()
    if gbm is None:
        _log.error("Forge model not found. Run: python3 train_fusion_model.py")
        return

    # Build card data lookup (local copy — never mutate caller's list)
    card_data = {c["name"]: c for c in cards}
    missing = [n for n in candidate_scores if n not in card_data]
    if missing:
        if card_provider is not None:
            all_colors = {"W", "U", "B", "R", "G", "C"}
            all_legal = card_provider.get_color_legal(all_colors)
            legal_by_name = {cd["name"]: cd for cd in all_legal}
            for name in missing:
                cd = legal_by_name.get(name)
                if cd:
                    card_data[name] = {
                        "oracle_id": cd["oracle_id"], "name": cd["name"],
                        "type_line": cd.get("type_line", ""),
                        "mana_cost": cd.get("mana_cost", ""),
                        "cmc": cd.get("cmc", 0),
                    }
        else:
            for i in range(0, len(missing), 500):
                chunk = missing[i:i + 500]
                ph = ",".join("?" * len(chunk))
                for row in conn.execute(
                    f"SELECT oracle_id, name, type_line, mana_cost, cmc, edhrec_rank "
                    f"FROM cards WHERE name IN ({ph}) AND type_line NOT LIKE '%Token%'", chunk
                ).fetchall():
                    card_data[row[1]] = {
                        "oracle_id": row[0], "name": row[1],
                        "type_line": row[2] or "",
                        "mana_cost": row[3] or "", "cmc": row[4] or 0,
                    }

    cmdr_cd = card_data.get(commander)
    cmdr_oid = cmdr_cd.get("oracle_id", "") if cmdr_cd else ""

    ctx = forge_ctx or ForgeFeatureContext(conn, preload_edges=True, card_provider=card_provider)
    card_oid = {name: cd.get("oracle_id", "") for name, cd in card_data.items()}
    deck_oids = {card_oid.get(n) for n in deck_cards if n in card_oid} - {None, ""}
    cmdr_ctx = CmdrFeatureContext(ctx, cmdr_oid, deck_oids)

    cand_list = [(n, card_data[n]) for n in candidate_scores if n in card_data]
    card_oids_list = [cd.get("oracle_id") or card_oid.get(name, "") for name, cd in cand_list]
    card_cmcs = np.array([float(cd.get("cmc", 0)) for _, cd in cand_list], dtype=np.float32)
    X_batch = compute_batch_features(card_oids_list, card_cmcs, ctx, cmdr_ctx) if cand_list else None

    if X_batch is not None:
        X = X_batch.astype(np.float64)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            scores = gbm.predict(X, raw_score=True)

        # Post-scoring penalties
        def _oid_with_fallback(name: str, cd: dict) -> str:
            return cd.get("oracle_id") or card_oid.get(name, "")

        _apply_penalties(scores, cand_list, ctx, cmdr_ctx,
                         color_identity, oid_fn=_oid_with_fallback)

        # Mechanical synergy bonus
        _apply_mechanical_bonus(scores, cand_list, ctx, cmdr_ctx)

        for i, (name, _) in enumerate(cand_list):
            info = candidate_scores[name]
            info["total"] = float(scores[i]) * 10.0
            if info["total"] > 0:
                info["fusion_score"] = round(float(scores[i]), 4)
