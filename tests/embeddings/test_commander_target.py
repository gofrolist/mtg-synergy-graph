"""Unit 4 tests — commander target vector + hi-syn cache.

Covers:

* Happy path: single commander + EDHREC hi-syn list composes mean.
* Happy path: no EDHREC connection — target equals normalized
  commander vector.
* Happy path: partner pair — target composes both commanders' own
  vectors plus both commanders' hi-syn contributions.
* Happy path: ``hi_syn_limit`` honored — passing a smaller limit
  fetches only that many rows even when more exist.
* Edge case: commander absent from ``vectors`` but hi-syn matches
  present → target computed from hi-syn alone.
* Edge case: commander present but no hi-syn matches → target is
  commander vector, normalized.
* Edge case: empty ``commander_names`` tuple → ``None``.
* Edge case: all inputs missing → ``None``.
* Cache semantics: repeat call with same args hits the ``functools``
  cache; ``clear_cache()`` resets it.
* Error paths: missing table on EDHREC conn degrades to empty
  hi-syn; ``hi_syn_limit <= 0`` raises ``ValueError``.
* Integration: L2-normalized output is unit-length; hand-computed
  mean matches within 1e-6.
"""

from __future__ import annotations

import sqlite3

import numpy as np
import pytest

from mtg_synergy_graph.embeddings import commander_target as ct
from mtg_synergy_graph.validate import commander_to_slug

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_vector(seed: int, dim: int = 128) -> np.ndarray:
    """Deterministic L2-normalized float32 vector of a given dim."""
    rng = np.random.default_rng(seed)
    vec = rng.standard_normal(dim).astype(np.float32)
    norm = float(np.linalg.norm(vec))
    assert norm > 0.0
    return (vec / norm).astype(np.float32)


def _make_edhrec_db(
    rows: list[tuple[str, str, str, float]],
) -> sqlite3.Connection:
    """Build an in-memory ``edhrec_card_synergy`` table.

    Rows are ``(commander_slug, section, card_name, synergy)`` — the
    shape ``fetch_high_synergy_top_n_ordered`` consumes. Only the
    ``'High Synergy Cards'`` section is relevant to these tests.
    """
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE edhrec_card_synergy (
            commander_slug TEXT NOT NULL,
            section        TEXT NOT NULL,
            card_name      TEXT NOT NULL,
            synergy        REAL NOT NULL
        );
        """
    )
    conn.executemany(
        "INSERT INTO edhrec_card_synergy(commander_slug, section, card_name, synergy) VALUES (?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    return conn


@pytest.fixture(autouse=True)
def _reset_cache() -> None:
    """Reset the module-level ``functools.cache`` before every test.

    The cache retains ``sqlite3.Connection`` objects as keys across
    tests; clearing before each test prevents a retained reference from
    keeping a previous test's in-memory DB alive past its scope.
    """
    ct.clear_cache()
    yield
    ct.clear_cache()


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_golden_set_commander_target_matches_hand_computed_mean() -> None:
    """With EDHREC hi-syn, target = L2(mean(v_cmdr + hi-syn vectors))."""
    commander = "Korvold, Fae-Cursed King"
    slug = commander_to_slug(commander)
    vectors = {
        commander: _make_vector(seed=1),
        "Dockside Extortionist": _make_vector(seed=2),
        "Mayhem Devil": _make_vector(seed=3),
    }
    edhrec_rows = [
        (slug, "High Synergy Cards", "Dockside Extortionist", 0.9),
        (slug, "High Synergy Cards", "Mayhem Devil", 0.8),
    ]
    edhrec_conn = _make_edhrec_db(edhrec_rows)

    target = ct.build_commander_target_vector((commander,), vectors, edhrec_conn)

    expected = np.mean(
        np.stack(
            [
                vectors[commander],
                vectors["Dockside Extortionist"],
                vectors["Mayhem Devil"],
            ],
            axis=0,
        ),
        axis=0,
    )
    expected = expected / np.linalg.norm(expected)

    assert target is not None
    np.testing.assert_allclose(target, expected, atol=1e-6)
    # L2 norm ≈ 1.
    assert float(np.linalg.norm(target)) == pytest.approx(1.0, abs=1e-6)


def test_non_golden_set_commander_falls_back_to_commander_vector() -> None:
    """Without an EDHREC connection, target is L2-normalized commander vector."""
    commander = "Obscure Partner, Unknown"
    v = _make_vector(seed=7)
    vectors = {commander: v}

    target = ct.build_commander_target_vector((commander,), vectors, edhrec_conn=None)

    assert target is not None
    # v was already unit-norm, so L2-normalize returns v bitwise.
    np.testing.assert_allclose(target, v, atol=1e-7)


def test_partner_pair_composes_both_commanders_and_their_hi_syn() -> None:
    """Partner pair averages own vectors + both commanders' hi-syn matches."""
    a_name = "Halana, Kessig Ranger"
    b_name = "Alena, Kessig Trapper"
    a_slug = commander_to_slug(a_name)
    b_slug = commander_to_slug(b_name)
    vectors = {
        a_name: _make_vector(seed=11),
        b_name: _make_vector(seed=12),
        "Shared Hi Syn": _make_vector(seed=13),
        "Alena Only Hi Syn": _make_vector(seed=14),
    }
    edhrec_rows = [
        (a_slug, "High Synergy Cards", "Shared Hi Syn", 0.9),
        (b_slug, "High Synergy Cards", "Shared Hi Syn", 0.9),
        (b_slug, "High Synergy Cards", "Alena Only Hi Syn", 0.8),
    ]
    edhrec_conn = _make_edhrec_db(edhrec_rows)

    target = ct.build_commander_target_vector(
        (a_name, b_name),
        vectors,
        edhrec_conn,
    )

    # Expected: 4 vectors collected — both commanders + Shared twice +
    # Alena Only.  Shared counts once per commander query (the plan
    # spells this out: "each commander's hi-syn cards are all averaged
    # together", so double-counting when both partners share a hi-syn
    # is intentional, not a bug).
    expected = np.mean(
        np.stack(
            [
                vectors[a_name],
                vectors[b_name],
                vectors["Shared Hi Syn"],  # from a's query
                vectors["Shared Hi Syn"],  # from b's query
                vectors["Alena Only Hi Syn"],
            ],
            axis=0,
        ),
        axis=0,
    )
    expected = expected / np.linalg.norm(expected)

    assert target is not None
    np.testing.assert_allclose(target, expected, atol=1e-6)


def test_hi_syn_limit_is_honored() -> None:
    """Passing ``hi_syn_limit=2`` fetches only the top-2 hi-syn rows."""
    commander = "Muldrotha, the Gravetide"
    slug = commander_to_slug(commander)
    vectors = {
        commander: _make_vector(seed=21),
        "HiSyn 1": _make_vector(seed=22),
        "HiSyn 2": _make_vector(seed=23),
        "HiSyn 3": _make_vector(seed=24),
        "HiSyn 4": _make_vector(seed=25),
        "HiSyn 5": _make_vector(seed=26),
    }
    edhrec_rows = [(slug, "High Synergy Cards", f"HiSyn {i}", 1.0 - i * 0.1) for i in range(1, 6)]
    edhrec_conn = _make_edhrec_db(edhrec_rows)

    target = ct.build_commander_target_vector(
        (commander,),
        vectors,
        edhrec_conn,
        hi_syn_limit=2,
    )

    expected = np.mean(
        np.stack(
            [
                vectors[commander],
                vectors["HiSyn 1"],
                vectors["HiSyn 2"],
            ],
            axis=0,
        ),
        axis=0,
    )
    expected = expected / np.linalg.norm(expected)

    assert target is not None
    np.testing.assert_allclose(target, expected, atol=1e-6)


# ---------------------------------------------------------------------------
# Edge cases — missing vectors / empty inputs
# ---------------------------------------------------------------------------


def test_commander_missing_but_hi_syn_present_uses_hi_syn_alone() -> None:
    """Commander absent from ``vectors`` still yields a target from hi-syn."""
    commander = "Day Zero Commander"
    slug = commander_to_slug(commander)
    vectors = {
        "Some Hi Syn Card": _make_vector(seed=31),
    }
    edhrec_conn = _make_edhrec_db([(slug, "High Synergy Cards", "Some Hi Syn Card", 0.9)])

    target = ct.build_commander_target_vector((commander,), vectors, edhrec_conn)

    assert target is not None
    # Only one collected vector, so target ≈ that vector (it was already unit).
    np.testing.assert_allclose(target, vectors["Some Hi Syn Card"], atol=1e-7)


def test_commander_present_but_no_hi_syn_matches_uses_commander_only() -> None:
    """Hi-syn names that aren't in ``vectors`` are silently skipped."""
    commander = "Kess, Dissident Mage"
    slug = commander_to_slug(commander)
    vectors = {
        commander: _make_vector(seed=41),
        # Notably missing: "Mystical Tutor" below.
    }
    edhrec_conn = _make_edhrec_db([(slug, "High Synergy Cards", "Mystical Tutor", 0.9)])

    target = ct.build_commander_target_vector((commander,), vectors, edhrec_conn)

    assert target is not None
    # Target equals normalized commander vector (only collected one).
    np.testing.assert_allclose(target, vectors[commander], atol=1e-7)


def test_no_vectors_at_all_returns_none() -> None:
    """Neither commander nor hi-syn match → returns ``None``."""
    commander = "Nonexistent Commander"
    slug = commander_to_slug(commander)
    edhrec_conn = _make_edhrec_db([(slug, "High Synergy Cards", "Also Missing Card", 0.9)])
    vectors: dict[str, np.ndarray] = {}  # nothing populated

    target = ct.build_commander_target_vector((commander,), vectors, edhrec_conn)

    assert target is None


def test_empty_commander_names_returns_none() -> None:
    target = ct.build_commander_target_vector(
        commander_names=(),
        vectors={"Any Card": _make_vector(seed=99)},
        edhrec_conn=None,
    )
    assert target is None


# ---------------------------------------------------------------------------
# Cache semantics
# ---------------------------------------------------------------------------


def test_functools_cache_hits_on_repeated_call() -> None:
    """Calling twice with the same conn/commander/limit hits the cache."""
    commander = "Korvold, Fae-Cursed King"
    slug = commander_to_slug(commander)
    edhrec_conn = _make_edhrec_db([(slug, "High Synergy Cards", "Dockside Extortionist", 0.9)])
    vectors = {
        commander: _make_vector(seed=51),
        "Dockside Extortionist": _make_vector(seed=52),
    }

    before = ct._fetch_hi_syn_names_cached.cache_info()

    ct.build_commander_target_vector((commander,), vectors, edhrec_conn)
    after_first = ct._fetch_hi_syn_names_cached.cache_info()
    assert after_first.misses == before.misses + 1, "first call must miss"

    ct.build_commander_target_vector((commander,), vectors, edhrec_conn)
    after_second = ct._fetch_hi_syn_names_cached.cache_info()
    assert after_second.hits == after_first.hits + 1, "second call must hit"
    assert after_second.misses == after_first.misses, "second call must not miss"


def test_clear_cache_resets_cache_info() -> None:
    commander = "Any, Cached Commander"
    slug = commander_to_slug(commander)
    edhrec_conn = _make_edhrec_db([(slug, "High Synergy Cards", "HS", 0.9)])
    vectors = {commander: _make_vector(seed=61), "HS": _make_vector(seed=62)}

    ct.build_commander_target_vector((commander,), vectors, edhrec_conn)
    assert ct._fetch_hi_syn_names_cached.cache_info().currsize > 0

    ct.clear_cache()
    info = ct._fetch_hi_syn_names_cached.cache_info()
    assert info.hits == 0
    assert info.misses == 0
    assert info.currsize == 0


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


def test_missing_table_degrades_to_commander_vector() -> None:
    """A DB without the table logs at DEBUG and falls back to commander vec."""
    commander = "Broken DB Commander"
    vectors = {commander: _make_vector(seed=71)}

    # Connection with no edhrec_card_synergy table at all.
    broken_conn = sqlite3.connect(":memory:")

    target = ct.build_commander_target_vector(
        (commander,),
        vectors,
        broken_conn,
    )

    assert target is not None
    np.testing.assert_allclose(target, vectors[commander], atol=1e-7)


def test_hi_syn_limit_zero_raises() -> None:
    with pytest.raises(ValueError, match="hi_syn_limit must be >= 1"):
        ct.build_commander_target_vector(
            ("Any",),
            {},
            edhrec_conn=None,
            hi_syn_limit=0,
        )


def test_hi_syn_limit_negative_raises() -> None:
    with pytest.raises(ValueError, match="hi_syn_limit must be >= 1"):
        ct.build_commander_target_vector(
            ("Any",),
            {},
            edhrec_conn=None,
            hi_syn_limit=-3,
        )


# ---------------------------------------------------------------------------
# Integration
# ---------------------------------------------------------------------------


def test_full_pipeline_korvold_10_hi_syn_target_is_unit_length() -> None:
    """End-to-end: 10 hi-syn rows + own vector → unit-length deterministic target."""
    commander = "Korvold, Fae-Cursed King"
    slug = commander_to_slug(commander)
    hi_syn_names = [f"HS Card {i}" for i in range(1, 11)]
    vectors = {commander: _make_vector(seed=100)}
    vectors.update({name: _make_vector(seed=200 + i) for i, name in enumerate(hi_syn_names)})
    rows = [(slug, "High Synergy Cards", name, 1.0 - i * 0.05) for i, name in enumerate(hi_syn_names)]
    edhrec_conn = _make_edhrec_db(rows)

    target_a = ct.build_commander_target_vector((commander,), vectors, edhrec_conn, hi_syn_limit=10)
    # Clear cache and recompute — determinism check.
    ct.clear_cache()
    target_b = ct.build_commander_target_vector((commander,), vectors, edhrec_conn, hi_syn_limit=10)

    assert target_a is not None and target_b is not None
    np.testing.assert_array_equal(target_a, target_b)
    assert float(np.linalg.norm(target_a)) == pytest.approx(1.0, abs=1e-6)


def test_commander_name_case_sensitivity_is_deterministic() -> None:
    """Cache is keyed by exact commander name string.

    The cache does not normalize names — calling with different cases
    produces independent cache entries. Tests confirm both calls land
    (same fetch path, different slugs).
    """
    a = "Korvold, Fae-Cursed King"
    b = "korvold, fae-cursed king"
    vectors = {a: _make_vector(seed=301), b: _make_vector(seed=302)}
    # Both slugs happen to be identical, because commander_to_slug
    # lowercases; the underlying SQL will return the same rows. But
    # from the cache's perspective, different string keys = different
    # entries.
    edhrec_conn = _make_edhrec_db([(commander_to_slug(a), "High Synergy Cards", "Side Card", 0.9)])
    vectors["Side Card"] = _make_vector(seed=303)

    ct.build_commander_target_vector((a,), vectors, edhrec_conn)
    ct.build_commander_target_vector((b,), vectors, edhrec_conn)

    info = ct._fetch_hi_syn_names_cached.cache_info()
    # Two distinct commander-name keys → two misses.
    assert info.misses == 2
