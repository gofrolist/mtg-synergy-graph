"""End-to-end recommendation quality tests.

Runs the FULL pipeline (model + scoring + penalties) for sample commanders
and checks for known anti-patterns. Unlike NDCG (which only tests the model),
these tests validate the complete recommendation output.

Requires: trained model at data/fusion_model_forge.lgb
          populated DB at data/tags.db
"""
import os
import sqlite3
import sys

import pytest

# Skip entire module if model or DB don't exist
DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "tags.db")
MODEL_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "fusion_model_forge.lgb")

pytestmark = pytest.mark.skipif(
    not os.path.exists(MODEL_PATH) or not os.path.exists(DB_PATH)
    or os.path.getsize(DB_PATH) < 1_000_000,
    reason="Requires trained model and populated DB"
)

# Add scripts/ to path for validator imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))


@pytest.fixture(scope="module")
def pipeline_context():
    """Load model, context, and recommendations for test commanders once."""
    from mtg_synergy.recommend.scoring import batch_recommend
    from mtg_synergy.recommend.forge_features import ForgeFeatureContext

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # Diverse set of commanders covering different archetypes
    test_commanders = [
        "Kyler, Sigardian Emissary",    # GW +1/+1 counters, humans
        "Krenko, Mob Boss",              # Mono-R goblins, tokens
        "Atraxa, Praetors' Voice",       # 4-color superfriends/counters
        "Muldrotha, the Gravetide",      # Sultai graveyard
        "Isshin, Two Heavens as One",    # Mardu attack triggers
    ]

    # Filter to commanders that exist in DB
    existing = []
    for name in test_commanders:
        row = conn.execute("SELECT name FROM cards WHERE name = ?", (name,)).fetchone()
        if row:
            existing.append(name)

    if not existing:
        pytest.skip("No test commanders found in DB")

    all_recs = batch_recommend(conn, existing, top_n=30, verbose=False)
    ctx = ForgeFeatureContext(conn, preload_edges=False)

    yield conn, ctx, all_recs, existing
    conn.close()


def _get_card_info(conn, ctx, card_name):
    """Get card profile and text for a recommended card."""
    row = conn.execute(
        "SELECT oracle_id, type_line, oracle_text FROM cards WHERE name = ?",
        (card_name,)
    ).fetchone()
    if not row:
        return None, None, None, {}
    oid, tl, text = row
    profile = ctx._forge_profiles.get(oid, {})
    return oid, tl or "", text or "", profile


def _get_cmdr_info(conn, ctx, cmdr_name):
    """Get commander profile and subtypes."""
    from mtg_synergy.config import extract_subtypes
    row = conn.execute(
        "SELECT oracle_id, type_line FROM cards WHERE name = ?", (cmdr_name,)
    ).fetchone()
    if not row:
        return None, set(), {}
    oid = row[0]
    subtypes = extract_subtypes(row[1] or "")
    profile = ctx._forge_profiles.get(oid, {})
    return oid, subtypes, profile


class TestNoWrongCounterTypes:
    """Cards with wrong counter types should not appear in top recommendations."""

    def test_no_time_counters_for_p1p1_commanders(self, pipeline_context):
        conn, ctx, all_recs, commanders = pipeline_context
        for cmdr in commanders:
            _, _, cmdr_profile = _get_cmdr_info(conn, ctx, cmdr)
            if "P1P1" not in cmdr_profile.get("counter_types", set()):
                continue
            recs = all_recs.get(cmdr, [])
            for rank, (name, score) in enumerate(recs[:20], 1):
                _, _, _, profile = _get_card_info(conn, ctx, name)
                counters = profile.get("counter_types", set())
                specific = counters - {"All", "Any", "EachFromSource", "EachType"}
                if specific and "P1P1" not in specific:
                    pytest.fail(
                        f"{cmdr} #{rank}: {name} uses {specific} counters "
                        f"(commander wants P1P1)"
                    )

    def test_no_niche_counters_without_commander_match(self, pipeline_context):
        conn, ctx, all_recs, commanders = pipeline_context
        niche = {"TIME", "EXPERIENCE", "ENERGY"}
        for cmdr in commanders:
            _, _, cmdr_profile = _get_cmdr_info(conn, ctx, cmdr)
            cmdr_counters = cmdr_profile.get("counter_types", set())
            recs = all_recs.get(cmdr, [])
            for rank, (name, score) in enumerate(recs[:20], 1):
                _, _, _, profile = _get_card_info(conn, ctx, name)
                card_counters = profile.get("counter_types", set())
                card_niche = card_counters & niche
                if card_niche and not (card_niche & cmdr_counters):
                    other = card_counters - niche - {"All", "Any", "EachFromSource", "EachType"}
                    if not other:  # niche is the card's core mechanic
                        pytest.fail(
                            f"{cmdr} #{rank}: {name} uses {card_niche} counters "
                            f"(commander doesn't)"
                        )


class TestNoPartnerCards:
    """Partner-type cards should never appear as recommendations."""

    def test_no_choose_a_background(self, pipeline_context):
        conn, ctx, all_recs, commanders = pipeline_context
        for cmdr in commanders:
            recs = all_recs.get(cmdr, [])
            for rank, (name, score) in enumerate(recs, 1):
                _, _, _, profile = _get_card_info(conn, ctx, name)
                kws = profile.get("keywords", set())
                assert "Choose a Background" not in kws, \
                    f"{cmdr} #{rank}: {name} is 'Choose a Background' partner"

    def test_no_doctors_companion(self, pipeline_context):
        conn, ctx, all_recs, commanders = pipeline_context
        for cmdr in commanders:
            recs = all_recs.get(cmdr, [])
            for rank, (name, score) in enumerate(recs, 1):
                _, _, _, profile = _get_card_info(conn, ctx, name)
                kws = profile.get("keywords", set())
                assert "Doctor's companion" not in kws, \
                    f"{cmdr} #{rank}: {name} is Doctor's companion"


class TestNoExcludedTypes:
    """Cards excluding the commander's creature type should be penalized."""

    def test_no_excluded_subtypes_in_top10(self, pipeline_context):
        conn, ctx, all_recs, commanders = pipeline_context
        for cmdr in commanders:
            _, cmdr_subtypes, _ = _get_cmdr_info(conn, ctx, cmdr)
            if not cmdr_subtypes:
                continue
            recs = all_recs.get(cmdr, [])
            for rank, (name, score) in enumerate(recs[:10], 1):
                _, _, _, profile = _get_card_info(conn, ctx, name)
                excl = profile.get("excluded_subtypes", set())
                overlap = excl & cmdr_subtypes
                if overlap:
                    pytest.fail(
                        f"{cmdr} #{rank}: {name} excludes {overlap} "
                        f"(commander is {cmdr_subtypes})"
                    )


class TestNoTribalMismatch:
    """Cards requiring specific creature types the commander doesn't have."""

    def test_no_wrong_tribal_in_top10(self, pipeline_context):
        conn, ctx, all_recs, commanders = pipeline_context
        generic = {"card", "creature", "permanent", "self", "other", "nontoken",
                   "token", "artifact", "enchantment", "land", "spell", "any"}
        for cmdr in commanders:
            _, cmdr_subtypes, _ = _get_cmdr_info(conn, ctx, cmdr)
            if not cmdr_subtypes:
                continue
            recs = all_recs.get(cmdr, [])
            for rank, (name, score) in enumerate(recs[:10], 1):
                _, _, _, profile = _get_card_info(conn, ctx, name)
                req = profile.get("required_subtypes", set())
                specific = req - generic
                specific_lower = {s.lower() for s in specific}
                cmdr_lower = {s.lower() for s in cmdr_subtypes}
                if specific_lower and not (specific_lower & cmdr_lower):
                    pytest.fail(
                        f"{cmdr} #{rank}: {name} requires {specific} "
                        f"(commander is {cmdr_subtypes})"
                    )


class TestFullValidation:
    """Run the complete validation suite on all test commanders."""

    def test_zero_flags_on_test_commanders(self, pipeline_context):
        from validate_recommendations import validate_commanders, check_card

        conn, ctx, all_recs, commanders = pipeline_context
        results = validate_commanders(conn, commanders, ctx, all_recs)

        all_issues = []
        for cmdr, issues in results:
            for rank, name, score, warnings in issues:
                all_issues.append(f"{cmdr} #{rank}: {name} — {'; '.join(warnings)}")

        assert not all_issues, (
            f"Found {len(all_issues)} suspicious recommendations:\n"
            + "\n".join(all_issues)
        )
