"""Hidden gem engine — mechanical reasoning for rare synergy discovery.

Finds cards with strong mechanical interactions with a commander that
are NOT commonly played. Uses causal graph, mechanics vectors, and
forge profiles directly — no GBM model, no popularity bias.

Usage:
    python3 synergy_graph.py --commander "Krenko, Mob Boss" --gems
"""
import json
import sqlite3

import numpy as np

from mtg_synergy.recommend.cmdr_patterns import detect_cmdr_patterns
from mtg_synergy.recommend.forge_features import (
    ForgeFeatureContext, CmdrFeatureContext,
)
from mtg_synergy.recommend.mechanics_vectors import _concept_idx


def find_hidden_gems(cmdr_oid, conn, top_n=30, popularity_cap=0.05, verbose=True,
                     *, card_provider=None, cmdr_name=None, color_identity=None,
                     cmdr_oids=None, forge_ctx=None):
    """Find mechanically-synergistic cards that aren't commonly played.

    Scores cards by pure mechanical interaction with the commander:
    - Mechanics vector dot products (produces ↔ consumes)
    - Causal graph edge strength (direct + 2-hop)
    - Sacrifice/ETB/spellcast pattern matching
    - Functional fingerprint alignment

    Then FILTERS OUT popular cards (edhrec_deck_pct > popularity_cap)
    to surface hidden gems.

    Args:
        cmdr_oid: commander oracle_id (primary, for backward compat)
        conn: sqlite3 connection (for synergy data; None if forge_ctx given)
        top_n: number of results
        popularity_cap: max edhrec_deck_pct (0.05 = top 5% excluded)
        verbose: print progress
        card_provider: CardProvider for card data (optional)
        cmdr_name: commander name for display (optional, avoids cards lookup)
        color_identity: set of colors (optional, avoids cards lookup)
        cmdr_oids: list of commander oids for partner support (optional)
        forge_ctx: pre-loaded ForgeFeatureContext (optional)

    Returns:
        list of (card_name, score, reasons) tuples
    """
    oids = cmdr_oids or [cmdr_oid]
    ctx = forge_ctx or ForgeFeatureContext(conn, preload_edges=True,
                                           preload_strength=False,
                                           card_provider=card_provider)
    cmdr_ctx = CmdrFeatureContext(ctx, cmdr_oids=oids, deck_oids=set())

    # Commander mechanical profile (merged for partners)
    cmdr_profile = cmdr_ctx.cmdr_profile
    cmdr_produces = cmdr_ctx.cmdr_produces
    cmdr_consumes = cmdr_ctx.cmdr_consumes
    cmdr_func = cmdr_ctx.cmdr_func
    cmdr_verbs = cmdr_profile.get('verbs', set())
    cmdr_triggers = cmdr_profile.get('triggers', set())
    cmdr_trigger_filters = cmdr_profile.get('trigger_filters', set())
    cmdr_subtypes = cmdr_ctx.cmdr_subtypes
    cmdr_counters = cmdr_profile.get('counter_types', set())

    # Commander name for display
    if cmdr_name is None:
        if card_provider is not None:
            # Reverse-lookup: oid → name via name_to_oid dict
            oid_to_name = {v: k for k, v in card_provider.get_name_to_oid().items()}
            cmdr_name = oid_to_name.get(cmdr_oid, cmdr_oid[:8])
        else:
            row = conn.execute(
                "SELECT name FROM cards WHERE oracle_id = ?", (cmdr_oid,)).fetchone()
            cmdr_name = row[0] if row else cmdr_oid[:8]

    if verbose:
        print(f"\nHidden Gem Engine — {cmdr_name}")
        print(f"  Commander verbs: {cmdr_verbs}")
        print(f"  Commander triggers: {cmdr_triggers}")
        print(f"  Commander trigger_filters: {cmdr_trigger_filters}")
        if cmdr_subtypes:
            print(f"  Commander subtypes: {cmdr_subtypes}")

    # Get all color-legal candidates
    if color_identity is None:
        if card_provider is not None:
            cmdrs = card_provider.get_commanders([cmdr_oid])
            color_identity = cmdrs[0]["color_identity"] if cmdrs else set()
        else:
            ci_row = conn.execute(
                "SELECT color_identity FROM cards WHERE oracle_id = ?", (cmdr_oid,)
            ).fetchone()
            color_identity = set(json.loads(ci_row[0] or "[]")) if ci_row else set()

    cmdr_oid_set = set(oids)
    if card_provider is not None:
        legal = card_provider.get_color_legal(color_identity, exclude_oids=cmdr_oid_set)
        candidates = {}
        for cd in legal:
            candidates[cd["oracle_id"]] = {
                "name": cd["name"], "type_line": cd.get("type_line", ""),
                "cmc": cd.get("cmc", 0),
            }
    else:
        candidates = {}
        for row in conn.execute(
            "SELECT oracle_id, name, type_line, cmc, color_identity FROM cards "
            "WHERE legal_commander = 1 AND type_line NOT LIKE '%Token%'"
        ):
            oid, name, tl, cmc, ci_json = row
            if oid in cmdr_oid_set:
                continue
            card_ci = set(json.loads(ci_json or "[]"))
            if card_ci <= color_identity:
                candidates[oid] = {
                    "name": name, "type_line": tl or "", "cmc": cmc or 0,
                }

    if verbose:
        print(f"  Color-legal candidates: {len(candidates)}")

    # Pre-compute commander mechanical flags (once, outside loop)
    cmdr_flags = detect_cmdr_patterns(cmdr_verbs, cmdr_triggers, cmdr_trigger_filters)

    # Score each candidate by mechanical interaction
    scored = []
    for oid, card in candidates.items():
        score = 0.0
        reasons = []
        tl = card["type_line"]
        profile = ctx._forge_profiles.get(oid, {})

        if not profile:
            continue  # skip cards with no forge data

        card_verbs = profile.get('verbs', set())
        card_triggers = profile.get('triggers', set())
        card_trigger_filters = profile.get('trigger_filters', set())

        # ── 1. Mechanics vector: card produces what commander consumes ──
        card_prod = ctx._mech_produces.get(oid)
        card_cons = ctx._mech_consumes.get(oid)

        if cmdr_consumes is not None and card_prod is not None:
            fwd = float(np.dot(cmdr_consumes, card_prod))
            if fwd > 0.1:
                score += fwd * 3.0
                reasons.append(f"produces→cmdr_consumes({fwd:.2f})")

        if cmdr_produces is not None and card_cons is not None:
            rev = float(np.dot(cmdr_produces, card_cons))
            if rev > 0.1:
                score += rev * 3.0
                reasons.append(f"cmdr_produces→consumes({rev:.2f})")

        # ── 2. Causal graph: direct edge strength ──
        out_s = cmdr_ctx.cmdr_out.get(oid, 0.0)
        in_s = cmdr_ctx.cmdr_in.get(oid, 0.0)
        if out_s > 0:
            score += min(out_s, 2.0) * 2.0
            reasons.append(f"cmdr→card({out_s:.2f})")
        if in_s > 0:
            score += min(in_s, 2.0) * 2.0
            reasons.append(f"card→cmdr({in_s:.2f})")

        # ── 3. 2-hop graph: transitive connections ──
        hop2 = cmdr_ctx.cmdr_2hop_counts.get(oid, 0)
        if hop2 > 2:
            score += min(hop2, 10) * 0.3
            reasons.append(f"2hop({hop2})")

        # ── 4. Verb→trigger alignment ──
        # Card's verbs produce events that commander's triggers consume
        for v in card_verbs:
            matching = ctx._verb_triggers.get(v, set())
            hits = matching & cmdr_triggers
            if hits:
                score += len(hits) * 1.0
                reasons.append(f"verb:{v}→trigger:{','.join(hits)}")
        # Commander's verbs → card's triggers
        for v in cmdr_verbs:
            matching = ctx._verb_triggers.get(v, set())
            hits = matching & card_triggers
            if hits:
                score += len(hits) * 1.0
                reasons.append(f"cmdr:{v}→trigger:{','.join(hits)}")

        # ── 5. Creature ETB synergy (bidirectional) ──
        card_triggers_etb = ('ChangesZone' in card_triggers and
                             bool(card_trigger_filters & {'creature', 'permanent', 'nontoken'}))
        card_makes_creatures = bool(card_verbs & {'Token', 'Animate', 'Manifest'})

        if cmdr_flags.makes_creatures and card_triggers_etb:
            score += 3.0
            reasons.append("cmdr_makes_creatures→card_triggers_ETB")
        if cmdr_flags.triggers_etb and card_makes_creatures:
            score += 3.0
            reasons.append("card_makes_creatures→cmdr_triggers_ETB")

        # ── 6. Sacrifice outlet for death-trigger commanders ──
        if cmdr_flags.death_trigger and profile.get('_has_sac_cost', False):
            score += 2.5
            reasons.append("sac_outlet→cmdr_death_trigger")

        # ── 7. Spellcast trigger match ──
        if cmdr_flags.spellcast and ("Instant" in tl or "Sorcery" in tl):
            # Check if card type matches the trigger filter
            if not cmdr_trigger_filters or bool(
                cmdr_trigger_filters & {t.lower() for t in tl.replace("\u2014", " ").split()}
            ):
                cmc = card["cmc"] or 0
                # Cheaper spells = more castable = more triggers
                spell_value = max(0.5, 2.0 - cmc * 0.3)
                score += spell_value
                reasons.append(f"spell→cmdr_spellcast(cmc={cmc})")

        # ── 8. Tribal match ──
        if cmdr_subtypes and "Creature" in tl and "\u2014" in tl:
            try:
                card_subs = {s.lower() for s in tl.split("\u2014")[1].strip().split()}
                if cmdr_subtypes & card_subs:
                    score += 1.5
                    reasons.append(f"tribal({cmdr_subtypes & card_subs})")
            except (IndexError, AttributeError):
                pass

        # ── 9. Counter synergy ──
        if cmdr_counters:
            card_counters = profile.get('counter_types', set())
            shared = cmdr_counters & card_counters
            if shared:
                score += len(shared) * 1.5
                reasons.append(f"counter_match({shared})")

        # ── 10. Functional fingerprint cosine ──
        card_func = ctx._func_fingerprints.get(oid)
        if card_func is not None and cmdr_func is not None:
            norm_c = np.linalg.norm(cmdr_func)
            norm_d = np.linalg.norm(card_func)
            if norm_c > 0 and norm_d > 0:
                cos = float(np.dot(cmdr_func, card_func) / (norm_c * norm_d))
                if cos > 0.3:
                    score += cos * 2.0
                    reasons.append(f"func_cosine({cos:.2f})")

        if score > 0:
            scored.append((oid, card["name"], score, reasons))

    # Filter out popular cards
    popular = set()
    if popularity_cap > 0:
        for oid in candidates:
            pct = ctx._edhrec_deck_pct.get(oid, 0.0)
            if pct > popularity_cap:
                popular.add(oid)

    gems = []
    for oid, name, score, reasons in scored:
        if oid not in popular:
            card = candidates[oid]
            gems.append({
                "name": name,
                "score": score,
                "reasons": reasons,
                "type_line": card.get("type_line", ""),
                "cmc": card.get("cmc", 0),
            })
    gems.sort(key=lambda x: -x["score"])

    if verbose:
        n_filtered = sum(1 for oid, _, s, _ in scored if oid in popular and s > 0)
        print(f"  Scored cards: {len(scored)}, filtered popular: {n_filtered}")
        print(f"  Hidden gems found: {len(gems)}")

    return gems[:top_n]


def show_hidden_gems(cmdr_oid, conn, top_n=30, popularity_cap=0.05, **kwargs):
    """Display hidden gem recommendations in the same table format as --recommend."""
    from mtg_synergy.recommend.display import print_card_table

    gems = find_hidden_gems(cmdr_oid, conn, top_n=top_n,
                            popularity_cap=popularity_cap, **kwargs)

    if not gems:
        print("  No hidden gems found.")
        return

    print_card_table(
        f"TOP {top_n} HIDDEN GEMS — mechanically synergistic, rarely played",
        gems, top_n=top_n,
    )
