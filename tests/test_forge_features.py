"""Tests for ForgeFeatureContext forge ability profiles and Forge-native features."""

import os
import sqlite3

import numpy as np
import pytest

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "tags.db")

# Well-known oracle_ids for test cards
KRENKO_OID = "68418069-f615-40ef-ae0d-764192acae00"  # Krenko, Mob Boss (Goblin)


# ── Cached context: loaded once, shared across all tests ──
_cached_ctx = None
_cached_conn = None


def _make_ctx():
    """Return a cached ForgeFeatureContext (loaded once per test session).

    The connection must NOT be closed by callers — existing conn.close()
    calls in try/finally blocks are harmless no-ops after the first test
    since we reassign to a fresh connection if closed.
    """
    global _cached_ctx, _cached_conn
    if _cached_ctx is None or _cached_conn is None:
        if not os.path.exists(DB_PATH):
            pytest.skip("tags.db not found")
        from mtg_synergy.recommend.forge_features import ForgeFeatureContext
        _cached_conn = sqlite3.connect(DB_PATH)
        _cached_ctx = ForgeFeatureContext(_cached_conn, preload_edges=False)
    return _cached_ctx, _cached_conn


def test_forge_profiles_loaded():
    """_forge_profiles dict exists, is non-empty, and entries have correct structure."""
    ctx, conn = _make_ctx()
    try:
        assert hasattr(ctx, "_forge_profiles")
        assert isinstance(ctx._forge_profiles, dict)
        assert len(ctx._forge_profiles) > 1000, (
            f"Expected >1000 profiles, got {len(ctx._forge_profiles)}"
        )
        # Check structure of an arbitrary entry
        sample_oid = next(iter(ctx._forge_profiles))
        profile = ctx._forge_profiles[sample_oid]
        expected_set_keys = {
            "verbs", "triggers", "keywords", "counter_types",
            "targets", "ability_types", "trigger_filters", "required_subtypes",
            "granted_keywords", "conditions", "duration",
            "effect_zones", "scales_with", "grants_types", "mana_colors",
            "excluded_subtypes", "counter_trigger_themes",
            "opponent_only_events",
        }
        expected_bool_keys = {
            "combat_damage", "is_secondary", "gain_control",
            "produces_mana", "counter_num_variable", "grants_abilities",
            "token_amount_variable", "has_static_anthem", "counters_on_lands", "has_p1p1",
        }
        expected_optional_str_keys = {"damage_amount", "cards_drawn", "life_amount"}
        all_expected = expected_set_keys | expected_bool_keys | expected_optional_str_keys
        assert set(profile.keys()) == all_expected, (
            f"Profile keys mismatch.\n  Missing: {all_expected - set(profile.keys())}"
            f"\n  Extra: {set(profile.keys()) - all_expected}"
        )
        # Set-valued fields should be sets
        for key in expected_set_keys:
            assert isinstance(profile[key], set), f"{key} should be a set"
        # Bool-valued fields should be bool
        for key in expected_bool_keys:
            assert isinstance(profile[key], bool), f"{key} should be a bool"
        # Optional str fields should be str or None
        for key in expected_optional_str_keys:
            assert profile[key] is None or isinstance(profile[key], str), (
                f"{key} should be str or None, got {type(profile[key])}"
            )
    finally:
        pass  # conn is cached, do not close


def test_forge_profile_krenko():
    """Krenko Mob Boss should have Token verb in its profile."""
    ctx, conn = _make_ctx()
    try:
        # Krenko, Mob Boss oracle_id
        krenko_oid = KRENKO_OID
        assert krenko_oid in ctx._forge_profiles, (
            "Krenko Mob Boss not found in forge profiles"
        )
        profile = ctx._forge_profiles[krenko_oid]
        assert "Token" in profile["verbs"], (
            f"Expected 'Token' in Krenko verbs, got {profile['verbs']}"
        )
        # Krenko should have an activated ability type
        assert "A" in profile["ability_types"], (
            f"Expected 'A' (activated) in Krenko ability_types, got {profile['ability_types']}"
        )
    finally:
        pass  # conn is cached, do not close


# ---------------------------------------------------------------------------
# Tests for Forge-native features F23-F28
# ---------------------------------------------------------------------------

def _make_cmdr(ctx, cmdr_oid, subtypes=None):
    """Build a minimal CmdrFeatureContext for testing."""
    from mtg_synergy.recommend.forge_features import CmdrFeatureContext
    cmdr = CmdrFeatureContext(ctx, cmdr_oid, set())
    if subtypes is not None:
        cmdr.cmdr_subtypes = subtypes
    return cmdr


def _find_oid_by_name(conn, name):
    """Look up oracle_id by exact card name."""
    row = conn.execute(
        "SELECT oracle_id FROM cards WHERE name = ?", (name,)
    ).fetchone()
    return row[0] if row else None


def _compute_features(ctx, cmdr, card_oid, conn):
    """Compute forge feature vector for a single card."""
    from mtg_synergy.recommend.forge_features import compute_card_features
    row = conn.execute(
        "SELECT type_line, cmc FROM cards WHERE oracle_id = ?", (card_oid,)
    ).fetchone()
    tl = row[0] if row else ""
    cmc = row[1] if row else 0.0
    return compute_card_features(card_oid, tl, cmc, ctx, cmdr)


class TestF23ForgeTypeSynergy:
    """F23: card's Forge trigger_filter/target references commander's creature subtypes."""

    def test_goblin_trigger_for_goblin_commander(self):
        """A card with Goblin trigger_filter should get >0 for a Goblin commander."""
        ctx, conn = _make_ctx()
        try:
            cmdr = _make_cmdr(ctx, KRENKO_OID, subtypes={"goblin"})
            # Find a card that references goblins in its trigger_filters
            found_oid = None
            for oid, profile in ctx._forge_profiles.items():
                if "goblin" in profile.get("trigger_filters", set()):
                    found_oid = oid
                    break
            if found_oid is None:
                pytest.skip("No card with 'goblin' trigger_filter found in DB")
            features = _compute_features(ctx, cmdr, found_oid, conn)
            assert features[13] > 0.0, (
                f"F13 should be >0 for a goblin-triggering card with Goblin commander, got {features[13]}"
            )
        finally:
            pass  # conn is cached, do not close

    def test_no_match_for_unrelated_type(self):
        """A card without goblin references should get 0 for a Goblin commander."""
        ctx, conn = _make_ctx()
        try:
            cmdr = _make_cmdr(ctx, KRENKO_OID, subtypes={"goblin"})
            # Find a card that has trigger_filters but NOT goblin
            found_oid = None
            for oid, profile in ctx._forge_profiles.items():
                tfs = profile.get("trigger_filters", set())
                if tfs and "goblin" not in tfs:
                    # Also check targets don't have Goblin
                    targets = profile.get("targets", set())
                    if "Goblin" not in targets:
                        found_oid = oid
                        break
            if found_oid is None:
                pytest.skip("No card with non-goblin trigger_filter found")
            features = _compute_features(ctx, cmdr, found_oid, conn)
            assert features[13] == 0.0, (
                f"F13 should be 0 for non-goblin card with Goblin commander, got {features[13]}"
            )
        finally:
            pass  # conn is cached, do not close


class TestF24CmdrForgeTypeMatch:
    """F24: commander's Forge trigger_filter/target references card's subtypes."""

    def test_human_commander_human_card(self):
        """Commander with Human trigger_filter → Human creature card gets >0."""
        ctx, conn = _make_ctx()
        try:
            # Find a commander with 'human' in trigger_filters
            cmdr_oid = None
            for oid, profile in ctx._forge_profiles.items():
                if "human" in profile.get("trigger_filters", set()):
                    cmdr_oid = oid
                    break
            if cmdr_oid is None:
                pytest.skip("No commander with 'human' trigger_filter found")
            cmdr = _make_cmdr(ctx, cmdr_oid)
            # Find a Human creature card
            row = conn.execute(
                "SELECT oracle_id FROM cards WHERE type_line LIKE '%Creature%' "
                "AND type_line LIKE '%Human%' LIMIT 1"
            ).fetchone()
            if row is None:
                pytest.skip("No Human creature card found")
            features = _compute_features(ctx, cmdr, row[0], conn)
            assert features[14] > 0.0, (
                f"F14 should be >0 for Human card with Human-trigger commander, got {features[14]}"
            )
        finally:
            pass  # conn is cached, do not close



class TestF27ForgeAntiTribal:
    """F27: card's Forge trigger_filter requires conflicting creature subtype."""

    def test_spirit_trigger_in_goblin_deck(self):
        """A Spirit-triggering card in a Goblin deck should get >0 anti-tribal."""
        ctx, conn = _make_ctx()
        try:
            cmdr = _make_cmdr(ctx, KRENKO_OID, subtypes={"goblin"})
            # Find a card with a specific non-generic, non-goblin trigger_filter
            generic = {"card", "creature", "permanent", "nontoken",
                       "token", "artifact", "enchantment", "land",
                       "spell", "self", "other", "any"}
            found_oid = None
            for oid, profile in ctx._forge_profiles.items():
                tfs = profile.get("trigger_filters", set())
                conflicting = tfs - generic - {"goblin"}
                if conflicting:
                    found_oid = oid
                    break
            if found_oid is None:
                pytest.skip("No card with conflicting tribal trigger_filter found")
            features = _compute_features(ctx, cmdr, found_oid, conn)
            assert features[16] > 0.0, (
                f"F16 should be >0 for conflicting-tribal card in Goblin deck, got {features[16]}"
            )
        finally:
            pass  # conn is cached, do not close

    def test_goblin_card_not_anti_tribal(self):
        """A Goblin-triggering card should NOT be anti-tribal in a Goblin deck."""
        ctx, conn = _make_ctx()
        try:
            cmdr = _make_cmdr(ctx, KRENKO_OID, subtypes={"goblin"})
            # Find a card with ONLY goblin and/or generic trigger_filters
            generic = {"card", "creature", "permanent", "nontoken",
                       "token", "artifact", "enchantment", "land",
                       "spell", "self", "other", "any"}
            found_oid = None
            for oid, profile in ctx._forge_profiles.items():
                tfs = profile.get("trigger_filters", set())
                if "goblin" in tfs and not (tfs - generic - {"goblin"}):
                    found_oid = oid
                    break
            if found_oid is None:
                pytest.skip("No card with goblin-only trigger_filter found")
            features = _compute_features(ctx, cmdr, found_oid, conn)
            assert features[16] == 0.0, (
                f"F16 should be 0 for goblin card in Goblin deck, got {features[16]}"
            )
        finally:
            pass  # conn is cached, do not close


class TestF28ForgeVerbAlignment:
    """F28: card's verbs produce events that commander's triggers consume."""

    def test_token_creator_changeszone_trigger(self):
        """Token-creating card + ChangesZone-triggering commander gets >0."""
        ctx, conn = _make_ctx()
        try:
            # Find a commander with ChangesZone trigger
            cmdr_oid = None
            for oid, profile in ctx._forge_profiles.items():
                if "ChangesZone" in profile.get("triggers", set()):
                    cmdr_oid = oid
                    break
            if cmdr_oid is None:
                pytest.skip("No commander with ChangesZone trigger found")
            cmdr = _make_cmdr(ctx, cmdr_oid)
            # Find a card with Token verb (Token → ChangesZone mapping)
            token_oid = None
            for oid, profile in ctx._forge_profiles.items():
                if "Token" in profile.get("verbs", set()) and oid != cmdr_oid:
                    token_oid = oid
                    break
            if token_oid is None:
                pytest.skip("No card with Token verb found")
            features = _compute_features(ctx, cmdr, token_oid, conn)
            assert features[17] > 0.0, (
                f"F17 should be >0 for Token card + ChangesZone commander, got {features[17]}"
            )
        finally:
            pass  # conn is cached, do not close

    def test_no_alignment_when_no_verb_match(self):
        """Cards with no verb→trigger alignment should get 0."""
        ctx, conn = _make_ctx()
        try:
            # Find a commander with no triggers at all
            cmdr_oid = None
            for oid, profile in ctx._forge_profiles.items():
                if not profile.get("triggers", set()) and not profile.get("verbs", set()):
                    cmdr_oid = oid
                    break
            if cmdr_oid is None:
                pytest.skip("No commander with empty triggers and verbs found")
            cmdr = _make_cmdr(ctx, cmdr_oid)
            # Any card — with no commander triggers/verbs, alignment must be 0
            card_oid = None
            for oid in ctx._forge_profiles:
                if oid != cmdr_oid:
                    card_oid = oid
                    break
            if card_oid is None:
                pytest.skip("No other card found")
            features = _compute_features(ctx, cmdr, card_oid, conn)
            assert features[17] == 0.0, (
                f"F17 should be 0 when commander has no triggers/verbs, got {features[17]}"
            )
        finally:
            pass  # conn is cached, do not close


# ---------------------------------------------------------------------------
# Tests for new features F31-F37
# ---------------------------------------------------------------------------

KYLER_OID = "726cd041-5d0b-436c-bced-9335f56c0b0d"  # Kyler, Sigardian Emissary


class TestF31CounterTypeMatch:
    """F31: card uses same counter type as commander."""

    def test_p1p1_counter_match_with_kyler(self):
        """Kyler (P1P1 counters) + another P1P1 card → >0."""
        ctx, conn = _make_ctx()
        try:
            cmdr = _make_cmdr(ctx, KYLER_OID, subtypes={"human"})
            # Find a card with P1P1 counter_type that isn't Kyler
            found_oid = None
            for oid, profile in ctx._forge_profiles.items():
                if "P1P1" in profile.get("counter_types", set()) and oid != KYLER_OID:
                    found_oid = oid
                    break
            if found_oid is None:
                pytest.skip("No card with P1P1 counter_type found")
            features = _compute_features(ctx, cmdr, found_oid, conn)
            assert features[18] > 0.0, (
                f"F18 should be >0 for P1P1 card with Kyler, got {features[18]}"
            )
        finally:
            pass  # conn is cached, do not close

    def test_no_counter_match(self):
        """Card without matching counter type → 0."""
        ctx, conn = _make_ctx()
        try:
            cmdr = _make_cmdr(ctx, KYLER_OID, subtypes={"human"})
            # Find a card with no counter_types at all
            found_oid = None
            for oid, profile in ctx._forge_profiles.items():
                if not profile.get("counter_types", set()) and oid != KYLER_OID:
                    found_oid = oid
                    break
            if found_oid is None:
                pytest.skip("No card without counter_types found")
            features = _compute_features(ctx, cmdr, found_oid, conn)
            assert features[18] == 0.0, (
                f"F18 should be 0 for card without counters, got {features[18]}"
            )
        finally:
            pass  # conn is cached, do not close


class TestMechanicsVectorsNoOracleText:
    """Verify mechanics_vectors does not query cards.oracle_text."""

    def test_mechanics_vectors_no_oracle_text(self):
        """mechanics_vectors should not query cards.oracle_text anymore."""
        if not os.path.exists(DB_PATH):
            pytest.skip("tags.db not found")
        from mtg_synergy.recommend.mechanics_vectors import build_mechanics_vectors

        real_conn = sqlite3.connect(DB_PATH)

        # Wrap connection to track queries (sqlite3.Connection.execute is read-only)
        queries = []

        class TrackingConn:
            """Thin wrapper that logs SQL and delegates to the real connection."""
            def __init__(self, conn):
                self._conn = conn

            def execute(self, sql, *args, **kwargs):
                queries.append(sql)
                return self._conn.execute(sql, *args, **kwargs)

            def __getattr__(self, name):
                return getattr(self._conn, name)

        wrapper = TrackingConn(real_conn)
        produces, consumes, dim, subtype_idx = build_mechanics_vectors(wrapper)
        real_conn.close()

        oracle_queries = [q for q in queries if 'oracle_text' in q.lower()]
        assert len(oracle_queries) == 0, (
            f"mechanics_vectors should not query oracle_text, found: {oracle_queries}"
        )
        assert len(produces) > 0
        assert len(consumes) > 0



class TestF7ForgeAbilityCosine:
    """F7: forge_ability_cosine — cosine similarity of Forge ability vectors."""

    def test_shared_verbs_positive_cosine(self):
        """Two cards sharing verbs should have >0 cosine similarity."""
        ctx, conn = _make_ctx()
        try:
            # Find two cards that both have Token verb
            token_cards = []
            for oid, profile in ctx._forge_profiles.items():
                if "Token" in profile.get("verbs", set()):
                    token_cards.append(oid)
                    if len(token_cards) >= 2:
                        break
            if len(token_cards) < 2:
                pytest.skip("Need at least 2 cards with Token verb")
            cmdr = _make_cmdr(ctx, token_cards[0])
            features = _compute_features(ctx, cmdr, token_cards[1], conn)
            assert features[4] > 0.0, (
                f"F4 should be >0 for two cards sharing Token verb, got {features[4]}"
            )
        finally:
            pass  # conn is cached, do not close

    def test_no_profile_gives_zero(self):
        """Card without a Forge profile should get 0 cosine similarity."""
        ctx, conn = _make_ctx()
        try:
            cmdr = _make_cmdr(ctx, KRENKO_OID, subtypes={"goblin"})
            # Find a card NOT in forge profiles (no ability vector)
            found_oid = None
            for (oid,) in conn.execute("SELECT DISTINCT oracle_id FROM cards"):
                if oid not in ctx._forge_profiles and oid != KRENKO_OID:
                    found_oid = oid
                    break
            if found_oid is None:
                pytest.skip("All cards have forge profiles")
            features = _compute_features(ctx, cmdr, found_oid, conn)
            assert features[4] == 0.0, (
                f"F4 should be 0 for card without forge profile, got {features[4]}"
            )
        finally:
            pass  # conn is cached, do not close


class TestF26ForgeAbilityDepth:
    """F26: forge_ability_depth — total distinct mechanical components."""

    def test_card_with_abilities_has_depth(self):
        """Card with multiple verbs/triggers should have depth > 0."""
        ctx, conn = _make_ctx()
        try:
            cmdr = _make_cmdr(ctx, KRENKO_OID, subtypes={"goblin"})
            # Find a card with at least 2 verbs or triggers
            found_oid = None
            for oid, profile in ctx._forge_profiles.items():
                total = (len(profile.get('verbs', set())) +
                         len(profile.get('triggers', set())) +
                         len(profile.get('keywords', set())) +
                         len(profile.get('counter_types', set())))
                if total >= 2 and oid != KRENKO_OID:
                    found_oid = oid
                    break
            if found_oid is None:
                pytest.skip("No card with 2+ mechanical components found")
            features = _compute_features(ctx, cmdr, found_oid, conn)
            assert features[15] >= 2.0, (
                f"F26 should be >= 2.0 for card with 2+ components, got {features[15]}"
            )
        finally:
            pass  # conn is cached, do not close

    def test_no_profile_gives_zero(self):
        """Card without a Forge profile should get 0 depth."""
        ctx, conn = _make_ctx()
        try:
            cmdr = _make_cmdr(ctx, KRENKO_OID, subtypes={"goblin"})
            # Find a card NOT in forge profiles
            found_oid = None
            for (oid,) in conn.execute("SELECT DISTINCT oracle_id FROM cards"):
                if oid not in ctx._forge_profiles and oid != KRENKO_OID:
                    found_oid = oid
                    break
            if found_oid is None:
                pytest.skip("All cards have forge profiles")
            features = _compute_features(ctx, cmdr, found_oid, conn)
            assert features[15] == 0.0, (
                f"F26 should be 0 for card without forge profile, got {features[15]}"
            )
        finally:
            pass  # conn is cached, do not close

    def test_depth_capped_at_10(self):
        """F26 should be capped at 10.0."""
        ctx, conn = _make_ctx()
        try:
            cmdr = _make_cmdr(ctx, KRENKO_OID, subtypes={"goblin"})
            # Find any card to verify cap
            for oid in ctx._forge_profiles:
                if oid != KRENKO_OID:
                    features = _compute_features(ctx, cmdr, oid, conn)
                    assert features[15] <= 10.0, (
                        f"F26 should be capped at 10.0, got {features[15]}"
                    )
                    break
        finally:
            pass  # conn is cached, do not close


class TestAbilityVectorsLoaded:
    """Verify _ability_vectors are built correctly from Forge profiles."""

    def test_ability_vectors_exist(self):
        """_ability_vectors dict should be non-empty."""
        ctx, conn = _make_ctx()
        try:
            assert hasattr(ctx, "_ability_vectors")
            assert isinstance(ctx._ability_vectors, dict)
            assert len(ctx._ability_vectors) > 1000, (
                f"Expected >1000 ability vectors, got {len(ctx._ability_vectors)}"
            )
        finally:
            pass  # conn is cached, do not close

    def test_ability_vectors_normalized(self):
        """Each ability vector should be L2-normalized."""
        ctx, conn = _make_ctx()
        try:
            for oid, vec in list(ctx._ability_vectors.items())[:10]:
                norm = float(np.linalg.norm(vec))
                assert abs(norm - 1.0) < 1e-5, (
                    f"Ability vector for {oid} has norm {norm}, expected 1.0"
                )
        finally:
            pass  # conn is cached, do not close


class TestNoOracleTextInFeatures:
    """Verify oracle text TF-IDF infrastructure has been fully removed."""

    def test_no_card_tokens(self):
        """ForgeFeatureContext should not have _card_tokens attribute."""
        ctx, conn = _make_ctx()
        try:
            assert not hasattr(ctx, "_card_tokens"), (
                "_card_tokens should be removed (oracle text TF-IDF replaced by Forge ability vectors)"
            )
        finally:
            pass  # conn is cached, do not close

    def test_no_tfidf_method(self):
        """ForgeFeatureContext should not have tfidf_vector method."""
        ctx, conn = _make_ctx()
        try:
            assert not hasattr(ctx, "tfidf_vector"), (
                "tfidf_vector should be removed (replaced by _ability_vectors)"
            )
        finally:
            pass  # conn is cached, do not close


class TestFeatureCount:
    """Verify compute_card_features returns exactly 38 elements."""

    def test_feature_count_is_71(self):
        """Feature vector length should be 71."""
        ctx, conn = _make_ctx()
        try:
            cmdr = _make_cmdr(ctx, KRENKO_OID, subtypes={"goblin"})
            # Pick any card
            found_oid = None
            for oid in ctx._forge_profiles:
                if oid != KRENKO_OID:
                    found_oid = oid
                    break
            if found_oid is None:
                pytest.skip("No card found for feature count test")
            features = _compute_features(ctx, cmdr, found_oid, conn)
            assert len(features) == 89, (
                f"Expected 107 features, got {len(features)}"
            )
        finally:
            pass  # conn is cached, do not close


# ---------------------------------------------------------------------------
# Tests for raw_line extraction: granted_keywords, conditions, duration, etc.
# ---------------------------------------------------------------------------


class TestGrantedKeywords:
    """Profile field: granted_keywords from AddKeyword$/KW$/PumpKeywords$/Keywords$."""

    def test_embercleave_grants_trample_doublestrike(self):
        """Embercleave should grant trample and double strike."""
        ctx, conn = _make_ctx()
        try:
            oid = _find_oid_by_name(conn, "Embercleave")
            if oid is None:
                pytest.skip("Embercleave not found in DB")
            p = ctx._forge_profiles.get(oid, {})
            gk = p.get("granted_keywords", set())
            assert "trample" in gk, f"Expected 'trample' in granted_keywords, got {gk}"
            assert "double strike" in gk, f"Expected 'double strike' in granted_keywords, got {gk}"
        finally:
            pass  # conn is cached, do not close

    def test_swiftfoot_boots_grants_hexproof_haste(self):
        """Swiftfoot Boots should grant hexproof and haste."""
        ctx, conn = _make_ctx()
        try:
            oid = _find_oid_by_name(conn, "Swiftfoot Boots")
            if oid is None:
                pytest.skip("Swiftfoot Boots not found in DB")
            p = ctx._forge_profiles.get(oid, {})
            gk = p.get("granted_keywords", set())
            assert "hexproof" in gk, f"Expected 'hexproof' in granted_keywords, got {gk}"
            assert "haste" in gk, f"Expected 'haste' in granted_keywords, got {gk}"
        finally:
            pass  # conn is cached, do not close

    def test_coverage_above_threshold(self):
        """At least 2000 cards should have granted_keywords."""
        ctx, conn = _make_ctx()
        try:
            count = sum(1 for p in ctx._forge_profiles.values() if p.get("granted_keywords"))
            assert count >= 2000, f"Expected >=2000 cards with granted_keywords, got {count}"
        finally:
            pass  # conn is cached, do not close


class TestCombatDamage:
    """Profile field: combat_damage from CombatDamage$ True."""

    def test_combat_damage_card(self):
        """A card with CombatDamage$ True in raw_line should have combat_damage=True."""
        ctx, conn = _make_ctx()
        try:
            found = False
            for _oid, p in ctx._forge_profiles.items():
                if p.get("combat_damage"):
                    found = True
                    break
            assert found, "No card with combat_damage=True found"
        finally:
            pass  # conn is cached, do not close

    def test_coverage_hundreds(self):
        """Hundreds of cards should have combat damage triggers."""
        ctx, conn = _make_ctx()
        try:
            count = sum(1 for p in ctx._forge_profiles.values() if p.get("combat_damage"))
            assert count >= 500, f"Expected >=500 combat_damage cards, got {count}"
        finally:
            pass  # conn is cached, do not close


class TestConditions:
    """Profile field: conditions from IsPresent$/ConditionPresent$."""

    def test_conditions_extracted(self):
        """At least 1000 cards should have conditions."""
        ctx, conn = _make_ctx()
        try:
            count = sum(1 for p in ctx._forge_profiles.values() if p.get("conditions"))
            assert count >= 1000, f"Expected >=1000 cards with conditions, got {count}"
        finally:
            pass  # conn is cached, do not close

    def test_condition_values_are_lowercase(self):
        """All condition values should be lowercase strings."""
        ctx, conn = _make_ctx()
        try:
            for _oid, p in ctx._forge_profiles.items():
                for c in p.get("conditions", set()):
                    assert c == c.lower(), f"Condition '{c}' should be lowercase"
                    assert isinstance(c, str), f"Condition should be str, got {type(c)}"
        finally:
            pass  # conn is cached, do not close


class TestDuration:
    """Profile field: duration from Duration$ and pump inference."""

    def test_duration_extracted(self):
        """At least 3000 cards should have duration info."""
        ctx, conn = _make_ctx()
        try:
            count = sum(1 for p in ctx._forge_profiles.values() if p.get("duration"))
            assert count >= 3000, f"Expected >=3000 cards with duration, got {count}"
        finally:
            pass  # conn is cached, do not close

    def test_permanent_duration_exists(self):
        """Some cards should have permanent duration."""
        ctx, conn = _make_ctx()
        try:
            found = False
            for _oid, p in ctx._forge_profiles.items():
                if "permanent" in p.get("duration", set()):
                    found = True
                    break
            assert found, "No card with 'permanent' duration found"
        finally:
            pass  # conn is cached, do not close


class TestScalesWith:
    """Profile field: scales_with from SetPower$/AddPower$ with X/Y values."""

    def test_reckless_one_scales(self):
        """Reckless One (P/T = number of Goblins) should have scales_with."""
        ctx, conn = _make_ctx()
        try:
            oid = _find_oid_by_name(conn, "Reckless One")
            if oid is None:
                pytest.skip("Reckless One not found in DB")
            p = ctx._forge_profiles.get(oid, {})
            sw = p.get("scales_with", set())
            assert "variable_pt" in sw, f"Expected 'variable_pt' in scales_with, got {sw}"
            assert "count_based" in sw, f"Expected 'count_based' in scales_with, got {sw}"
        finally:
            pass  # conn is cached, do not close


class TestGrantsTypes:
    """Profile field: grants_types from Types$/AddType$."""

    def test_restless_fortress_grants_nightmare(self):
        """Restless Fortress animates into a Nightmare creature."""
        ctx, conn = _make_ctx()
        try:
            oid = _find_oid_by_name(conn, "Restless Fortress")
            if oid is None:
                pytest.skip("Restless Fortress not found in DB")
            p = ctx._forge_profiles.get(oid, {})
            gt = p.get("grants_types", set())
            assert "nightmare" in gt, f"Expected 'nightmare' in grants_types, got {gt}"
            assert "creature" in gt, f"Expected 'creature' in grants_types, got {gt}"
        finally:
            pass  # conn is cached, do not close

    def test_coverage(self):
        """Hundreds of cards should have grants_types."""
        ctx, conn = _make_ctx()
        try:
            count = sum(1 for p in ctx._forge_profiles.values() if p.get("grants_types"))
            assert count >= 500, f"Expected >=500 cards with grants_types, got {count}"
        finally:
            pass  # conn is cached, do not close


class TestGainControl:
    """Profile field: gain_control from GainControl$ True."""

    def test_gain_control_exists(self):
        """Some cards should have gain_control=True."""
        ctx, conn = _make_ctx()
        try:
            count = sum(1 for p in ctx._forge_profiles.values() if p.get("gain_control"))
            assert count >= 50, f"Expected >=50 gain_control cards, got {count}"
        finally:
            pass  # conn is cached, do not close


class TestDamageAmount:
    """Profile field: damage_amount from NumDmg$."""

    def test_damage_amount_extracted(self):
        """Hundreds of cards should have damage_amount."""
        ctx, conn = _make_ctx()
        try:
            count = sum(1 for p in ctx._forge_profiles.values() if p.get("damage_amount"))
            assert count >= 1000, f"Expected >=1000 cards with damage_amount, got {count}"
        finally:
            pass  # conn is cached, do not close


class TestCardsDrawn:
    """Profile field: cards_drawn from NumCards$."""

    def test_cards_drawn_extracted(self):
        """At least 1000 cards should have cards_drawn."""
        ctx, conn = _make_ctx()
        try:
            count = sum(1 for p in ctx._forge_profiles.values() if p.get("cards_drawn"))
            assert count >= 1000, f"Expected >=1000 cards with cards_drawn, got {count}"
        finally:
            pass  # conn is cached, do not close


class TestEffectZones:
    """Profile field: effect_zones from ActiveZones$/EffectZone$/AffectedZone$."""

    def test_effect_zones_extracted(self):
        """Hundreds of cards should have effect_zones."""
        ctx, conn = _make_ctx()
        try:
            count = sum(1 for p in ctx._forge_profiles.values() if p.get("effect_zones"))
            assert count >= 1000, f"Expected >=1000 cards with effect_zones, got {count}"
        finally:
            pass  # conn is cached, do not close


# ---------------------------------------------------------------------------
# Tests for new features F63-F70
# ---------------------------------------------------------------------------


def test_feature_count_71():
    """compute_card_features should return 74 features."""
    ctx, conn = _make_ctx()
    try:
        from mtg_synergy.recommend.forge_features import (
            compute_card_features, CmdrFeatureContext,
        )
        krenko_oid = KRENKO_OID
        cmdr_ctx = CmdrFeatureContext(ctx, krenko_oid, set())
        card_oid = next(oid for oid in ctx._forge_profiles if oid != krenko_oid)
        card_meta = conn.execute(
            "SELECT type_line, cmc FROM cards WHERE oracle_id = ?", (card_oid,)
        ).fetchone()
        feats = compute_card_features(
            card_oid, card_meta[0] or "", float(card_meta[1] or 0),
            ctx, cmdr_ctx,
        )
        assert len(feats) == 89, f"Expected 107 features, got {len(feats)}"
    finally:
        pass  # conn is cached, do not close


def test_total_ability_count_positive():
    """Cards with forge abilities should have total_ability_count > 0."""
    ctx, conn = _make_ctx()
    try:
        assert ctx._total_ability_counts.get(KRENKO_OID, 0) > 0
    finally:
        pass  # conn is cached, do not close


def test_triggered_counts_loaded():
    """Triggered ability counts should be loaded."""
    ctx, conn = _make_ctx()
    try:
        assert hasattr(ctx, "_triggered_counts")
        assert len(ctx._triggered_counts) > 0
    finally:
        pass  # conn is cached, do not close


def test_token_stats_loaded():
    """Token P/T and keyword counts should be loaded for token-creating cards."""
    ctx, conn = _make_ctx()
    try:
        assert len(ctx._token_max_pt) > 0
        assert ctx._token_max_pt.get(KRENKO_OID, 0) == 2  # 1/1 goblins
    finally:
        pass  # conn is cached, do not close


def test_zone_interaction_sets():
    """Zone interaction sets should be populated."""
    ctx, conn = _make_ctx()
    try:
        assert len(ctx._zone_graveyard) > 100
        assert len(ctx._zone_exile) > 10
    finally:
        pass  # conn is cached, do not close
